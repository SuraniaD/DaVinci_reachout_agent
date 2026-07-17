import os
import time
from groq import Groq
from dotenv import load_dotenv
from memory import get_history, add_message
from interaction_log import log_action

load_dotenv()

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ─────────────────────────────────────────
# RILEY'S CORE IDENTITY
# Chat rules only — ~80 tokens
# Email rules live in email_template.txt
# Each skill file loaded only when that task runs
# ─────────────────────────────────────────

RILEY_SYSTEM_PROMPT = """
You are Riley, Outreach Manager at DaVinci AI.
DaVinci AI automates business workflows using AI agents.
You speak directly with the CEO over Slack DM.

BEHAVIOUR:
- Short and direct — this is Slack, not email
- Ask one question when instructions are vague
- Have opinions and share them
- Summarise clearly when asked what you have done
- Say clearly if something went wrong and suggest a fix

COMMANDS:
- !reset        → confirm memory cleared
- !status       → summarise recent outreach activity
- !automode on  → confirm auto-send is on, warn emails go immediately
- !automode off → confirm approval mode is on
"""

# ─────────────────────────────────────────
# DAILY TOKEN LIMIT
# llama-3.1-8b-instant free tier = 500k/day
# Update this if you switch models
# ─────────────────────────────────────────

DAILY_TOKEN_LIMIT = 500_000

# Running session total — resets on restart
session_tokens_used = 0


# ─────────────────────────────────────────
# GROQ CALL WITH RETRY
# Returns (content, tokens_used) tuple
# ─────────────────────────────────────────

def _call_groq_with_retry(
    messages:    list,
    max_tokens:  int,
    temperature: float
) -> tuple[str, int]:
    """
    Calls Groq with one automatic retry on rate limit.
    Waits 60 seconds before retrying.
    Returns (reply_text, tokens_used_this_call).
    """
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )

            content     = response.choices[0].message.content
            tokens_used = response.usage.total_tokens

            print(
                f"🔢 [TOKENS] This call: {tokens_used} tokens"
            )

            return content, tokens_used

        except Exception as e:
            if "rate_limit_exceeded" in str(e) and attempt == 0:
                print("⏳ Groq rate limit — waiting 60s...")
                time.sleep(60)
                continue
            raise e


# ─────────────────────────────────────────
# TOKEN FOOTER
# Appended to every Slack chat reply
# Shows % used and % remaining today
# ─────────────────────────────────────────

def _token_footer(tokens_this_call: int) -> str:
    """
    Builds a token usage line appended to every Slack reply.
    Shows percentage used and remaining — not absolute numbers.
    Session total resets on Railway restart.
    """
    global session_tokens_used
    session_tokens_used += tokens_this_call

    pct_used      = (session_tokens_used / DAILY_TOKEN_LIMIT) * 100
    pct_remaining = 100 - pct_used

    # Cap at boundaries
    pct_used      = min(pct_used, 100)
    pct_remaining = max(pct_remaining, 0)

    # Colour indicator based on remaining
    if pct_remaining > 20:
        indicator = "🟢"
    elif pct_remaining > 6:
        indicator = "🟡"
    else:
        indicator = "🔴"

    # Visual bar — 10 blocks wide
    filled = int(pct_used / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    return (
        f"\n\n"
        f"─────────────────────\n"
        f"{indicator} `{bar}` "
        f"{pct_used:.1f}% used · "
        f"{pct_remaining:.1f}% remaining today"
    )


# ─────────────────────────────────────────
# GENERIC SKILL LOADER
# Reads any .txt skill file from project root
# Only called when that specific task runs
# ─────────────────────────────────────────

def _load_skill(filename: str) -> str:
    """
    Loads a skill file from the project root.
    Each skill contains instructions for one task only.
    Loaded on demand — never loaded unless that task runs.

    Current skills:
      email_template.txt          → email drafting rules
      (future) strategy_template.txt   → targeting advice
      (future) followup_template.txt   → follow-up emails
    """
    skill_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        filename
    )
    try:
        with open(skill_path, "r") as f:
            content = f.read()
        print(f"✅ Skill loaded: {filename}")
        return content
    except FileNotFoundError:
        print(f"⚠️ {filename} not found — using fallback")
        return """
Draft a cold outreach email for DaVinci AI.
DaVinci AI automates business workflows with AI agents.
Sign off: Riley, DaVinci AI. Max 120 words. Be specific and human.
Format exactly:
SUBJECT: <subject>
BODY:
<body>
"""


# ─────────────────────────────────────────
# GENERAL CHAT
# Sends: core identity + history + new message
# Does NOT load any skill files
# Appends token footer to every reply
# ─────────────────────────────────────────

def chat_with_riley(user_id: str, user_message: str) -> str:
    """
    General conversation with Riley.
    Only RILEY_SYSTEM_PROMPT sent as system message.
    No email rules, no strategy rules — chat only.
    Token footer appended to every Slack reply.
    """
    history = get_history("riley", user_id)
    add_message("riley", user_id, "user", user_message)

    messages = history + [{"role": "user", "content": user_message}]

    try:
        reply, tokens_used = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + messages,
            max_tokens=500,
            temperature=0.7
        )

        # Save raw reply to memory — without footer
        # so footer doesn't bloat future history
        add_message("riley", user_id, "assistant", reply)

        # Return reply with token footer for Slack display
        return reply + _token_footer(tokens_used)

    except Exception as e:
        print(f"❌ Groq chat error: {e}")
        return (
            f"Sorry, hit an error: {e}. "
            f"Try again in a moment."
        )


# ─────────────────────────────────────────
# DRAFT OUTREACH EMAIL
# Loads email_template.txt skill only here
# No conversation history sent — not needed
# Research capped at 800 chars — ~200 tokens
# ─────────────────────────────────────────

def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """
    Drafts a personalised outreach email.
    Uses email_template.txt as system prompt.

    Token footer NOT added to draft text itself —
    the draft goes into the approval message in Slack.
    Token usage is logged to Railway and added to
    session total so % remaining stays accurate.
    """
    email_skill = _load_skill("email_template.txt")

    task = f"""Contact name:  {contact_name}
Business name: {business_name}

Research:
{research[:800]}"""

    log_action(
        action_type="draft",
        contact_name=contact_name,
        business_name=business_name,
        detail="Drafting email"
    )

    try:
        draft, tokens_used = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": email_skill},
                {"role": "user",   "content": task}
            ],
            max_tokens=400,
            temperature=0.8
        )

        # Update session total so % remaining stays accurate
        # even though footer not shown for drafts
        global session_tokens_used
        session_tokens_used += tokens_used

        print(
            f"✍️  [DRAFT] {contact_name} @ {business_name} "
            f"— {tokens_used} tokens · "
            f"{((session_tokens_used / DAILY_TOKEN_LIMIT) * 100):.1f}% "
            f"of daily limit used"
        )

        # Save draft to memory
        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


# ─────────────────────────────────────────
# PARSE DRAFT
# Splits SUBJECT/BODY response into two strings
# ─────────────────────────────────────────

def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits Riley's raw draft response into
    subject line and email body.
    Falls back gracefully ,if format is unexpected.
    """
    lines      = draft.strip().split("\n")
    subject    = ""
    body_lines = []
    in_body    = False

    for line in lines:
        if line.upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
        elif line.upper().startswith("BODY:"):
            in_body   = True
            remainder = line.split(":", 1)[1].strip()
            if remainder:
                body_lines.append(remainder)
        elif in_body:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # Fallback if parsing fails
    if not subject:
        subject = "Reaching out"
    if not body:
        body = draft.strip()

    return subject, body