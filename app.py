import os
import time
import tempfile
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv

from memory import load_all_conversations, clear_history
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
from flows.research_flow import run_research_flow
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
from interaction_log import (
    log_action,
    get_recent_logs,
    format_logs_for_slack
)

load_dotenv()

# ─────────────────────────────────────────
# SLACK APPS — DEXTER ONLY
# Riley is handled by separate Railway service
# ─────────────────────────────────────────

dexter_app    = App(
    token=os.environ.get("DEXTER_BOT_TOKEN"),
    signing_secret=os.environ.get("DEXTER_SIGNING_SECRET")
)
dexter_client = WebClient(token=os.environ.get("DEXTER_BOT_TOKEN"))

queue_running  = set()


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
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return sum(
        1 for l in lines if _re.match(r'^[\•\-\*\–\—\d]', l)
    ) >= 2


# ═══════════════════════════════════════════
# DEXTER — RESEARCH RUNNER
# ═══════════════════════════════════════════

def _run_research(
    user_id: str, instruction: str, say,
    industry: str = None, location: str = None
):
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

    t = threading.Thread(target=_run)
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
                        f"_Tell Riley *!run <region>* to start outreach._"
                    )
                    break

                item        = pending[0]
                instruction = item["instruction"]
                queue_id    = item["id"]
                position    = item["position"]
                total_left  = len(pending)

                say(
                    f"🔬 *Queue item {position + 1}* — {total_left} remaining\n"
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
                    err  = str(e).lower()
                    is_rl = any(w in err for w in ["rate_limit", "quota", "exceeded"])

                    if is_rl:
                        rate_limited = True
                        say(
                            f"🔴 *Daily research limit reached.*\n"
                            f"Checking every hour — will resume automatically.\n"
                            f"_{total_left} items queued._"
                        )
                        check_count = 0
                        while True:
                            time.sleep(3600)
                            check_count += 1
                            try:
                                from groq import Groq
                                Groq(
                                    api_key=os.environ.get("GROQ_API_KEY_DEXTER")
                                ).chat.completions.create(
                                    model="openai/gpt-oss-120b",
                                    messages=[{"role": "user", "content": "hi"}],
                                    max_tokens=5
                                )
                                say(f"🟢 *Quota restored!* Resuming — {total_left} remaining...")
                                break
                            except Exception as te:
                                ts = str(te).lower()
                                if any(w in ts for w in ["rate_limit", "quota"]):
                                    say(f"⏳ Still limited ({check_count}h). Checking in 1 hour...")
                                else:
                                    say(f"⚠️ Check error: {str(te)[:80]}\nAttempting to resume...")
                                    break
                    else:
                        mark_failed(queue_id, str(e))
                        say(f"⚠️ Failed: _{instruction}_\n{str(e)[:100]}\nMoving on...")

                if not rate_limited:
                    time.sleep(2)

        except Exception as e:
            print(f"💥 [QUEUE] Crashed: {e}")
            say(f"❌ Queue error: {e}")
        finally:
            queue_running.discard(user_id)

    t = threading.Thread(target=_run)
    t.daemon = True
    t.start()


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

        user_ids = list(set(r["user_id"] for r in result.data))

        for uid in user_ids:
            pending = get_pending_queue(uid)
            if not pending:
                continue

            def notify(user_id, count):
                try:
                    dexter_client.chat_postMessage(
                        channel=user_id,
                        text=f"👋 Back after restart.\nResuming queue — *{count} items remaining*."
                    )
                    def say(msg):
                        dexter_client.chat_postMessage(channel=user_id, text=msg)
                    _run_queue(user_id, say)
                except Exception as e:
                    print(f"⚠️  [STARTUP] {e}")

            t = threading.Thread(target=notify, args=(uid, len(pending)))
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

    print(f"\n🔬 [DEXTER DM] '{text[:60]}{'...' if len(text) > 60 else ''}'")

    if text.lower() in ["!segments", "!segment", "!segs"]:
        cancel_elicitation(user_id)
        say(format_segment_summary_for_slack(get_segment_summary()))
        return

    if text.lower().startswith("!analytics"):
        cancel_elicitation(user_id)
        rows = get_analytics_summary(days=30)
        say(format_analytics_for_slack(rows, days=30))
        return

    if text.lower() == "!cycles":
        cancel_elicitation(user_id)
        requests = get_active_research_requests()
        if not requests:
            say("📋 No active research requests.")
            return
        lines = ["*🔄 Active Research Requests*\n"]
        for r in requests:
            cycles = r.get("search_cycles", [])
            latest = max((c["cycle_index"] for c in cycles), default=0)
            lines.append(
                f"*{r['raw_query']}*\n"
                f"   Cycle {latest}/{7} — "
                f"{r['good_leads_found']}/{r['target_size']} good leads\n"
                f"   Status: {r['status']}"
            )
        say("\n\n".join(lines))
        return

    if text.lower().startswith("!prospects"):
        cancel_elicitation(user_id)
        parts  = text.lower().split()
        status = None
        segment = None
        known_statuses = ["researched", "draft_ready", "approved", "sent", "replied", "closed", "skipped"]
        if len(parts) > 1:
            if parts[1] in known_statuses:
                status = parts[1]
            else:
                segment = " ".join(parts[1:])
        say(format_prospects_for_slack(
            get_prospects(status=status, limit=20, segment=segment),
            title=f"📋 Prospects — {status or segment or 'all'}"
        ))
        return

    if text.lower() == "!resetrun":
        cancel_elicitation(user_id)
        say("🗑️ Research session cancelled.")
        return

    if text.lower() in ["!queue", "!queue status"]:
        pending = get_pending_queue(user_id)
        summary = get_queue_summary(user_id)
        if not pending and summary["complete"] == 0:
            say("📋 No queue active.\n\nPaste a bullet list to queue research:\n```\n• vegan leather UK, 200\n• plant based Japan, 50\n```")
            return
        running = "🟢 *Running*" if user_id in queue_running else "⏸️ *Paused*"
        lines = [f"📋 *Queue* — {running}\n✅ {summary['complete']} · ⏳ {summary['pending']} · ❌ {summary['failed']}\n"]
        for item in pending[:10]:
            lines.append(f"  {item['position']+1}. {item['instruction']}")
        say("\n".join(lines))
        return

    if text.lower() in ["!queue clear", "!clearqueue"]:
        clear_queue(user_id)
        say("🗑️ Queue cleared.")
        return

    if text.lower() in ["!queue resume", "!resumequeue", "!resume"]:
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

    if text.lower().startswith("!add "):
        cancel_elicitation(user_id)
        query = text[5:].strip()
        if not query:
            say("⚠️ Example: *!add Monzo UK*")
            return
        _run_research(user_id=user_id, instruction=query, say=say)
        return

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
                say(start_clarification(user_id=user_id, original=query, industry="businesses", location="the specified area", question=aq))
            else:
                _run_research(user_id=user_id, instruction=query, say=say)
        return

    if _is_bullet_list(text):
        cancel_elicitation(user_id)
        instructions = _parse_bullet_list(text)
        if not instructions:
            say("⚠️ Couldn't parse that list.")
            return
        if not save_queue(user_id, instructions):
            say("❌ Failed to save queue.")
            return
        lines = [f"📋 *Queued {len(instructions)} tasks:*\n"]
        for i, inst in enumerate(instructions):
            lines.append(f"  {i+1}. {inst}")
        lines.append("\n_*!queue* to check · *!queue clear* to cancel_")
        say("\n".join(lines))
        if user_id not in queue_running:
            _run_queue(user_id, say)
        return

    if is_in_elicitation(user_id):
        question, query, industry, location = handle_elicitation_reply(user_id, text)
        if question:
            say(question)
        elif query:
            say(f"✅ Got it — searching for *{query}*...")
            _run_research(user_id=user_id, instruction=query, say=say, industry=industry, location=location)
        return

    research_signals = [
        "find", "search", "look for", "research", "get me", "i need",
        "can you find", "businesses", "companies", "shops", "brands",
        "stores", "clients", "leads", "prospects", "leather", "vegan", "plant"
    ]
    if any(s in text.lower() for s in research_signals):
        if _needs_elicitation(text):
            say(start_elicitation(user_id, text))
        else:
            aq = _detect_ambiguity(text)
            if aq:
                say(start_clarification(user_id=user_id, original=text, industry="businesses", location="the specified area", question=aq))
            else:
                _run_research(user_id=user_id, instruction=text, say=say)
        return

    say("_Thinking..._")
    say(chat_with_dexter(user_id, text))


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting DaVinci AI — Dexter only")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    print("📚 Loading conversation history...")
    load_all_conversations()

    print("▶️  Restoring Dexter queues...")
    _restore_queues()

    notify_user = os.environ.get("SLACK_CEO_USER_ID")
    if notify_user:
        from tools.followup_scheduler import start_followup_scheduler
        from slack_sdk import WebClient as _WC
        _riley_client = _WC(token=os.environ.get("RILEY_BOT_TOKEN", ""))
        start_followup_scheduler(_riley_client, notify_user)
        print("📅 Follow-up scheduler started.")
    else:
        print("⚠️  SLACK_CEO_USER_ID not set — follow-up scheduler disabled")

    print("🔬 Starting Dexter...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ DaVinci AI — Dexter live.")
    print("   Dexter → research + verify leads")
    print("   Riley  → separate Railway service")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Dexter runs on main thread — blocks and keeps process alive
    SocketModeHandler(
        dexter_app,
        os.environ.get("DEXTER_APP_TOKEN")
    ).start()