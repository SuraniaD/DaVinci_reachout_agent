import os
import tempfile
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv

from memory import load_all_conversations, clear_history
from agents.riley import chat_with_riley
from agents.dexter import (
    chat_with_dexter,
    research_businesses
)
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
from tools.run_state import (
    save_run_state,
    load_all_run_states,
    clear_run_state
)
from tools.prospect_db import (
    add_prospect,
    get_prospects,
    get_prospect_by_name,
    start_research_session,
    complete_research_session,
    format_prospects_for_slack
)
from interaction_log import (
    log_action,
    get_recent_logs,
    format_logs_for_slack
)

load_dotenv()

# ─────────────────────────────────────────
# TWO SLACK APPS — RILEY AND DEXTER
# Each has its own bot token and signing secret
# Both run in the same Railway process
# ─────────────────────────────────────────

riley_app = App(
    token=os.environ.get("RILEY_BOT_TOKEN"),
    signing_secret=os.environ.get("RILEY_SIGNING_SECRET")
)

dexter_app = App(
    token=os.environ.get("DEXTER_BOT_TOKEN"),
    signing_secret=os.environ.get("DEXTER_SIGNING_SECRET")
)

riley_client = WebClient(
    token=os.environ.get("RILEY_BOT_TOKEN")
)

dexter_client = WebClient(
    token=os.environ.get("DEXTER_BOT_TOKEN")
)

# ─────────────────────────────────────────
# RILEY'S APPROVAL STATE
# ─────────────────────────────────────────

approval_state = {}


# ─────────────────────────────────────────
# SHARED HELPER — DOWNLOAD FILE FROM SLACK
# ─────────────────────────────────────────

def download_slack_file(
    file_info: dict,
    bot_token: str
) -> str:
    import requests
    file_url  = file_info["url_private_download"]
    file_name = file_info["name"]
    headers   = {"Authorization": f"Bearer {bot_token}"}
    response  = requests.get(file_url, headers=headers)
    suffix    = (
        ".csv" if file_name.endswith(".csv") else ".xlsx"
    )
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix
    ) as tmp:
        tmp.write(response.content)
        return tmp.name


# ═══════════════════════════════════════════
# DEXTER EVENT HANDLERS
# ═══════════════════════════════════════════

