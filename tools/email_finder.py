import re
from ddgs import DDGS
from interaction_log import log_action


def extract_emails_from_text(text: str) -> list[str]:
    """
    Pulls any email addresses out of a block of text.
    """
    pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'
    emails  = re.findall(pattern, text)

    junk_domains = [
        "example.com", "test.com", "email.com",
        "domain.com", "yoursite.com", "sentry.io",
        "wixpress.com", "shopify.com", "squarespace.com",
        "wordpress.com", "mailchimp.com", "gmail.com"
    ]
    clean = [
        e.lower() for e in emails
        if not any(j in e.lower() for j in junk_domains)
    ]

    # Deduplicate preserving order
    seen   = set()
    result = []
    for e in clean:
        if e not in seen:
            seen.add(e)
            result.append(e)

    return result


def extract_domain_from_url(url: str) -> str:
    """Pulls the domain from a URL string."""
    url = url.strip().lower()
    url = url.replace("https://", "") \
             .replace("http://",  "") \
             .replace("www.",     "")
    url = url.split("/")[0]
    return url


def is_url(text: str) -> bool:
    """Returns True if text looks like a website URL."""
    text = text.strip().lower()
    return (
        text.startswith("http") or
        text.startswith("www.") or
        (
            "." in text and
            " " not in text and
            "@" not in text and
            len(text) > 4
        )
    )


def is_social_media(text: str) -> bool:
    """Returns True if text is a social media link."""
    keywords = [
        "instagram", "twitter", "facebook",
        "linkedin", "tiktok", "youtube",
        "@", "instagram only", "social only"
    ]
    text_lower = text.lower()
    return any(k in text_lower for k in keywords)


def find_email_from_website(
    business_name: str,
    website_url:   str
) -> str | None:
    """
    Searches the web for an email address linked to
    a specific business website.
    Used by both Riley (file_reader) and Dexter (research).

    Strategy:
    1. Search "[business] contact email [domain]"
    2. Search "site:[domain] contact email"
    3. Search "[business] hello@ OR contact@ OR info@"
    """
    domain = extract_domain_from_url(website_url)

    print(
        f"📧 [EMAIL FINDER] Searching via website — "
        f"{business_name} ({domain})"
    )

    queries = [
        f'"{business_name}" contact email {domain}',
        f'site:{domain} contact email',
        f'"{business_name}" hello@ OR contact@ OR info@'
    ]

    all_emails = []

    for query in queries:
        try:
            results = DDGS().text(query, max_results=5)
            for r in results:
                text   = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                )
                emails = extract_emails_from_text(text)
                all_emails.extend(emails)

            if all_emails:
                break  # Stop at first successful query

        except Exception as e:
            print(
                f"⚠️  [EMAIL FINDER] "
                f"Query failed: {e}"
            )
            continue

    if not all_emails:
        print(
            f"❌ [EMAIL FINDER] No email found for "
            f"{business_name} via website"
        )
        return None

    # Prefer emails at the same domain
    domain_clean   = domain.replace("www.", "")
    domain_emails  = [
        e for e in all_emails
        if domain_clean in e
    ]

    chosen = domain_emails[0] if domain_emails \
        else all_emails[0]

    print(
        f"✅ [EMAIL FINDER] Found via website: {chosen}"
    )

    log_action(
        action_type="email_found",
        business_name=business_name,
        detail=f"Found {chosen} via website search"
    )

    return chosen
