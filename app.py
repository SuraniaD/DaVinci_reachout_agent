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
    draft_with_feedback,
    draft_outreach_email,
    parse_draft
)
from agents.dexter import (
    chat_with_dexter,
    is_in_elicitation,
    handle_elicitation_reply,
    start_elicitation,
    start_clarification,
    cancel_elicitation,
    _needs_elicitation,
    _detect_ambiguity
)
from flows.research_flow  import run_research_flow
from flows.reachout_flow  import (
    run_verified_send,
    format_human_review_for_slack
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
    get_good_leads_for_region,
    get_prospect_by_name,
    update_prospect_status,
    start_research_session,
    complete_research_session,
    format_prospects_for_slack,
    format_pipeline_summary_for_slack,
    format_segment_summary_for_slack,
    format_analytics_for_slack,
    get_pipeline_summary,
    get_segment_summary,
    get_analytics_summary,
    get_human_review_queue,
    create_research_request,
    get_active_research_requests,
    extract_region,
)
from tools.research_queue import (
    save_queue,
    get_pending_queue,
    mark_complete,
    mark_failed,
    clear_queue,
    get_queue_summary
)
from tools.email_sender import check_send_limits
from tools.verifier import save_verification_result
from interaction_log import (
    log_action,
    get_recent_logs,
    format_logs_for_slack
)

load_dotenv()

# ─────────────────────────────────────────
# SLACK APPS
# ─────────────────────────────────────────

# riley_app = App(
#     token=os.environ.get("RILEY_BOT_TOKEN"),
#     signing_secret=os.environ.get("RILEY_SIGNING_SECRET")
# )
dexter_app = App(
    token=os.environ.get("DEXTER_BOT_TOKEN"),
    signing_secret=os.environ.get("DEXTER_SIGNING_SECRET")
)
riley_client  = WebClient(token=os.environ.get("RILEY_BOT_TOKEN"))
dexter_client = WebClient(token=os.environ.get("DEXTER_BOT_TOKEN"))

approval_state = {}
queue_running  = set()
stop_requested = set()


# ─────────────────────────────────────────
# FILE DOWNLOAD
# ─────────────────────────────────────────

def download_slack_file(file_info: dict, bot_token: str) -> str:
    import requests
    headers  = {"Authorization": f"Bearer {bot_token}"}
    response = requests.get(
        file_info["url_private_download"], headers=headers
    )
    suffix = ".csv" if file_info["name"].endswith(".csv") \
        else ".xlsx"
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=suffix
    ) as tmp:
        tmp.write(response.content)
        return tmp.name


# ─────────────────────────────────────────
# BULLET LIST HELPERS
# ─────────────────────────────────────────

def _parse_bullet_list(text: str) -> list[str]:
    import re as _re
    result = []
    for line in text.strip().split("\n"):
        line    = line.strip()
        cleaned = _re.sub(r'^[\•\-\*\–\—]\s*', '', line).strip()
        cleaned = _re.sub(r'^\d+[\.\)]\s*', '', cleaned).strip()
        if cleaned and len(cleaned) > 3:
            result.append(cleaned)
    return result


def _is_bullet_list(text: str) -> bool:
    import re as _re
    lines  = [l.strip() for l in text.split("\n") if l.strip()]
    return sum(
        1 for l in lines if _re.match(r'^[\•\-\*\–\—\d]', l)
    ) >= 2


# ─────────────────────────────────────────
# LEARN FROM APPROVAL
# ─────────────────────────────────────────

def _learn_from_approval(user_id: str, result: dict):
    contact = result["contact"]
    log_action(
        action_type="approved",
        contact_name=contact.get("name"),
        business_name=contact.get("business_name"),
        detail=f"Subject: {result.get('subject', '')}"
    )


# ═══════════════════════════════════════════
# DEXTER — RESEARCH RUNNER
# ═══════════════════════════════════════════

def _run_research(
    user_id: str, instruction: str, say,
    industry: str = None, location: str = None
):
    """Runs Phase A research flow in background thread."""
    def _run():
        try:
            run_research_flow(
                raw_query=instruction,
                user_id=user_id,
                say_fn=say
            )
            log_action(
                action_type="research",
                detail=f"Dexter: '{instruction}'"
            )
        except Exception as e:
            print(f"💥 [DEXTER] Run failed: {e}")
            say(f"❌ Research failed: {e}")

    t        = threading.Thread(target=_run)
    t.daemon = True
    t.start()