@dexter_app.event("message")
def handle_dexter_dm(event, say):
    """
    Central handler for all Dexter Slack DMs.

    Routing:
    1. Ignore bot messages
    2. Only handle DMs
    3. !prospects — show pipeline
    4. !add <business> — research one business
    5. !research or plain instruction — research query
    6. General chat — chat_with_dexter
    """
    if event.get("bot_id"):
        return
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text    = event.get("text", "").strip()

    print(
        f"\n🔬 [DEXTER DM] {user_id}: "
        f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
    )

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 else None

        print(
            f"📋 [DEXTER CMD] !prospects "
            f"status={status}"
        )

        prospects = get_prospects(
            status=status,
            limit=20
        )

        title = "📋 Prospect Pipeline"
        if status:
            title = f"📋 Prospects — {status}"

        say(format_prospects_for_slack(prospects, title))
        return

    # ── !add <business> ──────────────────
    if text.lower().startswith("!add "):
        business_query = text[5:].strip()
        if not business_query:
            say(
                "⚠️ Tell me which business to add.\n"
                "Example: *!add Little Fitzroy Edinburgh*"
            )
            return

        print(
            f"➕ [DEXTER CMD] !add: '{business_query}'"
        )

        _run_research(
            user_id=user_id,
            instruction=business_query,
            say=say,
            single=True
        )
        return

    # ── !research <query> ─────────────────
    if text.lower().startswith("!research "):
        query = text[10:].strip()
        if not query:
            say(
                "⚠️ Tell me what to research.\n"
                "Example: "
                "*!research vegan cafes in Berlin*"
            )
            return

        print(
            f"🔬 [DEXTER CMD] !research: '{query}'"
        )

        _run_research(
            user_id=user_id,
            instruction=query,
            say=say
        )
        return

    # ── PLAIN TEXT — research intent ──────
    # If message looks like a research instruction
    # treat it as a research request
    research_signals = [
        "find", "search", "look for", "research",
        "get me", "i need", "can you find",
        "businesses", "companies", "shops",
        "cafes", "bakeries", "agencies",
        "startups", "brands", "stores"
    ]
    text_lower = text.lower()
    is_research_intent = any(
        s in text_lower for s in research_signals
    )

    if is_research_intent:
        print(
            f"🔬 [DEXTER] Research intent detected"
        )
        _run_research(
            user_id=user_id,
            instruction=text,
            say=say
        )
        return

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [DEXTER] → general chat")
    say("_Thinking..._")
    reply = chat_with_dexter(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# DEXTER RESEARCH RUNNER
# Runs in background thread so Slack doesn't time out
# ─────────────────────────────────────────

def _run_research(
    user_id:     str,
    instruction: str,
    say,
    single:      bool = False
):
    """
    Runs Dexter's research in a background thread.
    single=True means research one specific business.
    single=False means find multiple businesses.
    """
    def _run():
        # Create a research session log
        session_id = start_research_session(
            user_id, instruction
        )

        try:
            # Get structured prospect data from Dexter
            prospects = research_businesses(
                user_id=user_id,
                instruction=instruction,
                say_fn=say
            )

            if not prospects:
                complete_research_session(
                    session_id, 0, "failed"
                )
                return

            # Write each prospect to DB
            added   = []
            skipped = []

            for p in prospects:
                p["source_query"] = instruction
                result = add_prospect(p)
                if result:
                    added.append(p["business_name"])
                else:
                    skipped.append(p["business_name"])

            # Complete the session log
            complete_research_session(
                session_id, len(added)
            )

            # Report back in Slack
            if not added and not skipped:
                say(
                    "⚠️ Found results but couldn't "
                    "extract clean business data. "
                    "Try being more specific."
                )
                return

            # Build summary message
            lines = [
                f"✅ *Research complete for: "
                f"'{instruction}'*\n"
            ]

            if added:
                lines.append(
                    f"*Added {len(added)} new prospects:*"
                )
                for name in added:
                    # Get the full prospect to show email
                    p_data = get_prospect_by_name(name)
                    email  = (
                        p_data.get("email", "no email")
                        if p_data else "no email"
                    )
                    loc = (
                        p_data.get("location", "")
                        if p_data else ""
                    )
                    line = f"  🔬 *{name}*"
                    if loc:
                        line += f" — {loc}"
                    line += f"\n     {email}"
                    lines.append(line)

            if skipped:
                lines.append(
                    f"\n*Already in DB ({len(skipped)}):*"
                )
                for name in skipped:
                    lines.append(f"  • {name}")

            lines.append(
                f"\n_Type *!prospects* to see your "
                f"full pipeline._"
            )

            say("\n\n".join(lines))

            log_action(
                action_type="research",
                detail=(
                    f"Dexter: '{instruction}' — "
                    f"{len(added)} added, "
                    f"{len(skipped)} skipped"
                )
            )

        except Exception as e:
            print(f"💥 [DEXTER] Research run failed: {e}")
            say(f"❌ Research failed: {e}")
            if session_id:
                complete_research_session(
                    session_id, 0, "failed"
                )

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ═══════════════════════════════════════════
# RILEY EVENT HANDLERS
# Exactly the same as before — no changes
# ═══════════════════════════════════════════

def _persist_state(user_id: str):
    if user_id not in approval_state:
        return
    state = approval_state[user_id]
    save_run_state(
        user_id=user_id,
        remaining=state["remaining_contacts"],
        stats=state["stats"]
    )


def start_outreach_run(
    user_id:  str,
    contacts: list[dict],
    say
):
    print(
        f"🚀 [RILEY] Starting run for {user_id} — "
        f"{len(contacts)} contacts"
    )
    clear_run_state(user_id)

    mode_msg = (
        "⚡ *Auto-send mode is ON*"
        if is_auto_mode(user_id)
        else "✋ *Approval mode is ON* — "
             "I'll show each draft before sending."
    )

    say(
        f"✅ Found *{len(contacts)} contacts*. "
        f"Starting outreach run now.\n\n{mode_msg}"
    )

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
    _persist_state(user_id)
    process_next_contact(user_id, say)


def post_draft_for_approval(
    user_id: str,
    result:  dict,
    say
):
    if user_id not in approval_state:
        return
    approval_state[user_id]["pending_result"] = result
    approval_state[user_id]["waiting"]        = True
    contact = result["contact"]
    print(
        f"✋ [RILEY] Waiting: "
        f"{contact['name']} @ {contact['business_name']}"
    )
    say(format_draft_for_slack(result))


def process_next_contact(user_id: str, say):
    def _run():
        if user_id not in approval_state:
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]

        print(
            f"📋 [RILEY LOOP] {len(remaining)} remaining"
        )

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
            clear_run_state(user_id)
            del approval_state[user_id]
            return

        contact = remaining.pop(0)
        print(
            f"▸  [RILEY LOOP] {contact['name']} "
            f"@ {contact['business_name']}"
        )

        result = process_contact(user_id, contact, say)

        if result is None:
            stats["failed"] += 1
            _persist_state(user_id)
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
            return

        if is_auto_mode(user_id):
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
                    f"❌ Failed: *{contact['name']}*. "
                    f"Moving on."
                )
            _persist_state(user_id)
            t = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
        else:
            _persist_state(user_id)
            post_draft_for_approval(user_id, result, say)

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


