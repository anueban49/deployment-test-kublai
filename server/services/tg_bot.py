# Telegram bot: the entry point of the pipeline. The product is BUYER leads -
# posts by people looking for a place ("I'm looking for a 2bed 1bath apt").
#
# Flow for every text message the user sends:
#   1. AI decides: casual chat (reply directly, stay on topic) or a search
#      route + params                              (services.openai.decide_action)
#   2. search checks the shared org_posts cache FIRST; Apify only on a miss
#   3. fetched posts are AI-classified seller/buyer; ONLY buyer posts are
#      saved to org_posts                          (services.openai.classify_posts)
#   4. only after the buyers are saved do they go into the user's chat
#
# Commands bypass step 1 so the user can force a route:
#   /region <keyword>            -> keyword search for buyer posts
#   /group                       -> latest buyer posts from the user's groups
#   /save <n>, /saved, /unsave <n> -> manage the agent's saved posts
import asyncio
import html
import logging
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone

# When this file is run directly (`python services/tg_bot.py`), `services/` ends
# up on sys.path instead of the server root. Replace that entry with the server
# root: it must NOT stay on the path, or services/openai.py would shadow the
# installed `openai` SDK the moment services.openai does `from openai import …`.
# Harmless when imported as `services.tg_bot` (the normal path via main.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
sys.path.insert(0, os.path.dirname(_HERE))

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import (
    AUTO_FETCH_HOUR,
    AUTO_FETCH_KEYWORD,
    AUTO_FETCH_WINDOW_HOURS,
    BASIC_WEEKLY_REQUESTS,
    ESSENTIALS_HOURLY_REQUESTS,
    FB_SEARCH_LOCATION,
    STALE_POST_DAYS,
    TG_BOT_TOKEN,
    UB_UTC_OFFSET_HOURS,
    WEB_APP_URL,
)
from services.openai import classify_posts, decide_action, filter_posts
from services.cleanup import cleanup_loop
from api.search import get_group_posts
from api.regional_search import search_posts as get_regional_posts
from db import repo

logger = logging.getLogger(__name__)

# Telegram rejects messages longer than 4096 chars; stay under with margin.
MAX_MESSAGE_LEN = 3900
MAX_POSTS_SHOWN = 10
MAX_POST_PREVIEW = 350

WELCOME = (
    "Сайн байна уу! 👋 I find real-estate BUYER leads on Facebook — people "
    "posting that they're looking for a place.\n\n"
    "Ask me things like:\n"
    "  • Any new posts about clients looking for a place?\n"
    "  • What's the latest buyer posts from my groups?\n\n"
    "Commands:\n"
    "/region <keyword> — keyword search for buyer posts around "
    f"{FB_SEARCH_LOCATION}\n"
    "/group — latest buyer posts from your saved groups\n"
    "/save <n> — save post number n from the last results (Essentials)\n"
    "/saved — list your saved posts (Essentials)\n"
    "/unsave <n> — remove post number n from your saved list (Essentials)\n"
    f"/watch — about the Essentials morning digest "
    f"(fresh posts daily at {AUTO_FETCH_HOUR:02d}:00 Ulaanbaatar time)\n"
    "/help — show this message"
)

# Ulaanbaatar local time, used to schedule the daily fetch.
UB_TZ = timezone(timedelta(hours=UB_UTC_OFFSET_HOURS), name="Asia/Ulaanbaatar")

# Inline menu: the two search entry points plus group management.
CB_SEARCH_KEYWORD = "search_keyword"
CB_SEARCH_GROUPS = "search_groups"
CB_ADD_GROUP = "add_group"
CB_LIST_GROUPS = "list_groups"

MENU_TEXT = "What would you like to do?"
MENU_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Search with keyword", callback_data=CB_SEARCH_KEYWORD)],
    [InlineKeyboardButton("🆕 Search latest from groups", callback_data=CB_SEARCH_GROUPS)],
    [
        InlineKeyboardButton("➕ Add Group", callback_data=CB_ADD_GROUP),
        InlineKeyboardButton("📋 My Groups", callback_data=CB_LIST_GROUPS),
    ],
])

