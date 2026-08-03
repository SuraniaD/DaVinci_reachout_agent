import re
from ddgs import DDGS
from interaction_log import log_action


def search_businesses(
    query:       str,
    max_results: int = 10
) -> str:
    """
    Single search for small targets (≤10).
    Returns raw text for 70B to process.
    """
    try:
        print(
            f"🌐 [WEB RESEARCHER] Searching: '{query}' "
            f"(max {max_results})"
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
                    f"Query failed: {e}"
                )
                continue

        if not all_results:
            return ""

        combined = "\n---\n".join(all_results)
        print(
            f"✅ [WEB RESEARCHER] "
            f"{len(all_results)} results"
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


def _generate_queries(
    industry: str,
    location: str,
    target:   int
) -> list[str]:
    """
    Generates a large diverse set of search queries
    tailored to the industry and location.

    Strategy:
    - City-by-city queries for regions/countries
    - Sub-category queries (restaurants, brands,
      manufacturers, retailers separately)
    - Contact-focused queries (email, founder, owner)
    - Directory and list queries
    - Japanese/local language hints where applicable

    More queries + more diversity = more unique results
    after deduplication.
    """
    queries   = []
    ind       = industry.strip()
    loc       = location.strip()

    # ── BASE QUERIES ─────────────────────
    queries += [
        f"{ind} {loc}",
        f"{ind} businesses {loc}",
        f"{ind} companies {loc}",
        f"{ind} brands {loc}",
        f"{ind} {loc} contact email",
        f"{ind} {loc} owner founder",
        f"list of {ind} businesses {loc}",
        f"{ind} {loc} directory",
        f"top {ind} {loc}",
        f"best {ind} {loc}",
        f"{ind} {loc} small business",
        f"{ind} startup {loc}",
        f"{ind} {loc} shop store",
        f"{ind} {loc} email contact website",
        f"{ind} producer manufacturer {loc}",
        f"{ind} retailer seller {loc}",
        f"{ind} {loc} independent",
        f"{ind} {loc} SME",
    ]

    loc_lower = loc.lower()

    # ── JAPAN SPECIFIC ────────────────────
    if any(w in loc_lower for w in [
        "japan", "japanese", "tokyo"
    ]):
        cities = [
            "Tokyo", "Osaka", "Kyoto", "Nagoya",
            "Sapporo", "Fukuoka", "Kobe", "Sendai",
            "Hiroshima", "Yokohama", "Kawasaki",
            "Saitama", "Chiba", "Nagano", "Okinawa"
        ]
        for city in cities:
            queries += [
                f"{ind} {city} Japan",
                f"{ind} {city} Japan email",
                f"{ind} restaurant cafe {city}",
                f"{ind} shop brand {city} Japan",
            ]
        # Japanese-market specific terms
        queries += [
            f"plant based food Japan vegan",
            f"vegan restaurant Japan list",
            f"plant based brand Japan online",
            f"Japan vegan food company email",
            f"plant protein Japan manufacturer",
            f"vegan cafe Tokyo Osaka contact",
            f"plant based diet Japan business",
            f"Japan vegetarian vegan brand email",
        ]

    # ── EUROPE SPECIFIC ───────────────────
    elif any(w in loc_lower for w in [
        "europe", "european", "eu"
    ]):
        countries = [
            "UK", "Germany", "Netherlands", "France",
            "Spain", "Italy", "Sweden", "Denmark",
            "Belgium", "Austria", "Switzerland",
            "Norway", "Finland", "Poland", "Portugal"
        ]
        for country in countries:
            queries += [
                f"{ind} {country}",
                f"{ind} {country} contact email",
                f"{ind} business {country} SME",
            ]

    # ── USA SPECIFIC ─────────────────────
    elif any(w in loc_lower for w in [
        "usa", "us", "united states", "america"
    ]):
        cities = [
            "New York", "Los Angeles", "Chicago",
            "San Francisco", "Seattle", "Austin",
            "Portland", "Denver", "Miami", "Boston",
            "Philadelphia", "Atlanta", "Dallas"
        ]
        for city in cities:
            queries += [
                f"{ind} {city}",
                f"{ind} {city} contact email",
            ]

    # ── UK SPECIFIC ──────────────────────
    elif any(w in loc_lower for w in [
        "uk", "united kingdom", "britain", "england"
    ]):
        cities = [
            "London", "Manchester", "Birmingham",
            "Edinburgh", "Bristol", "Leeds",
            "Glasgow", "Liverpool", "Sheffield"
        ]
        for city in cities:
            queries += [
                f"{ind} {city}",
                f"{ind} {city} contact email",
            ]

    # ── AUSTRALIA SPECIFIC ───────────────
    elif "australia" in loc_lower:
        cities = [
            "Sydney", "Melbourne", "Brisbane",
            "Perth", "Adelaide", "Gold Coast"
        ]
        for city in cities:
            queries += [
                f"{ind} {city} Australia",
                f"{ind} {city} contact email",
            ]

    # ── GENERIC COUNTRY/REGION ───────────
    else:
        # For any other location, add capital/major
        # city variants and contact-focused queries
        queries += [
            f"{ind} {loc} capital city",
            f"{ind} {loc} major cities",
            f"{ind} {loc} SME contact email website",
            f"list {ind} {loc} businesses directory",
            f"{ind} {loc} entrepreneur founder email",
        ]

    # ── REMOVE DUPLICATES ─────────────────
    seen    = set()
    unique  = []
    for q in queries:
        q_clean = q.strip().lower()
        if q_clean not in seen:
            seen.add(q_clean)
            unique.append(q.strip())

    print(
        f"🌐 [WEB RESEARCHER] Generated "
        f"{len(unique)} queries for '{ind}' in '{loc}' "
        f"(target: {target})"
    )

    return unique


def search_businesses_multi_query(
    industry: str,
    location: str,
    target:   int = 50
) -> list[str]:
    """
    Generates many diverse search queries and
    returns raw result blocks for 70B processing.

    Each block = one query's results as a string.
    Caller processes blocks until target is reached.

    Key improvements over the old version:
    - City-by-city queries for better coverage
    - Sub-category queries per business type
    - Higher max_results per query (15 not 10)
    - No hard cap on number of queries generated
    """
    queries     = _generate_queries(
        industry=industry,
        location=location,
        target=target
    )

    all_result_blocks = []
    seen_urls         = set()
    total_results     = 0

    for q in queries:
        try:
            # Higher max_results per query = more raw
            # material for the 70B model to extract from
            results     = DDGS().text(q, max_results=15)
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
                f"   🔍 '{q[:55]}' → "
                f"{len(block_parts)} results "
                f"(total unique: {total_results})"
            )

        except Exception as e:
            print(
                f"⚠️  [WEB RESEARCHER] "
                f"Query failed: {e}"
            )
            continue

    print(
        f"✅ [WEB RESEARCHER] "
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
            print(
                f"⚠️  [EMAIL SEARCH] "
                f"Query failed: {e}"
            )
            continue

    return None