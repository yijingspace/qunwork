"""Model-provider registry — descriptors + a factory, mirroring the connector
(`connectors/descriptors.py`) and web-search (`web/providers.py`) patterns.

A `ProviderDescriptor` declares a provider's UI config `fields` (rendered dynamically by the
GUI, same `to_dict()` shape connectors use) and a `build(profile, secrets)` factory that returns
a `ProviderClient`. The `ProviderRouter` selects a descriptor by the `provider:` prefix of a
model string and builds (and caches) its client from the matching SecretStore profile.

Today: `openai` (the default, with an optional custom endpoint that covers Azure OpenAI's
`/openai/v1` and any OpenAI-compliant gateway), `anthropic` (native Messages API via
`AnthropicProvider`), `gemini` (native Google GenAI API via `GeminiProvider`), and `ollama`
(local, OpenAI-compatible `/v1`). Bedrock/Vertex auth for Claude is future work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .anthropic_provider import AnthropicProvider
from .base import ProviderClient
from .gemini_provider import GeminiProvider
from .openai_provider import OpenAIProvider

DEFAULT_OLLAMA_URL = "http://localhost:11434"


@dataclass(frozen=True)
class ProviderField:
    """One config input for a provider, rendered by the GUI (mirrors connectors' `Field`)."""

    key: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""
    placeholder: str = ""
    # Pre-filled (still editable) form value — e.g. an OpenAI-compatible vendor's official
    # endpoint, so the user only has to paste a key. Distinct from `placeholder` (grey hint).
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "secret": self.secret,
            "required": self.required,
            "help": self.help,
            "placeholder": self.placeholder,
            "default": self.default,
        }


@dataclass(frozen=True)
class ProviderDescriptor:
    """A model provider: its UI fields + a factory that builds its `ProviderClient`."""

    name: str
    title: str
    needs_key: bool
    fields: list[ProviderField]
    build: Callable[[dict[str, Any], Any], ProviderClient] = field(repr=False)
    recommended_model: Optional[str] = (
        None  # pre-filled in the UI; auto-added on configure
    )
    env_key: Optional[str] = (
        None  # env var that can supply the API key (e.g. ANTHROPIC_API_KEY)
    )
    # One-line note under the provider title (e.g. "Connects through X's OpenAI-compatible API").
    blurb: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "needs_key": self.needs_key,
            "fields": [f.to_dict() for f in self.fields],
            "recommended_model": self.recommended_model,
            "blurb": self.blurb,
        }


