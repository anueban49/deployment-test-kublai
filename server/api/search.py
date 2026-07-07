import logging
from datetime import datetime

from apify_client import ApifyClient
from fastapi import APIRouter, HTTPException, Query

from config import APIFY_API_TOKEN, FB_SEARCH_MAX_POSTS

logger = logging.getLogger(__name__)

router = APIRouter()

client = ApifyClient(APIFY_API_TOKEN)

ACTOR_ID = "danek/facebook-search-ppr"


def _to_unix(value) -> int | None:
    """Normalize Apify's ISO-8601 `time` string to unix seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _images(item: dict) -> list[dict]:
    """Pull photo URLs (and any Facebook-provided OCR text) from attachments."""
    images = []
    for att in item.get("attachments") or []:
        if not isinstance(att, dict) or att.get("__typename") != "Photo":
            continue
        media = att.get("image") or att.get("photo_image") or {}
        url = media.get("uri") or att.get("thumbnail")
        if url:
            images.append({"url": url, "ocr_text": att.get("ocrText") or None})
    return images


def _simplify(item: dict) -> dict:
    """Trim a raw Apify group post to the fields we care about."""
    user = item.get("user") or {}
    text = item.get("text") or ""
    author_id = user.get("id")
    return {
        "post_id": item.get("id"),
        "url": item.get("url"),
        "message": text,
        "message_rich": text,
        "timestamp": _to_unix(item.get("time")),
        "images": _images(item),
        "author": {
            "id": author_id,
            "name": user.get("name"),
            "url": f"https://www.facebook.com/{author_id}" if author_id else None,
            "profile_picture_url": user.get("profilePic"),
        },
    }


# Sync endpoint: FastAPI runs it in a threadpool, so the long-running (blocking)
# Apify actor call does not block the event loop.
@router.get("/fbposts")
def get_group_posts(
    group_id: str = Query(..., description="Facebook group id to fetch posts from"),
    query: str = Query("", description="Optional text to filter the posts by"),
    max_posts: int = Query(None, ge=1, le=100, description="Max posts to fetch"),
):
    max_posts = max_posts or FB_SEARCH_MAX_POSTS
    logger.info(
        "Running Apify group scraper: group_id=%r query=%r max_posts=%d",
        group_id, query, max_posts,
    )

    run_input = {
        "startUrls": [{"url": f"https://www.facebook.com/groups/{group_id}"}],
        "resultsLimit": max_posts,
        "viewOption": "CHRONOLOGICAL",
    }

    try:
        run = client.actor(ACTOR_ID).call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
    except Exception:
        logger.exception("Apify actor run failed")
        raise HTTPException(status_code=502, detail="Failed to fetch posts from Apify")

    # 1. Trim each post to the fields we care about.
    posts = [_simplify(item) for item in items]

    # 2. If a query is supplied, filter by words appearing in the message text.
    if query:
        needle = query.casefold()
        posts = [post for post in posts if needle in (post["message"] or "").casefold()]

    logger.info("Apify group scraper succeeded (%d posts)", len(posts))
    return {"posts": posts}
