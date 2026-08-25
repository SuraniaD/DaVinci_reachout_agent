"""
Riley — Outreach Agent
Phase B orchestration via Slack DM.

Email drafting now includes verification gate
(x1 × x2 ≥ 0.81) before any send.
"""

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

Every email you draft is verified before sending:
x1 = accuracy vs stored research (target ≥ 0.9)
x2 = accuracy vs fresh online info (target ≥ 0.9)
combined = x1 × x2 (must reach 0.81 to send)

BEHAVIOUR:
- Short and direct — this is Slack, not email
- Ask one question when instructions are vague
- Have opinions and share them

COMMANDS:
- !run <region>       → start verified outreach
- !run <region> draft_ready → retry skipped
- !pipeline           → pipeline summary
- !segments           → geographic breakdown
- !analytics          → campaign stats (30 days)
- !review             → human review queue
- !approve-review <n> → send draft as-is
- !redraft-review <n> → redraft + re-verify
- !discard-review <n> → skip and move on
- !automode on/off    → toggle auto-send
- !showprefs          → list writing preferences
- !resetprefs         → clear preferences
- !resetrun           → cancel current run
- STOP                → pause current campaign
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
    return any(t in text.lower() for t in FEEDBACK_TRIGGERS)


def _extract_preference(
    user_id: str, feedback: str
) -> str | None:
    prompt = f"""
The CEO gave feedback on an outreach email:
"{feedback}"

If this is a reusable writing rule for ALL future emails,
extract it as one clear sentence.
If not reusable: NOT_A_PREFERENCE

Reply ONLY with the rule or NOT_A_PREFERENCE.
"""
    try:
        response = client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80, temperature=0.1
        )
        result = response.choices[0].message.content.strip()
        return None if result == "NOT_A_PREFERENCE" else result
    except Exception as e:
        print(f"⚠️  [PREFERENCES] Extraction: {e}")
        return None


def _call_groq_with_retry(
    messages: list, max_tokens: int, temperature: float
) -> tuple[str, int]:
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=CHAT_MODEL, messages=messages,
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


def _token_footer(tokens: int) -> str:
    global session_tokens_used
    session_tokens_used += tokens

    pct = min(session_tokens_used / DAILY_TOKEN_LIMIT * 100, 100)
    rem = max(100 - pct, 0)
    ind = "🟢" if rem > 20 else "🟡" if rem > 6 else "🔴"
    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))

    return (
        f"\n\n─────────────────────\n"
        f"{ind} `{bar}` "
        f"{pct:.1f}% used · {rem:.1f}% remaining today"
    )


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
            except Exception as e:
                print(f"⚠️  Could not read {path}: {e}")

    return f"""
Draft a cold outreach email for DaVinci AI.
2 paragraphs. Max 100 words.
Paragraph 1: specific hook about the business.
Paragraph 2: what DaVinci AI does and why relevant.

Format exactly:
SUBJECT: <subject line>

BODY:
<paragraph 1>

<paragraph 2>
"""


# ─────────────────────────────────────────
# GENERAL CHAT
# ─────────────────────────────────────────

def chat_with_riley(user_id: str, user_message: str) -> str:
    history = get_history("riley", user_id)
    add_message("riley", user_id, "user", user_message)

    try:
        reply, tokens = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + history + [
                {"role": "user", "content": user_message}
            ],
            max_tokens=500,
            temperature=0.7
        )

        if _looks_like_feedback(user_message):
            pref = _extract_preference(user_id, user_message)
            if pref:
                from tools.preferences import save_preference
                save_preference(user_id, pref)
                print(f"🧠 [LEARN] Chat: '{pref}'")

        add_message("riley", user_id, "assistant", reply)
        return reply + _token_footer(tokens)

    except Exception as e:
        print(f"❌ Groq chat error: {e}")
        return f"Sorry, hit an error: {e}."


# ─────────────────────────────────────────
# DRAFT EMAIL
# ─────────────────────────────────────────

