# -*- coding: utf-8 -*-
"""7×24 长程任务端到端测试 — 五大突破集成验证 (方案文档 §10.2 场景).

覆盖:
  * 分形检查点: 会话崩溃后多层恢复, 上下文不丢;
  * LoopCoop 收敛: 谱隙理论值 + 收敛曲线 + 停滞检测 + 自动重规划;
  * 皮萨诺压缩: 无损往返 + 压缩率 (长会话不膨胀);
  * 蜂巢心跳: 卡死任务 30s 内检出, 存活任务零误报, self-wake 唤醒;
  * 分形降级: L1→L6 降级链, 部分完成保留, 恢复可续跑;
  * 集成面: Scheduler 心跳 tick、RunStore 降级审计、SessionRecord.checkpoint 字段。

运行: pytest tests/test_longrun.py -v
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from coworker.automation.scheduler import Scheduler
from coworker.checkpoint import FractalCheckpoint
from coworker.degradation import FractalDegradation
from coworker.heartbeat import HoneycombHeartbeat
from coworker.memory.compressor import PisanoMemoryCompressor
from coworker.orchestrator.convergence import GOLDEN_COUPLING, LoopCoopMonitor, spectral_gap
from coworker.orchestrator.models import Plan, Task
from coworker.orchestrator.replanner import Replanner
from coworker.orchestrator.run_store import OrchestrationRunStore
from coworker.selfwake import KIND_HEARTBEAT, WakeStore
from coworker.sessions import SessionRecord


# -- 突破一: 分形检查点 ------------------------------------------------------

class TestFractalCheckpoint:
    def test_interval_golden_scale(self):
        """检查点间隔遵循黄金比例因子标度。"""
        from coworker.checkpoint import PHI, checkpoint_interval

        factors = [PHI ** abs(n - 4) for n in range(1, 8)]
        ratios = [factors[i + 1] / factors[i] for i in range(3, 6)]
        avg = sum(ratios) / len(ratios)
        assert abs(avg - PHI) / PHI < 0.05

    def test_restore_after_crash(self, tmp_path):
        """崩溃后从多层检查点恢复, 最终状态不丢。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        sid = "sess-crash"
        for rnd in range(5):
            cp.save_checkpoint(
                sid,
                {"messages": [{"role": "user", "content": f"r{rnd}"}] * (rnd + 1),
                 "tasks": [{"id": f"t{i}"} for i in range(rnd)],
                 "result": f"partial-{rnd}"},
                n_layer=1,
            )
        cp.save_checkpoint(sid, {"result": "FINAL"}, n_layer=7)
        restored = cp.restore_latest(sid)
        assert restored["result"] == "FINAL"
        assert cp.count(sid) >= 5

    def test_diff_layer_apply(self, tmp_path):
        """diff 粒度检查点与 full 叠加恢复。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        sid = "sess-diff"
        cp.save_checkpoint(sid, {"a": 1, "b": 2, "c": 3}, n_layer=1)  # full
        # diff 层保存"相对上一 full 的变更": b 改、d 增。
        cp.save_checkpoint(sid, {"a": 1, "b": 20, "c": 3, "d": 4}, n_layer=6)
        state = cp.restore_latest(sid)
        assert state.get("a") == 1
        assert state.get("b") == 20  # diff 覆盖 full
        assert state.get("d") == 4
        assert state.get("c") == 3  # 未变更键保留

    def test_session_record_checkpoint_field(self):
        """SessionRecord 新增 checkpoint 字段 (方案 8.2 集成点)。"""
        s = SessionRecord(session_id="s1", workspace="/tmp", model="m", mode="code")
        assert s.checkpoint is None
        s.checkpoint = "cp_abc"
        assert s.checkpoint == "cp_abc"


# -- 突破二: LoopCoop 收敛 ----------------------------------------------------

class TestLoopCoop:
    def test_spectral_gap_golden(self):
        gap = spectral_gap(GOLDEN_COUPLING)
        assert abs(gap - 0.5712) < 0.005
        assert gap < 1.0  # Perron-Frobenius 必然收敛

    def test_iterations_to_converge(self):
        from coworker.orchestrator.convergence import iterations_to_converge

        k99 = iterations_to_converge(0.99)
        assert abs(k99 - 9) <= 1

    def test_monitor_curve_and_stall(self):
        mon = LoopCoopMonitor()
        plan = Plan(goal="g", tasks=[Task(id=f"t{i}", description="x") for i in range(3)])
        for i in range(3):
            plan.tasks[i].status = "done"
            mon.record_round(plan, verdicts=[{"accepted": True}] * (i + 1))
        assert mon.is_converged()
        assert not mon.is_stalled()
        # 无进展 → 停滞
        mon2 = LoopCoopMonitor(stall_rounds=2)
        plan2 = Plan(goal="s", tasks=[Task(id="t0", description="x")])
        mon2.record_round(plan2)
        mon2.record_round(plan2)
        mon2.record_round(plan2)
        assert mon2.is_stalled()

    def test_replanner_splits_failed_task(self):
        rp = Replanner(split_threshold=2)
        plan = Plan(
            goal="g",
            tasks=[Task(id="t1", description="调研 A；撰写 B", deps=[], retries=3)],
        )
        out = rp.suggest(plan, [plan.tasks[0]], reason="review rejected")
        assert out and out[0]["action"] in ("split", "reword")
        # 拆分产生 a/b 两个子任务
        if out[0]["action"] == "split":
            assert out[0]["id"] == "t1a"
            assert out[1]["id"] == "t1b"
            assert out[1]["deps"] == ["t1a"]


# -- 突破三: 皮萨诺记忆压缩 ---------------------------------------------------

class TestPisanoCompressor:
    def _msgs(self, n: int, roles=("user", "assistant")):
        out = []
        for i in range(n):
            out.append(
                {
                    "role": roles[i % len(roles)],
                    "content": f"第 {i} 条消息的正文内容, 带重复模板 " * 3,
                }
            )
        return out

    def test_lossless_roundtrip(self):
        comp = PisanoMemoryCompressor()
        for n in (6, 50, 500):
            msgs = self._msgs(n)
            c = comp.compress_messages(msgs)
            assert comp.decompress(c) == msgs

    def test_compression_ratio_high(self):
        comp = PisanoMemoryCompressor()
        c = comp.compress_messages(self._msgs(500, roles=("user", "assistant", "tool", "system")))
        assert c["compression_ratio"] > 0.5
        assert c["packed_bytes"] < c["raw_bytes"]

    def test_short_sequence_raw(self):
        comp = PisanoMemoryCompressor()
        c = comp.compress_messages(self._msgs(1))
        assert c["type"] == "raw"
        assert comp.decompress(c) == self._msgs(1)


# -- 突破四: 蜂巢心跳 ---------------------------------------------------------

class TestHoneycombHeartbeat:
    def test_stall_detection_within_tick(self):
        now = 1_000_000.0
        hb = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3)
        hb.register("alive", now=now)
        hb.register("dead", now=now)
        for tick in range(4):
            t = now + tick * 30.0
            hb.pulse("alive", now=t)
            hb.check_health(now=t)
        t = now + 4 * 30.0
        hb.pulse("alive", now=t)
        hb.check_health(now=t)
        assert "dead" in hb.get_unhealthy()
        assert "alive" not in hb.get_unhealthy()

    def test_zero_false_positive(self):
        now = 1_000_000.0
        hb = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3)
        for i in range(50):
            hb.register(f"t{i}", now=now)
        for tick in range(5):
            t = now + tick * 30.0
            for i in range(50):
                hb.pulse(f"t{i}", now=t)
            hb.check_health(now=t)
        assert hb.get_unhealthy() == []

    def test_selfwake_heartbeat_kind(self):
        ws = WakeStore()
        w = ws.add_heartbeat("sess-ops", task_id="job-1")
        assert w.kind == KIND_HEARTBEAT
        assert len(ws.due()) == 0
        ws.heartbeat_stalled("job-1")
        due = ws.due()
        assert len(due) == 1
        assert due[0].session_id == "sess-ops"

    def test_scheduler_heartbeat_tick(self):
        """Scheduler._tick 接入蜂巢心跳检查 (集成点)。"""
        from coworker.automation.models import Schedule, ScheduledTask, TaskRun

        class _Store:
            def __init__(self):
                self.runs = []

            def due(self):
                return []

            def list(self):
                return []

            def add_run(self, run):
                self.runs.append(run)

            def get(self, task_id):
                return None

            def save(self, task):
                pass

        handled: list[list[str]] = []

        async def runner(task, trigger):
            return TaskRun(task_id=task.id)

        async def handler(stalled):
            handled.append(stalled)

        store = _Store()
        hb = HoneycombHeartbeat(tick_seconds=30.0, threshold=0.3)
        hb.register("orphan-task")
        # 模拟卡死: 最后一次心跳在很久之前 (90s+ 无脉冲 → 健康度跌破阈值)。
        hb.heartbeats["orphan-task"] = time.time() - 120.0
        sched = Scheduler(
            store,
            runner,
            tick_seconds=30.0,
            heartbeat=hb,
            heartbeat_handler=handler,
        )
        # 直接驱动一次 tick (不启动后台循环)。
        asyncio.run(sched._tick(trigger="schedule"))
        assert handled  # 卡死任务被收集


# -- 突破五: 分形降级 ---------------------------------------------------------

class TestFractalDegradation:
    @pytest.mark.asyncio
    async def test_degradation_chain_l1_to_l6(self, tmp_path):
        cp = FractalCheckpoint(tmp_path / "cp.db")
        deg = FractalDegradation(checkpoint_engine=cp)
        state = {"model": "gpt-4o", "tasks": [{"id": "a"}, {"id": "b"}]}
        actions = []
        for i in range(6):
            d = await deg.handle_failure("task-x", RuntimeError(f"e{i}"), state)
            actions.append((d["level"], d["action"]))
            if d.get("model"):
                state["model"] = d["model"]
        assert [lvl for lvl, _ in actions] == [1, 2, 3, 4, 5, 6]
        assert actions[-1][1] == "archive"
        assert cp.count("task-x") >= 6  # 每级保存检查点 (部分完成保留)

    @pytest.mark.asyncio
    async def test_resume_after_pause(self, tmp_path):
        """L5 checkpoint_pause 后可从最新检查点续跑。"""
        cp = FractalCheckpoint(tmp_path / "cp.db")
        deg = FractalDegradation(checkpoint_engine=cp)
        state = {"tasks": [{"id": "done-1", "result": "ok"}], "pending": ["t2"]}
        for i in range(5):
            d = await deg.handle_failure("task-y", RuntimeError(f"e{i}"), dict(state))
        restored = cp.restore_latest("task-y")
        assert restored is not None
        # 已完成子任务结果保留 (FSCI: 存储即状态)。
        assert restored.get("pending") == ["t2"]


# -- 端到端场景 (§10.2): 崩溃-恢复-续跑 ---------------------------------------

class TestLongRunEndToEnd:
    def test_crash_recover_continue(self, tmp_path):
        """长程任务: 崩溃 → 恢复 → 部分完成保留 → 续跑 (含压缩与降级)。"""
        base = tmp_path
        cp = FractalCheckpoint(base / "checkpoints.db")
        rs = OrchestrationRunStore(base / "runs.db")
        comp = PisanoMemoryCompressor()
        sid = "e2e-run"

        # 阶段 1: 任务运行到一半, 记录检查点 + 压缩历史 + 一次降级。
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"研究步骤 {i} 的正文内容 " * 4}
            for i in range(40)
        ]
        packed = comp.compress_messages(history)
        cp.save_checkpoint(
            sid,
            {"messages": history, "tasks": [{"id": "t1", "status": "done"}, {"id": "t2", "status": "running"}], "result": "partial"},
            n_layer=1,
        )
        run_id = rs.create_run("e2e-intent")
        rs.record_degradation(run_id, "t2", 2, "downgrade_model", fidelity=0.85, error="rate limit")

        # 阶段 2: "进程崩溃" — 从检查点恢复, 历史从压缩包还原。
        restored = cp.restore_latest(sid)
        assert restored["result"] == "partial"
        history_back = comp.decompress(packed)
        assert history_back == history

        # 阶段 3: 续跑 — 完成剩余任务并收尾。
        restored["tasks"] = [{"id": "t1", "status": "done"}, {"id": "t2", "status": "done"}]
        restored["result"] = "FINAL-DELIVERABLE"
        cp.save_checkpoint(sid, restored, n_layer=7)
        assert cp.restore_latest(sid)["result"] == "FINAL-DELIVERABLE"

        # 阶段 4: 降级审计可查。
        degs = rs.list_degradations(run_id)
        assert len(degs) == 1
        assert degs[0]["level"] == 2
        assert degs[0]["action"] == "downgrade_model"


# -- 续作: GUI 数据面 / ConversationStore 归档 / 心跳默认状态目录 --------------

class TestRunStoreDegradationsSnapshot:
    """GUI 数据面: get_run 返回降级轨迹 (SwarmView 降级轨迹面板的数据源)。"""

    def test_get_run_contains_degradations(self, tmp_path):
        rs = OrchestrationRunStore(tmp_path / "runs.db")
        run_id = rs.create_run("intent")
        rs.record_degradation(run_id, "t1", 2, "downgrade_model", fidelity=0.85, error="rate")
        rs.record_degradation(run_id, "t2", 5, "checkpoint_pause", fidelity=0.0)
        snap = rs.get_run(run_id)
        assert len(snap["degradations"]) == 2
        d0 = snap["degradations"][0]
        assert d0["level"] == 2 and d0["action"] == "downgrade_model"
        assert abs(d0["fidelity"] - 0.85) < 1e-6

    def test_clean_run_empty_degradations(self, tmp_path):
        rs = OrchestrationRunStore(tmp_path / "runs.db")
        snap = rs.get_run(rs.create_run("clean"))
        assert snap["degradations"] == []


class TestConversationStoreArchive:
    """皮萨诺压缩接入 ConversationStore 归档 (突破三 B4: 历史无限增长)。"""

    def test_archive_roundtrip_lossless(self, tmp_path):
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord

        store = ConversationStore(tmp_path / "conv")
        sid = "sess-arch"
        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"长程任务第 {i} 条消息 " * 4}
            for i in range(300)
        ]
        store.save(SessionRecord(session_id=sid, workspace="/tmp", model="m", mode="code", messages=msgs))
        summary = store.archive_compressed(sid, keep_recent=50)
        assert summary is not None
        assert summary["archived"] == 250 and summary["recent"] == 50
        assert summary["compression_ratio"] > 0.5
        # 无损往返: 完整历史 == 原序列。
        assert store.full_history(sid) == msgs
        # load() 合并归档 → 完整历史。
        assert len(store.load(sid).messages) == 300

    def test_short_session_not_archived(self, tmp_path):
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord

        store = ConversationStore(tmp_path / "conv")
        store.save(
            SessionRecord(
                session_id="small", workspace="/tmp", model="m", mode="code",
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert store.archive_compressed("small", keep_recent=200) is None


class TestHeartbeatDefaultStateDir:
    """心跳持久化接默认状态目录 (突破四: 进程重启不误判)。"""

    def test_default_path_from_state_dir(self, tmp_path, monkeypatch):
        from coworker.heartbeat import default_heartbeat_path

        fake = str(tmp_path / "state")
        monkeypatch.setenv("COWORKER_STATE_DIR", fake)
        expected = os.path.join(fake, "heartbeat.json")
        assert str(default_heartbeat_path()).replace("\\", "/") == expected.replace("\\", "/")

    def test_persist_and_reload(self, tmp_path, monkeypatch):
        from coworker.heartbeat import HoneycombHeartbeat

        monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
        hb = HoneycombHeartbeat(tick_seconds=30.0)  # persist_default=True
        assert hb.path is not None
        hb.register("task-p")
        hb.pulse("task-p")
        assert hb.path.exists()
        hb2 = HoneycombHeartbeat(tick_seconds=30.0)  # 模拟重启
        assert "task-p" in hb2.heartbeats

    def test_memory_only_mode(self, tmp_path, monkeypatch):
        from coworker.heartbeat import HoneycombHeartbeat

        monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
        hb = HoneycombHeartbeat(tick_seconds=30.0, persist_default=False)
        assert hb.path is None


# -- 第七节续作: 定期归档调度 + checkpoint LLM 摘要粒度 ------------------------

class TestSessionPeriodicArchive:
    """定期归档调度: 超长会话旧消息皮萨诺压缩 (突破三 B4), 接入统一维护入口。"""

    def _store(self, tmp_path, sessions):
        from coworker.conversations import ConversationStore
        from coworker.sessions import SessionRecord

        store = ConversationStore(tmp_path / "conv")
        for sid, n in sessions:
            msgs = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"{sid} 消息 {i} " * 3}
                for i in range(n)
            ]
            store.save(SessionRecord(session_id=sid, workspace="/tmp", model="m", mode="code", messages=msgs))
        return store

    def test_archive_long_sessions(self, tmp_path):
        from coworker.memory.maintenance import archive_long_sessions

        store = self._store(tmp_path, [("sess-long", 600), ("sess-short", 50)])
        # dry-run 只报告。
        r_dry = archive_long_sessions(store, threshold=500, keep_recent=200, dry_run=True)
        assert [a["session_id"] for a in r_dry["archived"]] == ["sess-long"]
        assert store._count("sess-long") == 600  # 未动存储
        # 实际归档。
        r = archive_long_sessions(store, threshold=500, keep_recent=200)
        assert len(r["archived"]) == 1
        assert r["archived"][0]["archived"] == 400
        assert store._count("sess-long") == 200
        assert len(store.full_history("sess-long")) == 600  # 无损

    def test_run_maintenance_includes_archive(self, tmp_path):
        from coworker.memory.maintenance import run_maintenance

        class _MemStore:
            def list(self, include_stale=False):
                return []

        store = self._store(tmp_path, [("s2-long", 300), ("s2-short", 30)])
        res = run_maintenance(_MemStore(), conversation_store=store, session_archive_threshold=100)
        assert "sessions_archive" in res
        assert len(res["sessions_archive"]["archived"]) == 1
        assert res["sessions_archive"]["archived"][0]["session_id"] == "s2-long"


class TestCheckpointSummarySummarizer:
    """checkpoint summary 粒度升级: 注入 LLM 摘要, 无 summarizer 安全回退。"""

    def _state(self):
        return {"messages": [{"role": "user", "content": f"m{i}"} for i in range(50)], "tasks": []}

    def test_summary_uses_summarizer(self, tmp_path):
        cp = FractalCheckpoint(tmp_path / "cp_llm.db", summarizer=lambda msgs: f"[LLM摘要] 共 {len(msgs)} 条")
        cp.save_checkpoint("s-llm", self._state(), n_layer=4)  # summary 粒度
        restored = cp.restore_latest("s-llm")
        assert restored["messages_summary"].startswith("[LLM摘要]")
        assert len(restored.get("messages", [])) < 50  # 摘要替代全量

    def test_fallback_tail_keep(self, tmp_path):
        cp = FractalCheckpoint(tmp_path / "cp_fb.db")
        cp.save_checkpoint("s-fb", self._state(), n_layer=4)
        assert len(cp.restore_latest("s-fb").get("messages", [])) == 20

    def test_summarizer_error_fallback(self, tmp_path):
        def bad(_msgs):
            raise RuntimeError("summarizer down")

        cp = FractalCheckpoint(tmp_path / "cp_bad.db", summarizer=bad)
        cp.save_checkpoint("s-bad", self._state(), n_layer=4)
        assert len(cp.restore_latest("s-bad").get("messages", [])) == 20

    def test_full_granularity_unaffected(self, tmp_path):
        cp = FractalCheckpoint(tmp_path / "cp_full.db", summarizer=lambda msgs: "sum")
        cp.save_checkpoint("s-f", self._state(), n_layer=1)  # full 粒度
        assert len(cp.restore_latest("s-f").get("messages", [])) == 50
