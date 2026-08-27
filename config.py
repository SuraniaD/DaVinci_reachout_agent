import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────
# GROQ MODELS
# ─────────────────────────────────────────

# Used for: keyword expansion, enrichment,
# query parsing, follow-up drafting
FAST_MODEL = "openai/gpt-oss-20b"

# Used for: research extraction, email drafting,
# verification scoring, cross-verify judgment
SMART_MODEL = "openai/gpt-oss-120b"

GROQ_API_KEY_DEXTER = os.environ.get("GROQ_API_KEY_DEXTER")
GROQ_API_KEY_RILEY  = os.environ.get("GROQ_API_KEY_RILEY")

# Single shared key fallback (if you use one key for all)
GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY",
    GROQ_API_KEY_DEXTER or GROQ_API_KEY_RILEY or ""
)

# ─────────────────────────────────────────
# RESEARCH FLOW (Phase A)
# ─────────────────────────────────────────

MAX_CYCLES           = 7    # Hard cap on research iterations
INITIAL_K            = 3    # Starting keyword variants per cycle
MAX_K                = 10   # Max keyword variants per cycle
CROSS_VERIFY_COUNT   = 1    # Passes required (1 fast check + domain gate)
MIN_RESEARCH_SUMMARY = 300  # Chars — enrich if below this
MX_LOOKUP_TIMEOUT    = 5    # Seconds — fail open if exceeded

# ─────────────────────────────────────────
# LEAD SCORING (0–100)
# ─────────────────────────────────────────

SCORE_NAMED_CONTACT     = 20
SCORE_BUSINESS_DOMAIN   = 15
SCORE_WEBSITE_CONFIRMED = 15
SCORE_RICH_SUMMARY      = 15
SCORE_SOCIAL_PRESENCE   = 10
SCORE_SPECIFIC_LOCATION = 10
SCORE_SPECIFIC_INDUSTRY = 10
SCORE_FIRST_PASS_VERIFY = 5

# ─────────────────────────────────────────
# VERIFICATION (Phase B)
# ─────────────────────────────────────────

X1_THRESHOLD       = 0.9   # Stored info similarity target
X2_THRESHOLD       = 0.9   # Fresh online info similarity target
COMBINED_THRESHOLD = 0.81  # x1 * x2 send gate
MAX_DRAFT_RETRIES  = 3     # Regeneration cap before human_review

# ─────────────────────────────────────────
# SENDING
# ─────────────────────────────────────────

DAILY_SEND_CAP  = 80    # Max emails per day
SEND_DELAY_MIN  = 30    # Seconds min between sends
SEND_DELAY_MAX  = 90    # Seconds max between sends
MAX_BOUNCE_RATE = 0.05  # 5% — auto-pause threshold

# ─────────────────────────────────────────
# FOLLOW-UPS
# ─────────────────────────────────────────

FOLLOW_UP_1_DAYS = 7    # Days after original send
FOLLOW_UP_2_DAYS = 14   # Days after original send
MAX_FOLLOW_UPS   = 2    # Total per prospect

# ─────────────────────────────────────────
# BACKGROUND TASKS
# ─────────────────────────────────────────

FOLLOWUP_CHECK_INTERVAL = 3600  # 1 hour

# ─────────────────────────────────────────
# RESEND (email sending)
# ─────────────────────────────────────────

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

SENDER_EMAIL = os.environ.get(
    "GMAIL_SENDER_EMAIL", "riley@davinciai.agency"
)
SENDER_NAME = os.environ.get(
    "GMAIL_SENDER_NAME", "Riley"
)

# ─────────────────────────────────────────
# JUNK EMAIL DOMAINS
# ─────────────────────────────────────────

JUNK_DOMAINS = {
    "example.com", "test.com", "email.com",
    "domain.com", "yoursite.com", "yourcompany.com",
    "wixpress.com", "wix.com",
    "shopify.com", "myshopify.com",
    "squarespace.com", "wordpress.com",
    "weebly.com", "webflow.io",
    "godaddy.com", "namecheap.com",
    "cloudflare.com", "cloudflaressl.com",
    "registrar-admin.com", "domaincontrol.com",
    "networksolutions.com", "register.com",
    "mailchimp.com", "mailgun.com",
    "sendgrid.com", "klaviyo.com",
    "constantcontact.com", "hubspot.com",
    "sentry.io", "bugsnag.com",
    "instagram.com", "facebook.com",
    "twitter.com", "linkedin.com",
    "tiktok.com", "youtube.com",
    "gmail.com", "yahoo.com", "hotmail.com",
    "outlook.com", "icloud.com", "protonmail.com",
    "wolt.com", "deliveroo.com", "ubereats.com",
    "doordash.com", "grubhub.com",
    "tripadvisor.com", "yelp.com",
    "opentable.com", "thefork.com",
    "booking.com", "expedia.com",
    "sfchronicle.com", "nytimes.com",
    "amazon.com", "etsy.com", "ebay.com",
    "greenhouse.io", "lever.co", "workday.com",
    "nyandcompany.com", "pladisglobal.com",
    "takeachef.com", "emporiumarcadebar.com",
}

# ─────────────────────────────────────────
# GENERIC EMAIL PREFIXES (lower score)
# ─────────────────────────────────────────

GENERIC_PREFIXES = {
    "info", "hello", "hi", "contact",
    "support", "team", "admin", "help",
    "office", "mail", "post", "enquiries",
    "enquiry", "general", "noreply", "no-reply",
}