import os
import tempfile
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv

from memory import load_all_conversations, clear_history
from agents.riley import chat_with_riley
from outreach_runner import (
    process_contact,
    send_approved_email,
    skip_contact,
    format_draft_for_slack,
    generate_summary,
    is_auto_mode,
    set_auto_mode
)
from tools.file_reader import read_contact_list
from interaction_log import (
    log_action,
    get_recent_logs,
    format_logs_for_slack
)

load_dotenv()

# ─────────────────────────────────────────
# SLACK APP
# ─────────────────────────────────────────

app = App(
    token=os.environ.get("RILEY_BOT_TOKEN"),
    signing_secret=os.environ.get("RILEY_SIGNING_SECRET")
)

slack_client = WebClient(
    token=os.environ.get("RILEY_BOT_TOKEN")
)

# ─────────────────────────────────────────
# APPROVAL STATE
# Tracks pending drafts and remaining contacts
# per user — stored in RAM
#
# Structure:
# {
#   "slack_user_id": {
#     "pending_result":     None or { contact, draft, subject, body }
#     "remaining_contacts": [ ... ]
#     "stats":              { sent, skipped, failed }
#     "waiting":            True/False — is Riley waiting for your reply?
#   }
# }
# ─────────────────────────────────────────

approval_state = {}


# ─────────────────────────────────────────
# DOWNLOAD FILE FROM SLACK
# ─────────────────────────────────────────

def download_slack_file(file_info: dict) -> str:
    """Downloads uploaded file from Slack to a temp file."""
    import requests
    file_url  = file_info["url_private_download"]
    file_name = file_info["name"]
    headers   = {
        "Authorization": (
            f"Bearer {os.environ.get('RILEY_BOT_TOKEN')}"
        )
    }
    response = requests.get(file_url, headers=headers)
    suffix   = ".csv" if file_name.endswith(".csv") else ".xlsx"
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix
    ) as tmp:
        tmp.write(response.content)
        return tmp.name


# ─────────────────────────────────────────
# POST DRAFT AND WAIT FOR APPROVAL
# Called after each draft is ready
# Sets waiting=True so handle_dm knows
# the next message is an approval reply
# ─────────────────────────────────────────

def post_draft_for_approval(user_id: str, result: dict, say):
    """
    Posts the draft to Slack and marks state as waiting.
    handle_dm checks the waiting flag to route replies
    to the approval handler instead of general chat.
    """
    if user_id not in approval_state:
        return

    # Store the pending result
    approval_state[user_id]["pending_result"] = result
    # Set waiting flag — critical for approval routing
    approval_state[user_id]["waiting"] = True

    # Post the draft
    say(format_draft_for_slack(result))


# ─────────────────────────────────────────
# PROCESS NEXT CONTACT
# Always runs in a new background thread
# ─────────────────────────────────────────

def process_next_contact(user_id: str, say):
    """
    Picks next contact, researches, drafts.
    Either posts for approval or auto-sends.
    Runs in background thread — never blocks Slack.
    """
    def _run():
        if user_id not in approval_state:
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]

        # No more contacts — run complete
        if not remaining:
            total = (
                stats["sent"] +
                stats["skipped"] +
                stats["failed"]
            )
            say(generate_summary(
                total=total,
                sent=stats["sent"],
                skipped=stats["skipped"],
                failed=stats["failed"]
            ))
            del approval_state[user_id]
            return

        # Take next contact off the list
        contact = remaining.pop(0)

        # Research + draft
        result = process_contact(user_id, contact, say)

        if result is None:
            # Something went wrong — move on
            stats["failed"] += 1
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
            return

        if is_auto_mode(user_id):
            # AUTO-SEND — send immediately, no approval
            success = send_approved_email(result)
            if success:
                stats["sent"] += 1
                say(
                    f"✅ Sent to *{contact['name']}* "
                    f"at *{contact['business_name']}*"
                )
            else:
                stats["failed"] += 1
                say(
                    f"❌ Failed to send to "
                    f"*{contact['name']}*. Moving on."
                )
            # Move to next contact
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()

        else:
            # APPROVAL MODE — post draft and wait
            # Uses post_draft_for_approval which sets
            # the waiting flag so handle_dm routes correctly
            post_draft_for_approval(user_id, result, say)

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ─────────────────────────────────────────
# FILE UPLOAD HANDLER
# ─────────────────────────────────────────

