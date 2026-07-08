from ddgs import DDGS
from interaction_log import log_action


def research_business(business_name: str) -> str:
    """
    Searches the web for a business name.
    Returns a short text summary of the top results.
    """
    try:
        print(f"🔍 Researching: {business_name}...")

        # New ddgs API — no context manager, just call directly
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

        summary = "\n".join(parts)

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