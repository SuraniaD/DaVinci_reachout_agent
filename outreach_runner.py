import re
from tools.research import research_business
from tools.email_sender import send_email
from tools.prospect_db import (
    save_draft,
    update_draft_status,
    update_prospect_status,
    get_latest_draft
)
from agents.riley import draft_outreach_email, parse_draft
from interaction_log import log_action
from datetime import datetime, timezone

auto_mode_settings = {}


def is_auto_mode(user_id: str) -> bool:
    return auto_mode_settings.get(user_id, False)


def set_auto_mode(user_id: str, value: bool):
    auto_mode_settings[user_id] = value
    mode = "AUTO-SEND" if value else "APPROVAL"
    print(f"⚙️  [MODE] {mode} for {user_id}")


# ─────────────────────────────────────────
# DB-FIRST PROCESSOR
# Used when Riley reads from prospects table
# Skips immediately if no email in DB
# No web research — Dexter already did it
# ─────────────────────────────────────────

def process_prospect_from_db(
    user_id:   str,
    prospect:  dict,
    say_fn
) -> dict | None:
    """
    Processes one prospect from the DB.
    Skips immediately if no email address — saves tokens.
    Uses research_summary from DB — no web search needed.
    Saves draft to email_drafts table.
    Updates prospect status to draft_ready.
    """
    name         = prospect.get("contact_name") or \
                   prospect.get("business_name", "")
    business     = prospect.get("business_name", "")
    email        = prospect.get("email", "")
    prospect_id  = prospect.get("id")
    research_sum = prospect.get("research_summary", "")
    location     = prospect.get("location", "")
    industry     = prospect.get("industry", "")

    # ── SKIP IF NO EMAIL ─────────────────
    # Skip before drafting — saves tokens and time
    if not email or str(email).strip().lower() in [
        "", "none", "null", "n/a", "not found"
    ]:
        print(
            f"⏭️  [OUTREACH] No email for "
            f"'{business}' — skipping immediately"
        )
        say_fn(
            f"⏭️ Skipping *{business}* — "
            f"no email address in DB.\n"
            f"_Ask Dexter to find the email first._"
        )
        if prospect_id:
            update_prospect_status(
                prospect_id=prospect_id,
                status="skipped"
            )
        return None

    # ── BUILD RESEARCH CONTEXT ────────────
    extra_context = ""
    if location:
        extra_context += f"Location: {location}\n"
    if industry:
        extra_context += f"Industry: {industry}\n"

    full_research = ""
    if extra_context:
        full_research += (
            f"Context from prospect list:\n"
            f"{extra_context}\n"
        )
    if research_sum:
        full_research += (
            f"Research summary:\n{research_sum}"
        )

    if not full_research:
        full_research = (
            f"Business: {business}. "
            f"Write a warm, general outreach email."
        )

    try:
        say_fn(
            f"✍️ Drafting email for "
            f"*{name}* at *{business}*..."
        )

        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=full_research
        )

        subject, body = parse_draft(draft)

        # Save draft to email_drafts table
        draft_row = save_draft(
            prospect_id=prospect_id,
            subject=subject,
            body=body,
            version=1,
            status="pending"
        )

        # Update prospect status → draft_ready
        update_prospect_status(
            prospect_id=prospect_id,
            status="draft_ready"
        )

        draft_id = draft_row["id"] if draft_row else None

        print(
            f"✅ [OUTREACH] Draft saved for "
            f"'{business}' (draft_id={draft_id})"
        )

        return {
            "contact": {
                "name":          name,
                "business_name": business,
                "email":         email,
                "prospect_id":   prospect_id
            },
            "draft":    draft,
            "subject":  subject,
            "body":     body,
            "draft_id": draft_id
        }

    except Exception as e:
        say_fn(
            f"⚠️ Error drafting for *{business}*: "
            f"{e}. Skipping."
        )
        log_action(
            action_type="error",
            contact_name=name,
            business_name=business,
            detail=str(e)
        )
        return None


# ─────────────────────────────────────────
# CSV PROCESSOR (legacy)
# Used when Riley receives a file upload
# Does web research since no DB research exists
# Skips immediately if no email
# ─────────────────────────────────────────

