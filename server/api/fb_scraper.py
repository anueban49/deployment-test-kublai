import http.client
import json
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query

from config import RAPID_API_HOST, RAPID_API_KEY

logger = logging.getLogger(__name__)

router = APIRouter()


def _simplify(post: dict) -> dict:
    """Keep only the fields we care about from a raw group post."""
    author = post.get("author") or {}
    return {
        "post_id": post.get("post_id"),
        "url": post.get("url"),
        "message": post.get("message"),
        "message_rich": post.get("message_rich"),
        "timestamp": post.get("timestamp"),
        "author": {
            "id": author.get("id"),
            "name": author.get("name"),
            "url": author.get("url"),
            "profile_picture_url": author.get("profile_picture_url"),
        },
    }


@router.get("/group")
async def get_facebook_posts(
    group_id: str = Query(..., description="Facebook group id to fetch posts from"),
    query: str = Query("", description="Optional text to filter the posts by"),
):
    logger.info("Fetching Facebook group posts: group_id=%r query=%r", group_id, query)

    headers = {
        "x-rapidapi-key": RAPID_API_KEY,
        "x-rapidapi-host": RAPID_API_HOST,
        "Content-Type": "application/json",
    }
    params = {"group_id": group_id, "sorting_order": "CHRONOLOGICAL"}
    path = "/group/posts?" + urlencode(params)

    conn = http.client.HTTPSConnection(RAPID_API_HOST)
    try:
        conn.request("GET", path, headers=headers)
        res = conn.getresponse()
        raw = res.read().decode("utf-8")
    except OSError:
        logger.exception("Request to facebook-scraper3 failed")
        raise HTTPException(status_code=502, detail="Failed to reach upstream API")
    finally:
        conn.close()

    if res.status != 200:
        logger.error("facebook-scraper3 returned %s: %s", res.status, raw)
        raise HTTPException(status_code=res.status, detail=raw)

    # 1. Fetch all posts and trim each to the fields we care about.
    payload = json.loads(raw)
    posts = [_simplify(post) for post in payload.get("posts", [])]

    # 2. If a query is supplied, filter by words appearing in the message text.
    if query:
        needle = query.casefold()
        posts = [
            post for post in posts
            if needle in (post["message"] or "").casefold()
            or needle in (post["message_rich"] or "").casefold()
        ]

    logger.info("Facebook group posts request succeeded (%d posts)", len(posts))
    return {"posts": posts}
