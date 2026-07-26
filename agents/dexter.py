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

DEXTER_SYSTEM_PROMPT = """
You are Dexter, Research Manager at DaVinci AI.
You find businesses for the CEO to reach out to.
You speak directly with the CEO over Slack DM.

BEHAVIOUR:
- Short and direct — this is Slack, not a report
- Say clearly what you found and what you couldn't find
- Ask one question if the instruction is too vague
- For best results, use specific search terms not
  conversational phrases e.g. "vegan cafes Amsterdam"
  not "can you find vegan cafes in Amsterdam please"

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
    """
    Loads a skill file from the project root.
    Only called when that specific task runs.
    """
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
Return a JSON array of businesses found.
Each business must have: business_name (never null),
contact_name (or null), email (or null),
website (or null), location, industry, research_summary.
research_summary: 2-3 sentences specific to this business.
Never invent email addresses. Never set business_name to null.
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


# ─────────────────────────────────────────
# EMAIL RESOLUTION
# Uses Riley's email_finder tools to find
# missing emails for researched prospects
# ─────────────────────────────────────────

def _resolve_email(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """
    Tries to find an email for a business using
    Riley's existing email_finder tools.

    Priority order:
    1. If website URL found — search for email at domain
    2. If no website — search by business name + location
    3. Return None if nothing found
    """
    from tools.email_finder import (
        find_email_from_website,
        find_email_from_business_name
    )

    print(
        f"📧 [EMAIL RESOLVER] Looking for email: "
        f"{business_name}"
    )

    # Strategy 1 — search via website domain
    if website:
        email = find_email_from_website(
            business_name, website
        )
        if email:
            print(
                f"✅ [EMAIL RESOLVER] Found via website: "
                f"{email}"
            )
            return email

    # Strategy 2 — search by business name + location
    email = find_email_from_business_name(
        business_name, location
    )
    if email:
        print(
            f"✅ [EMAIL RESOLVER] Found via name search: "
            f"{email}"
        )
        return email

    print(
        f"❌ [EMAIL RESOLVER] No email found for "
        f"{business_name}"
    )
    return None


# ─────────────────────────────────────────
# RESEARCH BUSINESSES
# Loads research_skill.txt — only here
# Uses 70B model for extraction
# Then hunts for missing emails using
# Riley's email_finder tools
# ─────────────────────────────────────────

def research_businesses(
    user_id:     str,
    instruction: str,
    say_fn
) -> list[dict]:
    """
    Researches businesses matching CEO instruction.

    Flow:
    1. DuckDuckGo web search (zero tokens)
    2. 70B model extracts structured business data
    3. For any prospect missing email — email_finder
       searches for it using website or business name
    4. Returns validated list ready for DB insert
    """
    from tools.web_researcher import search_businesses

    print(f"🔬 [DEXTER] Research: '{instruction}'")

    say_fn(
        f"🔬 On it — researching: *{instruction}*\n"
        f"_Searching the web..._"
    )

    # Step 1 — Web search, zero tokens
    raw_results = search_businesses(
        instruction, max_results=10
    )

    if not raw_results:
        say_fn(
            "⚠️ No web results found.\n"
            "Try specific keywords — e.g.\n"
            "_'plant based food brands Amsterdam "
            "Netherlands'_"
        )
        return []

    say_fn("🧠 Extracting business details...")

    # Step 2 — Load research skill (only here)
    research_skill = _load_skill("research_skill.txt")

    task = f"""
CEO instruction: "{instruction}"

Web search results:
{raw_results[:6000]}

Extract ALL distinct businesses you can find
in these results that match the CEO's instruction.
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

        # Defensive JSON parsing
        clean = re.sub(
            r'```(?:json)?\n?|\n?```',
            '',
            raw_output.strip()
        )
        array_match = re.search(
            r'\[.*\]', clean, re.DOTALL
        )
        if array_match:
            clean = array_match.group(0)

        raw_prospects = json.loads(clean)

        # Validate entries
        valid   = []
        invalid = []

        for p in raw_prospects:
            name = p.get("business_name")

            if not name or \
               str(name).strip().lower() in [
                   "none", "null", "unknown",
                   "", "n/a", "not found",
                   "not available"
               ]:
                invalid.append(p)
                print(
                    f"⚠️  [DEXTER] No business name — "
                    f"skipping"
                )
                continue

            has_data = any([
                p.get("email"),
                p.get("website"),
                p.get("location"),
                p.get("research_summary")
            ])
            if not has_data:
                invalid.append(p)
                print(
                    f"⚠️  [DEXTER] No useful data for "
                    f"'{name}' — skipping"
                )
                continue

            valid.append(p)

        print(
            f"✅ [DEXTER] {len(valid)} valid, "
            f"{len(invalid)} invalid skipped"
        )

        if not valid:
            say_fn(
                "⚠️ Found search results but couldn't "
                "extract clean business data.\n"
                "Try more specific keywords."
            )
            return []

        # ─────────────────────────────────────
        # Step 3 — Hunt for missing emails
        # For every prospect without an email,
        # use Riley's email_finder to search for one
        # ─────────────────────────────────────

        missing_email_count = sum(
            1 for p in valid if not p.get("email")
        )

        if missing_email_count > 0:
            say_fn(
                f"📧 {len(valid)} businesses found — "
                f"searching for "
                f"{missing_email_count} missing "
                f"email address"
                f"{'es' if missing_email_count > 1 else ''}..."
            )

        for i, p in enumerate(valid):
            if p.get("email"):
                print(
                    f"✅ [EMAIL] Already have email for "
                    f"{p['business_name']}: {p['email']}"
                )
                continue

            # No email — try to find it
            email = _resolve_email(
                business_name=p["business_name"],
                website=p.get("website"),
                location=p.get("location")
            )

            if email:
                valid[i]["email"] = email
                print(
                    f"✅ [EMAIL] Found for "
                    f"{p['business_name']}: {email}"
                )
            else:
                print(
                    f"⚠️  [EMAIL] Could not find email "
                    f"for {p['business_name']} — "
                    f"will still add to DB without email"
                )

        # Final email count
        with_email    = sum(
            1 for p in valid if p.get("email")
        )
        without_email = len(valid) - with_email

        print(
            f"📧 [DEXTER] Email summary: "
            f"{with_email} found, "
            f"{without_email} not found"
        )

        if without_email > 0:
            say_fn(
                f"⚠️ Could not find emails for "
                f"{without_email} business"
                f"{'es' if without_email > 1 else ''}. "
                f"They'll still be added to the pipeline "
                f"— Riley will try again when drafting."
            )

        return valid

    except json.JSONDecodeError as e:
        print(
            f"❌ [DEXTER] JSON parse failed: {e}\n"
            f"Raw: {raw_output[:500]}"
        )
        say_fn(
            "⚠️ Had trouble parsing results. "
            "Try a more specific instruction."
        )
        return []

    except Exception as e:
        print(f"❌ [DEXTER] Extraction failed: {e}")
        say_fn(f"❌ Research failed: {e}")
        return []