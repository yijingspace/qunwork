"""Telegram Mini App (P0 建议2): WebApp initData validation + one-time tickets.

Telegram Mini Apps authenticate via `initData` signed by the bot token (official
algorithm): secret_key = HMAC_SHA256(key="WebAppData", data=bot_token), then
hash = HMAC_SHA256(key=secret_key, data=the sorted `k=v` pairs joined by "\n").

We verify that hash + auth freshness, then hand out a short-lived one-time
ticket the mini app exchanges for the Inbox page — so the REST endpoints never
re-verify initData on every call, and a leaked page URL can't be reused.

No network here; everything is pure crypto, unit-testable without a bot token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets as _secrets
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qsl

logger = logging.getLogger("coworker.telegram_webapp")

# Telegram Mini App payloads must be fresh — an old initData is a replay.
_MAX_INIT_AGE = 3600.0  # 1 hour
_TICKET_TTL = 300.0  # 5 minutes


def validate_telegram_init_data(init_data: str, bot_token: str) -> Optional[dict[str, Any]]:
    """Verify a Telegram WebApp `initData` string against the bot token.

    Returns the parsed data (including `user` as a JSON string) on success, or
    None if the signature is bad / the payload is stale.
    """
    try:
        pairs = parse_qsl(init_data)
    except Exception:
        return None
    data = dict(pairs)
    got = data.pop("hash", "")
    if not got or not data:
        return None
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, got):
        return None
    try:
        auth_date = float(data.get("auth_date", 0))
    except (TypeError, ValueError):
        return None
    if time.time() - auth_date > _MAX_INIT_AGE:
        return None
    return data


def init_user_id(init_data: dict[str, Any]) -> Optional[str]:
    """The Telegram numeric user id from a validated initData payload."""
    try:
        user = json.loads(init_data.get("user") or "{}")
        uid = user.get("id")
        return str(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


class WebAppTickets:
    """One-time tickets for the Mini App page: `issue` a ticket bound to a
    Telegram user, `verify` consumes it once (short TTL)."""

    def __init__(self, ttl: float = _TICKET_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float]] = {}  # ticket -> (user_id, expiry)

    def issue(self, user_id: str) -> str:
        ticket = _secrets.token_urlsafe(16)
        with self._lock:
            self._tickets[ticket] = (user_id, time.time() + self._ttl)
        return ticket

    def verify(self, ticket: str) -> Optional[str]:
        """Consume one ticket; returns the user_id it was issued to, or None."""
        with self._lock:
            entry = self._tickets.pop(ticket, None)
        if entry is None:
            return None
        user_id, expiry = entry
        if time.time() > expiry:
            return None
        return user_id
