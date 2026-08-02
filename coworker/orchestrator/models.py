"""Orchestration data model for QunWork multi-agent loop cooperation (Phase 1).

Mirrors the LoopCoop task-queue Q / structured-state S_t concepts in the research
docs, kept minimal for the MVP: a task DAG (list + deps), per-task status/result,
and a run-level summary. No vector memory / governance loop yet (Phase 2+).
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    (字符数/核对结果/已写入…), leaving the pure body text."""
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
        if any(m in s for m in _TAIL_SHELL) and len(s) < 160:
            continue
        if s.startswith(("- ", "* ", "**")):
            continue  # bullet/list/emphasis meta lines
        lines.append(s)
    return "\n".join(lines).strip()


@dataclass
class Task:
    """A single unit of work in the orchestrated plan (a node in the task DAG)."""

    id: str
    description: str
    deps: list[str] = field(default_factory=list)
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
        # consolidation task normally carries the full report; if it is short or
        # process-like, stitch the product fragments into the deliverable instead
        last = products[-1]
        if len(last.result) >= 200:
            return clean_deliverable(last.result)
        return clean_deliverable("\n\n".join(t.result for t in products))

    def task_report(self) -> str:
        lines = [f"Goal: {self.plan.goal}"]
        for t in self.plan.tasks:
            mark = "✓" if t.done else ("⚠" if t.status == "needs_human" else "✗")
            conf = f" (confidence {t.confidence:.2f})" if t.confidence else ""
            lines.append(f"{mark} [{t.id}] {t.description}{conf}")
            if t.result:
                lines.append(f"    {t.result[:400]}")
        return "\n".join(lines)
