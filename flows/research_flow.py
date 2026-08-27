"""
Phase A — Research Flow Orchestrator
Wraps the research cycle loop with:
- Research request creation and tracking
- Quality check per prospect
- Keyword expansion when short of target
- Summary enrichment for thin summaries
- Domain-level deduplication
- Cycle counter with MAX_CYCLES hard cap

Uses Groq for all LLM calls (query parsing).
Research extraction uses Groq 70B via agents/dexter.py.
"""

import re
import json
import time
from groq import Groq

from config import (
    FAST_MODEL,
    GROQ_API_KEY,
    MAX_CYCLES,
    INITIAL_K,
    MAX_K,
    MIN_RESEARCH_SUMMARY,
)
from tools.quality_check   import quality_check
from tools.keyword_expander import expand_keywords
from tools.enricher         import enrich_summary
from tools.prospect_db      import (
    add_prospect,
    get_domain_exists,
    create_research_request,
    update_research_request,
    create_search_cycle,
    update_search_cycle,
    extract_region,
)


def _groq() -> Groq:
    return Groq(api_key=GROQ_API_KEY)


# ─────────────────────────────────────────
# QUERY PARSER
# ─────────────────────────────────────────

def _spell_correct_location(text: str) -> str:
    """
    Fixes common misspellings of country/city names
    before passing to the LLM parser.
    """
    corrections = {
        # Netherlands variants
        r'\bnetherland\b': 'Netherlands',
        r'\bnethrelands\b': 'Netherlands',
        r'\bnethreand\b': 'Netherlands',
        r'\bholland\b': 'Netherlands',
        # Germany
        r'\bgermeny\b': 'Germany',
        r'\bgemany\b': 'Germany',
        r'\bgermany\b': 'Germany',
        # United Kingdom
        r'\bu\.k\b': 'UK',
        r'\bunitied kingdom\b': 'United Kingdom',
        r'\bbritian\b': 'United Kingdom',
        r'\bbritain\b': 'United Kingdom',
        # Japan
        r'\bjappan\b': 'Japan',
        r'\bjapan\b': 'Japan',
        # France
        r'\bfrance\b': 'France',
        r'\bfarnce\b': 'France',
        # Australia
        r'\baustraila\b': 'Australia',
        r'\baustralia\b': 'Australia',
        # USA
        r'\busa\b': 'USA',
        r'\bus\b': 'USA',
        r'\bamerica\b': 'USA',
        r'\bunited states\b': 'USA',
        # Sweden
        r'\bsweden\b': 'Sweden',
        r'\bswede\b': 'Sweden',
        # Belgium
        r'\bbelgium\b': 'Belgium',
        r'\bbelgum\b': 'Belgium',
        # Denmark
        r'\bdenmark\b': 'Denmark',
        r'\bdenmakr\b': 'Denmark',
        # Spain
        r'\bspain\b': 'Spain',
        r'\bspain\b': 'Spain',
        # Italy
        r'\bitaly\b': 'Italy',
        r'\bitaley\b': 'Italy',
        # Canada
        r'\bcanada\b': 'Canada',
        r'\bcanda\b': 'Canada',
        # Thailand
        r'\bthailand\b': 'Thailand',
        r'\bthaland\b': 'Thailand',
        # India
        r'\bindia\b': 'India',
        r'\binida\b': 'India',
    }
    result = text
    for pattern, replacement in corrections.items():
        result = re.sub(
            pattern, replacement, result,
            flags=re.IGNORECASE
        )
    return result


# Known countries/cities for regex fallback
KNOWN_LOCATIONS = [
    "Netherlands", "Holland", "Germany", "Deutschland",
    "United Kingdom", "UK", "England", "Scotland",
    "France", "Japan", "Australia", "USA", "America",
    "United States", "Sweden", "Belgium", "Denmark",
    "Spain", "Italy", "Canada", "Thailand", "India",
    "Ireland", "Austria", "Switzerland", "Norway",
    "Finland", "Poland", "Portugal", "Singapore",
    "Amsterdam", "Rotterdam", "Berlin", "Munich",
    "Hamburg", "London", "Paris", "Tokyo", "Osaka",
    "Sydney", "Melbourne", "Toronto", "Vancouver",
    "Stockholm", "Copenhagen", "Brussels", "Vienna",
    "Madrid", "Barcelona", "Milan", "Rome",
]


