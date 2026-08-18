import os
import re
import time
import json
from groq import Groq
from dotenv import load_dotenv
from interaction_log import log_action

load_dotenv()

client = Groq(
    api_key=os.environ.get("GROQ_API_KEY_DEXTER")
)

RESEARCH_MODEL = "openai/gpt-oss-120b"
CHAT_MODEL     = "openai/gpt-oss-20b"

DEXTER_SYSTEM_PROMPT = """
You are Dexter, Research Manager at DaVinci AI.
DaVinci AI helps businesses automate workflows with AI agents.
You find business prospects for the CEO to reach out to.
You speak directly with the CEO over Slack DM.

You research ANY type of business in ANY location.
You are not limited to any industry or geography.

BEHAVIOUR:
- Short and direct — this is Slack, not a report
- Before researching vague requests, confirm industry and location
- Ask one clarifying question if the request is ambiguous
- Say clearly what you found and what you couldn't find
- Use specific search terms for best results

COMMANDS:
- !prospects              → show full pipeline
- !prospects researched   → show uncontacted only
- !prospects sent         → show emailed ones
- !research <query>       → research businesses
- !add <business>         → research one specific business
- !resetrun               → cancel current session
"""

DAILY_LIMIT_70B = 100_000
DAILY_LIMIT_8B  = 500_000

session_tokens_research = 0
session_tokens_chat     = 0

# ─────────────────────────────────────────
# ELICITATION STATE
# Stage flow:
#   industry → location → [clarify] → ready
# ─────────────────────────────────────────

elicitation_state = {}


def _needs_elicitation(text: str) -> bool:
    """
    Returns True if BOTH industry AND location
    are missing from the instruction.
    """
    text_lower = text.lower()

    location_words = [
        "in ", "at ", "near ", "around ",
        "uk", "usa", "us", "australia", "canada",
        "germany", "france", "netherlands", "india",
        "london", "berlin", "paris", "amsterdam",
        "new york", "sydney", "singapore", "dubai",
        "city", "country", "region", "europe",
        "asia", "africa", "america", "worldwide",
        "global", "international", "austria",
        "switzerland", "sweden", "norway", "denmark",
        "belgium", "spain", "italy", "portugal",
        "poland", "czech", "hungary", "romania",
        "japan", "japanese", "tokyo", "osaka"
    ]

    industry_words = [
        "cafe", "bakery", "restaurant", "shop",
        "agency", "startup", "saas", "software",
        "ecommerce", "retail", "brand", "company",
        "firm", "studio", "clinic", "gym", "salon",
        "hotel", "bar", "pub", "store", "service",
        "consultant", "freelancer", "manufacturer",
        "supplier", "distributor", "wholesaler",
        "vegan", "organic", "tech", "fintech",
        "health", "wellness", "fashion", "food",
        "beverage", "marketing", "design", "law",
        "accounting", "finance", "real estate",
        "construction", "education", "media",
        "plant-based", "sustainable", "eco",
        "beauty", "fitness", "apparel", "clothing",
        "jewellery", "jewelry", "furniture", "home",
        "pet", "travel", "insurance", "logistics",
        "recruitment", "hr", "legal", "dental",
        "medical", "pharmacy", "grocery", "dairy",
        "coffee", "tea", "juice", "smoothie",
        "brewery", "winery", "bakeries", "businesses",
        "companies", "brands", "shops", "agencies",
        "meat", "mock", "plant", "protein",
        "manufacturer", "producer", "seller"
    ]

    has_location = any(
        w in text_lower for w in location_words
    )
    has_industry = any(
        w in text_lower for w in industry_words
    )

    return not (has_location and has_industry)


