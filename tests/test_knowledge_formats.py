"""Multi-format knowledge import tests — extract_text dispatch, pdf/docx indexing,
search hits, API end-to-end (minimal in-memory fixtures)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from coworker.knowledge.extractors import (
    UnsupportedFormatError,
    extract_text,
    supported_extensions,
)
from coworker.knowledge.store import KnowledgeStore


# A real minimal one-page PDF (pypdf-generated, Helvetica, ASCII text) frozen as
# base64 so the test needs no PDF-writing dependency.
_PDF_FIXTURE_B64 = (
    "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAw"
    "IG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagoz"
    "IDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8"
    "PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Ci9Gb250IDw8Ci9GMSA8PAovVHlwZSAvRm9udAov"
    "U3VidHlwZSAvVHlwZTEKL0Jhc2VGb250IC9IZWx2ZXRpY2EKPj4KPj4KPj4KL01lZGlhQm94IFsg"
    "MC4wIDAuMCA2MTIgNzkyIF0KL1BhcmVudCAyIDAgUgovQ29udGVudHMgPDwKL0xlbmd0aCA1Nwo+"
    "PgpzdHJlYW0KQlQgL0YxIDEyIFRmIDcyIDcyMCBUZCAoUERGIGtub3dsZWRnZSBpbXBvcnQgd29y"
    "a3MpIFRqIEVUCmVuZHN0cmVhbQo+PgplbmRvYmoKeHJlZgowIDUKMDAwMDAwMDAwMCA2NTUzNSBm"
    "IAowMDAwMDAwMDE1IDAwMDAwIG4gCjAwMDAwMDAwNTQgMDAwMDAgbiAKMDAwMDAwMDExMyAwMDAw"
    "MCBuIAowMDAwMDAwMTYyIDAwMDAwIG4gCnRyYWlsZXIKPDwKL1NpemUgNQovUm9vdCAzIDAgUgov"
    "SW5mbyAxIDAgUgo+PgpzdGFydHhyZWYKNDI5CiUlRU9GCg=="
)


def _make_pdf_bytes(text: str) -> bytes:
    """Return the frozen minimal PDF (content is fixed 'PDF knowledge import works';
    `text` is accepted for readability but the fixture content is ASCII-only)."""
    import base64

    return base64.b64decode(_PDF_FIXTURE_B64)


def _make_docx_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


# -- extract_text dispatch ---------------------------------------------------


def test_extract_text_text_formats_and_encodings(tmp_path: Path):
    md = tmp_path / "a.md"
    md.write_text("固态电池说明文档", encoding="utf-8")
    text, fmt = extract_text(md)
    assert "固态电池" in text and fmt == "markdown"

    gbk = tmp_path / "b.txt"
    gbk.write_bytes("中文GBK内容".encode("gbk"))
    assert "中文GBK" in extract_text(gbk)[0]

    bom = tmp_path / "c.txt"
    bom.write_bytes(b"\xef\xbb\xbf" + "UTF-8 BOM 内容".encode("utf-8"))
    assert "UTF-8 BOM" in extract_text(bom)[0]

    (tmp_path / "d.json").write_text('{"k": 1}', encoding="utf-8")
    assert extract_text(tmp_path / "d.json")[1] == "json"


def test_extract_text_unsupported_format(tmp_path: Path):
    bad = tmp_path / "x.xyz"
    bad.write_text("hi", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        extract_text(bad)


def test_extract_pdf_roundtrip(tmp_path: Path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_pdf_bytes("PDF knowledge import works"))
    text, fmt = extract_text(pdf)
    assert fmt == "pdf"
    assert "knowledge import" in text


def test_extract_docx_roundtrip(tmp_path: Path):
    docx = tmp_path / "doc.docx"
    docx.write_bytes(_make_docx_bytes("DOCX 中的知识文件库段落"))
    text, fmt = extract_text(docx)
    assert fmt == "docx"
    assert "知识文件库" in text


def test_supported_extensions_listed():
    exts = set(supported_extensions())
    assert {".pdf", ".docx", ".md", ".txt"} <= exts


# -- store integration -------------------------------------------------------


def test_index_file_pdf_docx_and_search(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))

    (tmp_path / "p.pdf").write_bytes(_make_pdf_bytes("PDF format support verified"))
    (tmp_path / "d.docx").write_bytes(_make_docx_bytes("DOCX 支持验证：知识库"))
    assert store.index_file(tmp_path / "p.pdf", workspace=str(ws)) is not None
    assert store.index_file(tmp_path / "d.docx", workspace=str(ws)) is not None

    # both formats are indexed (sources carry the right extensions)
    paths = {it["source_path"] for it in store.list_items(workspace=str(ws))}
    assert any(p.endswith(".pdf") for p in paths)
    assert any(p.endswith(".docx") for p in paths)

    # docx (Chinese) hits; pdf content is present in the store
    hits2 = store.search("知识库", k=5, workspace=str(ws))
    assert any("DOCX 支持验证" in h["content"] for h in hits2)
    all_chunks = [
        h["content"]
        for q in ("knowledge", "import")
        for h in store.search(q, k=5, workspace=str(ws), min_score=0.01)
    ]
    assert any("PDF knowledge import works" in c for c in all_chunks)


def test_index_file_skips_corrupt_pdf_without_aborting(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really a pdf")
    assert store.index_file(corrupt, workspace=str(ws)) is None  # skipped, no crash
    assert store.list_items(workspace=str(ws)) == []
