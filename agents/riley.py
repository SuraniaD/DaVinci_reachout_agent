import os
from groq import Groq
from dotenv import load_dotenv
from memory import get_history, add_message
from interaction_log import log_action

load_dotenv()

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# ─────────────────────────────────────────
# RILEY'S PERSONALITY + RULES
# ─────────────────────────────────────────

RILEY_SYSTEM_PROMPT = """
You are Riley, the Outreach Manager at this company.
You are warm, persuasive, and excellent at personalised cold outreach.
You are having a direct conversation with the CEO over Slack DM.

YOUR JOB:
- Write short, personalised outreach emails to business contacts
- Use research provided to you about their business
- Help the CEO think through outreach strategy
- Report back clearly on what you've done

EMAIL WRITING RULES:
- Subject line: short, curiosity-driven, never generic
- Body: maximum 120 words
- Always mention something specific about their business
  (use the research provided — never make things up)
- One clear call to action at the end
- Sign off as: Riley, on behalf of [CEO name]
- Tone: professional but human, never salesy or pushy
- Never use words like "synergy", "leverage", "circle back"

WHEN DRAFTING AN EMAIL respond in EXACTLY this format
and nothing else — no explanation, no preamble:
SUBJECT: <subject line here>
BODY:
<email body here>

FOR NORMAL CONVERSATION:
- Be concise — this is a Slack DM, not an essay
- Ask clarifying questions when tasks are vague
- If someone asks what you've done, summarise clearly
- If budget or send decisions exceed your scope, flag to CEO

COMMANDS YOU UNDERSTAND:
- /reset    → tells the CEO you'll clear your memory
- /status   → summarise recent outreach activity
- /automode on  → confirm auto-send is now on
- /automode off → confirm approval mode is now on
"""


# ─────────────────────────────────────────
# GENERAL CHAT WITH RILEY
# ─────────────────────────────────────────

def chat_with_riley(user_id: str, user_message: str) -> str:
    """
    Handles general back-and-forth conversation.
    Called by app.py for any plain text DM that isn't
    a file upload or a specific command.
    """
    # Get conversation history from memory
    history = get_history("riley", user_id)

    # Save incoming message to memory
    add_message("riley", user_id, "user", user_message)

    # Build full message list for Groq
    # System prompt + history + new message
    messages = history + [{
        "role":    "user",
        "content": user_message
    }]

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + messages,
            max_tokens=500,
            temperature=0.7
        )

        reply = response.choices[0].message.content

        # Save Riley's reply to memory
        add_message("riley", user_id, "assistant", reply)

        return reply

    except Exception as e:
        error_msg = (
            f"Sorry, I hit an error talking to my AI brain: {e}. "
            f"Try again in a moment."
        )
        print(f"❌ Groq error in chat: {e}")
        return error_msg


# ─────────────────────────────────────────
# DRAFT AN OUTREACH EMAIL
# ─────────────────────────────────────────

def draft_outreach_email(
    user_id:       str,
    contact_name:  str,
    business_name: str,
    research:      str
) -> str:
    """
    Drafts a personalised outreach email for one contact.
    Called by outreach_runner.py for each row in the CSV.

    Returns a string in this exact format:
        SUBJECT: <subject line>
        BODY:
        <email body>
    """
    task = f"""
Draft a personalised outreach email for this contact:

Contact name:  {contact_name}
Business name: {business_name}

Research about their business:
{research}

Remember — use the research to make the email specific
to them. Do not use generic filler. Max 120 words in body.
"""

    # Log that we're drafting
    log_action(
        action_type="draft",
        contact_name=contact_name,
        business_name=business_name,
        detail="Drafting personalised email"
    )

    # Get history so Riley has context of the conversation
    history = get_history("riley", user_id)

    # Save this task to memory
    add_message("riley", user_id, "user", task)

    messages = history + [{"role": "user", "content": task}]

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": RILEY_SYSTEM_PROMPT}
            ] + messages,
            max_tokens=400,
            temperature=0.8  # slightly more creative for emails
        )

        draft = response.choices[0].message.content

        # Save draft to memory
        add_message("riley", user_id, "assistant", draft)

        return draft

    except Exception as e:
        print(f"❌ Groq error drafting email: {e}")
        raise Exception(f"Could not draft email: {e}")


# ─────────────────────────────────────────
# PARSE DRAFT INTO SUBJECT + BODY
# ─────────────────────────────────────────

def parse_draft(draft: str) -> tuple[str, str]:
    """
    Splits Riley's draft into subject and body.

    Input:
        SUBJECT: Quick question for GreenLeaf
        BODY:
        Hi Priya, I came across GreenLeaf...

    Returns:
        ("Quick question for GreenLeaf",
         "Hi Priya, I came across GreenLeaf...")
    """
    lines   = draft.strip().split("\n")
    subject = ""
    body_lines = []
    in_body = False

    for line in lines:
        if line.upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
        elif line.upper().startswith("BODY:"):
            in_body = True
            # Sometimes body starts on the same line
            remainder = line.split(":", 1)[1].strip()
            if remainder:
                body_lines.append(remainder)
        elif in_body:
            body_lines.append(line)

    body = "\n".join(body_lines).strip()

    # Fallback — if parsing fails, use whole draft as body
    if not subject:
        subject = f"Reaching out"
    if not body:
        body = draft.strip()

    return subject, body