def _normalize_ollama_url(url: Optional[str]) -> str:
    """Accept `http://host:11434` or `.../v1` and return an OpenAI-compatible base URL.

    Ollama serves its OpenAI-compatible API under `/v1`; the native API lives at the root, so we
    always target `<root>/v1`.
    """
    base = (url or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if not base:
        base = DEFAULT_OLLAMA_URL
    if not base.endswith("/v1"):
        base = base + "/v1"
    return base


def _build_openai(profile: dict[str, Any], secrets: Any) -> ProviderClient:
    # Key resolution stays in OpenAIProvider/resolve_api_key (explicit → env → SecretStore),
    # so we just hand it the SecretStore. An optional custom endpoint (Azure OpenAI /openai/v1,
    # OpenRouter, vLLM, …) comes from the stored profile.
    base_url = ((profile or {}).get("base_url") or "").strip() or None
    return OpenAIProvider(secrets=secrets, base_url=base_url)


def _build_anthropic(profile: dict[str, Any], secrets: Any) -> ProviderClient:
    # Key resolution stays in AnthropicProvider/resolve_api_key (explicit → env → SecretStore),
    # deferred to first call so the provider can be built before a key exists.
    # thinking_budget: hidden profile override — absent/invalid → the default (ON),
    # explicit 0 → off (see DEFAULT_THINKING_BUDGET).
    from .anthropic_provider import DEFAULT_THINKING_BUDGET

    api_key = ((profile or {}).get("api_key") or "").strip() or None
    try:
        thinking_budget = int(str((profile or {}).get("thinking_budget") or "").strip())
    except ValueError:
        thinking_budget = DEFAULT_THINKING_BUDGET
    return AnthropicProvider(
        api_key=api_key, secrets=secrets, thinking_budget=thinking_budget
    )


def _build_gemini(profile: dict[str, Any], secrets: Any) -> ProviderClient:
    # Same deferred-key contract as anthropic (GeminiProvider/resolve_api_key).
    api_key = ((profile or {}).get("api_key") or "").strip() or None
    return GeminiProvider(api_key=api_key, secrets=secrets)


def _build_ollama(profile: dict[str, Any], secrets: Any) -> ProviderClient:
    # Ollama's OpenAI-compatible endpoint ignores the key but the SDK requires a non-empty
    # string, so we pass a placeholder. `base_url` comes from the stored profile (or the default).
    base_url = _normalize_ollama_url((profile or {}).get("base_url"))
    return OpenAIProvider(api_key="ollama", base_url=base_url)


def _openai_compat(vendor: str, default_base_url: str, env_key: Optional[str] = None):
    """Builder factory for vendors reached through their OpenAI-compatible API (Z AI, DeepSeek,
    Kimi, MiniMax, Qwen, xAI, Mistral). The key is resolved from the vendor's OWN profile (or its
    env var) — deliberately NOT from the OpenAI env/SecretStore fallback, so a configured OpenAI
    key is never silently sent to a different vendor's endpoint. Missing key ⇒ fail fast with a
    vendor-named error (these are only built on demand, when one of their models is selected).
    """

    def build(profile: dict[str, Any], secrets: Any) -> ProviderClient:
        base_url = ((profile or {}).get("base_url") or "").strip() or default_base_url
        api_key = ((profile or {}).get("api_key") or "").strip() or (
            os.environ.get(env_key, "").strip() if env_key else ""
        )
        if not api_key:
            raise RuntimeError(
                f"No {vendor} API key configured — add it in Settings ▸ Models."
            )
        return OpenAIProvider(api_key=api_key, base_url=base_url)

    return build


def _compat(
    name: str,
    title: str,
    *,
    base_url: str,
    recommended_model: str,
    env_key: str,
    endpoint_help: str = "",
) -> ProviderDescriptor:
    """Descriptor for an OpenAI-compatible vendor: key + a prefilled, editable endpoint."""
    vendor = title.split(" (")[0]
    return ProviderDescriptor(
        name=name,
        title=title,
        needs_key=True,
        fields=[
            ProviderField(
                "api_key",
                f"{vendor} API key",
                secret=True,
            ),
            ProviderField(
                "base_url",
                "Endpoint",
                required=False,
                default=base_url,
                placeholder=base_url,
                help=endpoint_help
                or f"Prefilled with {vendor}'s official endpoint; edit only for a regional or proxy variant.",
            ),
        ],
        build=_openai_compat(vendor, base_url, env_key),
        recommended_model=recommended_model,
        env_key=env_key,
        blurb=f"Uses {vendor}'s OpenAI-compatible API — the endpoint is prefilled, just add your key.",
    )


DESCRIPTORS: list[ProviderDescriptor] = [
    ProviderDescriptor(
        name="openai",
        title="OpenAI",
        needs_key=True,
        fields=[
            ProviderField(
                "api_key",
                "OpenAI API key",
                secret=True,
                placeholder="sk-…",
            ),
            ProviderField(
                "base_url",
                "Custom endpoint (optional)",
                secret=False,
                required=False,
                placeholder="https://…/openai/v1",
                help="For Azure OpenAI, OpenRouter, vLLM, or any OpenAI-compliant server. Leave blank for api.openai.com.",
            ),
        ],
        build=_build_openai,
        recommended_model="gpt-5.6-sol",
        env_key="OPENAI_API_KEY",
    ),
    ProviderDescriptor(
        name="anthropic",
        title="Claude (Anthropic)",
        needs_key=True,
        fields=[
            ProviderField(
                "api_key",
                "Anthropic API key",
                secret=True,
                placeholder="sk-ant-…",
            ),
            # No thinking_budget field (owner call 2026-07-23): extended thinking is
            # on by default; the profile key stays a hidden override (0 = off).
        ],
        build=_build_anthropic,
        recommended_model="claude-fable-5",
        env_key="ANTHROPIC_API_KEY",
    ),
    ProviderDescriptor(
        name="gemini",
        title="Gemini (Google)",
        needs_key=True,
        fields=[
            ProviderField(
                "api_key",
                "Gemini API key",
                secret=True,
                placeholder="AIza…",
            ),
        ],
        build=_build_gemini,
        recommended_model="gemini-3.6-flash",
        env_key="GEMINI_API_KEY",
    ),
    # OpenAI-compatible vendors, listed as first-class providers so users don't need to know the
    # "point the OpenAI slot at a different endpoint" trick (owner call, 2026-07-04). Each keeps
    # its own key profile; the endpoint is prefilled and editable (regional variants in `help`).
    _compat(
        "zai",
        "Z AI (GLM)",
        base_url="https://api.z.ai/api/paas/v4",
        recommended_model="glm-5.2",
        env_key="ZAI_API_KEY",
        endpoint_help="Prefilled with Z AI's international endpoint. China mainland: https://open.bigmodel.cn/api/paas/v4",
    ),
    _compat(
        "deepseek",
        "DeepSeek",
        base_url="https://api.deepseek.com",
        recommended_model="deepseek-v4-flash",
        env_key="DEEPSEEK_API_KEY",
    ),
    _compat(
        "xiaomi",
        "小米 MiMo",
        base_url="https://api.xiaomimimo.com/v1",
        recommended_model="MiMo-7B-RL",
        env_key="XIAOMI_MIMO_API_KEY",
        endpoint_help="Prefilled with Xiaomi MiMo's OpenAI-compatible endpoint. Get your key at https://dev.xiaomimimo.com",
    ),
    _compat(
        "kimi",
        "Kimi (Moonshot AI)",
        base_url="https://api.moonshot.ai/v1",
        recommended_model="kimi-k2.6",
        env_key="MOONSHOT_API_KEY",
        endpoint_help="Prefilled with Moonshot's international endpoint. China mainland: https://api.moonshot.cn/v1",
    ),
    _compat(
        "minimax",
        "MiniMax",
        base_url="https://api.minimax.io/v1",
        recommended_model="MiniMax-M2.5",
        env_key="MINIMAX_API_KEY",
    ),
    _compat(
        "qwen",
        "Qwen (Alibaba)",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        recommended_model="qwen3-max",
        env_key="DASHSCOPE_API_KEY",
        endpoint_help="Prefilled with Alibaba Model Studio's international endpoint. China (Beijing): https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    _compat(
        "xai",
        "xAI (Grok)",
        base_url="https://api.x.ai/v1",
        recommended_model="grok-4.3",
        env_key="XAI_API_KEY",
    ),
    _compat(
        "mistral",
        "Mistral",
        base_url="https://api.mistral.ai/v1",
        recommended_model="mistral-large-latest",
        env_key="MISTRAL_API_KEY",
    ),
    # Resellers: many labs' models behind one key, using THEIR model namespaces (the curated
    # ids + display labels live in providers/matrix.py). TODO: add Groq and OpenRouter here
    # (+ their matrix rows) once the current provider surface is tested — deliberately
    # deferred to bound how much needs verifying at once (owner call, 2026-07-04).
    _compat(
        "together",
        "Together AI",
        base_url="https://api.together.xyz/v1",
        recommended_model="zai-org/GLM-5.2",
        env_key="TOGETHER_API_KEY",
    ),
    _compat(
        "fireworks",
        "Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        recommended_model="accounts/fireworks/models/glm-5p2",
        env_key="FIREWORKS_API_KEY",
    ),
    ProviderDescriptor(
        name="ollama",
        title="Ollama (local models)",
        needs_key=False,
        fields=[
            ProviderField(
                "base_url",
                "Ollama server URL",
                secret=False,
                required=False,
                placeholder=DEFAULT_OLLAMA_URL,
                help="Where `ollama serve` is listening. The OpenAI-compatible /v1 path is added automatically.",
            ),
        ],
        build=_build_ollama,
        # Reliable native tool-calling + strong coding quality (verified). Pull with
        # `ollama pull qwen3-coder:30b`.
        recommended_model="qwen3-coder:30b",
    ),
]

_BY_NAME = {d.name: d for d in DESCRIPTORS}

# User-added OpenAI-compatible providers (Settings ▸ Models ▸ "Add custom service").
# Stored in the SecretStore (`custom_providers`), synced here by the manager so the rest
# of the code (descriptors, routing, key verify) sees them exactly like curated ones.
_DYNAMIC: dict[str, ProviderDescriptor] = {}


def register_dynamic_provider(name: str, descriptor: ProviderDescriptor) -> None:
    _DYNAMIC[name] = descriptor


def unregister_dynamic_provider(name: str) -> None:
    _DYNAMIC.pop(name, None)


def _all_descriptors() -> list[ProviderDescriptor]:
    return [*DESCRIPTORS, *_DYNAMIC.values()]


def provider_descriptors() -> list[ProviderDescriptor]:
    return _all_descriptors()


def provider_names() -> list[str]:
    return [d.name for d in _all_descriptors()]


def get_descriptor(name: str) -> Optional[ProviderDescriptor]:
    return _BY_NAME.get(name) or _DYNAMIC.get(name)


def custom_compat_descriptor(name: str, info: dict[str, Any]) -> ProviderDescriptor:
    """Descriptor for a user-added OpenAI-compatible service (stored `custom_providers`)."""
    return _compat(
        name,
        str(info.get("title") or name),
        base_url=str(info.get("base_url") or "").strip() or "https://api.openai.com/v1",
        recommended_model=str(info.get("recommended_model") or "").strip() or "",
        env_key="",
    )


def build_provider_client(
    name: str, profile: dict[str, Any], secrets: Any
) -> ProviderClient:
    """Build a `ProviderClient` for `name` from its stored profile. Unknown → OpenAI default."""
    descriptor = get_descriptor(name) or _BY_NAME["openai"]
    return descriptor.build(profile or {}, secrets)


def detect_provider(api_key: str) -> Optional[str]:
    """Best-effort provider guess from an API key's shape, for the onboarding auto-detect.
    Returns a known provider name or None. Mirrors the GUI's client-side detection so both agree.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("AIza"):
        return "gemini"
    if key.startswith(("sk-", "sk_")):
        return "openai"
    return None


def _models_endpoint(
    name: str, d: ProviderDescriptor, key: str, base_url: Optional[str]
) -> tuple[str, dict[str, str], dict[str, str]]:
    """(url, headers, params) for one read-only GET to the provider's model list — the single
    place that knows each family's listing URL/headers. Shared by verify_provider_key (auth
    check) and list_provider_models (live ids), so the two can never drift apart."""
    if name == "anthropic":
        return (
            "https://api.anthropic.com/v1/models",
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            {},
        )
    if name == "gemini":
        return (
            "https://generativelanguage.googleapis.com/v1beta/models",
            {},
            {"key": key},
        )
    if name == "ollama":
        # Keyless local server; _normalize_ollama_url appends /v1 when missing.
        return (_normalize_ollama_url(base_url).rstrip("/") + "/models", {}, {})
    # openai + any OpenAI-compatible endpoint (Azure, OpenRouter, vendors, vLLM…)
    default_base = next(
        (f.default for f in d.fields if f.key == "base_url" and f.default), ""
    )
    base = (
        (base_url or "").strip().rstrip("/")
        or default_base.rstrip("/")
        or "https://api.openai.com/v1"
    )
    return base + "/models", {"Authorization": f"Bearer {key}"}, {}


def _models_get(
    name: str, d: ProviderDescriptor, key: str, base_url: Optional[str], timeout: float
):
    """Issue the model-list GET. Empty headers/params are omitted so keyless calls (ollama)
    stay keyless. Raises on network errors — callers map them to friendly errors."""
    import httpx

    url, headers, params = _models_endpoint(name, d, key, base_url)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if headers:
        kwargs["headers"] = headers
    if params:
        kwargs["params"] = params
    return httpx.get(url, **kwargs)


def verify_provider_key(
    name: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Validate a provider's credentials with one cheap, read-only call (list models) — the same
    pattern connectors use to validate tokens. Transient: callers pass the key directly so a user
    can Test before saving. Never raises; returns {ok, error?}.
    """
    import httpx

    d = get_descriptor(name) or _BY_NAME["openai"]
    key = (api_key or "").strip()
    try:
        resp = _models_get(name, d, key, base_url, timeout)
        if (
            name not in ("anthropic", "gemini", "ollama")
            and resp.status_code in (404, 405)
        ):
            # 供应商未实现 GET /models（不少国内网关只暴露 chat/completions）
            # → 用一次 1-token 对话探针验证鉴权（成本≈0，只为区分钥匙对错）。
            url, _, _ = _models_endpoint(name, d, key, base_url)
            base = url[: -len("/models")]
            resp = httpx.post(
                base + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": d.recommended_model or "default",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=timeout,
            )
            if resp.status_code < 300 or 400 <= resp.status_code < 500 and resp.status_code not in (401, 403):
                # 鉴权已通过（200，或模型不存在/参数/限流等 4xx 业务错误 — 都不是钥匙问题）
                return {"ok": True}
            # 401/403 → 下方统一"Invalid API key."; 5xx → 下方 generic HTTP 错误
    except Exception as exc:  # DNS/connection/timeout — never let it bubble to a 500
        return {
            "ok": False,
            "error": f"Couldn't reach {d.title} ({exc.__class__.__name__}).",
        }

    if resp.status_code < 300:
        return {"ok": True}
    if resp.status_code in (401, 403):
        if name == "ollama":
            return {"ok": False, "error": "Server rejected the request."}
        return {"ok": False, "error": "Invalid API key."}
    if resp.status_code == 404 and name == "ollama":
        return {
            "ok": False,
            "error": "Reached the server, but no OpenAI-compatible /v1 API there.",
        }
    return {"ok": False, "error": f"{d.title} returned HTTP {resp.status_code}."}


