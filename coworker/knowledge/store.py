"""Knowledge file library — workspace document indexing + manual entries.

SQLite-backed: `knowledge_items` (one row per document/entry) + `knowledge_chunks`
(content split into overlapping chunks, each with a vector for similarity search).

Retrieval uses an injectable embedder (cosine) with a char-n-gram cosine fallback
so Chinese text works out of the box without any external model.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

Embedder = Callable[[str], list[float]]

# Extensions we index + directories we never descend into.
# The authoritative route lives in coworker/knowledge/extractors (extract_text);
# this set mirrors it so scans skip unknown formats cheaply before reading.
_INDEX_EXTS = {".md", ".markdown", ".txt", ".rst", ".csv", ".log", ".json", ".pdf", ".docx"}
_SKIP_DIRS = {
    ".git",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    ".coworker",
    ".qunwork",
    ".state",
    ".tmp-state",
    "_swarm_reports",
    "target",
    "dist",
    "build",
    "site-packages",
    ".reasonix",
    "backups",
    # Obsidian app metadata — `.obsidian/` holds UI preferences (graph.json,
    # workspace.json, hotkeys.json…), NOT documents. Indexing them pollutes the
    # knowledge library with config files that have no semantic content (they
    # show up as weird orphan nodes like title="graph").
    ".obsidian",
    # 2026-09-06 存量污染治理 (主会话实证: 8-30 一天 +19384 条, items 2.2k→21.9k):
    # 构建产物 / 系统目录 / 二进制伴生文本没有知识价值, 却被「已索引父目录
    # 自我扩散」机制反复扫入 — bin 下一个 .txt 入库后 bin 目录成为永久扫描根,
    # 连锁吞进 PyInstaller _internal / node_modules / 盘根。
    "bin",
    "__pycache__",
    ".cargo",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "Windows",
    "$RECYCLE.BIN",
    "System Volume Information",
    ".graphflow-cache",
    "graphflow-out",
    # 2026-09-13 (owner-hit): 打包后的 Python 运行时 (PyInstaller onedir) — 一处 sidecar
    # 就带进上千条 license/元数据文本 (numpy 的 random 测试向量等), 纯噪音且随每次
    # 打包换版本重复入库。旧库里的存量由 purge 的 R1 处理。
    "_internal",
    ".pnpm-store",
}
# 派生产物文件名模式: OIR 交付物 / 审计报告回灌知识库 = 用产出污染源
# (主会话实证: 概念索引副本被当源文档二次索引)。fnmatch 语义。
_SKIP_FILE_PATTERNS = (
    "概念索引_*",
    "术语表_*",
    "hornet_bridge_manifest_*",
    "~$*",       # Office 锁文件
    "*.crdownload",
)
# 目录名模式 (fnmatch): 精确 _SKIP_DIRS 覆盖不到的动态生成目录。
_SKIP_DIR_PATTERNS = (
    ".audit-*",   # 测试审计临时目录 (每用例随机后缀)
    "*.egg-info",
)


def _skip_dir_pattern(part: str) -> bool:
    return any(fnmatch.fnmatch(part, pat) for pat in _SKIP_DIR_PATTERNS)
# Obsidian (and other tools') per-folder UI-config JSON that a scan could still
# reach even with `.obsidian` skipped (a stray config copied next to docs, or a
# vault layout where .obsidian lives elsewhere). These carry zero knowledge.
_OBSIDIAN_CONFIG_JSON = {
    "graph.json",
    "workspace.json",
    "workspace-mobile.json",
    "hotkeys.json",
    "app.json",
    "appearance.json",
    "community-plugins.json",
    "core-plugins.json",
    "daily-notes.json",
    "templates.json",
    "bookmarks.json",
    "web-clipper.json",
    "publish.json",
    "sync.json",
}
_CHUNK_SIZE = 600  # chars per chunk
_CHUNK_OVERLAP = 120
_NGRAM_N = 3  # char n-gram width for the fallback (no-embedder) vector model


def _ngram_vector(text: str, n: int = 3) -> dict[str, float]:
    """Char n-gram count vector — decent Chinese similarity without an embedder."""
    text = re.sub(r"\s+", "", text.lower())
    if len(text) < n:
        return {text: 1.0} if text else {}
    counts: dict[str, int] = {}
    for i in range(len(text) - n + 1):
        gram = text[i : i + n]
        counts[gram] = counts.get(gram, 0) + 1
    norm = math.sqrt(sum(c * c for c in counts.values())) or 1.0
    return {g: c / norm for g, c in counts.items()}


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) & set(b)
        if not keys:
            return 0.0
        # Query-coverage similarity: the share of the QUERY's char n-grams present in the
        # document. Deliberately not length-normalized on the document side — a long doc
        # with the query's n-grams once would otherwise be diluted below any threshold,
        # which is fatal for short Chinese queries ("固态电池" → only 2 trigrams).
        # Query vectors are L2-normalized, so len(a) is the query's distinct n-gram count.
        return len(keys) / len(a)
    # dense float lists (real embedder)
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # prefer breaking at a newline near the boundary
            nl = text.rfind("\n", start + size // 2, end)
            if nl != -1 and nl > start:
                end = nl + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - overlap
    return [c for c in chunks if c]


class KnowledgeStore:
    def __init__(
        self,
        db_path: str | Path,
        embedder: Optional[Embedder] = None,
        workspace: Optional[str] = None,
        access_log: bool = True,
        scan_roots: Optional[list[str]] = None,
    ) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._default_workspace = str(workspace) if workspace else None
        # 扫描根白名单 (2026-09-06 防扩散治理): 无参扫描时, 已索引文件的父
        # 目录只有落在这些根之下才成为扫描目标。空 = 仅 default_workspace。
        self._scan_roots = [str(r) for r in (scan_roots or [])]
        # 黄金衡分形存储 M0 (P1 前置): 访问频率时间序列埋点。use_count 只有
        # 累计值, 七衡分层验证需要 f_i (次/小时) — access_log 提供时间维度。
        self._access_log = access_log
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'manual',   -- manual | file | automation | swarm_report
                source_path TEXT,
                source_run_id TEXT,                    -- asset cross-index: which run sunk this entry
                title TEXT,
                fingerprint TEXT,                       -- mtime+size for file items
                created_at REAL,
                updated_at REAL,
                UNIQUE (workspace, source_path)
            )"""
        )
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN source_run_id TEXT")
            self._con.commit()
        except sqlite3.OperationalError:
            pass  # column already present
        # Asset lifecycle (Phase 3): usage counter feeds governance feedback;
        # retired entries are hidden from search/list but kept for audit.
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # 内容指纹去重 (2026-09-06): 同 workspace 下同内容的副本文件
            # (junction 双路径 / (1).md 复制) 只建一个条目。
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN content_hash TEXT")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # Research-relay chain: derived entries point at the knowledge item
            # they were researched from (knowledge → research → new knowledge).
            self._con.execute("ALTER TABLE knowledge_items ADD COLUMN parent_id INTEGER")
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            # S11 版本控制: 当前版本号 (更新/重索引时 +1, 旧内容快照历史)。
            self._con.execute(
                "ALTER TABLE knowledge_items ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
            self._con.commit()
        except sqlite3.OperationalError:
            pass
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                vector TEXT NOT NULL
            )"""
        )
        self._con.execute("CREATE INDEX IF NOT EXISTS ix_chunks_item ON knowledge_chunks(item_id)")
        # S11 知识资产版本控制 (蜂群审计报告 G5): 每次更新/重索引把旧内容
        # 快照进 knowledge_history, 支持审计与回滚 (rollback), 误删可回退。
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                title TEXT,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS ix_kh_item ON knowledge_history(item_id, version)"
        )
        # M0 访问日志: 每次检索命中写一行 (item_id, action, ts)。双维度清理
        # (天数+条数) 沿用 probe_history prune 模式, 防无限增长。
        self._con.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL DEFAULT 'search',
                ts REAL NOT NULL
            )"""
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS ix_kal_item_ts ON knowledge_access_log(item_id, ts)"
        )
        self._con.commit()

    # -- embed -----------------------------------------------------------------
    def _embed(self, text: str):
        if self._embedder is not None:
            try:
                return self._embedder(text)
            except Exception:
                pass
        return _ngram_vector(text)

    # -- indexing --------------------------------------------------------------
    def add_text(
        self,
        title: str,
        content: str,
        *,
        kind: str = "manual",
        workspace: Optional[str] = None,
        source_run_id: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> int:
        """Index a free-text entry (manual knowledge). Returns the item id.
        parent_id: the knowledge item this entry was researched/derived from
        (research-relay chain: knowledge → research → new knowledge)."""
        if not title or not content:
            raise ValueError("title and content are required")
        ws = str(workspace) if workspace else (self._default_workspace or "")
        chash = self._content_hash(content)
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO knowledge_items (workspace, kind, source_run_id, parent_id, title, content_hash, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ws, kind, source_run_id, parent_id, title, chash, time.time(), time.time()),
            )
            item_id = cur.lastrowid
            self._index_chunks(item_id, title, content)
            self._con.commit()
        return item_id

    def index_file(self, path: str | Path, *, workspace: Optional[str] = None, force: bool = False) -> Optional[int]:
        """Index one document file (md/txt/pdf/docx/...). Re-indexes only when the
        file changed (mtime+size fingerprint). Returns the item id, or None when
        skipped (unknown format, unreadable, or extraction failure)."""
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in _INDEX_EXTS:
            return None
        # 派生产物排除 (2026-09-06 防污染): OIR 交付物 / Office 锁文件回灌
        # 知识库 = 用产出污染源 (概念索引副本曾被当源文档二次索引)。
        if any(fnmatch.fnmatch(p.name, pat) for pat in _SKIP_FILE_PATTERNS):
            return None
        ws = (
            str(workspace)
            if workspace
            else (self._default_workspace or str(p.parent))
        )
        try:
            from .extractors import ExtractionError, UnsupportedFormatError, extract_text

            content, _fmt = extract_text(p)
        except UnsupportedFormatError:
            return None  # unknown format: skip silently
        except (ExtractionError, OSError):
            # Extraction failures must NOT be silently dropped: scans count them
            # as failed so the caller can surface the reason (e.g. a scanned PDF
            # with no text layer, an encrypted file, a corrupt document).
            raise
        # 内容质量门槛 (2026-09-06 防污染): 只拦垃圾不拦短知识 — 空/纯空白,
        # 二进制伪文本 (NUL 字节 / U+FFFD 替换符占比高)。2 字符的合法短笔记
        # ("内容") 必须通过 — 历史测试语料就是这样。
        n = len(content)
        if not content.strip():
            return None
        if content.count("\x00") / n > 0.1 or content.count("\ufffd") / n > 0.2:
            return None
        fp = f"{p.stat().st_mtime_ns}:{p.stat().st_size}"
        chash = self._content_hash(content)
        with self._lock:
            row = self._con.execute(
                "SELECT id, fingerprint, COALESCE(version, 1) FROM knowledge_items "
                "WHERE workspace=? AND source_path=?",
                (ws, str(p)),
            ).fetchone()
            if row and row[1] == fp and not force:
                return row[0]
            if row:
                # S11: 更新前把旧内容快照进历史版本链 (误删可回退)。
                old_chunks = self._con.execute(
                    "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                    (row[0],),
                ).fetchall()
                old_content = "\n".join(c[0] for c in old_chunks) if old_chunks else ""
                old_version = int(row[2]) if len(row) > 2 and row[2] else 1
                if old_content:
                    self._snapshot_history(row[0], p.stem, old_content, old_version)
                self._con.execute("DELETE FROM knowledge_chunks WHERE item_id=?", (row[0],))
                item_id = row[0]
                self._con.execute(
                    "UPDATE knowledge_items SET title=?, fingerprint=?, content_hash=?, updated_at=?, version=version+1 WHERE id=?",
                    (p.stem, fp, chash, time.time(), item_id),
                )
                self._index_chunks(item_id, p.stem, content)
                self._con.commit()
            else:
                # 内容指纹去重 (2026-09-06): 同 workspace 下已有同内容条目
                # (副本/(1)复制/junction 双路径) → 指回既有条目, 不建新行。
                dupe = self._find_dupe_by_hash(ws, chash, str(p))
                if dupe is not None:
                    return dupe
                cur = self._con.execute(
                    "INSERT INTO knowledge_items (workspace, kind, source_path, title, fingerprint, content_hash, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ws, "file", str(p), p.stem, fp, chash, time.time(), time.time()),
                )
                item_id = cur.lastrowid
                self._index_chunks(item_id, p.stem, content)
                self._con.commit()
        return item_id

    def scan_workspace(self, workspace: Optional[str] = None) -> dict:
        """Index every md/txt document under the workspace(s). Returns a summary
        (added = new files indexed, updated = changed files re-indexed, skipped = unchanged).

        When `workspace` is omitted, scans EVERY directory that has already been
        indexed (from `source_path` of existing file items) plus the default
        workspace — fixing "scan shows 0 added" when no default is set: the
        library is multi-workspace, so the scan must be too, not silently no-op."""
        if workspace:
            ws = str(workspace)
            return self._scan_tree(Path(ws), ws)
        targets = self._scan_targets()
        if not targets:
            return {"added": 0, "updated": 0, "deduped": 0, "skipped": 0, "failed": 0}
        total = {"added": 0, "updated": 0, "deduped": 0, "skipped": 0, "failed": 0, "failures": []}
        for target in targets:
            part = self._scan_tree(target["path"], target["ws"])
            for k in ("added", "updated", "deduped", "skipped", "failed"):
                total[k] += part.get(k, 0)
            total["failures"].extend(part.get("failures", []) or [])
        total["workspaces_scanned"] = len(targets)
        return total

    def _scan_targets(self) -> list[dict]:
        """Directories to scan when no explicit workspace is given: the default
        workspace, the configured scan roots, plus parent directories of
        already-indexed file items (multi-workspace aggregation — S3: the
        library is multi-workspace, so the scan must be too).

        2026-09-06 防扩散加固 (items 2.2k→21.9k 事故): 已索引父目录必须通过
        _SKIP_DIRS 部件 + _SKIP_DIR_PATTERNS 模式双重过滤 — bin/Program
        Files/.audit-* 等污染根不再被接纳为扫描目标; 连锁扩散由这些排除
        规则 + content_hash 去重 + 质量门槛共同阻断。"""
        seen: dict[str, str] = {}  # resolved path -> workspace label
        if self._default_workspace:
            seen[str(Path(self._default_workspace).resolve())] = self._default_workspace
        for r in self._scan_roots:
            try:
                seen.setdefault(str(Path(r).resolve()), r)
            except (OSError, ValueError):
                continue
        try:
            rows = self._con.execute(
                "SELECT DISTINCT source_path FROM knowledge_items WHERE kind='file'"
                " AND source_path IS NOT NULL AND source_path != ''"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for (sp,) in rows:
            try:
                d = Path(sp).parent
                parts = d.parts
                if any(
                    part in _SKIP_DIRS or _skip_dir_pattern(part) for part in parts
                ):
                    continue
                key = str(d.resolve())
                if key not in seen:
                    seen[key] = str(d)
            except (OSError, ValueError):
                continue
        return [
            {"path": Path(ws), "ws": ws}
            for ws in sorted(seen.values())
            if Path(ws).is_dir()
        ]

    def index_folder(
        self,
        folder: str | Path,
        *,
        workspace: Optional[str] = None,
        max_files: Optional[int] = 20000,
        max_total_bytes: Optional[int] = 2 * 1024 * 1024 * 1024,
    ) -> dict:
        """Index every md/txt document under an arbitrary LOCAL folder (which may
        live outside the workspace). Files are chunked + vectorized into the
        knowledge library; the original path is kept as source_path. Re-scanning
        the same folder is idempotent (fingerprint dedup). `max_files` /
        `max_total_bytes` guard against accidentally importing an enormous tree
        (e.g. a home directory); when the cap is hit the scan STOPS and reports
        `truncated` so the caller knows the folder wasn't fully imported."""
        root = Path(folder).resolve()
        if not root.is_dir():
            return {"added": 0, "updated": 0, "skipped": 0, "failed": 1}
        ws = str(workspace) if workspace else (self._default_workspace or "")
        return self._scan_tree(
            root, ws, max_files=max_files, max_total_bytes=max_total_bytes
        )

    def _scan_tree(
        self,
        root: Path,
        ws: str,
        *,
        max_files: Optional[int] = None,
        max_total_bytes: Optional[int] = None,
    ) -> dict:
        """Shared scan driver: fingerprint-snapshot dedup + per-file re-index."""
        with self._lock:
            snapshot = {
                src: fp
                for src, fp in self._con.execute(
                    "SELECT source_path, fingerprint FROM knowledge_items WHERE workspace=?",
                    (ws,),
                ).fetchall()
            }
        added = skipped = failed = updated = deduped = 0
        processed = total_bytes = 0
        truncated = False
        failures: list[dict[str, str]] = []
        skip_reasons: dict[str, int] = {}
        for p in root.rglob("*"):
            if p.is_dir() or p.suffix.lower() not in _INDEX_EXTS:
                if not p.is_dir():
                    ext = p.suffix.lower() or "(none)"
                    key = f"unsupported format {ext}"
                    skip_reasons[key] = skip_reasons.get(key, 0) + 1
                continue
            rel = p.relative_to(root)
            if any(
                part in _SKIP_DIRS or _skip_dir_pattern(part) for part in rel.parts
            ):
                skip_reasons["excluded directory"] = skip_reasons.get("excluded directory", 0) + 1
                continue
            if any(fnmatch.fnmatch(p.name, pat) for pat in _SKIP_FILE_PATTERNS):
                skip_reasons["derived artifact"] = skip_reasons.get("derived artifact", 0) + 1
                continue
            # Obsidian UI-config JSON (graph.json & co.) carries no knowledge even
            # when it sits outside a skipped `.obsidian` dir — never index it.
            if p.suffix.lower() == ".json" and p.name in _OBSIDIAN_CONFIG_JSON:
                skip_reasons["obsidian config json"] = (
                    skip_reasons.get("obsidian config json", 0) + 1
                )
                continue
            if max_files is not None and processed >= max_files:
                truncated = True
                break
            try:
                st = p.stat()
                if max_total_bytes is not None and total_bytes >= max_total_bytes:
                    truncated = True
                    break
                total_bytes += st.st_size
                processed += 1
                fp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                failed += 1
                if len(failures) < 50:
                    failures.append({"path": str(p), "reason": "cannot stat file"})
                continue
            if snapshot.get(str(p)) == fp:
                skip_reasons["unchanged"] = skip_reasons.get("unchanged", 0) + 1
                skipped += 1
                continue
            is_update = str(p) in snapshot
            try:
                rid = self.index_file(p, workspace=ws, force=True)
            except Exception as exc:
                failed += 1
                if len(failures) < 50:
                    failures.append({"path": str(p), "reason": str(exc)[:200]})
                continue
            if rid is None:
                # 低质量内容 / 派生产物 / 提取失败 — index_file 拒收
                skipped += 1
                continue
            if is_update:
                updated += 1
            else:
                meta = self.get_item_meta(rid)
                if meta and meta.get("source_path") and Path(meta["source_path"]) != p:
                    # 内容指纹去重: 指回既有条目 (副本/junction 双路径)
                    deduped += 1
                else:
                    added += 1
        return {
            "added": added,
            "updated": updated,
            "deduped": deduped,
            "skipped": skipped,
            "failed": failed,
            "truncated": truncated,
            "failures": failures,
            "skip_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: -kv[1])),
        }

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cur = self._con.execute("DELETE FROM knowledge_items WHERE id=?", (item_id,))
            self._con.execute("DELETE FROM knowledge_chunks WHERE item_id=?", (item_id,))
            self._con.commit()
        return cur.rowcount > 0

    def set_retired(self, item_id: int, retired: bool) -> bool:
        """Asset lifecycle (Phase 3): mark an entry retired (hidden from search)
        or restore it. Audit trail is preserved — retirement is not deletion."""
        with self._lock:
            cur = self._con.execute(
                "UPDATE knowledge_items SET retired = ? WHERE id = ?",
                (1 if retired else 0, item_id),
            )
            self._con.commit()
        return cur.rowcount > 0

    def item_content(self, item_id: int) -> str:
        """Reassemble an item's full text from its chunks (HORNET builder needs
        the body, which list_items deliberately omits)."""
        with self._lock:
            rows = self._con.execute(
                "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                (item_id,),
            ).fetchall()
        return "\n".join(r[0] for r in rows)

    def get_item_meta(self, item_id: int) -> Optional[dict]:
        """One item's metadata row (no body) — for cross-referencing a HORNET
        node back to its source knowledge entry (问题3: 共振命中→原文)."""
        with self._lock:
            row = self._con.execute(
                "SELECT id, kind, source_path, source_run_id, parent_id, title,"
                " created_at, updated_at, use_count, retired, workspace"
                " FROM knowledge_items WHERE id=?",
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "kind": row[1],
            "source_path": row[2],
            "source_run_id": row[3],
            "parent_id": row[4],
            "title": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "use_count": row[8],
            "retired": row[9],
            "workspace": row[10],
        }

    def count_items(self, workspace: Optional[str] = None, *, include_retired: bool = False) -> int:
        """Number of items a LIST would show — active only, unless asked otherwise.

        The list hides retired rows; a count that included them made the page claim
        "200/21782" while only 8,944 rows could ever load (owner-hit 2026-09-13: 12,838
        of those were superseded versions, i.e. the count over-reported by 2.4×).
        `include_retired=True` is for audit/maintenance callers.
        """
        ws = str(workspace) if workspace else self._default_workspace
        retired_filter = "" if include_retired else " AND retired=0"
        with self._lock:
            if ws:
                row = self._con.execute(
                    f"SELECT COUNT(*) FROM knowledge_items WHERE workspace=?{retired_filter}",
                    (ws,),
                ).fetchone()
            else:
                row = self._con.execute(
                    f"SELECT COUNT(*) FROM knowledge_items WHERE 1=1{retired_filter}"
                ).fetchone()
        return row[0] if row else 0

    def backfill_content_hashes(
        self,
        *,
        limit: Optional[int] = None,
        active_only: bool = False,
        commit_every: int = 200,
    ) -> dict:
        """Give legacy rows a content hash so content-level dedupe can see them.

        content_hash landed 2026-09-06; everything indexed before it carries an empty
        hash, which made `_find_dupe_by_hash` blind — identical documents in different
        paths/workspaces could never be recognised. Prefer RE-EXTRACTING the file (that
        yields the same hash a fresh index computes, so new and old rows compare equal);
        fall back to hashing the stored chunks when the source is gone.

        Two knobs exist because the real library is 21.7k rows on a slow, nearly-full
        volume (2026-09-13: 564 rows in 2 minutes with a commit per row):
        ``active_only`` skips retired rows — invisible to search and to dedupe, and their
        chunks are the ones the purge deletes, so hashing them is work thrown away;
        ``commit_every`` batches the writes into one fsync per N rows.
        """
        sql = (
            "SELECT id, source_path FROM knowledge_items "
            "WHERE (content_hash IS NULL OR content_hash='') AND kind='file'"
        )
        if active_only:
            sql += " AND retired=0"
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._con.execute(sql).fetchall()
            skipped_retired = 0
            if active_only:
                skipped_retired = self._con.execute(
                    "SELECT COUNT(*) FROM knowledge_items WHERE (content_hash IS NULL "
                    "OR content_hash='') AND kind='file' AND retired=1"
                ).fetchone()[0]

        filled = from_disk = from_chunks = skipped = 0
        batch = max(1, int(commit_every))
        pending = 0
        for item_id, src in rows:
            content: Optional[str] = None
            if src:
                try:
                    p = Path(src)
                    if p.is_file():
                        from .extractors import extract_text

                        content, _fmt = extract_text(p)
                        if content:
                            from_disk += 1
                except Exception:
                    content = None  # unreadable/gone → fall back to the stored chunks
            if not content:
                with self._lock:
                    chunks = self._con.execute(
                        "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                        (item_id,),
                    ).fetchall()
                content = "\n".join(c[0] for c in chunks) if chunks else None
                if content:
                    from_chunks += 1
            if not content:
                skipped += 1
                continue
            with self._lock:
                self._con.execute(
                    "UPDATE knowledge_items SET content_hash=? WHERE id=?",
                    (self._content_hash(content), item_id),
                )
                pending += 1
                if pending >= batch:
                    self._con.commit()
                    pending = 0
            filled += 1
        with self._lock:
            self._con.commit()
        return {
            "candidates": len(rows),
            "filled": filled,
            "from_disk": from_disk,
            "from_chunks": from_chunks,
            "skipped": skipped,
            "skipped_retired": skipped_retired,
        }

    def list_items(
        self,
        workspace: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        ws = str(workspace) if workspace else self._default_workspace
        with self._lock:
            if ws:
                rows = self._con.execute(
                    "SELECT id, kind, source_path, source_run_id, parent_id, title, created_at, updated_at, use_count, retired FROM knowledge_items "
                    "WHERE workspace=? AND retired=0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (ws, limit, offset),
                ).fetchall()
            else:
                rows = self._con.execute(
                    "SELECT id, kind, source_path, source_run_id, parent_id, title, created_at, updated_at, use_count, retired FROM knowledge_items "
                    "WHERE retired=0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [
            {
                "id": r[0],
                "kind": r[1],
                "source_path": r[2],
                "source_run_id": r[3],
                "parent_id": r[4],
                "title": r[5],
                "created_at": r[6],
                "updated_at": r[7],
                "use_count": r[8],
                "retired": r[9],
            }
            for r in rows
        ]

    # -- search ----------------------------------------------------------------
    def search(
        self,
        query: str,
        k: int = 5,
        workspace: Optional[str] = None,
        min_score: float = 0.1,
    ) -> list[dict]:
        """Top-k ITEMS matching the query (best chunk per item), with metadata.

        `min_score` filters out weak n-gram matches; per-item dedup keeps a long
        document's many chunks from crowding out the whole result list.
        """
        qv = self._embed(query)
        ws = str(workspace) if workspace else self._default_workspace
        scored: list[tuple[float, dict]] = []
        # 短查询子串回退 (2026-09-06 主会话实证): trigram 模型对 <3 字符查询
        # 结构性失效 — 查询向量 key 是整串 ("宇宙"), 块向量 key 是三元组
        # ("的宇宙"), 交集恒空、余弦恒 0; 中文核心术语以 2 字为主 (宇宙/量子/
        # 分形/守恒), 连含"宇宙"222 次的文档都搜不到。回退直接做子串扫描:
        # 子串命中 = 真命中; 分数按出现次数封顶 (1次=0.34, 2次=0.67, ≥3次=1.0),
        # 与 query-coverage 量纲兼容, min_score 过滤语义不变。
        # 仅在 n-gram 回退路径生效 (真实 embedder 的稠密向量无此问题)。
        cleaned_q = re.sub(r"\s+", "", query.lower())
        short_query = (
            self._embedder is None
            and isinstance(qv, dict)
            and bool(cleaned_q)
            and len(cleaned_q) < _NGRAM_N
        )
        if short_query:
            # 子串过滤下推到 SQL (LIKE): GB 级库全表拉取到 Python 再逐块
            # re.sub 实测 88s/查询; LIKE 让 SQLite 在 C 层过滤, 只拉命中块。
            like = (
                "%" + cleaned_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            with self._lock:
                if ws:
                    rows = self._con.execute(
                        """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                           FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                           WHERE i.workspace=? AND i.retired=0 AND c.content LIKE ? ESCAPE '\\' ORDER BY c.id""",
                        (ws, like),
                    ).fetchall()
                else:
                    rows = self._con.execute(
                        """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                           FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                           WHERE i.retired=0 AND c.content LIKE ? ESCAPE '\\' ORDER BY c.id""",
                        (like,),
                    ).fetchall()
        else:
            with self._lock:
                if ws:
                    rows = self._con.execute(
                        """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                           FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                           WHERE i.workspace=? AND i.retired=0 ORDER BY c.id""",
                        (ws,),
                    ).fetchall()
                else:
                    rows = self._con.execute(
                        """SELECT c.item_id, c.chunk_index, c.content, c.vector, i.kind, i.title, i.source_path, i.source_run_id, i.use_count
                           FROM knowledge_chunks c JOIN knowledge_items i ON i.id = c.item_id
                           WHERE i.retired=0 ORDER BY c.id"""
                    ).fetchall()
        for item_id, ci, content, vec_json, kind, title, src, src_run, use_count in rows:
            if short_query:
                occ = content.lower().count(cleaned_q)
                if not occ:
                    continue  # LIKE 命中但含空白断开的查询串 (罕见), 跳过
                score = min(1.0, occ / 3.0)
            else:
                try:
                    vec = json.loads(vec_json)
                except Exception:
                    continue
                score = _cosine(qv, vec)
                if score <= 0:
                    continue
            scored.append(
                (
                    score,
                    {
                        "item_id": item_id,
                        "chunk_index": ci,
                        "content": content,
                        "score": round(score, 4),
                        "kind": kind,
                        "title": title,
                        "source_path": src,
                        "source_run_id": src_run,
                        "use_count": use_count,
                    },
                )
            )
        scored.sort(key=lambda t: -t[0])
        seen: set[int] = set()
        results: list[dict] = []
        for _, hit in scored:
            if hit["score"] < min_score:
                continue
            if hit["item_id"] in seen:
                continue
            seen.add(hit["item_id"])
            results.append(hit)
            if len(results) >= k:
                break
        # Asset lifecycle (Phase 3): a retrieval ticks the usage counter — the
        # governance feedback signal for the asset loop (used assets rank better).
        # M0: 同事务写 access_log 时间序列 (P1 七衡分层的数据源)。
        if results:
            with self._lock:
                self._con.executemany(
                    "UPDATE knowledge_items SET use_count = use_count + 1 WHERE id = ?",
                    [(r["item_id"],) for r in results],
                )
                if self._access_log:
                    now = time.time()
                    self._con.executemany(
                        "INSERT INTO knowledge_access_log (item_id, action, ts) VALUES (?, 'search', ?)",
                        [(r["item_id"], now) for r in results],
                    )
                self._con.commit()
        return results

    # -- M0 access log (黄金衡分形存储 P1 前置) --------------------------------
    def prune_access_log(self, *, keep_days: int = 30, keep_count: int = 100_000) -> dict:
        """双维度清理访问日志: 天数窗口 (ts 早于 keep_days 天) + 条数窗口
        (仅保留最新 keep_count 条)。返回 {removed, kept, keep_days, keep_count}。
        与 probe_history prune 同款约定, 防日志无限增长。"""
        with self._lock:
            before = self._con.execute(
                "SELECT COUNT(*) FROM knowledge_access_log"
            ).fetchone()[0]
            cutoff = time.time() - keep_days * 86400
            self._con.execute("DELETE FROM knowledge_access_log WHERE ts < ?", (cutoff,))
            self._con.execute(
                "DELETE FROM knowledge_access_log WHERE id NOT IN "
                "(SELECT id FROM knowledge_access_log ORDER BY id DESC LIMIT ?)",
                (keep_count,),
            )
            self._con.commit()
            after = self._con.execute(
                "SELECT COUNT(*) FROM knowledge_access_log"
            ).fetchone()[0]
        return {
            "removed": before - after,
            "kept": after,
            "keep_days": keep_days,
            "keep_count": keep_count,
        }

    def access_frequency(self, *, window_hours: Optional[float] = None) -> dict[int, float]:
        """P1 数据源: 每 item 的访问频率 {item_id: f_i}。

        window_hours=None → 累计代理: use_count (全历史, 无时间维度);
        window_hours=T → access_log 在最近 T 小时内的次数换算为次/小时。
        """
        with self._lock:
            if window_hours is None:
                rows = self._con.execute(
                    "SELECT id, use_count FROM knowledge_items WHERE use_count > 0"
                ).fetchall()
                return {int(r[0]): float(r[1]) for r in rows}
            cutoff = time.time() - window_hours * 3600
            rows = self._con.execute(
                "SELECT item_id, COUNT(*) FROM knowledge_access_log "
                "WHERE ts >= ? GROUP BY item_id",
                (cutoff,),
            ).fetchall()
        hours = max(window_hours, 1e-9)
        return {int(r[0]): float(r[1]) / hours for r in rows}

    # -- internals -------------------------------------------------------------
    @staticmethod
    def _content_hash(content: str) -> str:
        """内容指纹 (sha256) — 副本/junction 双路径的同一份文档只建一个条目。"""
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()

    def _find_dupe_by_hash(self, ws: str, content_hash: str, exclude_path: str) -> Optional[int]:
        """同 workspace 下与给定内容指纹相同、且路径不同的既有未退役条目。
        注意: 调用方必须已持有 self._lock (threading.Lock 不可重入)。"""
        row = self._con.execute(
            "SELECT id FROM knowledge_items WHERE workspace=? AND content_hash=? "
            "AND retired=0 AND (source_path IS NULL OR source_path<>?) ORDER BY id LIMIT 1",
            (ws, content_hash, exclude_path),
        ).fetchone()
        return row[0] if row else None

    def _index_chunks(self, item_id: int, title: str, content: str) -> None:
        chunks = _chunk_text(content)
        if not chunks:
            return
        for i, chunk in enumerate(chunks):
            vec = self._embed(chunk)
            self._con.execute(
                "INSERT INTO knowledge_chunks (item_id, chunk_index, content, vector) VALUES (?,?,?,?)",
                (item_id, i, chunk, json.dumps(vec)),
            )

    # -- S11 知识资产版本控制 (蜂群审计报告 G5) ------------------------------
    def _snapshot_history(self, item_id: int, title: str, content: str, version: int) -> None:
        """把当前内容快照进 knowledge_history (更新/重索引前调用)。"""
        chunks = _chunk_text(content)
        snapshot = "\n".join(chunks) if chunks else content
        self._con.execute(
            "INSERT INTO knowledge_history (item_id, version, title, content, created_at) "
            "VALUES (?,?,?,?,?)",
            (item_id, version, title, snapshot, time.time()),
        )

    def history(self, item_id: int, limit: int = 50) -> list[dict]:
        """版本链: 该知识条目的历史版本 (旧->新, 含当前版本标记)。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT version, title, content, created_at FROM knowledge_history "
                "WHERE item_id=? ORDER BY version DESC LIMIT ?",
                (item_id, limit),
            ).fetchall()
            cur = self._con.execute(
                "SELECT version, title FROM knowledge_items WHERE id=?",
                (item_id,),
            ).fetchone()
        out = [
            {"version": r[0], "title": r[1], "content": r[2], "created_at": r[3], "current": False}
            for r in rows
        ]
        if cur is not None:
            out.insert(
                0,
                {
                    "version": cur[0],
                    "title": cur[1],
                    "content": "（当前版本内容, 见 knowledge_chunks）",
                    "created_at": None,
                    "current": True,
                },
            )
        return out

    def rollback(self, item_id: int, version: int) -> bool:
        """回滚到指定历史版本: 恢复该版本内容并快照当前内容, 版本 +1。
        返回是否成功; 目标版本不存在返回 False (不改动)。"""
        with self._lock:
            cur = self._con.execute(
                "SELECT version, title FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone()
            if cur is None:
                return False
            hist = self._con.execute(
                "SELECT title, content FROM knowledge_history "
                "WHERE item_id=? AND version=?",
                (item_id, version),
            ).fetchone()
            if hist is None:
                return False
            # 快照当前内容 → 历史, 然后恢复目标版本
            current_title = cur[1]
            chunks = self._con.execute(
                "SELECT content FROM knowledge_chunks WHERE item_id=? ORDER BY chunk_index",
                (item_id,),
            ).fetchall()
            current_content = "\n".join(c[0] for c in chunks)
            self._snapshot_history(item_id, current_title, current_content, cur[0])
            self._con.execute(
                "DELETE FROM knowledge_chunks WHERE item_id=?", (item_id,)
            )
            new_title = hist[0] or current_title
            self._con.execute(
                "UPDATE knowledge_items SET title=?, version=version+1, updated_at=? WHERE id=?",
                (new_title, time.time(), item_id),
            )
            self._index_chunks(item_id, new_title, hist[1])
            self._con.commit()
        return True

    def dedupe(self, *, dry_run: bool = False) -> dict:
        """S5 知识去重: 跨 workspace 的重复条目 (同 title + 首 chunk 内容完全
        一致) — 知识库多工作区扫描常产生同源副本 (G5: 全局检索+副本清理)。

        保守规则: 仅当两条记录 title 相同 AND 内容指纹 (首个 chunk 内容)
        完全一致时视为重复; 保留 id 较小的一条, 其余 retired (不物理删除,
        保审计)。不同 workspace 的同内容条目也会去重 (跨工作区副本)。
        返回 {"retired": [ids], "dry_run": bool}。
        """
        with self._lock:
            rows = self._con.execute(
                "SELECT id, title FROM knowledge_items WHERE retired=0 ORDER BY id"
            ).fetchall()
            # 首 chunk 内容 (内容指纹)
            first_chunk: dict[int, str] = {}
            for rid, _t in rows:
                c = self._con.execute(
                    "SELECT content FROM knowledge_chunks WHERE item_id=? "
                    "ORDER BY chunk_index LIMIT 1",
                    (rid,),
                ).fetchone()
                first_chunk[rid] = c[0] if c else ""
        retired: list[int] = []
        seen: dict[tuple, int] = {}
        for rid, title in rows:
            key = ((title or "").strip(), first_chunk.get(rid, "").strip())
            if not key[0] or not key[1]:
                continue  # 无内容的不参与去重
            if key in seen:
                retired.append(rid)
                if not dry_run:
                    self._con.execute(
                        "UPDATE knowledge_items SET retired=1 WHERE id=?", (rid,)
                    )
            else:
                seen[key] = rid
        if not dry_run:
            self._con.commit()
        return {"retired": retired, "dry_run": dry_run}

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
