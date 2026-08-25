"""
Dexter — Research Agent
Phase A orchestration via Slack DM.

All research now goes through flows/research_flow.py
which handles: cycle loop, quality check,
keyword expansion, enrichment, DB storage.
"""

import os
import re
import time
import json
from groq import Groq
from dotenv import load_dotenv
from interaction_log import log_action

load_dotenv()

# Keep Groq for Dexter chat — Claude API used for
# research extraction (in research_flow.py)
client = Groq(
    api_key=os.environ.get("GROQ_API_KEY_DEXTER")
)

RESEARCH_MODEL = "openai/gpt-oss-120b"
CHAT_MODEL     = "openai/gpt-oss-20b"

DEXTER_SYSTEM_PROMPT = """
You are Dexter, Research Manager at DaVinci AI.
DaVinci AI helps businesses automate workflows with AI agents.
You find and verify business prospects for the CEO.
You speak directly with the CEO over Slack DM.

Every prospect you add is:
- Cross-verified from 2 independent sources
- Has a confirmed working email address
- Has a minimum 300-character research summary
- Scored 0-100 for outreach priority

BEHAVIOUR:
- Short and direct — this is Slack, not a report
- Before researching vague requests, confirm industry + location
- Ask one clarifying question if request is ambiguous
- Report cycle progress as it happens
- Tell the CEO clearly if target is unreachable

COMMANDS:
- !segments              → show geographic pipeline
- !prospects             → show all prospects
- !prospects <region>    → filter by region
- !cycles                → show active research requests
- !analytics             → campaign stats
- !queue                 → show research queue
- !queue clear / resume
"""

DAILY_LIMIT_70B = 100_000
DAILY_LIMIT_8B  = 500_000

session_tokens_research = 0
session_tokens_chat     = 0

elicitation_state = {}


# ─────────────────────────────────────────
# ELICITATION + AMBIGUITY DETECTION
# (unchanged from previous version)
# ─────────────────────────────────────────

def _needs_elicitation(text: str) -> bool:
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
        "japan", "japanese", "tokyo", "osaka",
        "thailand", "chiang mai", "bangkok"
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
        "plant-based", "sustainable", "eco",
        "beauty", "fitness", "apparel", "clothing",
        "meat", "mock", "plant", "protein",
        "producer", "seller", "leather", "business",
        "businesses", "companies", "brands"
    ]

    has_location = any(w in text_lower for w in location_words)
    has_industry = any(w in text_lower for w in industry_words)

    return not (has_location and has_industry)


def _detect_ambiguity(text: str) -> str | None:
    text_lower = text.lower()

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
            "outreach angles)_"
        )

    if re.search(r'\bboth\b.{0,40}\band\b', text_lower):
        return (
            "You mentioned *both* — should I search as "
            "one combined list, or separate searches?\n\n"
            "_(Separate searches give better results)_"
        )

    multi_match = re.findall(
        r'\b(cafe|bakery|restaurant|shop|agency|'
        r'startup|manufacturer|retailer|supplier|'
        r'distributor|producer|brand|company)\b',
        text_lower
    )
    if len(multi_match) >= 3:
        types = list(set(multi_match))
        return (
            f"You mentioned several types: "
            f"*{', '.join(types)}*.\n\n"
            f"Which should I prioritise?"
        )

    return None


def _build_search_query(
    industry:      str,
    location:      str,
    original:      str = "",
    clarification: str = ""
) -> str:
    if clarification:
        query = f"{clarification} {industry} {location}"
        return re.sub(r'\s+', ' ', query).strip()

    if original and not _needs_elicitation(original):
        query = re.sub(r'^\d+\s+', '', original.strip())
        query = re.sub(
            r'\b(both|make and sell|make or sell|'
            r'list of|list \d+)\b',
            '', query, flags=re.IGNORECASE
        )
        return re.sub(r'\s+', ' ', query).strip()

    return f"{industry.lower()} {location}"


def start_elicitation(user_id: str, original: str) -> str:
    elicitation_state[user_id] = {
        "stage": "industry", "industry": "",
        "location": "", "clarification": "",
        "original": original
    }
    return (
        "What *industry or type of business* "
        "should I research?\n\n"
        "Examples: _vegan restaurants, "
        "SaaS companies, leather brands..._"
    )


def start_clarification(
    user_id: str, original: str,
    industry: str, location: str, question: str
) -> str:
    elicitation_state[user_id] = {
        "stage": "clarify", "industry": industry,
        "location": location, "clarification": "",
        "original": original
    }
    return question


def handle_elicitation_reply(
    user_id: str, text: str
) -> tuple[str | None, str | None, str | None, str | None]:
    state = elicitation_state.get(user_id)
    if not state:
        return None, None, None, None

    stage = state["stage"]

    if stage == "industry":
        state["industry"] = text.strip()
        state["stage"]    = "location"
        return (
            f"Got it — *{state['industry']}*.\n\n"
            f"Which *country, city, or region*?\n\n"
            f"Examples: _Japan, UK, Netherlands..._"
        ), None, None, None

    elif stage == "location":
        state["location"] = text.strip()
        industry = state["industry"]
        location = state["location"]
        original = state["original"]

        ambiguity_q = _detect_ambiguity(
            f"{original} {industry} {location}"
        )
        if ambiguity_q:
            state["stage"] = "clarify"
            return ambiguity_q, None, None, None

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
        industry      = state["industry"]
        location      = state["location"]
        original      = state["original"]
        clarification = state["clarification"]

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


