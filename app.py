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
from interaction_log import log_action, get_recent_logs, format_logs_for_slack

load_dotenv()

# ─────────────────────────────────────────
# INITIALISE SLACK BOLT APP
# ─────────────────────────────────────────

app = App(
    token=os.environ.get("RILEY_BOT_TOKEN"),
    signing_secret=os.environ.get("RILEY_SIGNING_SECRET")
)

slack_client = WebClient(token=os.environ.get("RILEY_BOT_TOKEN"))

# ─────────────────────────────────────────
# APPROVAL FLOW STATE
# Tracks who is waiting for approval
# and what the pending result is
#
# Format:
# {
#   "slack_user_id": {
#     "pending_result": { contact, draft, subject, body },
#     "remaining_contacts": [ ... ],
#     "stats": { sent, skipped, failed }
#   }
# }
# ─────────────────────────────────────────

approval_state = {}


# ─────────────────────────────────────────
# HELPER — DOWNLOAD FILE FROM SLACK
# ─────────────────────────────────────────

def download_slack_file(file_info: dict) -> str:
    """
    Downloads a file uploaded to Slack.
    Saves it to a temp file and returns the path.
    Riley reads it from there.
    """
    file_url  = file_info["url_private_download"]
    file_name = file_info["name"]

    # Download using bot token for auth
    import requests
    headers  = {"Authorization": f"Bearer {os.environ.get('RILEY_BOT_TOKEN')}"}
    response = requests.get(file_url, headers=headers)

    # Save to a temporary file
    suffix = ".csv" if file_name.endswith(".csv") else ".xlsx"
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix
    ) as tmp:
        tmp.write(response.content)
        return tmp.name


# ─────────────────────────────────────────
# HELPER — RUN OUTREACH FOR NEXT CONTACT
# Called after each approve/skip to move
# to the next contact in the list
# ─────────────────────────────────────────

def process_next_contact(user_id: str, say):
    """
    Picks the next contact from the remaining list,
    processes it (research + draft), then either:
    - Posts draft for approval (approval mode)
    - Sends immediately (auto-send mode)
    """
    state     = approval_state.get(user_id)
    remaining = state["remaining_contacts"]
    stats     = state["stats"]

    # No more contacts — run is complete
    if not remaining:
        total = stats["sent"] + stats["skipped"] + stats["failed"]
        say(generate_summary(
            total=total,
            sent=stats["sent"],
            skipped=stats["skipped"],
            failed=stats["failed"]
        ))
        # Clean up state
        del approval_state[user_id]
        return

    # Take the next contact off the list
    contact = remaining.pop(0)

    # Research + draft
    result = process_contact(user_id, contact, say)

    if result is None:
        # Something went wrong — skip and move on
        stats["failed"] += 1
        process_next_contact(user_id, say)
        return

    if is_auto_mode(user_id):
        # AUTO-SEND — send immediately, no approval needed
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
                f"❌ Failed to send to *{contact['name']}*. "
                f"Moving on."
            )
        # Immediately process the next one
        process_next_contact(user_id, say)

    else:
        # APPROVAL MODE — post draft and wait
        state["pending_result"] = result
        say(format_draft_for_slack(result))


# ─────────────────────────────────────────
# MAIN EVENT HANDLER
# All Slack DM messages come through here
# ─────────────────────────────────────────

