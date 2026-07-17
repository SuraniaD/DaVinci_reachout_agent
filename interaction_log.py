import threading
from database import supabase


def log_action(
    action_type:   str,
    contact_name:  str = None,
    business_name: str = None,
    detail:        str = None
):
    """
    Logs any Riley action to Supabase — always async.
    Also prints to Railway logs immediately so you
    can see exactly what Riley is doing in real time.
    """
    # Print to Railway logs immediately — synchronous
    # This shows up in Railway dashboard right away
    _print_log(action_type, contact_name, business_name, detail)

    # Save to Supabase in background — async
    def save():
        try:
            supabase.table("interaction_logs").insert({
                "action_type":   action_type,
                "contact_name":  contact_name,
                "business_name": business_name,
                "detail":        detail
            }).execute()
        except Exception as e:
            print(f"⚠️  [DB] Could not save log to Supabase: {e}")

    thread = threading.Thread(target=save)
    thread.daemon = True
    thread.start()


def _print_log(
    action_type:   str,
    contact_name:  str = None,
    business_name: str = None,
    detail:        str = None
):
    """
    Prints a clearly formatted log line to stdout.
    Stdout goes straight to Railway's log viewer.
    """
    # Pick an emoji per action type
    icons = {
        "file_read":  "📂",
        "research":   "🔍",
        "draft":      "✍️ ",
        "sent":       "✅",
        "skipped":    "⏭️ ",
        "failed":     "❌",
        "error":      "💥",
        "automode":   "⚡",
        "reset":      "🔄",
        "status":     "📋",
        "chat":       "💬",
    }
    icon = icons.get(action_type.lower(), "▸ ")

    # Build the log line
    parts = [f"{icon} [{action_type.upper()}]"]

    if contact_name and business_name:
        parts.append(f"{contact_name} @ {business_name}")
    elif contact_name:
        parts.append(contact_name)
    elif business_name:
        parts.append(business_name)

    if detail:
        # Truncate very long details for readability
        short = detail[:200] + "..." if len(detail) > 200 else detail
        parts.append(f"— {short}")

    print(" ".join(parts))


def get_recent_logs(limit: int = 20) -> list:
    """Fetches most recent logs from Supabase."""
    try:
        result = supabase.table("interaction_logs") \
            .select("*") \
            .order("created_at", desc=True) \
            .limit(limit) \
            .execute()
        return result.data
    except Exception as e:
        print(f"⚠️  [DB] Could not fetch logs: {e}")
        return []


def format_logs_for_slack(logs: list) -> str:
    """Formats log rows into a readable Slack message."""
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
            short = (
                detail[:120] + "..."
                if len(detail) > 120
                else detail
            )
            line += f"\n  _{short}_"

        lines.append(line)

    return "\n\n".join(lines)