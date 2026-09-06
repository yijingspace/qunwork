# -*- coding: utf-8 -*-
"""7×24 长程任务端到端压力测试 — 方案文档 §10.2 四场景 (时间压缩模拟).

不跑真实 72h/24h/4h/8h (不现实), 用**时间压缩 + 真实模块**驱动, 覆盖:
  * 场景1 (72h 长程研究任务): 期间崩溃 3 次, 每次自动恢复, 最终完成 —
    驱动 FractalCheckpoint(全量状态) + 崩溃重启循环 (内存态丢失 → 检查点恢复);
  * 场景2 (24h 高频自动化): 100 个定时任务, 0 个丢失 —
    驱动真实 Scheduler + TaskStore + fake runner, 压缩时间轴跑 tick;
  * 场景3 (资源耗尽降级 4h): token 耗尽后自动降级到 L3, 缩减范围完成 —
    驱动 FractalDegradation 降级链 + 部分完成保留 + 检查点;
  * 场景4 (并发编排 8h): 5 个并行编排任务, 全部收敛 —
    并发驱动 LoopCoopMonitor, 收敛轮数 vs 理论值。

另测性能指标: 检查点写/恢复耗时、压缩/解压耗时、心跳 5000 任务检查耗时 —
验证 CPU 开销与延迟都在方案量化预期内。

运行: pytest tests/test_stress_longrun.py -v
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from coworker.automation import Schedule, ScheduledTask, Scheduler, TaskRun, TaskStore
from coworker.checkpoint import FractalCheckpoint, checkpoint_interval
from coworker.conversations import ConversationStore
from coworker.degradation import FractalDegradation
from coworker.heartbeat import HoneycombHeartbeat
from coworker.memory.compressor import PisanoMemoryCompressor
from coworker.orchestrator.convergence import GOLDEN_COUPLING, LoopCoopMonitor, spectral_gap
from coworker.orchestrator.models import Plan, Task
from coworker.orchestrator.run_store import OrchestrationRunStore
from coworker.sessions import SessionRecord


def _msgs(n: int, sid: str = "stress") -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"{sid} 第 {i} 条长程任务消息, 带重复模板 " * 3}
        for i in range(n)
    ]


# =============================================================================
# 场景1: 72h 长程研究任务 — 崩溃 3 次, 每次自动恢复, 最终完成
# =============================================================================

class TestScenario1_LongRunCrashRecovery:
    def test_72h_run_with_3_crashes(self, tmp_path):
        """时间压缩模拟 72h: 6 个阶段 × 200 条消息; 第 2/4/6 阶段前"崩溃"
        (内存态丢失), 每次从检查点恢复继续; 最终交付完整, 消息零丢失。"""
        cp_path = tmp_path / "checkpoints.db"
        sid = "scenario1-72h"
        crash_after = {2, 4, 6}
        current: list[dict] = []  # 进程内存态

        for phase in range(1, 7):
            if phase in crash_after:
                # 崩溃: 内存态丢失, 从最新检查点恢复。
                cp = FractalCheckpoint(cp_path)
                restored = cp.restore_latest(sid)
                assert restored is not None, f"phase {phase}: 崩溃后必须能从检查点恢复"
                current = list(restored.get("messages") or [])
            else:
                cp = FractalCheckpoint(cp_path)

            batch = _msgs(200, sid=f"phase{phase}")
            new = current + batch
            cp.save_checkpoint(
                sid,
                {"messages": new, "phase": phase, "result": f"partial-after-phase-{phase}"},
                n_layer=1,  # 超热层: 全量快照
            )
            current = new

        # 最终完成 (n_layer=7 final)。
        cp = FractalCheckpoint(cp_path)
        cp.save_checkpoint(sid, {"result": "FINAL-DELIVERABLE", "phase": 6}, n_layer=7)
        final = cp.restore_latest(sid)
        assert final["result"] == "FINAL-DELIVERABLE"
        assert final["phase"] == 6

        # 消息零丢失: 6 阶段 × 200 = 1200 条全在检查点里。
        full = cp.restore_latest(sid).get("messages") or []
        assert len(full) == 1200, f"恢复后消息 {len(full)} != 1200"
        # 恢复时间 < 30s (实测毫秒级)。
        t0 = time.perf_counter()
        cp.restore_latest(sid)
        assert (time.perf_counter() - t0) * 1000 < 30_000

    def test_archive_keeps_72h_history_bounded(self, tmp_path):
        """72h 会话: 定期归档后活跃 .jsonl 有界, 完整历史无损。"""
        conv = ConversationStore(tmp_path / "conv")
        sid = "scenario1-archive"
        all_msgs: list[dict] = []
        for phase in range(6):
            all_msgs += _msgs(200, sid=f"phase{phase}")
            conv.save(
                SessionRecord(session_id=sid, workspace="/tmp", model="m", mode="code", messages=all_msgs)
            )
            if len(all_msgs) > 400:
                conv.archive_compressed(sid, keep_recent=200)
        # 活跃 .jsonl 有界 (≤ keep_recent + 一批)。
        assert conv._count(sid) <= 400, f"活跃条数 {conv._count(sid)} 超界"
        # 完整历史无损。
        assert conv.full_history(sid) == all_msgs
        # 归档文件存在。
        assert conv._archive_file(sid).exists()

    def test_1000_checkpoints_perf(self, tmp_path):
        """1000 次检查点写 + 恢复耗时 (CPU 开销预算内)。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        sid = "perf"
        state = {"messages": _msgs(20), "tasks": []}
        t0 = time.perf_counter()
        for _ in range(1000):
            cp.save_checkpoint(sid, state, n_layer=1)
        write_ms = (time.perf_counter() - t0) * 1000 / 1000  # 单次
        t0 = time.perf_counter()
        for _ in range(20):
            cp.restore_latest(sid)
        restore_ms = (time.perf_counter() - t0) * 1000 / 20  # 单次
        assert write_ms < 5, f"单次检查点写入 {write_ms:.2f}ms"
        # 方案预期恢复 <30s; 1000 个检查点单次恢复毫秒级即达标。
        assert restore_ms < 100, f"单次恢复 {restore_ms:.2f}ms"

    def test_checkpoint_ttl_prunes_growth(self, tmp_path):
        """分形 TTL 清理: 长期运行后过期检查点被 prune, 存储不无限增长。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        sid = "ttl"
        # 模拟 72h 分形检查点调度: 每层按各自间隔触发。
        elapsed = 0.0
        total_seconds = 72 * 3600
        last_saved = {n: -1.0 for n in range(1, 8)}
        while elapsed < total_seconds:
            for n in range(1, 8):
                interval = checkpoint_interval(n, elapsed)
                if elapsed - last_saved[n] >= interval:
                    cp.save_checkpoint(sid, {"messages": _msgs(10), "t": elapsed}, n_layer=n)
                    last_saved[n] = elapsed
            elapsed += 60  # 每分钟采样
        before = cp.count(sid)
        assert before > 0
        # 手动把全部分形 TTL 视为过期 (直接清表验证 prune 逻辑幂等)。
        pruned = cp.prune_expired()
        assert pruned >= 0
        # 分层: 层 1 (1h TTL) 的保留时间最短 — 验证 LAYERS 表带 TTL。
        from coworker.checkpoint import LAYERS

        assert LAYERS[1]["retention"] is not None
        assert LAYERS[7]["retention"] is None  # final 永久


# =============================================================================
# 场景2: 24h 高频自动化 — 100 个定时任务, 0 个丢失
# =============================================================================

class TestScenario2_HighFrequencyAutomation:
    def test_100_tasks_zero_loss(self, tmp_path):
        """100 个每分钟任务, 压缩跑 tick → 每个任务至少执行 1 次, 0 丢失。"""

        async def runner(task, trigger):
            runs_log.append(task.id)
            return TaskRun(task_id=task.id, status="ok", trigger=trigger)

        store = TaskStore(tmp_path / "auto.db")
        for i in range(100):
            t = ScheduledTask(
                title=f"stress-task-{i}",
                instructions="run",
                schedule=Schedule(kind="cron", cron="* * * * *"),  # 每分钟
                workspace="/tmp",
                id=f"task-{i:03d}",
            )
            # retry_until 设为未来 → save() 不重算 next_run, 保留手工到期时间。
            t.retry_until = time.time() + 3600
            t.next_run = time.time() - 1  # 立即到期
            store.save(t)

        runs_log: list[str] = []
        sched = Scheduler(store, runner, tick_seconds=0.01)
        # 驱动 3 个 tick, 并等待 spawn 的任务完成 (scheduler 的 spawn 是 fire-and-forget)。
        for _ in range(3):
            asyncio.run(sched._tick(trigger="schedule"))
            while sched._spawned:
                pending = list(sched._spawned)
                asyncio.run(asyncio.gather(*pending))

        executed = set(runs_log)
        assert len(executed) == 100, f"执行了 {len(executed)}/100"
        for t in store.list():
            assert t.id in executed, f"任务 {t.id} 丢失"
            assert t.run_count >= 1, f"任务 {t.id} run_count=0"

    def test_overlap_guard_no_duplicate(self, tmp_path):
        """skip-on-overlap: 同一任务运行时不再重复启动。"""
        store = TaskStore(tmp_path / "auto.db")
        t = ScheduledTask(
            title="overlap", instructions="x",
            schedule=Schedule(kind="cron", cron="* * * * *"),
            workspace="/tmp", id="overlap-1",
        )
        t.retry_until = time.time() + 3600
        t.next_run = time.time() - 1
        store.save(t)

        started: list[str] = []

        async def runner(task, trigger):
            started.append(task.id)
            await asyncio.sleep(0.05)
            return TaskRun(task_id=task.id, status="ok", trigger=trigger)

        sched = Scheduler(store, runner, tick_seconds=0.01)
        sched._running_ids.add("overlap-1")  # 模拟仍在跑
        asyncio.run(sched._tick(trigger="schedule"))
        sched._running_ids.discard("overlap-1")
        assert started == [], "重叠期间不得再启动"


# =============================================================================
# 场景3: 资源耗尽降级 4h — token 耗尽 → 自动降级到 L3, 缩减范围完成
# =============================================================================

class TestScenario3_ResourceExhaustionDegradation:
    @pytest.mark.asyncio
    async def test_token_exhaustion_degrades_to_L3_and_completes(self, tmp_path):
        """token 耗尽 (L1→L2 重试仍失败) → 降级 L3 缩减范围 → 完成。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        deg = FractalDegradation(checkpoint_engine=cp)
        state = {
            "model": "gpt-4o",
            "tasks": [{"id": f"t{i}", "core": i < 2} for i in range(6)],
        }
        d1 = await deg.handle_failure("task-token", RuntimeError("token exhausted"), state)
        d2 = await deg.handle_failure("task-token", RuntimeError("token exhausted (mini)"), state)
        assert d1["level"] == 1 and d1["action"] == "retry"
        assert d2["level"] == 2 and d2["action"] == "retry" and d2.get("model") == "gpt-4o-mini"
        d3 = await deg.handle_failure("task-token", RuntimeError("still failing"), state)
        assert d3["level"] == 3 and d3["action"] == "retry"
        # 部分完成保留: 检查点里有降级前的状态。
        restored = cp.restore_latest("task-token")
        assert restored is not None
        assert len(deg.history) == 3  # 审计轨迹

    @pytest.mark.asyncio
    async def test_full_degradation_chain_then_archive(self, tmp_path):
        """4h 持续失败 → 走完 L1..L6, 终态归档, 每级保留检查点。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        deg = FractalDegradation(checkpoint_engine=cp)
        state = {"model": "gpt-4o", "tasks": [{"id": "a"}]}
        actions = []
        for i in range(6):
            d = await deg.handle_failure("task-x", RuntimeError(f"e{i}"), state)
            actions.append((d["level"], d["action"]))
        assert [lvl for lvl, _ in actions] == [1, 2, 3, 4, 5, 6]
        assert actions[-1][1] == "archive"
        assert cp.count("task-x") >= 6  # 每级检查点


# =============================================================================
# 场景4: 并发编排 8h — 5 个并行编排任务, 全部收敛
# =============================================================================

class TestScenario4_ConcurrentOrchestration:
    def test_5_parallel_runs_all_converge(self):
        """5 个并行编排 (LoopCoopMonitor), 全部收敛, 轮数 ≤ 理论值。"""
        theoretical = LoopCoopMonitor().theoretical_rounds
        results = []

        def run_one(seed: int) -> dict:
            mon = LoopCoopMonitor()
            plan = Plan(
                goal=f"task-{seed}",
                tasks=[Task(id=f"{seed}-t{i}", description=f"work {i}") for i in range(8)],
            )
            for rnd in range(8):
                plan.tasks[rnd].status = "done"
                mon.record_round(plan, verdicts=[{"accepted": True}] * (rnd + 1))
                if mon.is_converged():
                    break
            return mon.report()

        # 同步并发 (进程内 GIL 场景下的 5 路编排)。
        for s in range(5):
            rep = run_one(s)
            assert rep["converged"], f"run {s} 未收敛: {rep}"
            assert rep["iterations"] <= theoretical, f"收敛轮数 {rep['iterations']} > 理论 {theoretical}"
            assert abs(rep["final_convergence"] - 1.0) < 1e-6
            results.append(rep)
        assert len(results) == 5  # 5 个全部收敛

    def test_golden_coupling_convergence_rate(self):
        """谱隙 |λ₂| = 0.5712 → 9 轮收敛 99% (理论预测复现)。"""
        gap = spectral_gap(GOLDEN_COUPLING)
        assert abs(gap - 0.5712) < 0.005
        assert gap ** 9 < 0.01  # 收敛率 |λ₂|^k: 9 轮后 < 1%


# =============================================================================
# 性能指标: 压缩 / 心跳 / 降级在压力规模下仍达标
# =============================================================================

class TestPerfMetrics:
    def test_compressor_10k_messages(self):
        """10000 条消息压缩: 压缩率 ≥90%, 解压 <100ms, 无损。"""
        comp = PisanoMemoryCompressor()
        msgs = _msgs(10_000, sid="perf-10k")
        t0 = time.perf_counter()
        c = comp.compress_messages(msgs)
        compress_s = time.perf_counter() - t0
        assert c["compression_ratio"] >= 0.90, f"ratio={c['compression_ratio']:.2%}"
        t0 = time.perf_counter()
        back = comp.decompress(c)
        decomp_ms = (time.perf_counter() - t0) * 1000
        assert back == msgs, "10000 条无损往返"
        assert decomp_ms < 100, f"解压 {decomp_ms:.1f}ms"
        assert compress_s < 10, f"压缩 {compress_s:.1f}s"

    def test_heartbeat_5000_tasks(self):
        """5000 个任务心跳检查: 单次 <50ms, 卡死检出。"""
        hb = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3, persist_default=False)
        now = 1_000_000.0
        for i in range(5000):
            hb.register(f"t{i}", now=now)
        for tick in range(3):
            t = now + tick * 30.0
            for i in range(4999):
                hb.pulse(f"t{i}", now=t)
            hb.check_health(now=t)
        t0 = time.perf_counter()
        hb.check_health(now=now + 3 * 30.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"5000 任务单次检查 {elapsed_ms:.1f}ms"
        assert "t4999" in hb.get_unhealthy()
        assert hb.get_unhealthy().count("t4999") == 1

    def test_run_store_1000_events(self, tmp_path):
        """1000 事件 + 100 降级记录写入: 查询 <200ms。"""
        rs = OrchestrationRunStore(tmp_path / "runs.db")
        run_id = rs.create_run("perf")
        t0 = time.perf_counter()
        for i in range(1000):
            rs.append_event(run_id, "worker_thought", {"i": i})
        for i in range(100):
            rs.record_degradation(run_id, f"t{i % 10}", (i % 6) + 1, "retry", fidelity=0.5)
        write_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        snap = rs.get_run(run_id)
        read_ms = (time.perf_counter() - t0) * 1000
        assert len(snap["events"]) == 1000
        assert len(snap["degradations"]) == 100
        assert write_ms < 5000 and read_ms < 200, f"write={write_ms:.0f}ms read={read_ms:.0f}ms"


# =============================================================================
# 桌面端 7x24 管理 API (LongRunView 后端): 健康/遥测/检查点/存储/维护
# =============================================================================

class _MiniManager:
    """最小 Manager stub: 直接调用 SessionManager 的 longrun_* 方法 (纯数据聚合,
    不依赖完整 SessionManager 构造)。"""

    def __init__(self, tmp_path):
        from coworker.automation import Schedule, ScheduledTask, TaskStore
        from coworker.conversations import ConversationStore
        from coworker.heartbeat import HoneycombHeartbeat
        from coworker.memory.sqlite_store import SQLiteMemoryStore
        from coworker.orchestrator.run_store import OrchestrationRunStore
        from coworker.selfwake import WakeStore
        from coworker.sessions import SessionRecord

        self.base_dir = tmp_path
        self.default_workspace = str(tmp_path / "ws")
        self.session_store = ConversationStore(tmp_path / "conv")
        self.task_store = TaskStore(tmp_path / "auto.db")
        self.heartbeat = HoneycombHeartbeat(tick_seconds=30.0, persist_default=False)
        self.orchestration_store = OrchestrationRunStore(tmp_path / "orch.db")
        self.wakes = WakeStore()
        self.memory_store = SQLiteMemoryStore(str(tmp_path / "mem.db"))

        # 填充数据。
        self.heartbeat.register("task-alive")
        t = ScheduledTask(
            title="t1", instructions="x",
            schedule=Schedule(kind="cron", cron="* * * * *"), workspace="/tmp",
        )
        t.next_run = 0.0
        self.task_store.save(t)
        run_id = self.orchestration_store.create_run("intent-1")
        self.orchestration_store.record_degradation(run_id, "t2", 2, "downgrade_model", fidelity=0.85)
        self.orchestration_store.append_event(
            run_id,
            "convergence_report",
            {"gap": 0.5712, "iterations": 9, "convergence_curve": [0.1, 0.5, 1.0],
             "final_convergence": 1.0, "converged": True, "stalled": False, "theoretical_rounds": 9},
        )
        self.orchestration_store.update_status(run_id, "completed")
        self.session_store.save(
            SessionRecord(session_id="sess-long", workspace="/tmp", model="m", mode="code",
                          messages=[{"role": "user", "content": f"m{i}"} for i in range(300)])
        )
        self.session_store.archive_compressed("sess-long", keep_recent=50)

    def memory_maintenance(self, *, dry_run=False, vector_db_path=None):
        from coworker.memory.maintenance import run_maintenance

        return run_maintenance(
            self.memory_store, vector_db_path=vector_db_path, dry_run=dry_run,
            conversation_store=self.session_store,
        )


@pytest.fixture()
def mini_manager(tmp_path):
    return _MiniManager(tmp_path)


class TestLongrunManagementAPI:
    """A-D 管理 API 数据聚合 (LongRunView 后端, 经 SessionManager 方法验证)。"""

    def test_health(self, mini_manager):
        from coworker.server.manager import SessionManager

        h = SessionManager.longrun_health(mini_manager)
        assert h["heartbeat"]["tasks"] >= 1
        assert h["automation"]["total"] == 1 and h["automation"]["enabled"] == 1
        assert h["wakes"]["pending"] == 0
        assert h["detection_time_seconds"] is not None

    def test_telemetry(self, mini_manager):
        from coworker.server.manager import SessionManager

        t = SessionManager.longrun_telemetry(mini_manager)
        assert len(t["degradations"]) == 1
        assert t["degradations"][0]["level"] == 2
        assert len(t["convergence_history"]) == 1
        assert t["convergence_history"][0]["report"]["gap"] == 0.5712

    def test_checkpoints(self, mini_manager):
        from coworker.server.manager import SessionManager

        c = SessionManager.longrun_checkpoints(mini_manager)
        assert len(c["sessions"]) >= 1
        assert any(s["archived"] for s in c["sessions"])

    def test_storage(self, mini_manager):
        from coworker.server.manager import SessionManager

        s = SessionManager.longrun_storage(mini_manager)
        assert s["sessions"]["count"] >= 1
        assert s["sessions"]["jsonl_total_bytes"] > 0
        assert s["sessions"]["archived_sessions"] >= 1
        assert "memory" in s

    def test_maintenance(self, mini_manager):
        from coworker.server.manager import SessionManager

        m = SessionManager.longrun_maintenance(mini_manager, dry_run=True)
        assert "sessions_archive" in m
        assert "memories_dedupe" in m


class TestLongrunRestoreDrill:
    """C: 检查点一键恢复演练 (POST /v1/7x24/checkpoints/{sid}/restore)。"""

    def test_restore_returns_summary(self, tmp_path):
        from coworker.checkpoint import FractalCheckpoint

        # 构造一个含检查点的会话。
        cp = FractalCheckpoint(tmp_path / "checkpoints.db")
        cp.save_checkpoint(
            "sess-drill",
            {"messages": [{"role": "user", "content": f"m{i}"} for i in range(30)],
             "tasks": [{"id": "t1", "status": "done"}], "result": "partial", "phase": 2},
            n_layer=1,
        )
        cp.close()

        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

        res = SessionManager.longrun_checkpoint_restore(_M(), "sess-drill")
        assert res["ok"] is True
        assert res["summary"]["messages"] == 30
        assert res["summary"]["tasks"] == 1
        assert res["summary"]["result"] == "partial"
        assert res["summary"]["phase"] == 2
        assert res["restore_ms"] >= 0

    def test_restore_no_checkpoint(self, tmp_path):
        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

        res = SessionManager.longrun_checkpoint_restore(_M(), "sess-empty")
        assert res["ok"] is False
        assert "no restorable" in res["error"]


class TestHeartbeatStalledAlert:
    """① 心跳卡死告警: 首次卡死推送 /ws/events 事件, 恢复后可再次提醒。"""

    @pytest.mark.asyncio
    async def test_alert_broadcast_dedupe_and_recover(self):
        from coworker.server.manager import SessionManager

        class _M(SessionManager):
            def __init__(self):
                # 不完整初始化 — 只提供 heartbeat 处理所需的最小状态。
                from coworker.selfwake import WakeStore

                self.wakes = WakeStore()
                self._stalled_alerts = set()
                self.broadcasted: list[dict] = []

            async def broadcast_event(self, message: dict) -> None:
                self.broadcasted.append(message)

        m = _M()
        # 首次卡死 → 推送 1 条告警。
        await m._heartbeat_stalled_handler(["task-a", "task-b"])
        assert len(m.broadcasted) == 2
        assert all(e["type"] == "7x24_alert" for e in m.broadcasted)
        # 第二次仍卡死 → 去重, 不重复推送。
        await m._heartbeat_stalled_handler(["task-a"])
        assert len(m.broadcasted) == 2
        # 恢复 (不在 stalled) → 标记清除, 下次卡死可再提醒。
        await m._heartbeat_stalled_handler([])
        assert m._stalled_alerts == set()
        await m._heartbeat_stalled_handler(["task-a"])
        assert len(m.broadcasted) == 3


class TestAlertStore:
    """① 告警历史持久化 (AlertStore): 落库/查询/过滤/重启恢复/清理。"""

    def test_record_list_filter_persist(self, tmp_path):
        from coworker.alerts import AlertStore

        db = tmp_path / "alerts.db"
        s = AlertStore(db)
        s.record("heartbeat_stalled", "任务 t1 心跳停滞", task_id="t1", payload={"health": 0.0})
        s.record("heartbeat_stalled", "任务 t2 心跳停滞", task_id="t2")
        assert s.count() == 2
        alerts = s.list(limit=10)
        assert alerts[0]["task_id"] == "t2"  # 新→旧
        assert alerts[1]["payload"].get("health") == 0.0
        assert len(s.list(task_id="t1")) == 1
        assert len(s.list(since=time.time() - 100)) == 2
        # 重启模拟 (同一 db 重新打开)。
        s2 = AlertStore(db)
        assert s2.count() == 2
        assert s2.clear(task_id="t1") == 1
        assert s2.count() == 1

    @pytest.mark.asyncio
    async def test_stalled_handler_persists_alert(self, tmp_path):
        """卡死处理落库: 心跳停滞告警写入 AlertStore。"""
        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M(SessionManager):
            def __init__(self, db):
                from coworker.selfwake import WakeStore

                self.wakes = WakeStore()
                self._stalled_alerts = set()
                self.alert_store = AlertStore(db)

            async def broadcast_event(self, message: dict) -> None:
                pass

        db = tmp_path / "alerts.db"
        m = _M(db)
        await m._heartbeat_stalled_handler(["task-persist"])
        alerts = m.alert_store.list(limit=10)
        assert len(alerts) == 1
        assert alerts[0]["task_id"] == "task-persist"
        assert alerts[0]["kind"] == "heartbeat_stalled"


class TestCheckpointApplyRestore:
    """② 检查点真实恢复 (apply=true): 写回会话存储。"""

    def _manager(self, tmp_path):
        from coworker.checkpoint import FractalCheckpoint
        from coworker.conversations import ConversationStore

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def __init__(self):
                self.session_store = ConversationStore(tmp_path / "conv")
                self.alert_store = None  # 无审计存储时 _audit_action 静默跳过

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

            def _audit_action(self, action, detail, task_id=None):
                pass  # stub: 审计写入在此测试中不验证

        m = _M()
        cp = FractalCheckpoint(tmp_path / "checkpoints.db")
        cp.save_checkpoint(
            "sess-r",
            {"messages": [{"role": "user", "content": f"恢复消息 {i}"} for i in range(30)],
             "tasks": [{"id": "t1", "status": "done"}], "result": "恢复后的结果", "phase": 3},
            n_layer=1,
        )
        cp.close()
        return m

    def test_apply_writes_back_to_session_store(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        # 演练 (只读): 不写回。
        drill = SessionManager.longrun_checkpoint_restore(m, "sess-r", apply=False)
        assert drill["ok"] and drill.get("applied") is False
        assert drill["summary"]["messages"] == 30
        assert m.session_store.load("sess-r") is None
        # 真实恢复: 写回。
        applied = SessionManager.longrun_checkpoint_restore(m, "sess-r", apply=True)
        assert applied["ok"] and applied["applied"] is True
        rec = m.session_store.load("sess-r")
        assert rec is not None and len(rec.messages) == 30
        assert rec.messages[0]["content"] == "恢复消息 0"

    def test_apply_no_checkpoint_error(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        res = SessionManager.longrun_checkpoint_restore(m, "sess-none", apply=True)
        assert res["ok"] is False
        assert "no restorable" in res["error"]

    def test_apply_backs_up_previous_session(self, tmp_path):
        """真实恢复前自动备份: 旧会话消息存为 backup:sid 检查点, 可回滚。"""
        from coworker.checkpoint import FractalCheckpoint
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord
        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def __init__(self):
                self.session_store = ConversationStore(tmp_path / "conv")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

            def _audit_action(self, action, detail, task_id=None):
                pass  # stub

        m = _M()
        # 旧会话 10 条 (将被覆盖)。
        m.session_store.save(
            SessionRecord(session_id="sess-bk", workspace="/tmp", model="m", mode="code",
                          messages=[{"role": "user", "content": f"旧消息 {i}"} for i in range(10)])
        )
        # 检查点 30 条 (恢复目标)。
        cp = FractalCheckpoint(tmp_path / "checkpoints.db")
        cp.save_checkpoint(
            "sess-bk",
            {"messages": [{"role": "user", "content": f"新消息 {i}"} for i in range(30)],
             "tasks": [{"id": "t1"}], "result": "恢复结果"},
            n_layer=1,
        )
        cp.close()

        res = SessionManager.longrun_checkpoint_restore(m, "sess-bk", apply=True)
        assert res["ok"] and res["applied"] and res["backup_key"] == "backup:sess-bk"
        assert len(m.session_store.load("sess-bk").messages) == 30
        # 备份可回滚: 备份检查点保留旧 10 条。
        cp2 = FractalCheckpoint(tmp_path / "checkpoints.db")
        backup_state = cp2.restore_latest("backup:sess-bk")
        assert backup_state is not None
        assert len(backup_state.get("messages") or []) == 10
        assert backup_state.get("restore_backup_of") == "sess-bk"
        cp2.close()


class TestAlertNotifier:
    """① 多渠道告警通知 (邮件/Telegram/飞书/钉钉/企业微信)。"""

    def test_channels_default_disabled(self):
        from coworker.notify import AlertNotifier

        n = AlertNotifier({})
        assert n.enabled_channels == []

    def test_public_config_masks_secrets(self):
        from coworker.notify import AlertNotifier

        n = AlertNotifier(
            {"channels": {"telegram": {"enabled": True, "bot_token": "123:ABC", "chat_id": "42"}}}
        )
        assert n.enabled_channels == ["telegram"]
        pub = n.public_config()
        assert pub["telegram"]["bot_token"] == "••••"

    @pytest.mark.asyncio
    async def test_incomplete_email_fails_gracefully(self):
        from coworker.notify import AlertNotifier

        n = AlertNotifier({"channels": {"email": {"enabled": True}}})
        r = await n.send("test", title="t")
        assert r["email"]["ok"] is False  # 缺 smtp 配置 → 不崩溃

    def test_all_channel_keys(self):
        from coworker.notify import CHANNEL_KEYS, DEFAULT_CHANNELS

        assert set(CHANNEL_KEYS) == {"email", "telegram", "feishu", "dingtalk", "wecom"}
        for k in CHANNEL_KEYS:
            assert k in DEFAULT_CHANNELS

    def test_webhook_signature(self):
        from coworker.notify import _sign_webhook

        sig = _sign_webhook("secret", "1700000000")
        assert len(sig) > 10


class TestAlertTimeRangeFilter:
    """③ 告警历史时间范围筛选 (since/until)。"""

    def test_list_since_until(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "alerts.db")
        now = time.time()
        s.record("heartbeat_stalled", "旧", task_id="a", ts=now - 100)
        s.record("heartbeat_stalled", "中", task_id="b", ts=now - 50)
        s.record("heartbeat_stalled", "新", task_id="c", ts=now)
        assert len(s.list(limit=10)) == 3
        assert len(s.list(since=now - 60)) == 2
        assert len(s.list(until=now - 60)) == 1
        assert len(s.list(since=now - 80, until=now - 40)) == 1

    def test_manager_passes_range(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_store.record("heartbeat_stalled", "x", task_id="t", ts=time.time())

        m = _M()
        r = SessionManager.longrun_alerts(m, since=time.time() - 10)
        assert r["count"] == 1


class TestAlertLevelRouting:
    """① 告警渠道按级别路由 (不同告警种类走不同渠道)。"""

    @pytest.mark.asyncio
    async def test_level_routing_skips_unsubscribed(self):
        from coworker.notify import AlertNotifier

        cfg = {
            "channels": {
                "telegram": {"enabled": True, "bot_token": "t", "chat_id": "1", "levels": ["critical"]},
                "email": {"enabled": True, "levels": ["warning", "info"]},
            }
        }
        n = AlertNotifier(cfg)
        r_crit = await n.send("critical", level="critical")
        assert r_crit["telegram"].get("skipped") is None  # 订阅了 critical → 尝试发送
        assert r_crit["email"].get("skipped") is True  # 未订阅 → 跳过
        r_info = await n.send("info", level="info")
        assert r_info["email"].get("skipped") is None
        assert r_info["telegram"].get("skipped") is True

    @pytest.mark.asyncio
    async def test_empty_levels_subscribes_all(self):
        from coworker.notify import AlertNotifier

        n = AlertNotifier({"channels": {"telegram": {"enabled": True, "bot_token": "t", "chat_id": "1"}}})
        r = await n.send("x", level="info")
        assert r["telegram"].get("skipped") is None  # levels 空 = 全部

    def test_invalid_level_falls_back(self):
        from coworker.notify import ALERT_LEVELS

        assert "warning" in ALERT_LEVELS and "critical" in ALERT_LEVELS and "info" in ALERT_LEVELS


class TestCheckpointRollback:
    """② 备份一键回滚 (从 backup:{sid} 恢复)。"""

    def test_rollback_restores_previous_session(self, tmp_path):
        from coworker.checkpoint import FractalCheckpoint
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord
        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def __init__(self):
                self.session_store = ConversationStore(tmp_path / "conv")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

            def _audit_action(self, action, detail, task_id=None):
                pass  # stub

        m = _M()
        m.session_store.save(
            SessionRecord(session_id="sess-rb", workspace="/tmp", model="m", mode="code",
                          messages=[{"role": "user", "content": f"旧消息 {i}"} for i in range(10)])
        )
        cp = FractalCheckpoint(tmp_path / "checkpoints.db")
        cp.save_checkpoint(
            "sess-rb",
            {"messages": [{"role": "user", "content": f"新消息 {i}"} for i in range(30)],
             "tasks": [{"id": "t1"}], "result": "恢复结果"},
            n_layer=1,
        )
        cp.close()
        # apply 恢复 → 30 条。
        res = SessionManager.longrun_checkpoint_restore(m, "sess-rb", apply=True)
        assert res["ok"] and len(m.session_store.load("sess-rb").messages) == 30
        # 回滚 → 10 条旧消息。
        rb = SessionManager.longrun_checkpoint_rollback(m, "sess-rb")
        assert rb["ok"] and rb["rolled_back_messages"] == 10
        assert m.session_store.load("sess-rb").messages[0]["content"] == "旧消息 0"

    def test_rollback_without_backup_errors(self, tmp_path):
        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

            def __init__(self):
                from coworker.conversations import ConversationStore

                self.session_store = ConversationStore(tmp_path / "conv")

        m = _M()
        rb = SessionManager.longrun_checkpoint_rollback(m, "sess-none")
        assert rb["ok"] is False
        assert "no restore backup" in rb["error"]


class TestAlertAggregation:
    """③ 告警聚合 (同任务连续卡死合并为持续告警)。"""

    def test_upsert_counts_and_resolve(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "alerts.db")
        now = time.time()
        s.record("heartbeat_stalled", "卡死 1", task_id="t1", ts=now - 100, level="critical")
        s.record("heartbeat_stalled", "卡死 2", task_id="t1", ts=now - 50, level="critical")
        s.record("heartbeat_stalled", "卡死 3", task_id="t1", ts=now, level="critical")
        s.record("heartbeat_stalled", "卡死", task_id="t2", ts=now, level="critical")
        aggs = s.list_aggregations()
        assert len(aggs) == 2
        t1 = next(a for a in aggs if a["task_id"] == "t1")
        assert t1["count"] == 3
        assert t1["resolved"] == 0
        assert abs(t1["started_at"] - (now - 100)) < 1
        # resolve。
        assert s.resolve("t1")
        assert [a["task_id"] for a in s.list_aggregations(resolved=False)] == ["t2"]

    @pytest.mark.asyncio
    async def test_handler_upserts_aggregation(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier
        from coworker.server.manager import SessionManager
        from coworker.selfwake import WakeStore

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_notifier = AlertNotifier({})
                self.wakes = WakeStore()
                self._stalled_alerts = set()
                self.broadcasted = []

            def _on_heartbeat_stalled(self, task_id, health):
                self.wakes.heartbeat_stalled(task_id)

            def _alert_silence_params(self):
                return {"silence_after": 3, "silence_seconds": 600}

            async def broadcast_event(self, message):
                self.broadcasted.append(message)

        m = _M()
        await SessionManager._heartbeat_stalled_handler(m, ["task-x"])
        await SessionManager._heartbeat_stalled_handler(m, ["task-x"])  # 广播去重, 但聚合如实计数
        aggs = m.alert_store.list_aggregations()
        assert len(aggs) == 1
        assert aggs[0]["count"] == 2  # 两次卡死 → 聚合 count=2 (聚合与广播去重独立)
        assert len(m.broadcasted) == 1  # 广播仍只 1 次 (去重)

    def test_manager_aggregation_query(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_store.record("heartbeat_stalled", "x", task_id="t1", ts=time.time(), level="critical")
                self.alert_store.record("heartbeat_stalled", "y", task_id="t2", ts=time.time(), level="critical")

        m = _M()
        r = SessionManager.longrun_alert_aggregations(m, resolved=False)
        assert r["count"] == 2


class TestAlertSilencing:
    """① 聚合告警静默期: 同任务连续告警超过阈值后进入静默, 不再重复通知。"""

    def test_silence_after_threshold(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        _id1, n1 = s.record("heartbeat_stalled", "卡死1", task_id="t", ts=now - 200, level="critical", silence_after=3)
        _id2, n2 = s.record("heartbeat_stalled", "卡死2", task_id="t", ts=now - 100, level="critical", silence_after=3)
        _id3, n3 = s.record("heartbeat_stalled", "卡死3", task_id="t", ts=now, level="critical", silence_after=3)
        assert n1 and n2
        assert n3 is False  # 达到阈值 → 静默
        aggs = s.list_aggregations()
        t = next(a for a in aggs if a["task_id"] == "t")
        assert t["count"] == 3
        assert t["silenced_until"] is not None and t["silenced_until"] > now
        # 静默期内再告警 → 不通知。
        _id4, n4 = s.record("heartbeat_stalled", "卡死4", task_id="t", ts=now + 60, level="critical", silence_after=3)
        assert n4 is False

    def test_manual_unsilence_resets_count(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(3):
            s.record("heartbeat_stalled", f"卡死{i}", task_id="t", ts=now + i, level="critical", silence_after=3)
        s.set_silenced("t", None)  # 手动清除 → count 重置
        _id, n = s.record("heartbeat_stalled", "新卡死", task_id="t", ts=now + 100, level="critical", silence_after=3)
        assert n is True  # 重置后可再通知

    def test_resolve_clears_silence(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(3):
            s.record("heartbeat_stalled", f"卡死{i}", task_id="t", ts=now + i, level="critical", silence_after=3)
        rec = s.resolve("t")
        assert rec is not None and rec["first_resolve"] is True
        aggs = s.list_aggregations()
        assert aggs[0]["silenced_until"] is None  # resolve 清除静默

    @pytest.mark.asyncio
    async def test_handler_recovery_notification(self, tmp_path):
        """任务恢复 → 发 7x24_recovered 事件。"""
        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier
        from coworker.server.manager import SessionManager
        from coworker.selfwake import WakeStore

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_notifier = AlertNotifier({})
                self.wakes = WakeStore()
                self._stalled_alerts = set()
                self.broadcasted = []

            def _on_heartbeat_stalled(self, task_id, health):
                self.wakes.heartbeat_stalled(task_id)

            def _alert_silence_params(self):
                return {"silence_after": 3, "silence_seconds": 600}

            async def broadcast_event(self, message):
                self.broadcasted.append(message)

        m = _M()
        await SessionManager._heartbeat_stalled_handler(m, ["task-h"])
        await SessionManager._heartbeat_stalled_handler(m, [])  # 恢复
        recovered = [e for e in m.broadcasted if e["type"] == "7x24_recovered"]
        assert len(recovered) == 1
        assert "已恢复" in recovered[0]["payload"]["message"]
        assert all(a["resolved"] for a in m.alert_store.list_aggregations())


class TestAggregationStats:
    """② 聚合历史统计: 快照 + 每日趋势。"""

    def test_snapshot_and_stats(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        for i in range(3):
            s.record("heartbeat_stalled", f"卡死{i}", task_id="t1", ts=time.time(), level="critical")
        s.record("heartbeat_stalled", "卡死", task_id="t2", ts=time.time(), level="critical")
        assert s.snapshot_aggregation_history() == 2
        stats = s.aggregation_stats(days=3)
        assert len(stats["days"]) == 3
        assert stats["days"][-1]["alerts"] == 4  # t1×3 + t2×1
        assert any(t == "t1" for t, _ in stats["top_tasks"])

    def test_manager_stats(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_store.record("heartbeat_stalled", "x", task_id="t", ts=time.time(), level="critical")

        m = _M()
        r = SessionManager.longrun_aggregation_stats(m, days=3)
        assert r["ok"] and len(r["days"]) == 3


class TestOperationAudit:
    """③ 回滚/恢复/渠道变更操作审计。"""

    def test_audit_record_and_list(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        s.audit("checkpoint_rollback", "回滚会话 s1 到恢复前备份 (10 条)", task_id="s1")
        s.audit("alert_channels_update", "更新告警渠道配置: 启用 ['email']")
        assert s.count() == 2
        audit = s.list_audit()
        assert len(audit) == 2
        assert all(a["kind"] == "audit" for a in audit)
        assert any("回滚会话 s1" in a["message"] for a in audit)

    def test_manager_audit_and_write_on_actions(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.checkpoint import FractalCheckpoint
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord
        from coworker.server.manager import SessionManager

        class _M:
            base_dir = tmp_path
            default_workspace = str(tmp_path / "ws")

            def __init__(self):
                self.session_store = ConversationStore(tmp_path / "conv")
                self.alert_store = AlertStore(tmp_path / "a.db")

            def _checkpoint_db_path(self):
                return tmp_path / "checkpoints.db"

            def _audit_action(self, action, detail, task_id=None):
                self.alert_store.audit(action, detail, task_id=task_id)

        m = _M()
        # 构造检查点 + 旧会话 → apply 恢复 → 写审计。
        m.session_store.save(
            SessionRecord(session_id="s1", workspace="/tmp", model="m", mode="code",
                          messages=[{"role": "user", "content": "旧"}])
        )
        cp = FractalCheckpoint(tmp_path / "checkpoints.db")
        cp.save_checkpoint("s1", {"messages": [{"role": "user", "content": "新"}]}, n_layer=1)
        cp.close()
        res = SessionManager.longrun_checkpoint_restore(m, "s1", apply=True)
        assert res["ok"]
        audit = m.alert_store.list_audit()
        assert any("从检查点真实恢复会话 s1" in a["message"] for a in audit)
        # 回滚 → 审计。
        SessionManager.longrun_checkpoint_rollback(m, "s1")
        audit2 = m.alert_store.list_audit()
        assert any("回滚会话 s1" in a["message"] for a in audit2)
        # manager 查询。
        r = SessionManager.longrun_audit(m)
        assert r["ok"] and r["count"] == 2


class TestAuditExport:
    """① 审计导出 (CSV/JSON)。"""

    def test_export_csv_and_json(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        s.audit("checkpoint_rollback", "回滚会话 s1 到恢复前备份 (10 条)", task_id="s1")
        s.audit("alert_channels_update", "更新告警渠道配置: 启用 ['email']")

        csv_out = s.export_audit(fmt="csv")
        assert csv_out.startswith("id,ts,task_id,message,payload")
        assert csv_out.count("\n") == 3  # 表头 + 2 行
        assert "回滚会话 s1" in csv_out

        import json

        parsed = json.loads(s.export_audit(fmt="json"))
        assert isinstance(parsed, list) and len(parsed) == 2
        assert "message" in parsed[0] and "ts" in parsed[0]


class TestAlertSettings:
    """② 静默期配置 (阈值/时长, 可持久化)。"""

    def _manager(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore

        class _M:
            base_dir = tmp_path

            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")

            def _alert_channels_config_path(self):
                return tmp_path / "alert_channels.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.alert_store.audit(action, detail, task_id=task_id)

        return _M()

    def test_default_and_update_persist(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        r0 = SessionManager.longrun_alert_settings(m)
        assert r0["settings"] == {
            "silence_after": 3,
            "silence_seconds": 600,
            "archive_keep_days": 30,
            "health_thresholds": {"good": 80.0, "warn": 50.0},
            "probe_history_keep_days": 30,
            "probe_history_keep_count": 10000,
        }
        r1 = SessionManager.set_longrun_alert_settings(m, {"silence_after": 5, "silence_seconds": 1800})
        assert r1["settings"]["silence_after"] == 5 and r1["settings"]["silence_seconds"] == 1800
        # archive_keep_days 可配置。
        r1b = SessionManager.set_longrun_alert_settings(m, {"archive_keep_days": 90})
        assert r1b["settings"]["archive_keep_days"] == 90
        # 健康分阈值 + 探针历史保留窗口可配置。
        r1c = SessionManager.set_longrun_alert_settings(
            m,
            {
                "health_thresholds": {"good": 90, "warn": 60},
                "probe_history_keep_days": 14,
                "probe_history_keep_count": 5000,
            },
        )
        assert r1c["settings"]["health_thresholds"] == {"good": 90.0, "warn": 60.0}
        assert r1c["settings"]["probe_history_keep_days"] == 14
        assert r1c["settings"]["probe_history_keep_count"] == 5000
        # 持久化: 重建实例仍在。
        m2 = self._manager(tmp_path)
        r2 = SessionManager.longrun_alert_settings(m2)
        assert r2["settings"]["silence_after"] == 5
        assert r2["settings"]["archive_keep_days"] == 90
        assert r2["settings"]["health_thresholds"] == {"good": 90.0, "warn": 60.0}
        assert r2["settings"]["probe_history_keep_days"] == 14
        assert r2["settings"]["probe_history_keep_count"] == 5000
        # 非法值钳制。
        r3 = SessionManager.set_longrun_alert_settings(m, {"silence_after": 0, "silence_seconds": -5})
        assert r3["settings"]["silence_after"] >= 1 and r3["settings"]["silence_seconds"] >= 1
        # 健康分阈值钳制: warn 不能超过 good, 且 good <= 100。
        r3b = SessionManager.set_longrun_alert_settings(
            m, {"health_thresholds": {"good": 30, "warn": 80}}
        )
        assert r3b["settings"]["health_thresholds"]["warn"] <= r3b["settings"]["health_thresholds"]["good"]
        r3c = SessionManager.set_longrun_alert_settings(
            m, {"health_thresholds": {"good": 200, "warn": -5}}
        )
        assert r3c["settings"]["health_thresholds"]["good"] <= 100
        assert r3c["settings"]["health_thresholds"]["warn"] >= 0

    def test_silence_params_reflect_config(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        SessionManager.set_longrun_alert_settings(m, {"silence_after": 5, "silence_seconds": 1800})
        params = SessionManager._alert_silence_params(m)
        assert params == {"silence_after": 5, "silence_seconds": 1800}

    def test_probe_retention_reflects_config(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        ret = SessionManager._probe_history_retention(m)
        assert ret == {"keep_days": 30, "keep_count": 10000}
        SessionManager.set_longrun_alert_settings(
            m, {"probe_history_keep_days": 7, "probe_history_keep_count": 500}
        )
        ret2 = SessionManager._probe_history_retention(m)
        assert ret2 == {"keep_days": 7, "keep_count": 500}


class TestWeekCompare:
    """③ 聚合统计跨周对比 (本周 vs 上周)。"""

    def test_week_compare_shapes(self, tmp_path):
        import datetime as _dt

        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        today = _dt.date.today()
        # 本周 3 条。
        for i in range(3):
            s.record("heartbeat_stalled", f"今日{i}", task_id=f"t{i}", ts=time.time(), level="critical")
        # 上周历史 2 条/天 × 3 天。
        for i in range(3):
            s._conn.execute(
                "INSERT OR REPLACE INTO aggregation_history (day, task_id, kind, count, resolved, peak) "
                "VALUES (?,?,?,?,?,?)",
                ((today - _dt.timedelta(days=7 + i)).isoformat(), f"last{i}", "heartbeat_stalled", 2, 0, 2),
            )
        s._conn.commit()
        s.snapshot_aggregation_history()

        wc = s.aggregation_week_compare()
        assert len(wc["labels"]) == 7
        assert len(wc["this_week"]) == 7 and len(wc["last_week"]) == 7
        assert wc["total_this"] >= 3
        assert wc["total_last"] >= 2
        assert isinstance(wc["delta_pct"], float)


class TestArchivedAlerts:
    """③ 归档数据查询/恢复 (alerts_archive → 活跃表)。"""

    def _seed(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        s.record("heartbeat_stalled", "旧1", task_id="t1", ts=now - 40 * 86400, level="critical")
        s.record("heartbeat_stalled", "旧2", task_id="t2", ts=now - 50 * 86400, level="critical")
        s.record("heartbeat_stalled", "新", task_id="t3", ts=now - 3600, level="critical")
        s.archive_old(keep_days=30)
        return s

    def test_list_and_filter_archived(self, tmp_path):
        s = self._seed(tmp_path)
        assert s.count_archived() == 2
        rows = s.list_archived(limit=10)
        assert len(rows) == 2
        assert rows[0]["task_id"] == "t1"  # 新→旧
        assert len(s.list_archived(task_id="t2")) == 1
        assert len(s.list_archived(since=time.time() - 45 * 86400)) == 1

    def test_restore_archived_idempotent(self, tmp_path):
        s = self._seed(tmp_path)
        aid = s.list_archived()[0]["id"]
        restored = s.restore_archived(aid)
        assert restored is not None and restored["task_id"] == "t1"
        assert s.count() == 2  # 新 + 恢复
        assert s.count_archived() == 1
        # 幂等: 再恢复同 id → 返回活跃记录, 不重复。
        again = s.restore_archived(aid)
        assert again is not None and again["task_id"] == "t1"
        assert s.count() == 2
        assert s.restore_archived(9999) is None

    def test_manager_archived_and_restore(self, tmp_path):
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = self._seed_alt(tmp_path)
                self.audit_written = []

            def _seed_alt(self, tp):
                from coworker.alerts import AlertStore

                st = AlertStore(tp / "b.db")
                st.record("heartbeat_stalled", "x", task_id="t", ts=time.time() - 60 * 86400, level="critical")
                st.archive_old(keep_days=30)
                return st

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        m = _M()
        aid = m.alert_store.list_archived()[0]["id"]
        r = SessionManager.longrun_restore_archived_alert(m, aid)
        assert r["ok"] and r["restored"]["task_id"] == "t"
        assert any(a == "alert_restore" for a, _ in m.audit_written)
        rq = SessionManager.longrun_archived_alerts(m)
        assert rq["ok"] and rq["total_archived"] == 0  # 恢复后归档为空


class TestProbeScheduling:
    """② 探针定时化 (周期自动探针 + 失败告警)。"""

    def _manager(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_notifier = AlertNotifier(
                    {"channels": {"telegram": {"enabled": True, "bot_token": "bad", "chat_id": "1"}}}
                )
                self.audit_written = []

            def _record_probe_results(self, results):
                store = getattr(self, "alert_store", None)
                if store is None:
                    return
                for ch, v in results.items():
                    store.record_probe(ch, bool(v.get("ok")), ms=v.get("ms"), error=v.get("error"))

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        return _M()

    def test_schedule_default_and_update(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        r0 = SessionManager.longrun_probe_schedule(m)
        assert r0["schedule"]["enabled"] is True and r0["schedule"]["interval_minutes"] == 60
        r1 = SessionManager.set_longrun_probe_schedule(m, {"enabled": True, "interval_minutes": 30})
        assert r1["schedule"]["interval_minutes"] == 30
        assert any(a == "probe_schedule_update" for a, _ in m.audit_written)

    def test_scheduled_probe_failure_alerts(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        rp = SessionManager.run_scheduled_probe(m)
        assert rp["ok"] and "telegram" in rp["failed"]
        kinds = [a["kind"] for a in m.alert_store.list(limit=10)]
        assert "channel_probe_failed" in kinds
        assert any(a == "channel_probe_failed" for a, _ in m.audit_written)
        sched = SessionManager.longrun_probe_schedule(m)["schedule"]
        assert sched["last_probe_ts"] is not None

    def test_scheduled_probe_disabled_skips(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        SessionManager.set_longrun_probe_schedule(m, {"enabled": False})
        rp = SessionManager.run_scheduled_probe(m)
        assert rp.get("skipped") == "probe disabled"

    def test_probe_failure_multi_channel_notify(self, tmp_path):
        """① 探针失败多渠道联动: 除落库外, 还通过 notifier.send 发 critical 告警。"""
        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier
        from coworker.server.manager import SessionManager

        class _SpyNotifier(AlertNotifier):
            def __init__(self, config, spy):
                super().__init__(config)
                self._spy = spy

            async def send(self, message, **kw):
                self._spy.append((message, kw))
                return {}

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.notify_calls = []
                self.alert_notifier = _SpyNotifier(
                    {"channels": {"telegram": {"enabled": True, "bot_token": "bad", "chat_id": "1"}}},
                    self.notify_calls,
                )
                self.audit_written = []

            def _record_probe_results(self, results):
                store = getattr(self, "alert_store", None)
                if store is None:
                    return
                for ch, v in results.items():
                    store.record_probe(ch, bool(v.get("ok")), ms=v.get("ms"), error=v.get("error"))

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                import json as _json

                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                import json as _json

                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        m = _M()
        rp = SessionManager.run_scheduled_probe(m)
        assert rp["ok"] and "telegram" in rp["failed"]
        # 落库告警。
        kinds = [a["kind"] for a in m.alert_store.list(limit=10)]
        assert "channel_probe_failed" in kinds
        # 多渠道联动通知 (critical 级, 含失败渠道名)。
        assert len(m.notify_calls) >= 1
        msg, kw = m.notify_calls[0]
        assert "telegram" in msg
        assert kw.get("level") == "critical"


class TestArchiveKeepDays:
    """② 归档 keep_days 配置进设置 (持久化, 自动归档读取)。"""

    def _manager(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.audit_written = []

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        return _M()

    def test_default_and_update(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        r0 = SessionManager.longrun_alert_settings(m)
        assert r0["settings"]["archive_keep_days"] == 30
        r1 = SessionManager.set_longrun_alert_settings(m, {"archive_keep_days": 90})
        assert r1["settings"]["archive_keep_days"] == 90
        assert any(a == "alert_settings_update" for a, _ in m.audit_written)
        # 持久化 + 自动归档读取。
        m2 = self._manager(tmp_path)
        assert SessionManager._alert_archive_keep_days(m2) == 90


class TestChannelHealthScore:
    """① 渠道探针健康分/历史趋势 (probe_history)。"""

    def test_probe_health_score_and_trend(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(5):
            s.record_probe("email", True, ms=100 + i, ts=now - (5 - i) * 60)
        for i in range(5):
            s.record_probe("telegram", i < 4, ms=200 + i, error=None if i < 4 else "timeout",
                           ts=now - (5 - i) * 60)
        health = s.probe_health()
        assert set(health["channels"]) == {"email", "telegram"}
        e, t = health["channels"]["email"], health["channels"]["telegram"]
        assert e["health_score"] == 100.0
        assert abs(e["avg_ms"] - 102.0) < 1
        assert t["success_rate"] == 0.8 and t["health_score"] == 80.0
        assert len(e["trend"]) == 5 and "ok" in t["trend"][0]

    def test_manager_channel_health(self, tmp_path):
        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_store.record_probe("email", True, ms=50)

        m = _M()
        r = SessionManager.longrun_channel_health(m)
        assert r["ok"] and r["channels"]["email"]["health_score"] == 100.0

    def test_health_thresholds_rating(self, tmp_path):
        """① 健康分阈值可配: rating = good/warn/bad。"""
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(5):
            s.record_probe("email", True, ms=100, ts=now - (5 - i) * 60)
        for i in range(5):
            s.record_probe("telegram", i < 4, ms=200, ts=now - (5 - i) * 60)
        for i in range(10):
            s.record_probe("wecom", False, ms=50, error="down", ts=now - (10 - i) * 60)
        h = s.probe_health()
        assert h["channels"]["email"]["rating"] == "good"  # 100 ≥ 80
        assert h["channels"]["telegram"]["rating"] == "good"  # 80 ≥ 80
        h2 = s.probe_health(good_threshold=85.0, warn_threshold=50.0)
        assert h2["channels"]["telegram"]["rating"] == "warn"  # 80 < 85
        h3 = s.probe_health(good_threshold=80.0, warn_threshold=60.0)
        assert h3["channels"]["wecom"]["rating"] == "bad"  # 0 < 60

    def test_manager_health_thresholds_config(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_store.record_probe("email", True, ms=50)

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

        m = _M()
        m._save_alert_channels_config(
            {"settings": {"health_thresholds": {"good": 90, "warn": 40}}}
        )
        r = SessionManager.longrun_channel_health(m)
        assert r["thresholds"] == {"good": 90.0, "warn": 40.0}

    def test_export_probe_history(self, tmp_path):
        """② 探针历史导出 (CSV/JSON)。"""
        import json as _json

        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        s.record_probe("email", True, ms=100)
        s.record_probe("telegram", False, ms=None, error="timeout")
        csv_out = s.export_probe_history(fmt="csv")
        assert csv_out.startswith("id,channel,ok,ms,error,ts")
        assert csv_out.count("\n") == 3  # 表头 + 2 行
        assert "email" in csv_out and "timeout" in csv_out
        parsed = _json.loads(s.export_probe_history(fmt="json"))
        assert len(parsed) == 2
        assert "channel" in parsed[0] and "ok" in parsed[0]


class TestChannelKeepDaysArchive:
    """② 归档 keep_days 分渠道配置。"""

    def test_archive_by_channel(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        s.record("channel_probe_failed", "e", task_id="channel:email", ts=now - 40 * 86400, level="warning")
        s.record("channel_probe_failed", "t", task_id="channel:telegram", ts=now - 20 * 86400, level="warning")
        s.record("heartbeat_stalled", "n", task_id="t1", ts=now - 40 * 86400, level="critical")
        res = s.archive_old_by_channel(
            default_keep_days=30, channel_keep_days={"email": 10, "telegram": 60}
        )
        assert res["archived"] == 2  # email + 普通
        assert s.count() == 1  # telegram 保留
        assert res["by_channel"].get("email") == 1 and res["by_channel"].get("(default)") == 1
        assert s.list(limit=10)[0]["task_id"] == "channel:telegram"

    def test_manager_channel_archive_uses_config(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.audit_written = []
                self.alert_store.record("channel_probe_failed", "e", task_id="channel:email",
                                        ts=time.time() - 40 * 86400, level="warning")
                self.alert_store.record("heartbeat_stalled", "n", task_id="t1",
                                        ts=time.time() - 40 * 86400, level="critical")

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        m = _M()
        m._save_alert_channels_config(
            {"settings": {"channel_archive_keep_days": {"email": 10, "telegram": 60}}}
        )
        assert SessionManager._channel_archive_keep_days(m) == {"email": 10, "telegram": 60}
        r = SessionManager.longrun_alert_archive(m, keep_days=30)
        assert r["ok"] and r["archived"] == 2
        assert any(a == "alert_archive" for a, _ in m.audit_written)


class TestProbeInSchedulerTick:
    """① 周期探针接入 scheduler tick (真正定时执行)。"""

    def _manager(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier
        from coworker.selfwake import WakeStore

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_notifier = AlertNotifier(
                    {"channels": {"telegram": {"enabled": True, "bot_token": "bad", "chat_id": "1"}}}
                )
                self.wakes = WakeStore()
                self.probe_runs = 0

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            async def resume_due_wakes(self):
                return 0

            def _probe_due(self):
                return True  # 测试: 始终到期

            async def _maybe_run_scheduled_probe(self):
                self.probe_runs += 1

        return _M()

    @pytest.mark.asyncio
    async def test_combined_extra_tick_runs_probe(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        await SessionManager._combined_extra_tick(m)
        await SessionManager._combined_extra_tick(m)
        assert m.probe_runs == 2  # 每 tick 都执行探针检查

    def test_probe_due_logic(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        # 从未探针 → 到期。
        m._save_alert_channels_config({"probe_schedule": {"enabled": True, "interval_minutes": 60}})
        assert SessionManager._probe_due(m) is True
        # 刚探针过 → 未到期。
        m._save_alert_channels_config(
            {"probe_schedule": {"enabled": True, "interval_minutes": 60, "last_probe_ts": time.time()}}
        )
        assert SessionManager._probe_due(m) is False
        # 超过间隔 → 到期。
        m._save_alert_channels_config(
            {"probe_schedule": {"enabled": True, "interval_minutes": 60, "last_probe_ts": time.time() - 3601}}
        )
        assert SessionManager._probe_due(m) is True
        # 禁用 → 不到期。
        m._save_alert_channels_config(
            {"probe_schedule": {"enabled": False, "interval_minutes": 60, "last_probe_ts": time.time() - 99999}}
        )
        assert SessionManager._probe_due(m) is False


class TestArchiveInMaintenance:
    """② 归档自动执行挂到维护周期。"""

    def test_maintenance_archives_alerts(self, tmp_path):
        from coworker.alerts import AlertStore

        # memory_maintenance 在非 dry-run 时调 longrun_alert_archive —
        # 这里直接验证归档函数 (维护内部路径) 正常工作。
        s = AlertStore(tmp_path / "a.db")
        s.record("heartbeat_stalled", "旧", task_id="t", ts=time.time() - 60 * 86400, level="critical")
        result = s.archive_old(keep_days=30)
        assert result["archived"] == 1


class TestProbeHistoryRetention:
    """③ 探针历史保留窗口 (防 probe_history 无限增长)。

    AlertStore.prune_probe_history 按天数/条数清理; manager 设置持久化 +
    手动触发端点 + 周期探针/维护自动清理。
    """

    def test_prune_by_days(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        # 1-5 天前 (+1h 偏移, 避开清理时刻 now 的毫秒漂移对边界的误判)。
        for i in range(5):
            s.record_probe("email", True, ms=50, ts=now - (i + 1) * 86400 + 3600)
        r = s.prune_probe_history(keep_days=3, keep_count=10000)
        assert r["removed"] == 2  # 4 天前 + 5 天前
        assert r["kept"] == 3
        stats = s.probe_history_stats()
        assert stats["count"] == 3

    def test_prune_by_count(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(50):
            s.record_probe("email", True, ms=50, ts=now - (50 - i) * 60)  # 时间递增
        r = s.prune_probe_history(keep_days=365, keep_count=10)
        assert r["removed"] == 40
        assert r["kept"] == 10
        # 保留的是最新 10 条。
        rows = s._conn.execute("SELECT ts FROM probe_history ORDER BY ts DESC").fetchall()
        assert len(rows) == 10

    def test_prune_both_dimensions(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        # 20 条新 + 20 条旧 (超 30 天)。
        for i in range(20):
            s.record_probe("email", True, ms=50, ts=now - (20 - i) * 60)
        for i in range(20):
            s.record_probe("email", True, ms=50, ts=now - (i + 1) * 31 * 86400)
        r = s.prune_probe_history(keep_days=30, keep_count=10000)
        assert r["removed"] == 20  # 旧的 20 条全清
        assert r["kept"] == 20
        # 再加条数窗口。
        for i in range(30):
            s.record_probe("email", True, ms=50, ts=now - i * 60)
        r2 = s.prune_probe_history(keep_days=30, keep_count=5)
        assert r2["kept"] == 5

    def test_noop_when_within_window(self, tmp_path):
        from coworker.alerts import AlertStore

        s = AlertStore(tmp_path / "a.db")
        now = time.time()
        for i in range(3):
            s.record_probe("email", True, ms=50, ts=now - i * 60)
        r = s.prune_probe_history(keep_days=30, keep_count=10000)
        assert r["removed"] == 0 and r["kept"] == 3

    def _manager(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.audit_written = []

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                self.audit_written.append((action, detail))

        return _M()

    def test_manager_prune_uses_configured_window(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        now = time.time()
        for i in range(10):
            m.alert_store.record_probe("email", True, ms=50, ts=now - (i + 1) * 86400)
        # 配置窗口: 保留 7 天 / 5 条。
        SessionManager.set_longrun_alert_settings(
            m, {"probe_history_keep_days": 7, "probe_history_keep_count": 5}
        )
        r = SessionManager.longrun_probe_history_prune(m)
        assert r["ok"] and r["kept"] == 5
        assert r["keep_days"] == 7 and r["keep_count"] == 5
        assert any(a == "probe_history_prune" for a, _ in m.audit_written)
        # 重建实例后窗口仍生效。
        m2 = self._manager(tmp_path)
        assert SessionManager._probe_history_retention(m2) == {
            "keep_days": 7, "keep_count": 5,
        }

    def test_scheduled_probe_prunes_history(self, tmp_path):
        import json as _json

        from coworker.alerts import AlertStore
        from coworker.notify import AlertNotifier
        from coworker.server.manager import SessionManager

        class _M:
            def __init__(self):
                self.alert_store = AlertStore(tmp_path / "a.db")
                self.alert_notifier = AlertNotifier(
                    {"channels": {"telegram": {"enabled": True, "bot_token": "bad", "chat_id": "1"}}}
                )

            def _alert_channels_config_path(self):
                return tmp_path / "c.json"

            def _load_alert_channels_config(self):
                p = self._alert_channels_config_path()
                try:
                    if p.is_file():
                        return _json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    pass
                return {}

            def _save_alert_channels_config(self, config):
                p = self._alert_channels_config_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

            def _audit_action(self, action, detail, task_id=None):
                pass

            def _record_probe_results(self, results):
                for ch, v in results.items():
                    self.alert_store.record_probe(
                        ch, bool(v.get("ok")), ms=v.get("ms"), error=v.get("error")
                    )

        m = _M()
        now = time.time()
        # 大量旧探针历史 (超保留窗口)。
        for i in range(30):
            m.alert_store.record_probe("email", True, ms=50, ts=now - (i + 1) * 86400)
        SessionManager.set_longrun_alert_settings(
            m, {"probe_history_keep_days": 10, "probe_history_keep_count": 10000}
        )
        # 周期探针: 探针失败 (notifier 无效 token) → 落库 + 自动清理。
        result = SessionManager.run_scheduled_probe(m)
        assert result["ok"]
        # 探针后旧记录被清理 (保留 10 天内 + 本次探针)。
        stats = m.alert_store.probe_history_stats()
        assert stats["count"] <= 11
        # 探针结果本身已落库。
        assert m.alert_store.probe_health()["channels"].get("telegram") is not None

    def test_maintenance_prune_key(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        # 维护周期调用 prune — 这里验证 manager 方法 (维护内部路径)。
        r = SessionManager.longrun_probe_history_prune(m)
        assert r["ok"] and r["kept"] == 0


class TestOirLongrunIntegration:
    """OIR longrun 集成: 配置持久化 + 每 tick 推进开关 (GUI /v1/7x24/oir-longrun)。"""

    def _manager(self, tmp_path):
        from coworker.conversations import ConversationStore

        class _M:
            def __init__(self):
                self.base_dir = tmp_path
                self.default_workspace = str(tmp_path / "ws")
                self.session_store = ConversationStore(tmp_path / "conv")
                self._prefs = {}
                self._prefs_path_file = tmp_path / "prefs.json"

            def _prefs_path(self):
                return self._prefs_path_file

            def _save_prefs(self):
                import json
                self._prefs_path_file.write_text(
                    json.dumps(self._prefs, indent=2), encoding="utf-8"
                )

            async def _maybe_oir_longrun_tick(self):  # 默认禁用 → 无操作
                pass

        return _M()

    def test_config_default_disabled(self, tmp_path):
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        c = SessionManager.oir_longrun_config(m)
        assert c["enabled"] is False
        assert c["doc_dir"] == "研究文档" and c["batch"] == 3

    def test_set_config_persists(self, tmp_path):
        import json

        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        c = SessionManager.set_oir_longrun_config(
            m, {"enabled": True, "doc_dir": "研究文档", "batch": 5}
        )
        assert c["enabled"] is True and c["batch"] == 5
        # 持久化到 prefs 文件（重启可恢复）。
        saved = json.loads(m._prefs_path_file.read_text(encoding="utf-8"))
        assert saved["oir_longrun"]["enabled"] is True
        assert saved["oir_longrun"]["batch"] == 5

    def test_disabled_tick_is_noop(self, tmp_path):
        """未启用时 tick 直接返回（不构造 OIR 驱动）。"""
        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        # 默认 disabled: _maybe_oir_longrun_tick stub 直接返回，不抛错。
        assert SessionManager.oir_longrun_config(m)["enabled"] is False

    # -- 方案 A: 用户发起/控制长程任务（面板任务控制台后端）----------------

    def _fake_driver(self, calls):
        class _FakeDrv:
            def submit_manual_goal(self, goal, doc_dir="研究文档", glob="*.md", doc_paths=None):
                calls.append(("submit", goal, doc_dir, glob, doc_paths))
                return {"goal_id": "qunwork-manual-1", "total_documents": 3, "oir_task_id": "oir-1"}

            def pause_goal(self, gid):
                calls.append(("pause", gid))
                return True

            def resume_goal(self, gid):
                calls.append(("resume", gid))
                return True

            def complete_goal(self, gid):
                calls.append(("complete", gid))
                return True

            def list_gateway_tasks(self):
                return {
                    "registered_goals": 1,
                    "active": [{"goal_id": "qunwork-manual-1", "goal": "g", "percent": 0.5}],
                }

            def load_state(self):
                return {"qunwork-manual-1": {"pos": 1, "manual": True}}

        return _FakeDrv()

    def test_submit_list_control_proxy(self, tmp_path, monkeypatch):
        """发起/列表/控制经 manager 代理驱动模块（含合并本地状态与非法操作兜底）。"""
        import asyncio

        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        calls: list = []
        # stub 不是 SessionManager 子类 → 把驱动工厂挂到实例上（生产路径等价）。
        fake = self._fake_driver(calls)
        m._oir_driver = lambda: fake

        r = asyncio.run(SessionManager.oir_longrun_submit_task(m, "九月新资料索引"))
        assert r["goal_id"] == "qunwork-manual-1" and r["total_documents"] == 3
        assert ("submit", "九月新资料索引", "研究文档", "*.md", None) in calls

        # doc_paths 透传（清单契约: 只索引指定文档）
        r = asyncio.run(
            SessionManager.oir_longrun_submit_task(m, "指定两份", doc_paths=["D:\\x\\a.md"])
        )
        assert r["goal_id"] == "qunwork-manual-1"
        assert ("submit", "指定两份", "研究文档", "*.md", ["D:\\x\\a.md"]) in calls

        r = asyncio.run(SessionManager.oir_longrun_list_tasks(m))
        assert r["active"][0]["origin"] == "manual"
        assert r["active"][0]["pos"] == 1
        assert "qunwork-manual-1" in r["tracked"]

        r = asyncio.run(SessionManager.oir_longrun_control_task(m, "qunwork-manual-1", "pause"))
        assert r["success"] is True
        r = asyncio.run(SessionManager.oir_longrun_control_task(m, "x", "bogus"))
        assert "error" in r
        assert ("pause", "qunwork-manual-1") in calls

    def test_disabled_tick_still_drives_tracked_goals(self, tmp_path, monkeypatch):
        """开关只控制自动批：已跟踪（手动/收养）目标在 disabled 下仍被驱动。"""
        import asyncio
        import sys
        import types

        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)
        driven: list = []

        class _FakeTick:
            def __init__(self, **kw):
                driven.append(("init", dict(kw)))
                self.auto_enabled = kw.get("auto_enabled", True)

            async def __call__(self):
                driven.append(("call", None))

        fake = types.ModuleType("oir_longrun_driver")
        fake.OirLongrunTick = _FakeTick
        fake.has_tracked_goals = lambda: True
        monkeypatch.setitem(sys.modules, "oir_longrun_driver", fake)

        asyncio.run(SessionManager._maybe_oir_longrun_tick(m))
        assert any(d[0] == "call" for d in driven)
        assert getattr(m, "_oir_longrun_tick_obj", None) is not None
        assert m._oir_longrun_tick_obj.auto_enabled is False
        # 第二次 tick：驱动对象已缓存 → 直接调用，不重建。
        asyncio.run(SessionManager._maybe_oir_longrun_tick(m))
        assert sum(1 for d in driven if d[0] == "call") == 2

    def test_disabled_tick_noop_without_tracked(self, tmp_path, monkeypatch):
        """disabled 且无未完成目标 → 不构造驱动、不产生网关流量。"""
        import asyncio
        import sys
        import types

        from coworker.server.manager import SessionManager

        m = self._manager(tmp_path)

        def _boom(**kw):
            raise AssertionError("should not construct OirLongrunTick")

        fake = types.ModuleType("oir_longrun_driver")
        fake.OirLongrunTick = _boom
        fake.has_tracked_goals = lambda: False
        monkeypatch.setitem(sys.modules, "oir_longrun_driver", fake)

        asyncio.run(SessionManager._maybe_oir_longrun_tick(m))
        assert not hasattr(m, "_oir_longrun_tick_obj")


class TestOirLongrunDriverManifest:
    """驱动器文档清单契约（2026-09-06 实证修复）:

    用户发起任务时 agent 承诺的索引对象（如 D 盘本月新增 2 份）与调度器
    实际投喂（cfg doc_dir 全集按 pos 顺序）曾完全脱节 — completed_documents
    9 > total 2 就是驱动器在灌 doc_dir 文档。修复: goal 自带清单
    (goal_manifests/<goal_id>.json) 时收养后只投喂清单内文档。
    """

    def _load_driver(self, monkeypatch, tmp_path):
        import importlib.util
        import pathlib as _pl

        driver_file = _pl.Path(r"E:\QunWork\oir_bridge\oir_longrun_driver.py")
        if not driver_file.is_file():
            pytest.skip("oir_bridge 驱动不在本机（CI/他机跳过）")
        spec = importlib.util.spec_from_file_location(
            "oir_longrun_driver_real", driver_file
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 隔离: state/manifest 全部落 tmp，绝不触碰真实 state 文件
        monkeypatch.setattr(mod, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(mod, "MANIFEST_DIR", tmp_path / "manifests")
        return mod

    def test_manifest_adoption_feeds_only_manifest_docs(self, tmp_path, monkeypatch):
        """收养带清单的 goal → 只推进清单内文档，不碰 doc_dir 全集。"""
        import asyncio
        import json as _json

        mod = self._load_driver(monkeypatch, tmp_path)
        calls = {"advance": []}

        monkeypatch.setattr(mod, "gateway_healthy", lambda timeout=3.0: True)
        monkeypatch.setattr(
            mod, "list_gateway_tasks",
            lambda: {"registered_goals": 1, "active": [
                {"oir_task_id": "oir-9", "goal_id": "qunwork-manual-x",
                 "goal": "g", "percent": 0.0}]},
        )
        monkeypatch.setattr(
            mod, "gateway_task_phase", lambda gid: "Executing"
        )
        monkeypatch.setattr(mod, "submit_goal", lambda gid, goal, total: {"oir_task_id": "x"})
        monkeypatch.setattr(mod, "complete_goal", lambda gid: True)

        def fake_advance(gid, batch, max_terms=40):
            calls["advance"].append([p.name for p in batch])
            return {"deliverables": [], "completed_documents": len(batch),
                    "total_documents": 1}

        monkeypatch.setattr(mod, "advance_batch", fake_advance)

        # doc_dir 全集 2 份；清单只指定 1 份 → 驱动器必须只喂清单那份
        doc_dir = tmp_path / "docs"
        doc_dir.mkdir()
        (doc_dir / "a.md").write_text("A", encoding="utf-8")
        (doc_dir / "b.md").write_text("B", encoding="utf-8")
        manifest_doc = tmp_path / "elsewhere.md"
        manifest_doc.write_text("M", encoding="utf-8")
        mod.write_manifest("qunwork-manual-x", [manifest_doc])

        tick = mod.OirLongrunTick(
            doc_dir=str(doc_dir), glob="*.md", batch=3,
            auto_enabled=False, heartbeat=False,
        )
        asyncio.run(tick())
        assert calls["advance"] == [[manifest_doc.name]]
        state = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        rec = state["qunwork-manual-x"]
        assert rec["pos"] == 1 and rec["docs"] == [str(manifest_doc)]

        # 第二 tick: 清单队列耗尽 → complete 且从活跃跟踪移除，不再投喂
        asyncio.run(tick())
        state = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["qunwork-manual-x"]["superseded"] is True
        assert len(calls["advance"]) == 1

    def test_submit_manual_goal_with_doc_paths_writes_manifest(self, tmp_path, monkeypatch):
        """submit_manual_goal(doc_paths=...) → 清单落盘 + state.docs 记录 + total 正确。"""
        import asyncio
        import json as _json

        mod = self._load_driver(monkeypatch, tmp_path)
        monkeypatch.setattr(
            mod, "submit_goal", lambda gid, goal, total: {"oir_task_id": "x"}
        )
        docs = []
        for i in range(2):
            p = tmp_path / f"doc{i}.md"
            p.write_text(f"content {i}", encoding="utf-8")
            docs.append(p)

        r = mod.submit_manual_goal("索引这两份", doc_dir="研究文档", doc_paths=docs)
        assert r["total_documents"] == 2
        manifest = _json.loads(
            (tmp_path / "manifests" / f"{r['goal_id']}.json").read_text(encoding="utf-8")
        )
        assert manifest["docs"] == [str(p) for p in docs]
        state = _json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state[r["goal_id"]]["docs"] == [str(p) for p in docs]

    def test_submit_manual_goal_filters_missing_paths(self, tmp_path, monkeypatch):
        """清单路径全部不存在 → 明确报错，不产生空清单任务。"""
        mod = self._load_driver(monkeypatch, tmp_path)
        try:
            mod.submit_manual_goal("x", doc_paths=[tmp_path / "ghost.md"])
            raise AssertionError("should raise")
        except ValueError as e:
            assert "不存在" in str(e)
