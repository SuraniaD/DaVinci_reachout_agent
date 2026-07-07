from tools.research import research_business
from tools.email_sender import send_email, record_skipped
from agents.riley import draft_outreach_email, parse_draft
from interaction_log import log_action

# ─────────────────────────────────────────
# AUTO-SEND MODE TOGGLE
# Stored in RAM — resets to False on restart
# (approval mode is the safe default)
# ─────────────────────────────────────────

auto_mode_settings = {}  # { user_id: True/False }


def is_auto_mode(user_id: str) -> bool:
    """Returns True if auto-send is on for this user."""
    return auto_mode_settings.get(user_id, False)


def set_auto_mode(user_id: str, value: bool):
    """Switches auto-send on or off for this user."""
    auto_mode_settings[user_id] = value
    mode = "AUTO-SEND" if value else "APPROVAL"
    print(f"Mode set to {mode} for {user_id}")


# ─────────────────────────────────────────
# PROCESS ONE CONTACT
# Called per row — does research + draft
# Returns contact data + draft for app.py to handle
# ─────────────────────────────────────────

def process_contact(
    user_id: str,
    contact: dict,
    say_fn
) -> dict | None:
    """
    Handles one contact:
    1. Research their business
    2. Draft a personalised email
    3. Return the result for app.py to handle
       (app.py decides approve vs auto-send
        because it controls the Slack conversation)

    say_fn: the Slack say() function —
            used to post status updates to you
            as Riley works through the list

    Returns a dict with everything needed to send:
    {
        "contact":  { name, business_name, email },
        "draft":    "SUBJECT: ...\nBODY:\n...",
        "subject":  "...",
        "body":     "..."
    }
    Or None if something went wrong.
    """
    name     = contact.get("name", "")
    business = contact.get("business_name", "")
    email    = contact.get("email", "")

    try:
        # Step 1 — Research
        say_fn(f"🔍 Researching *{business}*...")
        research = research_business(business)

        # Step 2 — Draft
        say_fn(f"✍️ Drafting email for *{name}* at *{business}*...")
        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=research
        )

        # Step 3 — Parse into subject + body
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


# ─────────────────────────────────────────
# SEND AN APPROVED EMAIL
# Called by app.py after you type "approve"
# ─────────────────────────────────────────

def send_approved_email(result: dict) -> bool:
    """
    Sends the email for an approved contact.
    Returns True if sent, False if failed.
    """
    contact = result["contact"]
    success = send_email(
        to_email=contact["email"],
        subject=result["subject"],
        body=result["body"],
        contact_name=contact["name"],
        business_name=contact["business_name"]
    )
    return success


# ─────────────────────────────────────────
# SKIP A CONTACT
# Called by app.py after you type "skip"
# ─────────────────────────────────────────

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


# ─────────────────────────────────────────
# FORMAT DRAFT FOR SLACK
# Makes the approval message look clean in Slack
# ─────────────────────────────────────────

def format_draft_for_slack(result: dict) -> str:
    """
    Formats a draft into a clean Slack message
    asking for your approval.
    """
    contact = result["contact"]
    name     = contact["name"]
    business = contact["business_name"]
    email    = contact["email"]
    subject  = result["subject"]
    body     = result["body"]

    return (
        f"📩 *Draft for {name} at {business}*\n"
        f"*To:* {email}\n"
        f"*Subject:* {subject}\n\n"
        f"{body}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Reply *approve* to send · "
        f"*skip* to skip · "
        f"or describe changes and I'll redraft"
    )


# ─────────────────────────────────────────
# GENERATE RUN SUMMARY
# Posted in Slack when all contacts are done
# ─────────────────────────────────────────

def generate_summary(
    total:   int,
    sent:    int,
    skipped: int,
    failed:  int
) -> str:
    """
    Builds the final summary message posted in Slack
    when the outreach run is complete.
    """
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