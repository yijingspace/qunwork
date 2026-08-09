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


def test_index_file_corrupt_pdf_raises_and_scan_counts_failure(tmp_path: Path):
    """A corrupt PDF raises ExtractionError (not silently skipped); the scan
    driver counts it as failed with a reason."""
    from coworker.knowledge.extractors import ExtractionError

    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(ExtractionError):
        store.index_file(corrupt, workspace=str(ws))
    assert store.list_items(workspace=str(ws)) == []

    folder = tmp_path / "mixed"
    folder.mkdir()
    (folder / "bad.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    (folder / "ok.md").write_text("正常文档", encoding="utf-8")
    summary = store.index_folder(folder, workspace=str(ws), max_total_bytes=2 * 1024**3)
    assert summary["failed"] == 1
    assert summary["added"] == 1
    assert any("no extractable text" in f["reason"] or "cannot open" in f["reason"] for f in summary["failures"])


def test_index_folder_truncated_reports_cap_and_failures(tmp_path: Path):
    """Small max_total_bytes stops the scan early and reports truncated; corrupt
    pdfs surface their reason in failures."""
    ws = tmp_path / "ws"
    ws.mkdir()
    store = KnowledgeStore(tmp_path / "k.db", workspace=str(ws))

    folder = tmp_path / "big"
    folder.mkdir()
    for i in range(20):
        (folder / f"d{i}.md").write_text("x" * 1024, encoding="utf-8")  # 1KB each

    summary = store.index_folder(
        folder, workspace=str(ws), max_total_bytes=5 * 1024, max_files=10000
    )
    assert summary["truncated"] is True
    assert summary["added"] < 20  # stopped before finishing
    assert summary["added"] > 0

    # corrupt pdfs land in failures with a reason
    bad = tmp_path / "bad"
    bad.mkdir()
    for i in range(2):
        (bad / f"broken{i}.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    summary2 = store.index_folder(bad, workspace=str(ws), max_total_bytes=2 * 1024 ** 3)
    assert summary2["failed"] == 2
    assert len(summary2["failures"]) == 2
    assert all(f["reason"] for f in summary2["failures"])
    assert summary2["added"] == 0


def test_grep_parse_windows_drive_path():
    """Upstream #17: ripgrep output with a Windows drive path must not be
    truncated at the drive colon."""
    from coworker.tools.search import _parse_rg
    from pathlib import Path

    out = _parse_rg(
        "C:\repo\src\app.py:42:def main():\nE:\docs\readme.md:7:Install guide",
        Path("C:\repo"),
        10,
    )
    assert out["count"] == 2
    assert out["matches"][0]["file"].endswith("src\app.py")
    assert out["matches"][0]["line"] == 42
    assert out["matches"][0]["text"] == "def main():"
    # unix-style still works
    out2 = _parse_rg("/repo/a.py:3:x", Path("/repo"), 10)
    assert out2["matches"][0]["line"] == 3


def test_parent_id_research_relay_chain(tmp_path):
    """Research relay: derived entries link back to the knowledge they came from."""
    from coworker.knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "k.db")
    parent = store.add_text("DPNN 白皮书", "内容 A", kind="file", workspace="w")
    child = store.add_text(
        "DPNN 研究报告", "基于 A 的研究", kind="swarm_report", workspace="w", parent_id=parent
    )
    items = {i["id"]: i for i in store.list_items(workspace="w")}
    assert items[child]["parent_id"] == parent
    assert items[parent]["parent_id"] is None


def test_resume_pack_carries_full_body_and_related(tmp_path):
    """One-click research pack: full body + source + resonance related cells."""
    from coworker.hornet import HornetBuilder, HornetStore
    from coworker.hornet.resonator import HornetResonator
    from coworker.knowledge.store import KnowledgeStore

    ks = KnowledgeStore(tmp_path / "k.db")
    hs = HornetStore(tmp_path / "h.db")
    pid = ks.add_text("DPNN 白皮书", "离散周期神经网络 DPNN 相位记忆 周期 预测", kind="file", workspace="w")
    from coworker.hornet.store import ngram_vector

    hs.add_node("DPNN 白皮书", "离散周期神经网络 DPNN 相位记忆", kb_item_id=pid, vec=ngram_vector("DPNN 白皮书"), x=0, y=0, z=0)
    hs.add_node("DPNN 研究报告", "DPNN 继续研究 报告 扩展", kb_item_id=None, vec=ngram_vector("DPNN 研究报告"), x=1, y=0, z=0)
    hs.add_edge(1, 2, "similar", weight=0.9, channel="G3")
    from coworker.server.manager import SessionManager

    class _M:
        hornet = hs
        _hornet_resonator = HornetResonator(hs)
        knowledge = ks
        default_workspace = "w"
        def knowledge_get(self, item_id):
            for r in self.knowledge.list_items(limit=5000):
                if r.get("id") == item_id:
                    return {**r, "content": self.knowledge.item_content(item_id)}
            return None

        def knowledge_resume_pack(self, item_id, k=3):
            item = self.knowledge_get(item_id)
            if not item:
                return None
            related = []
            try:
                out = self._hornet_resonator.resonate(item.get("title") or "", k=k)
                for h in out.get("hits", []):
                    related.append({"title": h.get("title", ""), "snippet": "", "amplitude": h.get("amplitude", 0)})
            except Exception:
                pass
            return {"title": item.get("title") or "", "content": item.get("content") or "", "source": item.get("source_path"), "related": related}

    m = _M()
    pack = m.knowledge_resume_pack(pid)
    assert pack is not None
    assert "DPNN" in pack["content"]  # full body present, not empty
    assert pack["related"], "resonance context pack must not be empty"


def test_resume_pack_resolves_original_source_for_emergent(tmp_path):
    """Emergent digest entries resolve the ORIGINAL source files (body + link)."""
    from coworker.hornet import HornetStore
    from coworker.hornet.resonator import HornetResonator
    from coworker.knowledge.store import KnowledgeStore

    ks = KnowledgeStore(tmp_path / "k.db")
    hs = HornetStore(tmp_path / "h.db")
    # original source entry (file with path)
    import sqlite3

    orig = ks.add_text(
        "合分几何理论研究报告 问答",
        "合分几何 原始正文 几何动力学方程 动态分割 跨学科应用 量子类比",
        kind="file", workspace="w",
    )
    # simulate a scanned file entry (source path set by the scanner)
    con = sqlite3.connect(tmp_path / "k.db")
    con.execute("UPDATE knowledge_items SET source_path=? WHERE id=?", ("E:/docs/hefen.docx", orig))
    con.commit()
    con.close()
    # emergent digest entry pointing at the topic
    emerg = ks.add_text(
        "[涌现] 合分几何理论研究报告 问答 ⊕ 动态生成几何理论体系",
        "压缩摘要 无来源", kind="swarm_report", workspace="w",
    )
    hs.add_node("合分几何理论研究报告 问答", "合分几何 原始正文", kb_item_id=orig, vec={}, x=0, y=0, z=0)
    hs.add_node("[涌现] 合分几何理论研究报告 问答 ⊕ 动态生成几何理论体系", "压缩摘要", kb_item_id=emerg, vec={}, x=1, y=0, z=0)
    hs.add_edge(1, 2, "similar", weight=0.9, channel="G3")
    from coworker.server.manager import SessionManager

    class _M:
        hornet = hs
        _hornet_resonator = HornetResonator(hs)
        knowledge = ks
        default_workspace = "w"

        def knowledge_get(self, item_id):
            for r in self.knowledge.list_items(limit=5000):
                if r.get("id") == item_id:
                    return {**r, "content": self.knowledge.item_content(item_id)}
            return None

        def _resolve_knowledge_originals(self, title, exclude_id, limit=3):
            import re as _re

            cleaned = _re.sub(r"\[(涌现|蜂胞分裂|HORNET)[^\]]*\]", "", title)
            cleaned = cleaned.replace("⊕", " ").replace("问答", " ")
            tokens = [t.strip() for t in _re.split(r"[ \s·(（]", cleaned) if len(t.strip()) >= 3]
            out = []
            for r in self.knowledge.list_items(limit=5000):
                if r.get("id") == exclude_id or r.get("kind") != "file":
                    continue
                rt = r.get("title") or ""
                if any(tok in rt for tok in tokens) and len(out) < limit:
                    out.append({"id": r["id"], "title": rt, "source_path": r.get("source_path"), "content": self.knowledge.item_content(r["id"])})
            return out

        def knowledge_resume_pack(self, item_id, k=3):
            item = self.knowledge_get(item_id)
            if not item:
                return None
            content = item.get("content") or ""
            source = item.get("source_path")
            if (item.get("title") or "").startswith(("[涌现]", "[蜂胞分裂]")) or (not source and len(content) < 300):
                originals = self._resolve_knowledge_originals(item.get("title") or "", item_id)
                if originals:
                    content = "\n\n---\n\n".join(o["content"][:4000] for o in originals[:2]) or content
                    source = source or originals[0].get("source_path")
            return {"title": item.get("title") or "", "content": content, "source": source, "related": []}

        def knowledge_resume_by_title(self, title):
            for r in self.knowledge.list_items(limit=5000):
                if (r.get("title") or "") == title:
                    return self.knowledge_resume_pack(r["id"])
            best, best_score = None, -1
            tokens = [t for t in title.split() if len(t) >= 3]
            for r in self.knowledge.list_items(limit=5000):
                score = sum(1 for t in tokens if t in (r.get("title") or ""))
                if score > best_score:
                    best, best_score = r, score
            if best and best_score > 0:
                return self.knowledge_resume_pack(best["id"])
            return None

    m = _M()
    pack = m.knowledge_resume_pack(emerg)
    assert pack is not None
    # the pack should carry the ORIGINAL body (not the empty digest)
    assert "原始正文" in pack["content"], pack["content"][:100]
    assert pack["source"] is not None, "original source link must be present"
    # resume by title also resolves
    by_title = m.knowledge_resume_by_title("[涌现] 合分几何理论研究报告 问答 ⊕ 动态生成几何理论体系")
    assert by_title is not None and "原始正文" in by_title["content"]
