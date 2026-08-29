"""QunMesh M1: StigmergyBus 四信道信息素总线测试。

覆盖研究方案 M1 验证指标: 四信道存取 / 惰性蒸发 / TTL 过期 / SQLite 场恢复,
外加 PheromoneField 兼容契约 (load 信道同形 API) 与回滚开关解析。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coworker.pheromone import (
    CHANNELS,
    PheromoneField,
    StigmergyBus,
    _pheromone_bus_from_config,
)


class FakeClock:
    """可推进的注入时钟 — 蒸发/TTL 测试不睡真时间 (防负载下 flaky)。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_bus(**kw) -> StigmergyBus:
    clock = FakeClock()
    return StigmergyBus(now_fn=clock, **kw), clock


def test_four_channels_isolated():
    """四信道同 key 互不干扰 (task 招领不抬高 load 负载)。"""
    bus, _ = make_bus()
    bus.deposit("agent-1", 1.0, channel="load")
    bus.deposit("task-7", 1.0, channel="task")
    bus.deposit("task-7", 1.0, channel="result")
    assert bus.level("agent-1", channel="load") == 1.0
    assert bus.level("task-7", channel="task") == 1.0
    assert bus.level("task-7", channel="result") == 1.0
    assert bus.level("task-7", channel="load") == 0.0
    # total_load 只算 load 信道 (兼容 _select_batch 语义)
    assert bus.total_load() == pytest.approx(1.0)
    # 未知信道拒绝
    with pytest.raises(ValueError):
        bus.deposit("x", 1.0, channel="unknown")


def test_lazy_evaporation():
    """惰性蒸发: 读时按半衰期衰减, 低于 decay_to 归零消失。"""
    bus, clock = make_bus(half_life=10.0)
    bus.deposit("w1", 4.0, channel="load")
    clock.advance(10.0)
    assert bus.level("w1") == pytest.approx(2.0)
    clock.advance(20.0)  # 30s = 3 半衰期 → 0.5 < decay_to(0.05)? 4/8=0.5 → 存活
    assert bus.level("w1") == pytest.approx(0.5)
    clock.advance(60.0)  # 远超 → 归零
    assert bus.level("w1") == 0.0
    assert "w1" not in bus.levels()


def test_ttl_expiry_absolute():
    """TTL 绝对寿命: 到期即失效 — 即使强度尚未蒸发殆尽 (防僵尸招领)。"""
    bus, clock = make_bus(half_life=1000.0)  # 蒸发极慢, TTL 主导
    bus.deposit("task-A", 2.0, channel="task", ttl=50.0)
    clock.advance(49.0)
    assert bus.level("task-A", channel="task") > 0.0
    clock.advance(2.0)  # 51s > ttl=50
    assert bus.level("task-A", channel="task") == 0.0
    # load 信道默认无 TTL (旧语义: 只靠蒸发)
    bus.deposit("w2", 2.0, channel="load")
    clock.advance(500.0)
    assert bus.level("w2") > 0.0


def test_legacy_api_compat():
    """PheromoneField 兼容契约: 位置参数 deposit + levels/busy_keys/total_load
    在两种实现上行为一致 (orchestrator 调用点零改动)。"""
    for impl in (PheromoneField(half_life=3600.0), StigmergyBus(half_life=3600.0)):
        impl.deposit("exec-1", 1.0)  # 位置参数 → load 信道
        impl.deposit("exec-2", 1.0)
        assert impl.total_load() == pytest.approx(2.0)
        assert impl.level("exec-1") == pytest.approx(1.0)
        assert set(impl.busy_keys(threshold=1.0)) == {"exec-1", "exec-2"}
        impl.deposit("exec-2", -1.0)  # 撤回
        assert impl.busy_keys(threshold=1.0) == ["exec-1"]
        assert set(impl.levels()) == {"exec-1"}


def test_sqlite_persistence_recovery(tmp_path: Path):
    """场恢复: bus A 写盘 → close → bus B 同路径打开 → 场不丢 (7×24 重启等价)。"""
    db = tmp_path / "pheromone_bus.db"
    bus_a, _ = make_bus(db_path=db, half_life=3600.0)
    bus_a.deposit("exec-1", 3.0)
    bus_a.deposit("task-9", 1.0, channel="task", payload="评审任务招领")
    bus_a.close()

    bus_b, _ = make_bus(db_path=db, half_life=3600.0)
    assert bus_b.level("exec-1") == pytest.approx(3.0)
    assert bus_b.level("task-9", channel="task") == pytest.approx(1.0)
    assert bus_b.payload_of("task-9", channel="task") == "评审任务招领"
    bus_b.close()


def test_payload_roundtrip_and_overwrite():
    """payload 摘要: result 信道产物通知; 续投不传 payload 时保留旧值。"""
    bus, clock = make_bus()
    bus.deposit("r-1", 1.0, channel="result", payload="summary:tests-passed")
    assert bus.payload_of("r-1", channel="result") == "summary:tests-passed"
    bus.deposit("r-1", 1.0, channel="result")  # 续投不带 payload → 保留
    assert bus.payload_of("r-1", channel="result") == "summary:tests-passed"
    bus.deposit("r-1", 1.0, channel="result", payload="summary:v2")
    assert bus.payload_of("r-1", channel="result") == "summary:v2"


def test_top_and_channels_summary():
    """top(k) 与四信道总览 — M2 网格调度 / 7×24 遥测的消费入口。"""
    bus, _ = make_bus()
    bus.deposit("t1", 2.0, channel="task")
    bus.deposit("t2", 5.0, channel="task")
    bus.deposit("w1", 1.0, channel="load")
    assert bus.top("task", k=2) == [("t2", 5.0), ("t1", 2.0)]
    summary = bus.channels_summary()
    assert set(summary) == set(CHANNELS)
    assert summary["task"]["signals"] == 2.0
    assert summary["load"]["signals"] == 1.0


def test_config_switch_rollback():
    """回滚开关: pheromone_bus_enabled=false → 旧 PheromoneField; 其余 → 总线。"""
    assert isinstance(_pheromone_bus_from_config(None), StigmergyBus)
    assert isinstance(_pheromone_bus_from_config({}), StigmergyBus)
    assert isinstance(
        _pheromone_bus_from_config({"pheromone_bus_enabled": True}), StigmergyBus
    )
    assert isinstance(
        _pheromone_bus_from_config({"pheromone_bus_enabled": False}), PheromoneField
    )
    class ObjCfg:
        pheromone_bus_enabled = False

    assert isinstance(_pheromone_bus_from_config(ObjCfg()), PheromoneField)
