from ddgs import DDGS
from interaction_log import log_action


def research_business(business_name: str) -> str:
    """
    Searches DuckDuckGo for a business name.
    Returns a text summary capped at 800 characters.

    Cap reason: first 800 chars contain the most
    useful facts. Extra length costs tokens in the
    Groq draft call with minimal quality improvement.

    No LLM call here — pure web search.
    Zero tokens spent in this function.
    """
    try:
        print(f"🔍 Researching: {business_name}...")

        results = DDGS().text(business_name, max_results=4)

        if not results:
            summary = (
                f"No specific information found about "
                f"{business_name} online. "
                f"Write a general but warm outreach email."
            )
            log_action(
                action_type="research",
                business_name=business_name,
                detail="No results found"
            )
            return summary

        parts = []
        for r in results:
            if r.get("body"):
                parts.append(f"{r['title']}: {r['body']}")

        # Cap at 800 characters — ~200 tokens
        summary = "\n".join(parts)[:800]

        log_action(
            action_type="research",
            business_name=business_name,
            detail=summary[:300]
        )

        print(f"✅ Research done — {len(results)} results found")
        return summary

    except Exception as e:
        error_msg = (
            f"Research failed for {business_name}: {str(e)}. "
            f"Write a general but warm outreach email."
        )
        log_action(
            action_type="error",
            business_name=business_name,
            detail=f"Research failed: {str(e)}"
        )
        print(f"⚠️ Research failed for {business_name}: {e}")
        return error_msg