import os
import time
import json
import re
from groq import Groq
from dotenv import load_dotenv
from interaction_log import log_action

load_dotenv()

# ─────────────────────────────────────────
# DEXTER USES KEY 1 — SEPARATE FROM RILEY
# ─────────────────────────────────────────

client = Groq(api_key=os.environ.get("GROQ_API_KEY_DEXTER"))

RESEARCH_MODEL = "llama-3.3-70b-versatile"
CHAT_MODEL     = "llama-3.1-8b-instant"

# ─────────────────────────────────────────
# DEXTER'S CORE IDENTITY — ~60 tokens only
# No research rules here
# No output format rules here
# No pipeline formatting here
# Those all live in skill files
# ─────────────────────────────────────────

DEXTER_SYSTEM_PROMPT = """
You are Dexter, Research Manager at DaVinci AI.
You find businesses for the CEO to reach out to.
You speak directly with the CEO over Slack DM.

BEHAVIOUR:
- Short and direct — this is Slack, not a report
- Say clearly what you found and what you couldn't find
- Ask one question if the instruction is too vague

COMMANDS:
- !prospects           → show full pipeline
- !prospects researched → show uncontacted only
- !prospects sent      → show emailed ones
- !research <query>    → research businesses
- !add <business>      → research one specific business
- !resetrun            → cancel current session
"""

# ─────────────────────────────────────────
# DAILY TOKEN LIMITS
# ─────────────────────────────────────────

DAILY_LIMIT_70B = 100_000
DAILY_LIMIT_8B  = 500_000

session_tokens_research = 0
session_tokens_chat     = 0


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
            (session_tokens_research / DAILY_LIMIT_70B) * 100,
            100
        )
        label = "research (70B)"
    else:
        session_tokens_chat += tokens_this_call
        pct_used = min(
            (session_tokens_chat / DAILY_LIMIT_8B) * 100,
            100
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
# SKILL LOADER — same pattern as Riley
# ─────────────────────────────────────────

def _load_skill(filename: str) -> str:
    """
    Loads a skill file from the project root.
    Only called when that specific task runs.

    Current Dexter skills:
      research_skill.txt   → how to extract business data
      prospects_skill.txt  → how to format pipeline view
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
                print(f"✅ [DEXTER] Skill loaded: {filename}")
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

    # Fallbacks per skill
    if filename == "research_skill.txt":
        return """
Extract business prospect data from web search results.
Return a JSON array of businesses found.
Each business must have:
  business_name, contact_name (or null), email (or null),
  website (or null), location, industry, research_summary.
research_summary: 2-3 sentences specific to this business.
Never invent email addresses.
Respond ONLY with a JSON array, no explanation.
"""
    if filename == "prospects_skill.txt":
        return """
Format prospect data as a clean Slack pipeline summary.
Group by status. Be concise. Use emojis for status.
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
            if "rate_limit_exceeded" in str(e) and attempt == 0:
                print(
                    f"⏳ [DEXTER] Rate limit on {model}"
                    f" — waiting 60s..."
                )
                time.sleep(60)
                continue
            raise e


# ─────────────────────────────────────────
# GENERAL CHAT
# Core identity prompt only — 8B model
# ─────────────────────────────────────────

def chat_with_dexter(
    user_id:      str,
    user_message: str
) -> str:
    """
    General conversation with Dexter.
    Uses core identity prompt only (~60 tokens).
    No skill files loaded here.
    """
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
        return reply + _token_footer(tokens_used, CHAT_MODEL)

    except Exception as e:
        print(f"❌ [DEXTER] Chat error: {e}")
        return f"Sorry, hit an error: {e}."


# ─────────────────────────────────────────
# RESEARCH BUSINESSES
# Loads research_skill.txt — only here
# Uses 70B model for better extraction
# No chat history sent — not needed
# ─────────────────────────────────────────

def research_businesses(
    user_id:     str,
    instruction: str,
    say_fn
) -> list[dict]:
    """
    Researches businesses matching CEO instruction.
    Loads research_skill.txt as system prompt.
    Uses 70B for better structured data extraction.
    Returns list of prospect dicts for DB insertion.
    """
    from tools.web_researcher import search_businesses

    print(f"🔬 [DEXTER] Research: '{instruction}'")

    say_fn(
        f"🔬 On it — researching: *{instruction}*\n"
        f"_Searching the web..._"
    )

    # Step 1 — Web search (no LLM, no tokens)
    raw_results = search_businesses(instruction)

    if not raw_results:
        say_fn(
            "⚠️ No results found. Try being more specific\n"
            "e.g. _'vegan cafes in Berlin Germany'_"
        )
        return []

    say_fn("🧠 Extracting business details...")

    # Step 2 — Load research skill
    # This is the ONLY place research_skill.txt loads
    research_skill = _load_skill("research_skill.txt")

    # Step 3 — Build extraction task
    # Skill goes as system, task goes as user message
    # No history sent — not relevant for extraction
    task = f"""
CEO instruction: "{instruction}"

Web search results:
{raw_results[:3000]}

Extract up to 5 businesses from these results.
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
            max_tokens=2000,
            temperature=0.1
        )

        # Update research token counter
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

        # Strip markdown fences if present
        clean = re.sub(
            r'```(?:json)?\n?|\n?```',
            '',
            raw_output.strip()
        )

        prospects = json.loads(clean)
        print(
            f"✅ [DEXTER] Extracted "
            f"{len(prospects)} prospects"
        )
        return prospects

    except json.JSONDecodeError as e:
        print(
            f"❌ [DEXTER] JSON parse failed: {e}\n"
            f"Raw: {raw_output[:300]}"
        )
        say_fn(
            "⚠️ Trouble parsing results. "
            "Try a more specific instruction."
        )
        return []

    except Exception as e:
        print(f"❌ [DEXTER] Extraction failed: {e}")
        say_fn(f"❌ Research failed: {e}")
        return []