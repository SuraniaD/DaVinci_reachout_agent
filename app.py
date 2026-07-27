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
    process_prospect_from_db,
    process_contact,
    send_approved_email,
    skip_contact,
    save_redraft,
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
    get_prospects_for_outreach,
    get_prospect_by_name,
    update_prospect_status,
    start_research_session,
    complete_research_session,
    format_prospects_for_slack,
    format_pipeline_summary_for_slack,
    get_pipeline_summary
)
from interaction_log import (
    log_action,
    get_recent_logs,
    format_logs_for_slack
)

load_dotenv()

# ─────────────────────────────────────────
# TWO SLACK APPS
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

approval_state = {}


# ─────────────────────────────────────────
# DOWNLOAD FILE FROM SLACK
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
# DEXTER RESEARCH RUNNER
# ═══════════════════════════════════════════

def _run_research(
    user_id:     str,
    instruction: str,
    say,
    industry:    str = None,
    location:    str = None
):
    def _run():
        session_id = start_research_session(
            user_id, instruction
        )

        try:
            prospects = research_businesses(
                user_id=user_id,
                instruction=instruction,
                say_fn=say,
                industry=industry,
                location=location
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
                    "⚠️ No valid businesses added.\n"
                    "Try more specific keywords."
                )
                return

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
                    f"\n_{len(invalid)} entries "
                    f"had no business name — skipped_"
                )

            lines.append(
                f"\n_Tell Riley *!run* to start "
                f"drafting emails for these prospects._"
            )

            say("\n\n".join(lines))

            log_action(
                action_type="research",
                detail=(
                    f"Dexter: '{instruction}' — "
                    f"{len(added)} added"
                )
            )

        except Exception as e:
            print(f"💥 [DEXTER] Run failed: {e}")
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

    # !prospects
    if text.lower().startswith("!prospects"):
        cancel_elicitation(user_id)
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 else None
        prospects = get_prospects(
            status=status, limit=20
        )
        title = (
            f"📋 Prospects — {status}"
            if status else "📋 Prospect Pipeline"
        )
        say(format_prospects_for_slack(prospects, title))
        return

    # !resetrun
    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    # !add
    if text.lower().startswith("!add "):
        cancel_elicitation(user_id)
        business_query = text[5:].strip()
        if not business_query:
            say(
                "⚠️ Tell me which business to add.\n"
                "Example: *!add Monzo London UK*"
            )
            return
        _run_research(
            user_id=user_id,
            instruction=business_query,
            say=say
        )
        return

    # !research
    if text.lower().startswith("!research "):
        cancel_elicitation(user_id)
        query = text[10:].strip()
        if not query:
            say(
                "⚠️ Example: "
                "*!research SaaS startups London UK*"
            )
            return
        if _needs_elicitation(query):
            say(start_elicitation(user_id, query))
        else:
            _run_research(
                user_id=user_id,
                instruction=query,
                say=say
            )
        return

    # Mid-elicitation
    if is_in_elicitation(user_id):
        question, query, industry, location = \
            handle_elicitation_reply(user_id, text)

        if question:
            say(question)
        elif query:
            say(
                f"✅ Got it — searching for "
                f"*{industry}* in *{location}*..."
            )
            _run_research(
                user_id=user_id,
                instruction=query,
                say=say,
                industry=industry,
                location=location
            )
        return

    # Research intent
    research_signals = [
        "find", "search", "look for", "research",
        "get me", "i need", "can you find",
        "businesses", "companies", "shops",
        "cafes", "bakeries", "agencies",
        "startups", "brands", "stores",
        "clients", "leads", "prospects"
    ]
    if any(s in text.lower() for s in research_signals):
        if _needs_elicitation(text):
            say(start_elicitation(user_id, text))
        else:
            _run_research(
                user_id=user_id,
                instruction=text,
                say=say
            )
        return

    # General chat
    say("_Thinking..._")
    reply = chat_with_dexter(user_id, text)
    say(reply)


