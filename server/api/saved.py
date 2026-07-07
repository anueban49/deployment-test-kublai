# Saved posts CRUD for the UI. No auth layer yet: the agent identifies itself
# via agent_id (the Telegram user id) in the query/body.
import logging

from fastapi import APIRouter, HTTPException, Query

from db import repo
from db.models import SavedPostCreate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/saved")
def list_saved(agent_id: str = Query(..., description="Agent (Telegram user) id")):
    try:
        return {"saved": repo.list_saved_posts(agent_id)}
    except Exception:
        logger.exception("Listing saved posts failed")
        raise HTTPException(status_code=502, detail="Database unavailable")


@router.post("/saved")
def create_saved(body: SavedPostCreate):
    try:
        saved = repo.save_post(body.agent_id, body.model_dump())
    except Exception:
        logger.exception("Saving post failed")
        raise HTTPException(status_code=502, detail="Database unavailable")
    return {"saved": saved}


@router.delete("/saved/{post_id}")
def delete_saved(post_id: str, agent_id: str = Query(..., description="Agent (Telegram user) id")):
    try:
        deleted = repo.delete_saved_post(agent_id, post_id)
    except Exception:
        logger.exception("Deleting saved post failed")
        raise HTTPException(status_code=502, detail="Database unavailable")
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved post not found")
    return {"deleted": post_id}
