
import logging

from apify_client import ApifyClient
from fastapi import APIRouter, HTTPException, Query

from config import APIFY_API_TOKEN, FB_SEARCH_LOCATION, FB_SEARCH_MAX_POSTS

logger = logging.getLogger(__name__)

router = APIRouter()

client = ApifyClient(APIFY_API_TOKEN)

ACTOR_ID = "danek/facebook-search-ppr"


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
    """Trim a raw search result to the same shape the group scrapers return."""
    author = item.get("author") or {}
    group = item.get("associated_group") or {}
    return {
        "post_id": item.get("post_id"),
        "url": item.get("url"),
        "message": item.get("message"),
        "message_rich": item.get("message_rich"),
        "timestamp": item.get("timestamp"),
        "images": _images(item),
        "reactions_count": item.get("reactions_count"),
        "comments_count": item.get("comments_count"),
        "group_name": group.get("name") if isinstance(group, dict) else None,
        "author": {
            "id": author.get("id"),
            "name": author.get("name"),
            "url": author.get("url"),
            "profile_picture_url": author.get("profile_picture_url"),
        },
    }


def search_posts(
    keyword: str,
    location: str | None = None,
    max_posts: int | None = None,
    recent_only: bool = True,
) -> dict:
    """Run the Apify regional search actor and return simplified posts.

    Plain function (not the FastAPI handler) so the Telegram bot can call it
    directly without going through HTTP.
    """
    location = location or FB_SEARCH_LOCATION
    max_posts = max_posts or FB_SEARCH_MAX_POSTS
    logger.info(
        "Running Apify regional search: keyword=%r location=%r max_posts=%d",
        keyword, location, max_posts,
    )

    run_input = {
        "query": keyword,
        "search_type": "posts",
        "max_posts": max_posts,
        "recent_posts": True,
        "location": location,
    }

    try:
        run = client.actor(ACTOR_ID).call(run_input=run_input)
        items = list(client.dataset(run.default_dataset_id).iterate_items())
    except Exception:
        logger.exception("Apify regional search actor run failed")
        raise HTTPException(status_code=502, detail="Failed to fetch posts from Apify")

    posts = [_simplify(item) for item in items]
    logger.info("Apify regional search succeeded (%d posts)", len(posts))
    return {"posts": posts}


# Sync endpoint: FastAPI runs it in a threadpool, so the long-running (blocking)
# Apify actor call does not block the event loop.
@router.get("/regional")
def get_regional_posts(
    keyword: str = Query(..., description="Keyword to search Facebook posts for"),
    location: str = Query(None, description=f"Region to search in (default: {FB_SEARCH_LOCATION})"),
    max_posts: int = Query(None, ge=1, le=100, description="Max posts to fetch"),
):
    return search_posts(keyword, location=location, max_posts=max_posts)
