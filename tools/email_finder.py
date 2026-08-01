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
        "wixpress.com", "shopify.com",
        "squarespace.com", "wordpress.com",
        "mailchimp.com", "gmail.com",
        "yahoo.com", "hotmail.com"
    ]
    clean = [
        e.lower() for e in emails
        if not any(
            j in e.lower() for j in junk_domains
        )
    ]

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
    Searches the web for an email linked to a website.
    Used by both Riley (file_reader) and Dexter.
    """
    domain = extract_domain_from_url(website_url)

    print(
        f"📧 [EMAIL FINDER] Via website — "
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
                break

        except Exception as e:
            print(
                f"⚠️  [EMAIL FINDER] "
                f"Query failed: {e}"
            )
            continue

    if not all_emails:
        print(
            f"❌ [EMAIL FINDER] No email for "
            f"{business_name} via website"
        )
        return None

    domain_clean  = domain.replace("www.", "")
    domain_emails = [
        e for e in all_emails
        if domain_clean in e
    ]

    chosen = domain_emails[0] \
        if domain_emails else all_emails[0]

    print(
        f"✅ [EMAIL FINDER] Via website: {chosen}"
    )

    log_action(
        action_type="email_found",
        business_name=business_name,
        detail=f"Found {chosen} via website"
    )

    return chosen


def find_email_from_business_name(
    business_name: str,
    location:      str = None
) -> str | None:
    """
    Searches for an email when no website is available.
    """
    query = f'"{business_name}" contact email'
    if location:
        query += f" {location}"

    print(
        f"📧 [EMAIL FINDER] Via name — "
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
                f"Via name: {chosen}"
            )
            log_action(
                action_type="email_found",
                business_name=business_name,
                detail=f"Found {chosen} by name"
            )
            return chosen

    except Exception as e:
        print(
            f"⚠️  [EMAIL FINDER] "
            f"Name search failed: {e}"
        )

    print(
        f"❌ [EMAIL FINDER] Not found: "
        f"{business_name}"
    )
    return None


def find_email_aggressive(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """
    Aggressive multi-strategy email search.
    Tries 5 different approaches before giving up.
    Called by audit_prospect before rejecting a lead.

    Strategy order:
    1. Website domain — site:domain.com contact
    2. Business name + email + location
    3. Founder/owner search
    4. LinkedIn snippet search
    5. Common email prefix patterns
    """
    print(
        f"🔍 [EMAIL AGGRESSIVE] All strategies: "
        f"{business_name}"
    )

    junk_domains = [
        "example.com", "test.com", "email.com",
        "domain.com", "yoursite.com", "sentry.io",
        "wixpress.com", "shopify.com",
        "squarespace.com", "wordpress.com",
        "mailchimp.com", "gmail.com",
        "yahoo.com", "hotmail.com"
    ]

    def clean_emails(text: str) -> list[str]:
        pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'
        found   = re.findall(pattern, text)
        return [
            e.lower() for e in found
            if not any(
                j in e.lower() for j in junk_domains
            )
        ]

    domain   = None
    queries  = []

    if website:
        domain = website \
            .replace("https://", "") \
            .replace("http://",  "") \
            .replace("www.",     "") \
            .split("/")[0]

    # Strategy 1 — website domain
    if domain:
        queries.append(f'site:{domain} email contact')
        queries.append(
            f'"{business_name}" {domain} email'
        )

    # Strategy 2 — name + location
    loc_str = f" {location}" if location else ""
    queries.append(
        f'"{business_name}"{loc_str} contact email'
    )

    # Strategy 3 — founder/owner
    queries.append(
        f'"{business_name}" founder owner '
        f'email{loc_str}'
    )

    # Strategy 4 — LinkedIn snippets
    queries.append(
        f'site:linkedin.com "{business_name}" email'
    )

    for q in queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                text   = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                )
                emails = clean_emails(text)

                if domain:
                    domain_clean  = domain.replace(
                        "www.", ""
                    )
                    domain_emails = [
                        e for e in emails
                        if domain_clean in e
                    ]
                    if domain_emails:
                        print(
                            f"✅ [EMAIL AGGRESSIVE] "
                            f"Domain match: "
                            f"{domain_emails[0]}"
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

    # Strategy 5 — common prefix guesses
    # Only if we have a domain to guess against
    if domain:
        for prefix in [
            "hello", "info", "contact",
            "hi", "team", "support"
        ]:
            guessed = f"{prefix}@{domain}"
            print(
                f"🔍 [EMAIL AGGRESSIVE] "
                f"Trying prefix guess: {guessed}"
            )
            # Verify the guess exists via search
            try:
                results = DDGS().text(
                    guessed, max_results=3
                )
                for r in results:
                    text   = (
                        r.get("title", "") + " " +
                        r.get("body",  "")
                    )
                    emails = clean_emails(text)
                    # Only return if the guessed email
                    # actually appears in results
                    if guessed in emails:
                        print(
                            f"✅ [EMAIL AGGRESSIVE] "
                            f"Prefix confirmed: {guessed}"
                        )
                        return guessed
            except Exception:
                continue

    print(
        f"❌ [EMAIL AGGRESSIVE] "
        f"All strategies failed: {business_name}"
    )
    return None


def audit_prospect(prospect: dict) -> dict:
    """
    Audit gate — runs before DB insert.
    Decides pass / reject for each prospect.

    Rules:
    - PASS:   has a valid email + business name
    - ENRICH: has business name but no email — try harder
    - REJECT: no business name, or enrichment failed

    Returns:
    {
        "decision": "pass" | "reject",
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
        if re.match(
            r'^[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}$',
            email.strip()
        ):
            return {
                "decision": "pass",
                "reason":   f"email confirmed: {email}",
                "prospect": prospect
            }

    # No email — try aggressive enrichment
    print(
        f"🔍 [AUDIT] No email for "
        f"'{business_name}' — enriching..."
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
                f"enriched — "
                f"found email: {found_email}"
            ),
            "prospect": prospect
        }

    # Reject — all strategies failed
    return {
        "decision": "reject",
        "reason":   (
            "no email found after aggressive search"
        ),
        "prospect": prospect
    }