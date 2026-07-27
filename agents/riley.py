import os
import re
import time
from groq import Groq
from dotenv import load_dotenv
from memory import get_history, add_message
from interaction_log import log_action

load_dotenv()

client = Groq(
    api_key=os.environ.get("GROQ_API_KEY_RILEY")
)

# ─────────────────────────────────────────
# RILEY'S CORE IDENTITY — ~80 tokens
# Chat rules only
# Email rules live in email_template.txt
# Each skill loaded only when that task runs
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

WHEN THE CEO GIVES FEEDBACK ON AN EMAIL DRAFT:
- Always acknowledge what you will change
- Confirm you have saved it as a preference for future drafts

COMMANDS:
- !reset          → confirm memory cleared
- !status         → summarise recent outreach activity
- !automode on    → confirm auto-send is on
- !automode off   → confirm approval mode is on
- !showprefs      → list all saved preferences
- !resetprefs     → clear all saved preferences
- !resetrun       → cancel current outreach run
- !run            → start outreach from DB prospects
- !pipeline       → show prospect pipeline
"""

# ─────────────────────────────────────────
# DAILY TOKEN LIMITS
# ─────────────────────────────────────────

DAILY_TOKEN_LIMIT   = 500_000
session_tokens_used = 0

# ─────────────────────────────────────────
# HARDCODED LINKS
# Always injected into emails by parse_draft
# Never left to the model's discretion
# ─────────────────────────────────────────

BOOKING_LINK = (
    "https://cal.com/deepanshu-surania/"
    "discovery-call?overlayCalendar=true"
)
WEBSITE_LINK = "https://davinciai.agency"

CTA_LINE = (
    f'Worth a quick <a href="{BOOKING_LINK}">'
    f"15-minute call</a>?"
)
SIGNOFF_LINE = (
    f'Riley, <a href="{WEBSITE_LINK}">DaVinci AI</a>'
)

# ─────────────────────────────────────────
# FEEDBACK TRIGGERS
# Used to detect when CEO is giving style
# feedback on a draft — saved as preference
# ─────────────────────────────────────────

FEEDBACK_TRIGGERS = [
    "don't", "dont", "stop", "never",
    "always", "make it", "keep it",
    "shorter", "longer", "simpler",
    "more", "less", "no more", "avoid",
    "instead", "from now on", "going forward",
    "next time", "in future", "every time",
    "too", "change", "remove", "add",
    "without", "use", "write"
]


def _looks_like_feedback(text: str) -> bool:
    """Returns True if message looks like email style feedback."""
    text_lower = text.lower()
    return any(t in text_lower for t in FEEDBACK_TRIGGERS)


def _extract_preference(
    user_id:  str,
    feedback: str
) -> str | None:
    """
    Uses Groq to extract a clean reusable preference
    from the CEO's feedback message.
    Returns a short rule or None if not reusable feedback.
    """
    prompt = f"""
The CEO gave this feedback on an outreach email draft:
"{feedback}"

If this contains a reusable writing rule for ALL future
emails, extract it as one clear sentence.
If not reusable, reply: NOT_A_PREFERENCE

Reply ONLY with the rule or NOT_A_PREFERENCE.
Examples:
  Input: "make it shorter, 3 sentences max"
  Output: Keep email body to 3 sentences maximum.

  Input: "don't use bullet points"
  Output: Never use bullet points in email body.

  Input: "approve"
  Output: NOT_A_PREFERENCE
"""
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
            temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        if result == "NOT_A_PREFERENCE" or not result:
            return None
        return result

    except Exception as e:
        print(f"⚠️  [PREFERENCES] Extraction failed: {e}")
        return None


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
                f"🔢 [TOKENS] {tokens_used} tokens this call"
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
# Appended to Slack chat replies only
# Never added to email drafts
# ─────────────────────────────────────────

def _token_footer(tokens_this_call: int) -> str:
    """
    Builds token usage percentage bar for Slack replies.
    Shows % used and remaining of daily limit.
    Session total resets on Railway restart.
    """
    global session_tokens_used
    session_tokens_used += tokens_this_call

    pct_used      = min(
        (session_tokens_used / DAILY_TOKEN_LIMIT) * 100,
        100
    )
    pct_remaining = max(100 - pct_used, 0)

    if pct_remaining > 20:
        indicator = "🟢"
    elif pct_remaining > 6:
        indicator = "🟡"
    else:
        indicator = "🔴"

    filled = int(pct_used / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    return (
        f"\n\n─────────────────────\n"
        f"{indicator} `{bar}` "
        f"{pct_used:.1f}% used · "
        f"{pct_remaining:.1f}% remaining today"
    )


# ─────────────────────────────────────────
# SKILL LOADER
# Reads any .txt skill file from project root
# Only called when that specific task runs
# ─────────────────────────────────────────

def _load_skill(filename: str) -> str:
    """
    Loads a skill file from the project root.
    Tries multiple path strategies for Railway compatibility.

    Current skills:
      email_template.txt     → email drafting rules
      (future) strategy_template.txt → targeting advice
      (future) followup_template.txt → follow-up emails
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
                print(f"✅ Skill loaded: {filename}")
                return content
            except Exception as e:
                print(
                    f"⚠️  Could not read {path}: {e}"
                )

    print(
        f"❌ {filename} not found — using fallback\n"
        f"   CWD: {os.getcwd()}\n"
        f"   Files: {os.listdir(os.getcwd())}"
    )

    # Fallback includes hardcoded links so
    # they always appear even without the skill file
    return f"""
Draft a cold outreach email for DaVinci AI.
DaVinci AI automates business workflows with AI agents.
Max 120 words. Be specific and human.
End with exactly:
{CTA_LINE}

{SIGNOFF_LINE}

Format:
SUBJECT: <subject>
BODY:
<body>
"""