def parse_query(raw_query: str) -> dict:
    """
    Parses natural language research request into:
    { industry, location, target_size }
    Uses Groq FAST_MODEL.

    Strips command words and spell-corrects locations
    before passing to the model.
    """
    # Spell correct the whole query first
    corrected = _spell_correct_location(raw_query)

    # Pre-clean: strip leading command words
    clean = re.sub(
        r'^\s*(research|find|get me|look for|search for|'
        r'i need|can you find|add|!research|!add)\s+',
        '', corrected.strip(), flags=re.IGNORECASE
    ).strip()

    prompt = f"""You extract structured search parameters
from a business research request.

Request: "{clean}"

Reply with JSON only — no explanation, no markdown, no extra text:
{{
  "industry": "<the type of business to search for>",
  "location": "<country or city — MUST be extracted from the request>",
  "target_size": <integer, default 10 if not mentioned>
}}

CRITICAL RULES:
- "industry" = ONLY the business type. Remove location names and numbers from it.
- "location" = the country or city in the request. NEVER output "worldwide" or "global" if any country or city name appears.
- Any country name in the request (Netherlands, Germany, Japan, UK, etc.) MUST be the location.
- Numbers at the end = target_size, not part of industry.
- Strip command words: research, find, get me, look for — not part of industry.

Examples (follow these exactly):
  "mock meats Netherlands, 50"
  → {{"industry": "mock meat brands", "location": "Netherlands", "target_size": 50}}

  "research vegan cafes in Netherlands, 50"
  → {{"industry": "vegan cafes", "location": "Netherlands", "target_size": 50}}

  "find 200 mock meat brands in Germany"
  → {{"industry": "mock meat brands", "location": "Germany", "target_size": 200}}

  "plant based food Japan 30"
  → {{"industry": "plant-based food businesses", "location": "Japan", "target_size": 30}}

  "eco leather UK"
  → {{"industry": "eco leather companies", "location": "United Kingdom", "target_size": 10}}

  "vegan restaurants Amsterdam 20"
  → {{"industry": "vegan restaurants", "location": "Amsterdam", "target_size": 20}}
"""

    for attempt in range(2):
        try:
            response = _groq().chat.completions.create(
                model=FAST_MODEL,
                max_tokens=200,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}]
            )
            raw  = response.choices[0].message.content.strip()
            raw  = raw.replace("```json", "").replace("```", "").strip()

            # Extract JSON if wrapped in extra text
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                raw = match.group(0)

            data = json.loads(raw)

            industry    = (data.get("industry") or "businesses").strip()
            location    = (data.get("location") or "worldwide").strip()
            target_size = int(data.get("target_size") or 10)
            target_size = max(1, min(target_size, 500))

            # Sanity check — if industry still contains
            # the location or looks wrong, log it
            if location.lower() in industry.lower() and                location.lower() != "worldwide":
                industry = re.sub(
                    re.escape(location), '',
                    industry, flags=re.IGNORECASE
                ).strip().strip(',').strip()

            print(
                f"🔎 [QUERY PARSER] "
                f"industry='{industry}' "
                f"location='{location}' "
                f"target={target_size}"
            )
            return {
                "industry":    industry,
                "location":    location,
                "target_size": target_size
            }

        except Exception as e:
            if "rate_limit" in str(e).lower() and attempt == 0:
                time.sleep(60)
                continue
            print(f"❌ [QUERY PARSER] Error: {e} | raw: {raw[:100] if 'raw' in dir() else 'n/a'}")
            break

    # Fallback — smart regex extraction using known locations
    clean_fb = re.sub(
        r'^(research|find|get me|look for|search for|'
        r'i need|can you find)\s+',
        '', corrected.strip(), flags=re.IGNORECASE
    )
    number_match = re.search(r'\b(\d+)\b', clean_fb)
    target       = int(number_match.group(1)) if number_match else 10

    # Try to find a known location in the query
    location_fb = "worldwide"
    matched_loc = ""
    for loc in sorted(KNOWN_LOCATIONS, key=len, reverse=True):
        if re.search(r'\b' + re.escape(loc) + r'\b',
                     clean_fb, re.IGNORECASE):
            location_fb = loc
            matched_loc = loc
            break

    # Industry = strip command words, numbers, location, "in"
    industry_fb = clean_fb
    industry_fb = re.sub(r'\s*,?\s*\d+.*$', '', industry_fb).strip()
    industry_fb = re.sub(r'\s+in\s+.*$', '', industry_fb,
                          flags=re.IGNORECASE).strip()
    if matched_loc:
        industry_fb = re.sub(
            r'\b' + re.escape(matched_loc) + r'\b',
            '', industry_fb, flags=re.IGNORECASE
        ).strip().strip(',').strip()
    industry_fb = industry_fb or "businesses"

    print(
        f"⚠️  [QUERY PARSER] Fallback: "
        f"industry='{industry_fb}' location='{location_fb}'"
    )
    return {
        "industry":    industry_fb,
        "location":    location_fb,
        "target_size": min(target, 500)
    }