def _detect_ambiguity(text: str) -> str | None:
    """
    Detects ambiguous phrasing that needs one
    clarifying question before research starts.
    Returns a question string or None.
    """
    text_lower = text.lower()

    # Pattern 1 — "make and sell" / "make or sell"
    if re.search(
        r'\b(make|manufacture|produce)\b.{0,20}'
        r'\b(and|or)\b.{0,20}'
        r'\b(sell|retail|distribute)\b',
        text_lower
    ):
        return (
            "Do you want businesses that *manufacture* "
            "this product, or ones that *sell/retail* it "
            "— or both?\n\n"
            "_(Manufacturers and retailers need different "
            "outreach angles, so I'll search them "
            "separately if you want both)_"
        )

    # Pattern 2 — "both X and Y"
    if re.search(r'\bboth\b.{0,40}\band\b', text_lower):
        return (
            "You mentioned *both* — should I search for "
            "these as one combined list, or run separate "
            "searches for each type?\n\n"
            "_(Separate searches give better results "
            "for each category)_"
        )

    # Pattern 3 — 3+ business types mentioned
    multi_match = re.findall(
        r'\b(cafe|bakery|restaurant|shop|agency|'
        r'startup|manufacturer|retailer|supplier|'
        r'distributor|producer|brand|company)\b',
        text_lower
    )
    if len(multi_match) >= 3:
        types = list(set(multi_match))
        return (
            f"You mentioned several business types: "
            f"*{', '.join(types)}*.\n\n"
            f"Which should I prioritise, or should I "
            f"search for all of them?"
        )

    # Pattern 4 — contradictory size qualifiers
    if re.search(
        r'\b(small|medium|large|enterprise|startup)\b'
        r'.{0,10}\band\b.{0,10}'
        r'\b(small|medium|large|enterprise|startup)\b',
        text_lower
    ):
        return (
            "What size of business should I focus on — "
            "small/medium, or larger enterprises?\n\n"
            "_(This affects which search terms work best)_"
        )

    return None


def _build_search_query(
    industry:      str,
    location:      str,
    original:      str = "",
    clarification: str = ""
) -> str:
    """
    Builds a clean search query.
    Uses clarification answer if provided.
    Uses original instruction directly when specific.
    """
    if clarification:
        query = (
            f"{clarification} {industry} {location}"
        ).strip()
        query = re.sub(r'\s+', ' ', query).strip()
        print(
            f"🔎 [DEXTER] Clarified query: '{query}'"
        )
        return query

    if original and not _needs_elicitation(original):
        query = re.sub(
            r'^\d+\s+', '', original.strip()
        ).strip()
        query = re.sub(
            r'\b(both|make and sell|make or sell)\b',
            '', query, flags=re.IGNORECASE
        ).strip()
        query = re.sub(r'\s+', ' ', query).strip()
        print(
            f"🔎 [DEXTER] Using original: '{query}'"
        )
        return query

    query = (
        f"{industry.strip().lower()} {location.strip()}"
    )
    print(f"🔎 [DEXTER] Built query: '{query}'")
    return query


def start_elicitation(
    user_id:  str,
    original: str
) -> str:
    elicitation_state[user_id] = {
        "stage":         "industry",
        "industry":      "",
        "location":      "",
        "clarification": "",
        "original":      original
    }
    print(
        f"❓ [ELICIT] Starting for {user_id}: "
        f"'{original[:50]}'"
    )
    return (
        "What *industry or type of business* "
        "should I research?\n\n"
        "Examples: _SaaS companies, vegan restaurants, "
        "law firms, e-commerce brands, "
        "marketing agencies, gyms, hotels..._"
    )


def start_clarification(
    user_id:  str,
    original: str,
    industry: str,
    location: str,
    question: str
) -> str:
    elicitation_state[user_id] = {
        "stage":         "clarify",
        "industry":      industry,
        "location":      location,
        "clarification": "",
        "original":      original
    }
    print(
        f"❓ [CLARIFY] Ambiguity for {user_id}"
    )
    return question


