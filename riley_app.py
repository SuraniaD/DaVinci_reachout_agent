"""
riley_app.py — Riley's standalone Slack service with Rex gate.

Separate Railway service from app.py (Dexter-only).

Flow:
  !run / !pipeline → start_outreach_run()
    → process_next_contact() per prospect
      → outreach_runner.process_prospect_from_db()  (draft + verify)
        → rex gate (tools/reviewer.py)
          passed  → auto-send → ✅ to #davinci-outreach
          failed  → 🔍 card  → CEO reviews via !send-N / !edit-N / !skip-N
"""

from __future__ import annotations

import os
import threading

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv

from memory import load_all_conversations
from agents.riley import (
    draft_with_feedback,
    parse_draft,
    chat_with_riley,
)
from outreach_runner import (
    process_prospect_from_db,
    send_approved_email,
    skip_contact,
    save_redraft,
    generate_summary,
    format_draft_for_slack,
    set_auto_mode,
    is_auto_mode,
)
from tools.reviewer import review_draft, format_rex_result_for_slack
from tools.prospect_db import (
    get_prospects_for_outreach,
    format_pipeline_summary_for_slack,
    format_segment_summary_for_slack,
    format_analytics_for_slack,
    get_pipeline_summary,
    get_segment_summary,
    get_analytics_summary,
    get_prospects,
)
from interaction_log import log_action

load_dotenv()

# ─────────────────────────────────────────
# SLACK APP — RILEY ONLY
# ─────────────────────────────────────────

riley_app    = App(
    token=os.environ.get("RILEY_BOT_TOKEN"),
    signing_secret=os.environ.get("RILEY_SIGNING_SECRET")
)
riley_client = WebClient(token=os.environ.get("RILEY_BOT_TOKEN"))

# #davinci-outreach channel ID — set as Railway env var
OUTREACH_CHANNEL_ID = os.environ.get("OUTREACH_CHANNEL_ID", "")

# ─────────────────────────────────────────
# REX PENDING QUEUE (in-memory)
# {index → draft_item_dict}
# draft_item has keys: contact, draft, subject, body, draft_id,
#                      research_summary, x1, x2, combined
# ─────────────────────────────────────────

_rex_pending: dict[int, dict] = {}
_rex_counter: int = 0
_rex_lock    = threading.Lock()


def _rex_next_index() -> int:
    global _rex_counter
    with _rex_lock:
        _rex_counter += 1
        return _rex_counter


# ─────────────────────────────────────────
# PER-USER APPROVAL STATE (non-Rex path)
# ─────────────────────────────────────────

approval_state: dict[str, dict | None] = {}


# ─────────────────────────────────────────
# CHANNEL HELPERS
# ─────────────────────────────────────────

def _post_to_outreach_channel(text: str):
    """Posts a message to #davinci-outreach (silent no-op if not configured)."""
    if not OUTREACH_CHANNEL_ID:
        print("⚠️  [REX] OUTREACH_CHANNEL_ID not set — skipping channel post")
        return
    try:
        riley_client.chat_postMessage(
            channel=OUTREACH_CHANNEL_ID,
            text=text
        )
    except Exception as e:
        print(f"❌ [REX] Channel post failed: {e}")


# ─────────────────────────────────────────
# REX ROUTING
# ─────────────────────────────────────────

def _rex_auto_send(item: dict, say_fn):
    """
    Rex approved → send email + post ✅ to #davinci-outreach.
    """
    contact  = item["contact"]
    business = contact.get("business_name", "?")
    subject  = item["subject"]
    body     = item["body"]

    # Format the ✅ channel announcement
    result = review_draft(
        subject=subject,
        body=body,
        research_summary=item.get("research_summary", ""),
        x1_score=item.get("x1", 1.0),
        x2_score=item.get("x2", 1.0),
        combined_score=item.get("combined", 1.0),
    )
    _post_to_outreach_channel(
        format_rex_result_for_slack(result, business, subject, body, 0)
    )

    success = send_approved_email(item)
    if success:
        say_fn(f"✅ Sent to *{contact['name']}* at *{business}*.")
        log_action(
            action_type="sent",
            contact_name=contact.get("name"),
            business_name=business,
            detail="Rex auto-approved"
        )
    else:
        say_fn(f"⚠️ Failed to send to *{business}*.")


