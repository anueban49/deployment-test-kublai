# Web account management for Google (Supabase) users.
#
# A Google user has no Telegram id until they connect the bot, so on the first
# dashboard visit we create a placeholder users row keyed by their web (auth)
# uid — that is what makes the account visible and lets them manage groups /
# settings before linking. When they later invite the bot and share their phone,
# repo.link_telegram_user merges this placeholder into the Telegram row.
import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import require_web_account
from db import repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account")

_GROUP_URL_RE = re.compile(r"facebook\.com/groups/([A-Za-z0-9._\-]+)", re.IGNORECASE)


def _parse_group_ref(text: str) -> tuple[str, str] | None:
    """(group_id, url) from a facebook.com/groups/... link or a bare numeric id."""
    match = _GROUP_URL_RE.search(text or "")
    if match:
        gid = match.group(1)
        return gid, f"https://www.facebook.com/groups/{gid}"
    candidate = (text or "").strip()
    if candidate.isdigit():
        return candidate, f"https://www.facebook.com/groups/{candidate}"
    return None


def _account(acc: dict) -> dict:
    """Resolve (creating if needed) the users row for this web account."""
    return repo.get_or_create_web_account(acc["id"], acc.get("email"))


def _profile(row: dict) -> dict:
    """Public shape the dashboard renders."""
    settings = row.get("settings") or {}
    # user_id differs from the web uid once a Telegram account is linked.
    linked = row.get("user_id") != row.get("web_user_id")
    return {
        "username": row.get("username"),
        "mail": row.get("mail"),
        "phone_verified": bool(row.get("phone_number")),
        "membership_type": row.get("membership_type", "basic"),
        "bot_invited": bool(row.get("bot_invited")),
        "linked": linked,
        "settings": {"watch_enabled": bool(settings.get("watch_enabled", True))},
    }


class UsernameBody(BaseModel):
    username: str


class SettingsBody(BaseModel):
    watch_enabled: bool


class GroupBody(BaseModel):
    group: str  # a facebook.com/groups/... URL or a bare numeric id


@router.get("")
async def get_account(acc: dict = Depends(require_web_account)):

    row = await asyncio.to_thread(_account, acc)
    uid = row["user_id"]
    groups = await asyncio.to_thread(repo.list_user_groups, uid)
    saved = (
        await asyncio.to_thread(repo.list_saved_posts, uid)
        if row.get("membership_type") == "essentials"
        else []
    )
    return {"profile": _profile(row), "groups": groups, "saved": saved}


@router.post("/username")
async def set_username(
    body: UsernameBody, acc: dict = Depends(require_web_account)
):

    username = body.username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3–32 characters: letters, numbers or _.",
        )
    row = await asyncio.to_thread(_account, acc)
    updated = await asyncio.to_thread(repo.set_account_username, row["user_id"], username)
    return {"profile": _profile(updated or row)}


@router.patch("/settings")
async def patch_settings(
    body: SettingsBody, acc: dict = Depends(require_web_account)
):

    row = await asyncio.to_thread(_account, acc)
    if row.get("membership_type") != "essentials":
        raise HTTPException(
            status_code=403, detail="The watch digest is an Essentials feature."
        )
    settings = dict(row.get("settings") or {})
    settings["watch_enabled"] = body.watch_enabled
    updated = await asyncio.to_thread(
        repo.update_account_settings, row["user_id"], settings
    )
    return {"profile": _profile(updated or {**row, "settings": settings})}


@router.post("/groups")
async def add_group(body: GroupBody, acc: dict = Depends(require_web_account)):

    parsed = _parse_group_ref(body.group)
    if not parsed:
        raise HTTPException(
            status_code=422,
            detail="Send a facebook.com/groups/... link or a numeric group id.",
        )
    group_id, group_url = parsed
    row = await asyncio.to_thread(_account, acc)
    await asyncio.to_thread(
        repo.add_user_group, row["user_id"], group_id, group_url
    )
    groups = await asyncio.to_thread(repo.list_user_groups, row["user_id"])
    return {"groups": groups}


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str, acc: dict = Depends(require_web_account)):

    row = await asyncio.to_thread(_account, acc)
    await asyncio.to_thread(repo.remove_user_group, row["user_id"], group_id)
    groups = await asyncio.to_thread(repo.list_user_groups, row["user_id"])
    return {"groups": groups}


@router.delete("/saved/{post_id}")
async def delete_saved(post_id: str, acc: dict = Depends(require_web_account)):

    row = await asyncio.to_thread(_account, acc)
    if row.get("membership_type") != "essentials":
        raise HTTPException(
            status_code=403, detail="Saved posts are an Essentials feature."
        )
    await asyncio.to_thread(repo.delete_saved_post, row["user_id"], post_id)
    saved = await asyncio.to_thread(repo.list_saved_posts, row["user_id"])
    return {"saved": saved}
