"""DOCX text extraction for the knowledge library.

python-docx (paragraphs + tables, document order; nested tables joined with
' | '). Lazy import: a missing dependency raises a readable ExtractionError.
"""

from __future__ import annotations

from pathlib import Path

from . import ExtractionError


def extract_docx(path: str | Path) -> str:
    """Extract paragraphs and table cells from a .docx in document order."""
    try:
        import docx  # python-docx
    except ImportError as exc:  # pragma: no cover - env dependent
        raise ExtractionError(
            "DOCX support requires 'python-docx' — install it (pip install "
            "python-docx) to import Word documents into the knowledge library"
        ) from exc

    p = Path(path)
    try:
        document = docx.Document(str(p))
    except Exception as exc:
        raise ExtractionError(f"cannot open DOCX {p.name}: {exc}") from exc

    parts: list[str] = []

    def visit_table(table) -> None:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                text = cell.text.strip()
                if text:
                    cells.append(text)
            if cells:
                parts.append(" | ".join(cells))

    # Iterate the body in document order: paragraphs and tables interleave.
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            # paragraph
            text = child.text or ""
            # gather runs (keeps it simple: concatenate direct text)
            for sub in child.iter():
                if sub.text:
                    text += sub.text
            if text.strip():
                parts.append(text.strip())
        elif child.tag.endswith("}tbl"):
            try:
                # map the XML table back to a Table object via the part
                from docx.table import Table

                visit_table(Table(child, document))
            except Exception:
                pass

    result = "\n".join(parts)
    if not result.strip():
        raise ExtractionError(f"DOCX {p.name} contains no extractable text")
    return result
