# FastAPI server + Telegram bot in one process.
#
# `uvicorn main:app` is now the single entry point: the lifespan hook boots the
# Telegram bot (polling) alongside the HTTP API, so there is no separate
# `python services/tg_bot.py` process to remember. The bot's post_init also
# starts the background loops (org_posts cleanup + hourly auto-fetch), so this
# file must NOT start cleanup_loop itself or posts would be purged twice.
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.account import router as account_router

# Payment flow temporarily disabled (WIP) — keep the code, just don't wire it up.
# from api.payments import router as payments_router
from api.regional_search import router as regional_search_router
from api.saved import router as saved_router
from api.search import router as search_router
from api.telegram_link import router as telegram_link_router
from config import TG_BOT_TOKEN
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
    await tg_app.updater.start_polling()
    logger.info("Telegram bot polling started inside the server process")

    yield  # FastAPI serves HTTP requests here

    logger.info("Shutting down Telegram bot")
    for task in tg_bot._background_tasks:
        task.cancel()
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
app.include_router(account_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