def draft_outreach_email(
    user_id: str, contact_name: str,
    business_name: str, research: str
) -> str:
    """
    Drafts a personalised outreach email.
    Does NOT verify — verification happens in
    flows/reachout_flow.py after drafting.
    """
    email_skill = _load_skill("email_template.txt")

    from tools.preferences import build_preferences_block
    prefs = build_preferences_block(user_id)
    if prefs:
        email_skill = prefs + "\n\n" + email_skill

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
        draft, tokens = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": email_skill},
                {"role": "user",   "content": task}
            ],
            max_tokens=600,
            temperature=0.8
        )

        global session_tokens_used
        session_tokens_used += tokens

        print(
            f"📝 [DRAFT RAW] '{business_name}':\n"
            f"{'─'*40}\n{draft}\n{'─'*40}"
        )

        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq draft error: {e}")
        raise Exception(f"Could not draft email: {e}")


# ─────────────────────────────────────────
# DRAFT WITH FEEDBACK (redraft)
# ─────────────────────────────────────────

def draft_with_feedback(
    user_id: str, feedback: str,
    original_draft: str, contact_name: str,
    business_name: str
) -> tuple[str, str | None]:
    """
    Redrafts incorporating CEO feedback or
    verification failure feedback.
    Saves preference first if feedback is CEO input.
    """
    learned = None
    if _looks_like_feedback(feedback):
        pref = _extract_preference(user_id, feedback)
        if pref:
            from tools.preferences import save_preference
            save_preference(user_id, pref)
            learned = pref

    email_skill = _load_skill("email_template.txt")
    from tools.preferences import build_preferences_block
    prefs = build_preferences_block(user_id)
    if prefs:
        email_skill = prefs + "\n\n" + email_skill

    task = f"""Feedback on this draft: "{feedback}"

Original draft:
{original_draft}

Rewrite applying this feedback exactly.
SUBJECT: on first line, then BODY: on its own line,
then two paragraphs."""

    try:
        new_draft, tokens = _call_groq_with_retry(
            messages=[
                {"role": "system", "content": email_skill},
                {"role": "user",   "content": task}
            ],
            max_tokens=600,
            temperature=0.7
        )

        global session_tokens_used
        session_tokens_used += tokens

        add_message("riley", user_id, "assistant", new_draft)
        return new_draft, learned

    except Exception as e:
        print(f"❌ [RILEY] Redraft error: {e}")
        raise Exception(f"Could not redraft: {e}")


# ─────────────────────────────────────────
# PARSE DRAFT
# ─────────────────────────────────────────

def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits raw draft into (subject, body).
    Three-level fallback for model format variations.
    Strips CTA/signoff and re-attaches correct HTML.
    """
    divider = "─────────────────────"
    if divider in draft:
        draft = draft[:draft.index(divider)].strip()

    lines            = draft.strip().split("\n")
    subject          = ""
    body_lines       = []
    in_body          = False
    subject_line_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if re.match(r'^subject\s*:', stripped, re.I) \
           and not subject:
            subject          = re.split(
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

    # Fallback 1: no BODY marker
    if not body_lines and subject_line_idx is not None:
        for line in lines[subject_line_idx + 1:]:
            body_lines.append(line)

    # Fallback 2: no markers at all
    if not body_lines and not subject:
        body_lines = lines

    body = "\n".join(body_lines).strip()

    if not subject:
        subject = "Reaching out from DaVinci AI"
    if not body:
        body = draft.strip()

    # Strip standalone CTA/signoff lines
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

    cleaned = []
    for line in body.split("\n"):
        stripped = line.strip()
        lower    = stripped.lower()

        if any(re.match(p, lower) for p in cta_patterns) \
           or "overlayCalendar" in line:
            print(f"🧹 [PARSE] CTA: '{stripped[:50]}'")
            continue

        if any(
            re.search(p, lower, re.I)
            for p in signoff_patterns
        ):
            print(f"🧹 [PARSE] Signoff: '{stripped[:50]}'")
            continue

        cleaned.append(line)

    body = "\n".join(cleaned).strip().rstrip(",. \n")

    print(
        f"📝 [PARSE] subject='{subject}' "
        f"body={len(body)} chars"
    )

    body = f"{body}\n\n{CTA_LINE}\n\n{SIGNOFF_LINE}"
    return subject, body