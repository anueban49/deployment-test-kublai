# QPay payment flow: create invoice -> UI shows QR -> QPay callback (or the
# UI's status poll) -> verify server-to-server -> fulfill (pro membership).
#
# No user auth layer yet (same as api/saved.py): the UI sends user_id in the
# body. The QPay callback is treated as an untrusted hint — anyone can call
# that URL, so payment is only ever confirmed via /v2/payment/check.
import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from config import ESSENTIALS_PRICE_MNT, PUBLIC_BASE_URL
from db import repo
from services import tg_bot
from services.qpay import qpay

logger = logging.getLogger(__name__)

router = APIRouter()


class PaymentCreate(BaseModel):
    user_id: str


def _public_order(order: dict) -> dict:
    """Shape the UI needs; qpay_invoice_id stays server-side."""
    return {
        "id": order["id"],
        "status": order["status"],
        "amount": float(order["amount"]),
        "currency": order["currency"],
        "qr_text": order.get("qr_text"),
        "qr_image": order.get("qr_image"),
    }


@router.post("/payments/create")
async def create_payment(body: PaymentCreate):
    our_invoice_id = str(uuid.uuid4())  # doubles as the unique sender_invoice_no
    callback_url = f"{PUBLIC_BASE_URL}/payments/qpay-callback?ref={our_invoice_id}"

    try:
        result = await qpay.create_invoice(
            amount=ESSENTIALS_PRICE_MNT,
            description=f"KublAI Essentials membership ({our_invoice_id})",
            sender_invoice_no=our_invoice_id,
            callback_url=callback_url,
        )
    except Exception:
        logger.exception("QPay invoice creation failed")
        raise HTTPException(status_code=502, detail="Could not create QPay invoice")

    row = {
        "id": our_invoice_id,
        "user_id": body.user_id,
        "qpay_invoice_id": result["invoice_id"],
        "amount": ESSENTIALS_PRICE_MNT,
        "currency": "MNT",
        "status": "PENDING",
        "qr_text": result.get("qr_text"),
        "qr_image": result.get("qr_image"),
    }
    try:
        order = await asyncio.to_thread(repo.create_payment_order, row)
    except Exception:
        logger.exception("Storing payment order failed")
        raise HTTPException(status_code=502, detail="Database unavailable")

    return {"order": _public_order(order)}


async def _verify_and_fulfill(order: dict) -> dict:
    """Ask QPay whether the invoice is paid; if so, flip the order to PAID
    (atomically) and grant the membership. Returns the up-to-date order."""
    if order["status"] == "PAID":
        return order

    result = await qpay.check_payment(order["qpay_invoice_id"])
    paid_amount = sum(
        float(p.get("payment_amount", 0))
        for p in result.get("rows", [])
        if p.get("payment_status") == "PAID"
    )
    if paid_amount < float(order["amount"]):
        return order

    winner = await asyncio.to_thread(repo.mark_order_paid, order["id"])
    if winner:  # we won the compare-and-set -> fulfill exactly once
        await _fulfill(order)
        return winner
    return {**order, "status": "PAID"}  # someone else fulfilled concurrently


async def _fulfill(order: dict) -> None:
    """Grant Essentials. If the web account is linked to a Telegram user, the
    membership lands on that users row and the bot pings them; otherwise a
    placeholder row keyed by the web uid holds it until they link
    (repo.link_telegram_user merges it then)."""
    tg_user = await asyncio.to_thread(repo.get_user_by_web_id, order["user_id"])
    member_id = tg_user["user_id"] if tg_user else order["user_id"]
    await asyncio.to_thread(repo.set_membership, member_id, "essentials")
    logger.info("Order %s paid; user %s is now essentials", order["id"], member_id)

    bot = tg_bot.get_bot()
    if tg_user and bot:
        try:
            await bot.send_message(
                chat_id=tg_user["user_id"],
                text="✅ Payment confirmed — Essentials membership is active! ⭐",
            )
        except Exception:
            logger.exception("Telegram payment notification failed (non-fatal)")


async def _verify_by_ref(order_id: str) -> None:
    order = await asyncio.to_thread(repo.get_payment_order, order_id)
    if order:
        await _verify_and_fulfill(order)


@router.get("/payments/qpay-callback")
async def qpay_callback(ref: str, background: BackgroundTasks):
    """QPay hits this after payment. Verify in the background and return 200
    fast so QPay doesn't keep retrying."""
    background.add_task(_verify_by_ref, ref)
    return {"ok": True}


@router.get("/payments/{order_id}/status")
async def payment_status(order_id: str):
    """The UI polls this. Actively re-checks QPay while the order is pending,
    so payment is detected even if the callback never reaches us (localhost)."""
    try:
        order = await asyncio.to_thread(repo.get_payment_order, order_id)
    except Exception:
        logger.exception("Loading payment order failed")
        raise HTTPException(status_code=502, detail="Database unavailable")
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")

    if order["status"] == "PENDING":
        try:
            order = await _verify_and_fulfill(order)
        except Exception:
            logger.exception("QPay payment check failed")
            raise HTTPException(status_code=502, detail="Could not check payment status")

    return {"order": _public_order(order), "paid": order["status"] == "PAID"}
