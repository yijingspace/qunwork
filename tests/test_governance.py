"""Governance loop (Phase 2) tests — health metrics and command decisions."""

from coworker.orchestrator.governance import (
    ESCALATE,
    NOP,
    PAUSE,
    REVERT,
    WARN,
    Governance,
    GovernanceConfig,
)
from coworker.orchestrator.models import Plan, Task


def _plan(*tasks: Task) -> Plan:
    return Plan(goal="goal", tasks=list(tasks))


def _task(tid: str, desc: str, status: str = "done", confidence: float = 0.9, result: str = "r") -> Task:
    return Task(id=tid, description=desc, status=status, confidence=confidence, result=result)


def _gov(goal: str = "Write a report", **kw) -> Governance:
    return Governance(goal=goal, config=GovernanceConfig(**kw))


# -- metrics ----------------------------------------------------------------


def test_viscosity_zero_with_distinct_results():
    gov = _gov()
    for i in range(4):
        gov.record_step(_task(f"t{i}", f"task {i}", result=f"distinct result number {i}"), f"r{i}", True)
    assert gov.viscosity() == 0.0


def test_viscosity_high_with_stuck_results():
    gov = _gov()
    for _ in range(4):
        gov.record_step(_task("t0", "same task", result="identical output"), "identical output", False)
    assert gov.viscosity() > 0.5


def test_drift_uses_embedder_when_provided():
    def embedder(s: str) -> list[float]:
        return [1.0 if "report" in s else 0.0, 1.0 if "code" in s else 0.0]

    gov = Governance(goal="Write a report", embedder=embedder)
    plan = _plan(_task("t0", "Refactor the codebase", status="running"))
    assert gov.drift(plan) > 0.9  # orthogonal embeddings → high drift


def test_drift_fallback_difflib():
    gov = _gov()
    plan = _plan(_task("t0", "Write a report about QunWork", status="running"))
    assert gov.drift(plan) < 0.5


def test_red_line_hit():
    gov = _gov(red_lines=["drop table"])
    plan = _plan(_task("t0", "Clean up the DB — drop table logs", status="pending"))
    assert gov.red_line_hit(plan)
    assert gov.inspect(plan).action == PAUSE


# -- decisions --------------------------------------------------------------


def test_nominal_command():
    gov = _gov()
    plan = _plan(_task("t0", "Write intro", result="intro done"))
    gov.record_step(_task("t0", "Write intro", result="intro done"), "intro done", True)
    cmd = gov.inspect(plan)
    assert cmd.action == NOP


def test_drift_escalates_to_human():
    gov = _gov(check_every=1, drift_threshold=0.1)
    plan = _plan(_task("t0", "Write a report", status="running"))
    cmd = gov.inspect(plan)  # goal=="Write a report" vs task=="Write a report" — similarity high
    # Force high drift with an unrelated running task.
    plan.tasks[0].description = "Set up the Kubernetes cluster"
    cmd2 = gov.inspect(plan)
    assert cmd2.action == WARN and cmd2.escalate


def test_viscosity_mid_warns():
    gov = _gov(check_every=1)
    plan = _plan(_task("t0", "Write a report"))
    # two identical + one different -> viscosity 0.5 (mid band: 0.4..0.66)
    for r in ["stuck output", "stuck output", "different output"]:
        gov.record_step(_task("t0", "Write a report", result=r), r, False)
    cmd = gov.inspect(plan)
    assert cmd.action == WARN


def test_viscosity_high_reverts():
    gov = _gov(check_every=1, viscosity_high=0.2)
    plan = _plan(_task("t0", "Write a report"))
    for _ in range(3):
        gov.record_step(_task("t0", "Write a report", result="stuck output"), "stuck output", False)
    cmd = gov.inspect(plan)
    assert cmd.action == REVERT


def test_revert_target_picks_low_confidence_done_task():
    gov = _gov()
    plan = _plan(
        _task("t0", "a", confidence=0.9),
        _task("t1", "b", confidence=0.3),
    )
    tgt = gov.revert_target(plan)
    assert tgt is not None and tgt.id == "t1"
    # bug #14: a stuck-again task may be reverted again (capped at _MAX_REVERTS)
    assert gov.revert_target(plan) is not None  # second revert allowed
    assert gov.revert_target(plan) is None  # capped — no oscillation


def test_max_warnings_escalates():
    gov = _gov(check_every=1, max_warnings=2, drift_threshold=0.4)
    plan = _plan(_task("t0", "task", status="running"))
    plan.tasks[0].description = "Completely unrelated topic here"
    for _ in range(2):
        cmd = gov.inspect(plan)
        assert cmd.action in (WARN, ESCALATE)
        if cmd.action == WARN:
            gov.note_warning(cmd)
    cmd = gov.inspect(plan)
    assert cmd.action == ESCALATE


def test_red_line_sees_execution_intent():
    """bug #12: a benign description hiding a dangerous executed action must trip the red line."""
    gov = _gov(goal="Summarize the file", red_lines=["rm -rf"])
    plan = _plan(
        _task("t0", "Read the file", confidence=0.9, result="I ran rm -rf /tmp to clean up"),
        _task("t1", "Summarize", status="pending", confidence=0.0, result=""),
    )
    assert gov.red_line_hit(plan) is True


def test_revert_second_occurrence_and_cap():
    """bug #14: second revert allowed, third capped."""
    gov = _gov()
    plan = _plan(_task("t0", "a", confidence=0.9), _task("t1", "b", confidence=0.3))
    assert gov.revert_target(plan) is not None
    assert gov.revert_target(plan) is not None
    assert gov.revert_target(plan) is None
