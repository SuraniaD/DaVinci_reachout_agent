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

CHAT_MODEL = "openai/gpt-oss-20b"

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
- !reset            → confirm memory cleared
- !status           → summarise recent outreach activity
- !automode on/off  → toggle auto-send mode
- !showprefs        → list all saved preferences
- !resetprefs       → clear all saved preferences
- !resetrun         → cancel current outreach run
- !run              → start outreach from DB prospects
- !run <segment>    → run outreach for one segment only
- !pipeline         → show prospect pipeline summary
- !segments         → show all segments with stats
- STOP              → stop current campaign after this draft
"""

DAILY_TOKEN_LIMIT   = 500_000
session_tokens_used = 0

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
  Output: Always open with a specific detail about
  the prospect's business.
"""
    try:
        response = client.chat.completions.create(
            model=CHAT_MODEL,
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
                model=CHAT_MODEL,
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
You are Riley at DaVinci AI.
Write a short cold outreach email (2 paragraphs, max 100 words).
Paragraph 1: specific hook about the prospect's business.
Paragraph 2: what DaVinci AI does and why it's relevant.

Output format — follow exactly:
SUBJECT: <subject line>

BODY:
<paragraph 1>

<paragraph 2>
"""


def chat_with_riley(
    user_id:      str,
    user_message: str
) -> str:
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


def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
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
{research[:800]}

Write the email now. Follow the output format exactly.
Start with SUBJECT: on the first line."""

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
            max_tokens=600,
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
        print(
            f"📝 [DRAFT RAW] '{business_name}':\n"
            f"{'─'*40}\n{draft}\n{'─'*40}"
        )

        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


def draft_with_feedback(
    user_id:        str,
    feedback:       str,
    original_draft: str,
    contact_name:   str,
    business_name:  str
) -> tuple[str, str | None]:
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

    email_skill = _load_skill("email_template.txt")
    from tools.preferences import build_preferences_block
    prefs_block = build_preferences_block(user_id)
    if prefs_block:
        email_skill = prefs_block + "\n\n" + email_skill

    task = f"""CEO feedback on this draft: "{feedback}"

Original draft:
{original_draft}

Rewrite the email applying this feedback exactly.
Follow the output format — start with SUBJECT: on the
first line, then a blank line, then BODY: on its own line,
then the email body in two paragraphs."""

    try:
        new_draft, tokens_used = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": email_skill},
                {"role": "user",   "content": task}
            ],
            max_tokens=600,
            temperature=0.7
        )

        global session_tokens_used
        session_tokens_used += tokens_used

        add_message(
            "riley", user_id, "assistant", new_draft
        )
        return new_draft, learned

    except Exception as e:
        print(f"❌ [RILEY] Redraft error: {e}")
        raise Exception(f"Could not redraft: {e}")


def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits raw draft into subject and body.

    Three-level fallback:
    1. SUBJECT: + BODY: markers (standard)
    2. SUBJECT: found but no BODY: — use remaining lines
    3. No markers — use full draft as body

    Strips standalone CTA/signoff lines only.
    Re-attaches correct HTML links exactly once.
    """
    # Strip token footer
    divider = "─────────────────────"
    if divider in draft:
        draft = draft[:draft.index(divider)].strip()

    lines   = draft.strip().split("\n")
    subject = ""
    body_lines = []
    in_body    = False
    subject_line_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if re.match(r'^subject\s*:', stripped, re.I) \
           and not subject:
            subject = re.split(
                r'subject\s*:', stripped, flags=re.I
            )[1].strip().strip('"\'')
            subject_line_idx = i
            continue

        if re.match(r'^body\s*:', stripped, re.I):
            in_body   = True
            remainder = re.split(
                r'body\s*:', stripped, flags=re.I
            )[1].strip()
            if remainder:
                body_lines.append(remainder)
            continue

        if in_body:
            body_lines.append(line)

    # ── FALLBACK 1: SUBJECT found, no BODY: ──
    if not body_lines and subject_line_idx is not None:
        print(
            "⚠️  [PARSE] No BODY: marker — "
            "using lines after SUBJECT"
        )
        for line in lines[subject_line_idx + 1:]:
            body_lines.append(line)

    # ── FALLBACK 2: no markers at all ────────
    if not body_lines and not subject:
        print(
            "⚠️  [PARSE] No markers found — "
            "using full draft as body"
        )
        body_lines = lines

    body = "\n".join(body_lines).strip()

    if not subject:
        subject = "Reaching out from DaVinci AI"
    if not body:
        body = draft.strip()

    # ── STRIP STANDALONE CTA / SIGNOFF LINES ─
    # Only strip lines that are ENTIRELY a CTA
    # or signoff — not lines that contain a call
    # reference mid-paragraph

    cta_patterns = [
        r'^worth a quick.*?call\??\.?$',
        r'^would you be open to a.*?call\??\.?$',
        r'^(can we|shall we|let\'s) (hop|jump|get) on',
        r'^schedule a call',
        r'^book a call',
        r'^happy to (jump|hop) on',
        r'^15.minute call',
        r'^cal\.com',
        r'overlayCalendar',
    ]

    signoff_patterns = [
        r'^riley,?\s*davinci\s*ai\.?$',
        r'^riley,?\s*$',
        r'^- riley$',
        r'^— riley$',
        r'^riley,?\s*https?://\S+$',
        r'davinciai\.agency',
        r'^riley,?\s*<a\s',
    ]

    cleaned_lines = []
    for line in body.split("\n"):
        stripped   = line.strip()
        lower      = stripped.lower()

        is_cta = any(
            re.match(p, lower)
            for p in cta_patterns
        ) or "overlayCalendar" in line

        is_signoff = any(
            re.search(p, lower, re.I)
            for p in signoff_patterns
        )

        if is_cta:
            print(
                f"🧹 [PARSE] CTA stripped: "
                f"'{stripped[:60]}'"
            )
            continue

        if is_signoff:
            print(
                f"🧹 [PARSE] Signoff stripped: "
                f"'{stripped[:60]}'"
            )
            continue

        cleaned_lines.append(line)

    body = "\n".join(cleaned_lines).strip()
    body = body.rstrip(",. \n")

    print(
        f"📝 [PARSE] subject='{subject}' "
        f"body_chars={len(body)} "
        f"preview='{body[:80]}'"
    )

    # Re-attach correct HTML links exactly once
    body = (
        f"{body}\n\n"
        f"{CTA_LINE}\n\n"
        f"{SIGNOFF_LINE}"
    )

    return subject, body