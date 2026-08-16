"""S9 记忆跨 worker 实时同步与检索回退 (蜂群审计报告 G3).

契约:
  * add_deduped: 写入 blackboard 前去重 — 相同/高度相似文本不重复 add
    (并行 worker 避免重复探测, 记忆不膨胀);
  * 检索回退: 无 embedder 时 difflib 回退 (已有); 失败不崩溃。
"""

from __future__ import annotations

from coworker.orchestrator.vectormemory import VectorMemory


def test_add_deduped_skips_identical():
    mem = VectorMemory()
    assert mem.add_deduped("部署前先跑测试", task_id="t0") is True
    assert mem.add_deduped("部署前先跑测试", task_id="t1") is False  # 完全相同
    assert len(mem) == 1


def test_add_deduped_skips_near_duplicate():
    mem = VectorMemory()
    assert mem.add_deduped("部署前先跑测试再发布到生产环境") is True
    # 高度相似 (ratio >= 0.92): 几乎一样的文本 + 极小差异
    assert mem.add_deduped("部署前先跑测试再发布到生产环境!") is False
    assert len(mem) == 1


def test_add_deduped_normalizes_whitespace():
    mem = VectorMemory()
    assert mem.add_deduped("部署前  先跑 测试") is True
    assert mem.add_deduped("部署前 先跑 测试") is False  # 空白归一化后相同
    assert len(mem) == 1


def test_add_deduped_keeps_distinct():
    mem = VectorMemory()
    assert mem.add_deduped("部署前先跑测试") is True
    assert mem.add_deduped("重构支付模块的接口") is True  # 不同主题
    assert len(mem) == 2


def test_add_deduped_empty_text():
    mem = VectorMemory()
    assert mem.add_deduped("") is False
    assert len(mem) == 0


def test_search_fallback_without_embedder():
    """无 embedder → difflib 回退, 中文检索可用, 不崩溃。"""
    mem = VectorMemory()
    mem.add("量子计算利用叠加态")
    mem.add("固态电池电解质")
    hits = mem.search("量子 叠加", k=2)
    assert hits and hits[0].text == "量子计算利用叠加态"


def test_search_recovers_from_bad_embedder():
    """embedder 抛异常 → 回退 difflib, 不崩溃 (检索回退)。"""
    def bad_embedder(s):
        raise RuntimeError("embedder down")

    mem = VectorMemory(embedder=bad_embedder)
    mem.add("记忆系统研究")
    hits = mem.search("记忆", k=1)
    assert hits and "记忆" in hits[0].text
