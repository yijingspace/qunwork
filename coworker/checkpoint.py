"""分形检查点引擎 (Fractal Checkpoint Engine) — 7×24 长程任务落地 (突破方案一).

依据《QunWork 7x24长程任务架构·创新研究与突破方案》第三节:
  * 黄金衡七层衰减: 七层粒度 (full/messages/summary/diff/final) × 黄金比例频率
    ``Δt_cp(n, t) = Δt₀ · φ^{|n-4|} · e^{-t/T_decay}``;
  * 检查点存储开销上界: ``S_cp ≤ S_session · 1/(1-φ^{-1}) ≈ 2.618 · S_session``;
  * 崩溃后可从最近的检查点逐层叠加恢复 (restore_latest), 上下文丢失量降 ~90%。

与 B1 瓶颈对应: ``SessionRecord`` 原本无 ``checkpoint()`` 方法, 消息存 JSONL 追加写,
崩溃即丢失全部未持久化上下文。本引擎为任意 ``session_id`` 提供独立的检查点存储层,
不侵入 ``ConversationStore`` 的 JSONL 主存储。

存储: SQLite (``checkpoints`` 表), 每条检查点带 n_layer / granularity / retention TTL;
过期检查点由 ``prune_expired()`` 清理 (带黄金比例 TTL)。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PHI = (1.0 + math.sqrt(5.0)) / 2.0  # 黄金比例 ≈ 1.6180339887
T_DECAY_SECONDS = 24 * 3600  # 时间衰减常数 (24h): 越运行越频繁

# 七层粒度表: interval=基准间隔(秒), granularity=粒度, retention=保留时长(秒/None=永久)
LAYERS: dict[int, dict[str, Any]] = {
    1: {"interval": 30, "granularity": "full", "retention": 3600},       # 1h
    2: {"interval": 60, "granularity": "full", "retention": 6 * 3600},   # 6h
    3: {"interval": 120, "granularity": "messages", "retention": 24 * 3600},  # 24h
    4: {"interval": 300, "granularity": "summary", "retention": 7 * 86400},   # 7d
    5: {"interval": 600, "granularity": "summary", "retention": 30 * 86400},  # 30d
    6: {"interval": 1800, "granularity": "diff", "retention": 90 * 86400},    # 90d
    7: {"interval": 3600, "granularity": "final", "retention": None},         # forever
}


def checkpoint_interval(n_layer: int, elapsed_seconds: float = 0.0) -> float:
    """分形检查点间隔: ``Δt_cp(n, t) = Δt₀ · φ^{|n-4|} · e^{-t/T_decay}``.

    n=1 (超热) 时最短, n=7 (冷存) 时最长; 运行越久衰减因子使间隔越短 (越要勤备份)。
    """
    n = max(1, min(7, int(n_layer)))
    base = float(LAYERS[n]["interval"])
    phi_factor = PHI ** abs(n - 4)
    decay = math.exp(-max(0.0, elapsed_seconds) / T_DECAY_SECONDS)
    return base * phi_factor * decay


def golden_ttl(seconds: int) -> float:
    """黄金比例 TTL: 保留时长按 φ 分形延长, 层越高保留越久。"""
    return float(seconds) * (PHI ** 0.5)  # φ^0.5 ≈ 1.272


def _now() -> float:
    return time.time()


class FractalCheckpoint:
    """分形检查点引擎 — 七层粒度 × 黄金比例频率, SQLite 持久化。

    用法::

        cp = FractalCheckpoint("/path/to/.qunwork/checkpoints.db")
        cp.save_checkpoint(session_id, {"messages": [...], "tasks": [...]}, n_layer=3)
        state = cp.restore_latest(session_id)

    集成点 (方案 8.2): ``SessionRecord`` 增加 ``checkpoint`` 字段引用最新检查点;
    编排循环每轮调用 ``save_checkpoint``, 失败时 ``restore_latest`` 增量恢复。
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        summarizer: Optional[Callable[[list[dict]], str]] = None,
    ) -> None:
        """分形检查点引擎。

        ``summarizer``: 可选消息摘要回调 (接收消息列表, 返回摘要文本) —
        summary 粒度 (n=4/5) 用它生成 LLM 摘要而非简单截断; 缺省回退为
        保留最近 20 条消息 (无外部模型依赖)。设计为可注入, 由调用方
        (server/manager) 传入真实的摘要函数。
        """
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.summarizer = summarizer
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                n_layer INTEGER NOT NULL,
                granularity TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cp_session ON checkpoints(session_id, seq)"
        )
        self._conn.commit()

    # -- 写入 ---------------------------------------------------------------
    def should_checkpoint(
        self, session_id: str, n_layer: int, *, now: Optional[float] = None
    ) -> bool:
        """判断该层级是否该做检查点 (按分形间隔)。"""
        now = _now() if now is None else now
        last = self._last_time(session_id, n_layer)
        if last is None:
            return True  # 该层从未检查点 → 立即做
        interval = checkpoint_interval(n_layer, now - last)
        return (now - last) >= interval

    def save_checkpoint(
        self, session_id: str, engine_state: dict, n_layer: int
    ) -> str:
        """保存检查点 — 按层级选择粒度, 返回检查点 id。

        granularity: full=完整快照 / messages=最近 50 条 / summary=摘要 /
        diff=相对上一 full 的差异 / final=仅结果。
        """
        n = max(1, min(7, int(n_layer)))
        layer = LAYERS[n]
        granularity = layer["granularity"]
        snapshot = self._select_snapshot(engine_state, granularity, session_id)
        retention = layer["retention"]
        expires_at = (_now() + golden_ttl(retention)) if retention else None
        cpid = uuid.uuid4().hex[:16]
        with self._lock:
            seq = self._next_seq(session_id)
            self._conn.execute(
                "INSERT INTO checkpoints (id, session_id, seq, n_layer, granularity, "
                "data, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    cpid,
                    session_id,
                    seq,
                    n,
                    granularity,
                    json.dumps(snapshot, ensure_ascii=False, default=str),
                    _now(),
                    expires_at,
                ),
            )
            self._conn.commit()
        logger.debug("checkpoint saved: session=%s seq=%d layer=%d (%s)",
                     session_id, seq, n, granularity)
        return cpid

    def _select_snapshot(self, state: dict, granularity: str, session_id: str) -> dict:
        if granularity == "full":
            return {"kind": "full", "state": state}
        if granularity == "messages":
            msgs = state.get("messages") or []
            return {"kind": "messages", "state": {"messages": msgs[-50:]}}
        if granularity == "summary":
            # 摘要粒度: 优先用注入的 summarizer 生成 LLM 摘要 (突破方案 §3.3
            # "summary = LLM 摘要"); 无 summarizer 时回退为元数据保留 + 最近
            # 20 条消息 (不依赖外部模型, 检查点永远可恢复)。
            out = dict(state)
            msgs = out.get("messages")
            if isinstance(msgs, list):
                if self.summarizer is not None:
                    try:
                        out["messages_summary"] = self.summarizer(msgs)
                        out.pop("messages", None)  # 摘要替代全量消息
                    except Exception:
                        logger.exception("summarizer failed — falling back to tail-keep")
                        out["messages"] = msgs[-20:]
                else:
                    out["messages"] = msgs[-20:]  # 最近 20 条保底
            return {"kind": "summary", "state": out}
        if granularity == "diff":
            last_full = self._last_full_state(session_id)
            return {"kind": "diff", "diff": self._compute_diff(last_full, state)}
        return {"kind": "final", "state": {"result": state.get("result")}}

    def _compute_diff(self, base: Optional[dict], new: dict) -> dict:
        """仅差异: 顶层键级别 diff (增/改/删), 与 base 相同的键不重复存储。"""
        base = base or {}
        diff: dict[str, Any] = {"added": {}, "changed": {}, "removed": []}
        for k, v in new.items():
            if k not in base:
                diff["added"][k] = v
            elif base[k] != v:
                diff["changed"][k] = v
        for k in base:
            if k not in new:
                diff["removed"].append(k)
        return diff

    def _apply_diff(self, state: dict, diff: dict) -> dict:
        out = dict(state)
        out.update(diff.get("added") or {})
        out.update(diff.get("changed") or {})
        for k in diff.get("removed") or []:
            out.pop(k, None)
        return out

    def _last_full_state(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM checkpoints WHERE session_id = ? AND granularity = 'full' "
                "ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        return payload.get("state") if isinstance(payload, dict) else None

    def _last_time(self, session_id: str, n_layer: int) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM checkpoints WHERE session_id = ? AND n_layer = ? "
                "ORDER BY seq DESC LIMIT 1",
                (session_id, int(n_layer)),
            ).fetchone()
        return float(row[0]) if row else None

    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM checkpoints WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) + 1

    # -- 恢复 ---------------------------------------------------------------
    def restore_latest(self, session_id: str) -> Optional[dict]:
        """恢复最新检查点 — 从最高粒度 (full) 开始, 逐层叠加 diff/增量。

        返回恢复后的引擎状态 dict; 无检查点时返回 None。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, n_layer, granularity, data FROM checkpoints "
                "WHERE session_id = ? AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY seq",
                (session_id, _now()),
            ).fetchall()
        if not rows:
            return None
        state: dict = {}
        applied = False
        for _seq, _layer, granularity, data in rows:
            payload = json.loads(data)
            kind = payload.get("kind")
            if kind == "full":
                state = dict(payload.get("state") or {})
                applied = True
            elif kind in ("messages", "summary", "final"):
                state.update(payload.get("state") or {})
                applied = True
            elif kind == "diff":
                state = self._apply_diff(state, payload.get("diff") or {})
                applied = True
        return state if applied else None

    def latest(self, session_id: str) -> Optional[dict]:
        """最新一条检查点的原始记录 (供降级引擎 L5 checkpoint_pause 用)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, n_layer, granularity, data, created_at FROM checkpoints "
                "WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "seq": row[0],
            "n_layer": row[1],
            "granularity": row[2],
            "data": json.loads(row[3]),
            "created_at": row[4],
        }

    # -- 维护 ---------------------------------------------------------------
    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, n_layer, granularity, created_at, expires_at FROM checkpoints "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "seq": r[0],
                "n_layer": r[1],
                "granularity": r[2],
                "created_at": r[3],
                "expires_at": r[4],
            }
            for r in rows
        ]

    def prune_expired(self) -> int:
        """清理过期检查点, 返回删除条数。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM checkpoints WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (_now(),),
            )
            self._conn.commit()
        return cur.rowcount

    def count(self, session_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            logger.debug("checkpoint store close failed", exc_info=True)
