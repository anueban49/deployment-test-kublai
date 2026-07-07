import os

from dotenv import load_dotenv

load_dotenv()

# --- Facebook scrapers ---
RAPID_API_HOST = os.getenv("RAPID_API_HOST")
RAPID_API_KEY = os.getenv("RAPID_API_KEY")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

# Default Facebook group the bot scrapes when the user does not supply one.
FB_GROUP_ID = os.getenv("FB_GROUP_ID", "")

# Regional search defaults (danek/facebook-search-ppr actor).
FB_SEARCH_LOCATION = os.getenv("FB_SEARCH_LOCATION", "Ulaanbaatar, Mongolia")
# NOTE: free Apify accounts are hard-capped at 5 results per run by the actor.
FB_SEARCH_MAX_POSTS = int(os.getenv("FB_SEARCH_MAX_POSTS", "20"))

# --- Telegram bot ---
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
# Fallback for the t.me deep link when the bot is not running in this process
# (normally the username is read from the live bot).
TG_BOT_USERNAME = os.getenv("TG_BOT_USERNAME")
# Where the web UI lives; the bot sends unverified users here to sign in and
# connect their Telegram.
WEB_APP_URL = os.getenv("WEB_APP_URL", "http://localhost:5173")

# Delivery mode. With a public HTTPS base URL the bot registers a webhook (the
# right model for a single web service like Render — no getUpdates polling, so
# no "terminated by other getUpdates" 409s). Without one it falls back to
# long-polling, which is convenient for local dev. Render injects
# RENDER_EXTERNAL_URL automatically; WEBHOOK_BASE_URL overrides it.
WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
).rstrip("/")
# Secret Telegram echoes back in the X-Telegram-Bot-Api-Secret-Token header so
# we can reject forged webhook calls. Derived from the bot token if unset.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
if not TELEGRAM_WEBHOOK_SECRET and TG_BOT_TOKEN:
    import hashlib

    TELEGRAM_WEBHOOK_SECRET = hashlib.sha256(TG_BOT_TOKEN.encode()).hexdigest()[:32]

# --- Google Gemini (google-genai) ---
GENAI_API_KEY = os.getenv("GENAI_API_KEY")
GENAI_MODEL = os.getenv("GENAI_MODEL", "gemini-2.5-flash")

# --- OpenAI (the active AI decision layer) ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# --- Database housekeeping ---
# Org posts whose last_posted is older than this many days get purged, i.e.
# the shared cache keeps one week of posts.
STALE_POST_DAYS = int(os.getenv("STALE_POST_DAYS", "7"))
# How often the cleanup poller wakes up, in seconds.
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "3600"))

# --- Automated fetch (the bot searches on its own and notifies subscribers) ---
AUTO_FETCH_KEYWORD = os.getenv("AUTO_FETCH_KEYWORD", "байр зарна")
# The daily fetch runs once every 24h, at this local hour in Ulaanbaatar.
# Mongolia is UTC+8 year-round (no DST since 2017).
AUTO_FETCH_HOUR = int(os.getenv("AUTO_FETCH_HOUR", "7"))
UB_UTC_OFFSET_HOURS = int(os.getenv("UB_UTC_OFFSET_HOURS", "8"))
# Only posts younger than this many hours count as "new" for notifications.
AUTO_FETCH_WINDOW_HOURS = int(os.getenv("AUTO_FETCH_WINDOW_HOURS", "24"))

# --- QPay (Merchant V2) ---
# Sandbox by default; switch to https://merchant.qpay.mn in production.
QPAY_BASE_URL = os.getenv("QPAY_BASE_URL", "https://merchant-sandbox.qpay.mn")
QPAY_CLIENT_ID = os.getenv("QPAY_CLIENT_ID")
QPAY_CLIENT_SECRET = os.getenv("QPAY_CLIENT_SECRET")  # server-side ONLY
QPAY_INVOICE_CODE = os.getenv("QPAY_INVOICE_CODE")
# Must be publicly reachable for QPay's payment callback (e.g. an ngrok URL in dev).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
# Price of the Essentials membership, in MNT.
ESSENTIALS_PRICE_MNT = int(os.getenv("ESSENTIALS_PRICE_MNT", "10000"))

# --- Plans ---
# Basic plan: this many data requests (bot searches) per rolling 7 days.
BASIC_WEEKLY_REQUESTS = int(os.getenv("BASIC_WEEKLY_REQUESTS", "3"))
# Essentials plan: request anytime, but capped at this many per rolling hour
# (plus the automatic morning digest).
ESSENTIALS_HOURLY_REQUESTS = int(os.getenv("ESSENTIALS_HOURLY_REQUESTS", "10"))

# --- Supabase ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
# The server is a trusted backend, so it uses the service_role/secret key
# (SECRET_KEY) which bypasses row-level security. The publishable/anon key
# (SUPABASE_KEY) is subject to RLS and is only a fallback for local setups.
SUPABASE_SECRET_KEY = os.getenv("SECRET_KEY")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_DB_PASS = os.getenv("SUPABASE_DB_PASS")
