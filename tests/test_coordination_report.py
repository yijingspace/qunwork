"""Benchmark showcase: coordination report rendering + template seeding."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coworker.orchestrator.coordination_report import redact_report, render_coordination_report


def _sample_run() -> dict:
    now = 1700000000.0
    return {
        "run_id": "orch_abc123",
        "intent": "生成全球 AI 市场分析报告",
        "status": "completed",
        "created_at": now,
        "updated_at": now + 123,
        "final": "这是一份完整的市场分析报告正文……",
        "events": [
            {"kind": "run_started", "payload": {"intent": "生成全球 AI 市场分析报告"}},
            {"kind": "plan_ready", "payload": {
                "goal": "生成全球 AI 市场分析报告",
                "tasks": [
                    {"id": "t0", "description": "调研市场格局", "deps": []},
                    {"id": "t1", "description": "分析竞争态势", "deps": ["t0"]},
                    {"id": "t2", "description": "汇编成稿", "deps": ["t0", "t1"]},
                ],
            }},
            {"kind": "task_started", "payload": {"id": "t0", "attempt": 1}},
            {"kind": "worker_thought", "payload": {"task_id": "t0", "text": "先从公开数据梳理市场规模……"}},
            {"kind": "task_result", "payload": {"id": "t0", "result": "市场规模 400 亿美元"}},
            {"kind": "task_review", "payload": {"id": "t0", "accepted": True, "confidence": 0.9, "reason": "数据充分"}},
            {"kind": "task_done", "payload": {"id": "t0", "status": "done", "confidence": 0.9}},
            {"kind": "task_requeue_waiting", "payload": {"id": "t1", "reason": "竞争分析不够深入"}},
            {"kind": "task_requeue_approved", "payload": {"id": "t1"}},
            {"kind": "task_done", "payload": {"id": "t1", "status": "done", "confidence": 0.85}},
            {"kind": "task_done", "payload": {"id": "t2", "status": "done", "confidence": 0.95}},
            {"kind": "governance", "payload": {"step": 1, "action": "WARN", "reason": "漂移", "metrics": {"drift": 0.3}}},
            {"kind": "run_completed", "payload": {"status": "completed", "runs": 6}},
        ],
    }


def test_render_coordination_report_contains_narrative():
    md = render_coordination_report(_sample_run())
    assert "# 协同报告" in md
    assert "生成全球 AI 市场分析报告" in md        # intent
    assert "t0" in md and "t2" in md               # tasks
    assert "调研市场格局" in md                     # task description
    assert "先从公开数据梳理市场规模" in md          # thought chain
    assert "✅ 通过" in md and "❌ 未通过" in md      # verdicts (accepted + requeue)
    assert "WARN" in md and "漂移" in md            # governance
    assert "最终交付" in md and "市场分析报告正文" in md


def test_render_empty_run_graceful():
    md = render_coordination_report({"run_id": "x", "intent": "", "events": []})
    assert "没有解析到任务计划" in md


# -- G5: publishable redacted sample --------------------------------------------------------

_RAW_REPORT = """# 协同报告
## 2. 执行与思维链
### `t0` — 调研市场格局
- **思维链节选**:
  - 读取 /Users/rohit/QunWork/launch-note/README.md 并抄送给 rohit@openworker.com
  - key sk-abcdef0123456789ABCDEF 与 token=deadbeefcafe0123456789abcdef 出现在环境里
  - 参考 https://api.example.com/v1/items?token=supersecret&page=2
- Windows 侧: C:\\Users\\rohit\\QunWork\\notes.md
### `t1` — 竞争分析
- 报告落在 /home/analyst/reports/market.md 与 /tmp/scratch.txt
"""


def test_redact_report_masks_workspace_home_and_identity():
    md = redact_report(
        _RAW_REPORT,
        workspace="/Users/rohit/QunWork/launch-note",
        home="/Users/rohit",
    )
    # Run workspace + home become placeholders (the useful tail survives).
    assert "<workspace>/README.md" in md
    assert "~\\QunWork\\notes.md" in md  # windows profile prefix → ~ (separators kept)
    assert "/Users/rohit" not in md
    assert "<email>" in md
    assert "rohit@openworker.com" not in md
    # Other people's absolute paths collapse to ~ too.
    assert "/home/analyst/reports/market.md" not in md
    assert "~/reports/market.md" in md
    # Credentials: key-shaped token masked, URL query secret masked.
    assert "sk-abcdef0123456789ABCDEF" not in md
    assert "token=[redacted]" in md
    assert "supersecret" not in md
    assert "page=2" in md  # non-secret params survive
    # The sample says it was redacted.
    assert "已脱敏" in md


def test_redact_report_keeps_ordinary_prose_and_paths_intact():
    md = redact_report(
        "任务完成，产出 3 个文件；参考 notes/market.md 与 README.md。",
        workspace=None,
        home=None,
    )
    assert "notes/market.md" in md
    assert "README.md" in md
    assert "产出 3 个文件" in md


def test_benchmark_seed_templates(tmp_path):
    """Seeding writes the three benchmark templates exactly once."""
    from coworker.conversations import ConversationStore

    store = ConversationStore(tmp_path / "data")
    # simulate the manager seeding path
    existing = store.list_swarm_templates()
    if not existing:
        for title, intent in [
            ("市场分析报告 · 协同样板", "用蜂群生成一份全球 AI 市场分析报告"),
            ("代码库重构 · 协同样板", "用蜂群分析当前代码库"),
            ("每周自动化周报 · 协同样板", "用蜂群生成本周工作总结"),
        ]:
            store.add_swarm_template(title, intent, [])
    rows = store.list_swarm_templates()
    assert len(rows) == 3
    assert any("市场分析报告" in r["title"] for r in rows)
