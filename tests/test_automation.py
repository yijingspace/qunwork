"""Tests for automation — models, store, next-run math, scheduler loop, tools, REST.

No network and no LLM: the scheduler's runner is injected with a fake; the agent-facing tools
operate on a real SQLite store; execution policy (catch-up, overlap) is exercised directly.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from coworker.automation import (
    Schedule,
    ScheduledTask,
    Scheduler,
    TaskRun,
    TaskStore,
    compute_next_run,
)
from coworker.automation.tools import scheduling_tools


def _task(**kw) -> ScheduledTask:
    kw.setdefault("title", "Daily brief")
    kw.setdefault("instructions", "search the web and brief me")
    kw.setdefault("schedule", Schedule(kind="cron", cron="10 19 * * *"))
    kw.setdefault("workspace", "/tmp/cw-auto")
    return ScheduledTask(**kw)


# -- model / schedule ----------------------------------------------------------
def test_schedule_human():
    assert Schedule("cron", cron="10 19 * * *").human() == "Every day at ~7:10 PM"
    assert "Monday" in Schedule("cron", cron="0 9 * * 0").human()
    assert Schedule("cron", cron="0 9 5 * *").human() == "Monthly on day 5 at ~9:00 AM"
    assert Schedule("once", fire_at="2026-07-01T09:00:00").human().startswith("Once at")


def test_task_gets_own_thread_id():
    t = _task()
    assert t.task_session_id == f"__task__{t.id}"
    assert t.public()["schedule"] == "Every day at ~7:10 PM"


def test_compute_next_run_cron_explicit_utc():
    t = _task(schedule=Schedule(kind="cron", cron="10 19 * * *", timezone="UTC"))
    after = datetime(2026, 6, 5, 18, 0, tzinfo=timezone.utc).timestamp()
    nxt = compute_next_run(t, after=after)
    assert datetime.fromtimestamp(nxt, tz=timezone.utc) == datetime(
        2026, 6, 5, 19, 10, tzinfo=timezone.utc
    )


def test_compute_next_run_defaults_to_local_time():
    """Default 'local' tz: '7:10pm' fires at 19:10 on the *machine's* clock, not UTC."""
    t = _task()  # Schedule default timezone == "local"
    assert t.schedule.timezone == "local"
    nxt = compute_next_run(t)
    local = datetime.fromtimestamp(nxt).astimezone()
    assert (local.hour, local.minute) == (19, 10)


def test_compute_next_run_once_in_past_is_none():
    past = "2020-01-01T00:00:00+00:00"
    t = _task(schedule=Schedule(kind="once", fire_at=past))
    assert compute_next_run(t) is None


