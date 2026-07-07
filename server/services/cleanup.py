# Polling housekeeping: every CLEANUP_INTERVAL_SECONDS, delete org_posts whose
# last_posted is older than STALE_POST_DAYS days (one week: the shared post
# cache retention). Started by the Telegram bot's post_init; the delete is
# idempotent so overlapping pollers are harmless.
import asyncio
import logging

from config import CLEANUP_INTERVAL_SECONDS, STALE_POST_DAYS
from db import repo

logger = logging.getLogger(__name__)


def purge_once() -> int:
    """One cleanup pass. Returns how many rows were deleted."""
    return repo.purge_stale_org_posts(STALE_POST_DAYS)


async def cleanup_loop() -> None:
    logger.info(
        "Cleanup poller started: purging org_posts older than %d days every %d s",
        STALE_POST_DAYS, CLEANUP_INTERVAL_SECONDS,
    )
    while True:
        try:
            await asyncio.to_thread(purge_once)
        except Exception:
            # DB down or tables missing: log and keep polling.
            logger.exception("Cleanup pass failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
