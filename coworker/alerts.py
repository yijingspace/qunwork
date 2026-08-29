"""7x24 告警历史持久化 — 已推送的 7x24_alert 事件落库, 可回溯。

与突破方案四 (蜂巢心跳) 配套: 心跳卡死任务经 ``broadcast_event`` 推送
``7x24_alert`` 事件的同时落一条到 SQLite (``alerts`` 表), 桌面端管理页
可查询历史告警 (任务/类型/消息/时间), 支持按任务过滤与清理。

表: alerts(id, kind, task_id, message, ts, payload)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AlertStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                task_id TEXT,
                message TEXT NOT NULL,
                ts REAL NOT NULL,
                payload TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_task ON alerts(task_id)"
        )
        # 告警/审计归档表: 旧记录按天数归档到这里 (与 alerts 同构)。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts_archive (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                task_id TEXT,
                message TEXT NOT NULL,
                ts REAL NOT NULL,
                payload TEXT,
                archived_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_archive_ts ON alerts_archive(ts)"
        )
        # 告警聚合 (同任务连续卡死合并为一条持续告警):
        #   id / task_id / kind / level / started_at / updated_at / count / resolved
        #   silenced_until: 静默期截止 (epoch 秒) — 同任务连续告警超过阈值后,
        #   在该时间前不再重复通知 (聚合告警的静默期)。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_aggregations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'warning',
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                resolved INTEGER NOT NULL DEFAULT 0,
                silenced_until REAL
            )
            """
        )
        # 聚合历史统计 (持久化统计): 按天快照每任务聚合状态,
        # 供"告警聚合持久化统计 (历史聚合趋势)"。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aggregation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                peak INTEGER NOT NULL DEFAULT 0,
                UNIQUE(day, task_id)
            )
            """
        )
        # 操作审计 (回滚/恢复/渠道变更): kind='audit' 的事件也落 alerts 表,
        # 通过 kind 过滤, 保证与告警历史同一查询通道。
        # 渠道探针历史: 每次探针结果持久化, 供健康分/趋势计算。
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS probe_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                ok INTEGER NOT NULL,
                ms REAL,
                error TEXT,
                ts REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_probe_ch ON probe_history(channel, ts)"
        )
        self._conn.commit()

    def record(
        self,
        kind: str,
        message: str,
        *,
        task_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        ts: Optional[float] = None,
        level: str = "warning",
        silence_after: int = 3,
        silence_seconds: float = 600.0,
    ) -> tuple[int, bool]:
        """落一条告警, 返回 (id, should_notify)。

        同时 upsert 聚合记录 (同任务连续告警合并); 当聚合 count 达到
        ``silence_after`` 阈值时, 进入静默期 ``silence_seconds`` — 静默期内
        同任务告警仍落库但 ``should_notify=False`` (不重复通知)。
        """
        ts = time.time() if ts is None else ts
        should_notify = True
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (kind, task_id, message, ts, payload) VALUES (?,?,?,?,?)",
                (kind, task_id, message, ts, json.dumps(payload or {}, ensure_ascii=False)),
            )
            if task_id:
                # 聚合 upsert: 同任务未解决告警 → count+1 / updated_at 刷新。
                self._conn.execute(
                    """
                    INSERT INTO alert_aggregations (task_id, kind, level, started_at, updated_at, count, resolved)
                    VALUES (?, ?, ?, ?, ?, 1, 0)
                    ON CONFLICT(task_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        count = alert_aggregations.count + 1,
                        resolved = 0
                    """,
                    (task_id, kind, level, ts, ts),
                )
                row = self._conn.execute(
                    "SELECT count, silenced_until FROM alert_aggregations WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is not None:
                    count = int(row["count"])
                    silenced_until = row["silenced_until"]
                    # 静默期判定: 达到阈值 → 设置/保持静默; 静默期内不再通知。
                    if count >= silence_after and silenced_until is None:
                        self._conn.execute(
                            "UPDATE alert_aggregations SET silenced_until = ? WHERE task_id = ?",
                            (ts + silence_seconds, task_id),
                        )
                        silenced_until = ts + silence_seconds
                    if silenced_until is not None and ts < silenced_until:
                        should_notify = False
            self._conn.commit()
        return int(cur.lastrowid), should_notify

    def resolve(self, task_id: str) -> Optional[dict[str, Any]]:
        """任务恢复 (不再卡死) → 标记聚合告警为已解决并清除静默期。

        返回 {"task_id", "count", "started_at", "duration"} 或 None (无聚合)。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, count, started_at, updated_at, resolved FROM alert_aggregations WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            was_resolved = bool(row["resolved"])
            cur = self._conn.execute(
                "UPDATE alert_aggregations SET resolved = 1, updated_at = ?, silenced_until = NULL "
                "WHERE task_id = ?",
                (time.time(), task_id),
            )
            self._conn.commit()
        if cur.rowcount == 0 and was_resolved:
            return None
        return {
            "task_id": row["task_id"],
            "count": int(row["count"]),
            "started_at": row["started_at"],
            "duration": max(0.0, (time.time() - row["started_at"])),
            "first_resolve": not was_resolved,
        }

    def set_silenced(self, task_id: str, until: Optional[float]) -> bool:
        """手动设置/清除静默期。

        清除 (until=None) 时同时把 count 重置为 0 — 语义: 手动解除静默 =
        重新计数, 避免 record() 的阈值判断立即重设静默。
        """
        with self._lock:
            if until is None:
                cur = self._conn.execute(
                    "UPDATE alert_aggregations SET silenced_until = NULL, count = 0 "
                    "WHERE task_id = ?",
                    (task_id,),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE alert_aggregations SET silenced_until = ? WHERE task_id = ?",
                    (until, task_id),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def list_aggregations(
        self, *, resolved: Optional[bool] = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """查询聚合告警 (按更新时间新→旧)。"""
        query = "SELECT * FROM alert_aggregations WHERE 1=1"
        params: list[Any] = []
        if resolved is not None:
            query += " AND resolved = ?"
            params.append(1 if resolved else 0)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- ② 聚合历史统计 (持久化趋势) ------------------------------------------
    def snapshot_aggregation_history(self, *, day: Optional[str] = None) -> int:
        """把当前聚合状态快照进 aggregation_history (按天, 幂等 upsert)。

        ``day`` 默认今天 (YYYY-MM-DD, 本地时区)。返回写入/更新条数。
        每次调用记录当天每任务的最新 count 与 peak (历史最大 count)。
        """
        import datetime as _dt

        day = day or _dt.date.today().isoformat()
        with self._lock:
            aggs = self._conn.execute("SELECT * FROM alert_aggregations").fetchall()
            for a in aggs:
                task_id = a["task_id"]
                count = int(a["count"])
                # 已有记录 → peak = max(旧 peak, 当前 count); 否则 peak = count。
                self._conn.execute(
                    """
                    INSERT INTO aggregation_history (day, task_id, kind, count, resolved, peak)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, task_id) DO UPDATE SET
                        count = excluded.count,
                        resolved = excluded.resolved,
                        peak = MAX(aggregation_history.peak, excluded.peak)
                    """,
                    (day, task_id, a["kind"], count, 1 if a["resolved"] else 0, count),
                )
            self._conn.commit()
        return len(aggs)

    def aggregation_stats(self, *, days: int = 14) -> dict[str, Any]:
        """告警聚合趋势: 最近 ``days`` 天每日告警总数/解决数/峰值任务。"""
        import datetime as _dt

        start = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, task_id, count, resolved, peak FROM aggregation_history "
                "WHERE day >= ? ORDER BY day",
                (start,),
            ).fetchall()
        # 按天聚合。
        per_day: dict[str, dict[str, Any]] = {}
        top_tasks: dict[str, int] = {}
        for r in rows:
            day = r["day"]
            d = per_day.setdefault(day, {"alerts": 0, "resolved": 0, "tasks": 0})
            d["alerts"] += int(r["count"])
            d["resolved"] += 1 if r["resolved"] else 0
            d["tasks"] += 1
            top_tasks[r["task_id"]] = max(top_tasks.get(r["task_id"], 0), int(r["peak"]))
        return {
            "days": [per_day.get((_dt.date.today() - _dt.timedelta(days=i)).isoformat(),
                                 {"alerts": 0, "resolved": 0, "tasks": 0})
                     for i in range(days - 1, -1, -1)],
            "top_tasks": sorted(top_tasks.items(), key=lambda x: x[1], reverse=True)[:10],
        }

    def aggregation_week_compare(self) -> dict[str, Any]:
        """③ 跨周对比: 本周 (最近 7 天) vs 上周 (前 7 天) 每日告警数。

        返回 {"this_week": [...], "last_week": [...], "total_this", "total_last",
        "delta_pct", "labels": [日期标签...]} — labels 对齐本周 7 天,
        last_week 按同一星期偏移 (上周同日)。
        """
        import datetime as _dt

        today = _dt.date.today()
        # 本周 7 天: [today-6 .. today]; 上周对应: [today-13 .. today-7]。
        this_days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        last_days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(13, 6, -1)]
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, count, resolved FROM aggregation_history "
                "WHERE day >= ? ORDER BY day",
                (last_days[0],),
            ).fetchall()
        by_day: dict[str, int] = {}
        for r in rows:
            by_day[r["day"]] = by_day.get(r["day"], 0) + int(r["count"])
        this_week = [by_day.get(d, 0) for d in this_days]
        last_week = [by_day.get(d, 0) for d in last_days]
        total_this = sum(this_week)
        total_last = sum(last_week)
        delta_pct = (
            ((total_this - total_last) / total_last * 100.0) if total_last else 0.0
        )
        return {
            "labels": [d[5:] for d in this_days],  # MM-DD
            "this_week": this_week,
            "last_week": last_week,
            "total_this": total_this,
            "total_last": total_last,
            "delta_pct": round(delta_pct, 1),
        }

    # -- ③ 操作审计 ------------------------------------------------------------
    def audit(
        self,
        action: str,
        detail: str,
        *,
        task_id: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> int:
        """记录一次管理操作 (回滚/恢复/渠道变更等) — kind='audit' 落 alerts 表。"""
        return self.record("audit", detail, task_id=task_id, ts=ts)[0]

    def list_audit(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """查询操作审计 (kind='audit' 的告警记录, 新→旧)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE kind = 'audit' ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out

    def export_audit(self, *, limit: int = 500, fmt: str = "json") -> str:
        """① 导出操作审计 (CSV/JSON), 返回文本内容。

        CSV 列: id, ts (ISO), task_id, message, payload; JSON 为数组。
        """
        import datetime as _dt
        import io

        rows = self.list_audit(limit=limit)
        if fmt == "csv":
            buf = io.StringIO()
            buf.write("id,ts,task_id,message,payload\n")
            for r in rows:
                ts_iso = _dt.datetime.fromtimestamp(r["ts"]).isoformat()

                def _csv(v):
                    s = "" if v is None else str(v)
                    if any(c in s for c in (",", '"', "\n")):
                        s = '"' + s.replace('"', '""') + '"'
                    return s

                buf.write(
                    f"{r['id']},{_csv(ts_iso)},{_csv(r.get('task_id'))},"
                    f"{_csv(r['message'])},{_csv(json.dumps(r.get('payload') or {}, ensure_ascii=False))}\n"
                )
            return buf.getvalue()
        # JSON 数组。
        return json.dumps(rows, ensure_ascii=False, indent=2)

    # -- ② 告警/审计定时自动归档 -------------------------------------------------
    def archive_old(
        self, *, keep_days: int = 30, now: Optional[float] = None
    ) -> dict[str, Any]:
        """把超过 ``keep_days`` 天的告警/审计记录归档到 alerts_archive。

        返回 {"archived": n, "kept": n}。幂等: 已归档记录不重复。
        """
        now = time.time() if now is None else now
        cutoff = now - keep_days * 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE ts < ?", (cutoff,)
            ).fetchall()
            archived = 0
            for r in rows:
                self._conn.execute(
                    "INSERT OR IGNORE INTO alerts_archive "
                    "(id, kind, task_id, message, ts, payload, archived_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r["id"], r["kind"], r["task_id"], r["message"], r["ts"],
                     r["payload"], now),
                )
                archived += 1
            # 删除已归档的活跃记录。
            self._conn.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))
            self._conn.commit()
        kept = self.count()
        return {"archived": archived, "kept": kept}

    def archive_old_by_channel(
        self,
        *,
        default_keep_days: int = 30,
        channel_keep_days: Optional[dict[str, int]] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """分渠道归档: 渠道相关告警 (task_id 前缀 ``channel:``) 按各自保留天数,
        其余按默认天数。返回 {"archived": n, "kept": n, "by_channel": {...}}。
        """
        now = time.time() if now is None else now
        channel_keep_days = channel_keep_days or {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE ts < ?", (now - default_keep_days * 86400,)
            ).fetchall()
            archived = 0
            by_channel: dict[str, int] = {}
            for r in rows:
                # 渠道相关告警 → 查该渠道保留天数 (无则用默认)。
                days = default_keep_days
                tid = r["task_id"] or ""
                if tid.startswith("channel:"):
                    ch = tid[len("channel:"):]
                    days = channel_keep_days.get(ch, default_keep_days)
                cutoff = now - days * 86400
                if r["ts"] >= cutoff:
                    continue  # 该渠道记录未到期
                self._conn.execute(
                    "INSERT OR IGNORE INTO alerts_archive "
                    "(id, kind, task_id, message, ts, payload, archived_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (r["id"], r["kind"], r["task_id"], r["message"], r["ts"],
                     r["payload"], now),
                )
                archived += 1
                by_channel[ch if tid.startswith("channel:") else "(default)"] = (
                    by_channel.get(ch if tid.startswith("channel:") else "(default)", 0) + 1
                )
            # 删除本次已归档的活跃记录 (仅 ts < 对应 cutoff 的)。
            for r in rows:
                tid = r["task_id"] or ""
                days = default_keep_days
                if tid.startswith("channel:"):
                    days = channel_keep_days.get(tid[len("channel:"):], default_keep_days)
                if r["ts"] < (now - days * 86400):
                    self._conn.execute(
                        "DELETE FROM alerts WHERE id = ?", (r["id"],)
                    )
            self._conn.commit()
        kept = self.count()
        return {"archived": archived, "kept": kept, "by_channel": by_channel}

    def count_archived(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM alerts_archive").fetchone()
        return int(row[0]) if row else 0

    # -- ① 渠道探针历史/健康分 --------------------------------------------------
    def record_probe(
        self, channel: str, ok: bool, *, ms: Optional[float] = None,
        error: Optional[str] = None, ts: Optional[float] = None,
    ) -> int:
        """记录一次渠道探针结果。"""
        ts = time.time() if ts is None else ts
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO probe_history (channel, ok, ms, error, ts) VALUES (?,?,?,?,?)",
                (channel, 1 if ok else 0, ms, error, ts),
            )
            self._conn.commit()
        return int(cur.lastrowid)

    def probe_health(
        self, *, window: int = 200, good_threshold: float = 80.0,
        warn_threshold: float = 50.0,
    ) -> dict[str, Any]:
        """渠道健康分: 最近 window 次探针的成功率/延迟均值 + 历史趋势点。

        返回 {channels: {name: {ok_count, total, success_rate, avg_ms,
        last_ts, rating, trend: [{ts, ok, ms}...]}}} — 健康分 = 成功率 × 100;
        rating: good (≥good_threshold) / warn (<good 且 ≥warn) / bad (<warn)。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, ok, ms, error, ts FROM probe_history "
                "ORDER BY ts DESC LIMIT ?",
                (int(window),),
            ).fetchall()
        channels: dict[str, dict[str, Any]] = {}
        for r in reversed(rows):  # 时间正序
            ch = r["channel"]
            c = channels.setdefault(
                ch,
                {"ok_count": 0, "total": 0, "ms_sum": 0.0, "ms_n": 0,
                 "last_ts": None, "trend": []},
            )
            c["total"] += 1
            if r["ok"]:
                c["ok_count"] += 1
            if r["ms"] is not None:
                c["ms_sum"] += float(r["ms"])
                c["ms_n"] += 1
            c["last_ts"] = r["ts"]
            # 趋势: 保留最近 30 个点。
            if len(c["trend"]) < 30:
                c["trend"].append({"ts": r["ts"], "ok": bool(r["ok"]), "ms": r["ms"]})
        out: dict[str, Any] = {}
        for ch, c in channels.items():
            rate = c["ok_count"] / c["total"] if c["total"] else 0.0
            score = round(rate * 100, 1)
            # 阈值判定 (可配 good/warn 边界)。
            rating = (
                "good" if score >= good_threshold
                else "warn" if score >= warn_threshold
                else "bad"
            )
            out[ch] = {
                "ok_count": c["ok_count"],
                "total": c["total"],
                "success_rate": round(rate, 3),
                "health_score": score,
                "rating": rating,
                "avg_ms": round(c["ms_sum"] / c["ms_n"], 1) if c["ms_n"] else None,
                "last_ts": c["last_ts"],
                "trend": c["trend"],
            }
        return {"channels": out}

    def export_probe_history(self, *, limit: int = 500, fmt: str = "json") -> str:
        """② 导出探针历史 (CSV/JSON), 返回文本内容。

        CSV 列: id, channel, ok, ms, error, ts (ISO); JSON 为数组。
        """
        import datetime as _dt
        import io

        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM probe_history ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        if fmt == "csv":
            buf = io.StringIO()
            buf.write("id,channel,ok,ms,error,ts\n")
            for r in rows:
                ts_iso = _dt.datetime.fromtimestamp(r["ts"]).isoformat()

                def _csv(v):
                    s = "" if v is None else str(v)
                    if any(c in s for c in (",", '"', "\n")):
                        s = '"' + s.replace('"', '""') + '"'
                    return s

                buf.write(
                    f"{r['id']},{_csv(r['channel'])},{'1' if r['ok'] else '0'},"
                    f"{_csv(r['ms'])},{_csv(r['error'])},{_csv(ts_iso)}\n"
                )
            return buf.getvalue()
        # JSON 数组。
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)

    def probe_history_stats(self) -> dict[str, Any]:
        """当前探针历史规模统计 (条数/最早/最新时间), 供保留策略参考。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest "
                "FROM probe_history"
            ).fetchone()
        return {
            "count": int(row["n"]) if row else 0,
            "oldest_ts": row["oldest"] if row else None,
            "newest_ts": row["newest"] if row else None,
        }

    def prune_probe_history(
        self, *, keep_days: int = 30, keep_count: int = 10000,
    ) -> dict[str, Any]:
        """③ 探针历史保留窗口: 删除超过 keep_days 天的旧记录, 且最多保留最新
        keep_count 条 (防 probe_history 无限增长)。

        两个维度都生效: 先按天数清理, 再按条数窗口清理。返回 {removed, kept,
        keep_days, keep_count} — removed 为本次实际删除行数 (去重后)。
        """
        keep_days = max(1, int(keep_days))
        keep_count = max(1, int(keep_count))
        now = time.time()
        with self._lock:
            before = int(
                self._conn.execute("SELECT COUNT(*) FROM probe_history").fetchone()[0]
            )
            # 天数维度: 删除 keep_days 天前的记录。
            self._conn.execute(
                "DELETE FROM probe_history WHERE ts < ?", (now - keep_days * 86400,)
            )
            # 条数维度: 超出 keep_count 时删除最旧的多余记录 (同 ts 边界整批删)。
            row = self._conn.execute(
                "SELECT COUNT(*) FROM probe_history"
            ).fetchone()
            total = int(row[0]) if row else 0
            if total > keep_count:
                boundary = self._conn.execute(
                    "SELECT ts FROM probe_history ORDER BY ts DESC LIMIT 1 OFFSET ?",
                    (keep_count - 1,),
                ).fetchone()
                if boundary is not None:
                    self._conn.execute(
                        "DELETE FROM probe_history WHERE ts < ?", (boundary["ts"],)
                    )
            self._conn.commit()
            after = int(
                self._conn.execute("SELECT COUNT(*) FROM probe_history").fetchone()[0]
            )
        return {
            "removed": max(0, before - after),
            "kept": after,
            "keep_days": keep_days,
            "keep_count": keep_count,
        }

    # -- ③ 归档数据查询/恢复入口 ------------------------------------------------
    def list_archived(
        self,
        *,
        limit: int = 50,
        task_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """查询归档历史 (alerts_archive, 新→旧)。可按任务/时间范围过滤。"""
        query = "SELECT * FROM alerts_archive WHERE 1=1"
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if since is not None:
            query += " AND ts >= ?"
            params.append(since)
        if until is not None:
            query += " AND ts <= ?"
            params.append(until)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out

    def restore_archived(self, archive_id: int) -> Optional[dict[str, Any]]:
        """把一条归档记录恢复到活跃表 (撤销归档), 返回恢复的记录或 None。

        幂等: 已恢复 (活跃表已有同 id) 则直接返回现有活跃记录, 不重复插入。
        """
        with self._lock:
            # 已恢复过? (活跃表已有同 id) → 直接返回活跃记录。
            existing = self._conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (archive_id,)
            ).fetchone()
            if existing is not None:
                d = dict(existing)
                try:
                    d["payload"] = json.loads(d.get("payload") or "{}")
                except (json.JSONDecodeError, TypeError):
                    d["payload"] = {}
                return d
            row = self._conn.execute(
                "SELECT * FROM alerts_archive WHERE id = ?", (archive_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "INSERT INTO alerts (id, kind, task_id, message, ts, payload) "
                "VALUES (?,?,?,?,?,?)",
                (row["id"], row["kind"], row["task_id"], row["message"],
                 row["ts"], row["payload"]),
            )
            # 从归档表移除。
            self._conn.execute(
                "DELETE FROM alerts_archive WHERE id = ?", (archive_id,)
            )
            self._conn.commit()
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        return d

    def list(
        self,
        *,
        limit: int = 50,
        task_id: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """查询告警历史 (新→旧)。可按任务过滤 / 时间范围过滤 (since..until)。"""
        query = "SELECT * FROM alerts WHERE 1=1"
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if since is not None:
            query += " AND ts >= ?"
            params.append(since)
        if until is not None:
            query += " AND ts <= ?"
            params.append(until)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()
        return int(row[0]) if row else 0

    def clear(self, *, task_id: Optional[str] = None) -> int:
        """清理告警历史 (全部或按任务), 返回删除条数。"""
        with self._lock:
            if task_id:
                cur = self._conn.execute(
                    "DELETE FROM alerts WHERE task_id = ?", (task_id,)
                )
            else:
                cur = self._conn.execute("DELETE FROM alerts")
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            logger.debug("alert store close failed", exc_info=True)