# ─────────────────────────────────────────
# GENERAL CHAT
# Uses core identity prompt — 8B model
# No email rules, no skill files
# Token footer appended to every reply
# ─────────────────────────────────────────

def chat_with_riley(user_id: str, user_message: str) -> str:
    """
    General conversation with Riley.
    Only RILEY_SYSTEM_PROMPT sent as system message.
    Detects feedback and saves as preference automatically.
    Token footer appended to every Slack reply.
    Raw reply (without footer) saved to memory.
    """
    history = get_history("riley", user_id)
    add_message("riley", user_id, "user", user_message)

    messages = history + [
        {"role": "user", "content": user_message}
    ]

    try:
        reply, tokens_used = _call_groq_with_retry(
            messages=[
                {
                    "role":    "system",
                    "content": RILEY_SYSTEM_PROMPT
                }
            ] + messages,
            max_tokens=500,
            temperature=0.7
        )

        # Detect feedback and save as preference
        if _looks_like_feedback(user_message):
            preference = _extract_preference(
                user_id, user_message
            )
            if preference:
                from tools.preferences import save_preference
                save_preference(user_id, preference)
                print(
                    f"🧠 [LEARN] Saved: '{preference}'"
                )

        # Save raw reply to memory WITHOUT footer
        # so footer never pollutes future history
        add_message("riley", user_id, "assistant", reply)

        # Return reply WITH footer for Slack display
        return reply + _token_footer(tokens_used)

    except Exception as e:
        print(f"❌ Groq chat error: {e}")
        return (
            f"Sorry, hit an error: {e}. "
            f"Try again in a moment."
        )


# ─────────────────────────────────────────
# DRAFT OUTREACH EMAIL
# Loads email_template.txt — only here
# No conversation history sent — not needed
# Research capped at 800 chars
# Preferences injected at top of prompt
# Token footer NOT added — draft goes to email
# ─────────────────────────────────────────

def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """
    Drafts a personalised outreach email.

    What gets sent to Groq:
      system → email_template.txt + preferences (~280-350t)
      user   → contact details + research (capped 800 chars)

    What does NOT get sent:
      - Conversation history (irrelevant for drafting)
      - RILEY_SYSTEM_PROMPT (chat rules irrelevant here)
      - Token footer (never goes in emails)
    """
    email_skill = _load_skill("email_template.txt")

    # Inject CEO preferences at top of prompt
    # so they override default template rules
    from tools.preferences import build_preferences_block
    prefs_block = build_preferences_block(user_id)

    if prefs_block:
        email_skill = prefs_block + "\n\n" + email_skill
        print(f"🧠 [DRAFT] Preferences injected")
    else:
        print("🧠 [DRAFT] No preferences saved yet")

    # Cap research at 800 chars — ~200 tokens
    # First 800 chars have the most useful facts
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
        # but do NOT add footer to the draft itself
        global session_tokens_used
        session_tokens_used += tokens_used

        pct = (
            session_tokens_used / DAILY_TOKEN_LIMIT
        ) * 100
        print(
            f"✍️  [DRAFT] {contact_name} @ {business_name} "
            f"— {tokens_used} tokens · "
            f"{pct:.1f}% of daily limit used"
        )

        # Save raw draft to memory — no footer
        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


# ─────────────────────────────────────────
# PARSE DRAFT
# Splits SUBJECT/BODY into two strings
# Strips token footer before parsing
# ENFORCES correct CTA and sign-off links
# regardless of what the model wrote
# ─────────────────────────────────────────

def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits Riley's raw draft response into
    subject line and email body.

    Critical: strips whatever CTA/sign-off the model
    wrote and re-attaches the correct HTML versions.
    This guarantees hyperlinks are always present
    even if the model outputs plain text.
    """
    # Strip token footer if present
    divider = "─────────────────────"
    if divider in draft:
        draft = draft[:draft.index(divider)].strip()

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

    # ── ENFORCE CORRECT LINKS ─────────────
    # Strip any version of CTA the model wrote
    # (plain text, wrong link, or paraphrased)
    body = re.sub(
        r'\n*Worth a quick.*?(?:call|minutes?)\??\.?\s*$',
        '',
        body,
        flags=re.IGNORECASE | re.DOTALL
    ).strip()

    # Strip any version of sign-off the model wrote
    body = re.sub(
        r'\n*Riley,?\s*(?:DaVinci AI|davinciai\.agency'
        r'|<a[^>]*>.*?</a>)?\s*$',
        '',
        body,
        flags=re.IGNORECASE | re.DOTALL
    ).strip()

    # Also strip loose trailing punctuation
    # left after sign-off removal
    body = body.rstrip(",. \n")

    # Re-attach correct HTML links
    # These are the only versions that ever go in emails
    body = (
        f"{body}\n\n"
        f"{CTA_LINE}\n\n"
        f"{SIGNOFF_LINE}"
    )

    return subject, body