"""Orchestration data model for QunWork multi-agent loop cooperation (Phase 1).

Mirrors the LoopCoop task-queue Q / structured-state S_t concepts in the research
docs, kept minimal for the MVP: a task DAG (list + deps), per-task status/result,
and a run-level summary. No vector memory / governance loop yet (Phase 2+).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Process/planning utterances that must never be shipped as the deliverable.
_PROCESS_MARKERS = (
    "正在",
    "即将",
    "我准备",
    "现在拼接",
    "接下来",
    "然后",
    "用 shell",
    "用shell",
    "核验",
    "确认",
    "开始写",
    "准备写",
    "将写入",
    "计划",
    "Let me",
    "I will",
    "Now",
    "checking",
    "verifying",
)


def _is_process_text(text: str) -> bool:
    """Heuristic: is this a process/plan sentence rather than a finished product?"""
    t = text.strip()
    if t.startswith("⚠ task timed out"):
        return True
    if len(t) >= 300:  # long outputs are almost certainly real content
        return False
    return any(m in t for m in _PROCESS_MARKERS)


# Meta/shell lines an executor may wrap around the real product (delivery headers,
# character-count notes, verification notes). Never shipped.
_SECTION_RE = re.compile(r"^===\s*SECTION\s*:\s*(.+?)\s*=== *$", re.M)


def _assemble_sections(products: list["Task"]) -> Optional[str]:
    """S4 蜂群结构化结果协议: 多个产物若带 ===SECTION:<标题>=== 章节标记,
    按章节自动聚合 (相同章节合并去重), 无标记时返回 None (走原逻辑)。

    协议: worker 章节产出以 `===SECTION: 标题===` 开头标记所属章节,
    后续行是该章节内容。多个 worker 写同一章节 → 内容合并。
    """
    sections: list[tuple[str, list[str]]] = []
    index: dict[str, int] = {}
    found = False
    for t in products:
        text = (t.result or "").strip()
        if not text:
            continue
        lines = text.splitlines()
        if not lines or not _SECTION_RE.match(lines[0]):
            continue
        found = True
        m = _SECTION_RE.match(lines[0])
        title = m.group(1).strip()
        body = "\n".join(lines[1:]).strip()
        if title in index:
            sections[index[title]][1].append(body)
        else:
            index[title] = len(sections)
            sections.append((title, [body]))
    if not found:
        return None
    out = []
    for title, bodies in sections:
        out.append(f"## {title}")
        out.append("\n\n".join(b for b in bodies if b))
    return "\n\n".join(out)


# Meta/shell lines an executor may wrap around the real product (delivery headers,
# character-count notes, verification notes). Never shipped.
_TAIL_SHELL = (
    "核对结果",
    "全文共",
    "以上为",
    "以下为",
    "共 ",
    "字符数",
    "字数",
    "来源标注",
    "已写入",
    "已保存",
    "草稿文件",
    "读回核验",
    "验收口径",
    "衔接说明",
    "内容要素",
    "定义要素",
    "独立完整",
    "纯成品正文",
    "草稿已保存",
)


def clean_deliverable(text: str) -> str:
    """Strip the delivery shell an executor may wrap around the product:
    a '**Task [t0] 交付…**' header, artifact links, and trailing meta lines
    (字符数/核对结果/已写入…), leaving the pure body text.

    无损原则 (S3 修复): 只删除*包装壳* (任务头/尾注/artifact 链接), 绝不
    删除正文内容 — 特别是 Markdown 的列表项 (`- `) / 加粗 (`**`) / 标题,
    那是报告的正文, 不是 meta。此前把 `- `/`**` 开头的行当 meta 删除,
    导致蜂群报告 (大量列表/加粗) 从上万字被砍到几千字 — 产物有损。
    """
    import re

    t = re.sub(
        r"^\**\s*Task\s*\[[^\]]*\]\s*[^*\n]*\**\s*\n",
        "",
        text,
    )
    # artifact/file links -> bare text
    t = re.sub(r"\[([^\]]*)\]\((?:artifact|file|attachment):[^)]*\)", r"\1", t)
    # drop markdown blockquotes around the body (" > 正文")
    t = re.sub(r"(?m)^\s*>\s?", "", t)
    lines = []
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        # 删除包装壳行, 保留正文 (无损原则):
        #  1) 短行且含 _TAIL_SHELL 标记 → 尾注 (字数/核对/已写入/衔接说明);
        #  2) bullet 行 (`- `/`* `) 若其内容也是纯 shell 说明 (短 + 含 shell
        #     标记) → 视为交付说明删除; 否则是正文列表项, 必须保留。
        # 长行 (>40) 一律视为正文, 永不删除 (报告正文/列表项/表格行)。
        stripped = s.lstrip("-* ")
        is_bullet = s[:1] in ("-", "*")
        shellish = any(m in s for m in _TAIL_SHELL) and len(s) <= 40
        if shellish and (not is_bullet or len(stripped) <= 40):
            continue
        lines.append(s)
    return "\n".join(lines).strip()


@dataclass
class Task:
    """A single unit of work in the orchestrated plan (a node in the task DAG)."""

    id: str
    description: str
    deps: list[str] = field(default_factory=list)
    # P0 建议3: executor role override — empty means "use the run's executor_agent"
    # (the global choice). The command deck's retarget action sets it at runtime.
    agent: str = ""
    # pending | running | done | needs_human
    status: str = "pending"
    result: str = ""
    retries: int = 0
    confidence: float = 0.0

    @property
    def done(self) -> bool:
        return self.status == "done"

    def ready(self, by_id: dict[str, "Task"]) -> bool:
        """A task is ready when pending and every dependency is done."""
        if self.status != "pending":
            return False
        return all(by_id.get(d) is not None and by_id[d].done for d in self.deps)


@dataclass
class Plan:
    """The parsed task plan produced by the planner worker."""

    goal: str
    tasks: list[Task] = field(default_factory=list)

    def by_id(self) -> dict[str, Task]:
        return {t.id: t for t in self.tasks}

    def all_done(self) -> bool:
        return all(t.done for t in self.tasks)

    def needs_human(self) -> bool:
        return any(t.status == "needs_human" for t in self.tasks)


@dataclass
class ReviewVerdict:
    """Structured verdict from the reviewer worker (validation gate)."""

    accepted: bool
    reason: str = ""
    confidence: float = 0.0
    needs_human: bool = False


@dataclass
class OrchestrationResult:
    """Final outcome of an orchestrated run."""

    intent: str
    plan: Plan
    summary: str = ""
    status: str = "completed"  # completed | needs_human | paused | failed
    runs: int = 0
    governance_report: str = ""  # health metrics + governance commands, if any
    report_path: str = ""  # file the assembled report was written to (if any)

    def final_report(self) -> str:
        """The finished deliverable — with process-text filtering: a timed-out
        consolidator's last message is often a plan sentence ("now stitching…"),
        which must never be shipped as the deliverable. Real product fragments
        are stitched instead. Delivery shells (headers/notes) are stripped."""
        done = [t for t in self.plan.tasks if t.done and t.result]
        if not done:
            return clean_deliverable(self.summary)
        products = [t for t in done if not _is_process_text(t.result)]
        if not products:
            # every result is process text / placeholders — keep any real fragments
            real = [t.result for t in done if not t.result.startswith("⚠ task timed out")]
            return clean_deliverable("\n\n".join(real) if real else done[-1].result)
        if len(products) == 1:
            return clean_deliverable(products[0].result)
        # S4 蜂群结构化结果协议: 若产物带 ===SECTION:<标题>=== 章节标记,
        # 按章节自动聚合 (相同章节合并去重), 避免拼接错乱/重复标题。
        assembled = _assemble_sections(products)
        if assembled is not None:
            return clean_deliverable(assembled)
        # consolidation task normally carries the full report; if it is short or
        # process-like, stitch the product fragments into the deliverable instead
        last = products[-1]
        if len(last.result) >= 200:
            return clean_deliverable(last.result)
        # A timed-out task's trailing thought (short, non-deliverable) must not be
        # stitched in alongside the real chapter bodies. Keep the substantial
        # products; fall back to everything only if none clears the bar.
        substantial = [t for t in products if len((t.result or "").strip()) >= 200]
        return clean_deliverable("\n\n".join(t.result for t in (substantial or products)))

    def task_report(self) -> str:
        lines = [f"Goal: {self.plan.goal}"]
        for t in self.plan.tasks:
            mark = "✓" if t.done else ("⚠" if t.status == "needs_human" else "✗")
            conf = f" (confidence {t.confidence:.2f})" if t.confidence else ""
            lines.append(f"{mark} [{t.id}] {t.description}{conf}")
            if t.result:
                lines.append(f"    {t.result[:400]}")
        return "\n".join(lines)
