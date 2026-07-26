import os
import time
import json
import re
from groq import Groq
from dotenv import load_dotenv
from interaction_log import log_action

load_dotenv()

client = Groq(
    api_key=os.environ.get("GROQ_API_KEY_DEXTER")
)

RESEARCH_MODEL = "llama-3.3-70b-versatile"
CHAT_MODEL     = "llama-3.1-8b-instant"

# ─────────────────────────────────────────
# CORE IDENTITY — ~60 tokens
# General purpose — NOT vegan specific
# ─────────────────────────────────────────

DEXTER_SYSTEM_PROMPT = """
You are Dexter, Research Manager at DaVinci AI.
DaVinci AI helps businesses automate workflows with AI agents.
You find business prospects for the CEO to reach out to.
You speak directly with the CEO over Slack DM.

You research ANY type of business in ANY location.
You are not limited to any industry or geography.

BEHAVIOUR:
- Short and direct — this is Slack, not a report
- Before researching, confirm industry and location
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
# Tracks which users Dexter is asking
# clarifying questions to before researching
# ─────────────────────────────────────────

# Structure per user_id:
# {
#   "stage":    "industry" | "location" | "ready"
#   "industry": str
#   "location": str
#   "original": str  ← original message that triggered flow
# }

elicitation_state = {}


def _needs_elicitation(text: str) -> bool:
    """
    Returns True if the instruction is too vague
    and needs industry/location clarification.

    Skips elicitation if the message already contains
    enough specific detail — both an industry type
    and a location hint.
    """
    text_lower = text.lower()

    # Location indicators
    location_words = [
        "in ", "at ", "near ", "around ",
        "uk", "usa", "us", "australia", "canada",
        "germany", "france", "netherlands", "india",
        "london", "berlin", "paris", "amsterdam",
        "new york", "sydney", "singapore", "dubai",
        "city", "country", "region", "europe",
        "asia", "africa", "america"
    ]

    # Industry indicators
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
        "construction", "education", "media"
    ]

    has_location = any(w in text_lower for w in location_words)
    has_industry = any(w in text_lower for w in industry_words)

    # If both are present — no need to ask
    if has_location and has_industry:
        return False

    # If neither — definitely ask
    # If only one — ask for the missing one
    return True


def _build_search_query(
    industry: str,
    location: str,
    original: str = ""
) -> str:
    """
    Builds a clean, specific DuckDuckGo search query
    from confirmed industry and location.
    """
    # Clean up the inputs
    industry = industry.strip().lower()
    location = location.strip()

    query = f"{industry} businesses {location}"

    print(
        f"🔎 [DEXTER] Built query: '{query}' "
        f"(from industry='{industry}' "
        f"location='{location}')"
    )

    return query


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
                print(
                    f"✅ [DEXTER] Skill loaded: {filename}"
                )
                return content
            except Exception as e:
                print(
                    f"⚠️  [DEXTER] Could not read "
                    f"{path}: {e}"
                )

    print(
        f"❌ [DEXTER] {filename} not found — "
        f"using fallback"
    )

    if filename == "research_skill.txt":
        return """
Extract business prospect data from web search results.
Return a JSON array of ALL businesses found.
Each entry must have: business_name (never null),
contact_name (or null), email (or null),
website (or null), location, industry, research_summary.
research_summary: 2-3 sentences specific to this business.
Never invent emails. Never set business_name to null.
Respond ONLY with a JSON array, no explanation.
"""
    return ""


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
                    f"⏳ [DEXTER] Rate limit on {model}"
                    f" — waiting 60s..."
                )
                time.sleep(60)
                continue
            raise e


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


def _resolve_email(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    from tools.email_finder import (
        find_email_from_website,
        find_email_from_business_name
    )

    print(
        f"📧 [EMAIL RESOLVER] Looking for: "
        f"{business_name}"
    )

    if website:
        email = find_email_from_website(
            business_name, website
        )
        if email:
            print(
                f"✅ [EMAIL RESOLVER] Via website: "
                f"{email}"
            )
            return email

    email = find_email_from_business_name(
        business_name, location
    )
    if email:
        print(
            f"✅ [EMAIL RESOLVER] Via name: {email}"
        )
        return email

    print(
        f"❌ [EMAIL RESOLVER] Not found for "
        f"{business_name}"
    )
    return None


def research_businesses(
    user_id:     str,
    instruction: str,
    say_fn
) -> list[dict]:
    """
    Researches businesses matching the instruction.
    instruction should already have industry + location
    confirmed via elicitation before this is called.
    """
    from tools.web_researcher import search_businesses

    print(f"🔬 [DEXTER] Research: '{instruction}'")

    say_fn(
        f"🔬 Researching: *{instruction}*\n"
        f"_Searching the web..._"
    )

    raw_results = search_businesses(
        instruction, max_results=10
    )

    if not raw_results:
        say_fn(
            "⚠️ No web results found.\n"
            "Try different keywords or a broader location."
        )
        return []

    say_fn("🧠 Extracting business details...")

    research_skill = _load_skill("research_skill.txt")

    task = f"""
