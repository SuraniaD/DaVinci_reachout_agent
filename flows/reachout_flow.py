"""
Phase B — Reachout Flow Orchestrator
Handles the verified send loop:
- Fetch good_lead prospects by region
- Draft email (Riley)
- Verify (x1 × x2)
- Send via Gmail with rate limiting
- Schedule follow-up
- Route failures to human review
"""

import re
from datetime import datetime, timezone

from config import (
    COMBINED_THRESHOLD,
    MAX_DRAFT_RETRIES,
    DAILY_SEND_CAP,
)
from tools.verifier import (
    verify_draft,
    save_verification_result,
)
from tools.email_sender import (
    send_email,
    check_send_limits,
    schedule_followup,
    update_analytics,
)
from tools.prospect_db import (
    update_prospect_status,
    get_human_review_queue,
)
from interaction_log import log_action


def run_verified_send(
    user_id:   str,
    prospect:  dict,
    draft_subject: str,
    draft_body:    str,
    say_fn,
    draft_fn,          # callable(feedback) → new draft
    approval_mode: bool = True
) -> dict:
    """
    Runs the verification loop for one prospect.

    Flow:
    1. Verify draft (x1, x2, combined)
    2a. Pass → send → schedule follow-up → return
    2b. Fail → diagnose → redraft → retry (max 3)
    2c. Max retries → human_review queue → return

    Returns status dict with outcome.
    """
    business_name = prospect.get("business_name", "")
    prospect_id   = str(prospect.get("id", ""))
    region        = prospect.get("region", "unknown")

    revision_count = 0
    current_subject = draft_subject
    current_body    = draft_body

    while revision_count <= MAX_DRAFT_RETRIES:

        # Verify current draft
        vr = verify_draft(
            prospect=prospect,
            draft_subject=current_subject,
            draft_body=current_body,
            revision_count=revision_count
        )

        if vr.passed:
            # Check send limits before sending
            can_send, limit_reason = check_send_limits()
            if not can_send:
                # Save as pending — will be retried
                save_verification_result(
                    prospect_id=prospect_id,
                    draft_subject=current_subject,
                    draft_body=current_body,
                    result=vr,
                    send_status="pending"
                )
                return {
                    "status":  "rate_limited",
                    "reason":  limit_reason
                }

            # SEND
            success, gmail_msg_id, gmail_thread_id = (
                send_email(
                    to_email=prospect.get("email", ""),
                    subject=current_subject,
                    body=current_body,
                    contact_name=(
                        prospect.get("contact_name", "")
                    ),
                    business_name=business_name
                )
            )

            if success:
                # Save verification result
                oe = save_verification_result(
                    prospect_id=prospect_id,
                    draft_subject=current_subject,
                    draft_body=current_body,
                    result=vr,
                    send_status="sent",
                    gmail_message_id=gmail_msg_id,
                    gmail_thread_id=gmail_thread_id
                )

                # Update prospect status
                update_prospect_status(
                    prospect_id=prospect["id"],
                    status="sent"
                )

                # Schedule follow-up
                if oe:
                    schedule_followup(
                        prospect_id=prospect_id,
                        outreach_email_id=oe["id"],
                        followup_number=1
                    )

                # Update analytics
                update_analytics(
                    region=region,
                    industry=prospect.get("industry"),
                    sent=1,
                    x1_score=vr.x1_score,
                    x2_score=vr.x2_score,
                    combined=vr.combined_score
                )

                log_action(
                    action_type="sent",
                    business_name=business_name,
                    detail=(
                        f"x1={vr.x1_score:.2f} "
                        f"x2={vr.x2_score:.2f} "
                        f"combined={vr.combined_score:.3f}"
                    )
                )

                return {
                    "status":           "sent",
                    "x1":               vr.x1_score,
                    "x2":               vr.x2_score,
                    "combined":         vr.combined_score,
                    "gmail_message_id": gmail_msg_id,
                    "gmail_thread_id":  gmail_thread_id,
                }

            else:
                return {
                    "status": "send_failed",
                    "reason": "Gmail API error"
                }

        else:
            # FAIL — check retry budget
            revision_count += 1

            if revision_count > MAX_DRAFT_RETRIES:
                break

            # Redraft with targeted feedback
            say_fn(
                f"⚠️ Draft verification failed "
                f"(attempt {revision_count}/{MAX_DRAFT_RETRIES})\n"
                f"x1={vr.x1_score:.2f} "
                f"x2={vr.x2_score:.2f} "
                f"combined={vr.combined_score:.3f}\n"
                f"_Regenerating..._"
            )

            try:
                new_draft = draft_fn(vr.feedback)
                # Parse new draft
                from agents.riley import parse_draft
                new_subject, new_body = parse_draft(
                    new_draft
                )
                current_subject = new_subject
                current_body    = new_body
            except Exception as e:
                print(
                    f"❌ [REACHOUT FLOW] "
                    f"Redraft error: {e}"
                )
                break

    # MAX RETRIES EXHAUSTED → human review
    vr = verify_draft(
        prospect=prospect,
        draft_subject=current_subject,
        draft_body=current_body,
        revision_count=revision_count
    )

    oe = save_verification_result(
        prospect_id=prospect_id,
        draft_subject=current_subject,
        draft_body=current_body,
        result=vr,
        send_status="human_review"
    )

    update_analytics(
        region=region,
        industry=prospect.get("industry"),
        human_review=1
    )

    log_action(
        action_type="human_review",
        business_name=business_name,
        detail=(
            f"Max retries reached. "
            f"x1={vr.x1_score:.2f} "
            f"x2={vr.x2_score:.2f}"
        )
    )

    return {
        "status":        "human_review",
        "x1":            vr.x1_score,
        "x2":            vr.x2_score,
        "combined":      vr.combined_score,
        "outreach_email": oe,
    }


def format_human_review_for_slack(
    queue: list[dict]
) -> str:
    """Formats human review queue for !review command."""
    if not queue:
        return (
            "✅ Human review queue is empty.\n"
            "All drafts passed verification."
        )

    lines = [
        f"⚠️ *Human Review Queue* "
        f"({len(queue)} items)\n"
    ]

    for i, item in enumerate(queue, 1):
        prospect      = item.get("prospects", {})
        biz           = prospect.get(
            "business_name", "Unknown"
        )
        x1            = item.get("x1_score", 0)
        x2            = item.get("x2_score", 0)
        combined      = item.get("combined_score", 0)
        reason        = item.get("failure_reason", "")
        subject       = item.get("draft_subject", "")
        body_preview  = re.sub(
            r'<[^>]+>', '',
            item.get("draft_body", "")
        )[:80]

        lines.append(
            f"*{i}. {biz}*\n"
            f"   x1: `{x1:.2f}` · "
            f"x2: `{x2:.2f}` · "
            f"combined: `{combined:.3f}`\n"
            f"   _{reason}_\n"
            f"   Subject: {subject}\n"
            f"   Preview: _{body_preview}..._\n"
            f"   *!approve-review {i}* · "
            f"*!redraft-review {i}* · "
            f"*!discard-review {i}*"
        )

    return "\n\n".join(lines)