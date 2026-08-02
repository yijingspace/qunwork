"""Unified text extraction for the knowledge library.

Routes files by extension: plain-text formats are read directly (with UTF-8 /
BOM / GBK encoding detection); binary formats (.pdf / .docx) are parsed by their
extractor, which lazily imports its library so a missing dependency yields a
readable error instead of a hard crash.
"""

from __future__ import annotations

import codecs
from pathlib import Path

# Formats read straight as text (extension -> label).
_TEXT_EXTS = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".rst": "rst",
    ".csv": "csv",
    ".log": "log",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".ini": "ini",
    ".cfg": "config",
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".htm": "html",
}

# Binary formats handled by a dedicated extractor (extension -> module fn).
_BINARY_EXTS = {
    ".pdf": "extract_pdf",
    ".docx": "extract_docx",
}

_SUPPORTED_EXTS = frozenset(_TEXT_EXTS) | frozenset(_BINARY_EXTS)


class UnsupportedFormatError(ValueError):
    """Raised when a file extension has no extractor."""


class ExtractionError(RuntimeError):
    """Raised when an extractor fails (e.g. missing dependency or corrupt file)."""


def extract_text(path: str | Path) -> tuple[str, str]:
    """Extract readable text from `path`.

    Returns (text, format_label). Raises UnsupportedFormatError for unknown
    extensions and ExtractionError when parsing fails — callers treat both as
    'skip this file' rather than aborting the whole import.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext in _BINARY_EXTS:
        from . import docx as _docx
        from . import pdf as _pdf

        fn = {"extract_pdf": _pdf.extract_pdf, "extract_docx": _docx.extract_docx}[
            _BINARY_EXTS[ext]
        ]
        try:
            return fn(str(p)), ext.lstrip(".")
        except ExtractionError:
            raise
        except Exception as exc:  # any parser failure is an extraction failure
            raise ExtractionError(f"failed to extract {ext} file: {exc}") from exc
    if ext in _TEXT_EXTS:
        return _read_text(p), _TEXT_EXTS[ext]
    raise UnsupportedFormatError(f"unsupported format: {ext or '(none)'}")


def supported_extensions() -> list[str]:
    return sorted(_SUPPORTED_EXTS)


def _read_text(p: Path) -> str:
    """Read a text file with encoding detection: BOM first, then UTF-8, then GBK
    (a pragmatic fallback for Chinese documents), finally lossy UTF-8."""
    raw = p.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[2:].decode("utf-16-be", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")
