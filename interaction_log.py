import threading
from database import supabase


# ─────────────────────────────────────────
# LOG AN ACTION
# ─────────────────────────────────────────

def log_action(
    action_type:   str,
    contact_name:  str = None,
    business_name: str = None,
    detail:        str = None
):
    """
    Logs a single action Riley took.

    action_type options:
        "research"  — searched the web for a business
        "draft"     — created an email draft
        "sent"      — sent an email
        "skipped"   — skipped a contact
        "error"     — something went wrong

    contact_name  — person being outreached (optional)
    business_name — their company (optional)
    detail        — extra context, search summary,
                    draft preview, error message etc
    """
    def save():
        try:
            supabase.table("interaction_logs").insert({
                "action_type":   action_type,
                "contact_name":  contact_name,
                "business_name": business_name,
                "detail":        detail
            }).execute()
        except Exception as e:
            print(f"⚠️ Could not log action: {e}")

    # Always async — logging should never slow Riley down
    thread = threading.Thread(target=save)
    thread.daemon = True
    thread.start()


# ─────────────────────────────────────────
# FETCH RECENT LOGS (for Riley to report back)
# ─────────────────────────────────────────

def get_recent_logs(limit: int = 20) -> list:
    """
    Fetches the most recent actions from Supabase.
    Used when you ask Riley "what did you do?" in Slack.
    """
    try:
        result = supabase.table("interaction_logs") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        return result.data
    except Exception as e:
        print(f"⚠️ Could not fetch logs: {e}")
        return []


def format_logs_for_slack(logs: list) -> str:
    """
    Turns the raw log rows into a readable
    Slack message you can actually read.
    """
    if not logs:
        return "No actions logged yet."

    lines = ["*📋 Recent Activity:*\n"]
    for log in reversed(logs):  # show oldest first
        time    = log["created_at"][:16].replace("T", " ")
        action  = log["action_type"].upper()
        contact = log.get("contact_name") or ""
        biz     = log.get("business_name") or ""
        detail  = log.get("detail") or ""

        # Build a clean one-line summary per action
        if contact and biz:
            line = f"`{time}` *{action}* — {contact} @ {biz}"
        elif biz:
            line = f"`{time}` *{action}* — {biz}"
        else:
            line = f"`{time}` *{action}*"

        if detail:
            # Truncate long details so Slack message stays readable
            short_detail = detail[:120] + "..." \
                if len(detail) > 120 else detail
            line += f"\n  _{short_detail}_"

        lines.append(line)

    return "\n\n".join(lines)