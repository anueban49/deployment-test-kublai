# FastAPI server + Telegram bot in one process.
#
# `uvicorn main:app` is the single entry point: the lifespan hook boots the
# Telegram bot alongside the HTTP API, so there is no separate
# `python services/tg_bot.py` process to remember. The bot's post_init also
# starts the background loops (org_posts cleanup + hourly auto-fetch), so this
# file must NOT start cleanup_loop itself or posts would be purged twice.
#
# Delivery mode: with a public HTTPS base URL (WEBHOOK_BASE_URL /
# RENDER_EXTERNAL_URL) the bot registers a webhook and receives updates via the
# /telegram/webhook route — no polling, so no "terminated by other getUpdates"
# 409 conflicts. Without one it long-polls (local dev).
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from api.account import router as account_router

# Payment flow temporarily disabled (WIP) — keep the code, just don't wire it up.
# from api.payments import router as payments_router
from api.regional_search import router as regional_search_router
from api.saved import router as saved_router
from api.search import router as search_router
from api.telegram_link import router as telegram_link_router
from api.telegram_webhook import router as telegram_webhook_router
from config import TELEGRAM_WEBHOOK_SECRET, TG_BOT_TOKEN, WEBHOOK_BASE_URL
from services import tg_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not TG_BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN is not set in the environment/.env")

    # run_polling() would block and own the event loop, so drive the
    # Application lifecycle manually inside FastAPI's loop instead.
    tg_app = tg_bot.build_app(TG_BOT_TOKEN)
    await tg_app.initialize()  # also fires post_init -> cleanup + auto-fetch loops
    await tg_app.start()

    webhook_mode = WEBHOOK_BASE_URL.startswith("https://")
    if webhook_mode:
        webhook_url = f"{WEBHOOK_BASE_URL}/telegram/webhook"
        # drop_pending_updates clears any backlog (e.g. from a prior polling
        # instance) so we don't replay old messages on the switch-over.
        await tg_app.bot.set_webhook(
            url=webhook_url,
            secret_token=TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Telegram webhook registered: %s", webhook_url)
    else:
        # Local dev: no public URL, so long-poll. Clear any webhook first, or
        # getUpdates would 409 against the still-registered webhook.
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
        await tg_app.updater.start_polling()
        logger.info("Telegram bot polling started (no WEBHOOK_BASE_URL set)")

    yield  # FastAPI serves HTTP requests here

    logger.info("Shutting down Telegram bot")
    for task in tg_bot._background_tasks:
        task.cancel()
    if tg_app.updater and tg_app.updater.running:
        await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search_router)
app.include_router(regional_search_router)
app.include_router(saved_router)
# app.include_router(payments_router)  # disabled (WIP)
app.include_router(telegram_link_router)
app.include_router(telegram_webhook_router)
app.include_router(account_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    # Entry point for `python main.py` (e.g. on Render): bind 0.0.0.0:$PORT so
    # the platform detects the open port. Under `uvicorn main:app` this block is
    # skipped — pass --host 0.0.0.0 --port $PORT there instead.
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
