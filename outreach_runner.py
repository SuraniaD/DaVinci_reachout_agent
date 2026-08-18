import re
from tools.research import research_business
from tools.email_sender import send_email
from tools.prospect_db import (
    save_draft,
    update_draft_status,
    update_prospect_status,
    get_latest_draft
)
from agents.riley import (
    draft_outreach_email,
    draft_with_feedback,
    parse_draft
)
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
# ─────────────────────────────────────────

def process_prospect_from_db(
    user_id:  str,
    prospect: dict,
    say_fn
) -> dict | None:
    """
    Processes one prospect from the DB.

    Guards:
    1. Skip if no email
    2. Skip if research_summary missing or < 50 chars
    3. Skip if draft body is empty after parsing

    Saves draft to email_drafts.
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

    # ── GUARD 1: no email ────────────────
    if not email or str(email).strip().lower() in [
        "", "none", "null", "n/a", "not found"
    ]:
        print(
            f"⏭️  [OUTREACH] No email for "
            f"'{business}' — skipping"
        )
        say_fn(
            f"⏭️ Skipping *{business}* — "
            f"no email address.\n"
            f"_Ask Dexter to find the email first._"
        )
        if prospect_id:
            update_prospect_status(
                prospect_id=prospect_id,
                status="skipped"
            )
        return None

    # ── GUARD 2: no research summary ─────
    if not research_sum or \
       len(research_sum.strip()) < 50:
        print(
            f"⏭️  [OUTREACH] No research summary for "
            f"'{business}' — skipping"
        )
        say_fn(
            f"⏭️ Skipping *{business}* — "
            f"no research summary in DB.\n"
            f"_Ask Dexter to re-research this business._"
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
        full_research += f"Context:\n{extra_context}\n"
    full_research += f"Research summary:\n{research_sum}"

    try:
        say_fn(
            f"✍️ Drafting for *{name}* "
            f"at *{business}*..."
        )

        draft = draft_outreach_email(
            user_id=user_id,
            contact_name=name,
            business_name=business,
            research=full_research
        )

        # ── DEBUG: log raw draft ──────────
        print(
            f"📝 [OUTREACH] Raw draft for "
            f"'{business}':\n"
            f"{'─' * 40}\n"
            f"{draft}\n"
            f"{'─' * 40}"
        )

        subject, body = parse_draft(draft)

        # ── DEBUG: log parsed result ──────
        print(
            f"📝 [OUTREACH] Parsed — "
            f"subject='{subject}' "
            f"body_len={len(body.strip())}"
        )

        # ── GUARD 3: empty body ───────────
        # Strip HTML and CTA to check if there's
        # actual body content from the model
        body_check = re.sub(r'<[^>]+>', '', body)
        body_check = body_check.replace(
            "Worth a quick 15-minute call?", ""
        ).replace(
            "Riley, DaVinci AI", ""
        ).strip()

        if len(body_check) < 30:
            print(
                f"⏭️  [OUTREACH] Empty body for "
                f"'{business}' — skipping\n"
                f"Raw draft was:\n{draft}"
            )
            say_fn(
                f"⏭️ Skipping *{business}* — "
                f"model returned an empty draft.\n"
                f"_Research summary may be too thin. "
                f"Ask Dexter to re-research._"
            )
            if prospect_id:
                update_prospect_status(
                    prospect_id=prospect_id,
                    status="skipped"
                )
            return None

        # Save draft to email_drafts table
        draft_row = save_draft(
            prospect_id=prospect_id,
            subject=subject,
            body=body,
            version=1,
            status="pending"
        )

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
# ─────────────────────────────────────────

def process_contact(
    user_id: str,
    contact: dict,
    say_fn
) -> dict | None:
    """
    Legacy handler for CSV-uploaded contacts.
    Skips immediately if no email.
    Does web research since no DB summary exists.
    """
    name          = contact.get("name", "")
    business      = contact.get("business_name", "")
    email         = contact.get("email", "")
    extra_context = contact.get("extra_context", "")

    if not email or str(email).strip().lower() in [
        "", "none", "null", "n/a", "not found"
    ]:
        print(
            f"⏭️  [OUTREACH] No email for "
            f"'{business}' — skipping"
        )
        say_fn(
            f"⏭️ Skipping *{business}* — no email."
        )
        return None

    try:
        say_fn(f"🔍 Researching *{business}*...")
        research = research_business(business)

        say_fn(
            f"✍️ Drafting for *{name}* "
            f"at *{business}*..."
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
# ─────────────────────────────────────────

def send_approved_email(result: dict) -> bool:
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
# ─────────────────────────────────────────

def skip_contact(
    result:   dict,
    feedback: str = None
):
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
# ─────────────────────────────────────────

def save_redraft(
    result:  dict,
    subject: str,
    body:    str,
    draft:   str
) -> dict:
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
# ─────────────────────────────────────────

def format_draft_for_slack(result: dict) -> str:
    contact = result["contact"]

    # Strip HTML for Slack preview
    clean_body = re.sub(r'<[^>]+>', '', result["body"])

    # Strip token footer if present
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