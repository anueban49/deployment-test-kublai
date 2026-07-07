# QPay Merchant V2 client.
#
# Auth layers kept separate on purpose:
#   1. User <-> backend: none yet (user_id in the body, same as the rest of the
#      API — see api/saved.py).
#   2. Backend <-> QPay: client_id/secret -> Bearer token, handled here.
#
# Basic auth is base64("client_id:client_secret") — encoding, not encryption;
# TLS protects it on the wire. QPay's docs warn against re-fetching tokens
# while one is still valid, so the token is cached and only refreshed near
# expiry. Single-process server, so an in-memory cache is enough; move it to
# Redis/DB if this ever runs multiple workers.
import base64
import logging
import time

import httpx

from config import (
    QPAY_BASE_URL,
    QPAY_CLIENT_ID,
    QPAY_CLIENT_SECRET,
    QPAY_INVOICE_CODE,
)

logger = logging.getLogger(__name__)


class QPayClient:
    def __init__(self):
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0  # unix timestamp

    async def _fetch_token(self) -> None:
        if not QPAY_CLIENT_ID or not QPAY_CLIENT_SECRET:
            raise RuntimeError("QPAY_CLIENT_ID / QPAY_CLIENT_SECRET are not set in the environment/.env")
        creds = f"{QPAY_CLIENT_ID}:{QPAY_CLIENT_SECRET}".encode()
        basic = base64.b64encode(creds).decode()
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{QPAY_BASE_URL}/v2/auth/token",
                headers={"Authorization": f"Basic {basic}"},
            )
            r.raise_for_status()
            data = r.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token")
        # expires_in is seconds; refresh 60s early so we never send a dead token
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60

    async def _refresh(self) -> None:
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{QPAY_BASE_URL}/v2/auth/refresh",
                headers={"Authorization": f"Bearer {self._refresh_token}"},
            )
            if r.status_code >= 400:
                # refresh token itself expired -> fall back to full re-auth
                await self._fetch_token()
                return
            data = r.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", self._refresh_token)
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60

    async def _valid_token(self) -> str:
        if self._access_token is None:
            await self._fetch_token()
        elif time.time() >= self._expires_at:
            await self._refresh()
        return self._access_token

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token = await self._valid_token()
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.request(
                method,
                f"{QPAY_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            if r.status_code == 401:
                # token revoked/expired despite our bookkeeping: re-auth once and retry
                await self._fetch_token()
                r = await http.request(
                    method,
                    f"{QPAY_BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    **kwargs,
                )
            r.raise_for_status()
            return r.json()

    async def create_invoice(
        self, *, amount: int, description: str, sender_invoice_no: str, callback_url: str
    ) -> dict:
        # sender_invoice_no MUST be unique per invoice (QPay rejects duplicates).
        # Returns qr_image / qr_text / bank-app deeplink urls to show the user.
        return await self._request("POST", "/v2/invoice", json={
            "invoice_code": QPAY_INVOICE_CODE,
            "sender_invoice_no": sender_invoice_no,
            "invoice_receiver_code": "terminal",
            "invoice_description": description,
            "amount": amount,
            "callback_url": callback_url,
        })

    async def check_payment(self, qpay_invoice_id: str) -> dict:
        # The ONLY source of truth for "was this paid?". Never trust the callback body.
        return await self._request("POST", "/v2/payment/check", json={
            "object_type": "INVOICE",
            "object_id": qpay_invoice_id,
            "offset": {"page_number": 1, "page_limit": 100},
        })


# Shared instance so the cached token is reused across requests.
qpay = QPayClient()
