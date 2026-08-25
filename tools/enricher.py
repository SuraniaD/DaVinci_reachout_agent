"""
Research Summary Enrichment Agent
Fires when research_summary < MIN_RESEARCH_SUMMARY chars.
Runs a targeted second search for the specific business
and summarises via Groq to give Riley richer drafting material.
"""

from groq import Groq
from ddgs import DDGS

from config import FAST_MODEL, GROQ_API_KEY, MIN_RESEARCH_SUMMARY


def enrich_summary(
    prospect: dict,
    industry: str
) -> str:
    """
    Targeted search + Groq summary for thin prospects.
    Returns enriched summary string.
    Only called when len(research_summary) < 300.
    """
    client        = Groq(api_key=GROQ_API_KEY)
    business_name = prospect.get("business_name", "")
    location      = prospect.get("location", "")
    domain        = prospect.get("domain", "")
    existing      = prospect.get("research_summary", "")

    print(
        f"🔍 [ENRICHER] Enriching '{business_name}' "
        f"(current: {len(existing)} chars)"
    )

    queries = [
        f'"{business_name}" about products services',
        f'"{business_name}" {location} who we are',
        f'"{business_name}" founder story',
    ]
    if domain:
        queries.append(f"site:{domain} about")

    raw_results = []
    seen_urls   = set()

    for q in queries:
        try:
            results = DDGS().text(q, max_results=5)
            for r in results:
                url = r.get("href", "")
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                body = r.get("body", "")
                if body:
                    raw_results.append(
                        f"Title: {r.get('title', '')}\n"
                        f"Body: {body}"
                    )
        except Exception as e:
            print(f"⚠️  [ENRICHER] Query failed: {e}")

    if not raw_results:
        print(f"⚠️  [ENRICHER] No results — keeping existing")
        return existing or ""

    combined = "\n---\n".join(raw_results[:8])

    prompt = f"""You are enriching a business research summary
for cold email outreach by DaVinci AI.

Business name: {business_name}
Industry: {industry}
Location: {location}
Existing summary: {existing or "none"}

New search results:
{combined[:3000]}

Write an enriched research summary of 4-6 sentences that:
1. States specifically what this business does
2. Mentions their specific products or services by name
3. Notes their size, growth stage, or team if found
4. Identifies one operational pain point AI could solve
5. Uses ONLY facts from the search results — no invention
6. Written in third person, professional tone

Reply with ONLY the summary text.
No preamble, no label, no quotes.
Minimum {MIN_RESEARCH_SUMMARY} characters.
"""

    try:
        response = client.chat.completions.create(
            model=FAST_MODEL,
            max_tokens=400,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        enriched = response.choices[0].message.content.strip()

        if len(enriched) > len(existing or ""):
            print(
                f"✅ [ENRICHER] '{business_name}': "
                f"{len(existing or '')} → {len(enriched)} chars"
            )
            return enriched

        return existing or enriched

    except Exception as e:
        print(f"❌ [ENRICHER] Groq error: {e}")
        return existing or ""