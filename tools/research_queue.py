from database import supabase
from interaction_log import log_action


def save_queue(
    user_id:      str,
    instructions: list[str]
) -> bool:
    """
    Saves a list of research instructions to the DB.
    Replaces any existing pending queue for this user.
    Each instruction becomes one row with a position index.
    """
    try:
        # Clear existing pending items only
        # completed/failed items stay for history
        supabase.table("research_queue") \
            .delete() \
            .eq("user_id", user_id) \
            .eq("status", "pending") \
            .execute()

        if not instructions:
            return True

        rows = [
            {
                "user_id":     user_id,
                "instruction": instruction.strip(),
                "position":    i,
                "status":      "pending"
            }
            for i, instruction in enumerate(instructions)
            if instruction.strip()
        ]

        supabase.table("research_queue") \
            .insert(rows) \
            .execute()

        print(
            f"✅ [QUEUE] Saved {len(rows)} instructions "
            f"for {user_id}"
        )
        return True

    except Exception as e:
        print(f"❌ [QUEUE] Save failed: {e}")
        return False


def get_pending_queue(
    user_id: str
) -> list[dict]:
    """
    Returns all pending instructions for a user
    in order of position.
    """
    try:
        result = supabase.table("research_queue") \
            .select("*") \
            .eq("user_id", user_id) \
            .eq("status", "pending") \
            .order("position", desc=False) \
            .execute()

        return result.data

    except Exception as e:
        print(f"❌ [QUEUE] Fetch failed: {e}")
        return []


def mark_complete(queue_id: int):
    """Marks one queue item as complete."""
    try:
        supabase.table("research_queue") \
            .update({"status": "complete"}) \
            .eq("id", queue_id) \
            .execute()

        print(f"✅ [QUEUE] Item {queue_id} complete")

    except Exception as e:
        print(
            f"❌ [QUEUE] Mark complete failed: {e}"
        )


def mark_failed(
    queue_id: int,
    reason:   str = ""
):
    """Marks one queue item as failed."""
    try:
        supabase.table("research_queue") \
            .update({
                "status": "failed",
                "error":  reason[:500]
            }) \
            .eq("id", queue_id) \
            .execute()

        print(
            f"❌ [QUEUE] Item {queue_id} failed: "
            f"{reason[:80]}"
        )

    except Exception as e:
        print(f"❌ [QUEUE] Mark failed error: {e}")


def clear_queue(user_id: str):
    """Clears all pending queue items for a user."""
    try:
        supabase.table("research_queue") \
            .delete() \
            .eq("user_id", user_id) \
            .eq("status", "pending") \
            .execute()

        print(f"✅ [QUEUE] Cleared for {user_id}")

    except Exception as e:
        print(f"❌ [QUEUE] Clear failed: {e}")


def get_queue_summary(user_id: str) -> dict:
    """Returns counts of pending/complete/failed items."""
    try:
        result = supabase.table("research_queue") \
            .select("status") \
            .eq("user_id", user_id) \
            .execute()

        counts = {
            "pending":  0,
            "complete": 0,
            "failed":   0
        }
        for row in result.data:
            s = row["status"]
            if s in counts:
                counts[s] += 1

        return counts

    except Exception as e:
        print(f"❌ [QUEUE] Summary failed: {e}")
        return {
            "pending": 0, "complete": 0, "failed": 0
        }