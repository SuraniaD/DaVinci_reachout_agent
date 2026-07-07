import threading
from database import supabase

# In-RAM cache — fast to read from
# Format: { "agent_name": { "slack_user_id": [ {role, content}, ... ] } }
conversation_cache = {}

MAX_HISTORY = 20  # max messages sent to Groq per call — keeps token usage low


# ─────────────────────────────────────────
# LOAD FROM SUPABASE ON STARTUP
# ─────────────────────────────────────────

def load_all_conversations():
    """
    Called once when app.py starts.
    Pulls all past conversations from Supabase into RAM cache.
    Riley wakes up with full memory even after a restart.
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

        print(f"✅ Memory loaded from Supabase — "
              f"{len(result.data)} messages restored")

    except Exception as e:
        print(f"⚠️ Could not load memory from Supabase: {e}")
        print("Starting with empty memory — "
              "conversations will still be saved going forward")


# ─────────────────────────────────────────
# READ HISTORY (from RAM — instant)
# ─────────────────────────────────────────

def get_history(agent: str, user_id: str) -> list:
    """
    Returns the conversation history for a specific user with a specific agent.
    Read from RAM — no database call, instant.
    Capped at MAX_HISTORY to keep Groq token usage manageable.
    """
    history = conversation_cache \
        .get(agent, {}) \
        .get(user_id, [])

    # Return only the last MAX_HISTORY messages
    return history[-MAX_HISTORY:]


# ─────────────────────────────────────────
# WRITE MESSAGE (to RAM + Supabase)
# ─────────────────────────────────────────

def add_message(agent: str, user_id: str, role: str, content: str):
    """
    Adds a message to both:
    1. RAM cache (instant — used for next Groq call)
    2. Supabase (permanent — survives restarts)

    The Supabase write happens in a background thread
    so Riley never pauses waiting for the database.
    """
    # Step 1 — write to RAM immediately
    if agent not in conversation_cache:
        conversation_cache[agent] = {}
    if user_id not in conversation_cache[agent]:
        conversation_cache[agent][user_id] = []

    conversation_cache[agent][user_id].append({
        "role":    role,
        "content": content
    })

    # Step 2 — write to Supabase in background thread
    # Riley continues without waiting for this
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


# ─────────────────────────────────────────
# CLEAR HISTORY (for /reset command)
# ─────────────────────────────────────────

def clear_history(agent: str, user_id: str):
    """
    Clears conversation history for a user.
    Wipes RAM cache and deletes from Supabase.
    Triggered when user types /reset in Slack DM.
    """
    # Clear RAM
    if agent in conversation_cache:
        conversation_cache[agent][user_id] = []

    # Clear Supabase
    try:
        supabase.table("conversations") \
            .delete() \
            .eq("agent",   agent) \
            .eq("user_id", user_id) \
            .execute()
        print(f"🗑️ History cleared for {user_id}")
    except Exception as e:
        print(f"⚠️ Could not clear history from Supabase: {e}")