"""Coordination report — the "swarm narrative" deliverable (dev-plan benchmark
cases). Renders a completed orchestration run's event stream into a human-facing
Markdown report that tells the *teamwork* story: the plan, the DAG, each worker's
execution + thought chain, governance verdicts, convergence, and the final
deliverable. This is the P0/P1 showcase artifact for the three benchmark cases
(market report / code refactor / weekly automation).
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (ValueError, OSError):
        return ""


def _fmt_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _clamp(text: str, n: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def render_coordination_report(run: dict[str, Any]) -> str:
    """Turn a stored run (get_run shape) into a Markdown coordination report."""
    intent = run.get("intent") or "(untitled goal)"
    status = run.get("status") or "unknown"
    final = run.get("final") or ""
    events = run.get("events") or []

    tasks: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    thoughts: list[dict[str, Any]] = []
    governance: list[dict[str, Any]] = []
    t_started = run.get("created_at") or 0.0
    t_finished = run.get("updated_at") or t_started

    for ev in events:
        kind = ev.get("kind")
        p = ev.get("payload") or {}
        if kind == "plan_ready":
            for raw in p.get("tasks") or []:
                tid = str(raw.get("id", ""))
                if tid and tid not in tasks:
                    tasks[tid] = {
                        "id": tid,
                        "description": raw.get("description", ""),
                        "deps": raw.get("deps") or [],
                        "status": "pending",
                        "confidence": 0.0,
                        "result": "",
                        "attempts": 1,
                        "verdicts": [],
                    }
                    order.append(tid)
        elif kind == "task_started":
            t = tasks.get(str(p.get("id", "")))
            if t:
                t["status"] = "running"
                t["attempts"] = int(p.get("attempt", 1) or 1)
        elif kind == "worker_thought":
            thoughts.append(p)
        elif kind == "task_result":
            t = tasks.get(str(p.get("id", "")))
            if t and isinstance(p.get("result"), str):
                t["result"] = p["result"]
        elif kind == "task_review":
            t = tasks.get(str(p.get("id", "")))
            if t:
                t["verdicts"].append(
                    {
                        "accepted": bool(p.get("accepted")),
                        "confidence": float(p.get("confidence") or 0),
                        "reason": p.get("reason", ""),
                    }
                )
        elif kind == "task_done":
            t = tasks.get(str(p.get("id", "")))
            if t:
                t["status"] = p.get("status", t["status"])
                t["confidence"] = float(p.get("confidence") or 0)
        elif kind == "task_requeue_waiting":
            t = tasks.get(str(p.get("id", "")))
            if t:
                t["status"] = "awaiting-deck"
                t["verdicts"].append(
                    {"accepted": False, "confidence": 0.0, "reason": p.get("reason", "")}
                )
        elif kind == "governance":
            governance.append(p)

    lines: list[str] = []
    lines.append("# 协同报告 · Swarm Coordination Report")
    lines.append("")
    lines.append(f"- **目标 (Intent)**: {intent}")
    lines.append(f"- **状态 (Status)**: {status}")
    lines.append(f"- **耗时 (Duration)**: {_fmt_duration(max(0.0, t_finished - t_started))}")
    lines.append(f"- **任务数 (Tasks)**: {len(order)}")
    lines.append(f"- **运行时间**: {_fmt_ts(t_started)} → {_fmt_ts(t_finished)}")
    lines.append("")

    # 1. plan / DAG
    lines.append("## 1. 任务计划 (Plan / DAG)")
    lines.append("")
    if order:
        lines.append("| 任务 | 描述 | 依赖 | 状态 | 置信度 |")
        lines.append("|---|---|---|---|---|")
        for tid in order:
            t = tasks[tid]
            deps = ", ".join(t["deps"]) or "—"
            lines.append(
                f"| `{t['id']}` | {_clamp(t['description'], 60)} | {deps} | "
                f"{t['status']} | {t['confidence']:.2f} |"
            )
    else:
        lines.append("_没有解析到任务计划。_")
    lines.append("")

    # 2. worker execution + thought chains
    lines.append("## 2. 执行与思维链 (Execution & Thought Chains)")
    lines.append("")
    thoughts_by_task: dict[str, list[str]] = {}
    for th in thoughts:
        tid = str(th.get("task_id", ""))
        text = str(th.get("text", "")).strip()
        if text:
            thoughts_by_task.setdefault(tid, []).append(text)
    for tid in order:
        t = tasks[tid]
        lines.append(f"### `{tid}` — {_clamp(t['description'], 80)}")
        lines.append("")
        if t["verdicts"]:
            v = t["verdicts"][-1]
            verdict_line = "✅ 通过" if v["accepted"] else "❌ 未通过"
            lines.append(f"- **评审**: {verdict_line} (置信度 {v['confidence']:.2f}) — {_clamp(v['reason'], 100)}")
        if t["attempts"] > 1:
            lines.append(f"- **尝试次数**: {t['attempts']}")
        if thoughts_by_task.get(tid):
            lines.append("- **思维链节选**:")
            for frag in thoughts_by_task[tid][:2]:
                lines.append(f"  - {_clamp(frag, 200)}")
        if t["result"]:
            lines.append("- **产出节选**:")
            lines.append(f"  ```\n  {_clamp(t['result'], 300)}\n  ```")
        lines.append("")
    if not order:
        lines.append("_本 run 没有任务执行记录。_")
        lines.append("")

    # 3. governance
    lines.append("## 3. 治理记录 (Governance)")
    lines.append("")
    if governance:
        lines.append("| 步骤 | 动作 | 原因 | 指标 |")
        lines.append("|---|---|---|---|")
        for g in governance:
            metrics = g.get("metrics") or {}
            metrics_txt = ", ".join(f"{k}={v}" for k, v in metrics.items()) or "—"
            lines.append(
                f"| {g.get('step', '')} | {g.get('action', '')} | "
                f"{_clamp(str(g.get('reason', '')), 80)} | {_clamp(metrics_txt, 80)} |"
            )
    else:
        lines.append("_本次运行没有触发治理指令。_")
    lines.append("")

    # 4. final deliverable
    lines.append("## 4. 最终交付 (Final Deliverable)")
    lines.append("")
    if final:
        lines.append(_clamp(final, 2000))
    else:
        lines.append("_运行未产出最终交付内容。_")
    lines.append("")
    lines.append("---")
    lines.append(f"_由 QunWork 蜂群协同生成 · {_fmt_ts(t_finished)}_")
    return "\n".join(lines)


def coordination_report_summary(run: dict[str, Any]) -> dict[str, Any]:
    """Machine-readable summary used by the API (`ok/status/duration/tasks/…`)."""
    events = run.get("events") or []
    t_started = run.get("created_at") or 0.0
    t_finished = run.get("updated_at") or t_started
    return {
        "ok": True,
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "intent": run.get("intent"),
        "duration_s": max(0.0, t_finished - t_started),
        "event_count": len(events),
    }


# -- Public-sample redaction ---------------------------------------------------------------
# A report is deeply personal: workspace paths, home directories, mail addresses, ticket
# links and the occasional key that leaked into a task result. Publishing a sample must not
# publish any of that, so the export runs the body through a fixed rule set and says so.

_REDACT_NOTE = "> 本样例已脱敏：路径、邮箱、URL 凭据与疑似密钥均已替换。"
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_USER_DIR_RE = re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/)([^\\/\s\"'`]+)")
# Key-prefixed credentials (OpenAI/Slack/GitHub/Google/AWS) — keep the prefix, mask the body.
_KEY_RE = re.compile(
    r"\b(?:sk|xoxb|xoxp|xapp|ghp|gho|github_pat_|glpat|AKIA|AIza|hf)[-_A-Za-z0-9]{8,}\b"
)
# A long unbroken token is presumed secret unless it is obviously a path or a slug.
_OPAQUE_RE = re.compile(r"\b(?![A-Za-z0-9_-]*\.(?:md|py|json|ts|tsx|js|html|txt)\b)[A-Za-z0-9_\-]{32,}\b")


def redact_report(
    markdown: str,
    *,
    workspace: Optional[str] = None,
    home: Optional[str] = None,
) -> str:
    """Return a publishable copy of a coordination report.

    Rule order matters. The run's own workspace goes first (so ``<workspace>/notes.md`` keeps
    its useful tail while hiding the project path), then ANY remaining per-user prefix
    (``/Users/<name>``, ``/home/<name>``, ``C:\\Users\\<name>``) collapses to ``~``, then the
    configured home — which may live somewhere else entirely. After that come emails, URL
    credentials, key-shaped tokens and long opaque tokens. Prose and relative paths are left
    alone: the point is a real sample, not a rewrite.
    """
    from ..audit import _redact_url_secrets

    if not markdown:
        return markdown

    def _swap(text: str, root: Optional[str], token: str) -> str:
        if not root:
            return text
        for variant in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
            if variant:
                text = text.replace(variant, token)
        return text

    text = _swap(markdown, workspace, "<workspace>")
    # Windows profile dirs must be handled before the bare `/Users/` forms, otherwise a
    # leftover drive prefix (`C:~\…`) survives.
    text = _USER_DIR_RE.sub("~", text)
    text = _swap(text, home, "~")
    text = _EMAIL_RE.sub("<email>", text)
    text = _KEY_RE.sub("<redacted>", text)
    text = _OPAQUE_RE.sub("<redacted>", text)
    text = _redact_url_secrets(text)
    return f"{_REDACT_NOTE}\n\n{text}"