# ═══════════════════════════════════════════
# DEXTER — QUEUE RUNNER
# ═══════════════════════════════════════════

def _run_queue(user_id: str, say):
    if user_id in queue_running:
        return

    def _run():
        queue_running.add(user_id)
        try:
            while True:
                pending = get_pending_queue(user_id)
                if not pending:
                    summary = get_queue_summary(user_id)
                    say(
                        f"✅ *Queue complete!*\n"
                        f"Completed: {summary['complete']} · "
                        f"Failed: {summary['failed']}\n\n"
                        f"_Tell Riley *!run <region>* "
                        f"to start outreach._"
                    )
                    break

                item        = pending[0]
                instruction = item["instruction"]
                queue_id    = item["id"]
                position    = item["position"]
                total_left  = len(pending)

                say(
                    f"🔬 *Queue item {position + 1}* "
                    f"— {total_left} remaining\n"
                    f"_{instruction}_"
                )

                rate_limited = False
                try:
                    run_research_flow(
                        raw_query=instruction,
                        user_id=user_id,
                        say_fn=say
                    )
                    mark_complete(queue_id)

                except Exception as e:
                    err = str(e).lower()
                    is_rl = any(w in err for w in [
                        "rate_limit", "quota", "exceeded"
                    ])

                    if is_rl:
                        rate_limited = True
                        say(
                            f"🔴 *Daily research limit reached.*\n"
                            f"Checking every hour — will resume "
                            f"automatically.\n"
                            f"_{total_left} items queued._"
                        )

                        check_count = 0
                        while True:
                            time.sleep(3600)
                            check_count += 1
                            try:
                                from groq import Groq
                                test = Groq(
                                    api_key=os.environ.get(
                                        "GROQ_API_KEY_DEXTER"
                                    )
                                ).chat.completions.create(
                                    model="openai/gpt-oss-120b",
                                    messages=[{"role": "user",
                                               "content": "hi"}],
                                    max_tokens=5
                                )
                                say(
                                    f"🟢 *Quota restored!* "
                                    f"Resuming queue — "
                                    f"{total_left} remaining..."
                                )
                                break
                            except Exception as te:
                                ts = str(te).lower()
                                if any(w in ts for w in [
                                    "rate_limit", "quota"
                                ]):
                                    say(
                                        f"⏳ Still limited "
                                        f"({check_count}h).\n"
                                        f"Checking in 1 hour..."
                                    )
                                else:
                                    say(
                                        f"⚠️ Check error: "
                                        f"{str(te)[:80]}\n"
                                        f"Attempting to resume..."
                                    )
                                    break
                    else:
                        mark_failed(queue_id, str(e))
                        say(
                            f"⚠️ Failed: _{instruction}_\n"
                            f"{str(e)[:100]}\nMoving on..."
                        )

                if not rate_limited:
                    time.sleep(2)

        except Exception as e:
            print(f"💥 [QUEUE] Crashed: {e}")
            say(f"❌ Queue error: {e}")
        finally:
            queue_running.discard(user_id)

    t        = threading.Thread(target=_run)
    t.daemon = True
    t.start()


