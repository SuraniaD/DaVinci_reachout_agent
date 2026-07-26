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
    research_businesses,
    is_in_elicitation,
    handle_elicitation_reply,
    start_elicitation,
    cancel_elicitation,
    _needs_elicitation
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
from tools.file_reader import (
    read_contact_list,
    parse_pasted_table
)
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
# Same Railway process, separate bot tokens
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
# RILEY APPROVAL STATE
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
# DEXTER — RESEARCH RUNNER
# ═══════════════════════════════════════════

def _run_research(
    user_id:     str,
    instruction: str,
    say
):
    """
    Runs Dexter's research in a background thread.
    Correctly reports added vs duplicate vs invalid.
    """
    def _run():
        session_id = start_research_session(
            user_id, instruction
        )

        try:
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

            added      = []
            duplicates = []
            invalid    = []

            for p in prospects:
                p["source_query"] = instruction
                result = add_prospect(p)

                if result:
                    added.append(p["business_name"])
                else:
                    name = p.get("business_name", "")
                    if name and name.lower() not in [
                        "none", "null", "unknown",
                        "", "n/a", "not found"
                    ]:
                        duplicates.append(name)
                    else:
                        invalid.append(str(p))

            complete_research_session(
                session_id, len(added)
            )

            if not added and not duplicates:
                say(
                    "⚠️ No valid businesses could be "
                    "added from those results.\n"
                    "Try more specific keywords."
                )
                return

            # Build summary message
            lines = [
                f"✅ *Research complete:* "
                f"_{instruction}_\n"
            ]

            if added:
                lines.append(
                    f"*Added {len(added)} "
                    f"new prospect"
                    f"{'s' if len(added) > 1 else ''}:*"
                )
                for name in added:
                    p_data = get_prospect_by_name(name)
                    email  = (
                        p_data.get("email") or "no email"
                        if p_data else "no email"
                    )
                    loc = (
                        p_data.get("location") or ""
                        if p_data else ""
                    )
                    line = f"  🔬 *{name}*"
                    if loc:
                        line += f" — {loc}"
                    line += f"\n     {email}"
                    lines.append(line)

            if duplicates:
                lines.append(
                    f"\n*Already in DB "
                    f"({len(duplicates)}):*"
                )
                for name in duplicates:
                    lines.append(f"  • {name}")

            if invalid:
                lines.append(
                    f"\n_{len(invalid)} entries had "
                    f"no business name and were skipped_"
                )

            lines.append(
                f"\n_Type *!prospects* to see "
                f"your full pipeline._"
            )

            say("\n\n".join(lines))

            log_action(
                action_type="research",
                detail=(
                    f"Dexter: '{instruction}' — "
                    f"{len(added)} added, "
                    f"{len(duplicates)} duplicate, "
                    f"{len(invalid)} invalid"
                )
            )

        except Exception as e:
            print(
                f"💥 [DEXTER] Research run failed: {e}"
            )
            say(f"❌ Research failed: {e}")
            if session_id:
                complete_research_session(
                    session_id, 0, "failed"
                )

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ═══════════════════════════════════════════
# DEXTER EVENT HANDLER
# ═══════════════════════════════════════════