# ---------------------------------------------------------------------------
# Data fetchers + the buyer pipeline. Both Apify calls are blocking, so they
# run in a thread to keep the bot responsive while an actor run takes 10-60
# seconds. Fetched posts are AI-classified and ONLY the buyer ones are saved
# to the shared org_posts cache - sellers and noise are dropped. The cache is
# global on purpose: one user's fetch saves everyone else the same Apify run.
# ---------------------------------------------------------------------------

async def _classify_buyers(posts: list[dict]) -> list[dict]:
    """AI-classify posts and keep only the buyer ones (people LOOKING FOR a
    place). This is the bot's product; sellers and noise are discarded."""
    if not posts:
        return []
    types = await asyncio.to_thread(classify_posts, posts)
    return [p for p, t in zip(posts, types) if t == "buyer"]


async def _save_buyers(buyers: list[dict], group_id: str | None = None) -> int:
    """Store buyer posts in the shared org_posts cache. Only posts published
    within the last week (STALE_POST_DAYS) are kept - anything older would be
    purged by the cleanup poller right away. Best-effort: a dead DB must not
    break the user-facing reply."""
    week_ago = datetime.now(timezone.utc).timestamp() - STALE_POST_DAYS * 86400
    buyers = [
        p for p in buyers
        if not p.get("timestamp") or p["timestamp"] >= week_ago
    ]
    if not buyers:
        return 0
    try:
        count = await asyncio.to_thread(
            repo.upsert_org_posts, buyers, ["buyer"] * len(buyers), group_id
        )
        logger.info("Saved %d buyer posts (group_id=%r)", count, group_id)
        return count
    except Exception:
        logger.exception("Saving buyer posts failed (non-fatal)")
        return 0


async def fetch_group_posts(group_id: str, keyword: str) -> list[dict]:
    result = await asyncio.to_thread(get_group_posts, group_id=group_id, query=keyword)
    return result.get("posts", [])


async def fetch_regional_posts(keyword: str) -> list[dict]:
    result = await asyncio.to_thread(get_regional_posts, keyword)
    return result.get("posts", [])


# ---------------------------------------------------------------------------
# Automated daily fetch: once every 24h at AUTO_FETCH_HOUR (Ulaanbaatar time)
# the bot searches the default keyword AND every Facebook group any user has
# added, keeps only posts from the last AUTO_FETCH_WINDOW_HOURS that are not
# yet in org_posts, ingests them and notifies every /watch subscriber.
# ---------------------------------------------------------------------------

