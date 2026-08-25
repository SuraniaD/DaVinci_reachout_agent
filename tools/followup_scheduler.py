"""
Follow-up Scheduler — Background Thread
Checks every hour for follow-ups due to be sent.

Follow-up 1: 7 days after original email
Follow-up 2: 14 days after original email

Sends via Resend. Uses Groq FAST_MODEL for drafting.
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta

import resend
from groq import Groq
from database import supabase
from config import (
    FAST_MODEL,
    GROQ_API_KEY,
    FOLLOW_UP_1_DAYS,
    FOLLOW_UP_2_DAYS,
    FOLLOWUP_CHECK_INTERVAL,
    SENDER_EMAIL,
    SENDER_NAME,
    RESEND_API_KEY,
)

resend.api_key = RESEND_API_KEY

_running = False
_thread  = None

SKIP_STATUSES = {
    "replied", "bounced", "unsubscribed",
    "closed", "skipped"
}

BOOKING_LINK = (
    "https://cal.com/deepanshu-surania/"
    "discovery-call?overlayCalendar=true"
)
WEBSITE_LINK = "https://davinciai.agency"


def _get_due_followups() -> list[dict]:
    try:
        now    = datetime.now(timezone.utc).isoformat()
        result = supabase.table("follow_ups") \
            .select(
                "*, "
                "prospects!inner(id, business_name, "
                "email, location, research_summary, "
                "outreach_status, contact_name), "
                "outreach_emails!inner("
                "id, draft_subject)"
            ) \
            .eq("status", "scheduled") \
            .lte("scheduled_for", now) \
            .execute()
        return result.data or []
    except Exception as e:
        print(f"❌ [FOLLOWUP] Fetch failed: {e}")
        return []


def _draft_followup(
    prospect:         dict,
    followup_number:  int,
    original_subject: str
) -> str:
    client        = Groq(api_key=GROQ_API_KEY)
    business_name = prospect.get("business_name", "")
    summary       = prospect.get("research_summary", "")[:400]

    if followup_number == 1:
        prompt = f"""Write a very short follow-up email (2-3 sentences max).

Business: {business_name}
Research: {summary}
Original subject: {original_subject}

- Reference we reached out previously
- Friendly, non-pushy
- One specific reason it's worth a chat
- End with a soft question

Write only the email body. No subject, no greeting, no sign-off.
Max 60 words."""
    else:
        prompt = f"""Write a brief final follow-up email (1-2 sentences only).

Business: {business_name}
Original subject: {original_subject}

- Acknowledge this is the last message
- Leave door open for future contact
- Warm, not passive-aggressive

Write only the body. No subject, no greeting, no sign-off.
Max 40 words."""

    try:
        response = client.chat.completions.create(
            model=FAST_MODEL,
            max_tokens=150,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ [FOLLOWUP] Draft error: {e}")
        if followup_number == 1:
            return (
                "Just wanted to bump this up in case it got "
                "buried — happy to share a few quick ideas on "
                "how similar businesses have saved time with AI "
                "automation. Worth a quick chat?"
            )
        return (
            "Last one from me — if the timing isn't right, "
            "no worries at all. Happy to reconnect whenever."
        )


def _build_html(body: str) -> str:
    cta     = (
        f'Worth a quick <a href="{BOOKING_LINK}" '
        f'style="color:#C9A25D;">15-minute call</a>?'
    )
    signoff = (
        f'Riley, <a href="{WEBSITE_LINK}" '
        f'style="color:#C9A25D;">DaVinci AI</a>'
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,
'Segoe UI',Roboto,sans-serif;font-size:15px;
line-height:1.6;color:#1a1a1a;max-width:560px;
margin:0 auto;padding:24px 16px;}}
a{{color:#C9A25D;text-decoration:none;}}
p{{margin:0 0 16px;}}
</style></head><body>
<p>{body}</p><p>{cta}</p><p>{signoff}</p>
</body></html>"""


def _send_via_resend(
    prospect:         dict,
    body:             str,
    original_subject: str,
) -> tuple[bool, str | None]:
    to_email      = prospect.get("email", "")
    business_name = prospect.get("business_name", "")

    if not to_email:
        return False, None

    try:
        params: resend.Emails.SendParams = {
            "from":     f"{SENDER_NAME} <{SENDER_EMAIL}>",
            "to":       [to_email],
            "subject":  f"Re: {original_subject}",
            "html":     _build_html(body),
            "reply_to": SENDER_EMAIL,
        }
        email     = resend.Emails.send(params)
        resend_id = email.get("id") or email.id
        print(
            f"✅ [FOLLOWUP] Sent to '{business_name}' "
            f"(id={resend_id})"
        )
        return True, resend_id
    except Exception as e:
        print(f"❌ [FOLLOWUP] Resend error: {e}")
        return False, None


