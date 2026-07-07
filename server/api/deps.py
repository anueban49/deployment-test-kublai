# Shared FastAPI dependencies.
import asyncio
import logging

from fastapi import Header, HTTPException

from db.supabase_client import get_client

logger = logging.getLogger(__name__)


async def require_web_user(authorization: str = Header(default="")) -> str:
    """Verify the caller's Supabase JWT server-side and return the auth uid.

    The client cannot forge this: the uid comes from Supabase Auth validating
    the token's signature, not from anything in the request body.
    """
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    jwt = authorization[7:].strip()
    try:
        res = await asyncio.to_thread(get_client().auth.get_user, jwt)
    except Exception:
        logger.info("JWT verification failed", exc_info=True)
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if res is None or res.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return str(res.user.id)


async def require_web_account(authorization: str = Header(default="")) -> dict:
    """Like require_web_user, but returns {'id', 'email'} — the account page
    needs the verified email to store on the users row."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    jwt = authorization[7:].strip()
    try:
        res = await asyncio.to_thread(get_client().auth.get_user, jwt)
    except Exception:
        logger.info("JWT verification failed", exc_info=True)
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if res is None or res.user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"id": str(res.user.id), "email": res.user.email}
