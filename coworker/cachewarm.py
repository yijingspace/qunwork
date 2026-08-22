"""Cache warm-up (P0 建议1): HORNET 冷度预警 × 缓存预热闭环.

When the org's context-cache hit rate is low (`min_hit_rate`) AND HORNET holds
knowledge nodes that have gone cold (low `freshness`), a warm pass re-injects
the coldest knowledge as a "virtual prompt" through the provider with
`max_tokens=1` — pushing those prefixes into the provider's automatic prefix
cache so later real turns hit instead of miss.

Cost accounting is honest: every warm call is a real (tiny) request and its
prompt tokens are recorded in the usage store with `surface="cachewarm"`, so
the UI can show "this week spent warming". The *saved* side is deliberately
NOT fabricated here — we report injected volume + the org hit-rate, and the
frontend labels the benefit as an estimate pending A/B measurement.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("coworker.cachewarm")

# The warm probe is discarded — a single dot keeps completion tokens ~0 while
# the prompt prefix (the knowledge payload) does the caching work.
_SYSTEM_PROMPT = (
    "You are a cache warm-up probe. The request is discarded — reply with a "
    "single dot and nothing else."
)


class CacheWarmer:
    """Selects the coldest HORNET knowledge and pushes its prefix through the
    provider so future real turns hit the automatic context cache."""

    def __init__(
        self,
        provider: Any,
        hornet: Any,
        usage_store: Any,
        *,
        default_model: Optional[str] = None,
        min_hit_rate: float = 0.3,
        max_items: int = 5,
        interval_hours: float = 6.0,
    ) -> None:
        self.provider = provider
        self.hornet = hornet
        self.usage_store = usage_store
        self.default_model = default_model
        self.min_hit_rate = min_hit_rate
        self.max_items = max_items
        self.interval_hours = interval_hours
        self.enabled = True  # toggled via POST /v1/cache/warm/toggle
        self.last_warm_at = 0.0
        self._lock = asyncio.Lock()

    # -- coldness ------------------------------------------------------------
    def cold_nodes(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """The coldest knowledge nodes: lowest `freshness` (HORNET decays it over
        time when a node is not re-hit) — these benefit most from re-injection."""
        nodes = (
            self.hornet.list_nodes(fields=("id", "title", "freshness"))
            if hasattr(self.hornet, "list_nodes")
            else []
        )
        if not nodes:
            return []
        scored = sorted(
            nodes, key=lambda n: (float(n.get("freshness", 1.0)), str(n.get("title", "")))
        )
        return scored[: limit or self.max_items]

    # -- the warm pass -------------------------------------------------------
    async def warm_once(
        self, *, max_items: Optional[int] = None, model: Optional[str] = None
    ) -> dict[str, Any]:
        """Inject the coldest knowledge prefixes (max_tokens=1), recording each
        call in the usage store (surface='cachewarm'). Returns a summary."""
        model = model or self.default_model
        if not model:
            return {"ok": False, "reason": "no model configured", "warmed": 0}
        nodes = self.cold_nodes(max_items)
        if not nodes:
            return {"ok": False, "reason": "no knowledge nodes to warm", "warmed": 0}

        async with self._lock:
            items: list[dict[str, Any]] = []
            total_prompt = 0
            total_cached = 0
            errors = 0
            for node in nodes:
                content = (node.get("content") or "").strip()[:2000]
                if not content:
                    continue
                messages = [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ]
                usage: Optional[dict[str, Any]] = None
                try:
                    async for chunk in self.provider.stream(
                        model=model, messages=messages, max_tokens=1
                    ):
                        turn = getattr(chunk, "turn", None)
                        if turn is not None and getattr(turn, "usage", None):
                            usage = turn.usage
                except Exception as exc:  # provider down / bad key — never block the loop
                    logger.warning("cache warm failed for %s: %s", node.get("title"), exc)
                    errors += 1
                    items.append({"title": node.get("title", ""), "error": str(exc)})
                    continue
                usage = usage or {}
                prompt = int(usage.get("prompt_tokens") or 0)
                cached = int(usage.get("cached_tokens") or 0)
                miss = int(usage.get("cache_miss_tokens") or prompt)
                total_prompt += prompt
                total_cached += cached
                self.usage_store.record(
                    {
                        "session_id": "__cachewarm__",
                        "surface": "cachewarm",
                        "run_id": None,
                        "model": model,
                        "prompt_tokens": prompt,
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "cached_tokens": cached,
                        "cache_miss_tokens": miss,
                    }
                )
                items.append(
                    {
                        "title": node.get("title", ""),
                        "prompt_tokens": prompt,
                        "cached_tokens": cached,
                    }
                )
            self.last_warm_at = time.time()
            return {
                "ok": True,
                "warmed": len([i for i in items if "error" not in i]),
                "errors": errors,
                "prompt_tokens": total_prompt,
                "cached_tokens": total_cached,
                "items": items,
            }

    # -- status --------------------------------------------------------------
    def status(self, *, week_seconds: float = 7 * 86400) -> dict[str, Any]:
        """Toggle state + this week's warm activity (injected tokens from the
        usage store). The saved-side estimate stays on the caller."""
        since = time.time() - week_seconds
        week = self.usage_store.surface_totals("cachewarm", since)
        return {
            "enabled": self.enabled,
            "min_hit_rate": self.min_hit_rate,
            "max_items": self.max_items,
            "interval_hours": self.interval_hours,
            "last_warm_at": self.last_warm_at,
            "week": week,
            "org_hit_rate": (self.usage_store.totals() or {}).get("cache_hit_rate", 0.0),
        }
