import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from database import supabase
from interaction_log import log_action

load_dotenv()

GMAIL_ADDRESS     = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")


def send_email(
    to_email:      str,
    subject:       str,
    body:          str,
    contact_name:  str = None,
    business_name: str = None
) -> bool:
    """
    Sends an email via Gmail SMTP.
    Records the sent email in outreach_records table.

    Returns True if sent successfully, False if failed.
    """
    try:
        print(f"📧 Sending email to {to_email}...")

        # Build the email
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        # Connect to Gmail and send
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        print(f"✅ Email sent to {to_email}")

        # Record in outreach_records
        _record_outreach(
            contact_name=contact_name,
            business_name=business_name,
            email_address=to_email,
            subject=subject,
            body=body,
            status="sent"
        )

        # Log the action
        log_action(
            action_type="sent",
            contact_name=contact_name,
            business_name=business_name,
            detail=f"Email sent to {to_email} — Subject: {subject}"
        )

        return True

    except Exception as e:
        print(f"❌ Failed to send email to {to_email}: {e}")

        # Record the failure too — important for debugging
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
    """
    Records a contact as skipped in outreach_records.
    Called when you type 'skip' in the approval flow.
    """
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
    """
    Private helper — writes one row to outreach_records.
    Called by send_email() and record_skipped().
    """
    try:
        row = {
            "contact_name":  contact_name,
            "business_name": business_name,
            "email_address": email_address,
            "subject":       subject,
            "body":          body,
            "status":        status
        }

        # Add sent_at timestamp only for sent emails
        if status == "sent":
            from datetime import datetime, timezone
            row["sent_at"] = datetime.now(timezone.utc).isoformat()

        supabase.table("outreach_records").insert(row).execute()

    except Exception as e:
        print(f"⚠️ Could not record outreach in Supabase: {e}")