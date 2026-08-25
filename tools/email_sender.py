"""
Sender Service — Resend API
Sends outreach emails via Resend.
Includes rate limiting, send spacing, bounce tracking.

Reply threading: Resend doesn't have native threading.
We store a resend_email_id per sent email so the
reply detector can match replies in Gmail inbox
by subject/from-address matching instead of thread_id.

Follow-ups are sent as new emails with
"Re: {original_subject}" as the subject.
"""

import os
import time
import random
import resend
from datetime import datetime, timezone, timedelta

from database import supabase
from config import (
    SENDER_EMAIL,
    SENDER_NAME,
    DAILY_SEND_CAP,
    SEND_DELAY_MIN,
    SEND_DELAY_MAX,
    MAX_BOUNCE_RATE,
)

# Initialise Resend with API key
resend.api_key = os.environ.get("RESEND_API_KEY", "")

BOOKING_LINK = (
    "https://cal.com/deepanshu-surania/"
    "discovery-call?overlayCalendar=true"
)
WEBSITE_LINK = "https://davinciai.agency"


def _get_sent_today() -> int:
    """Count emails sent today."""
    try:
        today  = datetime.now(timezone.utc).date()
        result = supabase.table("outreach_emails") \
            .select("id", count="exact") \
            .eq("send_status", "sent") \
            .gte("sent_at", today.isoformat()) \
            .execute()

        return result.count or 0

    except Exception as e:
        print(f"⚠️  [EMAIL SENDER] Sent count error: {e}")
        return 0


def _get_bounce_rate_today() -> float:
    """Returns today's bounce rate."""
    try:
        today  = datetime.now(timezone.utc).date()
        result = supabase.table("outreach_emails") \
            .select("send_status") \
            .gte("sent_at", today.isoformat()) \
            .in_("send_status", ["sent", "bounced"]) \
            .execute()

        rows    = result.data or []
        total   = len(rows)
        bounced = sum(
            1 for r in rows
            if r["send_status"] == "bounced"
        )

        return bounced / total if total > 0 else 0.0

    except Exception as e:
        print(f"⚠️  [EMAIL SENDER] Bounce rate error: {e}")
        return 0.0


def check_send_limits() -> tuple[bool, str]:
    """
    Returns (can_send, reason_if_not).
    Checks daily cap and bounce rate.
    """
    sent_today = _get_sent_today()
    if sent_today >= DAILY_SEND_CAP:
        return (
            False,
            f"Daily send cap reached "
            f"({sent_today}/{DAILY_SEND_CAP})"
        )

    bounce_rate = _get_bounce_rate_today()
    if bounce_rate > MAX_BOUNCE_RATE:
        return (
            False,
            f"Bounce rate too high "
            f"({bounce_rate:.1%} > "
            f"{MAX_BOUNCE_RATE:.1%}) — "
            f"pausing to protect domain reputation"
        )

    return True, ""


