from database import supabase
from interaction_log import log_action
from datetime import datetime, timezone


def _derive_segment(source_query: str) -> str:
    """
    Derives a clean segment label from source_query.
    Strips number prefixes and trailing count hints.
    e.g. "vegan restaurants Berlin, 20"
      → "vegan restaurants berlin"
    """
    if not source_query:
        return "uncategorised"

    import re
    segment = source_query.strip()
    segment = re.sub(r'^\d+\s+', '', segment)
    segment = re.sub(
        r',?\s*(list\s+of\s+)?\d+\s*$', '', segment
    )
    segment = re.sub(
        r'\s+', ' ', segment
    ).strip().lower()

    return segment or "uncategorised"


def add_prospect(prospect: dict) -> dict | None:
    """
    Writes one prospect to the prospects table.
    Derives segment from source_query at insert time.
    Validates required fields. Deduplicates by name.
    """
    try:
        business_name = prospect.get("business_name")

        if not business_name or \
           str(business_name).strip().lower() in [
               "none", "null", "unknown", "", "n/a",
               "not found", "not available"
           ]:
            print(
                f"⚠️  [PROSPECT DB] Skipping — "
                f"no valid business name"
            )
            return None

        business_name = str(business_name).strip()

        has_any_data = any([
            prospect.get("email"),
            prospect.get("website"),
            prospect.get("location"),
            prospect.get("research_summary")
        ])

        if not has_any_data:
            print(
                f"⚠️  [PROSPECT DB] Skipping "
                f"'{business_name}' — no useful data"
            )
            return None

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

        source_query = prospect.get("source_query") or ""
        segment      = _derive_segment(source_query)

        row = {
            "business_name":    business_name,
            "contact_name":     prospect.get("contact_name") or None,
            "email":            prospect.get("email") or None,
            "website":          prospect.get("website") or None,
            "location":         prospect.get("location") or None,
            "industry":         prospect.get("industry") or None,
            "research_summary": prospect.get("research_summary") or None,
            "source_query":     source_query or None,
            "segment":          segment,
            "outreach_status":  "researched"
        }

        result = supabase.table("prospects") \
            .insert(row) \
            .execute()

        if result.data:
            inserted = result.data[0]
            print(
                f"✅ [PROSPECT DB] Added: "
                f"'{business_name}' "
                f"(ID: {inserted['id']}) "
                f"segment: '{segment}' "
                f"email: {row.get('email', 'none')}"
            )
            log_action(
                action_type="prospect_added",
                business_name=business_name,
                detail=(
                    f"segment: {segment} — "
                    f"email: "
                    f"{prospect.get('email') or 'unknown'}"
                )
            )
            return inserted

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Could not add "
            f"'{prospect.get('business_name')}': {e}"
        )
        return None


def get_prospects_for_outreach(
    status:  str = "researched",
    limit:   int = 50,
    segment: str = None
) -> list[dict]:
    """
    Fetches prospects ready for Riley.
    Filters at DB level:
    - must have valid email
    - must have research_summary
    Optional segment filter for targeted campaigns.
    """
    try:
        query = supabase.table("prospects") \
            .select("*") \
            .eq("outreach_status", status) \
            .not_.is_("email", "null") \
            .neq("email", "") \
            .neq("email", "None") \
            .neq("email", "n/a") \
            .neq("email", "not found") \
            .not_.is_("research_summary", "null") \
            .neq("research_summary", "") \
            .order("created_at", desc=True) \
            .limit(limit)

        if segment:
            query = query.ilike(
                "segment", f"%{segment}%"
            )

        result = query.execute()
        rows   = result.data

        print(
            f"✅ [PROSPECT DB] Fetched "
            f"{len(rows)} prospects "
            f"status='{status}'"
            f"{(' segment~' + segment) if segment else ''}"
        )

        return rows

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Fetch failed: {e}"
        )
        return []


def get_prospects(
    status:  str = None,
    limit:   int = 20,
    segment: str = None
) -> list[dict]:
    """
    Fetches prospects for pipeline display.
    Optional status and segment filters.
    No email filter — shows full picture.
    """
    try:
        query = supabase.table("prospects") \
            .select(
                "id, business_name, contact_name, "
                "email, location, industry, segment, "
                "outreach_status, created_at"
            ) \
            .order("created_at", desc=True) \
            .limit(limit)

        if status:
            query = query.eq("outreach_status", status)

        if segment:
            query = query.ilike(
                "segment", f"%{segment}%"
            )

        result = query.execute()
        return result.data

    except Exception as e:
        print(f"❌ [PROSPECT DB] Fetch failed: {e}")
        return []


def get_segment_summary() -> dict:
    """
    Returns prospect counts grouped by segment
    and outreach_status.

    Returns:
    {
        "vegan restaurants berlin": {
            "researched": 8,
            "sent": 3,
            "replied": 1,
            "total": 12
        },
        ...
    }
    """
    try:
        result = supabase.table("prospects") \
            .select("segment, outreach_status") \
            .execute()

        summary = {}
        for row in result.data:
            seg    = row.get("segment") or "uncategorised"
            status = row.get("outreach_status", "unknown")

            if seg not in summary:
                summary[seg] = {"total": 0}

            summary[seg]["total"] = \
                summary[seg].get("total", 0) + 1
            summary[seg][status] = \
                summary[seg].get(status, 0) + 1

        summary = dict(
            sorted(
                summary.items(),
                key=lambda x: x[1].get("total", 0),
                reverse=True
            )
        )

        return summary

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Segment summary "
            f"failed: {e}"
        )
        return {}