def list_provider_models(
    name: str,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """该服务商的**实时**模型列表（只读 GET /models，与 verify_provider_key 同一条请求路径）。

    为什么需要它：curated 矩阵（providers/matrix.py）只收录人工验证过的少数模型 ——
    owner-hit 2026-09-13：接了 6 家服务商，会话选择器里却只有三四个模型，用户之前用的
    模型根本不在矩阵里。接好一家后，应该能看到**这家实际提供的模型**并直接勾选。

    解析各家形状：OpenAI 兼容 ``{"data":[{"id":…}]}``（含 anthropic / DeepSeek / 智谱 /
    Moonshot / 自建网关…）、Gemini ``{"models":[{"name":"models/gemini-…"}]}``（去掉前缀）、
    Ollama ``{"models":[{"model":…}]}``，以及少数把列表直接放在顶层的网关。顺序保留供应商
    自己的排序（通常新模型在前），仅去重。Never raises：{ok, models, count} 或 {ok, False, error}。
    """
    d = get_descriptor(name) or _BY_NAME["openai"]
    key = (api_key or "").strip()
    try:
        resp = _models_get(name, d, key, base_url, timeout)
    except Exception as exc:  # DNS/connection/timeout — never let it bubble to a 500
        return {
            "ok": False,
            "error": f"Couldn't reach {d.title} ({exc.__class__.__name__}).",
        }
    if resp.status_code in (404, 405):
        # 不少国内网关只暴露 chat/completions（verify 的 1-token 探针能验钥匙，但拿不到列表）
        return {
            "ok": False,
            "status": 404,
            "error": "This service does not expose a model list — add models by hand below.",
        }
    if resp.status_code in (401, 403):
        if name == "ollama":
            return {"ok": False, "error": "Server rejected the request."}
        return {"ok": False, "error": "Invalid API key."}
    if resp.status_code >= 300:
        return {"ok": False, "error": f"{d.title} returned HTTP {resp.status_code}."}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": f"{d.title} returned a non-JSON model list."}

    items: Any
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        if not items and isinstance(data.get("object"), list):
            items = data["object"]  # 个别网关把列表放在 object 键下
    elif isinstance(data, list):
        items = data  # 顶层就是列表的网关
    else:
        items = []

    ids: list[str] = []

    def _push(value: Any) -> None:
        if not isinstance(value, str):
            return
        v = value.strip()
        if not v:
            return
        if v.startswith("models/"):  # Gemini: "models/gemini-2.0-flash" → "gemini-2.0-flash"
            v = v[len("models/") :]
        ids.append(v)

    for it in items:
        if isinstance(it, dict):
            _push(it.get("id") or it.get("name") or it.get("model"))
        else:
            _push(it)

    seen: set[str] = set()
    models = [m for m in ids if not (m in seen or seen.add(m))]
    return {"ok": True, "models": models, "count": len(models)}