@dexter_app.event("message")
def handle_dexter_dm(event, say):
    """
    Central handler for all Dexter Slack DMs.

    Routing order:
    1. Ignore bot messages
    2. DMs only
    3. !prospects — show pipeline
    4. !resetrun  — cancel session
    5. !add       — research one business
    6. !research  — research with elicitation check
    7. Mid-elicitation reply
    8. Research intent — elicitation or direct
    9. General chat
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

    # Log elicitation state for debugging
    if is_in_elicitation(user_id):
        print(
            f"   [ELICIT STATE] Active for {user_id}"
        )

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        cancel_elicitation(user_id)

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
        title = (
            f"📋 Prospects — {status}"
            if status else
            "📋 Prospect Pipeline"
        )
        say(format_prospects_for_slack(prospects, title))
        return

    # ── !resetrun ─────────────────────────
    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    # ── !add <business> ──────────────────
    if text.lower().startswith("!add "):
        cancel_elicitation(user_id)
        business_query = text[5:].strip()

        if not business_query:
            say(
                "⚠️ Tell me which business to add.\n"
                "Example: *!add Monzo London UK*"
            )
            return

        print(
            f"➕ [DEXTER CMD] !add: '{business_query}'"
        )
        _run_research(
            user_id=user_id,
            instruction=business_query,
            say=say
        )
        return

    # ── !research <query> ─────────────────
    if text.lower().startswith("!research "):
        cancel_elicitation(user_id)
        query = text[10:].strip()

        if not query:
            say(
                "⚠️ Tell me what to research.\n"
                "Example: "
                "*!research SaaS startups London UK*"
            )
            return

        print(
            f"🔬 [DEXTER CMD] !research: '{query}'"
        )

        # Even with !research — elicit if vague
        if _needs_elicitation(query):
            reply = start_elicitation(user_id, query)
            say(reply)
        else:
            _run_research(
                user_id=user_id,
                instruction=query,
                say=say
            )
        return

    # ── MID-ELICITATION REPLY ─────────────
    # Must come BEFORE research intent detection
    # so elicitation answers aren't misrouted
    if is_in_elicitation(user_id):
        print(
            f"❓ [DEXTER] Elicitation reply: '{text}'"
        )

        question, query = handle_elicitation_reply(
            user_id, text
        )

        if question:
            # Still collecting info — ask next question
            say(question)

        elif query:
            # Have everything — start research
            say(
                f"✅ Got it — searching for "
                f"*{query}*..."
            )
            _run_research(
                user_id=user_id,
                instruction=query,
                say=say
            )

        return

    # ── RESEARCH INTENT DETECTION ─────────
    research_signals = [
        "find", "search", "look for", "research",
        "get me", "i need", "can you find",
        "businesses", "companies", "shops",
        "cafes", "bakeries", "agencies",
        "startups", "brands", "stores",
        "clients", "leads", "prospects"
    ]
    text_lower         = text.lower()
    is_research_intent = any(
        s in text_lower for s in research_signals
    )

    if is_research_intent:
        print(
            f"🔬 [DEXTER] Research intent detected"
        )

        if _needs_elicitation(text):
            # Vague — start elicitation flow
            reply = start_elicitation(user_id, text)
            say(reply)
        else:
            # Specific enough — research directly
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


# ═══════════════════════════════════════════
# RILEY — HELPERS
# ═══════════════════════════════════════════

def _persist_state(user_id: str):
    """Saves current run state to Supabase."""
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
    """Starts a fresh outreach run for Riley."""
    print(
        f"🚀 [RILEY] Run for {user_id} — "
        f"{len(contacts)} contacts"
    )
    clear_run_state(user_id)

    mode_msg = (
        "⚡ *Auto-send mode is ON* — "
        "I'll send emails without asking for approval."
        if is_auto_mode(user_id)
        else
        "✋ *Approval mode is ON* — "
        "I'll show each draft and wait for "
        "*approve* or *skip* before doing anything."
    )

    say(
        f"✅ Found *{len(contacts)} contacts*. "
        f"Starting outreach run now.\n\n"
        f"{mode_msg}\n\n"
        f"_Type *!automode on* or *!automode off* "
        f"to switch modes._"
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
    """Posts draft to Slack and sets waiting=True."""
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
    """
    Picks next contact, researches, drafts.
    Saves state after every action.
    Always runs in background thread.
    """
    def _run():
        if user_id not in approval_state:
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]

        print(
            f"📋 [RILEY LOOP] "
            f"{len(remaining)} remaining for {user_id}"
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
            f"@ {contact['business_name']} "
            f"({contact['email']})"
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
                print(
                    f"✅ [RILEY AUTO] Sent to "
                    f"{contact['email']}"
                )
                say(
                    f"✅ Sent to *{contact['name']}* "
                    f"at *{contact['business_name']}*"
                )
            else:
                stats["failed"] += 1
                print(
                    f"❌ [RILEY AUTO] Failed: "
                    f"{contact['email']}"
                )
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


def handle_file_upload(
    event:   dict,
    say,
    user_id: str
):
    """Handles CSV/Excel uploaded to Riley's Slack DM."""
    file_info = event["files"][0]
    file_name = file_info.get("name", "")

    print(
        f"📂 [RILEY] {user_id} uploaded: {file_name}"
    )

    if not file_name.endswith(
        (".csv", ".xlsx", ".xls")
    ):
        say("⚠️ Please upload a .csv or .xlsx file.")
        return

    say(
        f"📂 Got *{file_name}* — "
        f"reading contacts and looking up emails..."
    )

    try:
        print("⬇️  [RILEY] Downloading file...")
        file_path = download_slack_file(
            file_info,
            os.environ.get("RILEY_BOT_TOKEN")
        )
        print(f"✅ [RILEY] Downloaded to: {file_path}")

        result = read_contact_list(file_path, user_id)

        if isinstance(result, tuple):
            contacts, skipped = result
        else:
            contacts = result
            skipped  = []

        print(
            f"✅ [RILEY] {len(contacts)} contacts, "
            f"{len(skipped)} skipped"
        )

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
                f"{len(skipped)} contacts:*\n"
                f"{skipped_lines}"
            )

        if not contacts:
            say("❌ No contacts with emails found.")
            return

        start_outreach_run(user_id, contacts, say)

    except ValueError as e:
        print(f"❌ [RILEY] ValueError: {e}")
        say(f"⚠️ Problem with file: {e}")
    except Exception as e:
        print(f"💥 [RILEY] File error: {e}")
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
    """
    Handles reply when Riley is waiting for approval.
    approve → send, skip → skip, anything else → redraft.
    """
    state   = approval_state[user_id]
    result  = state["pending_result"]
    stats   = state["stats"]
    contact = result["contact"]

    print(
        f"📨 [RILEY REPLY] '{text}' for "
        f"{contact['name']} @ {contact['business_name']}"
    )

    # Clear waiting flag immediately
    state["waiting"]        = False
    state["pending_result"] = None

    print(f"🔓 [RILEY] waiting cleared for {user_id}")

    # ── APPROVE ──────────────────────────
    if text.lower().strip() == "approve":
        print(
            f"✅ [RILEY] Sending to {contact['email']}"
        )
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            print(
                f"✅ [RILEY] Sent → {contact['email']}"
            )
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*. "
                f"Moving to next contact..."
            )
        else:
            stats["failed"] += 1
            print(
                f"❌ [RILEY] Failed: {contact['email']}"
            )
            say("❌ Send failed. Moving to next contact...")

        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # ── SKIP ─────────────────────────────
    if text.lower().strip() == "skip":
        print(
            f"⏭️  [RILEY] Skipping {contact['name']}"
        )
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next contact..."
        )
        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # ── EDIT INSTRUCTIONS — REDRAFT ──────
    print(
        f"✏️  [RILEY] Redraft feedback: '{text[:80]}'"
    )
    say("Got it — redrafting with your feedback...")

    from agents.riley import parse_draft

    try:
        feedback_task = (
            f"CEO feedback on the draft: \"{text}\"\n\n"
            f"Original draft:\n{result['draft']}\n\n"
            f"Redraft incorporating this feedback. "
            f"Keep SUBJECT then BODY format."
        )
        new_draft             = chat_with_riley(
            user_id, feedback_task
        )
        new_subject, new_body = parse_draft(new_draft)

        print(
            f"✅ [RILEY] Redraft ready for "
            f"{contact['name']}"
        )

        new_result = {
            "contact": contact,
            "draft":   new_draft,
            "subject": new_subject,
            "body":    new_body
        }
        post_draft_for_approval(
            user_id, new_result, say
        )

    except Exception as e:
        print(f"💥 [RILEY] Redraft failed: {e}")
        say(
            f"⚠️ Redraft failed: {e}\n"
            f"Reply *approve* to send the original "
            f"or *skip* to skip this contact."
        )
        # Restore original so approve/skip still work
        state["pending_result"] = result
        state["waiting"]        = True
        print(
            f"🔒 [RILEY] Restored waiting for {user_id}"
        )


