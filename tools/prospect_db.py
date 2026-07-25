from database import supabase
from interaction_log import log_action


def add_prospect(prospect: dict) -> dict | None:
    """
    Writes one prospect to the prospects table.
    Called by Dexter after researching a business.
    Checks for duplicates by business_name first.
    Returns the inserted row or None if duplicate/failed.
    """
    try:
        business_name = prospect.get("business_name", "")

        # Check if already exists
        existing = supabase.table("prospects") \
            .select("id, business_name, outreach_status") \
            .ilike("business_name", business_name) \
            .execute()

        if existing.data:
            row = existing.data[0]
            print(
                f"⚠️  [PROSPECT DB] Already exists: "
                f"{business_name} "
                f"(status: {row['outreach_status']})"
            )
            return None

        row = {
            "business_name":    business_name,
            "contact_name":     prospect.get("contact_name"),
            "email":            prospect.get("email"),
            "website":          prospect.get("website"),
            "location":         prospect.get("location"),
            "industry":         prospect.get("industry"),
            "research_summary": prospect.get("research_summary"),
            "source_query":     prospect.get("source_query"),
            "outreach_status":  "researched"
        }

        result = supabase.table("prospects") \
            .insert(row) \
            .execute()

        if result.data:
            inserted = result.data[0]
            print(
                f"✅ [PROSPECT DB] Added: "
                f"{business_name} (ID: {inserted['id']})"
            )
            log_action(
                action_type="prospect_added",
                business_name=business_name,
                detail=(
                    f"Added by Dexter — "
                    f"email: {prospect.get('email', 'unknown')}"
                )
            )
            return inserted

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Could not add "
            f"{prospect.get('business_name')}: {e}"
        )
        return None


def get_prospects(
    status: str = None,
    limit:  int = 20
) -> list[dict]:
    """
    Fetches prospects from DB.
    Optional status filter: 'researched', 'sent' etc.
    """
    try:
        query = supabase.table("prospects") \
            .select(
                "id, business_name, contact_name, "
                "email, location, industry, "
                "outreach_status, created_at"
            ) \
            .order("created_at", desc=True) \
            .limit(limit)

        if status:
            query = query.eq("outreach_status", status)

        result = query.execute()
        return result.data

    except Exception as e:
        print(f"❌ [PROSPECT DB] Fetch failed: {e}")
        return []


def get_prospect_by_name(
    business_name: str
) -> dict | None:
    """
    Fetches one prospect's full details by name.
    Used when CEO asks Dexter for a deep dive.
    """
    try:
        result = supabase.table("prospects") \
            .select("*") \
            .ilike("business_name", f"%{business_name}%") \
            .limit(1) \
            .execute()

        if result.data:
            return result.data[0]
        return None

    except Exception as e:
        print(f"❌ [PROSPECT DB] Name lookup failed: {e}")
        return None


def update_prospect_status(
    prospect_id: int,
    status:      str
):
    """
    Updates outreach_status of a prospect.
    Called by Riley when status changes.
    Valid: researched → draft_ready → approved → sent
           → replied → closed → skipped
    """
    try:
        supabase.table("prospects") \
            .update({"outreach_status": status}) \
            .eq("id", prospect_id) \
            .execute()

        print(
            f"✅ [PROSPECT DB] ID {prospect_id} "
            f"→ {status}"
        )

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Status update failed: {e}"
        )


def start_research_session(
    user_id:     str,
    instruction: str
) -> int | None:
    """
    Creates a research_sessions row when Dexter
    starts a new research task.
    Returns the session ID.
    """
    try:
        result = supabase.table("research_sessions") \
            .insert({
                "user_id":         user_id,
                "instruction":     instruction,
                "prospects_found": 0,
                "status":          "running"
            }) \
            .execute()

        if result.data:
            session_id = result.data[0]["id"]
            print(
                f"✅ [PROSPECT DB] Session started: "
                f"ID {session_id}"
            )
            return session_id

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Session start failed: {e}"
        )
        return None


def complete_research_session(
    session_id:      int,
    prospects_found: int,
    status:          str = "complete"
):
    """Updates a research session when Dexter finishes."""
    try:
        supabase.table("research_sessions") \
            .update({
                "prospects_found": prospects_found,
                "status":          status
            }) \
            .eq("id", session_id) \
            .execute()

        print(
            f"✅ [PROSPECT DB] Session {session_id} "
            f"{status} — {prospects_found} found"
        )

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Session update failed: {e}"
        )


def format_prospects_for_slack(
    prospects: list[dict],
    title:     str = "📋 Prospect Pipeline"
) -> str:
    """
    Formats prospects into a clean Slack message.
    """
    if not prospects:
        return (
            "📋 No prospects found.\n"
            "Tell me what businesses to research "
            "and I'll get started."
        )

    status_icons = {
        "researched":  "🔬",
        "draft_ready": "✍️",
        "approved":    "✅",
        "sent":        "📧",
        "replied":     "💬",
        "closed":      "🏁",
        "skipped":     "⏭️"
    }

    lines = [f"*{title}* ({len(prospects)} total)\n"]

    for p in prospects:
        icon   = status_icons.get(
            p["outreach_status"], "•"
        )
        name   = p["business_name"]
        loc    = p.get("location", "")
        status = p["outreach_status"].replace("_", " ")
        email  = p.get("email") or "no email"

        line = f"{icon} *{name}*"
        if loc:
            line += f" — {loc}"
        line += f"\n   _{status}_ · {email}"
        lines.append(line)

    return "\n\n".join(lines)