async def auto_fetch_once(bot: Bot) -> int:
    """One automated cycle. Returns how many new posts were broadcast."""
    logger.info("Auto-fetch started (keyword=%r)", AUTO_FETCH_KEYWORD)

    # One batch per source: the default keyword search plus every Facebook
    # group any user follows (each group fetched once, however many users
    # added it). Batches keep their group_id so the cache stays attributable.
    batches: list[tuple[str | None, list[dict]]] = []
    result = await asyncio.to_thread(get_regional_posts, AUTO_FETCH_KEYWORD)
    batches.append((None, result.get("posts", [])))

    try:
        group_ids = await asyncio.to_thread(repo.list_all_group_ids)
    except Exception:
        logger.exception("Could not load user groups; fetching keyword only")
        group_ids = []
    for gid in group_ids:
        try:
            group_result = await asyncio.to_thread(
                get_group_posts, group_id=gid, query=""
            )
            batches.append((gid, group_result.get("posts", [])))
        except Exception:
            logger.exception("Auto-fetch of group %s failed; skipping", gid)

    # Classify each batch and keep buyers only - that's all we store or send.
    buyer_batches = [(gid, await _classify_buyers(batch)) for gid, batch in batches]

    # Union of all buyer posts, deduped by post_id.
    posts, seen_ids = [], set()
    for _, batch in buyer_batches:
        for p in batch:
            pid = str(p.get("post_id"))
            if p.get("post_id") and pid not in seen_ids:
                seen_ids.add(pid)
                posts.append(p)

    cutoff = datetime.now(timezone.utc).timestamp() - AUTO_FETCH_WINDOW_HOURS * 3600
    recent = [p for p in posts if p.get("timestamp") and p["timestamp"] >= cutoff]

    # "New" = inside the window AND not already saved by a previous cycle
    # or a user search. Check BEFORE saving, or everything looks old.
    known = await asyncio.to_thread(
        repo.existing_post_ids, [p["post_id"] for p in recent if p.get("post_id")]
    )
    new_posts = [p for p in recent if str(p.get("post_id")) not in known]

    for gid, batch in buyer_batches:
        await _save_buyers(batch, group_id=gid)

    logger.info(
        "Auto-fetch: %d fetched, %d within %dh, %d new",
        len(posts), len(recent), AUTO_FETCH_WINDOW_HOURS, len(new_posts),
    )
    if not new_posts:
        return 0

    # The morning digest is an Essentials-plan feature: every essentials user
    # gets it automatically, no /watch opt-in needed. In a private chat the
    # chat_id equals the Telegram user id.
    recipients = await asyncio.to_thread(repo.list_essentials_user_ids)
    if not recipients:
        logger.info("Auto-fetch: no essentials users to notify")
        return len(new_posts)

    header = (
        f"🔔 {len(new_posts)} new buyer post(s) "
        f"in the last {AUTO_FETCH_WINDOW_HOURS}h:"
    )
    chunks = format_response(new_posts, numbered=False)
    for chat_id in recipients:
        try:
            await bot.send_message(chat_id=chat_id, text=header)
            for chunk in chunks:
                await bot.send_message(
                    chat_id=chat_id, text=chunk,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
        except Forbidden:
            # Blocked the bot: plan membership stays, delivery just fails.
            logger.info("Essentials user %s blocked the bot; skipping", chat_id)
        except Exception:
            logger.exception("Failed to notify essentials user %s", chat_id)
    return len(new_posts)


def _seconds_until_next_run(now: datetime | None = None) -> float:
    """Seconds until the next AUTO_FETCH_HOUR:00 in Ulaanbaatar."""
    now = now or datetime.now(UB_TZ)
    next_run = datetime.combine(now.date(), time(hour=AUTO_FETCH_HOUR), tzinfo=UB_TZ)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def auto_fetch_loop(bot: Bot) -> None:
    logger.info(
        "Auto-fetch scheduler started: daily at %02d:00 Ulaanbaatar time "
        "(UTC+%d), window %d h, keyword %r",
        AUTO_FETCH_HOUR, UB_UTC_OFFSET_HOURS, AUTO_FETCH_WINDOW_HOURS, AUTO_FETCH_KEYWORD,
    )
    while True:
        # Sleep first: a restart during development should not burn an Apify
        # run every time the bot boots, and the fetch always lands at 07:00.
        delay = _seconds_until_next_run()
        logger.info("Next auto-fetch in %.0f min", delay / 60)
        await asyncio.sleep(delay)
        try:
            await auto_fetch_once(bot)
        except Exception:
            logger.exception("Auto-fetch cycle failed; will retry tomorrow")


# ---------------------------------------------------------------------------
# The two search flows. Both are DB-first: buyer posts already in org_posts
# are served from there, and Apify is only hit on a cache miss. Fetched posts
# are classified, the buyers saved, and only then shown to the user.
# ---------------------------------------------------------------------------

async def _require_verified(update: Update) -> bool:
    """Unknown users can't search. Verified = shared their phone number (the
    Telegram contact flow) OR linked a web account. Everyone else gets sent to
    share their number. Fails open on DB errors: verification is a gate, not a
    security boundary, and a dead DB shouldn't brick the bot."""
    user = update.effective_user
    try:
        db_user = await asyncio.to_thread(repo.get_user, str(user.id))
    except Exception:
        logger.exception("Verification lookup failed; letting the user through")
        return True
    if db_user and (db_user.get("phone_number") or db_user.get("web_user_id")):
        return True
    await update.effective_message.reply_text(
        "🔒 You're not verified yet. Send /start and tap “Share my phone "
        "number” to finish signing up.\n\n"
        f"Or sign in on the web and press “Invite the bot”: {WEB_APP_URL}",
        disable_web_page_preview=True,
    )
    return False


async def _check_quota(update: Update) -> bool:
    """Plan gate for data requests. Essentials: request anytime, capped at
    ESSENTIALS_HOURLY_REQUESTS per rolling hour. Basic: at most
    BASIC_WEEKLY_REQUESTS per rolling 7 days. Every allowed request is logged
    (that log IS the quota counter). Fails open on DB errors, same reasoning
    as _require_verified."""
    user_id = str(update.effective_user.id)
    try:
        db_user = await asyncio.to_thread(repo.get_user, user_id)
        if (db_user or {}).get("membership_type") == "essentials":
            used = await asyncio.to_thread(
                repo.count_recent_data_requests, user_id, 0, 1
            )
            if used >= ESSENTIALS_HOURLY_REQUESTS:
                await update.effective_message.reply_text(
                    f"⏳ Hourly limit reached: {ESSENTIALS_HOURLY_REQUESTS} data "
                    "requests per hour on Essentials.\n\n"
                    "Please try again in a little while.",
                )
                return False
        else:
            used = await asyncio.to_thread(repo.count_recent_data_requests, user_id)
            if used >= BASIC_WEEKLY_REQUESTS:
                await update.effective_message.reply_text(
                    f"⏳ Basic plan limit reached: {BASIC_WEEKLY_REQUESTS} data "
                    "requests per week.\n\n"
                    "Upgrade to Essentials to request anytime and get fresh "
                    f"posts every morning at {AUTO_FETCH_HOUR:02d}:00:\n"
                    f"{WEB_APP_URL}",
                    disable_web_page_preview=True,
                )
                return False
        await asyncio.to_thread(repo.record_data_request, user_id)
    except Exception:
        logger.exception("Quota check failed; letting the request through")
    return True


async def _require_essentials(update: Update) -> bool:
    """Gate for Essentials-only commands (saving posts, the /watch digest).
    Basic users get an upgrade prompt. Fails open on DB errors, same reasoning
    as _require_verified."""
    user_id = str(update.effective_user.id)
    try:
        db_user = await asyncio.to_thread(repo.get_user, user_id)
    except Exception:
        logger.exception("Plan check failed; letting the request through")
        return True
    if (db_user or {}).get("membership_type") == "essentials":
        return True
    await update.effective_message.reply_text(
        "⭐ This is an Essentials feature.\n"
        "Upgrade to save posts and get the morning digest of fresh buyer "
        f"posts every day at {AUTO_FETCH_HOUR:02d}:00:\n{WEB_APP_URL}",
        disable_web_page_preview=True,
    )
    return False


async def run_keyword_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    """Buyer posts matching a keyword: cached first, Apify on a miss."""
    if not await _require_verified(update):
        return
    if not await _check_quota(update):
        return
    msg = update.effective_message
    logger.info("Keyword search started: %r", keyword)
    await msg.reply_text("🔎 Searching, this can take up to a minute…")
    try:
        cached = await asyncio.to_thread(repo.list_org_posts, "buyer")
        matches = (
            await asyncio.to_thread(filter_posts, cached, keyword) if cached else []
        )
        if not matches:
            posts = await fetch_regional_posts(keyword)
            buyers = await _classify_buyers(posts)
            await _save_buyers(buyers)
            matches = await asyncio.to_thread(filter_posts, buyers, keyword)
    except Exception as e:
        logger.exception("Keyword search failed")
        await msg.reply_text(f"Something went wrong: {e}")
        return
    await _reply_with_posts(update, context, matches)


async def run_group_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: str | None = None
):
    """Latest buyer posts from the user's saved groups (or one specific
    group). Fresh cached posts are served without touching Apify."""
    if not await _require_verified(update):
        return
    if not await _check_quota(update):
        return
    msg = update.effective_message
    if group_id:
        group_ids = [str(group_id)]
    else:
        try:
            groups = await asyncio.to_thread(
                repo.list_user_groups, str(update.effective_user.id)
            )
        except Exception:
            logger.exception("Loading user groups failed")
            await msg.reply_text("Could not load your groups (database error).")
            return
        group_ids = [g["group_id"] for g in groups]
        if not group_ids:
            await msg.reply_text("No group found, please add group (➕ Add Group).")
            return

    await msg.reply_text("🔎 Getting the latest buyer posts from your group(s)…")
    shown, seen_ids = [], set()
    try:
        for gid in group_ids:
            # Cache hit = buyer posts of this group from the last 24h.
            cached = await asyncio.to_thread(
                repo.list_group_posts_since, gid, 1, 50, "buyer"
            )
            if not cached:
                posts = await fetch_group_posts(gid, keyword="")
                buyers = await _classify_buyers(posts)
                await _save_buyers(buyers, group_id=gid)
                cached = buyers
            for p in cached:
                pid = str(p.get("post_id"))
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    shown.append(p)
    except Exception as e:
        logger.exception("Group search failed")
        await msg.reply_text(f"Something went wrong: {e}")
        return
    await _reply_with_posts(update, context, shown)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_post(post: dict, number: int | None = None) -> str:
    message = (post.get("message") or "").strip()
    if len(message) > MAX_POST_PREVIEW:
        message = message[:MAX_POST_PREVIEW].rstrip() + "…"

    author = (post.get("author") or {}).get("name") or post.get("author_name") or "Unknown"
    prefix = f"{number}. " if number is not None else ""
    parts = [f"{prefix}👤 <b>{html.escape(author)}</b>"]

    ts = post.get("timestamp")
    if ts:
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            parts[0] += f" · {when}"
        except (ValueError, OSError, OverflowError):
            pass

    if message:
        parts.append(html.escape(message))
    url = post.get("url")
    if url:
        parts.append(f'<a href="{html.escape(url)}">Open post</a>')
    return "\n".join(parts)


