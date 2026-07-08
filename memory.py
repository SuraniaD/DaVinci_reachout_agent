import threading
from database import supabase

conversation_cache = {}

MAX_HISTORY = 20


def load_all_conversations():
    """
    Called once on startup.
    Loads all past conversations from Supabase into RAM.
    Riley wakes up with full memory after every restart.
    """
    try:
        result = supabase.table("conversations") \
            .select("*") \
            .order("created_at", desc=False) \
            .execute()

        for row in result.data:
            agent   = row["agent"]
            user_id = row["user_id"]
            role    = row["role"]
            content = row["content"]

            if agent not in conversation_cache:
                conversation_cache[agent] = {}
            if user_id not in conversation_cache[agent]:
                conversation_cache[agent][user_id] = []

            conversation_cache[agent][user_id].append({
                "role":    role,
                "content": content
            })

        print(
            f"✅ Memory loaded from Supabase — "
            f"{len(result.data)} messages restored"
        )

    except Exception as e:
        print(f"⚠️ Could not load memory from Supabase: {e}")
        print("Starting with empty memory")


def get_history(agent: str, user_id: str) -> list:
    """Read from RAM — instant, no database call."""
    history = conversation_cache \
        .get(agent, {}) \
        .get(user_id, [])
    return history[-MAX_HISTORY:]


def add_message(agent: str, user_id: str, role: str, content: str):
    """
    Write to RAM immediately.
    Write to Supabase in background thread.
    """
    if agent not in conversation_cache:
        conversation_cache[agent] = {}
    if user_id not in conversation_cache[agent]:
        conversation_cache[agent][user_id] = []

    conversation_cache[agent][user_id].append({
        "role":    role,
        "content": content
    })

    def save_to_db():
        try:
            supabase.table("conversations").insert({
                "agent":   agent,
                "user_id": user_id,
                "role":    role,
                "content": content
            }).execute()
        except Exception as e:
            print(f"⚠️ Memory save failed: {e}")

    thread = threading.Thread(target=save_to_db)
    thread.daemon = True
    thread.start()


def clear_history(agent: str, user_id: str):
    """Wipes RAM and Supabase for this user."""
    if agent in conversation_cache:
        conversation_cache[agent][user_id] = []

    try:
        supabase.table("conversations") \
            .delete() \
            .eq("agent",   agent) \
            .eq("user_id", user_id) \
            .execute()
        print(f"🗑️ History cleared for {user_id}")
    except Exception as e:
        print(f"⚠️ Could not clear history from Supabase: {e}")