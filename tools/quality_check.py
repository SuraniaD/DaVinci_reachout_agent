"""
Quality Check Agent
Validates each prospect through 6 sequential gates
before marking as good_lead = True.

Gates:
1. Name + contact present
2. Email format valid + not junk domain
3. MX record exists for domain
4. Domain not already in DB (dedup)
5. Cross-verify (2 independent search passes)
6. Lead scoring (0-100)
"""

import re
import time
import socket
import dns.resolver
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
    has_name_and_contact: bool   = False
    mx_valid:             bool   = False
    cross_verified_count: int    = 0
    lead_score:           int    = 0
    domain:               str    = ""
    first_pass_verified:  bool   = False


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
    email = email.strip().lower()

    if not EMAIL_PATTERN.match(email):
        return False, ""

    domain = email.split("@")[1]

    if any(j in domain for j in JUNK_DOMAINS):
        return False, domain

    return True, domain


# ─────────────────────────────────────────
# GATE 3: MX record lookup
# ─────────────────────────────────────────

def _check_mx_record(domain: str) -> bool:
    """
    Returns True if domain has at least one MX record.
    Fails open (returns True) if DNS times out.
    """
    try:
        resolver = dns.resolver.Resolver()
        resolver.lifetime = float(MX_LOOKUP_TIMEOUT)
        records = resolver.resolve(domain, 'MX')
        return len(records) > 0

    except dns.resolver.NXDOMAIN:
        # Domain does not exist at all
        return False

    except dns.resolver.NoAnswer:
        # Domain exists but no MX — check A record fallback
        try:
            resolver.resolve(domain, 'A')
            # Has A record but no MX — borderline, accept
            return True
        except Exception:
            return False

    except Exception:
        # Timeout or other error — fail open
        print(
            f"⚠️  [MX CHECK] Timeout/error for "
            f"'{domain}' — passing"
        )
        return True


# ─────────────────────────────────────────
# GATE 4: Domain deduplication
# ─────────────────────────────────────────

def _check_domain_not_duplicate(domain: str) -> bool:
    """
    Returns True if domain is NOT already in the DB.
    Import here to avoid circular import.
    """
    from tools.prospect_db import get_domain_exists
    exists = get_domain_exists(domain)
    return not exists


# ─────────────────────────────────────────
# GATE 5: Cross-verification (2 passes)
# ─────────────────────────────────────────

def _cross_verify(
    business_name: str,
    email:         str,
    domain:        str,
    location:      str = ""
) -> tuple[int, bool]:
    """
    Runs 2 independent search passes to confirm
    this business and email are real.

    Returns (count, first_pass_verified)
    where count is 0, 1, or 2.
    """
    count               = 0
    first_pass_verified = False

    loc_suffix = f" {location}" if location else ""

    # Pass 1: does the business + email appear together?
    pass1_queries = [
        f'"{business_name}"{loc_suffix} email contact',
        f'"{business_name}" {email}',
    ]

    for q in pass1_queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                text = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                ).lower()

                name_found  = business_name.lower() in text
                email_found = email.lower() in text or \
                              domain.lower() in text

                if name_found and email_found:
                    count               += 1
                    first_pass_verified  = True
                    print(
                        f"✅ [CROSS VERIFY] Pass 1: "
                        f"'{business_name}' confirmed"
                    )
                    break

            if first_pass_verified:
                break

        except Exception as e:
            print(f"⚠️  [CROSS VERIFY] Pass 1 error: {e}")

    # Pass 2: does the domain exist publicly?
    pass2_queries = [
        f'"{business_name}" site:{domain}',
        f'"{business_name}" {domain}',
    ]

    for q in pass2_queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                url  = r.get("href", "").lower()
                text = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                ).lower()

                domain_found = domain.lower() in url or \
                               domain.lower() in text
                name_found   = business_name.lower() in text

                if domain_found and name_found:
                    count += 1
                    print(
                        f"✅ [CROSS VERIFY] Pass 2: "
                        f"'{business_name}' domain confirmed"
                    )
                    break

            if count >= 2:
                break

        except Exception as e:
            print(f"⚠️  [CROSS VERIFY] Pass 2 error: {e}")

    print(
        f"📋 [CROSS VERIFY] '{business_name}': "
        f"{count}/2 passes"
    )
    return count, first_pass_verified


