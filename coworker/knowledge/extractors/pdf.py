"""PDF text extraction for the knowledge library.

Uses pypdf (pure Python, BSD — the same library as coworker/pdf_support.py).
PyMuPDF is deliberately NOT used: its AGPL license can't ride in the desktop
installer. pypdf is a lazy import so a missing dependency raises a readable
ExtractionError instead of crashing the import pipeline.
"""

from __future__ import annotations

from pathlib import Path

from . import ExtractionError


def extract_pdf(path: str | Path) -> str:
    """Extract text from a PDF page by page (blank lines between pages)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - env dependent
        raise ExtractionError(
            "PDF support requires 'pypdf' — install it (pip install pypdf) to "
            "import PDF files into the knowledge library"
        ) from exc

    p = Path(path)
    try:
        reader = PdfReader(str(p))
    except Exception as exc:
        raise ExtractionError(f"cannot open PDF {p.name}: {exc}") from exc

    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # a bad page shouldn't kill the whole document
            text = ""
        if text.strip():
            parts.append(text.strip())
    result = "\n\n".join(parts)
    if not result.strip():
        raise ExtractionError(f"PDF {p.name} contains no extractable text (scanned/image-only?)")
    return result