def _rex_flag_for_review(
    item:          dict,
    rex_result,
    say_fn,
    index:         int,
):
    """
    Rex failed → store in _rex_pending + post 🔍 card to #davinci-outreach.
    """
    contact  = item["contact"]
    business = contact.get("business_name", "?")
    subject  = item["subject"]
    body     = item["body"]

    _rex_pending[index] = item

    card = format_rex_result_for_slack(
        rex_result, business, subject, body, index
    )
    _post_to_outreach_channel(card)
    say_fn(
        f"🔍 Rex flagged *{business}* (#{index}) — "
        f"check #davinci-outreach for details."
    )


# ─────────────────────────────────────────
# REX CHANNEL COMMAND HANDLER
# ─────────────────────────────────────────

def _handle_rex_channel_command(text: str, user_id: str, say_fn, client):
    """
    Handles commands sent to #davinci-outreach:
      !review          — list all pending flagged drafts
      !send-N          — override Rex and send draft N
      !edit-N <note>   — redraft with CEO feedback, re-run Rex
      !skip-N          — skip prospect N
    """
    lower = text.strip().lower()

    # ── !review ───────────────────────────
    if lower == "!review":
        if not _rex_pending:
            say_fn("✅ No drafts pending Rex review.")
            return

        lines = [f"🔍 *{len(_rex_pending)} draft(s) pending review:*\n"]
        for idx, item in sorted(_rex_pending.items()):
            c = item["contact"]
            lines.append(
                f"  *#{idx}* — {c.get('name')} at "
                f"{c.get('business_name')} "
                f"| Subject: _{item['subject']}_"
            )
        lines.append(
            "\n`!send-N` · `!edit-N <feedback>` · `!skip-N`"
        )
        say_fn("\n".join(lines))
        return

    # ── !send-N ───────────────────────────
    import re as _re
    m = _re.match(r'^!send-(\d+)$', lower)
    if m:
        idx = int(m.group(1))
        if idx not in _rex_pending:
            say_fn(f"⚠️ No pending draft #{idx}.")
            return
        item    = _rex_pending.pop(idx)
        contact = item["contact"]
        success = send_approved_email(item)
        if success:
            say_fn(
                f"✅ Sent *{contact['name']}* at "
                f"*{contact['business_name']}* "
                f"(CEO override of Rex #{idx})."
            )
            log_action(
                action_type="sent",
                contact_name=contact.get("name"),
                business_name=contact.get("business_name"),
                detail=f"CEO override Rex #{idx}"
            )
        else:
            say_fn(f"❌ Failed sending #{idx}.")
        return

    # ── !edit-N <feedback> ────────────────
    m = _re.match(r'^!edit-(\d+)\s+(.*)', text.strip(), _re.DOTALL | _re.I)
    if m:
        idx      = int(m.group(1))
        feedback = m.group(2).strip()
        if idx not in _rex_pending:
            say_fn(f"⚠️ No pending draft #{idx}.")
            return
        if not feedback:
            say_fn(
                "⚠️ Please give feedback: "
                "`!edit-N <what to change>`"
            )
            return

        item    = _rex_pending.pop(idx)
        contact = item["contact"]
        business = contact.get("business_name", "?")
        say_fn(
            f"✍️ Redrafting *{business}* "
            f"with your feedback..."
        )

        def _do_edit():
            try:
                new_draft, _ = draft_with_feedback(
                    user_id=user_id,
                    feedback=feedback,
                    original_draft=item["draft"],
                    contact_name=contact.get("name", ""),
                    business_name=business
                )
                new_subject, new_body = parse_draft(new_draft)

                # Save the redraft to DB
                updated_item = save_redraft(
                    result=item,
                    subject=new_subject,
                    body=new_body,
                    draft=new_draft
                )
                updated_item["research_summary"] = item.get("research_summary", "")
                updated_item["x1"]      = item.get("x1", 1.0)
                updated_item["x2"]      = item.get("x2", 1.0)
                updated_item["combined"] = item.get("combined", 1.0)

                # Re-run Rex
                rex = review_draft(
                    subject=new_subject,
                    body=new_body,
                    research_summary=updated_item["research_summary"],
                    x1_score=updated_item["x1"],
                    x2_score=updated_item["x2"],
                    combined_score=updated_item["combined"],
                )

                if rex.passed:
                    _rex_auto_send(updated_item, say_fn)
                else:
                    new_idx = _rex_next_index()
                    _rex_flag_for_review(
                        updated_item, rex, say_fn, new_idx
                    )

            except Exception as e:
                say_fn(f"❌ Redraft failed for #{idx}: {e}")

        threading.Thread(target=_do_edit, daemon=True).start()
        return

    # ── !skip-N ───────────────────────────
    m = _re.match(r'^!skip-(\d+)$', lower)
    if m:
        idx = int(m.group(1))
        if idx not in _rex_pending:
            say_fn(f"⚠️ No pending draft #{idx}.")
            return
        item    = _rex_pending.pop(idx)
        contact = item["contact"]
        skip_contact(item, feedback="CEO skipped via !skip")
        say_fn(
            f"⏭️ Skipped *{contact.get('name')}* at "
            f"*{contact.get('business_name')}* (#{idx})."
        )
        return


