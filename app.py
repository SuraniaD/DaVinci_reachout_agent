import os
import time
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
    format_segment_summary_for_slack,
    get_pipeline_summary,
    get_segment_summary
)
from tools.research_queue import (
    save_queue,
    get_pending_queue,
    mark_complete,
    mark_failed,
    clear_queue,
    get_queue_summary
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
queue_running  = set()
stop_requested = set()


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


# ─────────────────────────────────────────
# BULLET LIST HELPERS
# ─────────────────────────────────────────

def _parse_bullet_list(text: str) -> list[str]:
    import re as _re
    lines  = text.strip().split("\n")
    result = []

    for line in lines:
        line    = line.strip()
        if not line:
            continue
        cleaned = _re.sub(
            r'^[\•\-\*\–\—]\s*', '', line
        ).strip()
        cleaned = _re.sub(
            r'^\d+[\.\)]\s*', '', cleaned
        ).strip()
        if cleaned and len(cleaned) > 3:
            result.append(cleaned)

    return result


def _is_bullet_list(text: str) -> bool:
    import re as _re
    lines = [
        l.strip() for l in text.split("\n")
        if l.strip()
    ]
    bullet_lines = sum(
        1 for l in lines
        if _re.match(r'^[\•\-\*\–\—\d]', l)
    )
    return bullet_lines >= 2


# ═══════════════════════════════════════════
# DEXTER — SINGLE RESEARCH RUN
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
# DEXTER — QUEUE RUNNER
# ═══════════════════════════════════════════

def _run_queue(user_id: str, say):
    if user_id in queue_running:
        print(
            f"⚠️  [QUEUE] Already running for {user_id}"
        )
        return

    def _run():
        queue_running.add(user_id)
        print(f"▶️  [QUEUE] Starting for {user_id}")

        try:
            while True:
                pending = get_pending_queue(user_id)

                if not pending:
                    summary = get_queue_summary(user_id)
                    say(
                        f"✅ *Queue complete!*\n\n"
                        f"📊 Results:\n"
                        f"• Completed: {summary['complete']}\n"
                        f"• Failed: {summary['failed']}\n\n"
                        f"_Tell Riley *!run* to start "
                        f"sending emails._"
                    )
                    break

                total_left  = len(pending)
                item        = pending[0]
                instruction = item["instruction"]
                queue_id    = item["id"]
                position    = item["position"]

                say(
                    f"🔬 *Queue item {position + 1}* "
                    f"— {total_left} remaining\n"
                    f"_Researching: {instruction}_"
                )

                session_id   = start_research_session(
                    user_id, instruction
                )
                rate_limited = False

                try:
                    prospects = research_businesses(
                        user_id=user_id,
                        instruction=instruction,
                        say_fn=say
                    )

                    added = []
                    if prospects:
                        for p in prospects:
                            p["source_query"] = instruction
                            result = add_prospect(p)
                            if result:
                                added.append(
                                    p["business_name"]
                                )

                    complete_research_session(
                        session_id, len(added)
                    )
                    mark_complete(queue_id)

                    say(
                        f"✅ *Done:* _{instruction}_\n"
                        f"   Added {len(added)} prospect"
                        f"{'s' if len(added) != 1 else ''}"
                        f"{' — moving to next...' if total_left > 1 else '.'}"
                    )

                except Exception as e:
                    err_str = str(e).lower()

                    is_rate_limit = any(
                        w in err_str for w in [
                            "rate_limit", "rate limit",
                            "quota", "tokens per day",
                            "daily limit", "exceeded"
                        ]
                    )

                    if is_rate_limit:
                        rate_limited = True
                        complete_research_session(
                            session_id, 0, "failed"
                        )
                        say(
                            f"🔴 *Daily research limit reached.*\n\n"
                            f"The 70B model quota resets every 24 hours. "
                            f"I'll check every hour and resume "
                            f"automatically when the quota is back.\n\n"
                            f"_{total_left} item"
                            f"{'s' if total_left != 1 else ''} "
                            f"still in queue — nothing lost._"
                        )

                        check_count = 0
                        while True:
                            time.sleep(3600)
                            check_count += 1

                            try:
                                from groq import Groq
                                test_client = Groq(
                                    api_key=os.environ.get(
                                        "GROQ_API_KEY_DEXTER"
                                    )
                                )
                                test_client.chat.completions.create(
                                    model="openai/gpt-oss-120b",
                                    messages=[{
                                        "role":    "user",
                                        "content": "hi"
                                    }],
                                    max_tokens=5
                                )
                                say(
                                    f"🟢 *Research quota restored!*\n"
                                    f"Resuming queue — "
                                    f"*{total_left} item"
                                    f"{'s' if total_left != 1 else ''} "
                                    f"remaining*..."
                                )
                                break

                            except Exception as test_err:
                                test_str = str(
                                    test_err
                                ).lower()
                                still_limited = any(
                                    w in test_str for w in [
                                        "rate_limit",
                                        "quota",
                                        "exceeded"
                                    ]
                                )
                                if still_limited:
                                    say(
                                        f"⏳ *Still rate limited* "
                                        f"({check_count}h elapsed).\n"
                                        f"Checking again in 1 hour...\n"
                                        f"_{total_left} item"
                                        f"{'s' if total_left != 1 else ''} "
                                        f"queued._"
                                    )
                                    continue
                                else:
                                    say(
                                        f"⚠️ Check error: "
                                        f"{str(test_err)[:80]}\n"
                                        f"Attempting to resume..."
                                    )
                                    break

                    else:
                        mark_failed(queue_id, str(e))
                        complete_research_session(
                            session_id, 0, "failed"
                        )
                        say(
                            f"⚠️ *Failed:* _{instruction}_\n"
                            f"Error: {str(e)[:100]}\n"
                            f"Moving to next item..."
                        )

                if not rate_limited:
                    time.sleep(2)

        except Exception as e:
            print(f"💥 [QUEUE] Runner crashed: {e}")
            say(f"❌ Queue runner crashed: {e}")

        finally:
            queue_running.discard(user_id)
            print(f"⏹️  [QUEUE] Stopped for {user_id}")

    thread = threading.Thread(target=_run)
    thread.daemon = True
    thread.start()


# ─────────────────────────────────────────
# RESTORE QUEUES ON STARTUP
# ─────────────────────────────────────────

def _restore_queues():
    try:
        from database import supabase as _sb
        result = _sb.table("research_queue") \
            .select("user_id") \
            .eq("status", "pending") \
            .execute()

        if not result.data:
            print("✅ [STARTUP] No pending queues")
            return

        user_ids = list(set(
            r["user_id"] for r in result.data
        ))

        print(
            f"▶️  [STARTUP] Resuming queues for "
            f"{len(user_ids)} user(s)"
        )

        for uid in user_ids:
            pending = get_pending_queue(uid)
            if not pending:
                continue

            def notify_and_resume(user_id, count):
                try:
                    dexter_client.chat_postMessage(
                        channel=user_id,
                        text=(
                            f"👋 Back after restart.\n\n"
                            f"Resuming research queue — "
                            f"*{count} item"
                            f"{'s' if count != 1 else ''} "
                            f"remaining*.\nStarting now..."
                        )
                    )

                    def say(msg):
                        dexter_client.chat_postMessage(
                            channel=user_id, text=msg
                        )

                    _run_queue(user_id, say)

                except Exception as e:
                    print(
                        f"⚠️  [STARTUP] Queue notify "
                        f"failed {user_id}: {e}"
                    )

            t = threading.Thread(
                target=notify_and_resume,
                args=(uid, len(pending))
            )
            t.daemon = True
            t.start()

    except Exception as e:
        print(f"❌ [STARTUP] Queue restore failed: {e}")


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

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        cancel_elicitation(user_id)
        parts   = text.lower().split()
        status  = None
        segment = None

        if len(parts) > 1:
            known_statuses = [
                "researched", "draft_ready", "approved",
                "sent", "replied", "closed", "skipped"
            ]
            if parts[1] in known_statuses:
                status = parts[1]
            else:
                segment = " ".join(parts[1:])

        prospects = get_prospects(
            status=status, limit=20, segment=segment
        )
        title = "📋 Prospect Pipeline"
        if status:
            title = f"📋 Prospects — {status}"
        if segment:
            title = f"📋 Prospects — {segment}"

        say(format_prospects_for_slack(prospects, title))
        return

    # ── !segments ─────────────────────────
    if text.lower() in [
        "!segments", "!segment", "!segs"
    ]:
        cancel_elicitation(user_id)
        summary = get_segment_summary()
        say(format_segment_summary_for_slack(summary))
        return

    # ── !resetrun ─────────────────────────
    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    # ── !queue status ─────────────────────
    if text.lower() in [
        "!queue", "!queue status", "!queuestatus"
    ]:
        pending = get_pending_queue(user_id)
        summary = get_queue_summary(user_id)

        if not pending and summary["complete"] == 0 \
           and summary["failed"] == 0:
            say(
                "📋 No queue active.\n\n"
                "Paste a bulleted list to queue tasks:\n"
                "```\n"
                "• vegan restaurants Berlin, 20\n"
                "• plant based brands Netherlands, 15\n"
                "```"
            )
            return

        running_status = (
            "🟢 *Running*"
            if user_id in queue_running
            else "⏸️ *Paused*"
        )

        lines = [
            f"📋 *Research queue* — {running_status}\n"
            f"✅ Complete: {summary['complete']} · "
            f"⏳ Pending: {summary['pending']} · "
            f"❌ Failed: {summary['failed']}\n"
        ]

        if pending:
            lines.append("*Pending items:*")
            for item in pending[:10]:
                lines.append(
                    f"  {item['position'] + 1}. "
                    f"{item['instruction']}"
                )
            if len(pending) > 10:
                lines.append(
                    f"  ...and {len(pending) - 10} more"
                )

        lines.append(
            f"\n_*!queue clear* to cancel · "
            f"*!queue resume* to restart if paused_"
        )
        say("\n".join(lines))
        return

    # ── !queue clear ──────────────────────
    if text.lower() in [
        "!queue clear", "!clearqueue"
    ]:
        clear_queue(user_id)
        say("🗑️ Queue cleared.")
        return

    # ── !queue resume ─────────────────────
    if text.lower() in [
        "!queue resume", "!resumequeue", "!resume"
    ]:
        pending = get_pending_queue(user_id)
        if not pending:
            say("📋 No pending items in queue.")
            return
        if user_id in queue_running:
            say("⚠️ Queue is already running.")
            return
        say(
            f"▶️ Resuming queue — "
            f"*{len(pending)} item"
            f"{'s' if len(pending) != 1 else ''} "
            f"remaining*..."
        )
        _run_queue(user_id, say)
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

        if _needs_elicitation(query):
            say(start_elicitation(user_id, query))
        else:
            ambiguity_q = _detect_ambiguity(query)
            if ambiguity_q:
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
                _run_research(
                    user_id=user_id,
                    instruction=query,
                    say=say
                )
        return

    # ── BULLET LIST → QUEUE ───────────────
    if _is_bullet_list(text):
        cancel_elicitation(user_id)
        instructions = _parse_bullet_list(text)

        if not instructions:
            say("⚠️ Couldn't parse that list.")
            return

        saved = save_queue(user_id, instructions)
        if not saved:
            say("❌ Failed to save queue. Try again.")
            return

        lines = [
            f"📋 *Queued {len(instructions)} research "
            f"task{'s' if len(instructions) != 1 else ''}"
            f" — starting now:*\n"
        ]
        for i, inst in enumerate(instructions):
            lines.append(f"  {i + 1}. {inst}")
        lines.append(
            f"\n_*!queue* to check progress · "
            f"*!queue clear* to cancel_"
        )
        say("\n".join(lines))

        if user_id not in queue_running:
            _run_queue(user_id, say)
        else:
            say("_Queue already running — items added._")
        return

    # ── MID-ELICITATION / CLARIFICATION ──
    if is_in_elicitation(user_id):
        question, query, industry, location = \
            handle_elicitation_reply(user_id, text)

        if question:
            say(question)
        elif query:
            say(
                f"✅ Got it — searching for *{query}*..."
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
    source:   str = "csv",
    segment:  str = None
):
    print(
        f"🚀 [RILEY] Run ({source}) — "
        f"{len(contacts)} contacts"
        f"{(' segment=' + segment) if segment else ''}"
    )
    clear_run_state(user_id)
    stop_requested.discard(user_id)

    mode_msg = (
        "⚡ *Auto-send ON*"
        if is_auto_mode(user_id)
        else
        "✋ *Approval mode ON* — "
        "I'll show each draft first."
    )

    seg_note = (
        f"\n📂 *Segment:* {segment}"
        if segment else ""
    )

    say(
        f"✅ Starting outreach for "
        f"*{len(contacts)} prospects*."
        f"{seg_note}\n\n"
        f"{mode_msg}\n\n"
        f"_Type *STOP* at any time to pause the run._"
    )

    approval_state[user_id] = {
        "pending_result":     None,
        "remaining_contacts": list(contacts),
        "waiting":            False,
        "source":             source,
        "segment":            segment,
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

        # ── STOP CHECK ────────────────────
        if user_id in stop_requested:
            state = approval_state.get(user_id, {})
            stats = state.get("stats", {})
            remaining = state.get(
                "remaining_contacts", []
            )
            say(
                f"⏹️ *Campaign stopped.*\n\n"
                f"📊 Progress so far:\n"
                f"• Sent: {stats.get('sent', 0)}\n"
                f"• Skipped: {stats.get('skipped', 0)}\n"
                f"• Failed: {stats.get('failed', 0)}\n\n"
                f"_{len(remaining)} prospects "
                f"not yet contacted._\n"
                f"_Type *!run* to start a new run._"
            )
            stop_requested.discard(user_id)
            clear_run_state(user_id)
            if user_id in approval_state:
                del approval_state[user_id]
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

    # ── STOP — highest priority ───────────
    if text.strip().upper() == "STOP":
        if user_id in approval_state:
            stop_requested.add(user_id)
            state = approval_state[user_id]

            if state.get("waiting"):
                result = state.get("pending_result")
                if result:
                    skip_contact(result)
                state["pending_result"] = None
                state["waiting"]        = False

            say(
                "⏹️ *Stopping campaign...*\n"
                "_Finishing current action, "
                "then halting. One moment._"
            )
        else:
            say(
                "ℹ️ No campaign currently running.\n"
                "_Type *!run* to start one._"
            )
        return

    # ── !run ─────────────────────────────
    if text.lower().startswith("!run"):
        parts   = text.split(None, 2)
        status  = "researched"
        segment = None

        known_statuses = [
            "researched", "draft_ready", "approved",
            "sent", "replied", "closed", "skipped"
        ]

        if len(parts) >= 2:
            if parts[1].lower() in known_statuses:
                status = parts[1].lower()
                if len(parts) >= 3:
                    segment = parts[2].strip()
            else:
                segment = " ".join(parts[1:]).strip()

        print(
            f"🚀 [RILEY CMD] !run "
            f"status={status} segment={segment}"
        )

        prospects = get_prospects_for_outreach(
            status=status,
            limit=50,
            segment=segment
        )

        print(
            f"📋 [RILEY] Fetched {len(prospects)} "
            f"prospects"
        )

        if not prospects:
            seg_hint = (
                f" in segment *{segment}*"
                if segment else ""
            )
            say(
                f"📋 No *{status.replace('_', ' ')}* "
                f"prospects{seg_hint} found.\n\n"
                f"• *!segments* to see all segments\n"
                f"• Ask Dexter to research businesses"
            )
            return

        with_email = [
            p for p in prospects
            if p.get("email") and
            "@" in str(p.get("email", ""))
        ]
        without_email = len(prospects) - len(with_email)

        if not with_email:
            say(
                f"⚠️ Found *{len(prospects)} prospects* "
                f"but emails look invalid.\n"
                f"Ask Dexter to re-research."
            )
            return

        seg_label = (
            f" in *{segment}*" if segment else ""
        )
        msg = (
            f"✅ Found *{len(with_email)} prospects*"
            f"{seg_label}."
        )
        if without_email > 0:
            msg += (
                f"\n⚠️ {without_email} will be skipped "
                f"(invalid email)."
            )
        msg += "\nStarting drafting now..."
        say(msg)

        start_outreach_run(
            user_id=user_id,
            contacts=with_email,
            say=say,
            source="db",
            segment=segment
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

    # ── !segments ─────────────────────────
    if text.lower() in [
        "!segments", "!segment", "!segs"
    ]:
        summary = get_segment_summary()
        say(format_segment_summary_for_slack(summary))
        return

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        parts   = text.lower().split()
        status  = None
        segment = None

        if len(parts) > 1:
            known_statuses = [
                "researched", "draft_ready", "approved",
                "sent", "replied", "closed", "skipped"
            ]
            if parts[1] in known_statuses:
                status = parts[1]
            else:
                segment = " ".join(parts[1:])

        prospects = get_prospects(
            status=status, limit=30, segment=segment
        )
        title = "📋 Prospect Pipeline"
        if status:
            title = f"📋 Prospects — {status}"
        if segment:
            title = f"📋 Prospects — {segment}"

        say(format_prospects_for_slack(prospects, title))
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
        stop_requested.discard(user_id)
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
            "segment":            None,
            "stats":              stats
        }

        def notify_and_resume(uid, rem, sts):
            try:
                riley_client.chat_postMessage(
                    channel=uid,
                    text=(
                        f"👋 Back after restart.\n\n"
                        f"Picking up outreach — "
                        f"*{len(rem)} prospects "
                        f"remaining*.\n"
                        f"Progress: "
                        f"{sts.get('sent', 0)} sent · "
                        f"{sts.get('skipped', 0)} "
                        f"skipped\n\nContinuing now..."
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

    print("▶️  Restoring interrupted Riley runs...")
    restore_interrupted_runs()

    print("▶️  Restoring Dexter research queues...")
    _restore_queues()

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