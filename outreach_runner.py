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
    print(f"⚙️  [MODE] Set to {mode} for {user_id}")


def process_contact(
    user_id: str,
    contact: dict,
    say_fn
) -> dict | None:
    """
    Handles one contact — research + draft.
    Now passes extra_context from sheet columns
    into the draft for better personalisation.
    """
    name          = contact.get("name", "")
    business      = contact.get("business_name", "")
    email         = contact.get("email", "")
    extra_context = contact.get("extra_context", "")

    try:
        say_fn(f"🔍 Researching *{business}*...")
        research = research_business(business)

        say_fn(
            f"✍️ Drafting email for "
            f"*{name}* at *{business}*..."
        )

        # Combine web research with sheet context
        full_research = ""
        if extra_context:
            full_research += (
                f"Context from prospect list:\n"
                f"{extra_context}\n\n"
            )
        full_research += (
            f"Web research:\n{research}"
        )

        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=full_research
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
            f"⚠️ Error processing *{name}* "
            f"at *{business}*: {e}. Skipping."
        )
        log_action(
            action_type="error",
            contact_name=name,
            business_name=business,
            detail=str(e)
        )
        return None


def send_approved_email(result: dict) -> bool:
    contact = result["contact"]
    return send_email(
        to_email=contact["email"],
        subject=result["subject"],
        body=result["body"],
        contact_name=contact["name"],
        business_name=contact["business_name"]
    )


def skip_contact(result: dict):
    contact = result["contact"]
    record_skipped(
        contact_name=contact["name"],
        business_name=contact["business_name"],
        email_address=contact["email"],
        subject=result.get("subject"),
        body=result.get("body")
    )


def format_draft_for_slack(result: dict) -> str:
    contact = result["contact"]
    return (
        f"📩 *Draft for {contact['name']} "
        f"at {contact['business_name']}*\n"
        f"*To:* {contact['email']}\n"
        f"*Subject:* {result['subject']}\n\n"
        f"{result['body']}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Reply *approve* to send · "
        f"*skip* to skip · "
        f"or describe changes to redraft"
    )


def generate_summary(
    total:   int,
    sent:    int,
    skipped: int,
    failed:  int
) -> str:
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