def format_response(posts: list[dict], numbered: bool = True) -> list[str]:
    """Turn filtered posts into one or more Telegram-sized HTML messages."""
    if not posts:
        return ["No matching posts found. Try different keywords."]

    # These chunks are sent with parse_mode=HTML: a literal <n> would be
    # rejected by Telegram as an unsupported tag, so escape the brackets.
    hint = " Use /save &lt;n&gt; to keep one." if numbered else ""
    chunks, current = [], f"Found {len(posts)} matching post(s).{hint}"
    for i, post in enumerate(posts[:MAX_POSTS_SHOWN], start=1):
        block = _format_post(post, number=i if numbered else None)
        if len(current) + len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    chunks.append(current)
    return chunks


async def _reply_with_posts(
    update: Update, context: ContextTypes.DEFAULT_TYPE, posts: list[dict]
) -> None:
    # Remember what was shown so /save <n> can reference results by number.
    # effective_message: this is also reached from button (callback) updates,
    # where update.message is None.
    context.user_data["last_posts"] = posts[:MAX_POSTS_SHOWN]
    for chunk in format_response(posts):
        await update.effective_message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def log_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log every incoming update to the console (registered in group -1, so it
    runs before the real handlers and never swallows the update)."""
    user = update.effective_user
    msg = update.effective_message
    if user:
        handle = f"@{user.username}, " if user.username else ""
        who = f"{user.full_name} ({handle}id={user.id})"
    else:
        who = "<no user>"
    if msg is not None:
        content = msg.text or msg.caption or "<non-text message>"
        logger.info("Incoming message from %s in chat %s: %r", who, msg.chat_id, content)
    else:
        logger.info("Incoming non-message update from %s: %s", who, type(update).__name__)


async def _bind_web_account(update: Update, token: str) -> None:
    """Deep-link payload of /start: consume the single-use token minted by
    the web app and bind this Telegram user to that web account."""
    user = update.effective_user
    try:
        web_user_id = await asyncio.to_thread(repo.consume_link_token, token)
        if web_user_id is None:
            await update.message.reply_text(
                "⚠️ That link is invalid or expired. Open the website and "
                "generate a new Telegram link."
            )
            return
        # users row must exist before we bind (and so the merge sees fresh data).
        await asyncio.to_thread(
            repo.touch_user, str(user.id), user.username, user.full_name
        )
        linked = await asyncio.to_thread(
            repo.link_telegram_user, str(user.id), web_user_id
        )
    except Exception:
        logger.exception("Linking web account failed")
        await update.message.reply_text(
            "Could not link your account (database error). Try again later."
        )
        return
    text = "✅ Your Telegram is now linked to your web account!"
    if linked.get("membership_type") == "essentials":
        text += "\n⭐ Essentials membership is active on this account."
    await update.message.reply_text(text)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # A payload ("/start <token>") is the web-app account-linking deep link.
    if context.args:
        await _bind_web_account(update, context.args[0])

    # Register / refresh the user row. Best-effort: a dead DB must not make
    # /start fall over.
    db_user = None
    try:
        db_user = await asyncio.to_thread(repo.get_user, str(user.id))
        await asyncio.to_thread(
            repo.touch_user, str(user.id), user.username, user.full_name
        )
    except Exception:
        logger.exception("User registration failed (non-fatal)")

    await update.message.reply_text(WELCOME)
    await update.message.reply_text(MENU_TEXT, reply_markup=MENU_KEYBOARD)

    # Auth: ask for the phone number once. Telegram only lets a user share
    # their own contact via this button, so a received contact is verified.
    # Web-linked accounts are already verified, so skip the prompt for them.
    already_verified = bool(
        (db_user or {}).get("phone_number") or (db_user or {}).get("web_user_id")
    )
    if not already_verified:
        contact_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Share my phone number", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "To finish signing up, please share your phone number "
            "with the button below.",
            reply_markup=contact_keyboard,
        )


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A shared contact: the auth step. Store the phone number on the user."""
    contact = update.message.contact
    user = update.effective_user
    # request_contact only sends the sender's own contact, but a contact can
    # also be attached manually - reject someone else's card.
    if contact.user_id is not None and contact.user_id != user.id:
        await update.message.reply_text(
            "Please share your own contact using the button, not someone else's."
        )
        return
    try:
        await asyncio.to_thread(
            repo.touch_user, str(user.id), user.username, user.full_name
        )
        await asyncio.to_thread(
            repo.set_user_phone, str(user.id), contact.phone_number
        )
    except Exception:
        logger.exception("Saving phone number failed")
        await update.message.reply_text(
            "Could not save your phone number (database error). Try again later."
        )
        return
    await update.message.reply_text(
        "✅ You're registered!", reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text(MENU_TEXT, reply_markup=MENU_KEYBOARD)


# --- "Add Group" / "My Groups" inline menu -------------------------------

_GROUP_URL_RE = re.compile(r"facebook\.com/groups/([A-Za-z0-9._\-]+)", re.IGNORECASE)


def _parse_group_ref(text: str) -> tuple[str, str | None] | None:
    """Extract (group_id, url) from a facebook.com/groups/... link, or accept
    a bare numeric ID. None if the text is neither."""
    match = _GROUP_URL_RE.search(text)
    if match:
        gid = match.group(1)
        return gid, f"https://www.facebook.com/groups/{gid}"
    candidate = text.strip()
    if candidate.isdigit():
        return candidate, f"https://www.facebook.com/groups/{candidate}"
    return None


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline menu presses (the two searches + group management)."""
    query = update.callback_query
    await query.answer()  # stop the button's loading spinner
    user_id = str(query.from_user.id)

    if query.data == CB_SEARCH_KEYWORD:
        # The search runs in handle_message when the keyword arrives.
        context.user_data["awaiting_keyword"] = True
        await query.message.reply_text(
            "Send me the keyword to search for, e.g. 2 өрөө байр."
        )
        return

    if query.data == CB_SEARCH_GROUPS:
        await run_group_search(update, context)
        return

    if query.data == CB_ADD_GROUP:
        # The actual save happens in handle_message when the link arrives.
        context.user_data["awaiting_group"] = True
        await query.message.reply_text(
            "Send me the Facebook group link (facebook.com/groups/...) "
            "or its numeric ID."
        )
        return

    if query.data == CB_LIST_GROUPS:
        try:
            groups = await asyncio.to_thread(repo.list_user_groups, user_id)
        except Exception:
            logger.exception("Listing groups failed")
            await query.message.reply_text("Could not load your groups (database error).")
            return
        if not groups:
            await query.message.reply_text(
                "You have no groups yet. Use ➕ Add Group to add one."
            )
            return
        lines = ["📋 Your groups:"]
        for i, g in enumerate(groups, start=1):
            label = g.get("group_name") or g.get("group_id")
            url = g.get("group_url")
            lines.append(f"{i}. {label}" + (f"\n   {url}" if url else ""))
        await query.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def _save_group_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Second half of the Add Group flow: parse and store the group."""
    parsed = _parse_group_ref(text)
    if not parsed:
        context.user_data["awaiting_group"] = True  # stay in the flow, let them retry
        await update.message.reply_text(
            "That doesn't look like a group link or numeric ID. "
            "Send something like facebook.com/groups/123456789 (or /start to cancel)."
        )
        return
    group_id, group_url = parsed
    user = update.effective_user
    try:
        # users row must exist first: user_groups.user_id references users.
        await asyncio.to_thread(
            repo.touch_user, str(user.id), user.username, user.full_name
        )
        await asyncio.to_thread(
            repo.add_user_group, str(user.id), group_id, group_url
        )
    except Exception:
        logger.exception("Saving group failed")
        await update.message.reply_text("Could not save the group (database error).")
        return
    await update.message.reply_text(
        f"✅ Group added: {group_url}\n"
        "Its latest posts will be fetched automatically every morning at "
        f"{AUTO_FETCH_HOUR:02d}:00.",
        disable_web_page_preview=True,
    )


async def handle_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/region <keyword> — force the keyword search route."""
    keyword = " ".join(context.args or []).strip()
    if not keyword:
        await update.message.reply_text("Usage: /region <keyword>, e.g. /region 2 өрөө байр")
        return
    await run_keyword_search(update, context, keyword)


async def handle_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/group — latest buyer posts from the user's saved groups."""
    await run_group_search(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Free-text message: the AI decides between casual chat (direct reply,
    no search) and one of the two search flows."""
    query = (update.message.text or "").strip()

    # If the user just pressed ➕ Add Group, this message is the group link.
    if context.user_data.pop("awaiting_group", False):
        await _save_group_from_text(update, context, query)
        return

    # If they pressed 🔎 Search with keyword, this message is the keyword.
    if context.user_data.pop("awaiting_keyword", False):
        if query:
            await run_keyword_search(update, context, query)
        else:
            await update.message.reply_text("Send me a keyword to search for.")
        return

    if not query:
        await update.message.reply_text("Send me something to search for.")
        return

    # No "Searching…" acknowledgement yet: a greeting like "hello" must get a
    # chat reply, not a search. The ack is sent inside the search flows.
    try:
        decision = await asyncio.to_thread(decide_action, query)
    except Exception as e:
        logger.exception("Routing failed")
        await update.message.reply_text(f"Something went wrong: {e}")
        return

    if decision["route"] == "chat":
        await update.message.reply_text(
            decision.get("reply") or "How can I help you today?",
            reply_markup=MENU_KEYBOARD,
        )
        return
    if decision["route"] == "group_posts":
        await run_group_search(update, context, group_id=decision.get("group_id"))
        return
    await run_keyword_search(update, context, decision["keyword"] or query)


def _parse_number(args, upper: int) -> int | None:
    """Parse a 1-based index from command args; None if absent/invalid."""
    if not args:
        return None
    try:
        n = int(args[0])
    except ValueError:
        return None
    return n if 1 <= n <= upper else None


async def handle_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/save <n> — save post number n from the last shown results."""
    if not await _require_verified(update):
        return
    if not await _require_essentials(update):
        return
    last_posts = context.user_data.get("last_posts") or []
    if not last_posts:
        await update.message.reply_text("Nothing to save yet — run a search first.")
        return
    n = _parse_number(context.args, len(last_posts))
    if n is None:
        await update.message.reply_text(f"Usage: /save <1-{len(last_posts)}>")
        return
    agent_id = str(update.effective_user.id)
    try:
        await asyncio.to_thread(repo.save_post, agent_id, last_posts[n - 1])
    except Exception:
        logger.exception("/save failed")
        await update.message.reply_text("Could not save the post (database error).")
        return
    await update.message.reply_text(f"✅ Saved post {n}. See them with /saved")


async def handle_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/saved — list this agent's saved posts."""
    if not await _require_verified(update):
        return
    if not await _require_essentials(update):
        return
    agent_id = str(update.effective_user.id)
    try:
        saved = await asyncio.to_thread(repo.list_saved_posts, agent_id)
    except Exception:
        logger.exception("/saved failed")
        await update.message.reply_text("Could not load saved posts (database error).")
        return
    if not saved:
        await update.message.reply_text("You have no saved posts. Use /save <n> after a search.")
        return
    for chunk in format_response(saved, numbered=True):
        await update.message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def handle_unsave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unsave <n> — delete the n-th post of the /saved listing."""
    if not await _require_verified(update):
        return
    if not await _require_essentials(update):
        return
    agent_id = str(update.effective_user.id)
    try:
        saved = await asyncio.to_thread(repo.list_saved_posts, agent_id)
    except Exception:
        logger.exception("/unsave failed")
        await update.message.reply_text("Could not load saved posts (database error).")
        return
    if not saved:
        await update.message.reply_text("You have no saved posts.")
        return
    n = _parse_number(context.args, len(saved))
    if n is None:
        await update.message.reply_text(f"Usage: /unsave <1-{len(saved)}> (see /saved)")
        return
    post_id = saved[n - 1].get("post_id")
    try:
        await asyncio.to_thread(repo.delete_saved_post, agent_id, post_id)
    except Exception:
        logger.exception("/unsave failed")
        await update.message.reply_text("Could not delete the post (database error).")
        return
    await update.message.reply_text(f"🗑 Removed saved post {n}.")


async def handle_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/watch — the morning digest is an Essentials-plan feature that is
    delivered automatically; this command just confirms that for Essentials
    users (Basic users get the upgrade prompt from _require_essentials)."""
    if not await _require_verified(update):
        return
    if not await _require_essentials(update):
        return
    await update.message.reply_text(
        f"🔔 You're on Essentials — every morning at {AUTO_FETCH_HOUR:02d}:00 "
        "I automatically send you the fresh buyer posts. Nothing to set up."
    )


async def handle_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unwatch — kept for backwards compatibility with the old opt-in model."""
    await update.message.reply_text(
        "The morning digest is tied to the Essentials plan now, so there is "
        "no subscription to stop."
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error in Telegram handler", exc_info=context.error)


# Keep references so the loops are not garbage-collected mid-flight.
_background_tasks: list[asyncio.Task] = []

# The live Bot, set once the Application initializes. Other modules (the
# payments router) use it to notify users; None until the bot is up.
_bot: Bot | None = None


def get_bot() -> Bot | None:
    return _bot


def get_bot_username() -> str | None:
    # Bot.username is cached by initialize()'s get_me call.
    return _bot.username if _bot else None


async def _post_init(app: Application) -> None:
    global _bot
    _bot = app.bot
    # Plain asyncio tasks (not app.create_task: PTB warns about tasks created
    # before Application.start and won't await infinite loops at shutdown
    # anyway). Housekeeping purge + the hourly auto-fetch.
    _background_tasks.append(asyncio.create_task(cleanup_loop()))
    _background_tasks.append(asyncio.create_task(auto_fetch_loop(app.bot)))


def build_app(token: str) -> Application:
    app = Application.builder().token(token).post_init(_post_init).build()
    # Group -1 runs before the default group: every update gets logged even if
    # a later handler picks it up (or none does).
    app.add_handler(TypeHandler(Update, log_incoming), group=-1)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_start))
    app.add_handler(CommandHandler("region", handle_region))
    app.add_handler(CommandHandler("group", handle_group))
    app.add_handler(CommandHandler("save", handle_save))
    app.add_handler(CommandHandler("saved", handle_saved))
    app.add_handler(CommandHandler("unsave", handle_unsave))
    app.add_handler(CommandHandler("watch", handle_watch))
    app.add_handler(CommandHandler("unwatch", handle_unwatch))
    # Inline menu buttons + the contact share that completes registration.
    app.add_handler(
        CallbackQueryHandler(
            handle_menu_button,
            pattern=(
                f"^({CB_SEARCH_KEYWORD}|{CB_SEARCH_GROUPS}"
                f"|{CB_ADD_GROUP}|{CB_LIST_GROUPS})$"
            ),
        )
    )
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    # Every non-command text message flows through the AI pipeline.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(handle_error)
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not TG_BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN is not set in the environment/.env")
    logger.info("Starting Telegram bot")
    app = build_app(TG_BOT_TOKEN)
    app.run_polling()


if __name__ == "__main__":
    main()