# ─────────────────────────────────────────
# PROCESS ONE PROSPECT (with Rex gate)
# ─────────────────────────────────────────

def process_next_contact(
    user_id:   str,
    prospect:  dict,
    say_fn,
) -> bool:
    """
    Drafts + verifies one prospect, then routes through Rex.
    Returns True if processing completed (sent or flagged), False if skipped.
    """
    # outreach_runner handles: no-email guard, no-research guard,
    # empty-body guard, and the x1×x2≥0.81 verification loop.
    item = process_prospect_from_db(
        user_id=user_id,
        prospect=prospect,
        say_fn=say_fn
    )

    if item is None:
        return False  # skipped by upstream guard

    subject = item["subject"]
    body    = item["body"]

    # Grab verification scores from the prospect record
    # (stored there by the verifier after passing)
    x1       = float(prospect.get("x1_score", 1.0) or 1.0)
    x2       = float(prospect.get("x2_score", 1.0) or 1.0)
    combined = float(prospect.get("combined_score", 1.0) or 1.0)

    item["research_summary"] = prospect.get("research_summary", "")
    item["x1"]       = x1
    item["x2"]       = x2
    item["combined"] = combined

    # ── REX GATE ─────────────────────────
    rex = review_draft(
        subject=subject,
        body=body,
        research_summary=item["research_summary"],
        x1_score=x1,
        x2_score=x2,
        combined_score=combined,
    )

    if rex.passed:
        _rex_auto_send(item, say_fn)
    else:
        idx = _rex_next_index()
        _rex_flag_for_review(item, rex, say_fn, idx)

    return True


# ─────────────────────────────────────────
# OUTREACH RUN
# ─────────────────────────────────────────

