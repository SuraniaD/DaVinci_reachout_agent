from database import supabase


def save_run_state(
    user_id:   str,
    remaining: list[dict],
    stats:     dict
):
    """
    Saves current outreach run state to Supabase.
    Called after every approve/skip/fail.
    """
    try:
        supabase.table("outreach_runs").upsert({
            "user_id":            user_id,
            "remaining_contacts": remaining,
            "stats":              stats,
            "updated_at":         "now()"
        }, on_conflict="user_id").execute()

        print(
            f"💾 [RUN STATE] Saved — "
            f"{len(remaining)} remaining for {user_id}"
        )

    except Exception as e:
        print(f"⚠️  [RUN STATE] Could not save: {e}")


def load_all_run_states() -> list[dict]:
    """
    Loads all interrupted runs on startup.
    Only returns runs that still have contacts remaining.
    """
    try:
        result = supabase.table("outreach_runs") \
            .select("*") \
            .execute()

        active = [
            row for row in result.data
            if row.get("remaining_contacts")
        ]

        print(
            f"💾 [RUN STATE] {len(active)} interrupted "
            f"run(s) found"
        )
        return active

    except Exception as e:
        print(f"⚠️  [RUN STATE] Could not load: {e}")
        return []


def clear_run_state(user_id: str):
    """
    Clears run state when a run completes or is cancelled.
    """
    try:
        supabase.table("outreach_runs") \
            .delete() \
            .eq("user_id", user_id) \
            .execute()

        print(f"🗑️  [RUN STATE] Cleared for {user_id}")

    except Exception as e:
        print(f"⚠️  [RUN STATE] Could not clear: {e}")