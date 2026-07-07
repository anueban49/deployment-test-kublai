# Data access for org_posts, saved_posts, users and user_groups
# (Supabase / PostgREST).
#
# All functions are synchronous (supabase-py is sync); async callers run them
# via asyncio.to_thread.
import logging
from datetime import datetime, timedelta, timezone

from db.supabase_client import get_client

logger = logging.getLogger(__name__)

ORG_POSTS = "org_posts"
PAYMENT_ORDERS = "payment_orders"
SAVED_POSTS = "saved_posts"
SUBSCRIBERS = "subscribers"
USERS = "users"
USER_GROUPS = "user_groups"
TELEGRAM_LINK_TOKENS = "telegram_link_tokens"
DATA_REQUESTS = "data_requests"


def _to_row(post: dict, post_type: str | None) -> dict:
    """Flatten a simplified post (api/*_scraper shape) into an org_posts row."""
    author = post.get("author") or {}
    ts = post.get("timestamp")
    if ts:
        last_posted = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    else:
        # No timestamp from the scraper: treat it as fresh so the cleanup
        # poller still ages it out three days from now.
        last_posted = datetime.now(timezone.utc)
    return {
        "post_id": str(post.get("post_id")),
        "url": post.get("url"),
        "message": post.get("message"),
        "message_rich": post.get("message_rich"),
        "timestamp": ts,
        "author_id": author.get("id"),
        "author_name": author.get("name"),
        "author_url": author.get("url"),
        "author_profile_picture_url": author.get("profile_picture_url"),
        "type": post_type,
        "last_posted": last_posted.isoformat(),
    }


def upsert_org_posts(
    posts: list[dict], types: list[str | None], group_id: str | None = None
) -> int:
    """Insert-or-update scraped posts with their seller/buyer classification.
    `group_id` tags posts that came from a Facebook group scrape, so the shared
    cache can later serve "posts of group X" without re-fetching."""
    rows = []
    for post, post_type in zip(posts, types):
        if not post.get("post_id"):
            continue
        row = _to_row(post, post_type)
        if group_id:
            row["group_id"] = str(group_id)
        rows.append(row)
    if not rows:
        return 0
    result = get_client().table(ORG_POSTS).upsert(rows, on_conflict="post_id").execute()
    count = len(result.data or [])
    logger.info("Upserted %d org_posts rows", count)
    return count


def list_group_posts_since(
    group_id: str,
    days: int = 7,
    limit: int = 50,
    post_type: str | None = None,
) -> list[dict]:
    """Posts of one group from the shared cache, newest first. This is the
    read path that saves an Apify run when the data is already there."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query = (
        get_client()
        .table(ORG_POSTS)
        .select("*")
        .eq("group_id", str(group_id))
        .gte("last_posted", cutoff.isoformat())
    )
    if post_type:
        query = query.eq("type", post_type)
    result = query.order("last_posted", desc=True).limit(limit).execute()
    return result.data or []


def purge_stale_org_posts(days: int) -> int:
    """Delete org_posts whose last_posted is older than `days` days ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = (
        get_client()
        .table(ORG_POSTS)
        .delete()
        .lt("last_posted", cutoff.isoformat())
        .execute()
    )
    count = len(result.data or [])
    if count:
        logger.info("Purged %d org_posts older than %s", count, cutoff.isoformat())
    return count


def existing_post_ids(post_ids: list[str]) -> set[str]:
    """Which of these post_ids are already in org_posts (used for dedup)."""
    if not post_ids:
        return set()
    result = (
        get_client()
        .table(ORG_POSTS)
        .select("post_id")
        .in_("post_id", [str(pid) for pid in post_ids])
        .execute()
    )
    return {row["post_id"] for row in result.data or []}


def list_org_posts(post_type: str | None = None, limit: int = 50) -> list[dict]:
    query = get_client().table(ORG_POSTS).select("*").order("last_posted", desc=True).limit(limit)
    if post_type:
        query = query.eq("type", post_type)
    return query.execute().data or []