def _mark_sent(
    followup_id:       str,
    prospect_id:       str,
    outreach_email_id: str,
    followup_number:   int,
    body:              str,
    resend_id:         str | None
):
    now = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table("follow_ups") \
            .update({"status": "sent", "sent_at": now}) \
            .eq("id", followup_id).execute()

        supabase.table("prospects") \
            .update({
                "follow_up_count":   followup_number,
                "last_contacted_at": now,
            }) \
            .eq("id", prospect_id).execute()

        row: dict = {
            "prospect_id":      prospect_id,
            "draft_body":       body,
            "send_status":      "sent",
            "sent_at":          now,
            "revision_count":   0,
            "passed_threshold": True,
            "x1_score":         1.0,
            "x2_score":         1.0,
            "combined_score":   1.0,
        }
        if resend_id:
            row["gmail_message_id"] = resend_id
        supabase.table("outreach_emails").insert(row).execute()

        if followup_number == 1:
            fu2_date = (
                datetime.now(timezone.utc) +
                timedelta(days=FOLLOW_UP_2_DAYS - FOLLOW_UP_1_DAYS)
            ).isoformat()
            supabase.table("follow_ups").insert({
                "prospect_id":       prospect_id,
                "outreach_email_id": outreach_email_id,
                "follow_up_number":  2,
                "scheduled_for":     fu2_date,
                "status":            "scheduled"
            }).execute()
            print(
                f"📅 [FOLLOWUP] Follow-up 2 scheduled "
                f"in {FOLLOW_UP_2_DAYS - FOLLOW_UP_1_DAYS} days"
            )
        elif followup_number == 2:
            supabase.table("prospects") \
                .update({"outreach_status": "closed"}) \
                .eq("id", prospect_id) \
                .eq("outreach_status", "sent") \
                .execute()

    except Exception as e:
        print(f"❌ [FOLLOWUP] DB update failed: {e}")


def process_due_followups(
    riley_client=None,
    notify_user_id: str = None
):
    due = _get_due_followups()
    if not due:
        print("📅 [FOLLOWUP] No follow-ups due")
        return

    print(f"📅 [FOLLOWUP] {len(due)} due")

    for fu in due:
        try:
            prospect          = fu.get("prospects", {})
            outreach_email    = fu.get("outreach_emails", {})
            followup_id       = fu["id"]
            followup_number   = fu["follow_up_number"]
            outreach_email_id = fu["outreach_email_id"]
            prospect_id       = fu["prospect_id"]
            original_subject  = outreach_email.get(
                "draft_subject", "our previous message"
            )
            business_name = prospect.get("business_name", "Unknown")

            if prospect.get("outreach_status") in SKIP_STATUSES:
                supabase.table("follow_ups") \
                    .update({"status": "cancelled"}) \
                    .eq("id", followup_id).execute()
                print(f"⏭️  [FOLLOWUP] Skipping '{business_name}'")
                continue

            body = _draft_followup(
                prospect, followup_number, original_subject
            )
            success, resend_id = _send_via_resend(
                prospect, body, original_subject
            )

            if success:
                _mark_sent(
                    followup_id, prospect_id,
                    outreach_email_id, followup_number,
                    body, resend_id
                )
                if riley_client and notify_user_id:
                    riley_client.chat_postMessage(
                        channel=notify_user_id,
                        text=(
                            f"📤 Follow-up {followup_number} "
                            f"sent to *{business_name}*."
                        )
                    )

        except Exception as e:
            print(f"💥 [FOLLOWUP] Error: {e}")


def start_followup_scheduler(
    riley_client=None,
    notify_user_id: str = None
):
    global _running, _thread
    if _running:
        return
    _running = True

    def _loop():
        global _running
        print(
            f"▶️  [FOLLOWUP] Started "
            f"(check every {FOLLOWUP_CHECK_INTERVAL}s)"
        )
        while _running:
            try:
                process_due_followups(
                    riley_client, notify_user_id
                )
            except Exception as e:
                print(f"💥 [FOLLOWUP] Loop error: {e}")
            time.sleep(FOLLOWUP_CHECK_INTERVAL)

    _thread        = threading.Thread(target=_loop)
    _thread.daemon = True
    _thread.start()


def stop_followup_scheduler():
    global _running
    _running = False
    print("⏹️  [FOLLOWUP] Stopped")