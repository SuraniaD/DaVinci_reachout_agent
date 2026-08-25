"""
Keyword Expansion Agent
Generates semantically similar search terms using Groq.
Fires when good_leads < target after a research cycle.
k increases each cycle up to MAX_K.
"""

import json
from groq import Groq
from config import FAST_MODEL, GROQ_API_KEY


def expand_keywords(
    industry:      str,
    location:      str,
    k:             int,
    already_tried: list[str]
) -> list[str]:
    """
    Generates k new search terms for industry x location
    that surface businesses not found in previous cycles.
    Returns a list of clean search query strings.
    """
    client    = Groq(api_key=GROQ_API_KEY)
    tried_str = json.dumps(already_tried)

    prompt = f"""You are helping find business leads online.

Original search:
  Industry: {industry}
  Location: {location}

Already tried these search terms (do not repeat):
{tried_str}

Generate exactly {k} ALTERNATIVE search query strings
that would find DIFFERENT businesses in the same category.

Rules:
- Each term must be different enough to surface new results
- Think about different angles:
  manufacturer vs retailer vs brand vs startup vs distributor
  direct-to-consumer vs wholesale vs B2B
  premium vs budget vs mass-market
  online-only vs physical vs omnichannel
- Include the location in each query
- Keep each query concise (4-8 words)
- Do NOT repeat any term from the already-tried list

Reply with a JSON array of strings only.
No explanation. No preamble. No markdown.
Example: ["vegan leather brands uk", "bio leather manufacturers england"]
"""

    try:
        response = client.chat.completions.create(
            model=FAST_MODEL,
            max_tokens=300,
            temperature=0.7,
            messages=[{"role": "user", "content": prompt}]
        )

        raw   = response.choices[0].message.content.strip()
        raw   = raw.replace("```json", "").replace(
            "```", ""
        ).strip()
        terms = json.loads(raw)

        if not isinstance(terms, list):
            return _fallback_expand(industry, location, k)

        tried_lower = {t.lower() for t in already_tried}
        new_terms   = [
            t for t in terms
            if isinstance(t, str)
            and t.strip().lower() not in tried_lower
        ]

        result = new_terms[:k]
        print(
            f"🔑 [KEYWORD EXPANDER] {len(result)} new "
            f"terms for '{industry}' (k={k}):\n"
            + "\n".join(f"  • {t}" for t in result)
        )
        return result

    except json.JSONDecodeError:
        return _fallback_expand(industry, location, k)
    except Exception as e:
        print(f"❌ [KEYWORD EXPANDER] Error: {e}")
        return _fallback_expand(industry, location, k)


def _fallback_expand(
    industry: str,
    location: str,
    k:        int
) -> list[str]:
    suffixes = [
        "brands", "companies", "businesses",
        "startups", "manufacturers", "retailers",
        "distributors", "suppliers", "producers",
        "shops", "stores"
    ]
    terms = [
        f"{industry} {s} {location}"
        for s in suffixes[:k]
    ]
    print(f"⚠️  [KEYWORD EXPANDER] Fallback: {len(terms)} terms")
    return terms