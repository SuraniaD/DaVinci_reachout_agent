import os
import resend
from datetime import datetime, timezone
from dotenv import load_dotenv
from database import supabase
from interaction_log import log_action

load_dotenv()

resend.api_key = os.environ.get("RESEND_API_KEY")
FROM_EMAIL     = os.environ.get(
    "RESEND_FROM_EMAIL", "riley@davinciai.agency"
)
FROM_NAME      = os.environ.get(
    "RESEND_FROM_NAME", "Riley, DaVinci AI"
)


def send_email(
    to_email:      str,
    subject:       str,
    body:          str,
    contact_name:  str = None,
    business_name: str = None
) -> bool:
    """
    Sends email via Resend API.
    Logs full response to Railway so we can see exactly
    what Resend returns — helps diagnose silent failures.
    """
    try:
        print(f"📧 [EMAIL] Attempting to send to {to_email}")
        print(f"📧 [EMAIL] From: {FROM_NAME} <{FROM_EMAIL}>")
        print(f"📧 [EMAIL] Subject: {subject}")
        print(f"📧 [EMAIL] Resend API key set: {bool(resend.api_key)}")

        params = {
            "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
            "to":      [to_email],
            "subject": subject,
            "text":    body
        }

        print(f"📧 [EMAIL] Sending via Resend...")
        response = resend.Emails.send(params)

        # Log the FULL response so we can see exactly
        # what Resend returned
        print(f"📧 [EMAIL] Full Resend response: {response}")

        # Check response has an ID
        email_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)

        if email_id:
            print(
                f"✅ [EMAIL] Accepted by Resend — "
                f"ID: {email_id} — "
                f"check Resend dashboard for delivery status"
            )
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
                detail=f"Sent to {to_email} — ID: {email_id}"
            )
            return True
        else:
            print(
                f"❌ [EMAIL] Resend returned no ID — "
                f"response: {response}"
            )
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
                detail=f"Resend returned no ID: {response}"
            )
            return False

    except Exception as e:
        print(f"❌ [EMAIL] Exception during send: {e}")
        print(f"❌ [EMAIL] Exception type: {type(e).__name__}")

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
            detail=f"Send exception: {str(e)}"
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
        print(f"⚠️ [DB] Could not record outreach: {e}")