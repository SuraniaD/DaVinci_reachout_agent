import os
import tempfile
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv

from memory import load_all_conversations, clear_history
from agents.riley import (
    chat_with_riley,
    draft_with_feedback
)
from agents.dexter import (
    chat_with_dexter,
    research_businesses,
    is_in_elicitation,
    handle_elicitation_reply,
    start_elicitation,
    start_clarification,
    cancel_elicitation,
    _needs_elicitation,
    _detect_ambiguity
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


# ─────────────────────────────────────────
# LEARN FROM APPROVAL
# ─────────────────────────────────────────

def _learn_from_approval(
    user_id: str,
    result:  dict
):
    contact = result["contact"]
    subject = result.get("subject", "")
    print(
        f"✅ [LEARN] Approved: "
        f"'{contact['business_name']}' — "
        f"'{subject[:50]}'"
    )
    log_action(
        action_type="approved",
        contact_name=contact.get("name"),
        business_name=contact.get("business_name"),
        detail=f"Subject approved: {subject}"
    )


# ═══════════════════════════════════════════
# DEXTER — RESEARCH RUNNER
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
                f"drafting emails._"
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
    """
    Central handler for all Dexter Slack DMs.

    Routing order:
    1. Ignore bot messages
    2. DMs only
    3. Commands
    4. Mid-elicitation / clarification reply
    5. Research intent
    6. General chat
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

    if is_in_elicitation(user_id):
        print(f"   [ELICIT STATE] Active")

    # ── !prospects ────────────────────────
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

    # ── !resetrun ─────────────────────────
    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    # ── !add ─────────────────────────────
    if text.lower().startswith("!add "):
        cancel_elicitation(user_id)
        business_query = text[5:].strip()
        if not business_query:
            say("⚠️ Example: *!add Monzo London UK*")
            return
        _run_research(
            user_id=user_id,
            instruction=business_query,
            say=say
        )
        return

    # ── !research ────────────────────────
    if text.lower().startswith("!research "):
        cancel_elicitation(user_id)
        query = text[10:].strip()

        if not query:
            say(
                "⚠️ Example: "
                "*!research vegan businesses USA*"
            )
            return

        print(
            f"🔬 [DEXTER CMD] !research: '{query}'"
        )

        if _needs_elicitation(query):
            print(f"   [ELICIT] Vague — asking")
            say(start_elicitation(user_id, query))
        else:
            ambiguity_q = _detect_ambiguity(query)
            if ambiguity_q:
                print(f"   [CLARIFY] Ambiguity detected")
                say(
                    start_clarification(
                        user_id=user_id,
                        original=query,
                        industry="businesses",
                        location="the specified area",
                        question=ambiguity_q
                    )
                )
            else:
                print(f"   [DIRECT] Clear — researching")
                _run_research(
                    user_id=user_id,
                    instruction=query,
                    say=say
                )
        return

    # ── MID-ELICITATION / CLARIFICATION ──
    if is_in_elicitation(user_id):
        print(
            f"❓ [DEXTER] Elicitation reply: '{text}'"
        )

        question, query, industry, location = \
            handle_elicitation_reply(user_id, text)

        if question:
            say(question)
        elif query:
            say(
                f"✅ Got it — searching for "
                f"*{query}*..."
            )
            _run_research(
                user_id=user_id,
                instruction=query,
                say=say,
                industry=industry,
                location=location
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
    if any(s in text.lower() for s in research_signals):
        print(f"🔬 [DEXTER] Research intent")
        if _needs_elicitation(text):
            say(start_elicitation(user_id, text))
        else:
            ambiguity_q = _detect_ambiguity(text)
            if ambiguity_q:
                say(
                    start_clarification(
                        user_id=user_id,
                        original=text,
                        industry="businesses",
                        location="the specified area",
                        question=ambiguity_q
                    )
                )
            else:
                _run_research(
                    user_id=user_id,
                    instruction=text,
                    say=say
                )
        return

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [DEXTER] → chat")
    say("_Thinking..._")
    say(chat_with_dexter(user_id, text))


# ═══════════════════════════════════════════
# RILEY — HELPERS
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
    print(
        f"🚀 [RILEY] Run ({source}) — "
        f"{len(contacts)} contacts"
    )
    clear_run_state(user_id)

    mode_msg = (
        "⚡ *Auto-send ON*"
        if is_auto_mode(user_id)
        else
        "✋ *Approval mode ON* — "
        "I'll show each draft first."
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

        contact  = remaining.pop(0)
        biz_name = (
            contact.get("business_name") or
            contact.get("name", "unknown")
        )
        print(f"▸  [RILEY LOOP] {biz_name}")

        if source == "db":
            email = contact.get("email") or ""
            email = str(email).strip()

            if not email or \
               email.lower() in [
                   "", "none", "null",
                   "n/a", "not found"
               ] or "@" not in email:
                stats["skipped"] += 1
                _persist_state(user_id)
                say(
                    f"⏭️ Skipping *{biz_name}* — "
                    f"no valid email."
                )
                t = threading.Thread(
                    target=process_next_contact,
                    args=(user_id, say)
                )
                t.daemon = True
                t.start()
                return

            result = process_prospect_from_db(
                user_id=user_id,
                prospect=contact,
                say_fn=say
            )
        else:
            result = process_contact(
                user_id=user_id,
                contact=contact,
                say_fn=say
            )

        if result is None:
            if source == "db":
                stats["skipped"] += 1
            else:
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
            success = send_approved_email(result)
            if success:
                stats["sent"] += 1
                _learn_from_approval(user_id, result)
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
                    f"*{contact_info['name']}*."
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

    if not file_name.endswith(
        (".csv", ".xlsx", ".xls")
    ):
        say("⚠️ Please upload a .csv or .xlsx file.")
        return

    say(f"📂 Got *{file_name}* — reading contacts...")

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
            skipped_lines = "\n".join(
                [f"  • {s}" for s in skipped[:10]]
            )
            if len(skipped) > 10:
                skipped_lines += (
                    f"\n  • ...and "
                    f"{len(skipped) - 10} more"
                )
            say(
                f"⚠️ No emails for "
                f"{len(skipped)} contacts:\n"
                f"{skipped_lines}"
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
    print(f"🔓 [RILEY] waiting cleared")

    # ── APPROVE ──────────────────────────
    if text.lower().strip() == "approve":
        success = send_approved_email(result)
        if success:
            stats["sent"] += 1
            _learn_from_approval(user_id, result)
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

    # ── SKIP ─────────────────────────────
    if text.lower().strip() == "skip":
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next...\n"
            f"_Type *!run draft_ready* to retry later._"
        )
        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # ── REDRAFT WITH LEARNING ─────────────
    print(f"✏️  [RILEY] Redraft: '{text[:80]}'")
    say("Got it — redrafting with your feedback...")

    from agents.riley import parse_draft

    try:
        new_draft, learned = draft_with_feedback(
            user_id=user_id,
            feedback=text,
            original_draft=result["draft"],
            contact_name=contact["name"],
            business_name=contact["business_name"]
        )

        new_subject, new_body = parse_draft(new_draft)

        print(
            f"✅ [RILEY] Redraft ready. "
            f"Learned: '{learned}'"
        )

        if learned:
            say(
                f"_Noted for all future drafts: "
                f"\"{learned}\"_"
            )

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
        print(f"💥 [RILEY] Redraft failed: {e}")
        say(
            f"⚠️ Redraft failed: {e}\n"
            f"Reply *approve* or *skip*."
        )
        state["pending_result"] = result
        state["waiting"]        = True
        print(f"🔒 [RILEY] Restored waiting")


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
    if text.lower().startswith("!run"):
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 \
            else "researched"

        if status not in ["researched", "draft_ready"]:
            say("⚠️ Use: *!run* or *!run draft_ready*")
            return

        print(f"🚀 [RILEY CMD] !run status={status}")

        # Fetches only prospects with emails
        # at DB level — no-email rows excluded
        prospects = get_prospects_for_outreach(
            status=status, limit=50
        )

        print(
            f"📋 [RILEY] Fetched {len(prospects)} "
            f"prospects with status='{status}'"
        )

        if not prospects:
            say(
                f"📋 No *{status.replace('_', ' ')}* "
                f"prospects with emails found.\n\n"
                f"• Ask Dexter to research businesses\n"
                f"• Or type *!pipeline {status}* to "
                f"see what's in the DB"
            )
            return

        # Secondary sanity check — must have @ in email
        with_email = [
            p for p in prospects
            if p.get("email") and
            "@" in str(p.get("email", ""))
        ]
        without_email = len(prospects) - len(with_email)

        print(
            f"📋 [RILEY] {len(with_email)} valid emails, "
            f"{without_email} invalid"
        )

        if not with_email:
            say(
                f"⚠️ Found *{len(prospects)} prospects* "
                f"but emails look invalid.\n"
                f"Ask Dexter to re-research."
            )
            return

        msg = (
            f"✅ Found *{len(with_email)} prospects* "
            f"with emails."
        )
        if without_email > 0:
            msg += (
                f"\n⚠️ {without_email} will be skipped "
                f"(invalid email format)."
            )
        msg += "\nStarting drafting now..."
        say(msg)

        start_outreach_run(
            user_id=user_id,
            contacts=with_email,
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
            say(format_prospects_for_slack(
                prospects,
                f"📋 Prospects — "
                f"{status.replace('_', ' ')}"
            ))
        else:
            counts = get_pipeline_summary()
            say(
                format_pipeline_summary_for_slack(counts)
            )
        return

    # ── !mark ────────────────────────────
    if text.lower().startswith("!mark "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            say(
                "⚠️ Usage:\n"
                "*!mark replied <business>*\n"
                "*!mark closed <business>*"
            )
            return

        action       = parts[1].lower().strip()
        business_str = parts[2].strip()

        if action not in ["replied", "closed"]:
            say("⚠️ Valid: *replied* or *closed*")
            return

        prospect = get_prospect_by_name(business_str)
        if not prospect:
            say(
                f"⚠️ Could not find *{business_str}*.\n"
                f"Type *!pipeline* to see all."
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

    # ── OTHER COMMANDS ────────────────────

    if text.lower() == "!reset":
        clear_history("riley", user_id)
        say("🔄 Memory cleared.")
        return

    if text.lower() == "!status":
        say(format_logs_for_slack(
            get_recent_logs(limit=15)
        ))
        return

    if text.lower() == "!automode on":
        set_auto_mode(user_id, True)
        say("⚡ *Auto-send ON*")
        return

    if text.lower() == "!automode off":
        set_auto_mode(user_id, False)
        say("✋ *Approval mode ON*")
        return

    if text.lower() == "!showprefs":
        from tools.preferences import get_preferences
        prefs = get_preferences(user_id)
        if not prefs:
            say(
                "🧠 No preferences saved yet.\n"
                "Give feedback on any draft and I'll "
                "remember it for all future emails."
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

    # ── APPROVAL REPLY ───────────────────
    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        handle_approval_reply(user_id, text, say)
        return

    # ── PASTED TABLE ─────────────────────
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

    # ── GENERAL CHAT ─────────────────────
    print(f"💬 [RILEY ROUTING] → chat")
    say("_Thinking..._")
    say(chat_with_riley(user_id, text))


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
                        f"👋 Back after restart.\n\n"
                        f"*{len(rem)} prospects "
                        f"remaining*.\n"
                        f"Progress: "
                        f"{sts.get('sent', 0)} sent · "
                        f"{sts.get('skipped', 0)} "
                        f"skipped\n\n"
                        f"Continuing now..."
                    )
                )

                def say(msg):
                    riley_client.chat_postMessage(
                        channel=uid, text=msg
                    )

                process_next_contact(uid, say)

            except Exception as e:
                print(f"⚠️  [STARTUP] {e}")

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
    print("   DM Dexter → research prospects.")
    print("   DM Riley  → !run to start outreach.")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    riley_handler.start()