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
from tools.file_reader import read_contact_list, parse_pasted_table
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
# Structure per user_id:
# {
#   "pending_result":     None or { contact, draft, subject, body }
#   "remaining_contacts": [ ... ]
#   "waiting":            True = Riley waiting for approve/skip
#   "stats":              { sent, skipped, failed }
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
# INITIALISE OUTREACH RUN
# Shared by file upload and pasted table
# ─────────────────────────────────────────

def start_outreach_run(
    user_id:  str,
    contacts: list[dict],
    say
):
    """
    Initialises approval state and starts the outreach loop.
    Called after contacts loaded from any source —
    file upload or pasted table.
    """
    print(
        f"🚀 [OUTREACH RUN] Starting run for user {user_id} "
        f"— {len(contacts)} contacts"
    )

    mode = "AUTO-SEND" if is_auto_mode(user_id) else "APPROVAL"
    print(f"⚙️  [OUTREACH RUN] Mode: {mode}")

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

    process_next_contact(user_id, say)


# ─────────────────────────────────────────
# POST DRAFT AND WAIT FOR APPROVAL
# ─────────────────────────────────────────

def post_draft_for_approval(
    user_id: str,
    result:  dict,
    say
):
    """
    Posts the draft to Slack and sets waiting=True.
    handle_dm checks waiting flag to route replies
    to approval handler instead of general chat.
    """
    if user_id not in approval_state:
        print(
            f"⚠️  [APPROVAL] approval_state missing "
            f"for {user_id} — cannot post draft"
        )
        return

    approval_state[user_id]["pending_result"] = result
    approval_state[user_id]["waiting"]        = True

    contact = result["contact"]
    print(
        f"✋ [APPROVAL] Posting draft for: "
        f"{contact['name']} @ {contact['business_name']}"
    )

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
            print(
                f"⚠️  [LOOP] approval_state cleared "
                f"for {user_id} — stopping"
            )
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]

        print(
            f"📋 [LOOP] {len(remaining)} contacts remaining "
            f"for user {user_id}"
        )

        # No more contacts — run complete
        if not remaining:
            print(
                f"🏁 [LOOP] Run complete for {user_id} — "
                f"sent: {stats['sent']}, "
                f"skipped: {stats['skipped']}, "
                f"failed: {stats['failed']}"
            )
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
        print(
            f"▸  [LOOP] Processing: {contact['name']} "
            f"@ {contact['business_name']} "
            f"({contact['email']})"
        )

        # Research + draft
        result = process_contact(user_id, contact, say)

        if result is None:
            print(
                f"❌ [LOOP] process_contact returned None "
                f"for {contact['name']} — marking as failed"
            )
            stats["failed"] += 1
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
            return

        if is_auto_mode(user_id):
            print(
                f"⚡ [AUTO-SEND] Sending immediately to "
                f"{contact['email']}"
            )
            success = send_approved_email(result)
            if success:
                stats["sent"] += 1
                print(
                    f"✅ [AUTO-SEND] Sent to "
                    f"{contact['email']}"
                )
                say(
                    f"✅ Sent to *{contact['name']}* "
                    f"at *{contact['business_name']}*"
                )
            else:
                stats["failed"] += 1
                print(
                    f"❌ [AUTO-SEND] Failed to send to "
                    f"{contact['email']}"
                )
                say(
                    f"❌ Failed to send to "
                    f"*{contact['name']}*. Moving on."
                )
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()

        else:
            post_draft_for_approval(user_id, result, say)

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ─────────────────────────────────────────
# FILE UPLOAD HANDLER
# ─────────────────────────────────────────

def handle_file_upload(event: dict, say, user_id: str):
    """
    Handles CSV/Excel file uploaded to Slack DM.
    Supports .csv, .xlsx, .xls
    """
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    print(
        f"📂 [FILE UPLOAD] User {user_id} "
        f"uploaded: {file_name}"
    )

    if not file_name.endswith((".csv", ".xlsx", ".xls")):
        print(
            f"❌ [FILE UPLOAD] Unsupported file type: "
            f"{file_name}"
        )
        say(
            "⚠️ I can only read CSV or Excel files "
            "(.csv, .xlsx, .xls). "
            "Please upload one of those."
        )
        return

    say(
        f"📂 Got your file — *{file_name}*. "
        f"Reading the contact list..."
    )

    try:
        print(
            f"⬇️  [FILE UPLOAD] Downloading from Slack..."
        )
        file_path = download_slack_file(file_info)
        print(
            f"✅ [FILE UPLOAD] Downloaded to: {file_path}"
        )

        contacts = read_contact_list(file_path, user_id)
        print(
            f"✅ [FILE UPLOAD] Parsed {len(contacts)} "
            f"contacts from file"
        )

        if not contacts:
            print("⚠️  [FILE UPLOAD] No valid contacts found")
            say(
                "⚠️ The file was empty or "
                "had no valid contacts."
            )
            return

        start_outreach_run(user_id, contacts, say)

    except ValueError as e:
        print(f"❌ [FILE UPLOAD] ValueError: {e}")
        say(f"⚠️ Problem with your file: {e}")

    except Exception as e:
        print(f"💥 [FILE UPLOAD] Unexpected error: {e}")
        say(f"❌ Something went wrong reading the file: {e}")
        log_action(
            action_type="error",
            detail=f"File upload error: {str(e)}"
        )


# ─────────────────────────────────────────
# HANDLE APPROVAL REPLY
# Called when waiting=True and user sends a message
# ─────────────────────────────────────────