# -- store ---------------------------------------------------------------------
def test_store_crud_and_due(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    t = _task(
        schedule=Schedule(kind="cron", cron="* * * * *")
    )  # every minute → due soon
    store.save(t)
    assert store.get(t.id).title == "Daily brief"
    assert [x.id for x in store.list()] == [t.id]
    # next_run computed + due() finds it once we're past next_run
    due = store.due(now=t.next_run + 1)
    assert [x.id for x in due] == [t.id]
    # disabled tasks are not due
    t.enabled = False
    store.save(t)
    assert store.due(now=t.next_run + 1 if t.next_run else 9e9) == []
    assert store.delete(t.id) is True and store.get(t.id) is None


def test_store_runs_history(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    t = _task()
    store.save(t)
    store.add_run(TaskRun(task_id=t.id, status="ok", result_text="hi"))
    store.add_run(TaskRun(task_id=t.id, status="error", error="boom"))
    runs = store.runs(t.id)
    assert len(runs) == 2 and runs[0].status in ("ok", "error")


# -- scheduler loop ------------------------------------------------------------
async def test_scheduler_runs_due_task_and_advances(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    t = _task(schedule=Schedule(kind="cron", cron="* * * * *"))
    store.save(t)
    # force it due now
    t.next_run = 1.0
    store.save(t)
    t.next_run = 1.0  # save() recomputes; push it into the past again
    store._conn.execute("UPDATE scheduled_tasks SET next_run=1.0 WHERE id=?", (t.id,))
    store._conn.commit()

    ran: list[str] = []

    async def runner(task, trigger):
        ran.append(task.id)
        return TaskRun(task_id=task.id, status="ok", trigger=trigger)

    sched = Scheduler(store, runner, tick_seconds=0.05)
    sched.start()
    await asyncio.sleep(0.2)
    await sched.stop()
    assert ran == [t.id]
    advanced = store.get(t.id)
    assert advanced.run_count == 1 and advanced.last_status == "ok"
    assert (
        advanced.next_run is not None and advanced.next_run > 1.0
    )  # moved to the future


async def test_scheduler_skips_overlapping_run(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    t = _task()
    store.save(t)
    gate = asyncio.Event()
    started = 0

    async def slow_runner(task, trigger):
        nonlocal started
        started += 1
        await gate.wait()
        return TaskRun(task_id=task.id, status="ok")

    sched = Scheduler(store, slow_runner)
    first = asyncio.create_task(sched.run_task(t, trigger="manual"))
    await asyncio.sleep(0.02)
    second = await sched.run_task(t, trigger="manual")  # overlaps → skipped
    assert second is None and started == 1
    gate.set()
    await first


# -- agent-facing tools --------------------------------------------------------
def test_create_and_list_tools(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    origin = {
        "surface": "cowork",
        "session_id": "s1",
        "workspace": "/tmp/ws",
        "agent": "cowork",
    }
    tools = {
        t.__name__: t
        for t in scheduling_tools(store, origin=origin, default_workspace="/tmp/ws")
    }

    out = tools["create_scheduled_task"](
        title="Brief", instructions="brief me", cron="10 19 * * *"
    )
    assert out["ok"] and out["schedule"] == "Every day at ~7:10 PM"
    # create surfaces a confirm card → gated
    assert (
        tools["create_scheduled_task"].__aisuite_tool_metadata__.requires_approval
        is True
    )

    listed = tools["list_scheduled_tasks"]()["tasks"]
    assert (
        len(listed) == 1
        and listed[0]["origin_session_id" if False else "title"] == "Brief"
    )
    saved = store.list()[0]
    assert saved.origin_session_id == "s1" and saved.workspace == "/tmp/ws"

    bad = tools["create_scheduled_task"](title="x", instructions="y", cron="not-a-cron")
    assert "invalid cron" in bad["error"]
    none = tools["create_scheduled_task"](title="x", instructions="y")
    assert "error" in none  # neither cron nor fire_at


def test_update_and_delete_tools(tmp_path):
    store = TaskStore(tmp_path / "auto.db")
    tools = {
        t.__name__: t
        for t in scheduling_tools(
            store, origin={"workspace": "/tmp/ws"}, default_workspace="/tmp/ws"
        )
    }
    tid = tools["create_scheduled_task"](
        title="X", instructions="do", cron="0 9 * * *"
    )["id"]
    assert (
        tools["update_scheduled_task"](id=tid, enabled=False)["task"]["enabled"]
        is False
    )
    assert store.get(tid).next_run is None  # disabled → no next run
    assert tools["delete_scheduled_task"](id=tid)["ok"] is True
    assert tools["update_scheduled_task"](id=tid)["error"]


# -- run persists as a continuable session -------------------------------------
async def test_scheduled_run_persists_continuable_session(tmp_path, monkeypatch):
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
    from coworker.server.manager import SessionManager, _last_assistant_text

    class ScriptedProvider(ProviderClient):
        def __init__(self, turns):
            self._turns = list(turns)

        def complete(self, *, model, messages, tools=None, **settings):
            return self._turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    # two turns: the scheduled run, then a follow-up question
    provider = ScriptedProvider(
        [
            AssistantTurn(text="Daily brief: all quiet.", finish_reason="stop"),
            AssistantTurn(text="Sure — here is more detail.", finish_reason="stop"),
        ]
    )
    manager = SessionManager(data_dir=tmp_path / "data", provider=provider)
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    run = await manager._run_scheduled_task(task, trigger="manual")
    assert run.status == "ok" and run.session_id == f"__run__{run.run_id}"
    assert run.result_text == "Daily brief: all quiet."

    # the run is now a real, reopenable session with the transcript
    record = manager.session_store.load(run.session_id)
    assert (
        record is not None
        and record.workspace
        and any("Scheduled run" in (m.get("content") or "") for m in record.messages)
    )
    # …and it is continuable: a follow-up turn reuses the same thread
    engine = manager.get_engine(run.session_id, workspace=str(ws), agent="cowork")
    async for _ in engine.run("tell me more"):
        pass
    assert _last_assistant_text(engine.messages) == "Sure — here is more detail."


def test_task_engine_has_no_scheduling_tools(tmp_path, monkeypatch):
    """A scheduled run executes its instructions — it must not be able to (re)schedule. With
    instructions like 'every day at 5:32pm, prepare…', an agent holding create_scheduled_task
    creates another automation instead of doing the task."""
    from coworker.providers import (
        AssistantTurn as _AT,
        ModelCapabilities,
        ProviderClient,
    )
    from coworker.server import SessionManager

    class _Provider(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return _AT(text="ok", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(data_dir=tmp_path / "data", provider=_Provider())
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    engine = manager._build_task_engine(task, session_id="__run__test")
    names = set(engine.registry.names())
    assert "create_scheduled_task" not in names
    assert "update_scheduled_task" not in names
    assert "write_file" in names  # the deliverable tools are still there


async def test_manual_run_prepare_and_finalize(tmp_path, monkeypatch):
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
    from coworker.server.manager import SessionManager

    class ScriptedProvider(ProviderClient):
        def __init__(self, turns):
            self._turns = list(turns)

        def complete(self, *, model, messages, tools=None, **settings):
            return self._turns.pop(0)

        def capabilities(self, model):
            return ModelCapabilities()

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(
        data_dir=tmp_path / "data",
        provider=ScriptedProvider(
            [AssistantTurn(text="Done — briefing ready.", finish_reason="stop")]
        ),
    )
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    # prepare: a "running" run + a session to open live (NOT executed yet)
    prep = manager.prepare_manual_run(task.id)
    assert prep["ok"] and prep["session_id"] == f"__run__{prep['run_id']}"
    # The prompt wraps the instructions in execute-now framing (so the live agent runs the task
    # instead of re-scheduling it) and carries them verbatim.
    assert prep["agent"] == "cowork"
    assert task.instructions in prep["prompt"]
    assert "do not create or modify any scheduled tasks" in prep["prompt"]
    assert manager.task_store.runs(task.id)[0].status == "running"

    # the GUI drives the run live over the session, then finalize records the outcome
    engine = manager.get_engine(prep["session_id"], workspace=str(ws), agent="cowork")
    async for _ in engine.run(prep["prompt"]):
        pass
    manager.save(prep["session_id"], engine)

    out = manager.finalize_manual_run(task.id, prep["run_id"])
    assert out["ok"] and out["run"]["status"] == "ok"
    assert out["run"]["result_text"] == "Done — briefing ready."
    assert manager.task_store.get(task.id).run_count == 1


# -- REST ----------------------------------------------------------------------
def test_automations_rest(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from coworker.server.app import create_app
    from coworker.server.manager import SessionManager

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    manager = SessionManager(data_dir=tmp_path / "data")
    # seed a task directly via the store
    t = _task(workspace=str(tmp_path / "ws"))
    manager.task_store.save(t)
    client = TestClient(create_app(manager))

    tasks = client.get("/v1/automations").json()["tasks"]
    assert (
        tasks[0]["title"] == "Daily brief"
        and tasks[0]["schedule"] == "Every day at ~7:10 PM"
    )
    assert (
        client.patch(f"/v1/automations/{t.id}", json={"enabled": False}).json()["task"][
            "enabled"
        ]
        is False
    )
    assert client.get(f"/v1/automations/{t.id}").json()["task"]["id"] == t.id
    assert client.delete(f"/v1/automations/{t.id}").json()["ok"] is True


# -- unseen-run tracking (UX-023 sidebar badges) --------------------------------
def test_unseen_runs_counted_and_cleared_by_mark_seen(tmp_path, monkeypatch):
    """list_automations surfaces unseen counts (runs after the seen mark), with
    unseen_failed keyed to the NEWEST unseen run; mark_automation_seen clears them
    and later runs count fresh."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    t = manager.task_store.save(_task())
    manager.task_store.add_run(TaskRun(task_id=t.id, status="ok"))
    manager.task_store.add_run(TaskRun(task_id=t.id, status="error"))

    row = manager.list_automations()["tasks"][0]
    assert row["unseen_runs"] == 2
    assert row["unseen_failed"] is True  # newest unseen run errored

    assert manager.mark_automation_seen(t.id)["ok"]
    row = manager.list_automations()["tasks"][0]
    assert row["unseen_runs"] == 0 and row["unseen_failed"] is False

    time.sleep(0.01)  # a run strictly after the seen mark
    manager.task_store.add_run(TaskRun(task_id=t.id, status="ok"))
    row = manager.list_automations()["tasks"][0]
    assert row["unseen_runs"] == 1 and row["unseen_failed"] is False

    assert not manager.mark_automation_seen("task-nope")["ok"]


@pytest.mark.asyncio
async def test_scheduled_run_broadcasts_run_started_event(tmp_path, monkeypatch):
    """UX-026: the moment a scheduled run starts, every /ws/events socket hears
    automation_run_started (the top-right toast). Dead sockets drop silently."""
    from coworker.providers import AssistantTurn, ModelCapabilities, ProviderClient
    from coworker.server.manager import SessionManager

    class ScriptedProvider(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return AssistantTurn(text="done", finish_reason="stop")

        def capabilities(self, model):
            return ModelCapabilities()

    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    manager = SessionManager(data_dir=tmp_path / "data", provider=ScriptedProvider())
    task = _task(workspace=str(ws), agent="cowork")
    manager.task_store.save(task)

    heard: list = []

    async def listener(message):
        heard.append(message)

    async def dead(message):
        raise RuntimeError("socket gone")

    manager.register_event_client(listener)
    manager.register_event_client(dead)
    run = await manager._run_scheduled_task(task, trigger="schedule")

    (event,) = [m for m in heard if m["type"] == "automation_run_started"]
    assert event["data"]["task_id"] == task.id
    assert event["data"]["task_title"] == task.title
    assert event["data"]["session_id"] == run.session_id
    assert event["data"]["trigger"] == "schedule"
    assert dead not in manager._event_clients  # dropped, not fatal


def test_reap_stale_runs_marks_crashed_running_as_error(tmp_path):
    from coworker.automation import TaskStore
    from coworker.automation.models import TaskRun

    store = TaskStore(tmp_path / "auto.db")
    run = TaskRun(task_id="t", trigger="schedule")
    store.add_run(run)
    # simulate a wedged run: backdate it beyond the reap window
    import json

    d = json.loads(store.find_run(run.run_id).to_json() if hasattr(store.find_run(run.run_id), "to_json") else json.dumps(store.find_run(run.run_id).to_dict()))
    d["status"] = "running"
    d["started_at"] = d["started_at"] - 10000
    store._conn.execute(
        "INSERT OR REPLACE INTO task_runs (run_id, task_id, started_at, data) VALUES (?, ?, ?, ?)",
        (run.run_id, run.task_id, d["started_at"], json.dumps(d)),
    )
    store._conn.commit()
    assert store.reap_stale_runs(older_than=60) == 1
    reaped = store.find_run(run.run_id)
    assert reaped.status == "error"
    assert "interrupted by restart" in (reaped.error or "")
    # a fresh running run is untouched
    run2 = TaskRun(task_id="t2", trigger="schedule")
    store.add_run(run2)
    assert store.reap_stale_runs(older_than=60) == 0
    assert store.find_run(run2.run_id).status == "running"


# -- P0 建议4: Rhythm × Automation ---------------------------------------------

def test_task_priority_default_and_roundtrip():
    """priority defaults to normal; survives store round-trip; legacy blobs
    (no priority key) load as normal — back-compatible."""
    t = _task()
    assert t.priority == "normal"
    assert t.public()["priority"] == "normal"
    t.priority = "low"
    assert t.public()["priority"] == "low"

    d = t.to_dict()
    del d["priority"]  # simulate a pre-feature persisted blob
    legacy = ScheduledTask.from_dict(d)
    assert legacy.priority == "normal"


async def test_scheduler_defers_low_priority_in_peak(tmp_path):
    """During a rhythm peak (gate False) a low-priority task is deferred, while
    a normal task still fires. Anti-starvation lets the deferred one through."""
    store = TaskStore(tmp_path / "auto.db")
    low = _task(title="Low-value cleanup", priority="low")
    normal = _task(title="Normal task")
    store.save(low)
    store.save(normal)
    ran: list[str] = []

    async def runner(task, trigger):
        ran.append(task.id)
        return TaskRun(task_id=task.id, status="ok", trigger=trigger)

    sched = Scheduler(
        store,
        runner,
        tick_seconds=0.05,
        rhythm_gate=lambda: False,  # permanent peak
    )
    for t in (low, normal):
        t.next_run = 1.0
        store._conn.execute(
            "UPDATE scheduled_tasks SET next_run=1.0 WHERE id=?", (t.id,)
        )
    store._conn.commit()

    sched.start()
    await asyncio.sleep(0.2)
    await sched.stop()
    # normal fired; low was deferred (≤5 deferrals) and did NOT run
    assert normal.id in ran
    assert low.id not in ran
    assert sched._deferrals.get(low.id, 0) > 0


async def test_scheduler_anti_starvation_runs_deferred_low_priority(tmp_path):
    """An unbroken peak must not postpone a low-priority task forever: after
    _max_rhythm_deferrals deferrals it runs anyway."""
    store = TaskStore(tmp_path / "auto.db")
    low = _task(title="cleanup", priority="low")
    store.save(low)
    ran: list[str] = []

    async def runner(task, trigger):
        ran.append(task.id)
        return TaskRun(task_id=task.id, status="ok", trigger=trigger)

    sched = Scheduler(store, runner, tick_seconds=0.02, rhythm_gate=lambda: False)
    sched._max_rhythm_deferrals = 2  # shorten the wait for the test
    low.next_run = 1.0
    store._conn.execute("UPDATE scheduled_tasks SET next_run=1.0 WHERE id=?", (low.id,))
    store._conn.commit()

    sched.start()
    await asyncio.sleep(0.2)
    await sched.stop()
    assert low.id in ran  # anti-starvation kicked in


def test_create_automation_accepts_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    ok = manager.create_automation(
        {
            "title": "nightly archive",
            "instructions": "archive old files",
            "cron": "0 3 * * *",
            "priority": "low",
        }
    )
    assert ok["ok"] is True
    assert ok["task"]["priority"] == "low"
    # invalid priority falls back to normal
    bad = manager.create_automation(
        {
            "title": "weird",
            "instructions": "x",
            "cron": "0 3 * * *",
            "priority": "urgent!!",
        }
    )
    assert bad["task"]["priority"] == "normal"


def test_rhythm_recommendations_picks_valley_hour(tmp_path, monkeypatch):
    """recommendations rank automations by valley_share and name the hour with
    fewest historical runs as the recommended trigger hour."""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    manager = SessionManager(data_dir=tmp_path / "data")
    # 20 runs: 16 at 09:00, 2 at 03:00, 2 at 04:00 → valley hour 03:00
    import datetime as _dt

    base = _dt.datetime(2026, 8, 1, 9, 0).timestamp()
    t = manager.task_store.save(_task(title="brief", schedule=Schedule(kind="cron", cron="0 9 * * *")))
    for i, h in enumerate([9] * 16 + [3] * 2 + [4] * 2):
        manager.task_store.add_run(
            TaskRun(task_id=t.id, started_at=base + i * 3600 * 24 + (h - 9) * 3600, status="ok")
        )
    manager.task_store.save(_task(title="no-history", schedule=Schedule(kind="cron", cron="0 12 * * *")))

    out = manager.rhythm_recommendations()
    assert "recommendations" in out
    recs = out["recommendations"]
    assert len(recs) == 1  # only the task with ≥4 runs
    r = recs[0]
    assert r["task_id"] == t.id
    assert r["recommended_hour"] == 3  # fewest runs
    assert r["current_hour"] == 9
    assert 0.0 < r["valley_share"] <= 1.0
    assert r["runs"] == 20


def test_compute_next_run_once_accepts_epoch_float():
    """_hornet_act_gap 曾传 epoch 浮点 fire_at → fromisoformat ValueError →
    next_run=None 永不运行 (2026-08 修复: 兼容浮点/数字字符串)。"""
    t = _task(schedule=Schedule(kind="once", fire_at=1_800_000_000.0))
    assert compute_next_run(t, after=1_700_000_000.0) == 1_800_000_000.0
    t2 = _task(schedule=Schedule(kind="once", fire_at="1800000000"))
    assert compute_next_run(t2, after=1_700_000_000.0) == 1_800_000_000.0
    # 无效 fire_at 仍安全返回 None
    t3 = _task(schedule=Schedule(kind="once", fire_at="not-a-date"))
    assert compute_next_run(t3, after=1_700_000_000.0) is None


def test_hornet_gap_task_gets_valid_next_run(tmp_path, monkeypatch):
    """_hornet_act_gap 创建的补全任务必须有有效 next_run(修复前 once+epoch/now
    → next_run=None → 永不运行)。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    actions: dict = {"task": []}
    mgr._hornet_act_gap(
        "知识空洞: graph", {"hint": "补充关联或合并"}, str(tmp_path / "ws"), actions
    )
    assert actions["task"] == ["知识空洞: graph"]
    hornet_tasks = [t for t in mgr.task_store.list() if t.title.startswith("[HORNET]")]
    assert len(hornet_tasks) == 1
    t = hornet_tasks[0]
    assert t.schedule.kind == "once"
    assert t.next_run is not None, "补全任务 next_run 必须非 None — 否则 scheduler 永不触发"
    assert t.next_run > 0


def test_hornet_gap_task_injects_gap_node_context(tmp_path, monkeypatch):
    """问题2: 空洞补全任务必须自带空洞节点上下文 (标题/内容/邻居) —
    否则 run 不知道补哪里、补什么, 只会盲目搜索 (2026-08 反馈)。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    # Seed a HORNET node (isolated) that the gap points at.
    node_id = mgr.hornet.add_node(
        kb_item_id=999, title="graph", content="Obsidian 图谱配置, 无知识语义",
    )
    actions: dict = {"task": []}
    mgr._hornet_act_gap(
        "知识空洞: graph",
        {"hint": "该节点没有语义连接,建议补充关联或合并", "node_id": node_id},
        str(tmp_path / "ws"),
        actions,
    )
    t = [t for t in mgr.task_store.list() if t.title.startswith("[HORNET]")][0]
    instructions = t.instructions
    # The gap node's title + content excerpt must ride into the task instructions.
    assert "graph" in instructions
    assert "Obsidian 图谱配置" in instructions
    assert "空洞节点信息" in instructions
    assert str(node_id) in instructions


# -- DPNN 大小周期嵌套 catch-up (自动化错过补跑) ------------------------------

def test_failed_run_retries_in_small_cycle(tmp_path):
    """失败的任务: next_run 设短重试 (小周期, 5 分钟), 而非跳到下个大周期
    (周报 9 点失败 → 几小时后重试当天, 不跳过)。"""
    store = TaskStore(tmp_path / "auto.db")
    calls = {"n": 0}

    async def failing_runner(task, trigger):
        calls["n"] += 1
        return TaskRun(task_id=task.id, status="error", error="boom")

    sched = Scheduler(store, failing_runner, tick_seconds=0.01)
    t = _task(schedule=Schedule(kind="cron", cron="0 9 * * 1"))
    store.save(t)

    import asyncio

    run = asyncio.run(sched.run_task(t, trigger="schedule"))
    assert run.status == "error"
    fresh = store.get(t.id)
    assert fresh.retry_until is not None  # 宽限窗口开启
    # 失败后 next_run 是短延迟 (5 分钟), 不是下周 9:00 (大周期)
    assert fresh.next_run is not None
    short = fresh.next_run - time.time()
    assert 0 < short < 3600  # 小周期: 分钟级, 不是按周


def test_success_advances_to_next_big_cycle(tmp_path):
    """成功的任务: 推进状态 (run_count+1), 清空重试状态 (retry_until=None)。"""
    store = TaskStore(tmp_path / "auto.db")

    async def ok_runner(task, trigger):
        return TaskRun(task_id=task.id, status="ok")

    sched = Scheduler(store, ok_runner, tick_seconds=0.01)
    t = _task(schedule=Schedule(kind="cron", cron="* * * * *"))
    store.save(t)

    import asyncio

    asyncio.run(sched.run_task(t, trigger="schedule"))
    fresh = store.get(t.id)
    assert fresh.retry_until is None  # 成功 → 无重试状态
    assert fresh.run_count == 1  # 已推进
    assert fresh.last_status == "ok"
    assert fresh.next_run is not None  # 已排下次


def test_retry_exhausted_advances_big_cycle(tmp_path):
    """重试耗尽 (max_retries 次失败) → 放弃本周期, 清重试状态 (不无限重试)。"""
    store = TaskStore(tmp_path / "auto.db")

    async def failing_runner(task, trigger):
        return TaskRun(task_id=task.id, status="error", error="still broken")

    sched = Scheduler(store, failing_runner, tick_seconds=0.01)
    sched.max_retries = 2
    t = _task(schedule=Schedule(kind="cron", cron="* * * * *"))
    store.save(t)

    import asyncio

    for _ in range(3):  # 3 次失败 > max_retries=2
        asyncio.run(sched.run_task(t, trigger="schedule"))
        t = store.get(t.id)

    fresh = store.get(t.id)
    assert fresh.retry_until is None  # 重试状态已清 (耗尽后放弃本周期)
    assert fresh.last_status == "error"
    assert fresh.next_run is not None  # 已排下次 cron (大周期推进)


def test_workspace_null_falls_back_in_task_engine(tmp_path, monkeypatch):
    """知识补全等任务 workspace=None 时, _build_task_engine 回退默认工作区,
    不 Path(None) 崩溃 (修复 'not NoneType' 错误)。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager

    mgr = SessionManager(data_dir=tmp_path / "data")
    t = _task(workspace=None)
    # 不抛异常即通过 (旧代码 Path(None) 崩溃)
    engine = mgr._build_task_engine(t, session_id="__task__test")
    assert engine is not None
    engine.executor.close()


def test_unattended_scheduler_run_builds_full_access_engine(tmp_path, monkeypatch):
    """问题1: 无人值守调度路径 (schedule/catchup) 的引擎走完全访问 (Mode.AUTO)
    — 知识库/文件/命令工具自动放行, 不再 park 在 Inbox 等人批准 (2026-08 反馈:
    '任务触发时申请知识库权限, 人不在场就一直等授权')。"""
    monkeypatch.setenv("COWORKER_STATE_DIR", str(tmp_path / "state"))
    from coworker.server.manager import SessionManager
    from coworker.permissions import Mode

    mgr = SessionManager(data_dir=tmp_path / "data")
    t = _task(workspace=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(exist_ok=True)
    engine = mgr._build_task_engine(t, session_id="__run__auto", mode=Mode.AUTO)
    # Full access: a knowledge tool + a file read evaluate allowed with NO user prompt.
    for tool in ("knowledge_search", "read_file", "run_shell"):
        decision = engine.permissions.evaluate(tool, {"query": "x" if tool == "knowledge_search" else "ls"})
        assert decision.allowed, f"{tool} must be auto-allowed in unattended run: {decision}"
        assert not decision.needs_user
    engine.executor.close()


def test_startup_catchup_runs_missed_due_task(tmp_path):
    """DPNN 大小周期: 触发点错过 (9 点没开机) 后启动, catchup 补跑当天的
    任务 — next_run 已过期 (< now) 的任务在启动时立即执行 (不跳到下周)。"""
    store = TaskStore(tmp_path / "auto.db")
    ran: list[str] = []

    async def runner(task, trigger):
        ran.append(trigger)
        return TaskRun(task_id=task.id, status="ok")

    sched = Scheduler(store, runner, tick_seconds=0.01)
    t = _task(schedule=Schedule(kind="cron", cron="* * * * *"))
    store.save(t)
    # 模拟错过: 直接改 DB 的 next_run 为过去 (触发点已过, 机器当时没开),
    # 绕过 save() 的重算 (save 会按 schedule 算未来)。
    past = time.time() - 600
    with store._lock:
        store._conn.execute(
            "UPDATE scheduled_tasks SET next_run=? WHERE id=?", (past, t.id)
        )
        store._conn.commit()

    # catchup tick: 启动时调 _tick(trigger="catchup") — due() 返回过期任务并执行
    import asyncio

    asyncio.run(sched._tick(trigger="catchup"))
    assert "catchup" in ran  # 错过的任务被 catchup 补跑
    fresh = store.get(t.id)
    assert fresh.last_status == "ok"
    assert fresh.next_run is not None and fresh.next_run > time.time()  # 已排下次
