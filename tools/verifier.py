"""
Verification Agent (Phase B)
Scores every email draft before it's sent.

x1 = how well draft reflects stored business info
x2 = how well draft reflects fresh online info
combined = x1 * x2 — must reach 0.81 to send

Uses Groq SMART_MODEL (70B) as judge.
On failure: diagnoses which dimension failed,
generates targeted feedback for redrafting,
retries up to MAX_DRAFT_RETRIES times.
"""

import re
import time
from groq import Groq
from dataclasses import dataclass
from ddgs import DDGS

from config import (
    SMART_MODEL,
    GROQ_API_KEY,
    X1_THRESHOLD,
    X2_THRESHOLD,
    COMBINED_THRESHOLD,
    MAX_DRAFT_RETRIES,
)


@dataclass
class VerificationResult:
    passed:         bool
    x1_score:       float
    x2_score:       float
    combined_score: float
    feedback:       str
    failure_reason: str
    revision_count: int


def _groq_client() -> Groq:
    return Groq(api_key=GROQ_API_KEY)


def _extract_score(text: str) -> float:
    text = text.strip()
    try:
        val = float(text)
        return max(0.0, min(1.0, val))
    except ValueError:
        pass
    match = re.search(r'(\d+\.?\d*)', text)
    if match:
        val = float(match.group(1))
        if val > 1.0:
            val = val / 100.0
        return max(0.0, min(1.0, val))
    return 0.5


def _get_x1_score(
    research_summary: str,
    draft_body:       str,
    business_name:    str
) -> float:
    clean_draft = re.sub(r'<[^>]+>', '', draft_body)

    prompt = f"""Score how well this outreach email reflects
the business information provided.

Business: {business_name}

Stored business information:
{research_summary[:1500]}

Email draft:
{clean_draft[:800]}

Score from 0.0 to 1.0:
1.0 = every specific claim is supported by the business info
0.9 = mostly accurate with minor generalisations
0.7 = email is generic but not inaccurate
0.5 = email is generic, makes no specific claims
0.3 = email contains claims not in the business info
0.0 = email contradicts the business information

Reply with a single decimal number only. Example: 0.87"""

    for attempt in range(2):
        try:
            response = _groq_client().chat.completions.create(
                model=SMART_MODEL,
                max_tokens=10,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )
            score = _extract_score(
                response.choices[0].message.content
            )
            print(f"   x1 score: {score:.2f}")
            return score
        except Exception as e:
            if "rate_limit" in str(e).lower() \
               and attempt == 0:
                time.sleep(60)
                continue
            print(f"❌ [VERIFIER] x1 error: {e}")
            return 0.5
    return 0.5


def _get_x2_score(
    business_name: str,
    location:      str,
    draft_body:    str
) -> tuple[float, str]:
    clean_draft = re.sub(r'<[^>]+>', '', draft_body)

    # Fresh DuckDuckGo search
    queries     = [
        f'"{business_name}" {location}',
        f'"{business_name}" about products',
    ]
    fresh_parts = []
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
                    fresh_parts.append(
                        f"{r.get('title', '')}: {body}"
                    )
        except Exception as e:
            print(f"⚠️  [VERIFIER] x2 search: {e}")

    if not fresh_parts:
        print("   x2: no fresh results — defaulting 0.75")
        return 0.75, ""

    fresh_info = "\n".join(fresh_parts[:6])[:2000]

    prompt = f"""Score how well this outreach email reflects
what is publicly known about this business online.

Business: {business_name}

Fresh online information:
{fresh_info}

Email draft:
{clean_draft[:800]}

Score from 0.0 to 1.0:
1.0 = email is factually accurate and specific
0.9 = mostly accurate, minor points unverifiable
0.7 = generic email, neither right nor wrong
0.5 = email makes claims unverifiable online
0.3 = email references things that don't match
0.0 = email contains false claims

Reply with a single decimal number only. Example: 0.91"""

    for attempt in range(2):
        try:
            response = _groq_client().chat.completions.create(
                model=SMART_MODEL,
                max_tokens=10,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}]
            )
            score = _extract_score(
                response.choices[0].message.content
            )
            print(f"   x2 score: {score:.2f}")
            return score, fresh_info
        except Exception as e:
            if "rate_limit" in str(e).lower() \
               and attempt == 0:
                time.sleep(60)
                continue
            print(f"❌ [VERIFIER] x2 error: {e}")
            return 0.5, fresh_info
    return 0.5, fresh_info


