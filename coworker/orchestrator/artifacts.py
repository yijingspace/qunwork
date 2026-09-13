"""Artifact verification for swarm deliverables.

Workers report their deliverable as a markdown link — ``[name](artifact:path)`` —
and that link was taken on faith. On a full volume the write left a **0-byte file**
behind and the run still looked like it had produced something; the owner opened the
"deliverable" and found it blank.

So before a result is reviewed/accepted we resolve every ``artifact:`` reference
against the workspace and say what we found. Problems are (a) emitted as an
``artifact_check`` event the GUI can show, and (b) appended to the text handed to the
reviewer, so the reviewer's verdict sees the same evidence the owner would.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Union

# `[label](artifact:path)` (the shape workers emit) and bare `artifact:path`.
_MD_REF_RE = re.compile(r"\[[^\]]*\]\(artifact:([^)\s]+)\)")
_BARE_REF_RE = re.compile(r"(?<!\()\bartifact:([^\s)\]\"'，。；]+)")

# A "complete" deliverable is tens of KB. Anything this small after a write is a
# strong hint the content never landed (truncated/partial write).
_TINY_BYTES = 512


def artifact_refs(text: str) -> list[str]:
    """Unique ``artifact:`` targets referenced by a result, in first-seen order."""
    if not text:
        return []
    refs: list[str] = []
    for match in _MD_REF_RE.finditer(text):
        refs.append(match.group(1))
    for match in _BARE_REF_RE.finditer(text):
        refs.append(match.group(1))
    seen: set[str] = set()
    ordered: list[str] = []
    for r in refs:
        r = r.strip().strip("`")
        if r and r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


def _resolve(ref: str, workspace: Union[str, Path]) -> Path:
    p = Path(ref)
    # A worker may name an absolute path under the workspace, or a workspace-relative
    # one. Anything else still gets checked (read-only) so the owner learns it isn't
    # where the swarm said it would be.
    return p if p.is_absolute() else Path(workspace) / ref


def verify_artifacts(text: str, workspace: Union[str, Path]) -> list[str]:
    """Problems with the artifacts a result claims, as human-readable lines.

    Empty when the text references nothing (many tasks legitimately produce no file)
    or when every referenced file exists with real content.
    """
    problems: list[str] = []
    for ref in artifact_refs(text):
        target = _resolve(ref, workspace)
        try:
            if not target.exists():
                problems.append(f"{ref}: 产物文件不存在（worker 声称已写入）")
                continue
            if target.is_dir():
                problems.append(f"{ref}: 这是一个目录，不是产物文件")
                continue
            size = target.stat().st_size
        except OSError as exc:
            problems.append(f"{ref}: 无法读取产物（{exc}）")
            continue
        if size == 0:
            problems.append(f"{ref}: 产物为空文件（0 字节）—— 通常意味着写盘失败")
        elif size < _TINY_BYTES:
            problems.append(f"{ref}: 产物异常小（{size} 字节），可能写入被截断")
    return problems


def artifact_warning(problems: Iterable[str]) -> str:
    """The note appended to a result so the reviewer (and the report) sees it."""
    items = list(problems)
    if not items:
        return ""
    lines = ["", "⚠️ 产物落地校验未通过（QunWork 自动核对）:"]
    lines += [f"- {p}" for p in items]
    return "\n".join(lines)