def start_outreach_run(
    user_id:   str,
    segment:   str | None,
    limit:     int,
    say_fn,
):
    """
    Fetches prospects and runs the Rex pipeline on each.
    Called in a daemon thread from handle_riley_dm.
    """
    approval_state[user_id] = None

    prospects = get_prospects_for_outreach(limit=limit, segment=segment)
    if not prospects:
        say_fn(
            "📭 No prospects ready for outreach.\n"
            "_Ask Dexter to research some leads first._"
        )
        return

    total = len(prospects)
    say_fn(
        f"🚀 Starting outreach run — "
        f"*{total} prospect{'s' if total != 1 else ''}*"
        + (f" in segment *{segment}*" if segment else "")
        + ".\n_Rex will auto-send clean drafts and flag anything that needs your eyes._"
    )

    sent = skipped = failed = 0

    for i, prospect in enumerate(prospects, 1):
        business = (
            prospect.get("business_name") or
            prospect.get("contact_name") or
            "?"
        )
        say_fn(
            f"📬 [{i}/{total}] Processing *{business}*..."
        )
        try:
            ok = process_next_contact(
                user_id=user_id,
                prospect=prospect,
                say_fn=say_fn
            )
            if ok:
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            say_fn(f"⚠️ Error on *{business}*: {e}. Skipping.")

    say_fn(generate_summary(total, sent, skipped, failed))

    if _rex_pending:
        say_fn(
            f"🔍 *{len(_rex_pending)} draft(s) still pending Rex review* "
            f"— check #davinci-outreach and use `!send-N`, "
            f"`!edit-N <feedback>`, or `!skip-N`."
        )


# ─────────────────────────────────────────
# SINGLE RILEY EVENT HANDLER
# ─────────────────────────────────────────