def _build_feedback(
    x1:               float,
    x2:               float,
    research_summary: str,
    fresh_info:       str,
    business_name:    str
) -> str:
    x1_failed = x1 < X1_THRESHOLD
    x2_failed = x2 < X2_THRESHOLD

    if x1_failed and x2_failed:
        return (
            f"Complete rewrite needed for {business_name}. "
            f"The email is generic and contains unverifiable "
            f"claims. Use ONLY these verified facts:\n"
            f"{research_summary[:600]}"
        )
    elif x1_failed:
        return (
            f"Make this email more specific to what "
            f"{business_name} actually does. Reference:\n"
            f"{research_summary[:600]}"
        )
    elif x2_failed:
        return (
            f"The email mentions things not confirmed online "
            f"about {business_name}. Be more conservative — "
            f"only claim what is clearly verifiable.\n"
            f"Fresh info shows:\n{fresh_info[:400]}"
        )
    return ""


def verify_draft(
    prospect:       dict,
    draft_subject:  str,
    draft_body:     str,
    revision_count: int = 0
) -> VerificationResult:
    """
    Runs x1 + x2 verification on a draft email.
    Returns VerificationResult with scores and feedback.
    """
    business_name    = prospect.get("business_name", "")
    location         = prospect.get("location", "")
    research_summary = prospect.get("research_summary", "")

    print(
        f"🔬 [VERIFIER] '{business_name}' "
        f"(revision {revision_count})"
    )

    x1             = _get_x1_score(
        research_summary, draft_body, business_name
    )
    x2, fresh_info = _get_x2_score(
        business_name, location, draft_body
    )

    combined = round(x1 * x2, 4)
    passed   = combined >= COMBINED_THRESHOLD

    print(
        f"   combined: {combined:.3f} "
        f"({'PASS ✅' if passed else 'FAIL ❌'})"
    )

    feedback       = ""
    failure_reason = ""

    if not passed:
        feedback = _build_feedback(
            x1, x2, research_summary,
            fresh_info, business_name
        )
        failure_reason = (
            f"x1={x1:.2f} x2={x2:.2f} "
            f"combined={combined:.3f} < {COMBINED_THRESHOLD}"
        )
        if revision_count >= MAX_DRAFT_RETRIES:
            failure_reason = (
                f"Max retries ({MAX_DRAFT_RETRIES}). "
                + failure_reason
            )

    return VerificationResult(
        passed=passed,
        x1_score=x1,
        x2_score=x2,
        combined_score=combined,
        feedback=feedback,
        failure_reason=failure_reason,
        revision_count=revision_count
    )


def save_verification_result(
    prospect_id:      str,
    draft_subject:    str,
    draft_body:       str,
    result:           VerificationResult,
    send_status:      str   = "pending",
    gmail_message_id: str   = None,
    gmail_thread_id:  str   = None,
) -> dict | None:
    from database import supabase
    try:
        row = {
            "prospect_id":      prospect_id,
            "draft_subject":    draft_subject,
            "draft_body":       draft_body,
            "x1_score":         float(result.x1_score),
            "x2_score":         float(result.x2_score),
            "combined_score":   float(result.combined_score),
            "passed_threshold": result.passed,
            "revision_count":   result.revision_count,
            "send_status":      send_status,
            "failure_reason":   result.failure_reason or None,
        }
        if gmail_message_id:
            row["gmail_message_id"] = gmail_message_id
        if gmail_thread_id:
            row["gmail_thread_id"] = gmail_thread_id

        res = supabase.table("outreach_emails") \
            .insert(row).execute()

        if res.data:
            return res.data[0]
    except Exception as e:
        print(f"❌ [VERIFIER] Save failed: {e}")
    return None