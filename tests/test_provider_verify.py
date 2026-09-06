"""Tests for provider key detection + the live (read-only) Test/verify path. SDK-free: the
single httpx.get is monkeypatched so no network is touched."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coworker.providers import detect_provider, verify_provider_key


# -- detect_provider ------------------------------------------------------------
@pytest.mark.parametrize(
    "key,expected",
    [
        ("sk-ant-api03-abc", "anthropic"),
        ("AIzaSyAbc123", "gemini"),
        ("sk-proj-abc", "openai"),
        ("sk_live_abc", "openai"),
        ("", None),
        ("   ", None),
        ("nonsense", None),
    ],
)
def test_detect_provider(key, expected):
    assert detect_provider(key) == expected


# -- verify_provider_key: status-code mapping + per-provider request shape -------
def _patch_get(monkeypatch, status=200, capture=None, raise_exc=None):
    def fake_get(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr("httpx.get", fake_get)


def test_verify_openai_ok(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=200, capture=cap)
    assert verify_provider_key("openai", api_key="sk-x") == {"ok": True}
    assert cap["url"] == "https://api.openai.com/v1/models"
    assert cap["headers"]["Authorization"] == "Bearer sk-x"


def test_verify_openai_custom_endpoint(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=200, capture=cap)
    verify_provider_key(
        "openai", api_key="sk-x", base_url="https://gw.example/openai/v1/"
    )
    # trailing slash trimmed, /models appended to the custom endpoint
    assert cap["url"] == "https://gw.example/openai/v1/models"


def test_verify_bad_key_is_invalid(monkeypatch):
    _patch_get(monkeypatch, status=401)
    assert verify_provider_key("openai", api_key="sk-bad") == {
        "ok": False,
        "error": "Invalid API key.",
    }


def test_verify_anthropic_headers(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=200, capture=cap)
    verify_provider_key("anthropic", api_key="sk-ant-x")
    assert cap["url"] == "https://api.anthropic.com/v1/models"
    assert cap["headers"]["x-api-key"] == "sk-ant-x"
    assert "anthropic-version" in cap["headers"]


def test_verify_gemini_key_param(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=200, capture=cap)
    verify_provider_key("gemini", api_key="AIza-x")
    assert cap["params"]["key"] == "AIza-x"


def test_verify_ollama_uses_v1_models_no_key(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=200, capture=cap)
    verify_provider_key("ollama", base_url="http://localhost:11434")
    assert cap["url"] == "http://localhost:11434/v1/models"
    assert "headers" not in cap  # keyless


def test_verify_network_error_is_clean(monkeypatch):
    _patch_get(monkeypatch, raise_exc=ConnectionError("boom"))
    res = verify_provider_key("openai", api_key="sk-x")
    assert res["ok"] is False
    assert "Couldn't reach" in res["error"]


def test_verify_unexpected_status(monkeypatch):
    _patch_get(monkeypatch, status=500)
    res = verify_provider_key("anthropic", api_key="sk-ant-x")
    assert res["ok"] is False
    assert "500" in res["error"]


# -- GET /models 404 回退: 部分网关只暴露 chat/completions (2026-09-06 SCNet 实证) --
def _patch_post(monkeypatch, status=200, capture=None):
    def fake_post(url, **kwargs):
        if capture is not None:
            capture["url"] = url
            capture.update(kwargs)
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr("httpx.post", fake_post)


def test_verify_models_404_falls_back_to_chat_probe(monkeypatch):
    cap: dict = {}
    _patch_get(monkeypatch, status=404)
    _patch_post(monkeypatch, status=200, capture=cap)
    res = verify_provider_key(
        "scnet-like", api_key="sk-x",
        base_url="https://api.scnet.cn/api/llm/v1",
    )
    assert res == {"ok": True}  # 鉴权通过
    assert cap["url"] == "https://api.scnet.cn/api/llm/v1/chat/completions"
    assert cap["json"]["max_tokens"] == 1  # 成本≈0
    assert cap["headers"]["Authorization"] == "Bearer sk-x"


def test_verify_models_404_probe_rejected_key(monkeypatch):
    _patch_get(monkeypatch, status=404)
    _patch_post(monkeypatch, status=401)
    res = verify_provider_key("scnet-like", api_key="sk-bad", base_url="https://x.example/v1")
    assert res == {"ok": False, "error": "Invalid API key."}


def test_verify_models_404_probe_model_error_still_ok(monkeypatch):
    """探针返回 400/404 (模型不存在等业务错误) — 鉴权已通过, 钥匙是好的。"""
    _patch_get(monkeypatch, status=404)
    _patch_post(monkeypatch, status=400)
    assert verify_provider_key("scnet-like", api_key="sk-x", base_url="https://x.example/v1") == {"ok": True}


def test_verify_models_404_probe_500_reports_failure(monkeypatch):
    _patch_get(monkeypatch, status=404)
    _patch_post(monkeypatch, status=500)
    res = verify_provider_key("scnet-like", api_key="sk-x", base_url="https://x.example/v1")
    assert res["ok"] is False
