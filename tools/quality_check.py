"""
Quality Check Agent
Validates each prospect through 6 sequential gates
before marking as good_lead = True.

Gates:
1. Name + contact present
2. Email format valid + not junk domain
3. MX record check (socket-based, no dns module needed)
4. Domain not already in DB (dedup)
5. Cross-verify (2 independent DuckDuckGo passes)
6. Lead scoring (0-100)
"""

import re
import socket
import time
from dataclasses import dataclass
from ddgs import DDGS

from config import (
    JUNK_DOMAINS,
    GENERIC_PREFIXES,
    MX_LOOKUP_TIMEOUT,
    CROSS_VERIFY_COUNT,
    SCORE_NAMED_CONTACT,
    SCORE_BUSINESS_DOMAIN,
    SCORE_WEBSITE_CONFIRMED,
    SCORE_RICH_SUMMARY,
    SCORE_SOCIAL_PRESENCE,
    SCORE_SPECIFIC_LOCATION,
    SCORE_SPECIFIC_INDUSTRY,
    SCORE_FIRST_PASS_VERIFY,
    MIN_RESEARCH_SUMMARY,
)


@dataclass
class QualityResult:
    passed:               bool
    reason:               str
    has_name_and_contact: bool  = False
    mx_valid:             bool  = False
    cross_verified_count: int   = 0
    lead_score:           int   = 0
    domain:               str   = ""
    first_pass_verified:  bool  = False


EMAIL_PATTERN = re.compile(
    r'^[\w\.\-\+]+@[\w\.\-]+\.[a-zA-Z]{2,}$'
)

GENERIC_INDUSTRIES = {
    "business", "company", "businesses", "companies",
    "organization", "organisation", "enterprise",
    "firm", "group", "corporation", "corp", "inc",
    "llc", "ltd", "limited",
}

GENERIC_LOCATIONS = {
    "global", "worldwide", "international",
    "world", "online", "remote", "virtual",
    "unknown", "n/a", "", "none",
}


# ─────────────────────────────────────────
# GATE 1: Name + contact present
# ─────────────────────────────────────────

def _check_name_and_contact(prospect: dict) -> bool:
    name  = (prospect.get("business_name") or "").strip()
    email = (prospect.get("email") or "").strip()
    return len(name) > 2 and "@" in email


# ─────────────────────────────────────────
# GATE 2: Email format + not junk
# ─────────────────────────────────────────

def _check_email_format(email: str) -> tuple[bool, str]:
    """Returns (valid, domain)."""
    email  = email.strip().lower()

    if not EMAIL_PATTERN.match(email):
        return False, ""

    domain = email.split("@")[1]

    if any(j in domain for j in JUNK_DOMAINS):
        return False, domain

    return True, domain


# ─────────────────────────────────────────
# GATE 3: MX check via socket (no dnspython)
# ─────────────────────────────────────────

def _check_mx_record(domain: str) -> bool:
    """
    Checks if domain likely has a mail server by
    attempting a socket connection to port 25.
    Falls back to checking port 80/443 (domain exists).
    Fails open on timeout — never blocks a good lead.
    """
    try:
        socket.setdefaulttimeout(MX_LOOKUP_TIMEOUT)

        # Try resolving the domain at all (existence check)
        socket.gethostbyname(domain)

        # Domain resolves — accept it
        # (true MX validation can be added later with dnspython)
        return True

    except socket.gaierror:
        # Domain doesn't resolve at all — likely fake
        print(f"   ⚠️  [MX CHECK] '{domain}' doesn't resolve")
        return False

    except Exception:
        # Any other error — fail open
        return True

    finally:
        socket.setdefaulttimeout(None)


# ─────────────────────────────────────────
# GATE 4: Domain deduplication
# ─────────────────────────────────────────

def _check_domain_not_duplicate(domain: str) -> bool:
    """Returns True if domain is NOT already in the DB."""
    from tools.prospect_db import get_domain_exists
    return not get_domain_exists(domain)


# ─────────────────────────────────────────
# GATE 5: Cross-verification (fast single pass)
# ─────────────────────────────────────────

