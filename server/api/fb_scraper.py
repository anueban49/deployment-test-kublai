import logging

from apify_client import ApifyClient
from fastapi import APIRouter, HTTPException, Query

from config import APIFY_API_TOKEN, FB_SEARCH_MAX_POSTS

logger = logging.getLogger(__name__)

router = APIRouter()

client = ApifyClient(APIFY_API_TOKEN)

# Group posts scraper. Takes two inputs: `group_url` (string) and
# `max_posts` (integer).
ACTOR_ID = "danek/facebook-groups-posts-scraper"


def _images(item: dict) -> list[dict]:
    """Collect photo URLs from the single `image` field and `album_preview`."""
    images = []
    if item.get("image"):
        images.append({"url": item["image"], "ocr_text": None})
    for photo in item.get("album_preview") or []:
        if isinstance(photo, dict) and photo.get("image_file_uri"):
            images.append({"url": photo["image_file_uri"], "ocr_text": None})
    return images


def _simplify(item: dict) -> dict:
    """Trim a raw danek group post to the same shape the other scrapers return."""
    author = item.get("author") or {}
    return {
        "post_id": item.get("post_id"),
        "url": item.get("url"),
        "message": item.get("message"),
        "message_rich": item.get("message_rich"),
        "timestamp": item.get("timestamp"),
        "images": _images(item),
        "reactions_count": item.get("reactions_count"),
        "comments_count": item.get("comments_count"),
        "author": {
            "id": author.get("id"),
            "name": author.get("name"),
            "url": author.get("url"),
            "profile_picture_url": author.get("profile_picture_url"),
        },
    }


# Sync endpoint: FastAPI runs it in a threadpool, so the long-running (blocking)
# Apify actor call does not block the event loop.
@router.get("/group")
def get_facebook_posts(
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
        "group_url": f"https://www.facebook.com/groups/{group_id}",
        "max_posts": max_posts,
    }

    try:
        run = client.actor(ACTOR_ID).call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
    except Exception:
        logger.exception("Apify group scraper actor run failed")
        raise HTTPException(status_code=502, detail="Failed to fetch posts from Apify")

    # 1. Trim each post to the fields we care about.
    posts = [_simplify(item) for item in items]

    # 2. If a query is supplied, filter by words appearing in the message text.
    if query:
        needle = query.casefold()
        posts = [
            post for post in posts
            if needle in (post["message"] or "").casefold()
            or needle in (post["message_rich"] or "").casefold()
        ]

    logger.info("Apify group scraper succeeded (%d posts)", len(posts))
    return {"posts": posts}
