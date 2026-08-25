"""
Prospect DB — Supabase CRUD
Extended for v2.0:
- good_lead, lead_score, domain, region fields
- get_good_leads_for_region (ordered by score)
- research_request and search_cycle tracking
- domain deduplication check
- segment summary (geographic)
- campaign analytics
"""

from database import supabase
from interaction_log import log_action
from datetime import datetime, timezone

# ─────────────────────────────────────────
# REGION EXTRACTION
# ─────────────────────────────────────────

REGION_MAP = {
    "japan": [
        "japan", "tokyo", "osaka", "kyoto", "nagoya",
        "fukuoka", "sapporo", "yokohama", "tochigi",
        "uji", "utsunomiya", "toyohashi", "hiroshima",
        "sendai", "chiba", "kawasaki", "saitama",
        "nagano", "okinawa"
    ],
    "usa": [
        "usa", ", us", "united states", "new york",
        "chicago", "los angeles", "san francisco",
        "seattle", "boston", "florida", "texas",
        "california", "austin", "miami", "portland",
        "denver", "atlanta", "washington", "brooklyn",
        "lawrenceville", "boise", "raleigh", "omaha",
        "michigan", "maryland", "new jersey", "boulder",
        "emeryville", "cranford", "redwood city",
        "yountville", "monterey", "carmel", "berkeley",
        "pleasanton", "addison", "plano", "denton",
        "mesquite", "beaumont", "orange park", "naples",
        "boca raton", "fort lauderdale", "orlando",
        "panama city", "america", "american"
    ],
    "uk": [
        "united kingdom", " uk", "uk,", "england",
        "london", "manchester", "bristol", "liverpool",
        "scotland", "birmingham", "corby",
        "south derbyshire", "britain", "welsh", "wales"
    ],
    "germany": [
        "germany", "deutschland", "berlin", "munich",
        "hamburg", "frankfurt", "mainz", "cologne",
        "düsseldorf", "stuttgart"
    ],
    "australia": [
        "australia", "sydney", "melbourne", "brisbane",
        "perth", "adelaide", "gold coast", "canberra",
        "blue mountains", "south australia",
        "western australia", "port lincoln"
    ],
    "netherlands": [
        "netherlands", "holland", "amsterdam",
        "rotterdam", "hilversum", "zwolle", "dutch"
    ],
    "france": [
        "france", "paris", "nice", "lyon", "marseille",
        "french", "bordeaux"
    ],
    "thailand": [
        "thailand", "bangkok", "chiang mai", "phuket",
        "mueang", "thai"
    ],
    "spain": [
        "spain", "madrid", "barcelona", "seville",
        "spanish", "valencia"
    ],
    "sweden": [
        "sweden", "stockholm", "gothenburg", "malmo",
        "swedish"
    ],
    "belgium": [
        "belgium", "brussels", "wevelgem", "antwerp",
        "belgian", "ghent"
    ],
    "denmark": [
        "denmark", "copenhagen", "aarhus", "danish"
    ],
    "austria": [
        "austria", "vienna", "austrian", "graz"
    ],
    "ireland": [
        "ireland", "dublin", "kildare", "irish"
    ],
    "italy": [
        "italy", "milan", "rome", "florence",
        "castelvolturno", "naples", "italian"
    ],
    "canada": [
        "canada", "toronto", "vancouver", "montreal",
        "canadian"
    ],
    "india": [
        "india", "mumbai", "delhi", "bangalore",
        "udaipur", "chennai", "indian"
    ],
    "taiwan": ["taiwan", "taipei"],
    "russia": ["russia", "moscow", "russian"],
    "europe": ["europe", "european"],
    "global": ["global", "worldwide", "international"],
}

REGION_PRIORITY = [
    "japan", "usa", "uk", "germany", "australia",
    "netherlands", "france", "thailand", "spain",
    "sweden", "belgium", "denmark", "austria",
    "ireland", "italy", "canada", "india",
    "taiwan", "russia", "europe", "global"
]

