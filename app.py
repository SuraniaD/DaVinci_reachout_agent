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
# ─────────────────────────────────────────

def start_outreach_run(
    user_id:  str,
    contacts: list[dict],
    say
):
    """
    Initialises approval state and starts the loop.
    Default mode is APPROVAL — never auto-sends
    unless you explicitly switch with !automode on.
    """
    print(
        f"🚀 [OUTREACH RUN] Starting for {user_id} — "
        f"{len(contacts)} contacts"
    )

    mode = "AUTO-SEND" if is_auto_mode(user_id) else "APPROVAL"
    print(f"⚙️  [OUTREACH RUN] Mode: {mode}")

    mode_msg = (
        "⚡ *Auto-send mode is ON* — "
        "I'll send emails without asking for approval."
        if is_auto_mode(user_id)
        else
        "✋ *Approval mode is ON* — "
        "I'll show you each draft and wait for your "
        "*approve* or *skip* before doing anything."
    )

    say(
        f"✅ Found *{len(contacts)} contacts*. "
        f"Starting outreach run now.\n\n"
        f"{mode_msg}\n\n"
        f"_Type *!automode on* or *!automode off* "
        f"to switch modes at any time._"
    )

    # Initialise state — waiting starts as False
    # It gets set to True only when a draft is posted
    approval_state[user_id] = {
        "pending_result":     None,
        "remaining_contacts": list(contacts),
        "waiting":            False,
        "stats": {
            "sent":    0,
            "skipped": 0,
            "failed":  0
        }
    }

    print(
        f"✅ [OUTREACH RUN] State initialised for {user_id}"
    )

    # Kick off the first contact
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
    This is the ONLY place waiting gets set to True.
    handle_dm checks this flag to route replies
    to the approval handler.
    """
    if user_id not in approval_state:
        print(
            f"⚠️  [APPROVAL] No state for {user_id} "
            f"— cannot post draft"
        )
        return

    # Store the result BEFORE posting to Slack
    # so it's ready when the user replies
    approval_state[user_id]["pending_result"] = result
    approval_state[user_id]["waiting"]        = True

    contact = result["contact"]
    print(
        f"✋ [APPROVAL] Waiting for approval: "
        f"{contact['name']} @ {contact['business_name']}\n"
        f"   waiting flag = True"
    )

    # Post the draft — user sees this in Slack
    say(format_draft_for_slack(result))

    print(
        f"✋ [APPROVAL] Draft posted — "
        f"waiting for approve/skip from {user_id}"
    )


# ─────────────────────────────────────────
# PROCESS NEXT CONTACT
# Always runs in a new background thread
# ─────────────────────────────────────────

def process_next_contact(user_id: str, say):
    """
    Picks next contact, researches, drafts.
    In approval mode — posts draft and sets waiting=True.
    In auto-send mode — sends immediately.
    Runs in background thread — never blocks Slack.
    """
    def _run():
        if user_id not in approval_state:
            print(
                f"⚠️  [LOOP] No state for {user_id} "
                f"— stopping"
            )
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]

        print(
            f"📋 [LOOP] {len(remaining)} contacts remaining "
            f"for {user_id}"
        )

        # No more contacts — run complete
        if not remaining:
            print(
                f"🏁 [LOOP] Run complete for {user_id} — "
                f"sent={stats['sent']} "
                f"skipped={stats['skipped']} "
                f"failed={stats['failed']}"
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

        # Take next contact
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
                f"❌ [LOOP] Failed for {contact['name']} "
                f"— moving on"
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
            # AUTO-SEND MODE
            print(
                f"⚡ [AUTO-SEND] Sending to "
                f"{contact['email']}..."
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
            # Move to next contact
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()

        else:
            # APPROVAL MODE
            # Post draft and set waiting=True
            # The loop STOPS here until user replies
            print(
                f"✋ [LOOP] Approval mode — "
                f"posting draft and waiting..."
            )
            post_draft_for_approval(user_id, result, say)

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ─────────────────────────────────────────
# FILE UPLOAD HANDLER
# ─────────────────────────────────────────

def handle_file_upload(event: dict, say, user_id: str):
    """
    Handles CSV/Excel uploaded to Slack DM.
    read_contact_list returns (contacts, skipped) tuple.
    """
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    print(
        f"📂 [FILE UPLOAD] {user_id} uploaded: {file_name}"
    )

    if not file_name.endswith((".csv", ".xlsx", ".xls")):
        print(
            f"❌ [FILE UPLOAD] Unsupported: {file_name}"
        )
        say(
            "⚠️ I can only read CSV or Excel files "
            "(.csv, .xlsx, .xls)."
        )
        return

    say(
        f"📂 Got your file — *{file_name}*. "
        f"Reading contacts and looking up any missing emails..."
    )

    try:
        print("⬇️  [FILE UPLOAD] Downloading...")
        file_path = download_slack_file(file_info)
        print(f"✅ [FILE UPLOAD] Saved to: {file_path}")

        # read_contact_list returns (contacts, skipped)
        result = read_contact_list(file_path, user_id)

        # Handle both old format (list) and new format (tuple)
        if isinstance(result, tuple):
            contacts, skipped = result
        else:
            contacts = result
            skipped  = []

        print(
            f"✅ [FILE UPLOAD] {len(contacts)} contacts, "
            f"{len(skipped)} skipped"
        )

        # Tell user about skipped contacts
        if skipped:
            skipped_lines = "\n".join(
                [f"  • {s}" for s in skipped[:10]]
            )
            if len(skipped) > 10:
                skipped_lines += (
                    f"\n  • ...and "
                    f"{len(skipped) - 10} more"
                )
            say(
                f"⚠️ *Could not find emails for "
                f"{len(skipped)} contacts — skipping:*\n"
                f"{skipped_lines}"
            )

        if not contacts:
            say(
                "❌ No contacts with emails found. "
                "Please check your file."
            )
            return

        start_outreach_run(user_id, contacts, say)

    except ValueError as e:
        print(f"❌ [FILE UPLOAD] ValueError: {e}")
        say(f"⚠️ Problem with your file: {e}")

    except Exception as e:
        print(f"💥 [FILE UPLOAD] Error: {e}")
        say(f"❌ Something went wrong: {e}")
        log_action(
            action_type="error",
            detail=f"File upload error: {str(e)}"
        )


# ─────────────────────────────────────────
# HANDLE APPROVAL REPLY
# ─────────────────────────────────────────

def handle_approval_reply(
    user_id: str,
    text:    str,
    say
):
    """
    Called when waiting=True and user sends a message.

    Three cases:
      "approve" → send the email
      "skip"    → skip this contact
      anything else → treat as edit feedback, redraft
    """
    state   = approval_state[user_id]
    result  = state["pending_result"]
    stats   = state["stats"]
    contact = result["contact"]

    print(
        f"📨 [APPROVAL REPLY] '{text}' from {user_id} "
        f"for {contact['name']} @ {contact['business_name']}"
    )

    # Clear waiting flag IMMEDIATELY
    # Must happen before any async work
    state["waiting"]        = False
    state["pending_result"] = None

    print(f"🔓 [APPROVAL] waiting flag cleared for {user_id}")

    # ── APPROVE ──────────────────────────
    if text.lower().strip() == "approve":
        print(
            f"✅ [APPROVAL] Approved — sending to "
            f"{contact['email']}"
        )
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            print(
                f"✅ [EMAIL SENT] {contact['name']} "
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
                f"❌ [EMAIL FAILED] {contact['email']}"
            )
            say(
                "❌ Send failed. "
                "Moving to next contact..."
            )
        process_next_contact(user_id, say)
        return

    # ── SKIP ─────────────────────────────
    if text.lower().strip() == "skip":
        print(
            f"⏭️  [SKIP] {contact['name']} "
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
        f"{contact['name']}: '{text[:80]}'"
    )
    say("Got it — redrafting with your feedback...")

    from agents.riley import parse_draft

    try:
        feedback_task = (
            f"The CEO gave this feedback on the draft: "
            f'"{text}"\n\n'
            f"Original draft:\n{result['draft']}\n\n"
            f"Please redraft incorporating this feedback. "
            f"Keep the same format: SUBJECT then BODY."
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

        # Post new draft — sets waiting=True again
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
        print(
            f"🔒 [REDRAFT] Restored waiting flag "
            f"for {user_id}"
        )


# ─────────────────────────────────────────
# MAIN MESSAGE HANDLER
# ─────────────────────────────────────────

@app.event("message")
def handle_dm(event, say):
    """
    Central handler for all Slack DMs.

    Routing order (strict):
    1. Ignore bot messages
    2. Only handle DMs
    3. File upload
    4. Commands (!reset, !status, !automode)
    5. Approval reply (if waiting=True)
    6. Pasted table
    7. General chat
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
        f"\n💬 [DM] From {user_id}: "
        f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
    )

    # Log current approval state for debugging
    if user_id in approval_state:
        state = approval_state[user_id]
        print(
            f"   [STATE] waiting={state.get('waiting')} "
            f"pending={state.get('pending_result') is not None} "
            f"remaining={len(state.get('remaining_contacts', []))}"
        )
    else:
        print(f"   [STATE] No active run for {user_id}")

    # ── FILE UPLOAD ──────────────────────
    if event.get("files"):
        print(f"📎 [ROUTING] File upload from {user_id}")
        handle_file_upload(event, say, user_id)
        return

    # ── COMMANDS ─────────────────────────
    if text.lower() == "!reset":
        print(f"🔄 [CMD] !reset from {user_id}")
        clear_history("riley", user_id)
        say(
            "🔄 Memory cleared. Starting fresh — "
            "I won't remember our previous conversations."
        )
        return

    if text.lower() == "!status":
        print(f"📋 [CMD] !status from {user_id}")
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    if text.lower() == "!automode on":
        print(f"⚡ [CMD] !automode on from {user_id}")
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send mode ON* — emails go out "
            "immediately without approval.\n"
            "Type *!automode off* to switch back."
        )
        return

    if text.lower() == "!automode off":
        print(f"✋ [CMD] !automode off from {user_id}")
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — I'll show you "
            "each draft and wait for *approve* or *skip*."
        )
        return

    # ── APPROVAL REPLY ───────────────────
    # Check waiting flag — this is the critical check
    # Must come BEFORE general chat routing
    if user_id in approval_state:
        state = approval_state[user_id]
        print(
            f"   [CHECK] waiting={state.get('waiting', False)}"
        )
        if state.get("waiting", False):
            print(
                f"📨 [ROUTING] → approval reply from {user_id}"
            )
            handle_approval_reply(user_id, text, say)
            return

    # ── PASTED TABLE ─────────────────────
    if (
        "\n" in text and
        "@" in text and
        ("|" in text or "\t" in text)
    ):
        print(
            f"📋 [ROUTING] Pasted table from {user_id}"
        )
        result = parse_pasted_table(text, user_id)

        if isinstance(result, tuple):
            contacts, skipped = result
        else:
            contacts = result
            skipped  = []

        if contacts:
            if skipped:
                skipped_lines = "\n".join(
                    [f"  • {s}" for s in skipped[:5]]
                )
                say(
                    f"⚠️ *Skipped {len(skipped)} contacts "
                    f"(no email):*\n{skipped_lines}"
                )
            say(
                f"📋 Found *{len(contacts)} contacts*. "
                f"Starting outreach run..."
            )
            start_outreach_run(user_id, contacts, say)
            return
        else:
            print(
                "⚠️  [PASTED TABLE] Could not parse "
                "— falling through to chat"
            )

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [ROUTING] → general chat for {user_id}")
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