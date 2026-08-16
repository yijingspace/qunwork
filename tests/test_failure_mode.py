"""S8 工具失败模式库与重试/熔断策略 (蜂群审计报告 G9).

契约:
  * 熔断: 工具连续失败 >= max_failures 次 → circuit open, 后续调用短路
    (不执行, 返回 CircuitOpen 错误);
  * 冷却: cooldown 过后自动 half-open (放行一次试探, 成功恢复/失败再开);
  * 记录: 成功衰减失败计数; 失败模式库 (tool, error_type) → 次数;
  * engine 集成: _execute_sync 熔断检查 + 成功/失败记录。
"""

from __future__ import annotations

import time

import pytest

from coworker.tools.failure_mode import (
    FailureModeRegistry,
    get_failure_registry,
)


# -- 熔断器 -------------------------------------------------------------------


def test_circuit_opens_after_consecutive_failures():
    reg = FailureModeRegistry(max_failures=3, cooldown_seconds=60)
    assert reg.check("flaky_tool") is None  # 初始放行
    reg.record_failure("flaky_tool", "TimeoutError")
    reg.record_failure("flaky_tool", "TimeoutError")
    assert reg.check("flaky_tool") is None  # 2 次还没开
    reg.record_failure("flaky_tool", "TimeoutError")
    reason = reg.check("flaky_tool")
    assert reason is not None and "circuit-open" in reason  # 3 次 → 熔断


def test_success_decays_failure_count():
    reg = FailureModeRegistry(max_failures=3, cooldown_seconds=60)
    reg.record_failure("t", "Err")
    reg.record_failure("t", "Err")
    reg.record_success("t")  # 成功衰减
    reg.record_failure("t", "Err")
    reg.record_failure("t", "Err")  # 2+1-1+1+1 = 3 → 开
    assert reg.check("t") is not None


def test_cooldown_recovers_to_half_open():
    # cooldown 有 1.0s 下限 (保护), 测试用 1.1s 等待
    reg = FailureModeRegistry(max_failures=2, cooldown_seconds=1.0)
    reg.record_failure("t", "Err")
    reg.record_failure("t", "Err")
    assert reg.check("t") is not None  # open
    time.sleep(1.1)
    assert reg.check("t") is None  # 冷却过 → half-open 放行


def test_half_open_probe_success_closes_circuit():
    reg = FailureModeRegistry(max_failures=2, cooldown_seconds=1.0)
    reg.record_failure("t", "Err")
    reg.record_failure("t", "Err")
    time.sleep(1.1)
    reg.check("t")  # half-open
    reg.record_success("t")  # 试探成功
    assert reg.check("t") is None  # 熔断关闭
    assert reg.status()[0]["circuit_open"] is False


def test_failure_mode_library_counts():
    reg = FailureModeRegistry(max_failures=10)
    reg.record_failure("read_file", "FileNotFoundError")
    reg.record_failure("read_file", "FileNotFoundError")
    reg.record_failure("run_shell", "PermissionError")
    modes = {m["tool"]: m for m in reg.failure_modes()}
    assert modes["read_file"]["count"] == 2
    assert modes["read_file"]["error_type"] == "FileNotFoundError"
    assert modes["run_shell"]["count"] == 1


def test_most_failing_sorted():
    reg = FailureModeRegistry(max_failures=10)
    reg.record_failure("a", "E1")
    reg.record_failure("a", "E1")
    reg.record_failure("b", "E2")
    top = reg.most_failing(limit=2)
    assert top[0]["tool"] == "a" and top[0]["failures"] == 2


def test_process_singleton():
    a = get_failure_registry()
    b = get_failure_registry()
    assert a is b


# -- engine 集成 ---------------------------------------------------------------


def test_engine_circuit_short_circuits_failing_tool(tmp_path):
    """engine 层: 工具反复失败 → 熔断, 后续调用短路不执行。"""
    from coworker.engine import TurnEngine
    from coworker.permissions import PermissionEngine
    from coworker.providers import ModelCapabilities, ProviderClient, ToolCall
    from coworker.tools import ToolRegistry

    calls = {"n": 0}

    def boom(**kwargs):
        calls["n"] += 1
        raise RuntimeError("always fails")

    boom.__name__ = "boom_tool"
    boom.__coworker_schema__ = {
        "type": "function",
        "function": {"name": "boom_tool", "description": "boom", "parameters": {"type": "object", "properties": {}}},
    }
    registry = ToolRegistry()
    registry.register(boom)
    permissions = PermissionEngine(workspace_root=tmp_path)

    class P(ProviderClient):
        def complete(self, *, model, messages, tools=None, **settings):
            return __import__("coworker.providers", fromlist=["AssistantTurn"]).AssistantTurn(
                text="ok", finish_reason="stop"
            )

        def capabilities(self, model):
            return ModelCapabilities()

    engine = TurnEngine(
        provider=P(), registry=registry, permissions=permissions, model="m"
    )
    from coworker.tools.failure_mode import FailureModeRegistry

    reg = FailureModeRegistry(max_failures=2, cooldown_seconds=60)
    import coworker.tools.failure_mode as fm

    orig_get = fm.get_failure_registry

    def fake_get():
        return reg

    fm.get_failure_registry = fake_get
    try:
        # 第一次失败
        r1, s1 = engine._execute_sync(ToolCall(id="c1", name="boom_tool", arguments={}))
        assert s1 == "error"
        # 第二次失败
        r2, s2 = engine._execute_sync(ToolCall(id="c2", name="boom_tool", arguments={}))
        assert s2 == "error"
        # 第三次: 熔断 — 不执行 (calls 不再增加), 返回 CircuitOpen
        before = calls["n"]
        r3, s3 = engine._execute_sync(ToolCall(id="c3", name="boom_tool", arguments={}))
        assert s3 == "error"
        assert r3.get("error_type") == "CircuitOpen"
        assert calls["n"] == before  # 未真正执行
    finally:
        fm.get_failure_registry = orig_get