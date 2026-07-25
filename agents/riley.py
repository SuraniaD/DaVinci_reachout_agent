import os
import time
from groq import Groq
from dotenv import load_dotenv
from memory import get_history, add_message
from interaction_log import log_action

load_dotenv()

client = Groq(api_key=os.environ.get("GROQ_API_KEY_RILEY"))

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
- Example: "Got it — keeping it under 80 words from now on.
  I've saved that preference so all future drafts follow it."

COMMANDS:
- !reset          → confirm memory cleared
- !status         → summarise recent outreach activity
- !automode on    → confirm auto-send is on
- !automode off   → confirm approval mode is on
- !showprefs      → list all saved preferences
- !resetprefs     → clear all saved preferences
- !resetrun       → cancel current outreach run
"""

DAILY_TOKEN_LIMIT   = 500_000
session_tokens_used = 0

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
    Uses Groq to extract a clean reusable preference
    from the CEO's feedback message.
    """
    prompt = f"""
The CEO gave this feedback on an outreach email draft:
"{feedback}"

If this feedback contains a reusable writing rule
(something Riley should apply to ALL future emails),
extract it as a short clear rule in 1 sentence.

If it is not a reusable rule reply with: NOT_A_PREFERENCE

Reply with ONLY the rule or NOT_A_PREFERENCE.
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


def _call_groq_with_retry(
    messages:    list,
    max_tokens:  int,
    temperature: float
) -> tuple[str, int]:
    """
    Calls Groq with one automatic retry on rate limit.
    Returns (reply_text, tokens_used).
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
            print(f"🔢 [TOKENS] {tokens_used} tokens this call")
            return content, tokens_used

        except Exception as e:
            if "rate_limit_exceeded" in str(e) and attempt == 0:
                print("⏳ Groq rate limit — waiting 60s...")
                time.sleep(60)
                continue
            raise e


def _token_footer(tokens_this_call: int) -> str:
    """
    Builds token usage line for Slack replies only.
    Never goes into emails.
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
        f"\n\n"
        f"─────────────────────\n"
        f"{indicator} `{bar}` "
        f"{pct_used:.1f}% used · "
        f"{pct_remaining:.1f}% remaining today"
    )


def _load_skill(filename: str) -> str:
    """
    Loads a skill file from the project root.
    Only called when that specific task runs.
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
        print(f"⚠️  {filename} not found — using fallback")
        return """
Draft a cold outreach email for DaVinci AI.
Sign off: Riley, DaVinci AI. Max 120 words.
Format: SUBJECT: <subject>\nBODY:\n<body>
"""


def chat_with_riley(user_id: str, user_message: str) -> str:
    """
    General conversation with Riley.
    Detects feedback and saves preferences automatically.
    Appends token footer to Slack replies only.
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
        return f"Sorry, hit an error: {e}."


def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """
    Drafts a personalised outreach email.
    Loads email_template.txt as system prompt.
    Injects learned preferences at top of prompt.
    No token footer added — draft goes into email.
    """
    email_skill = _load_skill("email_template.txt")

    # Inject preferences so Riley applies them
    from tools.preferences import build_preferences_block
    prefs_block = build_preferences_block(user_id)

    if prefs_block:
        email_skill = prefs_block + "\n\n" + email_skill
        print(f"🧠 [DRAFT] Injecting preferences")
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

        # Update session total for accurate % display
        # but do NOT add footer to the draft itself
        global session_tokens_used
        session_tokens_used += tokens_used

        pct = (session_tokens_used / DAILY_TOKEN_LIMIT) * 100
        print(
            f"✍️  [DRAFT] {contact_name} @ {business_name} "
            f"— {tokens_used} tokens · {pct:.1f}% used"
        )

        # Save raw draft to memory — no footer
        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits SUBJECT/BODY into two strings.
    Strips token footer before parsing so it never
    leaks into subject line or email body.
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

    if not subject:
        subject = "Reaching out"
    if not body:
        body = draft.strip()

    return subject, body