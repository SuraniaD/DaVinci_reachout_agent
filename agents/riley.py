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

DAILY_TOKEN_LIMIT   = 500_000
session_tokens_used = 0

# ─────────────────────────────────────────
# HARDCODED LINKS
# Always injected by parse_draft
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
    text_lower = text.lower()
    return any(t in text_lower for t in FEEDBACK_TRIGGERS)


def _extract_preference(
    user_id:  str,
    feedback: str
) -> str | None:
    """
    Uses Groq to extract a reusable preference
    from CEO feedback. Returns rule string or None.
    """
    prompt = f"""
The CEO gave this feedback on an outreach email draft:
"{feedback}"

If this contains a reusable writing rule for ALL future
emails, extract it as one clear sentence.
If not reusable reply: NOT_A_PREFERENCE

Reply ONLY with the rule or NOT_A_PREFERENCE.
Examples:
  Input: "make it shorter, 3 sentences max"
  Output: Keep email body to 3 sentences maximum.

  Input: "don't use bullet points"
  Output: Never use bullet points in email body.

  Input: "approve"
  Output: NOT_A_PREFERENCE

  Input: "the opening is too generic"
  Output: Always open with a specific detail about the prospect's business.
"""
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        if result == "NOT_A_PREFERENCE" or not result:
            return None
        return result

    except Exception as e:
        print(f"⚠️  [PREFERENCES] Extraction failed: {e}")
        return None


def _call_groq_with_retry(
    messages:    list,
    max_tokens:  int,
    temperature: float
) -> tuple[str, int]:
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
            print(f"🔢 [TOKENS] {tokens_used} tokens")
            return content, tokens_used

        except Exception as e:
            if "rate_limit_exceeded" in str(e) \
               and attempt == 0:
                print("⏳ Rate limit — waiting 60s...")
                time.sleep(60)
                continue
            raise e


def _token_footer(tokens_this_call: int) -> str:
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
                print(f"✅ Skill loaded: {filename}")
                return content
            except Exception as e:
                print(f"⚠️  Could not read {path}: {e}")

    print(
        f"❌ {filename} not found — using fallback\n"
        f"   CWD: {os.getcwd()}"
    )

    return f"""
Draft a cold outreach email for DaVinci AI.
DaVinci AI automates business workflows with AI agents.
Max 120 words. Be specific and human.
Two paragraphs separated by a blank line.
End with exactly:
{CTA_LINE}

{SIGNOFF_LINE}

Format:
SUBJECT: <subject line here>
BODY:
<paragraph 1>

<paragraph 2>

{CTA_LINE}

{SIGNOFF_LINE}
"""


# ─────────────────────────────────────────
# GENERAL CHAT
# Core identity prompt only
# Detects feedback and saves as preference
# ─────────────────────────────────────────

def chat_with_riley(
    user_id:      str,
    user_message: str
) -> str:
    """
    General conversation with Riley.
    Detects feedback and saves preferences.
    Token footer appended to Slack reply.
    Raw reply saved to memory without footer.
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

        if _looks_like_feedback(user_message):
            preference = _extract_preference(
                user_id, user_message
            )
            if preference:
                from tools.preferences import save_preference
                save_preference(user_id, preference)
                print(f"🧠 [LEARN] Chat: '{preference}'")

        add_message("riley", user_id, "assistant", reply)
        return reply + _token_footer(tokens_used)

    except Exception as e:
        print(f"❌ Groq chat error: {e}")
        return f"Sorry, hit an error: {e}."


# ─────────────────────────────────────────
# DRAFT OUTREACH EMAIL
# Loads email_template.txt
# Injects preferences at top of prompt
# No token footer — draft goes to email
# ─────────────────────────────────────────

def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """
    Drafts a personalised outreach email.
    Preferences injected at top so they override
    the template defaults.
    """
    email_skill = _load_skill("email_template.txt")

    from tools.preferences import build_preferences_block
    prefs_block = build_preferences_block(user_id)

    if prefs_block:
        email_skill = prefs_block + "\n\n" + email_skill
        print(f"🧠 [DRAFT] Preferences injected")
    else:
        print("🧠 [DRAFT] No preferences saved yet")

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

        global session_tokens_used
        session_tokens_used += tokens_used

        pct = (
            session_tokens_used / DAILY_TOKEN_LIMIT
        ) * 100
        print(
            f"✍️  [DRAFT] {contact_name} @ "
            f"{business_name} — "
            f"{tokens_used} tokens · {pct:.1f}% used"
        )

        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


# ─────────────────────────────────────────
# DRAFT WITH FEEDBACK
# Called when CEO requests a redraft
# Saves feedback as preference FIRST
# Then redrafts using email_template + prefs
# Returns (new_draft, learned_preference_or_None)
# ─────────────────────────────────────────

def draft_with_feedback(
    user_id:        str,
    feedback:       str,
    original_draft: str,
    contact_name:   str,
    business_name:  str
) -> tuple[str, str | None]:
    """
    Redrafts an email incorporating CEO feedback.

    Learning flow:
    1. Extract reusable preference from feedback
    2. Save to riley_preferences in Supabase
    3. Reload preferences (now includes new rule)
    4. Redraft using email_template + all preferences

    This means feedback improves THIS draft AND
    all future drafts — not just the current one.

    Returns (new_draft, learned_preference_or_None).
    """
    # Step 1 — Extract and save preference FIRST
    # so it's included in the redraft prompt below
    learned = None
    if _looks_like_feedback(feedback):
        preference = _extract_preference(
            user_id, feedback
        )
        if preference:
            from tools.preferences import save_preference
            save_preference(user_id, preference)
            learned = preference
            print(
                f"🧠 [LEARN] From redraft: '{preference}'"
            )

    # Step 2 — Load skill + updated preferences
    # (now includes the just-saved rule)
    email_skill = _load_skill("email_template.txt")
    from tools.preferences import build_preferences_block
    prefs_block = build_preferences_block(user_id)
    if prefs_block:
        email_skill = prefs_block + "\n\n" + email_skill
        print(
            f"🧠 [REDRAFT] Preferences injected "
            f"({'includes new rule' if learned else 'existing'})"
        )

    # Step 3 — Redraft with feedback + updated prefs
    task = f"""CEO feedback on this draft: "{feedback}"

