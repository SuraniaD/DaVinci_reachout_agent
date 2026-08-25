"""
Web Researcher — DuckDuckGo search
Generates diverse search queries for a given
industry + location and returns raw result blocks
for extraction.

Key fixes:
- Small delay between DDG calls to avoid rate limiting
- Queries don't duplicate the location string
- Keywords from expander already contain location —
  don't append it again
"""

import re
import time
import random
from ddgs import DDGS


def search_businesses(
    query:       str,
    max_results: int = 10
) -> str:
    """
    Single search. Returns raw text block.
    Used for small targets (≤ 10).
    """
    try:
        print(
            f"🌐 [WEB RESEARCHER] "
            f"Searching: '{query}'"
        )
        results     = DDGS().text(query, max_results=max_results)
        block_parts = []
        seen_urls   = set()

        for r in results:
            url = r.get("href", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if r.get("body"):
                block_parts.append(
                    f"Title: {r['title']}\n"
                    f"URL: {url}\n"
                    f"Body: {r['body']}\n"
                )

        combined = "\n---\n".join(block_parts)
        print(
            f"✅ [WEB RESEARCHER] "
            f"{len(block_parts)} results"
        )
        return combined

    except Exception as e:
        print(f"❌ [WEB RESEARCHER] Failed: {e}")
        return ""


def _generate_queries(
    industry: str,
    location: str,
    target:   int
) -> list[str]:
    """
    Generates a diverse set of search queries.

    IMPORTANT: If the keyword already contains the
    location (from keyword_expander), we don't append
    it again. We check before combining.
    """
    loc_lower      = location.strip().lower()
    ind_lower      = industry.strip().lower()
    loc_in_keyword = loc_lower in ind_lower

    def q(*parts) -> str:
        """Build query, skip location if already present."""
        base = " ".join(p for p in parts if p)
        if loc_in_keyword:
            return base  # location already in industry string
        return f"{base} {location}".strip()

    queries = []

    # Core queries
    queries += [
        q(industry),
        q(industry, "contact email"),
        q(industry, "owner founder email"),
        q(industry, "website"),
        q("list of", industry),
        q(industry, "directory"),
        q("best", industry),
        q("top", industry),
        q(industry, "small business"),
        q(industry, "independent"),
    ]

    # Email-focused
    queries += [
        q(industry, "email address"),
        q(industry, "contact us"),
        q(industry, "about us"),
        q('"' + industry + '"', "email"),
    ]

    # Location-specific city queries
    # Only add if location isn't already in keyword
    if not loc_in_keyword:
        city_map = {
            "netherlands": [
                "Amsterdam", "Rotterdam", "Utrecht",
                "The Hague", "Eindhoven", "Groningen",
                "Tilburg", "Almere", "Breda", "Nijmegen"
            ],
            "germany": [
                "Berlin", "Munich", "Hamburg",
                "Frankfurt", "Cologne", "Stuttgart",
                "Düsseldorf", "Leipzig", "Dresden"
            ],
            "uk": [
                "London", "Manchester", "Birmingham",
                "Bristol", "Edinburgh", "Leeds",
                "Glasgow", "Liverpool"
            ],
            "france": [
                "Paris", "Lyon", "Marseille",
                "Bordeaux", "Toulouse", "Nice"
            ],
            "japan": [
                "Tokyo", "Osaka", "Kyoto",
                "Yokohama", "Nagoya", "Fukuoka"
            ],
            "australia": [
                "Sydney", "Melbourne", "Brisbane",
                "Perth", "Adelaide"
            ],
            "usa": [
                "New York", "Los Angeles", "Chicago",
                "San Francisco", "Seattle", "Austin",
                "Portland", "Denver"
            ],
        }

        cities = city_map.get(loc_lower, [])
        for city in cities[:5]:  # max 5 cities
            queries.append(f"{industry} {city}")
            queries.append(f"{industry} {city} email")

    # Deduplicate
    seen   = set()
    unique = []
    for q_str in queries:
        q_clean = q_str.strip().lower()
        if q_clean not in seen and len(q_str.strip()) > 3:
            seen.add(q_clean)
            unique.append(q_str.strip())

    print(
        f"🌐 [WEB RESEARCHER] Generated "
        f"{len(unique)} queries for "
        f"'{industry}' (target: {target})"
    )
    return unique


def search_businesses_multi_query(
    industry: str,
    location: str,
    target:   int = 50
) -> list[str]:
    """
    Runs multiple searches and returns a list of
    raw result blocks for extraction.

    Adds a small random delay between requests
    to avoid DuckDuckGo rate limiting.
    """
    queries         = _generate_queries(industry, location, target)
    all_blocks      = []
    seen_urls       = set()
    total_results   = 0
    consecutive_failures = 0

    for i, query in enumerate(queries):
        # Stop if we've hit too many consecutive failures
        if consecutive_failures >= 5:
            print(
                f"⚠️  [WEB RESEARCHER] "
                f"5 consecutive failures — pausing 30s"
            )
            time.sleep(30)
            consecutive_failures = 0

        try:
            # Small delay between requests (0.5–1.5s)
            # Skip delay on first query
            if i > 0:
                time.sleep(random.uniform(0.5, 1.5))

            results     = DDGS().text(query, max_results=12)
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
                all_blocks.append(
                    "\n---\n".join(block_parts)
                )
                consecutive_failures = 0
            else:
                consecutive_failures += 1

            print(
                f"   🔍 '{query[:55]}' → "
                f"{len(block_parts)} results "
                f"(total unique: {total_results})"
            )

        except Exception as e:
            err = str(e).lower()
            if "no results" in err or "ratelimit" in err:
                consecutive_failures += 1
                print(
                    f"   ⚠️  '{query[:40]}' — "
                    f"no results ({consecutive_failures} streak)"
                )
            else:
                print(f"   ❌ Query failed: {e}")
                consecutive_failures += 1
            continue

    print(
        f"✅ [WEB RESEARCHER] "
        f"{total_results} unique results across "
        f"{len(all_blocks)} batches"
    )
    return all_blocks


def search_email_for_business(
    business_name: str,
    website:       str = None,
    location:      str = None
) -> str | None:
    """Targeted email search for a specific business."""
    queries = []

    if website:
        domain = re.sub(
            r'https?://(www\.)?', '', website
        ).split('/')[0]
        queries.append(f'"{business_name}" email {domain}')
        queries.append(f"site:{domain} contact email")

    queries.append(
        f'"{business_name}" '
        f'{location or ""} contact email'.strip()
    )

    email_pattern = re.compile(
        r'[\w\.\-\+]+@[\w\.\-]+\.[a-zA-Z]{2,}'
    )
    junk = [
        "example.com", "test.com", "shopify.com",
        "wixpress.com", "sentry.io", "cloudflare.com",
        "instagram.com", "facebook.com"
    ]

    for q in queries:
        try:
            time.sleep(random.uniform(0.3, 0.8))
            results = DDGS().text(q, max_results=5)
            for r in results:
                text   = (
                    r.get("title", "") + " " +
                    r.get("body",  "")
                )
                emails = email_pattern.findall(text)
                clean  = [
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
            print(f"⚠️  [EMAIL SEARCH] {e}")
            continue

    return None