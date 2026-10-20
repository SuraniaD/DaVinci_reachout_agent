"""
tools/reviewer.py — Rex, the automated email reviewer.

Checks every verified draft against 10 deterministic rules.
No LLM calls — pure Python string analysis.

Returns ReviewResult:
  passed=True  → auto-send immediately
  passed=False → flag to #davinci-outreach for CEO review
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────

MAX_SUBJECT_WORDS = 8
MAX_BODY_WORDS    = 120
EXACT_PARAGRAPHS  = 2

BOOKING_LINK = "cal.com/deepanshu-surania"
WEBSITE_LINK = "davinciai.agency"

# Verification score thresholds
X1_THRESHOLD       = 0.50
X2_THRESHOLD       = 0.35
COMBINED_THRESHOLD = 0.20

FORBIDDEN_WORDS = [
    "synergy", "leverage", "circle back", "touch base",
    "game-changing", "game changer", "revolutionary",
    "cutting-edge", "cutting edge", "streamline",
    "unlock", "empower", "robust", "scalable",
    "exciting opportunity", "innovative", "disruptive",
    "seamlessly", "seamless", "best-in-class",
    "world-class", "state-of-the-art", "value-add",
    "impactful", "holistic", "paradigm", "ecosystem",
    "synergize", "utilise", "utilize",
    "proactive", "mission-critical", "transformative",
    "thought leader", "thought leadership",
    "dynamic", "growth hacking", "viral",
    "next level", "next-level", "take it to the next",
    "on the same page", "move the needle",
    "low-hanging fruit", "low hanging fruit",
    "boil the ocean", "deep dive", "reach out",
    "i wanted to reach out",
    "hope this finds you well",
    "hope you are well",
    "hope you're well",
    "hope this email",
    "please do not hesitate",
    "do not hesitate to",
    "feel free to",
]

GENERIC_OPENERS = [
    "i hope this email finds you well",
    "i hope this message finds you",
    "i wanted to reach out",
    "i am reaching out",
    "i'm reaching out",
    "my name is",
    "i came across your",
    "i recently came across",
    "i was browsing",
    "i was looking at",
    "allow me to introduce",
    "as a fellow",
]

BULLET_PATTERNS = [
    r'^\s*[\•\-\*\–\—]\s+\S',   # bullet char
    r'^\s*\d+[\.\)]\s+\S',       # numbered list
]


# ─────────────────────────────────────────
# RESULT DATACLASS
# ─────────────────────────────────────────

@dataclass
class ReviewResult:
    passed:          bool
    failures:        list[str] = field(default_factory=list)
    warnings:        list[str] = field(default_factory=list)
    word_count:      int       = 0
    paragraph_count: int       = 0


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&amp;', '&') \
               .replace('&lt;', '<') \
               .replace('&gt;', '>') \
               .replace('&nbsp;', ' ')
    return text


def _count_words(text: str) -> int:
    clean = _strip_html(text)
    return len(clean.split())


def _get_paragraphs(body: str) -> list[str]:
    """
    Split body into content paragraphs.
    Excludes the CTA line and signoff line appended by parse_draft().
    """
    clean  = _strip_html(body)
    chunks = [p.strip() for p in re.split(r'\n\s*\n|\r\n\s*\r\n', clean)]
    paras  = []
    for chunk in chunks:
        if not chunk:
            continue
        lower = chunk.lower()
        if BOOKING_LINK in lower:
            continue
        if WEBSITE_LINK in lower:
            continue
        # Skip CTA-shaped lines
        if re.match(
            r'^(worth|would you|can we|shall we|'
            r'let\'s|schedule|book|happy|15.min)',
            lower
        ):
            continue
        # Skip Riley signoff
        if re.match(r'^riley,?\s*', lower, re.I):
            continue
        paras.append(chunk)
    return paras


def _has_bullet_in_body(body: str) -> bool:
    clean = _strip_html(body)
    for line in clean.split('\n'):
        for pat in BULLET_PATTERNS:
            if re.match(pat, line):
                return True
    return False


def _first_para_has_research_keyword(
    para1: str, research_summary: str
) -> bool:
    """Paragraph 1 should mention at least one keyword from research."""
    if not research_summary:
        return True  # can't check — give benefit of the doubt

    stop = {
        'the', 'a', 'an', 'is', 'it', 'in', 'on', 'at',
        'to', 'for', 'of', 'and', 'or', 'but', 'with',
        'that', 'this', 'we', 'our', 'your', 'you',
        'they', 'their', 'i', 'my', 'me', 'us',
        'are', 'was', 'be', 'been', 'have', 'has',
        'by', 'as', 'from', 'up', 'about',
    }

    def keywords(text: str) -> set[str]:
        words = re.findall(r'[a-z]{4,}', text.lower())
        return {w for w in words if w not in stop}

    kw_research = keywords(research_summary)
    kw_para     = keywords(para1)
    return bool(kw_para & kw_research)


# ─────────────────────────────────────────
# MAIN REVIEW FUNCTION
# ─────────────────────────────────────────

def review_draft(
    subject:          str,
    body:             str,
    research_summary: str   = "",
    x1_score:         float = 1.0,
    x2_score:         float = 1.0,
    combined_score:   float = 1.0,
) -> ReviewResult:
    """
    Runs all 10 deterministic rules against a draft.

    Hard failures → passed=False → flag for CEO review.
    Warnings only → passed=True  → auto-send (noted in channel).
    """
    failures: list[str] = []
    warnings: list[str] = []

    paragraphs  = _get_paragraphs(body)
    para_count  = len(paragraphs)
    para1_text  = paragraphs[0] if paragraphs else ""

    clean_body  = _strip_html(body)
    word_count  = _count_words(clean_body)
    subj_words  = len(subject.split())
    body_lower  = clean_body.lower()
    subj_lower  = subject.lower()

    # ── Rule 1: Subject ≤ 8 words ────────────────
    if subj_words > MAX_SUBJECT_WORDS:
        failures.append(
            f"Subject too long ({subj_words} words, "
            f"max {MAX_SUBJECT_WORDS}): \"{subject}\""
        )

    # ── Rule 2: Body ≤ 120 words ─────────────────
    if word_count > MAX_BODY_WORDS:
        failures.append(
            f"Body too long ({word_count} words, "
            f"max {MAX_BODY_WORDS})"
        )

    # ── Rule 3: Exactly 2 content paragraphs ─────
    if para_count != EXACT_PARAGRAPHS:
        failures.append(
            f"Email must have exactly {EXACT_PARAGRAPHS} "
            f"content paragraphs — found {para_count}"
        )

    # ── Rule 4: CTA booking link present ─────────
    if BOOKING_LINK not in body.lower():
        failures.append(
            f"Missing CTA link ({BOOKING_LINK})"
        )

    # ── Rule 5: Signoff website link present ─────
    if WEBSITE_LINK not in body.lower():
        failures.append(
            f"Missing signoff link ({WEBSITE_LINK})"
        )

    # ── Rule 6: No forbidden words ───────────────
    found_forbidden = []
    for word in FORBIDDEN_WORDS:
        if word in body_lower or word in subj_lower:
            found_forbidden.append(f'"{word}"')
    if found_forbidden:
        failures.append(
            f"Forbidden word(s): "
            f"{', '.join(found_forbidden[:5])}"
            + (f" (+{len(found_forbidden)-5} more)"
               if len(found_forbidden) > 5 else "")
        )

    # ── Rule 7: No bullet points ─────────────────
    if _has_bullet_in_body(body):
        failures.append(
            "Body contains bullet points — prose only"
        )

    # ── Rule 8: No generic opener (WARNING) ──────
    first_line = body_lower.strip().split('\n')[0]
    for opener in GENERIC_OPENERS:
        if opener in first_line:
            warnings.append(
                f"Generic opener: \"{opener}\""
            )
            break

    # ── Rule 9: Para 1 has research keywords (WARNING) ─
    if not _first_para_has_research_keyword(
        para1_text, research_summary
    ):
        warnings.append(
            "Para 1 lacks specific detail from research"
        )

    # ── Rule 10: Verification scores ─────────────
    if x1_score < X1_THRESHOLD:
        failures.append(
            f"x1 score too low "
            f"({x1_score:.2f}, min {X1_THRESHOLD})"
        )
    if x2_score < X2_THRESHOLD:
        failures.append(
            f"x2 score too low "
            f"({x2_score:.2f}, min {X2_THRESHOLD})"
        )
    if combined_score < COMBINED_THRESHOLD:
        failures.append(
            f"Combined score too low "
            f"({combined_score:.3f}, min {COMBINED_THRESHOLD})"
        )

    return ReviewResult(
        passed          = len(failures) == 0,
        failures        = failures,
        warnings        = warnings,
        word_count      = word_count,
        paragraph_count = para_count,
    )


# ─────────────────────────────────────────
# SLACK FORMATTING
# ─────────────────────────────────────────

def format_rex_result_for_slack(
    result:        ReviewResult,
    business_name: str,
    subject:       str,
    body:          str,
    index:         int,
) -> str:
    """
    Formats a Rex result for the #davinci-outreach channel.
    passed  → brief ✅ line
    failed  → full card with rules + commands
    """
    if result.passed:
        warn_line = ""
        if result.warnings:
            warn_line = (
                f"\n⚠️ _Warnings: "
                f"{' · '.join(result.warnings)}_"
            )
        return (
            f"✅ *Rex approved* — auto-sending to "
            f"*{business_name}*\n"
            f"_{result.word_count}w · "
            f"{result.paragraph_count} paras · "
            f"Subject: {subject}_"
            f"{warn_line}"
        )

    # Failed — full review card
    fail_lines = "\n".join(
        f"  • {f}" for f in result.failures
    )
    warn_lines = ""
    if result.warnings:
        warn_lines = (
            "\n⚠️ *Warnings:*\n"
            + "\n".join(f"  _{w}_" for w in result.warnings)
        )

    preview = _strip_html(body)[:200].strip()
    if len(_strip_html(body)) > 200:
        preview += "..."

    return (
        f"🔍 *Rex flagged* — needs review (#{index})\n"
        f"*Business:* {business_name}\n"
        f"*Subject:* {subject}\n"
        f"*Stats:* {result.word_count}w · "
        f"{result.paragraph_count} paras\n\n"
        f"❌ *Failed rules:*\n{fail_lines}"
        f"{warn_lines}\n\n"
        f"```{preview}```\n\n"
        f"*Commands:*\n"
        f"  `!send-{index}`        → send as-is\n"
        f"  `!edit-{index} <note>` → redraft with feedback\n"
        f"  `!skip-{index}`        → skip this prospect\n"
        f"  `!review`              → see all pending"
    )
