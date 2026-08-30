"""Line-numbered file reading (`read_file`) — replaces the aisuite toolkit's reader.

The toolkit's `read_file` returns raw text (the agent can't cite path:line without
counting) and raises outright on large files (the agent errors and guesses). This one
returns `cat -n`-style numbered lines, windows big files instead of failing, and tells
the agent how to continue reading. Read-only, workspace-scoped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aisuite as ai

_DEFAULT_MAX_LINES = 2000
_MAX_LINE_CHARS = 500

_ESCAPE_HINT = (
    "path escapes the workspace roots — use request_directory to gain access, "
    "or run_shell to read/copy outside paths"
)

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file, returning numbered lines ('   12\\ttext') so code can be "
            "referenced as path:line. Large files are windowed: pass start_line to continue "
            "where the previous read stopped. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, relative to the workspace.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, 1-based (default 1).",
                },
                "max_lines": {
                    "type": "integer",
                    "description": f"How many lines (default {_DEFAULT_MAX_LINES}).",
                },
            },
            "required": ["path"],
        },
    },
}


def file_tools(workspace: str, roots: Any = None) -> list:
    """Line-numbered/windowed file tools. `roots` (optional RootDir/str list, per
    coworker.roots) adds extra readable roots so multi-root sessions keep the safe,
    windowed reader instead of aisuite's exception-raising native one — the native
    `read_file` raises ValueError on outside paths and files >200KB, which the swarm
    surfaced as a repeated tool-failure mode (0.21.0 desktop run)."""
    root = Path(workspace).resolve()
    extra: list[Path] = []
    for r in roots or []:
        p = getattr(r, "path", r)  # RootDir has .path; str/Path pass through
        try:
            rp = Path(p).resolve()
        except (TypeError, ValueError, OSError):
            continue
        if rp != root and rp not in extra:
            extra.append(rp)

    def _within(candidate: Path, base: Path) -> bool:
        try:
            candidate.relative_to(base)
            return True
        except ValueError:
            return False

    def _resolve(path: str) -> Path | None:
        """Resolve `path` against the roots. Absolute paths must land inside a root
        (existence checked by the caller); relative paths hit the first root where
        the file actually exists (primary first, then extras) — a missing file in
        the primary must not shadow a real one in an extra root."""
        p = Path(path)
        if p.is_absolute():
            target = p.resolve()
            return target if _within(target, root) or any(_within(target, r) for r in extra) else None
        for base in [root] + extra:
            candidate = (base / p).resolve()
            if _within(candidate, base) and candidate.is_file():
                return candidate
        # No existing hit anywhere: fall back to the primary-root candidate so the
        # caller's "not a file" error names the expected location — but only when
        # that candidate stays inside the root (`../x` traversal must stay an
        # escape error, never a "not a file").
        fallback = (root / p).resolve()
        return fallback if _within(fallback, root) else None

    def _read_windowed(target: Path, path: str, start: int, n: int) -> dict[str, Any]:
        selected: list[str] = []
        total = 0
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    total = i
                    if i < start or len(selected) >= n:
                        continue
                    text = line.rstrip("\n")
                    if len(text) > _MAX_LINE_CHARS:
                        text = text[:_MAX_LINE_CHARS] + "… (line truncated)"
                    selected.append(f"{i:>6}\t{text}")
        except OSError as exc:
            return {"error": f"read failed: {exc}"}

        end = start + len(selected) - 1 if selected else start - 1
        try:
            shown = str(target.relative_to(root))
        except ValueError:
            shown = path
        result: dict[str, Any] = {
            "path": shown,
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "content": "\n".join(selected),
        }
        if end < total:
            result["note"] = (
                f"showing lines {start}-{end} of {total}; "
                f"call again with start_line={end + 1} to continue"
            )
        return result

    def read_file(
        path: str,
        start_line: int = 1,
        max_lines: int = _DEFAULT_MAX_LINES,
    ) -> dict[str, Any]:
        start = start_line if isinstance(start_line, int) and start_line > 0 else 1
        n = (
            max_lines
            if isinstance(max_lines, int) and max_lines > 0
            else _DEFAULT_MAX_LINES
        )
        n = min(n, _DEFAULT_MAX_LINES)
        target = _resolve(path)
        if target is None:
            return {"error": _ESCAPE_HINT}
        if not target.is_file():
            return {"error": f"not a file: {path}"}
        return _read_windowed(target, path, start, n)

    def read_file_lines(
        path: str,
        start_line: int = 1,
        max_lines: int = 100,
    ) -> dict[str, Any]:
        """Compatibility alias of read_file (same windowed behaviour) — keeps the
        tool name the models already know from the native toolkit."""
        return read_file(path, start_line=start_line, max_lines=max_lines)

    read_file.__name__ = "read_file"
    read_file.__doc__ = _SCHEMA["function"]["description"]
    read_file.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="read_file",
        category="filesystem",
        risk_level="low",
        capabilities=["read"],
        requires_approval=False,
    )
    read_file.__coworker_schema__ = _SCHEMA

    read_file_lines.__name__ = "read_file_lines"
    read_file_lines.__aisuite_tool_metadata__ = read_file.__aisuite_tool_metadata__
    read_file_lines.__coworker_schema__ = {
        "type": "function",
        "function": {
            "name": "read_file_lines",
            "description": (
                "Read a line range from a text file (numbered lines). Same behaviour "
                "and windows as read_file; kept as a separate name for compatibility."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, relative to the workspace.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read, 1-based (default 1).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "How many lines (default 100).",
                    },
                },
                "required": ["path"],
            },
        },
    }
    return [read_file, read_file_lines]
