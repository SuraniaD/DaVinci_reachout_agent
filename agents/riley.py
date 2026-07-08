from tools.research import research_business
from tools.email_sender import send_email, record_skipped
from agents.riley import draft_outreach_email, parse_draft
from interaction_log import log_action

auto_mode_settings = {}


def is_auto_mode(user_id: str) -> bool:
    return auto_mode_settings.get(user_id, False)


def set_auto_mode(user_id: str, value: bool):
    auto_mode_settings[user_id] = value
    mode = "AUTO-SEND" if value else "APPROVAL"
    print(f"Mode set to {mode} for {user_id}")


def process_contact(
    user_id: str,
    contact: dict,
    say_fn
) -> dict | None:
    """
    Researches and drafts for one contact.
    Returns result dict or None if something went wrong.
    """
    name     = contact.get("name", "")
    business = contact.get("business_name", "")
    email    = contact.get("email", "")

    try:
        say_fn(f"🔍 Researching *{business}*...")
        research = research_business(business)

        say_fn(f"✍️ Drafting email for *{name}* at *{business}*...")
        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=research
        )

        subject, body = parse_draft(draft)

        return {
            "contact": contact,
            "draft":   draft,
            "subject": subject,
            "body":    body
        }

    except Exception as e:
        say_fn(
            f"⚠️ Something went wrong processing "
            f"*{name}* at *{business}*: {e}. Skipping."
        )
        log_action(
            action_type="error",
            contact_name=name,
            business_name=business,
            detail=str(e)
        )
        return None


def send_approved_email(result: dict) -> bool:
    """Sends the email for an approved contact."""
    contact = result["contact"]
    return send_email(
        to_email=contact["email"],
        subject=result["subject"],
        body=result["body"],
        contact_name=contact["name"],
        business_name=contact["business_name"]
    )


def skip_contact(result: dict):
    """Records a contact as skipped."""
    contact = result["contact"]
    record_skipped(
        contact_name=contact["name"],
        business_name=contact["business_name"],
        email_address=contact["email"],
        subject=result.get("subject"),
        body=result.get("body")
    )


def format_draft_for_slack(result: dict) -> str:
    """Formats a draft into a clean Slack approval message."""
    contact = result["contact"]
    return (
        f"📩 *Draft for {contact['name']} at {contact['business_name']}*\n"
        f"*To:* {contact['email']}\n"
        f"*Subject:* {result['subject']}\n\n"
        f"{result['body']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Reply *approve* to send · "
        f"*skip* to skip · "
        f"or describe changes and I'll redraft"
    )


def generate_summary(
    total:   int,
    sent:    int,
    skipped: int,
    failed:  int
) -> str:
    """Final summary posted in Slack when run is complete."""
    return (
        f"✅ *Outreach run complete*\n\n"
        f"📊 *Results:*\n"
        f"• Total contacts: {total}\n"
        f"• Emails sent:    {sent}\n"
        f"• Skipped:        {skipped}\n"
        f"• Failed:         {failed}\n\n"
        f"Full details in your Supabase dashboard "
        f"under `outreach_records`."
    )