def handle_approval_reply(
    user_id: str,
    text:    str,
    say
):
    """
    Handles reply when Riley is waiting for approval.

    Three cases:
      "approve" → send the email, move to next contact
      "skip"    → skip this contact, move to next
      anything else → treat as edit feedback, redraft
    """
    state   = approval_state[user_id]
    result  = state["pending_result"]
    stats   = state["stats"]
    contact = result["contact"]

    print(
        f"📨 [APPROVAL] User replied: '{text}' "
        f"for {contact['name']} @ {contact['business_name']}"
    )

    # Clear waiting flag immediately
    # Prevents user getting stuck if something errors
    state["waiting"]        = False
    state["pending_result"] = None

    # ── APPROVE ──────────────────────────
    if text.lower() == "approve":
        print(
            f"✅ [APPROVAL] Approved — sending to "
            f"{contact['email']}"
        )
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            print(
                f"✅ [EMAIL SENT] {contact['name']} "
                f"@ {contact['business_name']} "
                f"→ {contact['email']}"
            )
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*. "
                f"Moving to next contact..."
            )
        else:
            stats["failed"] += 1
            print(
                f"❌ [EMAIL FAILED] Could not send to "
                f"{contact['email']}"
            )
            say(
                "❌ Send failed. "
                "Moving to next contact..."
            )
        process_next_contact(user_id, say)
        return

    # ── SKIP ─────────────────────────────
    if text.lower() == "skip":
        print(
            f"⏭️  [SKIP] Skipping {contact['name']} "
            f"@ {contact['business_name']}"
        )
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next contact..."
        )
        process_next_contact(user_id, say)
        return

    # ── EDIT INSTRUCTIONS ────────────────
    print(
        f"✏️  [REDRAFT] Feedback for "
        f"{contact['name']}: '{text[:100]}'"
    )
    say("Got it — redrafting with your feedback...")

    from agents.riley import parse_draft

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

        print(
            f"✅ [REDRAFT] New draft ready for "
            f"{contact['name']}"
        )

        new_result = {
            "contact": contact,
            "draft":   new_draft,
            "subject": new_subject,
            "body":    new_body
        }

        post_draft_for_approval(user_id, new_result, say)

    except Exception as e:
        print(f"💥 [REDRAFT] Failed: {e}")
        say(
            f"⚠️ Redraft failed: {e}\n"
            f"Reply *approve* to send the original "
            f"or *skip* to skip this contact."
        )
        # Restore original so approve/skip still work
        state["pending_result"] = result
        state["waiting"]        = True


# ─────────────────────────────────────────
# MAIN MESSAGE HANDLER
# ─────────────────────────────────────────

@app.event("message")
def handle_dm(event, say):
    """
    Central handler for all Slack DMs.

    Routing order:
    1. Ignore bot messages
    2. Only handle DMs
    3. File upload → handle_file_upload
    4. Commands → handle directly
    5. Waiting for approval → handle_approval_reply
    6. Pasted table → start_outreach_run
    7. General chat → chat_with_riley
    """
    # Ignore bot messages — prevents infinite loops
    if event.get("bot_id"):
        return

    # Only handle direct messages
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text    = event.get("text", "").strip()

    print(
        f"💬 [DM] User {user_id} sent: "
        f"'{text[:80]}{'...' if len(text) > 80 else ''}'"
    )

    # ── FILE UPLOAD ──────────────────────
    if event.get("files"):
        handle_file_upload(event, say, user_id)
        return

    # ── COMMANDS ─────────────────────────
    # Work even during an outreach run

    if text.lower() == "!reset":
        print(f"🔄 [COMMAND] !reset by user {user_id}")
        clear_history("riley", user_id)
        say(
            "🔄 Memory cleared. Starting fresh — "
            "I won't remember our previous conversations."
        )
        return

    if text.lower() == "!status":
        print(f"📋 [COMMAND] !status by user {user_id}")
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    if text.lower() == "!automode on":
        print(
            f"⚡ [COMMAND] !automode on by user {user_id}"
        )
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send mode ON* — emails go out "
            "immediately without approval. "
            "Type *!automode off* to switch back."
        )
        return

    if text.lower() == "!automode off":
        print(
            f"✋ [COMMAND] !automode off by user {user_id}"
        )
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — I'll show you "
            "each draft before sending."
        )
        return

    # ── APPROVAL FLOW ────────────────────
    # Check waiting flag — reliable even with threading

    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        print(
            f"📨 [ROUTING] Approval reply from {user_id}"
        )
        handle_approval_reply(user_id, text, say)
        return

    # ── PASTED TABLE ─────────────────────
    # Detect pipe or tab separated table pasted in DM
    # Must have separator, email, and multiple lines

    if (
        "\n" in text and
        "@" in text and
        ("|" in text or "\t" in text)
    ):
        print(
            f"📋 [ROUTING] Pasted table detected "
            f"from {user_id}"
        )
        contacts = parse_pasted_table(text, user_id)
        if contacts:
            print(
                f"✅ [PASTED TABLE] Parsed "
                f"{len(contacts)} contacts"
            )
            say(
                f"📋 I can see a table with "
                f"*{len(contacts)} contacts*. "
                f"Starting outreach run..."
            )
            start_outreach_run(user_id, contacts, say)
            return
        else:
            print(
                "⚠️  [PASTED TABLE] Could not parse "
                "contacts — falling through to chat"
            )

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [ROUTING] General chat for {user_id}")
    # say("_Thinking..._")
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