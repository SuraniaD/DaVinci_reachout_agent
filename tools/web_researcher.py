from ddgs import DDGS
from interaction_log import log_action


def search_businesses(query: str) -> str:
    """
    Searches DuckDuckGo for businesses matching a query.
    Returns raw text results for the 70B model to process.

    This is intentionally raw — the LLM does the
    extraction and structuring, not this function.
    """
    try:
        print(f"🌐 [WEB RESEARCHER] Searching: '{query}'")

        # Run multiple targeted searches for better coverage
        search_queries = [
            query,
            f"{query} contact email",
            f"{query} owner founder"
        ]

        all_results = []

        for q in search_queries:
            try:
                results = DDGS().text(q, max_results=5)
                for r in results:
                    if r.get("body"):
                        all_results.append(
                            f"Title: {r['title']}\n"
                            f"URL: {r.get('href', '')}\n"
                            f"Body: {r['body']}\n"
                        )
            except Exception as e:
                print(
                    f"⚠️  [WEB RESEARCHER] "
                    f"Search '{q}' failed: {e}"
                )
                continue

        if not all_results:
            print(
                f"❌ [WEB RESEARCHER] "
                f"No results for: '{query}'"
            )
            return ""

        combined = "\n---\n".join(all_results)
        print(
            f"✅ [WEB RESEARCHER] "
            f"{len(all_results)} results found"
        )

        log_action(
            action_type="research",
            detail=(
                f"Web search: '{query}' — "
                f"{len(all_results)} results"
            )
        )

        return combined

    except Exception as e:
        print(f"❌ [WEB RESEARCHER] Failed: {e}")
        return ""


def search_email_for_business(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """
    Dedicated email search for a specific business.
    Called when the main research didn't find an email.
    Returns email string or None.
    """
    import re

    queries = []

    if website:
        domain = website.replace("https://", "") \
                        .replace("http://", "") \
                        .replace("www.", "") \
                        .split("/")[0]
        queries.append(
            f'"{business_name}" email {domain}'
        )
        queries.append(f"site:{domain} contact email")

    queries.append(
        f'"{business_name}" '
        f'{location or ""} contact email'
    )

    email_pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'

    for q in queries:
        try:
            results = DDGS().text(q, max_results=4)
            for r in results:
                text = (
                    r.get("title", "") + " " +
                    r.get("body", "")
                )
                emails = re.findall(email_pattern, text)

                # Filter junk
                junk = [
                    "example.com", "test.com",
                    "shopify.com", "wixpress.com",
                    "sentry.io"
                ]
                clean = [
                    e.lower() for e in emails
                    if not any(j in e for j in junk)
                ]

                if clean:
                    print(
                        f"✅ [EMAIL SEARCH] "
                        f"Found: {clean[0]}"
                    )
                    return clean[0]

        except Exception as e:
            print(
                f"⚠️  [EMAIL SEARCH] "
                f"Query failed: {e}"
            )
            continue

    print(
        f"❌ [EMAIL SEARCH] "
        f"No email found for {business_name}"
    )
    return None