COUNTRY_FLAGS = {
    "japan":       "🇯🇵",
    "usa":         "🇺🇸",
    "uk":          "🇬🇧",
    "germany":     "🇩🇪",
    "australia":   "🇦🇺",
    "netherlands": "🇳🇱",
    "france":      "🇫🇷",
    "thailand":    "🇹🇭",
    "spain":       "🇪🇸",
    "sweden":      "🇸🇪",
    "belgium":     "🇧🇪",
    "denmark":     "🇩🇰",
    "austria":     "🇦🇹",
    "ireland":     "🇮🇪",
    "italy":       "🇮🇹",
    "canada":      "🇨🇦",
    "india":       "🇮🇳",
    "taiwan":      "🇹🇼",
    "russia":      "🇷🇺",
    "europe":      "🌍",
    "global":      "🌐",
    "unknown":     "❓",
}


def extract_region(location: str) -> str:
    if not location or \
       str(location).strip().lower() in [
           "", "none", "null", "n/a", "unknown"
       ]:
        return "unknown"

    loc = location.strip().lower()

    for region in REGION_PRIORITY:
        keywords = REGION_MAP.get(region, [])
        if any(kw in loc for kw in keywords):
            return region

    return "unknown"


# ─────────────────────────────────────────
# PROSPECT CRUD
# ─────────────────────────────────────────

def get_domain_exists(domain: str) -> bool:
    """Returns True if domain already in prospects."""
    try:
        result = supabase.table("prospects") \
            .select("id") \
            .eq("domain", domain.lower().strip()) \
            .limit(1) \
            .execute()

        return bool(result.data)

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Domain check failed: {e}"
        )
        return False