# ─────────────────────────────────────────
# RESEARCH WITH KEYWORDS
# ─────────────────────────────────────────

def _load_research_skill() -> str:
    import os
    for path in [
        os.path.join(os.getcwd(), "research_skill.txt"),
        "research_skill.txt"
    ]:
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
    return """Extract ALL distinct businesses from results.
Return a JSON array. Each entry must have:
business_name (required), contact_name (or null),
email (or null), website (or null), location,
industry, research_summary (min 2 sentences).
JSON array only, no explanation."""


def _research_with_keywords(
    keywords:    list[str],
    location:    str,
    target:      int,
    all_domains: set,
    say_fn
) -> list[dict]:
    """
    Runs web search for given keywords and extracts
    prospect dicts via Dexter's extraction function.
    """
    from tools.web_researcher import (
        search_businesses,
        search_businesses_multi_query
    )
    from agents.dexter import _extract_from_raw

    research_skill = _load_research_skill()
    all_candidates = []
    seen_names     = set()

    for keyword in keywords:
        query = f"{keyword} {location}".strip()

        if target <= 10:
            raw = search_businesses(query, max_results=10)
            if raw:
                batch = _extract_from_raw(
                    raw=raw,
                    industry=keyword,
                    location=location,
                    research_skill=research_skill
                )
                for p in batch:
                    name = (p.get("business_name") or "").strip().lower()
                    if name and name not in seen_names:
                        seen_names.add(name)
                        all_candidates.append(p)
        else:
            blocks = search_businesses_multi_query(
                industry=keyword,
                location=location,
                target=target
            )
            for raw_block in blocks:
                batch = _extract_from_raw(
                    raw=raw_block,
                    industry=keyword,
                    location=location,
                    research_skill=research_skill
                )
                for p in batch:
                    name = (p.get("business_name") or "").strip().lower()
                    if name and name not in seen_names:
                        seen_names.add(name)
                        all_candidates.append(p)

                if len(all_candidates) >= target * 3:
                    break

    print(
        f"🔬 [RESEARCH FLOW] {len(all_candidates)} candidates "
        f"from {len(keywords)} keywords"
    )
    return all_candidates


# ─────────────────────────────────────────
# MAIN RESEARCH FLOW
# ─────────────────────────────────────────