def get_prospect_by_name(
    business_name: str
) -> dict | None:
    try:
        result = supabase.table("prospects") \
            .select("*") \
            .ilike(
                "business_name", f"%{business_name}%"
            ) \
            .limit(1) \
            .execute()

        if result.data:
            return result.data[0]
        return None

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Name lookup failed: {e}"
        )
        return None


def update_prospect_status(
    prospect_id: int,
    status:      str
):
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


def save_draft(
    prospect_id: int,
    subject:     str,
    body:        str,
    version:     int = 1,
    status:      str = "pending"
) -> dict | None:
    try:
        result = supabase.table("email_drafts") \
            .insert({
                "prospect_id": prospect_id,
                "subject":     subject,
                "body":        body,
                "version":     version,
                "status":      status
            }) \
            .execute()

        if result.data:
            draft_id = result.data[0]["id"]
            print(
                f"✅ [PROSPECT DB] Draft saved — "
                f"ID {draft_id} "
                f"(prospect {prospect_id} v{version})"
            )
            return result.data[0]

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Draft save failed: {e}"
        )
        return None


def update_draft_status(
    draft_id: int,
    status:   str,
    feedback: str = None,
    sent_at:  str = None
):
    try:
        update_data = {"status": status}
        if feedback:
            update_data["ceo_feedback"] = feedback
        if sent_at:
            update_data["sent_at"] = sent_at

        supabase.table("email_drafts") \
            .update(update_data) \
            .eq("id", draft_id) \
            .execute()

        print(
            f"✅ [PROSPECT DB] Draft {draft_id} "
            f"→ {status}"
        )

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Draft update failed: {e}"
        )


def get_latest_draft(
    prospect_id: int
) -> dict | None:
    try:
        result = supabase.table("email_drafts") \
            .select("*") \
            .eq("prospect_id", prospect_id) \
            .order("version", desc=True) \
            .limit(1) \
            .execute()

        if result.data:
            return result.data[0]
        return None

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Get draft failed: {e}"
        )
        return None


def get_pipeline_summary() -> dict:
    """Returns counts per outreach_status."""
    try:
        result = supabase.table("prospects") \
            .select("outreach_status") \
            .execute()

        counts = {}
        for row in result.data:
            s = row["outreach_status"]
            counts[s] = counts.get(s, 0) + 1

        return counts

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Pipeline count "
            f"failed: {e}"
        )
        return {}


def start_research_session(
    user_id:     str,
    instruction: str
) -> int | None:
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
            f"❌ [PROSPECT DB] Session start "
            f"failed: {e}"
        )
        return None


def complete_research_session(
    session_id:      int,
    prospects_found: int,
    status:          str = "complete"
):
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
            f"❌ [PROSPECT DB] Session update "
            f"failed: {e}"
        )


def format_prospects_for_slack(
    prospects: list[dict],
    title:     str = "📋 Prospect Pipeline"
) -> str:
    if not prospects:
        return (
            "📋 No prospects found.\n"
            "Ask Dexter to research some businesses first."
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
        icon    = status_icons.get(
            p["outreach_status"], "•"
        )
        name    = p["business_name"]
        loc     = p.get("location") or ""
        status  = p["outreach_status"].replace("_", " ")
        email   = p.get("email") or "no email"
        segment = p.get("segment") or ""

        line = f"{icon} *{name}*"
        if loc:
            line += f" — {loc}"
        line += f"\n   _{status}_ · {email}"
        if segment:
            line += f"\n   📂 {segment}"
        lines.append(line)

    return "\n\n".join(lines)


def format_segment_summary_for_slack(
    summary: dict
) -> str:
    if not summary:
        return (
            "📊 No segments yet.\n"
            "Ask Dexter to research some businesses."
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

    status_order = [
        "researched", "draft_ready", "approved",
        "sent", "replied", "closed", "skipped"
    ]

    total_all = sum(
        v.get("total", 0) for v in summary.values()
    )
    lines = [
        f"*📂 Segments* "
        f"({total_all} total prospects)\n"
    ]

    for seg, counts in summary.items():
        total = counts.get("total", 0)
        lines.append(f"*{seg}* — {total} prospects")

        status_parts = []
        for s in status_order:
            if s in counts and s != "total":
                icon = status_icons.get(s, "•")
                status_parts.append(
                    f"{icon} {s.replace('_', ' ')}: "
                    f"{counts[s]}"
                )

        if status_parts:
            lines.append(
                "   " + " · ".join(status_parts)
            )

        lines.append("")

    lines.append(
        "_Use *!run <segment>* to target a segment_\n"
        "_e.g. *!run berlin* · *!run netherlands*_"
    )

    return "\n".join(lines)


def format_pipeline_summary_for_slack(
    counts: dict
) -> str:
    if not counts:
        return (
            "📊 Pipeline is empty.\n"
            "Ask Dexter to research some businesses."
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

    total = sum(counts.values())
    lines = [
        f"*📊 Pipeline Summary* ({total} total)\n"
    ]

    order = [
        "researched", "draft_ready", "approved",
        "sent", "replied", "closed", "skipped"
    ]

    for status in order:
        if status in counts:
            icon  = status_icons.get(status, "•")
            label = status.replace("_", " ").title()
            count = counts[status]
            lines.append(f"{icon} *{label}:* {count}")

    lines.append(
        f"\n_*!segments* to see by segment_\n"
        f"_*!pipeline <status>* to filter by status_"
    )

    return "\n".join(lines)