# ─────────────────────────────────────────
# RESTORE QUEUES
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

        for uid in user_ids:
            pending = get_pending_queue(uid)
            if not pending:
                continue

            def notify(user_id, count):
                try:
                    dexter_client.chat_postMessage(
                        channel=user_id,
                        text=(
                            f"👋 Back after restart.\n"
                            f"Resuming queue — "
                            f"*{count} items remaining*."
                        )
                    )
                    def say(msg):
                        dexter_client.chat_postMessage(
                            channel=user_id, text=msg
                        )
                    _run_queue(user_id, say)
                except Exception as e:
                    print(f"⚠️  [STARTUP] {e}")

            t        = threading.Thread(
                target=notify, args=(uid, len(pending))
            )
            t.daemon = True
            t.start()

    except Exception as e:
        print(f"❌ [STARTUP] Queue restore: {e}")


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
        f"\n🔬 [DEXTER DM] "
        f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
    )

    # ── !segments ─────────────────────────
    if text.lower() in ["!segments", "!segment", "!segs"]:
        cancel_elicitation(user_id)
        say(format_segment_summary_for_slack(
            get_segment_summary()
        ))
        return

    # ── !analytics ────────────────────────
    if text.lower().startswith("!analytics"):
        cancel_elicitation(user_id)
        rows = get_analytics_summary(days=30)
        say(format_analytics_for_slack(rows, days=30))
        return

    # ── !cycles ───────────────────────────
    if text.lower() == "!cycles":
        cancel_elicitation(user_id)
        requests = get_active_research_requests()
        if not requests:
            say("📋 No active research requests.")
            return
        lines = ["*🔄 Active Research Requests*\n"]
        for r in requests:
            cycles = r.get("search_cycles", [])
            latest = max(
                (c["cycle_index"] for c in cycles),
                default=0
            )
            lines.append(
                f"*{r['raw_query']}*\n"
                f"   Cycle {latest}/{7} — "
                f"{r['good_leads_found']}/{r['target_size']} "
                f"good leads\n"
                f"   Status: {r['status']}"
            )
        say("\n\n".join(lines))
        return

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        cancel_elicitation(user_id)
        parts  = text.lower().split()
        status = None
        segment = None
        known_statuses = [
            "researched", "draft_ready", "approved",
            "sent", "replied", "closed", "skipped"
        ]
        if len(parts) > 1:
            if parts[1] in known_statuses:
                status = parts[1]
            else:
                segment = " ".join(parts[1:])

        say(format_prospects_for_slack(
            get_prospects(
                status=status, limit=20, segment=segment
            ),
            title=(
                f"📋 Prospects — {status or segment or 'all'}"
            )
        ))
        return

    # ── !resetrun ─────────────────────────
    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    # ── !queue commands ───────────────────
    if text.lower() in ["!queue", "!queue status"]:
        pending = get_pending_queue(user_id)
        summary = get_queue_summary(user_id)
        if not pending and summary["complete"] == 0:
            say(
                "📋 No queue active.\n\n"
                "Paste a bullet list to queue research:\n"
                "```\n• vegan leather UK, 200\n"
                "• plant based Japan, 50\n```"
            )
            return
        running = "🟢 *Running*" \
            if user_id in queue_running \
            else "⏸️ *Paused*"
        lines   = [
            f"📋 *Queue* — {running}\n"
            f"✅ {summary['complete']} · "
            f"⏳ {summary['pending']} · "
            f"❌ {summary['failed']}\n"
        ]
        for item in pending[:10]:
            lines.append(
                f"  {item['position']+1}. "
                f"{item['instruction']}"
            )
        say("\n".join(lines))
        return

    if text.lower() in ["!queue clear", "!clearqueue"]:
        clear_queue(user_id)
        say("🗑️ Queue cleared.")
        return

    if text.lower() in [
        "!queue resume", "!resumequeue", "!resume"
    ]:
        pending = get_pending_queue(user_id)
        if not pending:
            say("📋 No pending items.")
            return
        if user_id in queue_running:
            say("⚠️ Queue already running.")
            return
        say(f"▶️ Resuming — {len(pending)} items...")
        _run_queue(user_id, say)
        return

    # ── !add ─────────────────────────────
    if text.lower().startswith("!add "):
        cancel_elicitation(user_id)
        query = text[5:].strip()
        if not query:
            say("⚠️ Example: *!add Monzo UK*")
            return
        _run_research(user_id=user_id, instruction=query, say=say)
        return

    # ── !research ────────────────────────
    if text.lower().startswith("!research "):
        cancel_elicitation(user_id)
        query = text[10:].strip()
        if not query:
            say("⚠️ Example: *!research vegan leather UK*")
            return
        if _needs_elicitation(query):
            say(start_elicitation(user_id, query))
        else:
            aq = _detect_ambiguity(query)
            if aq:
                say(start_clarification(
                    user_id=user_id, original=query,
                    industry="businesses",
                    location="the specified area",
                    question=aq
                ))
            else:
                _run_research(
                    user_id=user_id,
                    instruction=query, say=say
                )
        return

    # ── BULLET LIST → QUEUE ───────────────
    if _is_bullet_list(text):
        cancel_elicitation(user_id)
        instructions = _parse_bullet_list(text)
        if not instructions:
            say("⚠️ Couldn't parse that list.")
            return
        if not save_queue(user_id, instructions):
            say("❌ Failed to save queue.")
            return
        lines = [
            f"📋 *Queued {len(instructions)} tasks:*\n"
        ]
        for i, inst in enumerate(instructions):
            lines.append(f"  {i+1}. {inst}")
        lines.append(
            "\n_*!queue* to check · *!queue clear* to cancel_"
        )
        say("\n".join(lines))
        if user_id not in queue_running:
            _run_queue(user_id, say)
        return

    # ── MID-ELICITATION ──────────────────
    if is_in_elicitation(user_id):
        question, query, industry, location = \
            handle_elicitation_reply(user_id, text)
        if question:
            say(question)
        elif query:
            say(f"✅ Got it — searching for *{query}*...")
            _run_research(
                user_id=user_id, instruction=query,
                say=say, industry=industry,
                location=location
            )
        return

    # ── RESEARCH INTENT ───────────────────
    research_signals = [
        "find", "search", "look for", "research",
        "get me", "i need", "can you find",
        "businesses", "companies", "shops",
        "brands", "stores", "clients", "leads",
        "prospects", "leather", "vegan", "plant"
    ]
    if any(s in text.lower() for s in research_signals):
        if _needs_elicitation(text):
            say(start_elicitation(user_id, text))
        else:
            aq = _detect_ambiguity(text)
            if aq:
                say(start_clarification(
                    user_id=user_id, original=text,
                    industry="businesses",
                    location="the specified area",
                    question=aq
                ))
            else:
                _run_research(
                    user_id=user_id,
                    instruction=text, say=say
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
    user_id: str, contacts: list[dict], say,
    source: str = "csv", segment: str = None
):
    clear_run_state(user_id)
    stop_requested.discard(user_id)

    from outreach_runner import is_auto_mode
    mode_msg = (
        "⚡ *Auto-send ON* (with verification)"
        if is_auto_mode(user_id) else
        "✋ *Approval mode ON* — I'll show drafts first."
    )

    say(
        f"✅ Starting verified outreach for "
        f"*{len(contacts)} prospects*."
        f"{f' 📂 Segment: {segment}' if segment else ''}\n\n"
        f"{mode_msg}\n"
        f"_Every email is verified (x1×x2≥0.81) before send._\n"
        f"_Type *STOP* to pause._"
    )

    approval_state[user_id] = {
        "pending_result":     None,
        "remaining_contacts": list(contacts),
        "waiting":            False,
        "source":             source,
        "segment":            segment,
        "stats": {"sent": 0, "skipped": 0, "failed": 0,
                  "human_review": 0}
    }
    _persist_state(user_id)
    process_next_contact(user_id, say)