def save_post(agent_id: str, post: dict) -> dict:
    """Save a post for an agent (idempotent per agent+post)."""
    row = {
        "agent_id": str(agent_id),
        "post_id": str(post.get("post_id")),
        "url": post.get("url"),
        "message": post.get("message"),
        "author_name": (post.get("author") or {}).get("name") or post.get("author_name"),
    }
    result = (
        get_client()
        .table(SAVED_POSTS)
        .upsert(row, on_conflict="agent_id,post_id")
        .execute()
    )
    return (result.data or [row])[0]


def delete_saved_post(agent_id: str, post_id: str) -> bool:
    result = (
        get_client()
        .table(SAVED_POSTS)
        .delete()
        .eq("agent_id", str(agent_id))
        .eq("post_id", str(post_id))
        .execute()
    )
    return bool(result.data)


def add_subscriber(chat_id: str) -> None:
    get_client().table(SUBSCRIBERS).upsert(
        {"chat_id": str(chat_id)}, on_conflict="chat_id"
    ).execute()


def remove_subscriber(chat_id: str) -> bool:
    result = (
        get_client()
        .table(SUBSCRIBERS)
        .delete()
        .eq("chat_id", str(chat_id))
        .execute()
    )
    return bool(result.data)


def list_subscribers() -> list[str]:
    result = get_client().table(SUBSCRIBERS).select("chat_id").execute()
    return [row["chat_id"] for row in result.data or []]


