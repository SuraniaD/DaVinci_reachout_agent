import re
from ddgs import DDGS
from interaction_log import log_action


def extract_emails_from_text(text: str) -> list[str]:
    """
    Pulls any email addresses out of a block of text.
    Used on search results to find contact emails.
    """
    pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'
    emails  = re.findall(pattern, text)

    # Filter out obvious junk
    junk_domains = [
        "example.com", "test.com", "email.com",
        "domain.com", "yoursite.com", "sentry.io",
        "wixpress.com", "shopify.com"
    ]
    clean = [
        e.lower() for e in emails
        if not any(j in e.lower() for j in junk_domains)
    ]

    # Deduplicate while preserving order
    seen = set()
    result = []
    for e in clean:
        if e not in seen:
            seen.add(e)
            result.append(e)

    return result


def extract_domain_from_url(url: str) -> str:
    """
    Pulls the domain out of a URL string.
    e.g. "https://veganleatherco.com" → "veganleatherco.com"
    e.g. "veganleatherco.com" → "veganleatherco.com"
    """
    url = url.strip().lower()
    url = url.replace("https://", "").replace("http://", "")
    url = url.replace("www.", "")
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
    """Returns True if text is a social media handle or link."""
    social_keywords = [
        "instagram", "twitter", "facebook", "linkedin",
        "tiktok", "youtube", "@", "instagram only",
        "social only", "ig:", "fb:"
    ]
    text_lower = text.lower()
    return any(k in text_lower for k in social_keywords)


def find_email_from_website(
    business_name: str,
    website_url:   str
) -> str | None:
    """
    Given a business name and website URL,
    searches the web to find a contact email.

    Strategy:
    1. Search "[business name] contact email [domain]"
    2. Extract any emails from search results
    3. Prefer emails at the same domain as the website
    4. Fall back to any email found
    """
    domain = extract_domain_from_url(website_url)

    print(
        f"🔎 [EMAIL FINDER] Searching for email — "
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
                break  # Stop if we found something

        except Exception as e:
            print(f"⚠️  [EMAIL FINDER] Search failed: {e}")
            continue

    if not all_emails:
        print(
            f"❌ [EMAIL FINDER] No email found for "
            f"{business_name}"
        )
        return None

    # Prefer emails at the same domain
    domain_emails = [
        e for e in all_emails
        if domain.replace("www.", "") in e
    ]

    if domain_emails:
        chosen = domain_emails[0]
        print(
            f"✅ [EMAIL FINDER] Found domain email: "
            f"{chosen}"
        )
    else:
        chosen = all_emails[0]
        print(
            f"✅ [EMAIL FINDER] Found email: {chosen}"
        )

    log_action(
        action_type="email_found",
        business_name=business_name,
        detail=f"Found {chosen} via web search"
    )

    return chosen


def find_email_from_business_name(
    business_name: str,
    location:      str = None
) -> str | None:
    """
    When there's no website URL — search for the
    business by name and try to find a contact email.
    """
    query = f'"{business_name}" contact email'
    if location:
        query += f" {location}"

    print(
        f"🔎 [EMAIL FINDER] Searching by name — "
        f"{business_name}"
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
                f"✅ [EMAIL FINDER] Found: {chosen}"
            )
            log_action(
                action_type="email_found",
                business_name=business_name,
                detail=f"Found {chosen} by name search"
            )
            return chosen

    except Exception as e:
        print(f"⚠️  [EMAIL FINDER] Name search failed: {e}")

    print(
        f"❌ [EMAIL FINDER] No email found for "
        f"{business_name}"
    )
    return None