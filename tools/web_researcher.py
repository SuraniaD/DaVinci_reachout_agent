import re
from ddgs import DDGS
from interaction_log import log_action


def search_businesses(
    query:       str,
    max_results: int = 10
) -> str:
    """
    Searches DuckDuckGo for businesses matching a query.
    Returns raw text results for the 70B model to process.
    Runs multiple targeted queries for wider coverage.
    Deduplicates results by URL.
    """
    try:
        print(
            f"🌐 [WEB RESEARCHER] Searching: '{query}' "
            f"(max {max_results} per query)"
        )

        search_queries = [
            query,
            f"{query} contact email",
            f"{query} owner founder website",
            f"{query} list directory"
        ]

        all_results = []
        seen_urls   = set()

        for q in search_queries:
            try:
                results = DDGS().text(
                    q, max_results=max_results
                )
                for r in results:
                    url = r.get("href", "")
                    if url and url in seen_urls:
                        continue
                    seen_urls.add(url)

                    if r.get("body"):
                        all_results.append(
                            f"Title: {r['title']}\n"
                            f"URL: {url}\n"
                            f"Body: {r['body']}\n"
                        )

            except Exception as e:
                print(
                    f"⚠️  [WEB RESEARCHER] "
                    f"Query '{q}' failed: {e}"
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
            f"{len(all_results)} unique results"
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


def search_businesses_multi_query(
    industry: str,
    location: str,
    target:   int = 50
) -> list[str]:
    """
    Generates multiple specific search queries to
    find enough businesses to meet the target count.

    Returns a list of raw result strings — one per
    search query — for the 70B model to process in batches.

    Strategy:
    - Search by city/country variations
    - Search with different qualifiers
    - Search for business directories and lists
    - Search with email/contact terms
    """
    queries = []

    # Base variations
    queries.append(f"{industry} {location}")
    queries.append(f"{industry} businesses {location}")
    queries.append(f"{industry} companies {location}")
    queries.append(f"{industry} brands {location}")

    # Directory and list searches
    queries.append(
        f"list of {industry} businesses {location}"
    )
    queries.append(
        f"{industry} {location} directory"
    )
    queries.append(
        f"top {industry} {location} contact email"
    )

    # SMB specific
    queries.append(
        f"small {industry} business {location} "
        f"owner email"
    )
    queries.append(
        f"independent {industry} {location}"
    )

    # Location variations — break down regions
    location_lower = location.lower()
    if "europe" in location_lower:
        # Search major European countries individually
        for country in [
            "UK", "Germany", "Netherlands",
            "France", "Spain", "Italy",
            "Sweden", "Denmark", "Belgium"
        ]:
            queries.append(
                f"{industry} {country}"
            )
            queries.append(
                f"{industry} businesses {country} "
                f"contact email"
            )

    elif "usa" in location_lower or \
         "united states" in location_lower or \
         "america" in location_lower:
        for state in [
            "New York", "California", "Texas",
            "Florida", "Chicago", "Los Angeles",
            "San Francisco", "Seattle", "Boston"
        ]:
            queries.append(f"{industry} {state}")

    elif "uk" in location_lower or \
         "united kingdom" in location_lower:
        for city in [
            "London", "Manchester", "Birmingham",
            "Edinburgh", "Bristol", "Leeds",
            "Glasgow", "Liverpool"
        ]:
            queries.append(f"{industry} {city}")

    elif "australia" in location_lower:
        for city in [
            "Sydney", "Melbourne", "Brisbane",
            "Perth", "Adelaide"
        ]:
            queries.append(f"{industry} {city}")

    print(
        f"🌐 [WEB RESEARCHER] Generated "
        f"{len(queries)} search queries for "
        f"target of {target} businesses"
    )

    # Run searches and collect unique results
    all_result_blocks = []
    seen_urls         = set()
    total_results     = 0

    for q in queries:
        try:
            results = DDGS().text(q, max_results=10)
            block_parts = []

            for r in results:
                url = r.get("href", "")
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)

                if r.get("body"):
                    block_parts.append(
                        f"Title: {r['title']}\n"
                        f"URL: {url}\n"
                        f"Body: {r['body']}\n"
                    )
                    total_results += 1

            if block_parts:
                all_result_blocks.append(
                    "\n---\n".join(block_parts)
                )

            print(
                f"   🔍 '{q[:50]}' → "
                f"{len(block_parts)} results "
                f"(total: {total_results})"
            )

        except Exception as e:
            print(
                f"⚠️  [WEB RESEARCHER] "
                f"Query failed: {e}"
            )
            continue

    print(
        f"✅ [WEB RESEARCHER] Total: "
        f"{total_results} unique results across "
        f"{len(all_result_blocks)} batches"
    )

    return all_result_blocks


def search_email_for_business(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """
    Dedicated email search for a specific business.
    """
    queries = []

    if website:
        domain = website \
            .replace("https://", "") \
            .replace("http://",  "") \
            .replace("www.",     "") \
            .split("/")[0]
        queries.append(
            f'"{business_name}" email {domain}'
        )
        queries.append(
            f"site:{domain} contact email"
        )

    queries.append(
        f'"{business_name}" '
        f'{location or ""} contact email'
    )

    email_pattern = r'[\w\.-]+@[\w\.-]+\.[a-zA-Z]{2,}'

    for q in queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                text = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                )
                emails = re.findall(email_pattern, text)

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
            print(f"⚠️  [EMAIL SEARCH] Failed: {e}")
            continue

    return None