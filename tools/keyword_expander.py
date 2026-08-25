"""
Keyword Expansion Agent
Generates semantically similar search terms via Groq.
Fires when good_leads < target after a research cycle.
k increases each cycle up to MAX_K.
"""

import json
import time
from groq import Groq
from config import FAST_MODEL, GROQ_API_KEY


def expand_keywords(
    industry:      str,
    location:      str,
    k:             int,
    already_tried: list[str]
) -> list[str]:
    """
    Generates k new search terms combining industry
    variants WITH the location already baked in.
    Returns ready-to-use search query strings.
    """
    client    = Groq(api_key=GROQ_API_KEY)
    tried_str = json.dumps(already_tried)

    prompt = f"""You are helping find business leads online.

Task: Generate {k} alternative Google search queries
to find {industry} businesses in {location}.

Already tried (do not repeat these):
{tried_str}

Rules:
- Each query must include "{location}" in it
- Each query should approach the category differently:
  try different angles like: restaurant, eatery, dining,
  plant-based, sustainable, organic, wholefood, health food,
  juice bar, smoothie bar, eco, conscious, green, etc.
- Queries should be 3-6 words
- Think what someone would Google to find these businesses

Reply with a JSON array of strings ONLY.
No explanation, no markdown, no extra text.
Example: ["plant based restaurants Amsterdam", "vegan eateries Rotterdam"]
"""

    for attempt in range(2):
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

            # Extract JSON array if wrapped in text
            import re
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                raw = match.group(0)

            terms = json.loads(raw)

            if not isinstance(terms, list):
                return _fallback_expand(industry, location, k)

            tried_lower = {t.lower() for t in already_tried}
            new_terms   = [
                t for t in terms
                if isinstance(t, str)
                and t.strip().lower() not in tried_lower
            ]

            result = [t.strip() for t in new_terms[:k]]
            print(
                f"🔑 [KEYWORD EXPANDER] {len(result)} terms:\n"
                + "\n".join(f"  • {t}" for t in result)
            )
            return result

        except Exception as e:
            if "rate_limit" in str(e).lower() and attempt == 0:
                print("⚠️  [KEYWORD EXPANDER] Rate limit — waiting 60s")
                time.sleep(60)
                continue
            print(f"❌ [KEYWORD EXPANDER] Error: {e}")
            break

    return _fallback_expand(industry, location, k)


def _fallback_expand(
    industry: str,
    location: str,
    k:        int
) -> list[str]:
    """
    Rule-based fallback — generates location-aware queries
    without duplicating location in the term.
    """
    # Extract base industry (strip location if already there)
    import re
    base = re.sub(
        re.escape(location), '', industry,
        flags=re.IGNORECASE
    ).strip().strip(',').strip()

    if not base:
        base = industry

    variants = [
        f"plant based {base} {location}",
        f"vegan {base} {location}",
        f"organic {base} {location}",
        f"sustainable {base} {location}",
        f"healthy {base} {location}",
        f"eco {base} {location}",
        f"{base} restaurant {location}",
        f"{base} eatery {location}",
        f"{base} dining {location}",
        f"{base} food {location}",
    ]

    # Remove any that are the same as what we started with
    result = [v for v in variants if v.strip()][:k]
    print(
        f"⚠️  [KEYWORD EXPANDER] Fallback: "
        f"{len(result)} terms:\n"
        + "\n".join(f"  • {t}" for t in result)
    )
    return result