@app.event("message")
def handle_dm(event, say):
    """
    Central handler for all DM messages.
    Runs every time you send Riley a message.
    """
    # Ignore messages from bots (including Riley itself)
    # This prevents infinite loops
    if event.get("bot_id"):
        return

    # Only handle direct messages
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text    = event.get("text", "").strip()

    # ── FILE UPLOAD ──────────────────────────
    # You uploaded a CSV/Excel contact list
    if event.get("files"):
        handle_file_upload(event, say, user_id)
        return

    # ── COMMANDS ─────────────────────────────

    # /reset — clear conversation memory
    if text.lower() == "/reset":
        clear_history("riley", user_id)
        say(
            "🔄 Memory cleared. Starting fresh — "
            "I won't remember our previous conversations."
        )
        return

    # /status — show recent activity log
    if text.lower() == "/status":
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    # /automode on — switch to auto-send
    if text.lower() == "/automode on":
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send mode ON* — I'll send emails "
            "immediately without asking for approval. "
            "Type `/automode off` to switch back."
        )
        return

    # /automode off — switch to approval mode
    if text.lower() == "/automode off":
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — I'll show you each "
            "draft and wait for your approval before sending."
        )
        return

    # ── APPROVAL FLOW RESPONSES ───────────────
    # You're in the middle of an outreach run
    # and replying to a draft

    if user_id in approval_state and \
       approval_state[user_id].get("pending_result"):

        state  = approval_state[user_id]
        result = state["pending_result"]
        stats  = state["stats"]

        # APPROVE — send this email
        if text.lower() == "approve":
            state["pending_result"] = None
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
                say("❌ Send failed. Moving to next contact...")

            # Move to the next contact
            process_next_contact(user_id, say)
            return

        # SKIP — don't send this email
        if text.lower() == "skip":
            state["pending_result"] = None
            skip_contact(result)
            stats["skipped"] += 1
            contact = result["contact"]
            say(
                f"⏭️ Skipped *{contact['name']}*. "
                f"Moving to next contact..."
            )
            process_next_contact(user_id, say)
            return

        # EDIT INSTRUCTIONS — redraft with feedback
        # Treat anything else as editing instructions
        say("Got it — redrafting with your feedback...")
        contact = result["contact"]

        # Add feedback to context and redraft
        from agents.riley import draft_outreach_email, parse_draft
        from tools.research import research_business

        try:
            research = research_business(contact["business_name"])
            # Include original draft + feedback in the task
            feedback_task = (
                f"The CEO gave this feedback on the draft: "
                f'"{text}"\n\n'
                f"Original draft:\n{result['draft']}\n\n"
                f"Please redraft incorporating the feedback."
            )
            new_draft = chat_with_riley(user_id, feedback_task)
            new_subject, new_body = parse_draft(new_draft)

            # Update the pending result
            state["pending_result"] = {
                "contact": contact,
                "draft":   new_draft,
                "subject": new_subject,
                "body":    new_body
            }

            say(format_draft_for_slack(state["pending_result"]))

        except Exception as e:
            say(f"⚠️ Redraft failed: {e}. "
                f"Reply *skip* to skip or *approve* to send original.")
        return

    # ── GENERAL CHAT ─────────────────────────
    # Normal conversation with Riley
    say("_Thinking..._")
    reply = chat_with_riley(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# FILE UPLOAD HANDLER
# Separated for clarity
# ─────────────────────────────────────────

def handle_file_upload(event: dict, say, user_id: str):
    """
    Handles a CSV/Excel file upload from Slack.
    Downloads the file, reads contacts, starts outreach run.
    """
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    # Check it's a supported file type
    if not file_name.endswith((".csv", ".xlsx", ".xls")):
        say(
            "⚠️ I can only read CSV or Excel files (.csv, .xlsx). "
            "Please upload one of those."
        )
        return

    say(
        f"📂 Got your file — *{file_name}*. "
        f"Reading the contact list..."
    )

    try:
        # Download and read the file
        file_path = download_slack_file(file_info)
        contacts  = read_contact_list(file_path)

        if not contacts:
            say("⚠️ The file was empty or had no valid contacts.")
            return

        # Check current mode and tell the user
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
            f"_Type `/automode on` or `/automode off` "
            f"to switch modes at any time._"
        )

        # Set up approval state for this user
        approval_state[user_id] = {
            "pending_result":    None,
            "remaining_contacts": contacts,
            "stats": {
                "sent":    0,
                "skipped": 0,
                "failed":  0
            }
        }

        # Start processing the first contact
        process_next_contact(user_id, say)

    except ValueError as e:
        # File format errors — missing columns etc
        say(f"⚠️ Problem with your file: {e}")

    except Exception as e:
        say(f"❌ Something went wrong reading the file: {e}")
        log_action(
            action_type="error",
            detail=f"File upload error: {str(e)}"
        )


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Riley is starting up...")

    # Load all past conversations from Supabase into RAM
    # This is what gives Riley memory across restarts
    print("📚 Loading conversation history from Supabase...")
    load_all_conversations()

    print("🤖 Connecting to Slack...")

    # Start the Socket Mode handler
    # This connects to Slack and listens forever
    handler = SocketModeHandler(
        app,
        os.environ.get("RILEY_APP_TOKEN")
    )

    print("✅ Riley is live. DM Riley in Slack to start.")
    handler.start()