@riley_app.event("message")
def handle_riley_dm(event, say, client):
    """
    Routes all messages for Riley:
    - Channel messages (#davinci-outreach) → Rex commands
    - DM messages                          → Riley assistant
    """
    if event.get("bot_id"):
        return

    channel_type = event.get("channel_type", "")
    channel_id   = event.get("channel", "")
    user_id      = event.get("user", "")
    text         = (event.get("text") or "").strip()

    # ── CHANNEL: #davinci-outreach ────────
    if channel_type == "channel" or (
        OUTREACH_CHANNEL_ID and channel_id == OUTREACH_CHANNEL_ID
    ):
        rex_triggers = (
            text.lower().startswith("!send-") or
            text.lower().startswith("!edit-") or
            text.lower().startswith("!skip-") or
            text.lower() == "!review"
        )
        if rex_triggers:
            def _chan_say(msg):
                client.chat_postMessage(
                    channel=channel_id, text=msg
                )
            _handle_rex_channel_command(
                text=text,
                user_id=user_id,
                say_fn=_chan_say,
                client=client
            )
        return  # ignore other channel messages

    # ── DM: Riley assistant ───────────────
    if channel_type != "im":
        return

    print(
        f"\n💌 [RILEY DM] "
        f"'{text[:60]}{'...' if len(text) > 60 else ''}'"
    )

    # ── !pipeline ─────────────────────────
    if text.lower() in ["!pipeline", "!pipe"]:
        say(format_pipeline_summary_for_slack(get_pipeline_summary()))
        return

    # ── !segments ─────────────────────────
    if text.lower() in ["!segments", "!segment", "!segs"]:
        say(format_segment_summary_for_slack(get_segment_summary()))
        return

    # ── !analytics ────────────────────────
    if text.lower().startswith("!analytics"):
        rows = get_analytics_summary(days=30)
        say(format_analytics_for_slack(rows, days=30))
        return

    # ── !prospects ────────────────────────
    if text.lower().startswith("!prospects"):
        from tools.prospect_db import format_prospects_for_slack
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
            get_prospects(status=status, limit=20, segment=segment),
            title=f"📋 Prospects — {status or segment or 'all'}"
        ))
        return

    # ── !review (from DM) ─────────────────
    if text.lower() == "!review":
        if not _rex_pending:
            say("✅ No drafts pending Rex review.")
            return
        lines = [f"🔍 *{len(_rex_pending)} draft(s) pending:*\n"]
        for idx, item in sorted(_rex_pending.items()):
            c = item["contact"]
            lines.append(
                f"  *#{idx}* — {c.get('name')} at "
                f"{c.get('business_name')} "
                f"| _{item['subject']}_"
            )
        lines.append("\n`!send-N` · `!edit-N <feedback>` · `!skip-N`")
        say("\n".join(lines))
        return

    # ── !send-N / !edit-N / !skip-N from DM ──
    import re as _re
    if _re.match(r'^!(send|edit|skip)-\d+', text.lower()):
        _handle_rex_channel_command(
            text=text,
            user_id=user_id,
            say_fn=say,
            client=client
        )
        return

    # ── STOP ──────────────────────────────
    if text.upper() == "STOP":
        approval_state[user_id] = None
        say("🛑 Outreach run stopped.")
        return

    # ── !run [segment] ────────────────────
    if text.lower().startswith("!run"):
        parts   = text.split(maxsplit=1)
        segment = parts[1].strip() if len(parts) > 1 else None
        limit   = int(os.environ.get("OUTREACH_BATCH_SIZE", "10"))
        say(
            f"▶️ Starting Riley outreach"
            + (f" for segment *{segment}*" if segment else "")
            + "..."
        )

        def _run():
            start_outreach_run(
                user_id=user_id,
                segment=segment,
                limit=limit,
                say_fn=say
            )

        threading.Thread(target=_run, daemon=True).start()
        return

    # ── approve / skip (legacy non-Rex path) ─
    if approval_state.get(user_id):
        pending = approval_state[user_id]
        contact = pending["contact"]
        business = contact.get("business_name", "?")
        lower_text = text.lower().strip()

        if lower_text in ["approve", "send", "yes", "ok", "✅"]:
            success = send_approved_email(pending)
            if success:
                say(f"✅ Sent to *{contact['name']}* at *{business}*.")
            else:
                say(f"⚠️ Failed to send to *{business}*.")
            approval_state[user_id] = None
            return

        if lower_text in ["skip", "no", "reject", "❌"]:
            skip_contact(pending)
            say(f"⏭️ Skipped *{business}*.")
            approval_state[user_id] = None
            return

        # Treat as feedback → redraft
        say(f"✍️ Redrafting for *{business}* with your feedback...")

        def _redraft():
            try:
                new_draft, _ = draft_with_feedback(
                    user_id=user_id,
                    feedback=text,
                    original_draft=pending["draft"],
                    contact_name=contact.get("name", ""),
                    business_name=business
                )
                new_subject, new_body = parse_draft(new_draft)
                updated = save_redraft(
                    result=pending,
                    subject=new_subject,
                    body=new_body,
                    draft=new_draft
                )
                approval_state[user_id] = updated
                say(format_draft_for_slack(updated))
            except Exception as e:
                say(f"❌ Redraft failed: {e}")

        threading.Thread(target=_redraft, daemon=True).start()
        return

    # ── General chat ──────────────────────
    say("_Thinking..._")
    say(chat_with_riley(user_id, text))


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Starting DaVinci AI — Riley + Rex")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    print("📚 Loading conversation history...")
    load_all_conversations()

    # Follow-up scheduler (uses Claude Haiku via anthropic SDK)
    notify_user = os.environ.get("SLACK_CEO_USER_ID")
    if notify_user:
        try:
            from tools.followup_scheduler import start_followup_scheduler
            start_followup_scheduler(riley_client, notify_user)
            print("📅 Follow-up scheduler started.")
        except Exception as e:
            print(f"⚠️  Follow-up scheduler failed to start: {e}")
    else:
        print(
            "⚠️  SLACK_CEO_USER_ID not set — "
            "follow-up scheduler disabled"
        )

    if not OUTREACH_CHANNEL_ID:
        print(
            "⚠️  OUTREACH_CHANNEL_ID not set — "
            "Rex channel posts disabled"
        )
    else:
        print(
            f"📣 Rex will post to channel: "
            f"{OUTREACH_CHANNEL_ID}"
        )

    print("💌 Starting Riley...")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("✅ Riley live.")
    print("   Riley → email drafting + Rex gate")
    print("   Rex   → 10-rule reviewer (no LLM)")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    SocketModeHandler(
        riley_app,
        os.environ.get("RILEY_APP_TOKEN")
    ).start()
