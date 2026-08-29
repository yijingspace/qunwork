"""信息素场 (stigmergy) — 蜂群协调面的负载信号（评估报告 增量1）。

信息素思路的负载均衡: Agent 开始任务时向共享场「释放信息素」(忙标记),
结束后释放负信息素, 空闲时强度随时间**蒸发**。调度器**读信号**而非中心
轮询状态池 —— 忙的 Agent 信号强, 调度器自动降低其并发, 避免过载; 信号
丢失只损失"倾向", 不中断任务(天然容错)。

惰性蒸发: 读取时按时间衰减(半衰期), 不依赖后台清理任务 —— 无额外协程,
场中信息素自然随时间归零。

QunMesh M1 (丝瓜络拓扑·信息素总线): PheromoneField 升级为 StigmergyBus
四信道总线 (load/task/result/risk), 每条信息素带 TTL 与可选 payload,
SQLite 持久化 (进程重启场不丢, 沿用 checkpoint 的 check_same_thread=False
写盘模式)。旧 PheromoneField 原样保留; StigmergyBus 的 load 信道暴露同款
API (deposit(key, amount) / level / levels / total_load / busy_keys),
orchestrator 与 /v1/pheromone 调用点零改动。回滚开关:
prefs["pheromone_bus_enabled"]=false → manager 退回 PheromoneField。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional


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
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._marks: dict[str, tuple[float, float]] = {}  # key -> (intensity, last_ts)
        self._half_life = half_life
        self._cap = cap
        self._decay_to = decay_to
        # Injectable clock — tests advance time deterministically instead of
        # sleeping on a real half-life (flaky under load: a preempted process
        # evaporates past the >0.0 assertion).
        self._now = now_fn or time.time

    # -- internals ----------------------------------------------------------
    def _evaporated(self, intensity: float, age: float) -> float:
        # I(t) = I0 * 0.5^(age / half_life) — exponential evaporation.
        return intensity * (0.5 ** (age / self._half_life))

    # -- API -----------------------------------------------------------------
    def deposit(self, key: str, amount: float) -> None:
        """Release (positive) or withdraw (negative) pheromone for `key`. A task
        start deposits +1.0; its completion deposits -1.0. Capped and floored."""
        with self._lock:
            now = self._now()
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
            v = self._evaporated(intensity, self._now() - ts)
            if v < self._decay_to:
                self._marks.pop(key, None)
                return 0.0
            return v

    def levels(self) -> dict[str, float]:
        """All non-faded signals, most intense first. Reads also prune faded keys."""
        with self._lock:
            now = self._now()
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


# ---------------------------------------------------------------------------
# QunMesh M1: StigmergyBus 四信道信息素总线
# ---------------------------------------------------------------------------

CHANNELS = ("load", "task", "result", "risk")
# 各信道默认 TTL (秒)。load 沿用旧语义: 无绝对寿命 (蒸发负责归零);
# task/result/risk 是「招领/通知/风险」类信号, 必须有绝对寿命防僵尸招领。
DEFAULT_TTL: dict[str, Optional[float]] = {
    "load": None,
    "task": 300.0,
    "result": 600.0,
    "risk": 900.0,
}


def _pheromone_bus_from_config(cfg: Any) -> Any:
    """配置解析 (模块级纯函数 — stub 兼容约定): pheromone_bus_enabled=false
    回退旧 PheromoneField, 否则 StigmergyBus 四信道总线。cfg 容错 (None/dict/对象)。"""
    enabled = True
    if cfg is not None:
        if isinstance(cfg, dict):
            enabled = bool(cfg.get("pheromone_bus_enabled", True))
        else:
            enabled = bool(getattr(cfg, "pheromone_bus_enabled", True))
    if enabled:
        return StigmergyBus()
    return PheromoneField()


class StigmergyBus:
    """四信道信息素总线 (QunMesh M1)。

    每条信息素 = (channel, key, intensity, deposited_at, ttl, payload):
    - 蒸发沿用惰性模型 I(t) = I0·0.5^(age/half_life), 读时衰减;
    - TTL 是绝对寿命 (自最近一次 deposit 起算), 到期即失效 — 与蒸发正交;
    - 四信道共享同一套场语义, load 信道行为与 PheromoneField 完全一致;
    - SQLite 持久化 (db_path 给定时): deposit 即 UPSERT 落盘, 构造时载入,
      进程重启场不丢 (场恢复)。
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        half_life: float = 60.0,
        cap: float = 10.0,
        decay_to: float = 0.05,
        default_ttl: Optional[dict[str, Optional[float]]] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._lock = threading.Lock()
        # (channel, key) -> [intensity, deposited_at, ttl, payload]
        self._marks: dict[tuple[str, str], list] = {}
        self._half_life = half_life
        self._cap = cap
        self._decay_to = decay_to
        self._ttl = dict(DEFAULT_TTL)
        if default_ttl:
            self._ttl.update(default_ttl)
        # Injectable clock — 与 PheromoneField 同款测试约定。
        self._now = now_fn or time.time
        self._con: Optional[sqlite3.Connection] = None
        if db_path is not None:
            self._open_db(Path(db_path))

    # -- persistence ---------------------------------------------------------
    def _open_db(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS pheromone_marks (
                channel TEXT NOT NULL,
                key TEXT NOT NULL,
                intensity REAL NOT NULL,
                deposited_at REAL NOT NULL,
                ttl REAL,
                payload TEXT,
                PRIMARY KEY (channel, key)
            )"""
        )
        self._con.commit()
        # 场恢复: 载入全部存活信息素 (过期行读取时惰性清理, 这里全载再过滤)。
        now = self._now()
        for ch, key, intensity, dep, ttl, payload in self._con.execute(
            "SELECT channel, key, intensity, deposited_at, ttl, payload FROM pheromone_marks"
        ):
            if ttl is not None and now >= dep + ttl:
                continue
            self._marks[(ch, key)] = [float(intensity), float(dep), ttl, payload]

    def _persist(self, channel: str, key: str, mark: Optional[list]) -> None:
        if self._con is None:
            return
        if mark is None:
            self._con.execute(
                "DELETE FROM pheromone_marks WHERE channel=? AND key=?", (channel, key)
            )
        else:
            self._con.execute(
                "INSERT INTO pheromone_marks (channel, key, intensity, deposited_at, ttl, payload) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(channel, key) DO UPDATE SET "
                "intensity=excluded.intensity, deposited_at=excluded.deposited_at, "
                "ttl=excluded.ttl, payload=excluded.payload",
                (channel, key, mark[0], mark[1], mark[2], mark[3]),
            )
        self._con.commit()

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
            self._con = None

    # -- internals -----------------------------------------------------------
    def _evaporated(self, intensity: float, age: float) -> float:
        return intensity * (0.5 ** (age / self._half_life))

    def _live_value(self, mark: list, now: float) -> Optional[float]:
        """存活判定: TTL 到期 → None; 蒸发低于 decay_to → None。"""
        intensity, dep, ttl, _payload = mark
        if ttl is not None and now >= dep + ttl:
            return None
        v = self._evaporated(intensity, now - dep)
        return None if v < self._decay_to else v

    # -- API (load 信道与 PheromoneField 同形) ---------------------------------
    def deposit(
        self,
        key: str,
        amount: float,
        *,
        channel: str = "load",
        ttl: Optional[float] = ...,
        payload: Optional[str] = None,
    ) -> None:
        """释放/撤回信息素。位置参数 (key, amount) 与 PheromoneField.deposit
        完全兼容 (默认 load 信道)。ttl 缺省用信道默认值; payload 携带
        result/risk 信道的摘要 (任务招领/产物通知/风险描述)。"""
        if channel not in CHANNELS:
            raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}")
        with self._lock:
            now = self._now()
            slot = (channel, key)
            prev = self._marks.get(slot)
            if ttl is ...:
                ttl_eff = self._ttl.get(channel)
            else:
                ttl_eff = ttl
            if prev is not None:
                # 同槽位续投: 先蒸发旧强度, TTL 与 payload 按本次刷新
                cur = self._evaporated(prev[0], now - prev[1])
                if prev[3] is not None and payload is None:
                    payload = prev[3]
            else:
                cur = 0.0
            nxt = max(0.0, min(self._cap, cur + amount))
            if nxt < self._decay_to:
                self._marks.pop(slot, None)
                self._persist(channel, key, None)
            else:
                self._marks[slot] = [nxt, now, ttl_eff, payload]
                self._persist(channel, key, self._marks[slot])

    def level(self, key: str, *, channel: str = "load") -> float:
        with self._lock:
            mark = self._marks.get((channel, key))
            if mark is None:
                return 0.0
            now = self._now()
            v = self._live_value(mark, now)
            if v is None:
                self._marks.pop((channel, key), None)
                return 0.0
            return v

    def levels(self, *, channel: str = "load") -> dict[str, float]:
        """某信道全部存活信号, 最强优先 (默认 load — 兼容 pheromone_status)。"""
        with self._lock:
            now = self._now()
            out: dict[str, float] = {}
            for (ch, k), mark in list(self._marks.items()):
                if ch != channel:
                    continue
                v = self._live_value(mark, now)
                if v is None:
                    self._marks.pop((ch, k), None)
                else:
                    out[k] = round(v, 3)
            return dict(sorted(out.items(), key=lambda kv: kv[1], reverse=True))

    def payload_of(self, key: str, *, channel: str = "load") -> Optional[str]:
        """读取某信号的 payload 摘要 (result/risk 信道的产物/风险描述)。"""
        with self._lock:
            mark = self._marks.get((channel, key))
            return mark[3] if mark else None

    def top(self, channel: str, k: int = 5) -> list[tuple[str, float]]:
        """某信道最强前 k 条 — M2 网格调度 / M3 邻域感知的消费入口。"""
        lv = self.levels(channel=channel)
        return list(lv.items())[:k]

    def busy_keys(self, threshold: float = 1.0) -> list[str]:
        return [k for k, v in self.levels().items() if v >= threshold]

    def total_load(self) -> float:
        """load 信道总和 — _select_batch 的并发收缩信号 (兼容旧语义)。"""
        return sum(self.levels().values())

    def channels_summary(self) -> dict[str, dict[str, float]]:
        """四信道总览 (pheromone_status 的 channels 字段): 各信道信号数与强度和。"""
        with self._lock:
            now = self._now()
            out: dict[str, dict[str, float]] = {
                ch: {"signals": 0.0, "intensity": 0.0} for ch in CHANNELS
            }
            for (ch, _k), mark in list(self._marks.items()):
                v = self._live_value(mark, now)
                if v is None:
                    continue
                out[ch]["signals"] += 1
                out[ch]["intensity"] = round(out[ch]["intensity"] + v, 3)
        return out

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"StigmergyBus({self.channels_summary()})"