def find_email_aggressive(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """
    Aggressive multi-strategy email search.
    Tries 5 different search approaches before giving up.
    Called by the audit gate before rejecting a prospect.

    Strategy order:
    1. Website domain — site:domain.com contact email
    2. Business name + "email" + location
    3. Founder/owner search — "[business] founder owner email"
    4. LinkedIn about page (sometimes surfaces emails)
    5. Generic contact page patterns (hello@, info@, contact@)
    """
    import re as _re

    print(
        f"🔍 [EMAIL AGGRESSIVE] Trying all strategies: "
        f"{business_name}"
    )

    email_pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'

    junk_domains = [
        "example.com", "test.com", "email.com",
        "domain.com", "yoursite.com", "sentry.io",
        "wixpress.com", "shopify.com",
        "squarespace.com", "wordpress.com",
        "mailchimp.com", "gmail.com",
        "yahoo.com", "hotmail.com"
    ]

    def clean_emails(text: str) -> list[str]:
        found = _re.findall(email_pattern, text)
        return [
            e.lower() for e in found
            if not any(j in e.lower() for j in junk_domains)
        ]

    queries = []

    # Strategy 1 — website domain search
    if website:
        domain = website \
            .replace("https://", "") \
            .replace("http://",  "") \
            .replace("www.",     "") \
            .split("/")[0]
        queries.append(f'site:{domain} email contact')
        queries.append(
            f'"{business_name}" {domain} email'
        )

    # Strategy 2 — business name + location
    loc_str = f" {location}" if location else ""
    queries.append(
        f'"{business_name}"{loc_str} contact email'
    )

    # Strategy 3 — founder/owner
    queries.append(
        f'"{business_name}" founder owner '
        f'email{loc_str}'
    )

    # Strategy 4 — LinkedIn (sometimes has emails in
    # page snippets)
    queries.append(
        f'site:linkedin.com "{business_name}" email'
    )

    # Strategy 5 — common email prefixes
    if website:
        domain = website \
            .replace("https://", "") \
            .replace("http://",  "") \
            .replace("www.",     "") \
            .split("/")[0]
        for prefix in [
            "hello", "info", "contact",
            "hi", "team", "support"
        ]:
            queries.append(
                f'{prefix}@{domain}'
            )

    for q in queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                text = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                )
                emails = clean_emails(text)

                if website:
                    domain = website \
                        .replace("https://", "") \
                        .replace("http://",  "") \
                        .replace("www.",     "") \
                        .split("/")[0] \
                        .replace("www.", "")
                    # Prefer emails at the business domain
                    domain_emails = [
                        e for e in emails
                        if domain in e
                    ]
                    if domain_emails:
                        print(
                            f"✅ [EMAIL AGGRESSIVE] "
                            f"Domain match: {domain_emails[0]}"
                        )
                        return domain_emails[0]

                if emails:
                    print(
                        f"✅ [EMAIL AGGRESSIVE] "
                        f"Found: {emails[0]} "
                        f"via '{q[:40]}'"
                    )
                    return emails[0]

        except Exception as e:
            print(
                f"⚠️  [EMAIL AGGRESSIVE] "
                f"Query failed: {e}"
            )
            continue

    print(
        f"❌ [EMAIL AGGRESSIVE] "
        f"All strategies failed: {business_name}"
    )
    return None


def audit_prospect(prospect: dict) -> dict:
    """
    Audit gate — runs before DB insert.
    Returns audit result with pass/reject/enrich decision.

    Rules:
    - PASS:   has a valid email + business name
    - ENRICH: has business name but no email — try harder
    - REJECT: no business name, or enrichment failed

    Returns:
    {
        "decision": "pass" | "enrich" | "reject",
        "reason":   str,
        "prospect": dict  (may have email added)
    }
    """
    business_name = prospect.get("business_name", "")
    email         = prospect.get("email", "")
    website       = prospect.get("website")
    location      = prospect.get("location")

    # Hard reject — no business name
    if not business_name or \
       str(business_name).strip().lower() in [
           "none", "null", "unknown", "", "n/a"
       ]:
        return {
            "decision": "reject",
            "reason":   "no business name",
            "prospect": prospect
        }

    # Pass — already has a valid email
    if email and str(email).strip().lower() not in [
        "", "none", "null", "n/a", "not found"
    ]:
        # Quick sanity check — does it look like a real email
        import re as _re
        if _re.match(
            r'^[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}$',
            email.strip()
        ):
            return {
                "decision": "pass",
                "reason":   f"email confirmed: {email}",
                "prospect": prospect
            }

    # Enrich — has business name but no valid email
    # Try aggressive search before rejecting
    print(
        f"🔍 [AUDIT] No email for '{business_name}' "
        f"— trying enrichment..."
    )

    found_email = find_email_aggressive(
        business_name=business_name,
        website=website,
        location=location
    )

    if found_email:
        prospect["email"] = found_email
        return {
            "decision": "pass",
            "reason":   (
                f"enriched — found email: {found_email}"
            ),
            "prospect": prospect
        }

    # Reject — enrichment failed
    return {
        "decision": "reject",
        "reason":   (
            "no email found after aggressive search"
        ),
        "prospect": prospect
    }


def find_email_from_business_name(
    business_name: str,
    location:      str = None
) -> str | None:
    """
    Searches for an email when no website is available.
    Uses business name + optional location.
    Used by both Riley (file_reader) and Dexter (research).
    """
    query = f'"{business_name}" contact email'
    if location:
        query += f" {location}"

    print(
        f"📧 [EMAIL FINDER] Searching by name — "
        f"{business_name}"
        f"{f' ({location})' if location else ''}"
    )

    try:
        results    = DDGS().text(query, max_results=6)
        all_emails = []

        for r in results:
            text   = (
                r.get("title", "") + " " +
                r.get("body",  "")
            )
            emails = extract_emails_from_text(text)
            all_emails.extend(emails)

        if all_emails:
            chosen = all_emails[0]
            print(
                f"✅ [EMAIL FINDER] "
                f"Found by name: {chosen}"
            )
            log_action(
                action_type="email_found",
                business_name=business_name,
                detail=(
                    f"Found {chosen} by name search"
                )
            )
            return chosen

    except Exception as e:
        print(
            f"⚠️  [EMAIL FINDER] "
            f"Name search failed: {e}"
        )

    print(
        f"❌ [EMAIL FINDER] No email found for "
        f"{business_name}"
    )
    return None