def add_prospect(prospect: dict) -> dict | None:
    """
    Inserts one prospect.
    Sets region, segment, domain from location/email.
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

        # Check duplicate by name
        existing = supabase.table("prospects") \
            .select("id, business_name, outreach_status") \
            .ilike("business_name", business_name) \
            .execute()

        if existing.data:
            print(
                f"⚠️  [PROSPECT DB] Already exists: "
                f"{business_name}"
            )
            return None

        # Derive region and segment
        location = prospect.get("location") or ""
        region   = extract_region(location)

        # Extract domain from email
        email  = prospect.get("email") or ""
        domain = None
        if "@" in email:
            domain = email.split("@")[1].lower().strip()

        row = {
            "business_name":      business_name,
            "contact_name":       prospect.get("contact_name") or None,
            "email":              email or None,
            "website":            prospect.get("website") or None,
            "location":           location or None,
            "industry":           prospect.get("industry") or None,
            "research_summary":   prospect.get("research_summary") or None,
            "source_query":       prospect.get("source_query") or None,
            "segment":            region,
            "region":             region,
            "domain":             domain,
            "outreach_status":    "researched",
            # Quality check fields
            "good_lead":              prospect.get("good_lead", False),
            "has_name_and_contact":   prospect.get("has_name_and_contact", False),
            "cross_verified_count":   prospect.get("cross_verified_count", 0),
            "lead_score":             prospect.get("lead_score", 0),
            "mx_valid":               prospect.get("mx_valid", False),
            "enriched":               prospect.get("enriched", False),
        }

        result = supabase.table("prospects") \
            .insert(row) \
            .execute()

        if result.data:
            inserted = result.data[0]
            print(
                f"✅ [PROSPECT DB] Added: "
                f"'{business_name}' "
                f"region='{region}' "
                f"score={row['lead_score']} "
                f"good={row['good_lead']}"
            )
            log_action(
                action_type="prospect_added",
                business_name=business_name,
                detail=(
                    f"region: {region} | "
                    f"score: {row['lead_score']} | "
                    f"email: {email or 'none'}"
                )
            )
            return inserted

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Could not add "
            f"'{prospect.get('business_name')}': {e}"
        )
        return None


def get_good_leads_for_region(
    region:          str,
    limit:           int  = 50,
    industry_filter: str  = None,
    status:          str  = "researched"
) -> list[dict]:
    """
    Fetches verified good leads for a region.
    Ordered by lead_score DESC — best leads first.
    Only returns good_lead=TRUE prospects.
    """
    try:
        query = supabase.table("prospects") \
            .select("*") \
            .eq("region", region.lower().strip()) \
            .eq("good_lead", True) \
            .eq("outreach_status", status) \
            .not_.is_("email", "null") \
            .not_.is_("research_summary", "null") \
            .is_("unsubscribed_at", "null") \
            .is_("bounced_at", "null") \
            .order("lead_score", desc=True) \
            .limit(limit)

        if industry_filter:
            query = query.ilike(
                "industry", f"%{industry_filter}%"
            )

        result = query.execute()
        rows   = result.data or []

        print(
            f"✅ [PROSPECT DB] "
            f"{len(rows)} good leads for '{region}'"
            f"{f' ({industry_filter})' if industry_filter else ''}"
        )
        return rows

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Good leads fetch failed: {e}"
        )
        return []


def get_prospects_for_outreach(
    status:  str = "researched",
    limit:   int = 50,
    segment: str = None
) -> list[dict]:
    """
    Legacy fetch — used by Riley's !run.
    Now requires good_lead=TRUE.
    """
    try:
        query = supabase.table("prospects") \
            .select("*") \
            .eq("outreach_status", status) \
            .eq("good_lead", True) \
            .not_.is_("email", "null") \
            .not_.is_("research_summary", "null") \
            .is_("unsubscribed_at", "null") \
            .is_("bounced_at", "null") \
            .order("lead_score", desc=True) \
            .limit(limit)

        if segment:
            query = query.eq(
                "region", segment.strip().lower()
            )

        result = query.execute()
        return result.data or []

    except Exception as e:
        print(f"❌ [PROSPECT DB] Fetch failed: {e}")
        return []


def get_prospects(
    status:  str = None,
    limit:   int = 20,
    segment: str = None
) -> list[dict]:
    """For pipeline display — no good_lead filter."""
    try:
        query = supabase.table("prospects") \
            .select(
                "id, business_name, contact_name, "
                "email, location, industry, "
                "segment, region, lead_score, "
                "good_lead, outreach_status, created_at"
            ) \
            .order("created_at", desc=True) \
            .limit(limit)

        if status:
            query = query.eq("outreach_status", status)

        if segment:
            query = query.eq(
                "region", segment.strip().lower()
            )

        result = query.execute()
        return result.data or []

    except Exception as e:
        print(f"❌ [PROSPECT DB] Fetch failed: {e}")
        return []


def get_prospect_by_name(
    business_name: str
) -> dict | None:
    try:
        result = supabase.table("prospects") \
            .select("*") \
            .ilike(
                "business_name",
                f"%{business_name}%"
            ) \
            .limit(1) \
            .execute()

        if result.data:
            return result.data[0]
        return None

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Name lookup: {e}"
        )
        return None


def update_prospect_status(
    prospect_id: int,
    status:      str
):
    try:
        update = {"outreach_status": status}

        if status == "sent":
            update["last_contacted_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

        supabase.table("prospects") \
            .update(update) \
            .eq("id", prospect_id) \
            .execute()

        print(
            f"✅ [PROSPECT DB] "
            f"ID {prospect_id} → {status}"
        )

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Status update failed: {e}"
        )


# ─────────────────────────────────────────
# RESEARCH REQUEST TRACKING
# ─────────────────────────────────────────

def create_research_request(
    raw_query:   str,
    industry:    str,
    location:    str,
    region:      str,
    target_size: int
) -> str | None:
    """Creates a research_request row. Returns id."""
    try:
        result = supabase.table("research_requests") \
            .insert({
                "raw_query":   raw_query,
                "industry":    industry,
                "location":    location,
                "region":      region,
                "target_size": target_size,
                "status":      "running"
            }) \
            .execute()

        if result.data:
            req_id = result.data[0]["id"]
            print(
                f"✅ [PROSPECT DB] "
                f"Research request: {req_id}"
            )
            return req_id

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Research request failed: {e}"
        )
    return None


def update_research_request(
    request_id:      str,
    status:          str  = None,
    good_leads_found: int = None,
    total_cycles:    int  = None
):
    try:
        updates: dict = {}
        if status:
            updates["status"] = status
            if status in ["complete", "incomplete"]:
                updates["completed_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )
        if good_leads_found is not None:
            updates["good_leads_found"] = good_leads_found
        if total_cycles is not None:
            updates["total_cycles"] = total_cycles

        if updates:
            supabase.table("research_requests") \
                .update(updates) \
                .eq("id", request_id) \
                .execute()

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Request update failed: {e}"
        )


def create_search_cycle(
    request_id:    str,
    cycle_index:   int,
    keywords_used: list[str],
    k_value:       int
) -> str | None:
    """Creates a search_cycle row. Returns id."""
    try:
        result = supabase.table("search_cycles") \
            .insert({
                "request_id":    request_id,
                "cycle_index":   cycle_index,
                "keywords_used": keywords_used,
                "k_value":       k_value
            }) \
            .execute()

        if result.data:
            return result.data[0]["id"]

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Search cycle failed: {e}"
        )
    return None


def update_search_cycle(
    cycle_id:        str,
    leads_found:     int,
    good_leads_found: int
):
    try:
        supabase.table("search_cycles") \
            .update({
                "leads_found":      leads_found,
                "good_leads_found": good_leads_found
            }) \
            .eq("id", cycle_id) \
            .execute()

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Cycle update failed: {e}"
        )


def get_active_research_requests() -> list[dict]:
    """Returns all currently running research requests."""
    try:
        result = supabase.table("research_requests") \
            .select("*, search_cycles(*)") \
            .eq("status", "running") \
            .order("created_at", desc=True) \
            .execute()

        return result.data or []

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Active requests failed: {e}"
        )
        return []


# ─────────────────────────────────────────
# DRAFT / EMAIL TRACKING
# ─────────────────────────────────────────

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
            return result.data[0]

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Draft save: {e}"
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

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Draft update: {e}"
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
            f"❌ [PROSPECT DB] Get draft: {e}"
        )
        return None


def get_human_review_queue() -> list[dict]:
    """Returns all drafts awaiting human review."""
    try:
        result = supabase.table("outreach_emails") \
            .select(
                "*, prospects!inner("
                "business_name, email, location, region)"
            ) \
            .eq("send_status", "human_review") \
            .order("created_at", desc=True) \
            .execute()

        return result.data or []

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] "
            f"Human review queue: {e}"
        )
        return []


# ─────────────────────────────────────────
# PIPELINE & SEGMENT SUMMARIES
# ─────────────────────────────────────────

def get_pipeline_summary() -> dict:
    try:
        result = supabase.table("prospects") \
            .select("outreach_status") \
            .execute()

        counts = {}
        for row in result.data:
            s          = row["outreach_status"]
            counts[s]  = counts.get(s, 0) + 1
        return counts

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Pipeline count: {e}"
        )
        return {}


def get_segment_summary() -> dict:
    """
    Returns counts grouped by region + outreach_status.
    """
    try:
        result = supabase.table("prospects") \
            .select("region, outreach_status") \
            .execute()

        summary: dict = {}
        for row in result.data:
            region = row.get("region") or "unknown"
            status = row.get("outreach_status", "unknown")

            if region not in summary:
                summary[region] = {"total": 0}

            summary[region]["total"] += 1
            summary[region][status] = (
                summary[region].get(status, 0) + 1
            )

        return dict(
            sorted(
                summary.items(),
                key=lambda x: (
                    0 if x[0] == "unknown" else
                    x[1].get("total", 0)
                ),
                reverse=True
            )
        )

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Segment summary: {e}"
        )
        return {}


def get_analytics_summary(days: int = 30) -> list[dict]:
    """Returns campaign analytics for the last N days."""
    from datetime import date, timedelta
    since = (
        date.today() - timedelta(days=days)
    ).isoformat()

    try:
        result = supabase.table("campaign_analytics") \
            .select("*") \
            .gte("date", since) \
            .order("date", desc=True) \
            .execute()

        return result.data or []

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Analytics: {e}"
        )
        return []


def start_research_session(
    user_id: str, instruction: str
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
            return result.data[0]["id"]

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Session start: {e}"
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

    except Exception as e:
        print(
            f"❌ [PROSPECT DB] Session update: {e}"
        )


# ─────────────────────────────────────────
# SLACK FORMATTERS
# ─────────────────────────────────────────

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
        "researched":    "🔬",
        "draft_ready":   "✍️",
        "approved":      "✅",
        "sent":          "📧",
        "replied":       "💬",
        "closed":        "🏁",
        "skipped":       "⏭️",
        "bounced":       "⚠️",
        "unsubscribed":  "🚫",
    }

    lines = [f"*{title}* ({len(prospects)} total)\n"]

    for p in prospects:
        icon   = status_icons.get(
            p["outreach_status"], "•"
        )
        name   = p["business_name"]
        loc    = p.get("location") or ""
        status = p["outreach_status"].replace("_", " ")
        email  = p.get("email") or "no email"
        region = p.get("region") or ""
        score  = p.get("lead_score", 0)
        good   = p.get("good_lead", False)

        line = f"{icon} *{name}*"
        if loc:
            line += f" — {loc}"
        line += f"\n   _{status}_ · {email}"
        if region and region != "unknown":
            flag = COUNTRY_FLAGS.get(region, "🌐")
            line += f" · {flag} {region}"
        if good and score:
            line += f" · ⭐ {score}"
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
        "researched":    "🔬",
        "draft_ready":   "✍️",
        "approved":      "✅",
        "sent":          "📧",
        "replied":       "💬",
        "closed":        "🏁",
        "skipped":       "⏭️",
        "bounced":       "⚠️",
        "unsubscribed":  "🚫",
    }

    status_order = [
        "researched", "draft_ready", "approved",
        "sent", "replied", "closed",
        "skipped", "bounced", "unsubscribed"
    ]

    total_all = sum(
        v.get("total", 0)
        for v in summary.values()
        if isinstance(v, dict)
    )

    lines = [
        f"*🌍 Geographic Segments* "
        f"({total_all} total)\n"
    ]

    for region, counts in summary.items():
        if region == "unknown":
            continue

        total = counts.get("total", 0)
        flag  = COUNTRY_FLAGS.get(region, "🌐")

        status_parts = []
        for s in status_order:
            if s in counts and s != "total":
                icon = status_icons.get(s, "•")
                status_parts.append(
                    f"{icon} {counts[s]}"
                )

        status_str = " · ".join(status_parts)
        lines.append(
            f"{flag} *{region.upper()}* "
            f"— {total} prospects\n"
            f"   {status_str}"
        )

    if "unknown" in summary:
        unk = summary["unknown"]
        if unk.get("total", 0) > 0:
            lines.append(
                f"\n❓ *Unknown* "
                f"— {unk['total']} prospects"
            )

    lines.append(
        f"\n_*!run <country>* to start a campaign_\n"
        f"_e.g. *!run japan* · *!run germany*_"
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
        "researched":    "🔬",
        "draft_ready":   "✍️",
        "approved":      "✅",
        "sent":          "📧",
        "replied":       "💬",
        "closed":        "🏁",
        "skipped":       "⏭️",
        "bounced":       "⚠️",
        "unsubscribed":  "🚫",
    }

    total  = sum(counts.values())
    lines  = [
        f"*📊 Pipeline Summary* ({total} total)\n"
    ]
    order  = [
        "researched", "draft_ready", "approved",
        "sent", "replied", "closed",
        "skipped", "bounced", "unsubscribed"
    ]

    for status in order:
        if status in counts:
            icon  = status_icons.get(status, "•")
            label = status.replace("_", " ").title()
            count = counts[status]
            lines.append(f"{icon} *{label}:* {count}")

    lines.append(
        f"\n_*!segments* to see by country_\n"
        f"_*!analytics* for campaign stats_"
    )

    return "\n".join(lines)


def format_analytics_for_slack(
    rows: list[dict],
    days: int = 30
) -> str:
    if not rows:
        return (
            f"📊 No analytics data for "
            f"the last {days} days."
        )

    # Aggregate by region
    by_region: dict = {}
    for row in rows:
        region = row.get("region", "unknown")
        if region not in by_region:
            by_region[region] = {
                "sent":    0,
                "bounced": 0,
                "replies": 0,
                "x1_sum":  0,
                "x2_sum":  0,
                "n":       0,
            }

        r = by_region[region]
        r["sent"]    += row.get("emails_sent", 0)
        r["bounced"] += row.get("emails_bounced", 0)
        r["replies"] += row.get("replies_received", 0)
        if row.get("avg_x1_score"):
            r["x1_sum"] += row["avg_x1_score"]
            r["n"]      += 1

    lines = [
        f"*📊 Campaign Analytics* "
        f"(last {days} days)\n"
    ]

    for region, r in sorted(
        by_region.items(),
        key=lambda x: x[1]["sent"],
        reverse=True
    ):
        flag   = COUNTRY_FLAGS.get(region, "🌐")
        sent   = r["sent"]
        reply  = r["replies"]
        bounce = r["bounced"]
        rate   = (
            f"{reply/sent:.0%}" if sent > 0 else "—"
        )
        avg_x1 = (
            f"{r['x1_sum']/r['n']:.2f}"
            if r["n"] > 0 else "—"
        )

        lines.append(
            f"{flag} *{region.upper()}*\n"
            f"   📧 {sent} sent · "
            f"💬 {reply} replies ({rate}) · "
            f"⚠️ {bounce} bounced\n"
            f"   x1 avg: {avg_x1}"
        )

    return "\n\n".join(lines)