def run_research_flow(
    raw_query: str,
    user_id:   str,
    say_fn
):
    """
    Full Phase A research flow.
    Parses query → cycle loop → quality check →
    enrich → store → notify.
    """
    # Step 1 — Parse
    parsed      = parse_query(raw_query)
    industry    = parsed["industry"]
    location    = parsed["location"]
    target_size = parsed["target_size"]
    region      = extract_region(location)

    say_fn(
        f"🔍 *Parsed request:*\n"
        f"Industry: _{industry}_\n"
        f"Location: _{location}_\n"
        f"Region: _{region}_\n"
        f"Target: _{target_size} good leads_\n\n"
        f"Starting research cycle 1/{MAX_CYCLES}..."
    )

    # Step 2 — Create research request record
    request_id = create_research_request(
        raw_query=raw_query,
        industry=industry,
        location=location,
        region=region,
        target_size=target_size
    )

    # Step 3 — Cycle loop
    cycle             = 0
    k                 = INITIAL_K
    good_leads_count  = 0
    all_domains       = set()
    previous_keywords = []

    while good_leads_count < target_size \
          and cycle < MAX_CYCLES:

        cycle += 1

        # Keywords for this cycle
        if cycle == 1:
            keywords = [industry]
        else:
            k        = min(k + 1, MAX_K)
            keywords = expand_keywords(
                industry=industry,
                location=location,
                k=k,
                already_tried=previous_keywords
            )
            if not keywords:
                print(f"⚠️  [RESEARCH FLOW] No new keywords at cycle {cycle}")
                break

        previous_keywords += keywords

        say_fn(
            f"🔄 *Cycle {cycle}/{MAX_CYCLES}* — "
            f"{good_leads_count}/{target_size} good leads\n"
            f"_Keywords: {', '.join(keywords[:3])}"
            f"{'...' if len(keywords) > 3 else ''}_"
        )

        cycle_id = create_search_cycle(
            request_id=request_id,
            cycle_index=cycle,
            keywords_used=keywords,
            k_value=k
        ) if request_id else None

        # Step 4 — Research
        candidates = _research_with_keywords(
            keywords=keywords,
            location=location,
            target=target_size - good_leads_count,
            all_domains=all_domains,
            say_fn=say_fn
        )

        cycle_raw_count  = len(candidates)
        cycle_good_count = 0

        say_fn(f"🧪 Quality-checking {len(candidates)} candidates...")

        # Step 5 — Quality check each candidate
        for prospect in candidates:
            prospect["source_query"] = (
                keywords[0] if keywords else industry
            )

            # If no email, try to find one before QC
            email = (prospect.get("email") or "").strip()
            if not email or "@" not in email:
                website  = prospect.get("website") or ""
                biz_name = prospect.get("business_name", "")
                loc      = prospect.get("location", "")
                try:
                    from tools.web_researcher import (
                        search_email_for_business
                    )
                    found = search_email_for_business(
                        business_name=biz_name,
                        website=website,
                        location=loc
                    )
                    if found:
                        prospect["email"] = found
                        print(
                            f"   📧 Found email for "
                            f"'{biz_name}': {found}"
                        )
                except Exception:
                    pass

            # Fast domain pre-check
            email = (prospect.get("email") or "").strip()
            if "@" in email:
                domain = email.split("@")[1].lower()
                if domain in all_domains or get_domain_exists(domain):
                    continue

            # Full 6-gate quality check
            qr = quality_check(prospect)
            if not qr.passed:
                continue

            prospect["good_lead"]            = True
            prospect["has_name_and_contact"] = True
            prospect["cross_verified_count"] = qr.cross_verified_count
            prospect["lead_score"]           = qr.lead_score
            prospect["mx_valid"]             = qr.mx_valid
            prospect["domain"]               = qr.domain

            # Enrich thin summaries
            summary = (prospect.get("research_summary") or "")
            if len(summary.strip()) < MIN_RESEARCH_SUMMARY:
                prospect["research_summary"] = enrich_summary(
                    prospect, industry
                )
                prospect["enriched"] = True

            # Insert to DB
            inserted = add_prospect(prospect)
            if inserted:
                all_domains.add(qr.domain)
                good_leads_count  += 1
                cycle_good_count  += 1

                if good_leads_count >= target_size:
                    break

        if cycle_id:
            update_search_cycle(
                cycle_id=cycle_id,
                leads_found=cycle_raw_count,
                good_leads_found=cycle_good_count
            )

        if request_id:
            update_research_request(
                request_id=request_id,
                good_leads_found=good_leads_count,
                total_cycles=cycle
            )

        say_fn(
            f"✅ Cycle {cycle} done — "
            f"added {cycle_good_count} good leads "
            f"({good_leads_count}/{target_size} total)"
        )

    # Step 6 — Final status
    if good_leads_count >= target_size:
        if request_id:
            update_research_request(
                request_id=request_id,
                status="complete",
                good_leads_found=good_leads_count,
                total_cycles=cycle
            )
        say_fn(
            f"✅ *Research complete!*\n\n"
            f"Found *{good_leads_count}* verified leads "
            f"for _{industry}_ in _{location}_.\n"
            f"Ran {cycle} cycle(s).\n\n"
            f"_Tell Riley *!run {region}* to start outreach._"
        )
        status = "complete"
    else:
        if request_id:
            update_research_request(
                request_id=request_id,
                status="incomplete",
                good_leads_found=good_leads_count,
                total_cycles=cycle
            )
        say_fn(
            f"⚠️ *Research incomplete.*\n\n"
            f"Found *{good_leads_count}/{target_size}* "
            f"verified leads after {cycle} cycle(s).\n"
            f"The niche may be too narrow for the full target.\n\n"
            f"Saved what we found — type "
            f"*!run {region}* to use them,\n"
            f"or reduce the target and try again."
        )
        status = "incomplete"

    return {
        "good_leads_found": good_leads_count,
        "cycles":           cycle,
        "status":           status,
        "region":           region
    }