# ═══════════════════════════════════════════
# RILEY EVENT HANDLER
# ═══════════════════════════════════════════

@riley_app.event("message")
def handle_riley_dm(event, say):
    """
    Central handler for all Riley Slack DMs.

    Routing order:
    1. Ignore bot messages
    2. DMs only
    3. File upload
    4. Commands
    5. Approval reply (waiting flag)
    6. Pasted table
    7. General chat
    """
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

    # ── FILE UPLOAD ──────────────────────
    if event.get("files"):
        print(f"📎 [RILEY] File upload from {user_id}")
        handle_file_upload(event, say, user_id)
        return

    # ── COMMANDS ─────────────────────────

    if text.lower() == "!reset":
        print(f"🔄 [RILEY CMD] !reset from {user_id}")
        clear_history("riley", user_id)
        say("🔄 Memory cleared.")
        return

    if text.lower() == "!status":
        print(f"📋 [RILEY CMD] !status from {user_id}")
        logs      = get_recent_logs(limit=15)
        formatted = format_logs_for_slack(logs)
        say(formatted)
        return

    if text.lower() == "!automode on":
        print(
            f"⚡ [RILEY CMD] !automode on "
            f"from {user_id}"
        )
        set_auto_mode(user_id, True)
        say(
            "⚡ *Auto-send ON* — emails go immediately "
            "without approval.\n"
            "Type *!automode off* to switch back."
        )
        return

    if text.lower() == "!automode off":
        print(
            f"✋ [RILEY CMD] !automode off "
            f"from {user_id}"
        )
        set_auto_mode(user_id, False)
        say(
            "✋ *Approval mode ON* — I'll show each "
            "draft and wait for *approve* or *skip*."
        )
        return

    if text.lower() == "!showprefs":
        print(
            f"🧠 [RILEY CMD] !showprefs from {user_id}"
        )
        from tools.preferences import get_preferences
        prefs = get_preferences(user_id)
        if not prefs:
            say(
                "🧠 No preferences saved yet.\n"
                "Tell me things like "
                "_'keep it under 80 words'_ "
                "and I'll remember them for every draft."
            )
        else:
            prefs_list = "\n".join(
                [f"  {i+1}. {p}"
                 for i, p in enumerate(prefs)]
            )
            say(
                f"🧠 *Saved preferences "
                f"({len(prefs)}):*\n"
                f"{prefs_list}\n\n"
                f"_Type *!resetprefs* to clear all._"
            )
        return

    if text.lower() == "!resetprefs":
        print(
            f"🗑️  [RILEY CMD] !resetprefs from {user_id}"
        )
        from tools.preferences import clear_preferences
        clear_preferences(user_id)
        say("🗑️ All preferences cleared.")
        return

    if text.lower() == "!resetrun":
        print(
            f"🗑️  [RILEY CMD] !resetrun from {user_id}"
        )
        clear_run_state(user_id)
        if user_id in approval_state:
            del approval_state[user_id]
        say(
            "🗑️ Outreach run cancelled.\n"
            "Upload a new list to start fresh."
        )
        return

    # ── APPROVAL REPLY ───────────────────
    # Check waiting flag — must come before
    # general chat so "approve"/"skip" are routed right
    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        print(
            f"📨 [RILEY ROUTING] → approval reply"
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
            f"📋 [RILEY ROUTING] Pasted table "
            f"from {user_id}"
        )
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
                    f"(no email found)"
                )
            say(
                f"📋 Found *{len(contacts)} contacts*. "
                f"Starting outreach run..."
            )
            start_outreach_run(
                user_id, contacts, say
            )
            return
        else:
            print(
                "⚠️  [RILEY] Could not parse table "
                "— falling through to chat"
            )

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [RILEY ROUTING] → general chat")
    say("_Thinking..._")
    reply = chat_with_riley(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# RESTORE RILEY'S INTERRUPTED RUNS ON STARTUP
# ─────────────────────────────────────────

def restore_interrupted_runs():
    """
    On startup, checks Supabase for any Riley runs
    that were interrupted by a redeploy.
    Restores them and notifies the CEO in Slack.
    """
    interrupted = load_all_run_states()

    if not interrupted:
        print("✅ [STARTUP] No interrupted runs")
        return

    print(
        f"▶️  [STARTUP] Restoring "
        f"{len(interrupted)} interrupted run(s)..."
    )

    for row in interrupted:
        user_id   = row["user_id"]
        remaining = row["remaining_contacts"]
        stats     = row.get("stats", {
            "sent": 0, "skipped": 0, "failed": 0
        })

        print(
            f"▶️  [STARTUP] {user_id} — "
            f"{len(remaining)} contacts remaining"
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
                        f"Picking up your outreach run — "
                        f"*{len(rem)} contacts remaining*.\n"
                        f"Progress so far: "
                        f"{sts.get('sent', 0)} sent · "
                        f"{sts.get('skipped', 0)} skipped · "
                        f"{sts.get('failed', 0)} failed.\n\n"
                        f"Continuing now..."
                    )
                )

                def say(msg):
                    riley_client.chat_postMessage(
                        channel=uid,
                        text=msg
                    )

                process_next_contact(uid, say)

            except Exception as e:
                print(
                    f"⚠️  [STARTUP] Could not notify "
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
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    print("📚 Loading conversation history...")
    load_all_conversations()

    print("▶️  Checking for interrupted Riley runs...")
    restore_interrupted_runs()

    print("🔬 Starting Dexter (Research Agent)...")
    dexter_handler = SocketModeHandler(
        dexter_app,
        os.environ.get("DEXTER_APP_TOKEN")
    )

    # Dexter runs in background thread
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

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ Both agents live.")
    print("   DM Dexter in Slack to research prospects.")
    print("   DM Riley in Slack to send outreach emails.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Riley runs in main thread — blocks here
    riley_handler.start()