from database import supabase
from interaction_log import log_action


def save_preference(user_id: str, preference: str):
    """
    Saves a learned preference to Supabase.
    Persists across restarts — Riley remembers forever.
    """
    try:
        supabase.table("riley_preferences").insert({
            "user_id":    user_id,
            "preference": preference
        }).execute()

        print(
            f"🧠 [PREFERENCES] Saved: '{preference}'"
        )
        log_action(
            action_type="preference_saved",
            detail=f"Learned: {preference}"
        )

    except Exception as e:
        print(f"⚠️  [PREFERENCES] Could not save: {e}")


def get_preferences(user_id: str) -> list[str]:
    """Fetches all learned preferences for this user."""
    try:
        result = supabase.table("riley_preferences") \
            .select("preference") \
            .eq("user_id", user_id) \
            .order("created_at", desc=False) \
            .execute()

        return [row["preference"] for row in result.data]

    except Exception as e:
        print(f"⚠️  [PREFERENCES] Could not fetch: {e}")
        return []


def build_preferences_block(user_id: str) -> str:
    """
    Builds a formatted block of learned preferences
    to inject at the top of the email draft prompt.
    Returns empty string if no preferences yet.
    """
    prefs = get_preferences(user_id)

    if not prefs:
        return ""

    lines = [
        "CEO PREFERENCES — apply to every email:",
        "----------------------------------------"
    ]
    for p in prefs:
        lines.append(f"- {p}")

    return "\n".join(lines)


def clear_preferences(user_id: str):
    """Clears all preferences for this user."""
    try:
        supabase.table("riley_preferences") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

        print(f"🗑️  [PREFERENCES] Cleared for {user_id}")

    except Exception as e:
        print(f"⚠️  [PREFERENCES] Could not clear: {e}")