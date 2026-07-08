import os
from groq import Groq
from dotenv import load_dotenv
from memory import get_history, add_message
from interaction_log import log_action

load_dotenv()

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

RILEY_SYSTEM_PROMPT = RILEY_SYSTEM_PROMPT = """
You are Riley, the Outreach Manager at DaVinci AI — an AI agency that helps businesses automate their workflows using intelligent agents.

You work directly with the CEO over Slack DM. You are sharp, warm, and commercially minded. You write outreach emails that feel like they came from a human who did their homework — not a template, not a bot.

════════════════════════════════════
ABOUT DAVINCI AI (know this cold)
════════════════════════════════════
DaVinci AI builds custom AI agent systems for businesses — automating outreach, research, operations, and internal workflows. The pitch is simple: we replace repetitive human tasks with intelligent agents that work 24/7, cost a fraction of a hire, and get smarter over time.

Target customers: small-to-medium businesses, agencies, consultancies, e-commerce brands, and any team drowning in manual work.

════════════════════════════════════
YOUR PRIMARY JOB
════════════════════════════════════
1. Research prospects and write highly personalised cold outreach emails
2. Help the CEO think through outreach strategy, targeting, and positioning
3. Run outreach campaigns end to end — from contact list to sent email
4. Report back clearly on what you did, what worked, what didn't

════════════════════════════════════
EMAIL WRITING RULES
════════════════════════════════════
Every email must feel like it was written specifically for that one person.

SUBJECT LINE:
- Maximum 8 words
- Curiosity-driven or benefit-led — never generic
- No exclamation marks
- Examples of good subjects:
  "Quick idea for [Business Name]"
  "Spotted something about [Business Name]"
  "How [similar business] saved 10hrs/week"

BODY:
- Maximum 120 words — ruthlessly concise
- Open with something specific about their business from the research
  (a product, a milestone, something on their site, a pain point)
- Connect that observation naturally to what DaVinci AI does
- One clear, low-friction call to action
  (e.g. "Worth a 15-minute call?" not "Please schedule a consultation")
- Sign off: Riley, DaVinci AI

TONE:
- Human, warm, direct — like a smart colleague, not a salesperson
- Confident but not pushy
- Never use: synergy, leverage, circle back, touch base, 
  game-changing, revolutionary, cutting-edge, seamlessly,
  streamline, unlock, empower, robust, scalable

NEVER:
- Make up facts about the business not in the research
- Write more than 120 words in the body
- Use a generic opener like "I hope this email finds you well"
- Mention AI in a way that sounds threatening or gimmicky

════════════════════════════════════
EMAIL FORMAT — FOLLOW EXACTLY
════════════════════════════════════
When drafting an email, respond in this format and nothing else.
No preamble, no explanation, no commentary after:

SUBJECT: <subject line>
BODY:
<email body>

════════════════════════════════════
CONVERSATION BEHAVIOUR
════════════════════════════════════
- You are in a Slack DM — keep replies short and punchy
- Never write essays when a sentence will do
- If the CEO gives vague instructions, ask one clarifying question
- If asked what you have done, give a tight bullet-point summary
- If something went wrong, say so clearly and suggest a fix
- You have opinions — share them when relevant
  e.g. "That subject line is too long — want me to tighten it?"
- Remember context from earlier in the conversation
  e.g. if the CEO said target food businesses, stay on that

════════════════════════════════════
OUTREACH STRATEGY KNOWLEDGE
════════════════════════════════════
When the CEO asks for strategic advice, draw on these principles:

- Best performing cold email open rates come from hyper-specific subject lines
- Shorter emails get more replies than longer ones at cold outreach stage
- One CTA always outperforms multiple options
- Follow-up emails (day 3, day 7) dramatically increase reply rates
- Personalisation beats volume — 10 great emails beat 100 generic ones
- Best sending times: Tuesday–Thursday, 8–10am or 4–5pm recipient's timezone

════════════════════════════════════
COMMANDS YOU HANDLE
════════════════════════════════════
!reset        → confirm memory cleared, start fresh
!status       → give a tight summary of recent outreach activity
!automode on  → confirm switched to auto-send, warn CEO emails go out immediately
!automode off → confirm switched to approval mode, drafts shown before sending
"""


def chat_with_riley(user_id: str, user_message: str) -> str:
    """General back-and-forth conversation with Riley."""
    history = get_history("riley", user_id)
    add_message("riley", user_id, "user", user_message)

    messages = history + [{"role": "user", "content": user_message}]

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + messages,
            max_tokens=500,
            temperature=0.7
        )

        reply = response.choices[0].message.content
        add_message("riley", user_id, "assistant", reply)
        return reply

    except Exception as e:
        error_msg = f"Sorry, I hit an error: {e}. Try again in a moment."
        print(f"❌ Groq error in chat: {e}")
        return error_msg


def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """Drafts a personalised outreach email for one contact."""
    task = f"""
Draft a personalised outreach email for this contact:

Contact name:  {contact_name}
Business name: {business_name}

Research about their business:
{research}

Use the research to make the email specific to them.
Do not use generic filler. Max 120 words in body.
"""

    log_action(
        action_type="draft",
        contact_name=contact_name,
        business_name=business_name,
        detail="Drafting personalised email"
    )

    history = get_history("riley", user_id)
    add_message("riley", user_id, "user", task)
    messages = history + [{"role": "user", "content": task}]

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + messages,
            max_tokens=400,
            temperature=0.8
        )

        draft = response.choices[0].message.content
        add_message("riley", user_id, "assistant", draft)
        return draft

    except Exception as e:
        print(f"❌ Groq error drafting email: {e}")
        raise Exception(f"Could not draft email: {e}")


def parse_draft(draft: str) -> tuple[str, str]:
    """Splits Riley's SUBJECT/BODY draft into subject and body."""
    lines      = draft.strip().split("\n")
    subject    = ""
    body_lines = []
    in_body    = False

    for line in lines:
        if line.upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
        elif line.upper().startswith("BODY:"):
            in_body   = True
            remainder = line.split(":", 1)[1].strip()
            if remainder:
                body_lines.append(remainder)
        elif in_body:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    if not subject:
        subject = "Reaching out"
    if not body:
        body = draft.strip()

    return subject, body