# ─────────────────────────────────────────
# TOKEN FOOTER (Groq)
# ─────────────────────────────────────────

def _token_footer(tokens: int, model: str) -> str:
    global session_tokens_research, session_tokens_chat

    if "120b" in model.lower() or "70b" in model.lower():
        session_tokens_research += tokens
        pct  = min(
            session_tokens_research / DAILY_LIMIT_70B
            * 100, 100
        )
        label = "research (120B)"
    else:
        session_tokens_chat += tokens
        pct  = min(
            session_tokens_chat / DAILY_LIMIT_8B * 100,
            100
        )
        label = "chat (20B)"

    rem   = max(100 - pct, 0)
    ind   = "🟢" if rem > 20 else "🟡" if rem > 6 else "🔴"
    bar   = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))

    return (
        f"\n\n─────────────────────\n"
        f"{ind} `{bar}` "
        f"{pct:.1f}% used · {rem:.1f}% remaining "
        f"({label})"
    )


# ─────────────────────────────────────────
# SKILL LOADER (for extraction)
# ─────────────────────────────────────────

def _load_skill(filename: str) -> str:
    paths = [
        os.path.join(os.path.dirname(__file__), "..", filename),
        os.path.join(os.getcwd(), filename)
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return f.read()
            except Exception:
                pass

    return """Extract businesses from search results.
Return JSON array. Each: business_name, contact_name,
email, website, location, industry, research_summary.
JSON only, no explanation."""


# ─────────────────────────────────────────
# GROQ CALL
# ─────────────────────────────────────────

def _call_groq(
    messages: list, model: str,
    max_tokens: int, temperature: float = 0.7
) -> tuple[str, int]:
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=model, messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )
            return (
                response.choices[0].message.content,
                response.usage.total_tokens
            )
        except Exception as e:
            if "rate_limit" in str(e) and attempt == 0:
                time.sleep(60)
                continue
            raise e


# ─────────────────────────────────────────
# EXTRACTION (used by research_flow.py)
# ─────────────────────────────────────────

def _extract_from_raw(
    raw: str, industry: str,
    location: str, research_skill: str
) -> list[dict]:
    """
    Extracts prospects from raw search results.
    Called by flows/research_flow.py.
    Uses Groq 120B for extraction.
    """
    loc_str = (
        f'in {location}'
        if location and
        location.lower() not in ['worldwide', 'global', '']
        else '(any location)'
    )

    task = f"""
Search intent: Find {industry} businesses {loc_str}.

INSTRUCTIONS:
1. Extract EVERY distinct business name you see.
2. For email: look carefully for any @domain.com pattern
   in the text. Also check URLs — if you see a website
   like "veganplace.nl", the email is likely info@veganplace.nl
   or hello@veganplace.nl — include your best guess.
3. For website: extract any URL you see for this business.
4. If no email found at all, set email to null — do NOT
   invent one. But do try to infer from the website domain.
5. Include the business even if you only have name + website.
6. Write research_summary: 2-3 specific sentences about
   what this business does, their products/menu, location.

Web search results:
{raw[:8000]}
"""

    try:
        raw_output, tokens_used = _call_groq(
            messages=[
                {"role": "system", "content": research_skill},
                {"role": "user",   "content": task}
            ],
            model=RESEARCH_MODEL,
            max_tokens=4000,
            temperature=0.1
        )

        global session_tokens_research
        session_tokens_research += tokens_used

        clean = re.sub(
            r'```(?:json)?\n?|\n?```',
            '', raw_output.strip()
        )
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            clean = match.group(0)

        prospects = json.loads(clean)
        valid     = []

        for p in prospects:
            name = p.get("business_name")
            if not name or str(name).strip().lower() in [
                "none", "null", "unknown", "", "n/a",
                "not found", "not available"
            ]:
                continue

            has_data = any([
                p.get("email"),
                p.get("website"),
                p.get("location"),
                p.get("research_summary")
            ])
            if has_data:
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
# GENERAL CHAT
# ─────────────────────────────────────────

def chat_with_dexter(user_id: str, user_message: str) -> str:
    from memory import get_history, add_message

    history = get_history("dexter", user_id)
    add_message("dexter", user_id, "user", user_message)

    messages = history + [
        {"role": "user", "content": user_message}
    ]

    try:
        reply, tokens = _call_groq(
            messages=[
                {"role": "system", "content": DEXTER_SYSTEM_PROMPT}
            ] + messages,
            model=CHAT_MODEL,
            max_tokens=400,
            temperature=0.7
        )
        add_message("dexter", user_id, "assistant", reply)
        return reply + _token_footer(tokens, CHAT_MODEL)

    except Exception as e:
        print(f"❌ [DEXTER] Chat error: {e}")
        return f"Sorry, hit an error: {e}."


# ─────────────────────────────────────────
# LEGACY: research_businesses
# Still used by queue runner for backward compat
# New code uses flows/research_flow.py
# ─────────────────────────────────────────

def research_businesses(
    user_id: str, instruction: str, say_fn,
    industry: str = None, location: str = None,
    target: int = 10
) -> list[dict]:
    """
    Legacy entry point — delegates to research_flow.
    """
    from flows.research_flow import run_research_flow
    result = run_research_flow(
        raw_query=instruction,
        user_id=user_id,
        say_fn=say_fn
    )
    # Return empty — prospects now saved directly by flow
    return []