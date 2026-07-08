import threading
from database import supabase


def log_action(
    action_type:   str,
    contact_name:  str = None,
    business_name: str = None,
    detail:        str = None
):
    """Logs any action Riley takes — always async."""
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

    thread = threading.Thread(target=save)
    thread.daemon = True
    thread.start()


def get_recent_logs(limit: int = 20) -> list:
    """Fetches most recent actions from Supabase."""
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
    """Formats logs into a readable Slack message."""
    if not logs:
        return "No actions logged yet."

    lines = ["*📋 Recent Activity:*\n"]
    for log in reversed(logs):
        time    = log["created_at"][:16].replace("T", " ")
        action  = log["action_type"].upper()
        contact = log.get("contact_name") or ""
        biz     = log.get("business_name") or ""
        detail  = log.get("detail") or ""

        if contact and biz:
            line = f"`{time}` *{action}* — {contact} @ {biz}"
        elif biz:
            line = f"`{time}` *{action}* — {biz}"
        else:
            line = f"`{time}` *{action}*"

        if detail:
            short = detail[:120] + "..." \
                if len(detail) > 120 else detail
            line += f"\n  _{short}_"

        lines.append(line)

    return "\n\n".join(lines)