# ═══════════════════════════════════════════
# RILEY — OUTREACH LOOP (DB-FIRST)
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
    say,
    source:   str = "csv"
):
    """
    Starts an outreach run from a contact list.
    source: 'csv' for file uploads, 'db' for DB prospects.
    """
    print(
        f"🚀 [RILEY] Run ({source}) for {user_id} — "
        f"{len(contacts)} contacts"
    )
    clear_run_state(user_id)

    mode_msg = (
        "⚡ *Auto-send mode is ON*"
        if is_auto_mode(user_id)
        else
        "✋ *Approval mode is ON* — "
        "I'll show each draft and wait for "
        "*approve* or *skip*."
    )

    say(
        f"✅ Starting outreach for "
        f"*{len(contacts)} prospects*.\n\n"
        f"{mode_msg}\n\n"
        f"_Type *!automode on/off* to switch._"
    )

    approval_state[user_id] = {
        "pending_result":     None,
        "remaining_contacts": list(contacts),
        "waiting":            False,
        "source":             source,
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
    """
    Picks next contact, drafts email.
    For DB prospects: uses research_summary from DB.
    For CSV contacts: does web research.
    """
    def _run():
        if user_id not in approval_state:
            return

        state     = approval_state[user_id]
        remaining = state["remaining_contacts"]
        stats     = state["stats"]
        source    = state.get("source", "csv")

        print(
            f"📋 [RILEY LOOP] "
            f"{len(remaining)} remaining"
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

        # Route to correct processor
        if source == "db":
            # DB prospect — use research_summary
            result = process_prospect_from_db(
                user_id=user_id,
                prospect=contact,
                say_fn=say
            )
        else:
            # CSV contact — do web research
            result = process_contact(
                user_id=user_id,
                contact=contact,
                say_fn=say
            )

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
            contact_info = result["contact"]
            if not contact_info.get("email"):
                stats["skipped"] += 1
                say(
                    f"⏭️ Skipped *{contact_info['name']}* "
                    f"— no email address."
                )
                _persist_state(user_id)
                t = threading.Thread(
                    target=process_next_contact,
                    args=(user_id, say)
                )
                t.daemon = True
                t.start()
                return

            success = send_approved_email(result)
            if success:
                stats["sent"] += 1
                say(
                    f"✅ Sent to "
                    f"*{contact_info['name']}* "
                    f"at "
                    f"*{contact_info['business_name']}*"
                )
            else:
                stats["failed"] += 1
                say(
                    f"❌ Failed: "
                    f"*{contact_info['name']}*. "
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

        start_outreach_run(
            user_id=user_id,
            contacts=contacts,
            say=say,
            source="csv"
        )

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

    # ── APPROVE ──────────────────────────
    if text.lower().strip() == "approve":
        if not contact.get("email"):
            stats["skipped"] += 1
            say(
                f"⚠️ No email for "
                f"*{contact['name']}* — skipping."
            )
            _persist_state(user_id)
            process_next_contact(user_id, say)
            return

        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*. "
                f"Moving to next contact..."
            )
        else:
            stats["failed"] += 1
            say("❌ Send failed. Moving to next contact...")

        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # ── SKIP ─────────────────────────────
    if text.lower().strip() == "skip":
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next contact..."
        )
        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # ── REDRAFT ───────────────────────────
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

        # Save new version to DB
        new_result = save_redraft(
            result=result,
            subject=new_subject,
            body=new_body,
            draft=new_draft
        )

        post_draft_for_approval(
            user_id, new_result, say
        )

    except Exception as e:
        say(
            f"⚠️ Redraft failed: {e}\n"
            f"Reply *approve* or *skip*."
        )
        state["pending_result"] = result
        state["waiting"]        = True


# ═══════════════════════════════════════════
# RILEY EVENT HANDLER
# ═══════════════════════════════════════════

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

    # ── !run ─────────────────────────────
    # Start outreach from DB prospects
    if text.lower().startswith("!run"):
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 \
            else "researched"

        # Validate status
        valid_statuses = [
            "researched", "draft_ready"
        ]
        if status not in valid_statuses:
            say(
                f"⚠️ Unknown status '{status}'.\n"
                f"Use: *!run* or "
                f"*!run draft_ready*"
            )
            return

        print(
            f"🚀 [RILEY CMD] !run status={status}"
        )

        prospects = get_prospects_for_outreach(
            status=status,
            limit=50
        )

        if not prospects:
            status_label = status.replace("_", " ")
            say(
                f"📋 No prospects with status "
                f"*{status_label}* found.\n\n"
                f"Ask Dexter to research some businesses "
                f"first, then type *!run*."
            )
            return

        # Filter out prospects with no email
        # for draft_ready — those were already skipped
        with_email    = [
            p for p in prospects if p.get("email")
        ]
        without_email = len(prospects) - len(with_email)

        if without_email > 0:
            say(
                f"⚠️ {without_email} prospect"
                f"{'s' if without_email > 1 else ''} "
                f"have no email — will skip those."
            )

        if not with_email and status == "researched":
            say(
                "⚠️ None of the researched prospects "
                "have email addresses yet.\n"
                "Ask Dexter to find emails or "
                "add them manually."
            )
            return

        say(
            f"✅ Found *{len(prospects)} prospects* "
            f"with status _{status}_.\n"
            f"{f'({without_email} without email will be skipped) ' if without_email else ''}"
            f"Starting drafting now..."
        )

        start_outreach_run(
            user_id=user_id,
            contacts=prospects,
            say=say,
            source="db"
        )
        return

    # ── !pipeline ────────────────────────
    if text.lower().startswith("!pipeline"):
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 else None

        if status:
            prospects = get_prospects(
                status=status, limit=30
            )
            status_label = status.replace("_", " ")
            say(format_prospects_for_slack(
                prospects,
                f"📋 Prospects — {status_label}"
            ))
        else:
            counts = get_pipeline_summary()
            say(format_pipeline_summary_for_slack(counts))
        return

    # ── !mark replied/closed ─────────────
    if text.lower().startswith("!mark "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            say(
                "⚠️ Usage: "
                "*!mark replied <business name>*\n"
                "or *!mark closed <business name>*"
            )
            return

        action       = parts[1].lower()
        business_str = parts[2].strip()

        if action not in ["replied", "closed"]:
            say(
                "⚠️ Valid actions: "
                "*replied* or *closed*"
            )
            return

        prospect = get_prospect_by_name(business_str)
        if not prospect:
            say(
                f"⚠️ Could not find prospect "
                f"matching *{business_str}*."
            )
            return

        update_prospect_status(
            prospect_id=prospect["id"],
            status=action
        )

        icon = "💬" if action == "replied" else "🏁"
        say(
            f"{icon} *{prospect['business_name']}* "
            f"marked as *{action}*."
        )
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
            "⚡ *Auto-send ON* — "
            "emails go immediately."
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
        say("🗑️ Run cancelled.")
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
            start_outreach_run(
                user_id=user_id,
                contacts=contacts,
                say=say,
                source="csv"
            )
            return

    # GENERAL CHAT
    say("_Thinking..._")
    reply = chat_with_riley(user_id, text)
    say(reply)


# ─────────────────────────────────────────
# RESTORE INTERRUPTED RUNS
# ─────────────────────────────────────────

def restore_interrupted_runs():
    interrupted = load_all_run_states()

    if not interrupted:
        print("✅ [STARTUP] No interrupted runs")
        return

    print(
        f"▶️  [STARTUP] Restoring "
        f"{len(interrupted)} run(s)..."
    )

    for row in interrupted:
        user_id   = row["user_id"]
        remaining = row["remaining_contacts"]
        stats     = row.get("stats", {
            "sent": 0, "skipped": 0, "failed": 0
        })

        approval_state[user_id] = {
            "pending_result":     None,
            "remaining_contacts": list(remaining),
            "waiting":            False,
            "source":             "db",
            "stats":              stats
        }

        def notify_and_resume(uid, rem, sts):
            try:
                riley_client.chat_postMessage(
                    channel=uid,
                    text=(
                        f"👋 I'm back after a restart.\n\n"
                        f"Picking up outreach — "
                        f"*{len(rem)} prospects remaining*.\n"
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
                    f"⚠️  [STARTUP] Notify failed: {e}"
                )

        t = threading.Thread(
            target=notify_and_resume,
            args=(user_id, remaining, stats)
        )
        t.daemon = True
        t.start()


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting DaVinci AI agents...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    print("📚 Loading conversation history...")
    load_all_conversations()

    print("▶️  Restoring interrupted runs...")
    restore_interrupted_runs()

    print("🔬 Starting Dexter...")
    dexter_handler = SocketModeHandler(
        dexter_app,
        os.environ.get("DEXTER_APP_TOKEN")
    )
    dexter_thread = threading.Thread(
        target=dexter_handler.start
    )
    dexter_thread.daemon = True
    dexter_thread.start()
    print("✅ Dexter live.")

    print("📧 Starting Riley...")
    riley_handler = SocketModeHandler(
        riley_app,
        os.environ.get("RILEY_APP_TOKEN")
    )

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ Both agents live.")
    print("   DM Dexter → research prospects")
    print("   DM Riley  → !run to start outreach")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    riley_handler.start()