def _build_html_body(body: str) -> str:
    """
    Wraps plain/partial HTML body in a clean
    email-safe HTML wrapper.
    Keeps existing HTML if already present.
    """
    # If body already has full HTML structure, use as-is
    if "<html" in body.lower():
        return body

    # Wrap in minimal email-safe HTML
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont,
        'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      color: #1a1a1a;
      max-width: 560px;
      margin: 0 auto;
      padding: 24px 16px;
    }}
    a {{ color: #C9A25D; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    p {{ margin: 0 0 16px; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


def send_email(
    to_email:      str,
    subject:       str,
    body:          str,
    contact_name:  str = "",
    business_name: str = "",
    thread_id:     str = None,   # ignored for Resend
) -> tuple[bool, str | None, str | None]:
    """
    Sends one email via Resend API.

    Returns (success, resend_email_id, None).
    Third return value is None — Resend has no thread_id.
    Caller stores resend_email_id as gmail_message_id
    for reply matching via subject.

    Includes send spacing delay between sends.
    """
    print(
        f"📧 [EMAIL SENDER] Sending to "
        f"{business_name} <{to_email}>"
    )

    if not resend.api_key:
        print("❌ [EMAIL SENDER] RESEND_API_KEY not set")
        return False, None, None

    try:
        html_body = _build_html_body(body)

        params: resend.Emails.SendParams = {
            "from":    f"{SENDER_NAME} <{SENDER_EMAIL}>",
            "to":      [to_email],
            "subject": subject,
            "html":    html_body,
            "reply_to": SENDER_EMAIL,
        }

        email = resend.Emails.send(params)
        resend_id = email.get("id") or email.id

        print(
            f"✅ [EMAIL SENDER] Sent — "
            f"resend_id={resend_id}"
        )

        # Send spacing — random delay between sends
        delay = random.randint(SEND_DELAY_MIN, SEND_DELAY_MAX)
        print(f"⏳ [EMAIL SENDER] Spacing delay: {delay}s")
        time.sleep(delay)

        # resend_id stored as gmail_message_id
        # gmail_thread_id is None (Resend has no threading)
        return True, resend_id, None

    except Exception as e:
        print(f"❌ [EMAIL SENDER] Resend failed: {e}")
        return False, None, None


def schedule_followup(
    prospect_id:       str,
    outreach_email_id: str,
    followup_number:   int = 1
):
    """Schedules a follow-up email in the DB."""
    from config import FOLLOW_UP_1_DAYS, FOLLOW_UP_2_DAYS

    days = (
        FOLLOW_UP_1_DAYS
        if followup_number == 1
        else FOLLOW_UP_2_DAYS
    )
    scheduled_for = (
        datetime.now(timezone.utc) +
        timedelta(days=days)
    ).isoformat()

    try:
        supabase.table("follow_ups") \
            .insert({
                "prospect_id":       prospect_id,
                "outreach_email_id": outreach_email_id,
                "follow_up_number":  followup_number,
                "scheduled_for":     scheduled_for,
                "status":            "scheduled"
            }) \
            .execute()

        print(
            f"📅 [EMAIL SENDER] "
            f"Follow-up {followup_number} scheduled "
            f"in {days} days"
        )

    except Exception as e:
        print(
            f"❌ [EMAIL SENDER] "
            f"Schedule follow-up failed: {e}"
        )


def update_analytics(
    region:       str,
    industry:     str   = None,
    sent:         int   = 0,
    bounced:      int   = 0,
    x1_score:     float = None,
    x2_score:     float = None,
    combined:     float = None,
    human_review: int   = 0,
):
    """Updates campaign_analytics for today."""
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        existing = supabase.table("campaign_analytics") \
            .select("*") \
            .eq("region", region) \
            .eq("date", today) \
            .limit(1) \
            .execute()

        if existing.data:
            row    = existing.data[0]
            row_id = row["id"]
            updates: dict = {}

            if sent:
                updates["emails_sent"] = (
                    row.get("emails_sent", 0) + sent
                )
            if bounced:
                updates["emails_bounced"] = (
                    row.get("emails_bounced", 0) + bounced
                )
            if human_review:
                updates["human_review_count"] = (
                    row.get("human_review_count", 0)
                    + human_review
                )

            n = row.get("emails_sent", 0)

            if x1_score is not None:
                prev = row.get("avg_x1_score")
                updates["avg_x1_score"] = (
                    ((prev or x1_score) * n + x1_score)
                    / (n + 1) if prev else x1_score
                )
            if x2_score is not None:
                prev = row.get("avg_x2_score")
                updates["avg_x2_score"] = (
                    ((prev or x2_score) * n + x2_score)
                    / (n + 1) if prev else x2_score
                )
            if combined is not None:
                prev = row.get("avg_combined_score")
                updates["avg_combined_score"] = (
                    ((prev or combined) * n + combined)
                    / (n + 1) if prev else combined
                )

            if updates:
                supabase.table("campaign_analytics") \
                    .update(updates) \
                    .eq("id", row_id) \
                    .execute()

        else:
            supabase.table("campaign_analytics") \
                .insert({
                    "region":             region,
                    "industry":           industry,
                    "date":               today,
                    "emails_sent":        sent,
                    "emails_bounced":     bounced,
                    "human_review_count": human_review,
                    "avg_x1_score":       x1_score,
                    "avg_x2_score":       x2_score,
                    "avg_combined_score": combined,
                }) \
                .execute()

    except Exception as e:
        print(f"⚠️  [EMAIL SENDER] Analytics update: {e}")