def handle_elicitation_reply(
    user_id: str,
    text:    str
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Returns: (question, query, industry, location)
    """
    state = elicitation_state.get(user_id)
    if not state:
        return None, None, None, None

    stage = state["stage"]

    if stage == "industry":
        state["industry"] = text.strip()
        state["stage"]    = "location"
        print(
            f"❓ [ELICIT] Industry: '{state['industry']}'"
        )
        question = (
            f"Got it — *{state['industry']}*.\n\n"
            f"Which *country, city, or region* "
            f"should I focus on?\n\n"
            f"Examples: _London UK, Netherlands, "
            f"New York USA, Japan..._"
        )
        return question, None, None, None

    elif stage == "location":
        state["location"] = text.strip()
        industry          = state["industry"]
        location          = state["location"]
        original          = state["original"]

        print(
            f"✅ [ELICIT] Industry='{industry}' "
            f"Location='{location}'"
        )

        combined           = (
            f"{original} {industry} {location}"
        )
        ambiguity_question = _detect_ambiguity(combined)

        if ambiguity_question:
            state["stage"] = "clarify"
            print(
                f"❓ [ELICIT] Ambiguity detected"
            )
            return ambiguity_question, None, None, None

        state["stage"] = "ready"
        query = _build_search_query(
            industry=industry,
            location=location,
            original=original
        )
        del elicitation_state[user_id]
        return None, query, industry, location

    elif stage == "clarify":
        state["clarification"] = text.strip()
        state["stage"]         = "ready"

        industry      = state["industry"]
        location      = state["location"]
        original      = state["original"]
        clarification = state["clarification"]

        print(
            f"✅ [CLARIFY] Answer: '{clarification}'"
        )

        query = _build_search_query(
            industry=industry,
            location=location,
            original=original,
            clarification=clarification
        )
        del elicitation_state[user_id]
        return None, query, industry, location

    return None, None, None, None


def is_in_elicitation(user_id: str) -> bool:
    return user_id in elicitation_state


def cancel_elicitation(user_id: str):
    if user_id in elicitation_state:
        del elicitation_state[user_id]
        print(f"🚫 [ELICIT] Cancelled for {user_id}")


# ─────────────────────────────────────────
# TOKEN FOOTER
# ─────────────────────────────────────────

def _token_footer(
    tokens_this_call: int,
    model:            str
) -> str:
    global session_tokens_research, session_tokens_chat

    if "70b" in model.lower():
        session_tokens_research += tokens_this_call
        pct_used = min(
            (session_tokens_research / DAILY_LIMIT_70B)
            * 100, 100
        )
        label = "research (70B)"
    else:
        session_tokens_chat += tokens_this_call
        pct_used = min(
            (session_tokens_chat / DAILY_LIMIT_8B)
            * 100, 100
        )
        label = "chat (8B)"

    pct_remaining = max(100 - pct_used, 0)
    indicator     = (
        "🟢" if pct_remaining > 20 else
        "🟡" if pct_remaining > 6  else
        "🔴"
    )
    filled = int(pct_used / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    return (
        f"\n\n─────────────────────\n"
        f"{indicator} `{bar}` "
        f"{pct_used:.1f}% used · "
        f"{pct_remaining:.1f}% remaining today "
        f"({label})"
    )


# ─────────────────────────────────────────
# SKILL LOADER
# ─────────────────────────────────────────

def _load_skill(filename: str) -> str:
    paths = [
        os.path.join(
            os.path.dirname(__file__), "..", filename
        ),
        os.path.join(os.getcwd(), filename)
    ]

    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    content = f.read()
                print(f"✅ [DEXTER] Skill: {filename}")
                return content
            except Exception as e:
                print(f"⚠️  [DEXTER] Read error: {e}")

    print(f"❌ [DEXTER] {filename} not found — fallback")

    if filename == "research_skill.txt":
        return """
Extract ONLY businesses matching the search intent.
Return a JSON array. Each entry needs: business_name
(never null), contact_name (or null), email (or null),
website (or null), location, industry, research_summary.
Skip irrelevant results. Never invent emails.
Respond ONLY with JSON array, no explanation.
"""
    return ""


# ─────────────────────────────────────────
# GROQ CALL WITH RETRY
# ─────────────────────────────────────────

def _call_groq(
    messages:    list,
    model:       str,
    max_tokens:  int,
    temperature: float = 0.7
) -> tuple[str, int]:
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            content     = response.choices[0].message.content
            tokens_used = response.usage.total_tokens
            print(
                f"🔢 [DEXTER] {model}: "
                f"{tokens_used} tokens"
            )
            return content, tokens_used

        except Exception as e:
            if "rate_limit_exceeded" in str(e) \
               and attempt == 0:
                print(
                    f"⏳ [DEXTER] Rate limit — "
                    f"waiting 60s..."
                )
                time.sleep(60)
                continue
            raise e


# ─────────────────────────────────────────
# GENERAL CHAT
# ─────────────────────────────────────────

def chat_with_dexter(
    user_id:      str,
    user_message: str
) -> str:
    from memory import get_history, add_message

    history = get_history("dexter", user_id)
    add_message("dexter", user_id, "user", user_message)

    messages = history + [
        {"role": "user", "content": user_message}
    ]

    try:
        reply, tokens_used = _call_groq(
            messages=[
                {
                    "role":    "system",
                    "content": DEXTER_SYSTEM_PROMPT
                }
            ] + messages,
            model=CHAT_MODEL,
            max_tokens=400,
            temperature=0.7
        )

        add_message("dexter", user_id, "assistant", reply)
        return reply + _token_footer(
            tokens_used, CHAT_MODEL
        )

    except Exception as e:
        print(f"❌ [DEXTER] Chat error: {e}")
        return f"Sorry, hit an error: {e}."


# ─────────────────────────────────────────
# EXTRACT FROM RAW BATCH
# Higher text cap + more aggressive extraction
# ─────────────────────────────────────────

def _extract_from_raw(
    raw:            str,
    industry:       str,
    location:       str,
    research_skill: str
) -> list[dict]:
    """
    Processes one batch of raw search results.
    Passes industry + location for strict filtering.
    Increased text cap and more aggressive extraction.
    """
    task = f"""
Search intent: Find {industry} businesses in {location}.

IMPORTANT:
- Extract EVERY distinct business you can find
- Only include businesses matching: {industry} in {location}
- Extract as many as possible — do not limit yourself
- Include businesses even if information is partial
- If a business name appears with a website or email,
  always include it
- Focus on small and medium sized businesses

Web search results:
{raw[:8000]}
"""

    try:
        raw_output, tokens_used = _call_groq(
            messages=[
                {
                    "role":    "system",
                    "content": research_skill
                },
                {
                    "role":    "user",
                    "content": task
                }
            ],
            model=RESEARCH_MODEL,
            max_tokens=3000,
            temperature=0.1
        )

        global session_tokens_research
        session_tokens_research += tokens_used

        pct = (
            session_tokens_research / DAILY_LIMIT_70B
        ) * 100
        print(
            f"🔢 [DEXTER] 70B total: "
            f"{session_tokens_research} tokens "
            f"({pct:.1f}%)"
        )

        clean = re.sub(
            r'```(?:json)?\n?|\n?```',
            '', raw_output.strip()
        )
        array_match = re.search(
            r'\[.*\]', clean, re.DOTALL
        )
        if array_match:
            clean = array_match.group(0)

        prospects = json.loads(clean)

        valid = []
        for p in prospects:
            name = p.get("business_name")
            if not name or \
               str(name).strip().lower() in [
                   "none", "null", "unknown", "",
                   "n/a", "not found", "not available"
               ]:
                continue

            has_data = any([
                p.get("email"),
                p.get("website"),
                p.get("location"),
                p.get("research_summary")
            ])
            if not has_data:
                continue

            valid.append(p)

        print(f"   ✅ Batch: {len(valid)} valid")
        return valid

    except json.JSONDecodeError as e:
        print(f"❌ [DEXTER] JSON failed: {e}")
        return []
    except Exception as e:
        print(f"❌ [DEXTER] Batch failed: {e}")
        return []


# ─────────────────────────────────────────
# RESEARCH BUSINESSES
# Main research function
# Audit gate rejects prospects with no email
# ─────────────────────────────────────────

def research_businesses(
    user_id:     str,
    instruction: str,
    say_fn,
    industry:    str = None,
    location:    str = None,
    target:      int = 10
) -> list[dict]:
    """
    Researches businesses matching the instruction.

    Flow:
    1. Web search — many queries, city by city
    2. 70B extraction per batch
    3. Audit gate — aggressive email enrichment
    4. Reject any that fail all strategies
    5. Return only prospects with confirmed emails
    """
    from tools.web_researcher import (
        search_businesses,
        search_businesses_multi_query
    )

    # Extract target number from instruction
    number_match = re.search(r'\b(\d+)\b', instruction)
    if number_match:
        mentioned = int(number_match.group(1))
        if 1 < mentioned <= 500:
            target = mentioned

    # ── DETERMINE INDUSTRY + LOCATION ────
    if not industry or not location:
        parse_prompt = f"""
Extract the industry/business type and location from:
"{instruction}"

Reply exactly:
industry: <type of business — be specific>
location: <geographic location>

If unclear: unknown
"""
        try:
            parsed_raw, _ = _call_groq(
                messages=[
                    {
                        "role":    "user",
                        "content": parse_prompt
                    }
                ],
                model=CHAT_MODEL,
                max_tokens=60,
                temperature=0.1
            )

            for line in parsed_raw.strip().split("\n"):
                if line.startswith("industry:"):
                    val = line.split(":", 1)[1].strip()
                    if val.lower() != "unknown" \
                       and not industry:
                        industry = val
                elif line.startswith("location:"):
                    val = line.split(":", 1)[1].strip()
                    if val.lower() != "unknown" \
                       and not location:
                        location = val

        except Exception:
            pass

    industry = industry or "businesses"
    location = location or "worldwide"

    # ── BUILD SEARCH QUERY ────────────────
    if not _needs_elicitation(instruction):
        search_query = re.sub(
            r'^\d+\s+', '', instruction.strip()
        ).strip()
        search_query = re.sub(
            r'\b(both|make and sell|make or sell|'
            r'list of|list|list \d+)\b',
            '', search_query, flags=re.IGNORECASE
        ).strip()
        search_query = re.sub(
            r'\s+', ' ', search_query
        ).strip()
    else:
        search_query = f"{industry} {location}"

    print(
        f"🔬 [DEXTER] Research — "
        f"query='{search_query}' "
        f"industry='{industry}' "
        f"location='{location}' "
        f"target={target}"
    )

    say_fn(
        f"🔬 Researching: *{search_query}*\n"
        f"_Target: {target} businesses..._"
    )

    research_skill = _load_skill("research_skill.txt")

    all_valid  = []
    seen_names = set()

    if target <= 10:
        say_fn("🌐 Searching the web...")
        raw = search_businesses(
            search_query, max_results=10
        )

        if not raw:
            say_fn(
                "⚠️ No results found. "
                "Try different keywords."
            )
            return []

        say_fn("🧠 Extracting business details...")
        batch = _extract_from_raw(
            raw=raw,
            industry=industry,
            location=location,
            research_skill=research_skill
        )
        for p in batch:
            name = (p.get("business_name") or "").strip()
            if name and name.lower() not in seen_names:
                seen_names.add(name.lower())
                all_valid.append(p)

    else:
        say_fn(
            f"🌐 Running multiple searches to find "
            f"{target} *{search_query}* businesses..."
        )

        result_blocks = search_businesses_multi_query(
            industry=search_query,
            location=location,
            target=target
        )

        if not result_blocks:
            say_fn("⚠️ No results found.")
            return []

        total_blocks = len(result_blocks)
        say_fn(
            f"🧠 Extracting from "
            f"{total_blocks} search batches..."
        )

        for i, raw_block in enumerate(result_blocks):
            if len(all_valid) >= target:
                print(
                    f"🎯 [DEXTER] Target {target} reached"
                )
                break

            say_fn(
                f"_Batch {i + 1}/{total_blocks} — "
                f"{len(all_valid)}/{target} found..._"
            )

            batch = _extract_from_raw(
                raw=raw_block,
                industry=industry,
                location=location,
                research_skill=research_skill
            )

            for p in batch:
                name = (
                    p.get("business_name") or ""
                ).strip()

                if not name:
                    continue

                name_key = name.lower()
                if name_key in seen_names:
                    continue

                seen_names.add(name_key)
                all_valid.append(p)

                if len(all_valid) >= target:
                    break

        print(
            f"✅ [DEXTER] {len(all_valid)} unique "
            f"(target: {target})"
        )

        if len(all_valid) < target:
            say_fn(
                f"ℹ️ Found *{len(all_valid)}* businesses "
                f"(target was {target} — web results "
                f"were limited for this niche)."
            )
        else:
            say_fn(
                f"✅ Found *{len(all_valid)}* businesses! "
                f"Running audit check..."
            )

    if not all_valid:
        say_fn(
            "⚠️ No matching businesses found.\n"
            "Try more specific keywords."
        )
        return []

    # ── AUDIT GATE ────────────────────────
    from tools.email_finder import audit_prospect

    missing_count = sum(
        1 for p in all_valid if not p.get("email")
    )

    if missing_count > 0:
        say_fn(
            f"📧 {len(all_valid)} businesses found — "
            f"auditing {missing_count} without email..."
        )
    else:
        say_fn(
            f"📧 {len(all_valid)} businesses found — "
            f"running audit check..."
        )

    passed   = []
    enriched = []
    rejected = []

    for p in all_valid:
        result = audit_prospect(p)

        if result["decision"] == "pass":
            if not p.get("email") and \
               result["prospect"].get("email"):
                enriched.append(
                    result["prospect"]["business_name"]
                )
            passed.append(result["prospect"])

        else:
            rejected.append({
                "name":   p.get("business_name"),
                "reason": result["reason"]
            })
            print(
                f"❌ [AUDIT] Rejected: "
                f"'{p.get('business_name')}' — "
                f"{result['reason']}"
            )

    print(
        f"✅ [AUDIT] {len(passed)} passed, "
        f"{len(enriched)} enriched, "
        f"{len(rejected)} rejected"
    )

    if enriched:
        say_fn(
            f"✅ Found emails for "
            f"*{len(enriched)}* additional "
            f"business"
            f"{'es' if len(enriched) > 1 else ''} "
            f"through deeper search."
        )

    if rejected:
        rejected_list = "\n".join(
            f"  • {r['name']}"
            for r in rejected[:5]
        )
        if len(rejected) > 5:
            rejected_list += (
                f"\n  • ...and "
                f"{len(rejected) - 5} more"
            )
        say_fn(
            f"⚠️ *{len(rejected)}* "
            f"business"
            f"{'es' if len(rejected) > 1 else ''} "
            f"excluded — no email found:\n"
            f"{rejected_list}"
        )

    if not passed:
        say_fn(
            "⚠️ No prospects passed the audit.\n"
            "Try a more specific search — businesses "
            "with websites tend to have findable emails."
        )

    return passed