Search intent: "{instruction}"

Web search results:
{raw_results[:6000]}

Extract ALL distinct businesses matching the search intent.
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
            f"🔢 [DEXTER] Research total: "
            f"{session_tokens_research} tokens "
            f"({pct:.1f}% of 70B daily limit)"
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

        raw_prospects = json.loads(clean)

        valid   = []
        invalid = []

        for p in raw_prospects:
            name = p.get("business_name")

            if not name or \
               str(name).strip().lower() in [
                   "none", "null", "unknown", "",
                   "n/a", "not found", "not available"
               ]:
                invalid.append(p)
                continue

            has_data = any([
                p.get("email"),
                p.get("website"),
                p.get("location"),
                p.get("research_summary")
            ])
            if not has_data:
                invalid.append(p)
                continue

            valid.append(p)

        print(
            f"✅ [DEXTER] {len(valid)} valid, "
            f"{len(invalid)} invalid"
        )

        if not valid:
            say_fn(
                "⚠️ Couldn't extract clean business data.\n"
                "Try more specific keywords."
            )
            return []

        # Hunt for missing emails
        missing_count = sum(
            1 for p in valid if not p.get("email")
        )

        if missing_count > 0:
            say_fn(
                f"📧 {len(valid)} businesses found — "
                f"searching for "
                f"{missing_count} missing email"
                f"{'s' if missing_count > 1 else ''}..."
            )

        for i, p in enumerate(valid):
            if p.get("email"):
                continue

            email = _resolve_email(
                business_name=p["business_name"],
                website=p.get("website"),
                location=p.get("location")
            )

            if email:
                valid[i]["email"] = email

        with_email    = sum(
            1 for p in valid if p.get("email")
        )
        without_email = len(valid) - with_email

        print(
            f"📧 [DEXTER] {with_email} with email, "
            f"{without_email} without"
        )

        if without_email > 0:
            say_fn(
                f"⚠️ Could not find emails for "
                f"{without_email} business"
                f"{'es' if without_email > 1 else ''}. "
                f"Added to pipeline without email."
            )

        return valid

    except json.JSONDecodeError as e:
        print(f"❌ [DEXTER] JSON parse failed: {e}")
        say_fn("⚠️ Trouble parsing results. Try again.")
        return []

    except Exception as e:
        print(f"❌ [DEXTER] Extraction failed: {e}")
        say_fn(f"❌ Research failed: {e}")
        return []


# ─────────────────────────────────────────
# ELICITATION HELPERS
# Called from app.py to manage the
# industry → location → research flow
# ─────────────────────────────────────────

def start_elicitation(
    user_id:  str,
    original: str
) -> str:
    """
    Starts the elicitation flow.
    Stores state and returns the first question.
    """
    elicitation_state[user_id] = {
        "stage":    "industry",
        "industry": "",
        "location": "",
        "original": original
    }

    print(
        f"❓ [ELICIT] Starting for {user_id}: "
        f"'{original[:50]}'"
    )

    return (
        "What *industry or type of business* "
        "should I focus on?\n\n"
        "Examples: _SaaS companies, restaurants, "
        "law firms, e-commerce brands, "
        "marketing agencies, gyms, hotels..._"
    )


def handle_elicitation_reply(
    user_id: str,
    text:    str
) -> tuple[str | None, str | None]:
    """
    Handles a reply during the elicitation flow.

    Returns:
      (question_to_ask, None)     → still collecting info
      (None, search_query)        → ready to research
      (None, None)                → something went wrong
    """
    state = elicitation_state.get(user_id)
    if not state:
        return None, None

    stage = state["stage"]

    if stage == "industry":
        # CEO answered the industry question
        state["industry"] = text.strip()
        state["stage"]    = "location"

        print(
            f"❓ [ELICIT] Industry confirmed: "
            f"'{state['industry']}'"
        )

        question = (
            f"Got it — *{state['industry']}*.\n\n"
            f"Which *country, city, or region* "
            f"should I focus on?\n\n"
            f"Examples: _London UK, Netherlands, "
            f"New York USA, Southeast Asia, "
            f"Berlin Germany..._"
        )
        return question, None

    elif stage == "location":
        # CEO answered the location question
        state["location"] = text.strip()
        state["stage"]    = "ready"

        industry = state["industry"]
        location = state["location"]

        print(
            f"✅ [ELICIT] Complete — "
            f"industry='{industry}' "
            f"location='{location}'"
        )

        # Build the search query
        query = _build_search_query(
            industry=industry,
            location=location,
            original=state["original"]
        )

        # Clear elicitation state
        del elicitation_state[user_id]

        return None, query

    return None, None


def is_in_elicitation(user_id: str) -> bool:
    """Returns True if user is mid-elicitation."""
    return user_id in elicitation_state


def cancel_elicitation(user_id: str):
    """Cancels an in-progress elicitation."""
    if user_id in elicitation_state:
        del elicitation_state[user_id]
        print(f"🚫 [ELICIT] Cancelled for {user_id}")