Original draft:
{original_draft}

Redraft the email applying this feedback exactly.
Keep SUBJECT then BODY format.
Two paragraphs separated by a blank line."""

    try:
        new_draft, tokens_used = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": email_skill},
                {"role": "user",   "content": task}
            ],
            max_tokens=400,
            temperature=0.7
        )

        global session_tokens_used
        session_tokens_used += tokens_used

        pct = (
            session_tokens_used / DAILY_TOKEN_LIMIT
        ) * 100
        print(
            f"✍️  [REDRAFT] {contact_name} @ "
            f"{business_name} — "
            f"{tokens_used} tokens · {pct:.1f}% used"
        )

        add_message(
            "riley", user_id, "assistant", new_draft
        )
        return new_draft, learned

    except Exception as e:
        print(f"❌ [RILEY] Redraft error: {e}")
        raise Exception(f"Could not redraft: {e}")


# ─────────────────────────────────────────
# PARSE DRAFT
# Splits SUBJECT/BODY into two strings
# Strips ALL CTA and sign-off variants
# Re-attaches correct HTML links exactly once
# ─────────────────────────────────────────

def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits Riley's raw draft into subject and body.
    Strips whatever CTA/sign-off the model wrote.
    Re-attaches correct HTML links exactly once.
    """
    divider = "─────────────────────"
    if divider in draft:
        draft = draft[:draft.index(divider)].strip()

    lines      = draft.strip().split("\n")
    subject    = ""
    body_lines = []
    in_body    = False

    for line in lines:
        stripped = line.strip()

        if stripped.upper().startswith("SUBJECT:") \
           and not subject:
            subject = stripped.split(":", 1)[1].strip()
            continue

        if stripped.upper().startswith("BODY:"):
            in_body   = True
            remainder = stripped.split(":", 1)[1].strip()
            if remainder:
                body_lines.append(remainder)
            continue

        if in_body:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    if not subject:
        subject = "Reaching out"
    if not body:
        body = draft.strip()

    # ── STRIP CTA AND SIGN-OFF ────────────
    cta_phrases = [
        "15-minute call",
        "15 minute call",
        "worth a quick",
        "quick call",
        "schedule a call",
        "book a call",
        "hop on a call",
        "discovery call",
        "cal.com",
        "overlayCalendar",
    ]

    signoff_phrases = [
        "riley, davinci",
        "riley,davinci",
        "riley, <a",
        "riley,<a",
        "davinciai.agency",
    ]

    cleaned_lines = []
    for line in body.split("\n"):
        line_lower    = line.lower().strip()
        line_stripped = line.strip()

        if any(p in line_lower for p in cta_phrases):
            print(
                f"🧹 [PARSE] CTA stripped: "
                f"'{line_stripped[:60]}'"
            )
            continue

        if any(p in line_lower for p in signoff_phrases):
            print(
                f"🧹 [PARSE] Sign-off stripped: "
                f"'{line_stripped[:60]}'"
            )
            continue

        if line_lower in [
            "riley,", "riley", "riley, ",
            "riley, davinci ai",
            "riley, davinci ai.",
            "riley,davinci ai",
            "- riley",
            "— riley",
        ]:
            print(
                f"🧹 [PARSE] Bare Riley stripped: "
                f"'{line_stripped}'"
            )
            continue

        cleaned_lines.append(line)

    body = "\n".join(cleaned_lines).strip()
    body = body.rstrip(",. \n")

    # Re-attach correct HTML links exactly once
    body = (
        f"{body}\n\n"
        f"{CTA_LINE}\n\n"
        f"{SIGNOFF_LINE}"
    )

    return subject, body