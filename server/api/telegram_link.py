# Web account <-> Telegram linking.
#
# Flow: the frontend calls /telegram/link-url with its Supabase JWT -> the
# backend verifies the JWT server-side (api/deps.py) and mints a short-lived
# single-use token -> the user taps the returned t.me deep link -> Telegram
# sends the bot "/start <token>" -> the bot (services/tg_bot.py) consumes the
# token and binds the accounts. The uid comes from the verified JWT, never
# from the request body, so a caller can't mint tokens for someone else.
import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from api.deps import require_web_user
from config import TG_BOT_USERNAME
from db import repo
from services import tg_bot

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telegram/link-url")
async def create_link_url(user_id: str = Depends(require_web_user)):
    bot_username = tg_bot.get_bot_username() or TG_BOT_USERNAME
    if not bot_username:
        raise HTTPException(status_code=503, detail="Telegram bot is not running")

    # 32 hex chars, within Telegram's 64-char limit for /start payloads.
    token = uuid.uuid4().hex
    try:
        await asyncio.to_thread(repo.create_link_token, token, user_id)
    except Exception:
        logger.exception("Storing link token failed")
        raise HTTPException(status_code=502, detail="Database unavailable")

    return {"url": f"https://t.me/{bot_username}?start={token}", "token": token}


@router.get("/telegram/bot-info")
async def bot_info():
    """Public bot identity, so the UI can render t.me links without
    hardcoding the username."""
    bot_username = tg_bot.get_bot_username() or TG_BOT_USERNAME
    if not bot_username:
        raise HTTPException(status_code=503, detail="Telegram bot is not running")
    return {"username": bot_username}


@router.get("/telegram/link-status")
async def link_status(user_id: str = Depends(require_web_user)):
    """Is the calling web account bound to a Telegram user yet? The UI polls
    this after showing the deep link."""
    try:
        tg_user = await asyncio.to_thread(repo.get_user_by_web_id, user_id)
    except Exception:
        logger.exception("Loading link status failed")
        raise HTTPException(status_code=502, detail="Database unavailable")
    return {
        "linked": tg_user is not None,
        "telegram_username": (tg_user or {}).get("username"),
        "bot_invited": bool((tg_user or {}).get("bot_invited")),
    }
