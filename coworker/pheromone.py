"""信息素场 (stigmergy) — 蜂群协调面的负载信号（评估报告 增量1）。

信息素思路的负载均衡: Agent 开始任务时向共享场「释放信息素」(忙标记),
结束后释放负信息素, 空闲时强度随时间**蒸发**。调度器**读信号**而非中心
轮询状态池 —— 忙的 Agent 信号强, 调度器自动降低其并发, 避免过载; 信号
丢失只损失"倾向", 不中断任务(天然容错)。

惰性蒸发: 读取时按时间衰减(半衰期), 不依赖后台清理任务 —— 无额外协程,
场中信息素自然随时间归零。
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class PheromoneField:
    """A shared stigmergic load field: `key → intensity` with exponential
    evaporation. Thread-safe (the orchestrator writes from its event loop, the
    API reads from request threads)."""

    def __init__(
        self,
        *,
        half_life: float = 60.0,
        cap: float = 10.0,
        decay_to: float = 0.05,
    ) -> None:
        self._lock = threading.Lock()
        self._marks: dict[str, tuple[float, float]] = {}  # key -> (intensity, last_ts)
        self._half_life = half_life
        self._cap = cap
        self._decay_to = decay_to

    # -- internals ----------------------------------------------------------
    def _evaporated(self, intensity: float, age: float) -> float:
        # I(t) = I0 * 0.5^(age / half_life) — exponential evaporation.
        return intensity * (0.5 ** (age / self._half_life))

    # -- API -----------------------------------------------------------------
    def deposit(self, key: str, amount: float) -> None:
        """Release (positive) or withdraw (negative) pheromone for `key`. A task
        start deposits +1.0; its completion deposits -1.0. Capped and floored."""
        with self._lock:
            now = time.time()
            prev, ts = self._marks.get(key, (0.0, now))
            cur = self._evaporated(prev, now - ts)
            nxt = max(0.0, min(self._cap, cur + amount))
            if nxt < self._decay_to:
                self._marks.pop(key, None)
            else:
                self._marks[key] = (nxt, now)

    def level(self, key: str) -> float:
        """Current (evaporated) intensity for `key`, or 0.0 when faded out."""
        with self._lock:
            mark = self._marks.get(key)
            if mark is None:
                return 0.0
            intensity, ts = mark
            v = self._evaporated(intensity, time.time() - ts)
            if v < self._decay_to:
                self._marks.pop(key, None)
                return 0.0
            return v

    def levels(self) -> dict[str, float]:
        """All non-faded signals, most intense first. Reads also prune faded keys."""
        with self._lock:
            now = time.time()
            out: dict[str, float] = {}
            for k, (intensity, ts) in list(self._marks.items()):
                v = self._evaporated(intensity, now - ts)
                if v < self._decay_to:
                    self._marks.pop(k, None)
                else:
                    out[k] = round(v, 3)
        return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

    def busy_keys(self, threshold: float = 1.0) -> list[str]:
        """Keys whose signal is at/above `threshold` — the "currently busy" set."""
        return [k for k, v in self.levels().items() if v >= threshold]

    def total_load(self) -> float:
        """Sum of all live signals — a scalar proxy for how loaded the field is
        (each active task deposits ~1.0, so this ≈ active task count)."""
        return sum(self.levels().values())

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"PheromoneField({self.levels()})"