def post_draft_for_approval(user_id: str, result: dict, say):
    if user_id not in approval_state:
        return
    approval_state[user_id]["pending_result"] = result
    approval_state[user_id]["waiting"]        = True
    from outreach_runner import format_draft_for_slack
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

        # Stop check
        if user_id in stop_requested:
            state = approval_state.get(user_id, {})
            stats = state.get("stats", {})
            remaining = state.get("remaining_contacts", [])
            say(
                f"⏹️ *Campaign stopped.*\n\n"
                f"📊 Progress:\n"
                f"• Sent: {stats.get('sent', 0)}\n"
                f"• Skipped: {stats.get('skipped', 0)}\n"
                f"• Human review: "
                f"{stats.get('human_review', 0)}\n\n"
                f"_{len(remaining)} not yet contacted._\n"
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

        if not remaining:
            from outreach_runner import generate_summary
            total = sum(stats.values())
            say(generate_summary(
                total=total,
                sent=stats["sent"],
                skipped=stats["skipped"],
                failed=stats.get("failed", 0)
            ))
            clear_run_state(user_id)
            del approval_state[user_id]
            return

        contact  = remaining.pop(0)
        biz_name = (
            contact.get("business_name") or
            contact.get("name", "unknown")
        )

        # Validate email
        email = str(contact.get("email") or "").strip()
        if not email or "@" not in email or \
           email.lower() in ["none", "null", "n/a"]:
            stats["skipped"] += 1
            _persist_state(user_id)
            say(f"⏭️ Skipping *{biz_name}* — no valid email.")
            t        = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
            return

        # Draft email
        say(f"✍️ Drafting for *{biz_name}*...")

        contact_name = (
            contact.get("contact_name") or biz_name
        )
        research = contact.get("research_summary", "")

        try:
            draft = draft_outreach_email(
                user_id=user_id,
                contact_name=contact_name,
                business_name=biz_name,
                research=research
            )
            subject, body = parse_draft(draft)

        except Exception as e:
            say(f"⚠️ Draft failed for *{biz_name}*: {e}")
            stats["failed"] = stats.get("failed", 0) + 1
            _persist_state(user_id)
            t        = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()
            return

        result = {
            "contact": {
                "name":          contact_name,
                "business_name": biz_name,
                "email":         email,
                "prospect_id":   contact.get("id"),
            },
            "draft":    draft,
            "subject":  subject,
            "body":     body,
            "prospect": contact,
        }

        from outreach_runner import is_auto_mode

        if is_auto_mode(user_id):
            # Auto mode: verify then send
            can_send, limit_reason = check_send_limits()
            if not can_send:
                say(
                    f"⏸️ *Send limit reached:* "
                    f"{limit_reason}\n"
                    f"Remaining prospects saved for "
                    f"tomorrow's run."
                )
                _persist_state(user_id)
                return

            def _redraft_fn(feedback: str) -> str:
                new_draft, _ = draft_with_feedback(
                    user_id=user_id,
                    feedback=feedback,
                    original_draft=draft,
                    contact_name=contact_name,
                    business_name=biz_name
                )
                return new_draft

            send_result = run_verified_send(
                user_id=user_id,
                prospect=contact,
                draft_subject=subject,
                draft_body=body,
                say_fn=say,
                draft_fn=_redraft_fn,
                approval_mode=False
            )

            if send_result["status"] == "sent":
                stats["sent"] += 1
                _learn_from_approval(user_id, result)
                say(
                    f"✅ Sent to *{contact_name}* "
                    f"at *{biz_name}*\n"
                    f"_x1={send_result.get('x1', 0):.2f} "
                    f"x2={send_result.get('x2', 0):.2f} "
                    f"combined="
                    f"{send_result.get('combined', 0):.3f}_"
                )

            elif send_result["status"] == "human_review":
                stats["human_review"] = (
                    stats.get("human_review", 0) + 1
                )
                say(
                    f"⚠️ *{biz_name}* → human review\n"
                    f"_Verification failed after "
                    f"3 attempts. Type *!review*._"
                )

            elif send_result["status"] == "rate_limited":
                say(
                    f"⏸️ *Send limit:* "
                    f"{send_result.get('reason', '')}\n"
                    f"Pausing campaign."
                )
                _persist_state(user_id)
                return

            else:
                stats["failed"] = (
                    stats.get("failed", 0) + 1
                )
                say(
                    f"❌ Failed: *{biz_name}* — "
                    f"{send_result.get('reason', 'error')}"
                )

            _persist_state(user_id)
            t        = threading.Thread(
                target=process_next_contact,
                args=(user_id, say)
            )
            t.daemon = True
            t.start()

        else:
            # Approval mode: show draft for review
            _persist_state(user_id)
            post_draft_for_approval(user_id, result, say)

    t        = threading.Thread(target=_run)
    t.daemon = True
    t.start()


def handle_approval_reply(user_id: str, text: str, say):
    state   = approval_state[user_id]
    result  = state["pending_result"]
    stats   = state["stats"]
    contact = result["contact"]

    state["waiting"]        = False
    state["pending_result"] = None

    # APPROVE
    if text.lower().strip() == "approve":
        # Verify then send
        can_send, limit_reason = check_send_limits()
        if not can_send:
            say(
                f"⏸️ Send limit: {limit_reason}\n"
                f"Draft saved — try again tomorrow."
            )
            state["pending_result"] = result
            state["waiting"]        = True
            return

        say("🔬 Verifying draft before sending...")

        def _redraft_fn(feedback: str) -> str:
            nd, _ = draft_with_feedback(
                user_id=user_id,
                feedback=feedback,
                original_draft=result["draft"],
                contact_name=contact["name"],
                business_name=contact["business_name"]
            )
            return nd

        send_result = run_verified_send(
            user_id=user_id,
            prospect=result["prospect"],
            draft_subject=result["subject"],
            draft_body=result["body"],
            say_fn=say,
            draft_fn=_redraft_fn,
            approval_mode=True
        )

        if send_result["status"] == "sent":
            stats["sent"] += 1
            _learn_from_approval(user_id, result)
            say(
                f"✅ Sent to *{contact['name']}* "
                f"at *{contact['business_name']}*.\n"
                f"_x1={send_result.get('x1', 0):.2f} "
                f"x2={send_result.get('x2', 0):.2f}_\n"
                f"Moving to next..."
            )
        elif send_result["status"] == "human_review":
            stats["human_review"] = (
                stats.get("human_review", 0) + 1
            )
            say(
                f"⚠️ Verification failed — "
                f"added to review queue.\n"
                f"Moving to next..."
            )
        else:
            stats["failed"] = stats.get("failed", 0) + 1
            say("❌ Send failed. Moving to next...")

        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # SKIP
    if text.lower().strip() == "skip":
        from outreach_runner import skip_contact
        skip_contact(result)
        stats["skipped"] += 1
        say(
            f"⏭️ Skipped *{contact['name']}*. "
            f"Moving to next..."
        )
        _persist_state(user_id)
        process_next_contact(user_id, say)
        return

    # REDRAFT
    say("Got it — redrafting...")
    try:
        new_draft, learned = draft_with_feedback(
            user_id=user_id, feedback=text,
            original_draft=result["draft"],
            contact_name=contact["name"],
            business_name=contact["business_name"]
        )
        new_subject, new_body = parse_draft(new_draft)

        if learned:
            say(f"_Noted: \"{learned}\"_")

        new_result = {
            **result,
            "draft":   new_draft,
            "subject": new_subject,
            "body":    new_body,
        }
        post_draft_for_approval(user_id, new_result, say)

    except Exception as e:
        say(f"⚠️ Redraft failed: {e}\nReply *approve* or *skip*.")
        state["pending_result"] = result
        state["waiting"]        = True


# ─────────────────────────────────────────
# HUMAN REVIEW HANDLERS
# ─────────────────────────────────────────

def _handle_review_action(
    user_id: str, text: str, say
):
    """Handles !approve-review, !redraft-review, !discard-review."""
    import re as _re
    match = _re.match(
        r'!(approve|redraft|discard)-review\s+(\d+)',
        text.lower().strip()
    )
    if not match:
        return False

    action = match.group(1)
    index  = int(match.group(2)) - 1

    queue = get_human_review_queue()
    if index < 0 or index >= len(queue):
        say(
            f"⚠️ Item {index+1} not found. "
            f"Type *!review* to see the queue."
        )
        return True

    item        = queue[index]
    prospect    = item.get("prospects", {})
    biz_name    = prospect.get("business_name", "Unknown")
    oe_id       = item["id"]
    prospect_id = item["prospect_id"]

    if action == "approve":
        # Send as-is
        from database import supabase
        supabase.table("outreach_emails") \
            .update({"send_status": "pending"}) \
            .eq("id", oe_id) \
            .execute()

        success, mid, tid = __import__(
            "tools.email_sender", fromlist=["send_email"]
        ).send_email(
            to_email=prospect.get("email", ""),
            subject=item.get("draft_subject", ""),
            body=item.get("draft_body", ""),
            business_name=biz_name
        )

        if success:
            from database import supabase
            supabase.table("outreach_emails") \
                .update({
                    "send_status":      "sent",
                    "gmail_message_id": mid,
                    "gmail_thread_id":  tid,
                }) \
                .eq("id", oe_id) \
                .execute()
            update_prospect_status(prospect_id, "sent")
            say(f"✅ Sent *{biz_name}* (manual approval).")
        else:
            say(f"❌ Send failed for *{biz_name}*.")

    elif action == "redraft":
        say(
            f"What feedback should I use to redraft "
            f"the email for *{biz_name}*?"
        )
        # Store context for next message
        approval_state[f"review_{user_id}"] = {
            "item": item, "prospect": prospect
        }

    elif action == "discard":
        from database import supabase
        supabase.table("outreach_emails") \
            .update({"send_status": "failed"}) \
            .eq("id", oe_id) \
            .execute()
        update_prospect_status(prospect_id, "skipped")
        say(f"🗑️ Discarded *{biz_name}* from review queue.")

    return True


# ═══════════════════════════════════════════
# RILEY EVENT HANDLER
# ═══════════════════════════════════════════

# @riley_app.event("message")
# def handle_riley_dm(event, say):
#     if event.get("bot_id"):
#         return
#     if event.get("channel_type") != "im":
#         return

#     user_id = event["user"]
#     text    = event.get("text", "").strip()

#     print(
#         f"\n📧 [RILEY DM] "
#         f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
#     )

@riley_app.event("message")
def handle_riley_dm(event, say):
    # DISABLED — Riley v2.0 (LangGraph) on separate Railway service
    # handles all Riley DMs. This handler is intentionally empty.
    return

    # FILE UPLOAD
    if event.get("files"):
        file_info = event["files"][0]
        if not file_info["name"].endswith(
            (".csv", ".xlsx", ".xls")
        ):
            say("⚠️ Please upload a .csv or .xlsx file.")
            return
        say(f"📂 Got *{file_info['name']}* — reading...")
        try:
            path = download_slack_file(
                file_info,
                os.environ.get("RILEY_BOT_TOKEN")
            )
            result = read_contact_list(path, user_id)
            contacts, skipped = (
                result if isinstance(result, tuple)
                else (result, [])
            )
            if skipped:
                say(
                    f"⚠️ {len(skipped)} contacts "
                    f"skipped (no email)"
                )
            if contacts:
                start_outreach_run(
                    user_id=user_id, contacts=contacts,
                    say=say, source="csv"
                )
        except Exception as e:
            say(f"❌ Error: {e}")
        return

    # STOP
    if text.strip().upper() == "STOP":
        if user_id in approval_state:
            stop_requested.add(user_id)
            state = approval_state[user_id]
            if state.get("waiting"):
                from outreach_runner import skip_contact
                result = state.get("pending_result")
                if result:
                    skip_contact(result)
                state["pending_result"] = None
                state["waiting"]        = False
            say(
                "⏹️ *Stopping campaign...*\n"
                "_Finishing current action, then halting._"
            )
        else:
            say(
                "ℹ️ No campaign running.\n"
                "_Type *!run <region>* to start one._"
            )
        return

    # !run
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

        # Fetch good leads
        if segment:
            prospects = get_good_leads_for_region(
                region=segment,
                limit=50,
                status=status
            )
        else:
            prospects = get_prospects_for_outreach(
                status=status, limit=50
            )

        if not prospects:
            seg_hint = (
                f" in *{segment}*" if segment else ""
            )
            say(
                f"📋 No *{status.replace('_', ' ')}* "
                f"good leads{seg_hint}.\n\n"
                f"• *!segments* to see what's available\n"
                f"• Ask Dexter to research businesses"
            )
            return

        say(
            f"✅ Found *{len(prospects)} verified leads*"
            f"{f' in *{segment}*' if segment else ''}.\n"
            f"Starting verified outreach..."
        )

        start_outreach_run(
            user_id=user_id, contacts=prospects,
            say=say, source="db", segment=segment
        )
        return

    # !review
    if text.lower() == "!review":
        queue = get_human_review_queue()
        say(format_human_review_for_slack(queue))
        return

    # !approve/redraft/discard-review
    if text.lower().startswith(
        ("!approve-review", "!redraft-review",
         "!discard-review")
    ):
        if _handle_review_action(user_id, text, say):
            return

    # !pipeline
    if text.lower().startswith("!pipeline"):
        parts  = text.lower().split()
        status = parts[1] if len(parts) > 1 else None
        if status:
            say(format_prospects_for_slack(
                get_prospects(status=status, limit=30),
                f"📋 Prospects — {status}"
            ))
        else:
            say(format_pipeline_summary_for_slack(
                get_pipeline_summary()
            ))
        return

    # !segments
    if text.lower() in ["!segments", "!segment", "!segs"]:
        say(format_segment_summary_for_slack(
            get_segment_summary()
        ))
        return

    # !analytics
    if text.lower().startswith("!analytics"):
        rows = get_analytics_summary(days=30)
        say(format_analytics_for_slack(rows))
        return

    # !mark
    if text.lower().startswith("!mark "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            say("⚠️ *!mark replied <business>*")
            return
        action = parts[1].lower()
        biz    = parts[2].strip()
        if action not in ["replied", "closed"]:
            say("⚠️ Valid: *replied* or *closed*")
            return
        p = get_prospect_by_name(biz)
        if not p:
            say(f"⚠️ Not found: {biz}")
            return
        update_prospect_status(p["id"], action)
        say(
            f"{'💬' if action == 'replied' else '🏁'} "
            f"*{p['business_name']}* → {action}"
        )
        return

    # Other commands
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
        from outreach_runner import set_auto_mode
        set_auto_mode(user_id, True)
        say("⚡ *Auto-send ON* (with verification)")
        return

    if text.lower() == "!automode off":
        from outreach_runner import set_auto_mode
        set_auto_mode(user_id, False)
        say("✋ *Approval mode ON*")
        return

    if text.lower() == "!showprefs":
        from tools.preferences import get_preferences
        prefs = get_preferences(user_id)
        if not prefs:
            say("🧠 No preferences saved yet.")
        else:
            say(
                f"🧠 *Preferences ({len(prefs)}):*\n"
                + "\n".join(
                    f"  {i+1}. {p}"
                    for i, p in enumerate(prefs)
                )
                + "\n\n_*!resetprefs* to clear_"
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

    # Approval reply
    if user_id in approval_state and \
       approval_state[user_id].get("waiting"):
        handle_approval_reply(user_id, text, say)
        return

    # General chat
    say("_Thinking..._")
    say(chat_with_riley(user_id, text))


# ─────────────────────────────────────────
# RESTORE INTERRUPTED RILEY RUNS
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

        approval_state[user_id] = {
            "pending_result":     None,
            "remaining_contacts": list(remaining),
            "waiting":            False,
            "source":             "db",
            "segment":            None,
            "stats":              stats
        }

        def resume(uid, rem, sts):
            try:
                riley_client.chat_postMessage(
                    channel=uid,
                    text=(
                        f"👋 Back after restart.\n"
                        f"*{len(rem)} prospects remaining*.\n"
                        f"Continuing..."
                    )
                )
                def say(msg):
                    riley_client.chat_postMessage(
                        channel=uid, text=msg
                    )
                process_next_contact(uid, say)
            except Exception as e:
                print(f"⚠️  [STARTUP] {e}")

        t        = threading.Thread(
            target=resume,
            args=(user_id, remaining, stats)
        )
        t.daemon = True
        t.start()


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting DaVinci AI v2.0...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    print("📚 Loading conversation history...")
    load_all_conversations()

    print("▶️  Restoring Riley runs...")
    restore_interrupted_runs()

    print("▶️  Restoring Dexter queues...")
    _restore_queues()

    # Start background services
    notify_user = os.environ.get("SLACK_CEO_USER_ID")
    if notify_user:
        from tools.followup_scheduler import (
            start_followup_scheduler
        )
        start_followup_scheduler(riley_client, notify_user)
        print("📅 Follow-up scheduler started.")
    else:
        print(
            "⚠️  SLACK_CEO_USER_ID not set — "
            "follow-up scheduler disabled"
        )

    print("🔬 Starting Dexter...")
    dexter_handler = SocketModeHandler(
        dexter_app,
        os.environ.get("DEXTER_APP_TOKEN")
    )
    dexter_thread        = threading.Thread(
        target=dexter_handler.start
    )
    dexter_thread.daemon = True
    dexter_thread.start()
    print("✅ Dexter live.")

    # print("📧 Starting Riley...")
    # riley_handler = SocketModeHandler(
    #     riley_app,
    #     os.environ.get("RILEY_APP_TOKEN")
    # )

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ DaVinci AI — Dexter only.")
    print("   Dexter → research + verify leads")
    print("   Riley  → handled by separate service")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    # riley_handler.start()