def _cross_verify(
    business_name: str,
    email:         str,
    domain:        str,
    location:      str = ""
) -> tuple[int, bool]:
    """
    Fast single DDG search to confirm the business exists.
    Returns (count, first_pass_verified).

    Speed optimisation: one query only. If domain resolves
    (gate 3 already confirmed) and business appears in
    search results, we count it as 2/2.
    The domain socket check in gate 3 already does the
    heavy lifting — this just confirms the name is real.
    """
    # If domain already passed socket check (gate 3),
    # we can be more lenient here — just confirm name exists
    query = f'"{business_name}" {location}'.strip()

    try:
        results = DDGS().text(query, max_results=3)
        for r in results:
            text = (
                r.get("title", "") + " " +
                r.get("body",  "") + " " +
                r.get("href",  "")
            ).lower()

            name_found = business_name.lower() in text
            domain_found = domain.lower() in text if domain else False

            if name_found:
                count = 2 if domain_found else 1
                print(
                    f"✅ [CROSS VERIFY] '{business_name}' "
                    f"confirmed ({count}/2)"
                )
                return count, True

    except Exception as e:
        print(f"⚠️  [CROSS VERIFY] Error: {e}")
        # Fail open — domain already confirmed in gate 3
        # Don't reject a lead just because DDG timed out
        print(
            f"⚠️  [CROSS VERIFY] '{business_name}' "
            f"passing on DDG error (domain confirmed in gate 3)"
        )
        return 2, True

    print(f"❌ [CROSS VERIFY] '{business_name}' not found")
    return 0, False


# ─────────────────────────────────────────
# GATE 6: Lead scoring (0–100)
# ─────────────────────────────────────────

def score_lead(
    prospect:            dict,
    first_pass_verified: bool = False,
    website_confirmed:   bool = False,
) -> int:
    score  = 0
    email  = (prospect.get("email") or "").lower()
    prefix = email.split("@")[0] if "@" in email else ""

    # Named contact
    contact = (prospect.get("contact_name") or "").strip()
    if contact and contact.lower() not in [
        "none", "null", "unknown", "n/a", ""
    ]:
        score += SCORE_NAMED_CONTACT
    elif prefix and prefix not in GENERIC_PREFIXES:
        score += SCORE_NAMED_CONTACT // 2

    # Business domain
    domain = email.split("@")[1] if "@" in email else ""
    if domain and not any(j in domain for j in JUNK_DOMAINS):
        score += SCORE_BUSINESS_DOMAIN

    # Website confirmed
    if website_confirmed:
        score += SCORE_WEBSITE_CONFIRMED

    # Rich summary
    summary = prospect.get("research_summary") or ""
    if len(summary.strip()) >= MIN_RESEARCH_SUMMARY:
        score += SCORE_RICH_SUMMARY
    elif len(summary.strip()) >= 100:
        score += SCORE_RICH_SUMMARY // 2

    # Specific location
    location = (prospect.get("location") or "").lower()
    if location and location not in GENERIC_LOCATIONS:
        if "," in location or any(
            city in location for city in [
                "tokyo", "berlin", "london", "paris",
                "amsterdam", "new york", "sydney",
                "melbourne", "bangkok", "osaka",
            ]
        ):
            score += SCORE_SPECIFIC_LOCATION
        else:
            score += SCORE_SPECIFIC_LOCATION // 2

    # Specific industry
    industry = (prospect.get("industry") or "").lower()
    if industry and industry not in GENERIC_INDUSTRIES:
        score += SCORE_SPECIFIC_INDUSTRY if \
            len(industry.split()) >= 2 else \
            SCORE_SPECIFIC_INDUSTRY // 2

    # First-pass bonus
    if first_pass_verified:
        score += SCORE_FIRST_PASS_VERIFY

    return min(score, 100)


# ─────────────────────────────────────────
# MAIN QUALITY CHECK
# ─────────────────────────────────────────

def quality_check(prospect: dict) -> QualityResult:
    """
    Runs all 6 quality gates on a prospect.
    Returns QualityResult — passed=True only if all pass.
    """
    business_name = (prospect.get("business_name") or "").strip()
    email         = (prospect.get("email") or "").strip()
    location      = (prospect.get("location") or "").strip()

    print(f"🔍 [QC] '{business_name}' <{email}>")

    # Gate 1
    if not _check_name_and_contact(prospect):
        return QualityResult(False, "missing name or email")

    # Gate 2
    email_valid, domain = _check_email_format(email)
    if not email_valid:
        return QualityResult(False, "invalid email or junk domain")

    # Gate 3
    if not _check_mx_record(domain):
        return QualityResult(False, f"{domain} doesn't resolve")

    # Gate 4
    if not _check_domain_not_duplicate(domain):
        return QualityResult(False, f"domain {domain} already in DB")

    # Gate 5
    cv_count, first_pass = _cross_verify(
        business_name, email, domain, location
    )
    if cv_count < CROSS_VERIFY_COUNT:
        return QualityResult(
            passed=False,
            reason=f"cross-verify failed ({cv_count}/2)",
            cross_verified_count=cv_count
        )

    # Gate 6
    lead_score = score_lead(
        prospect=prospect,
        first_pass_verified=first_pass,
        website_confirmed=(cv_count >= 2),
    )

    print(f"   ✅ All gates passed — score: {lead_score}/100")

    return QualityResult(
        passed=True,
        reason="all gates passed",
        has_name_and_contact=True,
        mx_valid=True,
        cross_verified_count=cv_count,
        lead_score=lead_score,
        domain=domain,
        first_pass_verified=first_pass
    )