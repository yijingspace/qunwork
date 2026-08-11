"""Tests for P0 建议2 — Telegram inline-keyboard approvals + Mini App tickets.

initData validation is pure crypto (no network, no bot token needed): we sign a
payload with the same official algorithm and check accept/reject paths. The
one-time ticket store and the outbound interactive senders are also exercised
with fakes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from coworker.telegram_webapp import (
    WebAppTickets,
    init_user_id,
    validate_telegram_init_data,
)


def make_init_data(bot_token: str, *, user_id: int = 12345, auth_date=None) -> str:
    """Sign a payload the way the Telegram WebApp SDK does — for test fixtures."""
    user = json.dumps(
        {"id": user_id, "first_name": "Alice", "username": "alice"}
    )
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAH-test",
        "user": user,
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


# -- initData validation ------------------------------------------------------
def test_validate_init_data_ok():
    token = "TEST:BOT_TOKEN"
    data = validate_telegram_init_data(make_init_data(token), token)
    assert data is not None
    assert init_user_id(data) == "12345"


def test_validate_init_data_wrong_token_rejected():
    data = validate_telegram_init_data(
        make_init_data("REAL_TOKEN"), "WRONG_TOKEN"
    )
    assert data is None


def test_validate_init_data_tampered_rejected():
    token = "TEST:BOT_TOKEN"
    init = make_init_data(token)
    # flip a field after signing → signature no longer matches
    tampered = init.replace("username%22%3A+%22alice", "username%22%3A+%22mallory")
    assert validate_telegram_init_data(tampered, token) is None


def test_validate_init_data_stale_rejected():
    token = "TEST:BOT_TOKEN"
    stale = make_init_data(token, auth_date=time.time() - 7200)  # 2h old
    assert validate_telegram_init_data(stale, token) is None


def test_validate_init_data_garbage_rejected():
    assert validate_telegram_init_data("not=query&data", "t") is None
    assert validate_telegram_init_data("", "t") is None


# -- one-time page tickets ----------------------------------------------------
def test_tickets_one_time_and_ttl():
    ts = WebAppTickets(ttl=60)
    ticket = ts.issue("12345")
    assert ts.verify(ticket) == "12345"
    assert ts.verify(ticket) is None  # one-time


def test_tickets_expired():
    ts = WebAppTickets(ttl=0)  # expires immediately
    ticket = ts.issue("12345")
    assert ts.verify(ticket) is None


def test_tickets_unknown():
    assert WebAppTickets().verify("nope") is None


# -- manager ticket flow ------------------------------------------------------
def test_manager_webapp_ticket_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    # not configured → friendly error
    assert manager.telegram_webapp_ticket(make_init_data("T"))["ok"] is False

    manager.secrets.put("telegram:default", {"bot_token": "TEST:BOT_TOKEN"})
    # valid initData for an allowlisted user → ticket
    manager.secrets.put(
        "telegram:default", {"bot_token": "TEST:BOT_TOKEN", "allowed_users": ["12345"]}
    )
    out = manager.telegram_webapp_ticket(make_init_data("TEST:BOT_TOKEN"))
    assert out["ok"] is True
    assert out["url"].startswith("/v1/telegram/webapp?ticket=")
    ticket = out["url"].rsplit("=", 1)[1]
    assert manager.telegram_webapp_verify(ticket)["ok"] is True
    assert manager.telegram_webapp_verify(ticket)["ok"] is False  # consumed

    # wrong token → rejected
    assert manager.telegram_webapp_ticket(make_init_data("OTHER"))["ok"] is False
    # non-allowlisted user → rejected
    not_allowed = manager.telegram_webapp_ticket(
        make_init_data("TEST:BOT_TOKEN", user_id=999)
    )
    assert not_allowed["ok"] is False
    assert "not authorized" in not_allowed["error"]


# -- outbound interactive senders ---------------------------------------------
def test_send_telegram_interactive_payload(monkeypatch):
    captured: dict = {}

    class _Resp:
        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    from coworker.connectors.senders import _send_telegram_interactive
    from coworker.interactions import Button

    res = _send_telegram_interactive(
        "BOT", "chat-1", "Approve?", [Button("Approve", '{"id":"x123","r":"allow"}')]
    )
    assert res.ok is True and res.message_id == "42"
    payload = captured["json"]
    assert payload["chat_id"] == "chat-1"
    kb = payload["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["text"] == "Approve"
    assert kb[0][0]["callback_data"] == '{"id":"x123","r":"allow"}'


def test_update_telegram_message_payload(monkeypatch):
    captured: dict = {}

    class _Resp:
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    from coworker.connectors.senders import _update_telegram_message

    assert _update_telegram_message("BOT", "chat-1", "42", "✅ done") is True
    assert captured["json"] == {
        "chat_id": "chat-1",
        "message_id": 42,
        "text": "✅ done",
    }