# ─────────────────────────────────────────
# GATE 6: Lead scoring
# ─────────────────────────────────────────

def score_lead(
    prospect:            dict,
    first_pass_verified: bool = False,
    website_confirmed:   bool = False,
    social_found:        bool = False,
) -> int:
    score  = 0
    email  = (prospect.get("email") or "").lower()
    prefix = email.split("@")[0] if "@" in email else ""

    # Named contact (not generic prefix)
    contact = (prospect.get("contact_name") or "").strip()
    if contact and contact.lower() not in [
        "none", "null", "unknown", "n/a", ""
    ]:
        score += SCORE_NAMED_CONTACT
    elif prefix and prefix not in GENERIC_PREFIXES:
        score += SCORE_NAMED_CONTACT // 2

    # Business domain email
    domain = email.split("@")[1] if "@" in email else ""
    if domain and not any(j in domain for j in JUNK_DOMAINS):
        score += SCORE_BUSINESS_DOMAIN

    # Website confirmed in cross-verify
    if website_confirmed:
        score += SCORE_WEBSITE_CONFIRMED

    # Rich research summary
    summary = prospect.get("research_summary") or ""
    if len(summary.strip()) >= MIN_RESEARCH_SUMMARY:
        score += SCORE_RICH_SUMMARY
    elif len(summary.strip()) >= 100:
        score += SCORE_RICH_SUMMARY // 2

    # Social presence
    if social_found:
        score += SCORE_SOCIAL_PRESENCE

    # Specific location (city-level)
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
        if len(industry.split()) >= 2:
            score += SCORE_SPECIFIC_INDUSTRY
        else:
            score += SCORE_SPECIFIC_INDUSTRY // 2

    # First-pass verification bonus
    if first_pass_verified:
        score += SCORE_FIRST_PASS_VERIFY

    return min(score, 100)


# ─────────────────────────────────────────
# MAIN QUALITY CHECK FUNCTION
# ─────────────────────────────────────────

def quality_check(prospect: dict) -> QualityResult:
    """
    Runs all 6 quality gates on a prospect.
    Returns QualityResult with passed=True only
    if all gates pass.
    """
    business_name = (
        prospect.get("business_name") or ""
    ).strip()
    email    = (prospect.get("email") or "").strip()
    location = (prospect.get("location") or "").strip()

    print(
        f"🔍 [QUALITY CHECK] '{business_name}' "
        f"<{email}>"
    )

    # ── Gate 1: Name + contact ────────────
    if not _check_name_and_contact(prospect):
        print(
            f"   ❌ Gate 1 failed: missing name/email"
        )
        return QualityResult(
            passed=False,
            reason="missing business name or email"
        )

    # ── Gate 2: Email format ──────────────
    email_valid, domain = _check_email_format(email)
    if not email_valid:
        print(f"   ❌ Gate 2 failed: invalid email")
        return QualityResult(
            passed=False,
            reason=f"invalid email format or junk domain"
        )

    # ── Gate 3: MX record ────────────────
    mx_valid = _check_mx_record(domain)
    if not mx_valid:
        print(f"   ❌ Gate 3 failed: no MX record")
        return QualityResult(
            passed=False,
            reason=f"domain {domain} has no mail server"
        )

    # ── Gate 4: Domain dedup ─────────────
    if not _check_domain_not_duplicate(domain):
        print(f"   ❌ Gate 4 failed: domain duplicate")
        return QualityResult(
            passed=False,
            reason=f"domain {domain} already in database"
        )

    # ── Gate 5: Cross-verify ─────────────
    cv_count, first_pass = _cross_verify(
        business_name=business_name,
        email=email,
        domain=domain,
        location=location
    )

    if cv_count < CROSS_VERIFY_COUNT:
        print(
            f"   ❌ Gate 5 failed: "
            f"cross-verified {cv_count}/2"
        )
        return QualityResult(
            passed=False,
            reason=(
                f"could not cross-verify "
                f"({cv_count}/2 passes)"
            ),
            cross_verified_count=cv_count
        )

    # ── Gate 6: Score ─────────────────────
    lead_score = score_lead(
        prospect=prospect,
        first_pass_verified=first_pass,
        website_confirmed=(cv_count >= 2),
    )

    print(
        f"   ✅ All gates passed — "
        f"score: {lead_score}/100"
    )

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