def handle_file_upload(event: dict, say, user_id: str):
    """Handles CSV/Excel upload — reads contacts, starts run."""
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    if not file_name.endswith((".csv", ".xlsx", ".xls")):
        say(
            "⚠️ I can only read CSV or Excel files "
            "(.csv, .xlsx). Please upload one of those."
        )
        return

    say(
        f"📂 Got your file — *{file_name}*. "
        f"Reading the contact list..."
    )

    try:
        file_path = download_slack_file(file_info)
        contacts  = read_contact_list(file_path, user_id)

        if not contacts:
            say(
                "⚠️ The file was empty or "
                "had no valid contacts."
            )
            return

        mode_msg = (
            "⚡ *Auto-send mode is ON* — "
            "I'll send emails without asking for approval."
            if is_auto_mode(user_id)
            else
            "✋ *Approval mode is ON* — "
            "I'll show you each draft before sending."
        )

        say(
            f"✅ Found *{len(contacts)} contacts*. "
            f"Starting outreach run now.\n\n"
            f"{mode_msg}\n\n"
            f"_Type *!automode on* or *!automode off* "
            f"to switch modes at any time._"
        )

        # Initialise approval state for this user
        approval_state[user_id] = {
            "pending_result":     None,
            "remaining_contacts": contacts,
            "waiting":            False,
            "stats": {
                "sent":    0,
                "skipped": 0,
                "failed":  0
            }
        }

        # Start the outreach loop
        process_next_contact(user_id, say)

    except ValueError as e:
        say(f"⚠️ Problem with your file: {e}")

    except Exception as e:
        say(f"❌ Something went wrong: {e}")
        log_action(
            action_type="error",
            detail=f"File upload error: {str(e)}"
        )


# ─────────────────────────────────────────
# HANDLE APPROVAL REPLY
# Separated from handle_dm for clarity
# Called when waiting=True and user sends a message
# ─────────────────────────────────────────

def handle_approval_reply(
    user_id: str,
    text:    str,
    say
):
    """
    Handles your reply when Riley is waiting for approval.
    Three cases:
      "approve" → send the email
      "skip"    → skip this contact
      anything else → treat as edit instructions, redraft
    """
    state  = approval_state[user_id]
    result = state["pending_result"]
    stats  = state["stats"]

    # Clear waiting flag immediately
    # So if redraft fails, user isn't stuck
    state["waiting"] = False
    state["pending_result"] = None

    # ── APPROVE ──────────────────────────
    if text.lower() == "approve":
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            contact = result["contact"]
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*. "
                f"Moving to next contact..."
            )
        else:
            stats["failed"] += 1
            say(
                "❌ Send failed. "
                "Moving to next contact..."
            )
        process_next_contact(user_id, say)
        return

    # ── SKIP ─────────────────────────────
    if text.lower() == "skip":
        skip_contact(result)
        stats["skipped"] += 1
        contact = result["contact"]
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next contact..."
        )
        process_next_contact(user_id, say)
        return

    # ── EDIT INSTRUCTIONS ────────────────
    # Anything else = feedback to redraft with
    say("Got it — redrafting with your feedback...")
    contact = result["contact"]

    from agents.riley import (
        chat_with_riley,
        parse_draft
    )

    try:
        feedback_task = (
            f"The CEO gave this feedback on the draft: "
            f'"{text}"\n\n'
            f"Original draft:\n{result['draft']}\n\n"
            f"Please redraft incorporating this feedback."
        )
        new_draft             = chat_with_riley(
            user_id, feedback_task
        )
        new_subject, new_body = parse_draft(new_draft)

        new_result = {
            "contact": contact,
            "draft":   new_draft,
            "subject": new_subject,
            "body":    new_body
        }

        # Post new draft and wait again
        post_draft_for_approval(user_id, new_result, say)

    except Exception as e:
        say(
            f"⚠️ Redraft failed: {e}. \n"
            f"Reply *approve* to send the original "
            f"or *skip* to skip this contact."
        )
        # Restore original result so approve/skip still work
        state["pending_result"] = result
        state["waiting"]        = True


# ─────────────────────────────────────────
# MAIN MESSAGE HANDLER
# ─────────────────────────────────────────

@app.event("message")
def handle_dm(event, say):
    """
    Central handler for all Slack DMs.
    Routes to correct function based on content and state.

    Routing order:
    1. Ignore bot messages
    2. Only handle DMs
    3. File upload → handle_file_upload
    4. Commands → handle directly
    5. Waiting for approval → handle_approval_reply
    6. General chat → chat_with_riley
    """
    # Ignore bot messages — prevents infinite loops
    if event.get("bot_id"):
        return

    # Only handle direct messages
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text    = event.get("text", "").strip()

    # ── FILE UPLOAD ──────────────────────
    if event.get("files"):
        handle_file_upload(event, say, user_id)
        return

    # ── COMMANDS ─────────────────────────
    # Commands work even during an outreach run

    if text.lower() == "!reset":
        clear_history("riley", user_id)
        say(
            "🔄 Memory cleared. Starting fresh — "
            "I won't remember our previous conversations."
        )
        return

    if text.lower() == "!status":
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    if text.lower() == "!automode on":
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send mode ON* — emails go out "
            "immediately without approval. "
            "Type *!automode off* to switch back."
        )
        return

    if text.lower() == "!automode off":
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — I'll show you "
            "each draft before sending."
        )
        return

    # ── APPROVAL FLOW ────────────────────
    # Check the waiting flag — much more reliable
    # than checking pending_result which can be
    # None momentarily due to threading timing

    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        handle_approval_reply(user_id, text, say)
        return

    # ── GENERAL CHAT ─────────────────────
    say("_Thinking..._")
    reply = chat_with_riley(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Riley is starting up...")
    print("📚 Loading conversation history from Supabase...")
    load_all_conversations()
    print("🤖 Connecting to Slack...")
    handler = SocketModeHandler(
        app,
        os.environ.get("RILEY_APP_TOKEN")
    )
    print("✅ Riley is live. DM Riley in Slack to start.")
    handler.start()