def handle_file_upload(event: dict, say, user_id: str):
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    print(
        f"📂 [RILEY] {user_id} uploaded: {file_name}"
    )

    if not file_name.endswith((".csv", ".xlsx", ".xls")):
        say("⚠️ Please upload a .csv or .xlsx file.")
        return

    say(
        f"📂 Got *{file_name}* — "
        f"reading contacts..."
    )

    try:
        file_path = download_slack_file(
            file_info,
            os.environ.get("RILEY_BOT_TOKEN")
        )

        result = read_contact_list(file_path, user_id)

        if isinstance(result, tuple):
            contacts, skipped = result
        else:
            contacts = result
            skipped  = []

        if skipped:
            say(
                f"⚠️ Skipped {len(skipped)} contacts "
                f"(no email found)"
            )

        if not contacts:
            say("❌ No contacts with emails found.")
            return

        start_outreach_run(user_id, contacts, say)

    except ValueError as e:
        say(f"⚠️ Problem with file: {e}")
    except Exception as e:
        say(f"❌ Something went wrong: {e}")
        log_action(
            action_type="error",
            detail=f"File upload error: {str(e)}"
        )


def handle_approval_reply(
    user_id: str,
    text:    str,
    say
):
    state   = approval_state[user_id]
    result  = state["pending_result"]
    stats   = state["stats"]
    contact = result["contact"]

    print(
        f"📨 [RILEY REPLY] '{text}' for "
        f"{contact['name']}"
    )

    state["waiting"]        = False
    state["pending_result"] = None

    if text.lower().strip() == "approve":
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*. "
                f"Moving to next..."
            )
        else:
            stats["failed"] += 1
            say("❌ Send failed. Moving to next...")

        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    if text.lower().strip() == "skip":
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next..."
        )
        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # Redraft
    say("Got it — redrafting with your feedback...")

    from agents.riley import parse_draft

    try:
        feedback_task = (
            f"CEO feedback: \"{text}\"\n\n"
            f"Original draft:\n{result['draft']}\n\n"
            f"Redraft with this feedback. "
            f"Keep SUBJECT then BODY format."
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
        post_draft_for_approval(user_id, new_result, say)

    except Exception as e:
        say(
            f"⚠️ Redraft failed: {e}\n"
            f"Reply *approve* or *skip*."
        )
        state["pending_result"] = result
        state["waiting"]        = True


@riley_app.event("message")
def handle_riley_dm(event, say):
    if event.get("bot_id"):
        return
    if event.get("channel_type") != "im":
        return

    user_id = event["user"]
    text    = event.get("text", "").strip()

    print(
        f"\n📧 [RILEY DM] {user_id}: "
        f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
    )

    if user_id in approval_state:
        s = approval_state[user_id]
        print(
            f"   [STATE] waiting={s.get('waiting')} "
            f"remaining="
            f"{len(s.get('remaining_contacts', []))}"
        )

    # FILE UPLOAD
    if event.get("files"):
        handle_file_upload(event, say, user_id)
        return

    # COMMANDS
    if text.lower() == "!reset":
        clear_history("riley", user_id)
        say("🔄 Memory cleared.")
        return

    if text.lower() == "!status":
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    if text.lower() == "!automode on":
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send ON* — emails go immediately."
        )
        return

    if text.lower() == "!automode off":
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — "
            "I'll show each draft first."
        )
        return

    if text.lower() == "!showprefs":
        from tools.preferences import get_preferences
        prefs = get_preferences(user_id)
        if not prefs:
            say("🧠 No preferences saved yet.")
        else:
            prefs_list = "\n".join(
                [f"  {i+1}. {p}"
                 for i, p in enumerate(prefs)]
            )
            say(
                f"🧠 *Saved preferences:*\n"
                f"{prefs_list}"
            )
        return

    if text.lower() == "!resetprefs":
        from tools.preferences import clear_preferences
        clear_preferences(user_id)
        say("🗑️ Preferences cleared.")
        return

    if text.lower() == "!resetrun":
        clear_run_state(user_id)
        if user_id in approval_state:
            del approval_state[user_id]
        say(
            "🗑️ Run cancelled. "
            "Upload a new list to start fresh."
        )
        return

    # APPROVAL REPLY
    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        handle_approval_reply(user_id, text, say)
        return

    # PASTED TABLE
    if (
        "\n" in text and
        "@" in text and
        ("|" in text or "\t" in text)
    ):
        result = parse_pasted_table(text, user_id)
        if isinstance(result, tuple):
            contacts, skipped = result
        else:
            contacts = result
            skipped  = []

        if contacts:
            if skipped:
                say(
                    f"⚠️ Skipped {len(skipped)} "
                    f"(no email)"
                )
            start_outreach_run(user_id, contacts, say)
            return

    # GENERAL CHAT
    say("_Thinking..._")
    reply = chat_with_riley(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# RESTORE RILEY'S INTERRUPTED RUNS
# ─────────────────────────────────────────

def restore_interrupted_runs():
    interrupted = load_all_run_states()

    if not interrupted:
        print("✅ [STARTUP] No interrupted runs")
        return

    for row in interrupted:
        user_id   = row["user_id"]
        remaining = row["remaining_contacts"]
        stats     = row.get("stats", {
            "sent": 0, "skipped": 0, "failed": 0
        })

        print(
            f"▶️  [STARTUP] Restoring {user_id} — "
            f"{len(remaining)} remaining"
        )

        approval_state[user_id] = {
            "pending_result":     None,
            "remaining_contacts": list(remaining),
            "waiting":            False,
            "stats":              stats
        }

        def notify_and_resume(uid, rem, sts):
            try:
                riley_client.chat_postMessage(
                    channel=uid,
                    text=(
                        f"👋 I'm back after a restart.\n\n"
                        f"Picking up — "
                        f"*{len(rem)} contacts remaining*.\n"
                        f"Progress: "
                        f"{sts.get('sent', 0)} sent · "
                        f"{sts.get('skipped', 0)} skipped\n\n"
                        f"Continuing now..."
                    )
                )

                def say(msg):
                    riley_client.chat_postMessage(
                        channel=uid, text=msg
                    )

                process_next_contact(uid, say)

            except Exception as e:
                print(
                    f"⚠️  [STARTUP] Notify failed "
                    f"{uid}: {e}"
                )

        t = threading.Thread(
            target=notify_and_resume,
            args=(user_id, remaining, stats)
        )
        t.daemon = True
        t.start()


# ─────────────────────────────────────────
# STARTUP — BOTH AGENTS
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting DaVinci AI agents...")
    print("📚 Loading conversation history...")
    load_all_conversations()

    print("▶️  Restoring interrupted Riley runs...")
    restore_interrupted_runs()

    print("🔬 Starting Dexter (Research Agent)...")
    dexter_handler = SocketModeHandler(
        dexter_app,
        os.environ.get("DEXTER_APP_TOKEN")
    )

    # Start Dexter in a background thread
    dexter_thread = threading.Thread(
        target=dexter_handler.start
    )
    dexter_thread.daemon = True
    dexter_thread.start()
    print("✅ Dexter is live.")

    print("📧 Starting Riley (Outreach Agent)...")
    riley_handler = SocketModeHandler(
        riley_app,
        os.environ.get("RILEY_APP_TOKEN")
    )

    print("✅ Both agents live. DM Dexter or Riley in Slack.")
    riley_handler.start()  # Blocks — runs in main thread