import os
import resend
from datetime import datetime, timezone
from dotenv import load_dotenv
from database import supabase
from interaction_log import log_action

load_dotenv()

resend.api_key = os.environ.get("RESEND_API_KEY")
FROM_EMAIL     = os.environ.get("RESEND_FROM_EMAIL", "riley@davinciai.agency")
FROM_NAME      = os.environ.get("RESEND_FROM_NAME", "Riley, DaVinci AI")


def send_email(
    to_email:      str,
    subject:       str,
    body:          str,
    contact_name:  str = None,
    business_name: str = None
) -> bool:
    """
    Sends an email via Resend API.
    Records the result in Supabase outreach_records.
    Returns True if sent, False if failed.
    """
    try:
        print(f"📧 Sending email to {to_email}...")

        params = {
            "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
            "to":      [to_email],
            "subject": subject,
            "text":    body
        }

        response = resend.Emails.send(params)

        print(f"✅ Email sent to {to_email} — ID: {response['id']}")

        _record_outreach(
            contact_name=contact_name,
            business_name=business_name,
            email_address=to_email,
            subject=subject,
            body=body,
            status="sent"
        )

        log_action(
            action_type="sent",
            contact_name=contact_name,
            business_name=business_name,
            detail=f"Email sent to {to_email} — Subject: {subject}"
        )

        return True

    except Exception as e:
        print(f"❌ Failed to send email to {to_email}: {e}")

        _record_outreach(
            contact_name=contact_name,
            business_name=business_name,
            email_address=to_email,
            subject=subject,
            body=body,
            status="failed"
        )

        log_action(
            action_type="error",
            contact_name=contact_name,
            business_name=business_name,
            detail=f"Email send failed: {str(e)}"
        )

        return False


def record_skipped(
    contact_name:  str,
    business_name: str,
    email_address: str,
    subject:       str = None,
    body:          str = None
):
    """Records a skipped contact in Supabase."""
    _record_outreach(
        contact_name=contact_name,
        business_name=business_name,
        email_address=email_address,
        subject=subject,
        body=body,
        status="skipped"
    )

    log_action(
        action_type="skipped",
        contact_name=contact_name,
        business_name=business_name,
        detail=f"Skipped {email_address}"
    )

    print(f"⏭️ Skipped {contact_name} at {business_name}")


def _record_outreach(
    contact_name:  str,
    business_name: str,
    email_address: str,
    subject:       str,
    body:          str,
    status:        str
):
    """Writes one row to outreach_records in Supabase."""
    try:
        row = {
            "contact_name":  contact_name,
            "business_name": business_name,
            "email_address": email_address,
            "subject":       subject,
            "body":          body,
            "status":        status
        }

        if status == "sent":
            row["sent_at"] = datetime.now(timezone.utc).isoformat()

        supabase.table("outreach_records").insert(row).execute()

    except Exception as e:
        print(f"⚠️ Could not record outreach in Supabase: {e}")