def list_saved_posts(agent_id: str) -> list[dict]:
    result = (
        get_client()
        .table(SAVED_POSTS)
        .select("*")
        .eq("agent_id", str(agent_id))
        .order("saved_at", desc=True)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Users (auth) and their Facebook groups
# ---------------------------------------------------------------------------

def touch_user(
    user_id: str, username: str | None = None, full_name: str | None = None
) -> dict:
    """Register the user on first contact / bump last_accessed on every visit.
    Never touches phone_number (that only changes via set_user_phone). Any call
    here means the user is interacting with the bot, so mark bot_invited."""
    client = get_client()
    row = {
        "user_id": str(user_id),
        "last_accessed": datetime.now(timezone.utc).isoformat(),
        "bot_invited": True,
    }
    if full_name is not None:
        row["full_name"] = full_name
    result = client.table(USERS).upsert(row, on_conflict="user_id").execute()
    user = (result.data or [row])[0]
    # Seed the username from the Telegram @handle, but never overwrite one the
    # account already has (e.g. a Google web user's chosen username): username
    # is stable once set.
    if username and not user.get("username"):
        upd = (
            client.table(USERS)
            .update({"username": username})
            .eq("user_id", str(user_id))
            .is_("username", "null")
            .execute()
        )
        if upd.data:
            user = upd.data[0]
    return user


def set_user_phone(user_id: str, phone_number: str) -> None:
    """Store the phone number the user shared via their Telegram contact."""
    get_client().table(USERS).update({"phone_number": phone_number}).eq(
        "user_id", str(user_id)
    ).execute()


def get_user(user_id: str) -> dict | None:
    result = (
        get_client().table(USERS).select("*").eq("user_id", str(user_id)).execute()
    )
    return (result.data or [None])[0]


def get_or_create_web_account(web_user_id: str, mail: str | None = None) -> dict:
    """The users row for a Google web account. Returns the linked Telegram row
    if the account is already bound, otherwise a placeholder row keyed by the
    web (Supabase auth) uid — created on first dashboard visit so the account
    is visible before the bot is connected. Keeps `mail` fresh."""
    client = get_client()
    web_user_id = str(web_user_id)
    existing = get_user_by_web_id(web_user_id)
    if existing:
        if mail and existing.get("mail") != mail:
            client.table(USERS).update({"mail": mail}).eq(
                "user_id", existing["user_id"]
            ).execute()
            existing["mail"] = mail
        return existing
    # No linked row yet. Upsert on user_id so a payment-created placeholder
    # (keyed by the web uid, but without web_user_id) is enriched rather than
    # duplicated.
    row = {"user_id": web_user_id, "web_user_id": web_user_id}
    if mail:
        row["mail"] = mail
    result = client.table(USERS).upsert(row, on_conflict="user_id").execute()
    return (result.data or [row])[0]


def set_account_username(user_id: str, username: str) -> dict:
    """Set the account's chosen username (the web 'create username' step)."""
    result = (
        get_client()
        .table(USERS)
        .update({"username": username})
        .eq("user_id", str(user_id))
        .execute()
    )
    return (result.data or [{}])[0]


def update_account_settings(user_id: str, settings: dict) -> dict:
    """Replace the dashboard settings JSON (watch toggle, etc.)."""
    result = (
        get_client()
        .table(USERS)
        .update({"settings": settings})
        .eq("user_id", str(user_id))
        .execute()
    )
    return (result.data or [{}])[0]


def add_user_group(
    user_id: str,
    group_id: str,
    group_url: str | None = None,
    group_name: str | None = None,
) -> dict:
    """Save a Facebook group to the user's list (idempotent per user+group)."""
    row = {
        "user_id": str(user_id),
        "group_id": str(group_id),
        "group_url": group_url,
        "group_name": group_name,
    }
    result = (
        get_client()
        .table(USER_GROUPS)
        .upsert(row, on_conflict="user_id,group_id")
        .execute()
    )
    return (result.data or [row])[0]


def list_user_groups(user_id: str) -> list[dict]:
    result = (
        get_client()
        .table(USER_GROUPS)
        .select("*")
        .eq("user_id", str(user_id))
        .order("added_at", desc=True)
        .execute()
    )
    return result.data or []


def remove_user_group(user_id: str, group_id: str) -> bool:
    result = (
        get_client()
        .table(USER_GROUPS)
        .delete()
        .eq("user_id", str(user_id))
        .eq("group_id", str(group_id))
        .execute()
    )
    return bool(result.data)


def list_all_group_ids() -> list[str]:
    """Every distinct group any user follows - the daily fetch iterates these."""
    result = get_client().table(USER_GROUPS).select("group_id").execute()
    return sorted({row["group_id"] for row in result.data or []})


def create_payment_order(row: dict) -> dict:
    result = get_client().table(PAYMENT_ORDERS).insert(row).execute()
    return (result.data or [row])[0]


def get_payment_order(order_id: str) -> dict | None:
    result = (
        get_client().table(PAYMENT_ORDERS).select("*").eq("id", order_id).execute()
    )
    return (result.data or [None])[0]


def mark_order_paid(order_id: str) -> dict | None:
    """PENDING -> PAID exactly once. The status filter makes this an atomic
    compare-and-set, so a duplicate callback or a concurrent status poll can't
    fulfill the same order twice. Returns the row only for the winning caller."""
    result = (
        get_client()
        .table(PAYMENT_ORDERS)
        .update(
            {
                "status": "PAID",
                "paid_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", order_id)
        .eq("status", "PENDING")
        .execute()
    )
    return (result.data or [None])[0]


def set_membership(user_id: str, membership_type: str) -> None:
    """Upsert so paying works even before the user has talked to the bot."""
    get_client().table(USERS).upsert(
        {"user_id": str(user_id), "membership_type": membership_type},
        on_conflict="user_id",
    ).execute()


def record_data_request(user_id: str) -> None:
    """Log one data request (bot search); the basic-plan quota counts these."""
    get_client().table(DATA_REQUESTS).insert({"user_id": str(user_id)}).execute()


def count_recent_data_requests(user_id: str, days: int = 7, hours: int = 0) -> int:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    ).isoformat()
    result = (
        get_client()
        .table(DATA_REQUESTS)
        .select("id", count="exact")
        .eq("user_id", str(user_id))
        .gte("requested_at", cutoff)
        .execute()
    )
    return result.count or 0


def list_essentials_user_ids() -> list[str]:
    """Recipients of the automatic morning digest: Essentials users who have
    not turned the watch digest off in their settings."""
    result = (
        get_client()
        .table(USERS)
        .select("user_id, settings")
        .eq("membership_type", "essentials")
        .execute()
    )
    return [
        row["user_id"]
        for row in result.data or []
        if (row.get("settings") or {}).get("watch_enabled", True)
    ]


def get_user_by_web_id(web_user_id: str) -> dict | None:
    """The Telegram user bound to this web (Supabase auth) account, if any."""
    result = (
        get_client()
        .table(USERS)
        .select("*")
        .eq("web_user_id", web_user_id)
        .execute()
    )
    return (result.data or [None])[0]


def create_link_token(token: str, web_user_id: str) -> None:
    """expires_at is set by the table default (now() + 10 minutes)."""
    get_client().table(TELEGRAM_LINK_TOKENS).insert(
        {"token": token, "user_id": web_user_id}
    ).execute()


def consume_link_token(token: str) -> str | None:
    """Flip `used` in one conditional UPDATE - single-use even if two /start
    taps race, and expired tokens never match. Returns the web (Supabase auth)
    uid, or None if the token is unknown, used or expired."""
    now = datetime.now(timezone.utc).isoformat()
    result = (
        get_client()
        .table(TELEGRAM_LINK_TOKENS)
        .update({"used": True})
        .eq("token", token)
        .eq("used", False)
        .gt("expires_at", now)
        .execute()
    )
    row = (result.data or [None])[0]
    return str(row["user_id"]) if row else None


def link_telegram_user(tg_user_id: str, web_user_id: str) -> dict:
    """Bind a Telegram account to a web account.

    Handles the two awkward cases:
      - re-link: the web account was bound to a different Telegram user before
        -> the old binding is cleared (the unique index demands it);
      - registered/paid on the web before linking: a placeholder users row keyed
        by the web uid -> carry its profile (membership, mail, chosen username,
        settings) and its groups/saved posts over, then delete the placeholder.
    """
    client = get_client()
    tg_user_id = str(tg_user_id)
    web_user_id = str(web_user_id)

    # Free the unique web_user_id index (clears it off the placeholder and any
    # stale binding) before we claim it for the Telegram row.
    client.table(USERS).update({"web_user_id": None}).eq(
        "web_user_id", web_user_id
    ).neq("user_id", tg_user_id).execute()

    placeholder = get_user(web_user_id)
    merging = bool(placeholder and placeholder["user_id"] != tg_user_id)

    row = {"user_id": tg_user_id, "web_user_id": web_user_id}
    if merging:
        # Carry the web account's profile onto the Telegram row. Membership only
        # if it's a paid tier; the others whenever the placeholder has them.
        if placeholder.get("membership_type") and placeholder["membership_type"] != "basic":
            row["membership_type"] = placeholder["membership_type"]
        for field in ("mail", "username", "settings"):
            if placeholder.get(field) is not None:
                row[field] = placeholder[field]

    # Upsert the Telegram row FIRST so user_groups' FK target exists before we
    # reassign child rows off the placeholder.
    result = client.table(USERS).upsert(row, on_conflict="user_id").execute()

    if merging:
        client.table(USER_GROUPS).update({"user_id": tg_user_id}).eq(
            "user_id", web_user_id
        ).execute()
        client.table(SAVED_POSTS).update({"agent_id": tg_user_id}).eq(
            "agent_id", web_user_id
        ).execute()
        client.table(USERS).delete().eq("user_id", web_user_id).execute()

    return (result.data or [row])[0]
