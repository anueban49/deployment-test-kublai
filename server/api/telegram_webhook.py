# Telegram webhook receiver.
#
# In production the bot runs in webhook mode (set up in main.py's lifespan):
# Telegram POSTs each update here instead of us long-polling getUpdates. That
# removes the "terminated by other getUpdates request" 409 conflicts that a
# single web service hits, and keeps the process to just the HTTP server.
#
# Security: set_webhook registers a secret token; Telegram echoes it back in the
# X-Telegram-Bot-Api-Secret-Token header, which we verify before trusting the
# body.
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response
from telegram import Update

from config import TELEGRAM_WEBHOOK_SECRET
from services import tg_bot

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
):
    if (
        TELEGRAM_WEBHOOK_SECRET
        and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    application = tg_bot.get_application()
    if application is None:
        # Bot hasn't finished starting; tell Telegram to retry later.
        raise HTTPException(status_code=503, detail="Bot not ready")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(data, application.bot)
    # Hand off to the Application's processor and return 200 immediately so
    # Telegram doesn't retry while our handlers do their (slower) work.
    await application.update_queue.put(update)
    return Response(status_code=200)
