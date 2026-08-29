"""蜂巢心跳模型 (突破方案四) — 任务健康度的自相似振荡检测.

依据《QunWork 7x24长程任务架构·创新研究与突破方案》第六节:
  * 任务健康度 ``H(t) = H₀ · e^{-γt} · cos(ω_bee·t + φ)`` 在蜂巢共振频率上振荡;
  * 衰减率 ``γ = (1-α)·λ₂ ≈ 0.048``, 蜂巢共振率 ``α ≈ 0.8998 ≈ φ^{-0.5}``;
  * 故障检测时间 ``t_detect = (1/γ)·ln(H₀/H_threshold) ≈ 25s`` — 任务停止心跳后
    25 秒内可检测 (与 Scheduler tick 30s 对齐);
  * 域分裂: 健康度跌破阈值 → 任务可能卡死, 触发超时自动分裂为子任务 (降级)。

与 B3 瓶颈对应: 原 ``Scheduler._tick()`` 仅检查 ``due()``, 无心跳/存活检测,
后台任务丢失无感知。本模块提供独立心跳注册表 + 健康度评估, 供 Scheduler
每 tick 调用 ``check_health()``, 卡死任务进入降级链 (degradation.py)。
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PHI = (1.0 + math.sqrt(5.0)) / 2.0
ALPHA_BEE = 0.8998  # 蜂巢共振率 ≈ φ^{-0.5}
DECAY_RATE = (1.0 - ALPHA_BEE) * 0.48  # ≈ 0.048
TICK_SECONDS = 30.0  # 蜂巢共振周期, 与 Scheduler tick 对齐
HEALTH_THRESHOLD = 0.3  # 低于该值判定任务可能卡死
PULSE_BOOST = 0.2  # 每次心跳脉冲健康度回升量


def detection_time(threshold: float = HEALTH_THRESHOLD, h0: float = 1.0) -> float:
    """理论故障检测时间: (1/γ)·ln(H₀/H_threshold)。"""
    if DECAY_RATE <= 0:
        return float("inf")
    return (1.0 / DECAY_RATE) * math.log(max(h0, 1e-9) / max(threshold, 1e-9))


def default_heartbeat_path() -> Path:
    """默认心跳状态文件: ``<state_dir>/heartbeat.json``。

    与 QunWork 全局状态目录对齐 (``coworker.secrets.state_dir``):
      * ``$COWORKER_STATE_DIR`` 显式覆盖;
      * Windows: ``%APPDATA%\\coworker``;
      * macOS/Linux: ``~/.config/coworker``。
    惰性导入 secrets 避免循环依赖。
    """
    from .secrets import state_dir

    return state_dir() / "heartbeat.json"


class HoneycombHeartbeat:
    """蜂巢心跳模型 — 任务健康度的自相似振荡检测。

    用法::

        hb = HoneycombHeartbeat()
        hb.register("task-1")
        hb.pulse("task-1")                     # 每轮交互调用
        health = hb.check_health()             # 每 tick 调用 (Scheduler._tick)
        dead = hb.get_unhealthy()              # -> ["task-1"] (卡死任务)

    可持久化 (``path``): 进程重启后 ``load()`` 恢复心跳时间, 避免重启即误判。
    """

    def __init__(
        self,
        *,
        tick_seconds: float = TICK_SECONDS,
        threshold: float = HEALTH_THRESHOLD,
        path: Optional[str | Path] = None,
        persist_default: bool = True,
        on_unhealthy: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.tick_seconds = tick_seconds
        self.threshold = threshold
        # 持久化路径: 显式 path 优先; 缺省时接 QunWork 全局状态目录
        # (``persist_default=True`` → ``<state_dir>/heartbeat.json``), 进程重启
        # 后心跳记录仍在, 不误判。``persist_default=False`` 保持纯内存 (测试)。
        if path is not None:
            self.path = Path(path)
        elif persist_default:
            self.path = default_heartbeat_path()
        else:
            self.path = None
        self.on_unhealthy = on_unhealthy  # 回调 (task_id, health) — 接到降级链
        self._lock = threading.Lock()
        self.heartbeats: dict[str, float] = {}  # task_id -> last_beat
        self.health: dict[str, float] = {}  # task_id -> H(t)
        if self.path and self.path.is_file():
            self.load()

    # -- 注册 ---------------------------------------------------------------
    def register(self, task_id: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self.heartbeats[task_id] = now
            self.health[task_id] = 1.0
        logger.debug("heartbeat registered: %s", task_id)

    def unregister(self, task_id: str) -> None:
        with self._lock:
            self.heartbeats.pop(task_id, None)
            self.health.pop(task_id, None)
        self._save()

    def pulse(self, task_id: str, now: Optional[float] = None) -> None:
        """任务心跳脉冲 (每轮交互调用)。``now`` 可注入 (测试)。"""
        now = time.time() if now is None else now
        with self._lock:
            self.heartbeats[task_id] = now
            self.health[task_id] = min(1.0, self.health.get(task_id, 1.0) + PULSE_BOOST)
        self._save()

    # -- 检测 ---------------------------------------------------------------
    def check_health(self, now: Optional[float] = None) -> dict[str, float]:
        """检查所有任务健康度 (每 tick 调用)。返回 {task_id: H}。

        振荡模型: ``H = H_prev · e^{-γ·elapsed} · (0.75 + 0.25·cos(2π·elapsed/T))``
        — 自相似振荡作为叠加在衰减包络上的受控微扰 (因子 ∈ [0.5, 1.0]),
        健康度只随无心跳时长平滑衰减, 不会因半个周期振荡而周期性归零
        (假死)。长期无心跳则跌破阈值 (t_detect ≈ 25s)。

        ``now`` 可注入 (测试/回放): 缺省用系统时钟。
        """
        now = time.time() if now is None else now
        results: dict[str, float] = {}
        with self._lock:
            for task_id, last_beat in list(self.heartbeats.items()):
                elapsed = max(0.0, now - last_beat)
                oscillation = 0.75 + 0.25 * math.cos(2 * math.pi * elapsed / self.tick_seconds)
                decay = math.exp(-DECAY_RATE * elapsed)
                h = max(0.0, self.health.get(task_id, 1.0) * decay * oscillation)
                # 振荡归零保护: 无心跳超过 2 个周期 → 直接按衰减衰减到 0
                if elapsed > 2 * self.tick_seconds:
                    h = min(h, math.exp(-DECAY_RATE * elapsed))
                self.health[task_id] = h
                results[task_id] = h
        for task_id, h in results.items():
            if h < self.threshold and self.on_unhealthy is not None:
                try:
                    self.on_unhealthy(task_id, h)
                except Exception:
                    logger.exception("on_unhealthy callback failed for %s", task_id)
        return results

    def get_unhealthy(self, threshold: Optional[float] = None) -> list[str]:
        """返回可能卡死的任务 (健康度 < 阈值)。"""
        thr = self.threshold if threshold is None else threshold
        return [tid for tid, h in self.health.items() if h < thr]

    def alive(self, task_id: str, grace_seconds: Optional[float] = None) -> bool:
        """任务是否存活: 距上次心跳 < 1.5 个 tick (宽容窗口)。"""
        grace = grace_seconds or (1.5 * self.tick_seconds)
        last = self.heartbeats.get(task_id)
        if last is None:
            return False
        return (time.time() - last) <= grace

    # -- 持久化 -------------------------------------------------------------
    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"heartbeats": self.heartbeats, "health": self.health}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("heartbeat persist failed (best-effort)", exc_info=True)

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            with self._lock:
                self.heartbeats = {str(k): float(v) for k, v in (data.get("heartbeats") or {}).items()}
                self.health = {str(k): float(v) for k, v in (data.get("health") or {}).items()}
        except (OSError, ValueError, TypeError):
            logger.debug("heartbeat load failed (starting fresh)", exc_info=True)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "tasks": len(self.heartbeats),
            "alive": [
                tid for tid in self.heartbeats if (now - self.heartbeats[tid]) <= 1.5 * self.tick_seconds
            ],
            "unhealthy": self.get_unhealthy(),
            "detection_time_seconds": detection_time(self.threshold),
        }
