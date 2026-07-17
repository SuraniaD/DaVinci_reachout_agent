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


def _text_to_html(body: str) -> str:
    """
    Converts plain text email body to clean HTML.
    Preserves line breaks and renders any HTML links
    that Riley included (e.g. <a href="...">...</a>).
    """
    # Split into paragraphs on double newlines
    paragraphs = body.strip().split("\n\n")

    html_parts = []
    for para in paragraphs:
        # Convert single newlines within paragraph to <br>
        para_html = para.replace("\n", "<br>")
        html_parts.append(f"<p>{para_html}</p>")

    body_html = "\n".join(html_parts)

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: Arial, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      color: #222222;
      max-width: 600px;
      margin: 0 auto;
      padding: 20px;
    }}
    p {{
      margin: 0 0 16px 0;
    }}
    a {{
      color: #0066cc;
      text-decoration: underline;
    }}
  </style>
</head>
<body>
{body_html}
</body>
</html>"""


def send_email(
    to_email:      str,
    subject:       str,
    body:          str,
    contact_name:  str = None,
    business_name: str = None
) -> bool:
    """
    Sends email via Resend API as HTML.
    Converts Riley's plain text body to HTML
    so hyperlinks in the sign off and CTA render.
    """
    try:
        print(f"📧 [EMAIL] Sending to {to_email}...")
        print(f"📧 [EMAIL] From: {FROM_NAME} <{FROM_EMAIL}>")
        print(f"📧 [EMAIL] Subject: {subject}")

        html_body = _text_to_html(body)

        params = {
            "from":    f"{FROM_NAME} <{FROM_EMAIL}>",
            "to":      [to_email],
            "subject": subject,
            "html":    html_body,
            # Plain text fallback for email clients
            # that don't render HTML
            "text":    body
        }

        print(f"📧 [EMAIL] Sending via Resend...")
        response = resend.Emails.send(params)
        print(f"📧 [EMAIL] Full response: {response}")

        email_id = (
            response.get("id")
            if isinstance(response, dict)
            else getattr(response, "id", None)
        )

        if email_id:
            print(
                f"✅ [EMAIL] Accepted by Resend — "
                f"ID: {email_id}"
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
                f"❌ [EMAIL] No ID returned — "
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
                detail=f"No ID: {response}"
            )
            return False

    except Exception as e:
        print(f"❌ [EMAIL] Exception: {e}")
        print(f"❌ [EMAIL] Type: {type(e).__name__}")

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
            detail=f"Exception: {str(e)}"
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
    print(f"⏭️  Skipped {contact_name} @ {business_name}")


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
            row["sent_at"] = datetime.now(
                timezone.utc
            ).isoformat()

        supabase.table("outreach_records").insert(row).execute()

    except Exception as e:
        print(f"⚠️  [DB] Could not record outreach: {e}")