def process_contact(
    user_id: str,
    contact: dict,
    say_fn
) -> dict | None:
    """
    Legacy handler for CSV-uploaded contacts.
    Skips immediately if no email.
    Does web research since contact came from CSV.
    """
    name          = contact.get("name", "")
    business      = contact.get("business_name", "")
    email         = contact.get("email", "")
    extra_context = contact.get("extra_context", "")

    # Skip if no email
    if not email or str(email).strip().lower() in [
        "", "none", "null", "n/a", "not found"
    ]:
        print(
            f"⏭️  [OUTREACH] No email for "
            f"'{business}' — skipping"
        )
        say_fn(
            f"⏭️ Skipping *{business}* — "
            f"no email address."
        )
        return None

    try:
        say_fn(f"🔍 Researching *{business}*...")
        research = research_business(business)

        say_fn(
            f"✍️ Drafting email for "
            f"*{name}* at *{business}*..."
        )

        full_research = ""
        if extra_context:
            full_research += (
                f"Context:\n{extra_context}\n\n"
            )
        full_research += f"Web research:\n{research}"

        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=full_research
        )

        subject, body = parse_draft(draft)

        return {
            "contact": {
                "name":          name,
                "business_name": business,
                "email":         email,
                "prospect_id":   None
            },
            "draft":    draft,
            "subject":  subject,
            "body":     body,
            "draft_id": None
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


# ─────────────────────────────────────────
# SEND APPROVED EMAIL
# Updates draft + prospect status in DB
# ─────────────────────────────────────────

def send_approved_email(result: dict) -> bool:
    """
    Sends the email for an approved result.
    Updates draft status → sent.
    Updates prospect status → sent.
    """
    contact  = result["contact"]
    draft_id = result.get("draft_id")

    success = send_email(
        to_email=contact["email"],
        subject=result["subject"],
        body=result["body"],
        contact_name=contact["name"],
        business_name=contact["business_name"]
    )

    if success and draft_id:
        update_draft_status(
            draft_id=draft_id,
            status="sent",
            sent_at=datetime.now(
                timezone.utc
            ).isoformat()
        )
        prospect_id = contact.get("prospect_id")
        if prospect_id:
            update_prospect_status(
                prospect_id=prospect_id,
                status="sent"
            )

    return success


# ─────────────────────────────────────────
# SKIP CONTACT
# Marks draft as rejected
# Prospect stays at draft_ready for retry
# ─────────────────────────────────────────

def skip_contact(
    result:   dict,
    feedback: str = None
):
    """
    Records a skipped/rejected draft.
    Prospect stays at draft_ready — retryable
    with !run draft_ready.
    Draft marked as rejected with optional feedback.
    """
    contact  = result["contact"]
    draft_id = result.get("draft_id")

    if draft_id:
        update_draft_status(
            draft_id=draft_id,
            status="rejected",
            feedback=feedback
        )

    log_action(
        action_type="skipped",
        contact_name=contact.get("name"),
        business_name=contact.get("business_name"),
        detail=(
            f"Draft skipped — draft_id={draft_id}"
            f"{f', feedback: {feedback}' if feedback else ''}"
        )
    )

    print(
        f"⏭️  Skipped {contact.get('name')} "
        f"@ {contact.get('business_name')}"
    )


# ─────────────────────────────────────────
# SAVE REDRAFT
# New version row in email_drafts
# Old draft marked as rejected
# ─────────────────────────────────────────

def save_redraft(
    result:  dict,
    subject: str,
    body:    str,
    draft:   str
) -> dict:
    """
    Saves a new draft version after CEO feedback.
    Creates new email_drafts row (version + 1).
    Marks old draft as rejected.
    """
    contact     = result["contact"]
    prospect_id = contact.get("prospect_id")

    if prospect_id:
        existing = get_latest_draft(prospect_id)
        version  = (existing["version"] + 1) \
            if existing else 1

        old_draft_id = result.get("draft_id")
        if old_draft_id:
            update_draft_status(
                draft_id=old_draft_id,
                status="rejected"
            )

        new_draft_row = save_draft(
            prospect_id=prospect_id,
            subject=subject,
            body=body,
            version=version,
            status="pending"
        )

        new_draft_id = (
            new_draft_row["id"]
            if new_draft_row else None
        )

        print(
            f"✅ [OUTREACH] Redraft v{version} saved "
            f"(draft_id={new_draft_id})"
        )

        return {
            "contact":  contact,
            "draft":    draft,
            "subject":  subject,
            "body":     body,
            "draft_id": new_draft_id
        }

    return {
        "contact":  contact,
        "draft":    draft,
        "subject":  subject,
        "body":     body,
        "draft_id": None
    }


# ─────────────────────────────────────────
# FORMAT DRAFT FOR SLACK
# Strips HTML tags and token footer
# Clean preview — actual email has full HTML
# ─────────────────────────────────────────

def format_draft_for_slack(result: dict) -> str:
    """
    Formats draft into clean Slack approval message.
    Strips HTML tags — Slack doesn't render them.
    Strips token footer — not needed in draft preview.
    Actual email sent still has full HTML links.
    """
    contact = result["contact"]

    # Strip HTML tags for clean Slack preview
    clean_body = re.sub(r'<[^>]+>', '', result["body"])

    # Strip token footer if somehow present
    divider = "─────────────────────"
    if divider in clean_body:
        clean_body = clean_body[
            :clean_body.index(divider)
        ].strip()

    clean_body = clean_body.strip()

    email_line = (
        f"*To:* {contact['email']}"
        if contact.get("email")
        else "*To:* ⚠️ no email found"
    )

    return (
        f"📩 *Draft for {contact['name']} "
        f"at {contact['business_name']}*\n"
        f"{email_line}\n"
        f"*Subject:* {result['subject']}\n\n"
        f"{clean_body}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Reply *approve* to send · "
        f"*skip* to skip · "
        f"or describe changes to redraft"
    )


# ─────────────────────────────────────────
# GENERATE SUMMARY
# Posted when outreach run completes
# ─────────────────────────────────────────

def generate_summary(
    total:   int,
    sent:    int,
    skipped: int,
    failed:  int
) -> str:
    return (
        f"✅ *Outreach run complete*\n\n"
        f"📊 *Results:*\n"
        f"• Total processed: {total}\n"
        f"• Emails sent:     {sent}\n"
        f"• Skipped:         {skipped}\n"
        f"• Failed:          {failed}\n\n"
        f"_Type *!pipeline* to see the full "
        f"prospect pipeline._"
    )