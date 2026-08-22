"""Skill loading — Anthropic SKILL.md format with progressive disclosure.

A skill is a folder containing `SKILL.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body of instructions + optional resources/scripts.

Progressive disclosure: at session start only the catalog (name + description) is injected
into the agent's context; the full body is loaded on demand via the `load_skill` tool.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import aisuite as ai

from . import lock as _skill_lock
from . import security as _skill_security


@dataclass
class Skill:
    name: str
    description: str
    instructions: str = ""  # full body — loaded on demand
    path: Optional[str] = None
    allowed_tools: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    category: str = "general"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    updated_at: Optional[str] = None  # ISO timestamp from frontmatter
    # P1-6: draft 状态 (来自 HORNET 涌现自动生成, 等待用户审核)
    draft: bool = False
    # P1-6: 安全评分 (0-100, 由 skills/security.py 静态分析得出)
    security_score: Optional[int] = None
    # P1-8: 来源标记 ("manual" / "hornet_emergence")
    source: str = "manual"
    # Skill.lock 信息 (加载时填入, None = 未锁定)
    lock_meta: Optional[dict[str, Any]] = None
    # 脚本完整性 (verify_scripts_integrity 结果, None = 未检查)
    scripts_integrity: Optional[list[dict[str, Any]]] = None

    def catalog_row(self) -> dict:
        # 安全评分 -> 级别 (与 security.py 保持一致 — 分数越高越危险:
        # <=20 low / <=50 medium / <=80 high / else critical)。曾有一版把
        # 映射写反 (>=85 -> low), 导致最危险的技能在目录里显示为 low。
        from .security import level_for_score

        level: Optional[str] = (
            level_for_score(self.security_score) if self.security_score is not None else None
        )
        lock_exists = self.lock_meta is not None
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "category": self.category,
            "author": self.author,
            "tags": self.tags,
            "updated_at": self.updated_at,
            "draft": self.draft,
            "security_score": self.security_score,
            "security_level": level,
            "source": self.source,
            # Skill.lock 摘要 (双字段: 前端友好 + 老 API 兼容)
            "lock_exists": lock_exists,
            "locked": lock_exists,
            "lock_version": self.lock_meta.get("schema_version") if self.lock_meta else None,
            "lock_generated_at": self.lock_meta.get("generated_at") if self.lock_meta else None,
            "tool_count_locked": len(self.lock_meta.get("tools", [])) if self.lock_meta else 0,
            # 脚本完整性
            "scripts_tampered": (
                len(self.scripts_integrity) if self.scripts_integrity is not None else None
            ),
            # 兼容性占位 (由 manager / detail 级别动态查询填入)
            "compatible": None,
            "compat_severity": None,
            # 可用版本列表 (由 manager 级别聚合填入)
            "available_versions": None,
        }


class SkillLoader:
    def __init__(
        self,
        dirs: list[str | Path],
        *,
        registry_tool_schemas: Optional[Callable[[], dict[str, dict | list]]] = None,
        readonly_dirs: Optional[list[str | Path]] = None,
    ) -> None:
        """Load skills from directories.

        Args:
            dirs: skill 搜索目录。
            registry_tool_schemas: 可选的回调, 返回 {tool_name: schema}。
                生成 skill.lock 时能把真实工具 schema 算入签名哈希, 兼容性
                检测更准确。如果为 None, lock 里记录空 schema (参数列表仅
                记录提取到的名字)。
            readonly_dirs: 只读目录(如内置技能层 coworker/skills)。save_skill /
                import_skill 的写入目标会跳过这些目录 — 用户技能、涌现技能
                只能落到用户层(state_dir/skills 或 workspace/.coworker/skills),
                避免污染源码目录/安装目录(路径修复后内置层可写导致的副作用)。
        """
        self._dirs = [Path(d) for d in dirs]
        self._readonly = {Path(d) for d in (readonly_dirs or [])}
        self._skills: dict[str, Skill] = {}
        self._registry_tool_schemas = registry_tool_schemas
        # Fingerprint of the last completed scan (per-dir SKILL.md name+mtime).
        self._scan_snapshot: Optional[tuple] = None
        # C16: _skills is shared between the loop thread (catalog() for the
        # context provider) and worker/tool threads (load_skill -> refresh()).
        # refresh() clears and rebuilds the dict, so concurrent iteration is a
        # RuntimeError waiting to happen — serialize all access.
        self._lock = threading.RLock()
        self.refresh()

    def refresh(self) -> None:
        """(Re)scan every skill directory — call after saving a new skill so it is
        immediately available to the running engine.

        Skips the rescan when nothing changed since the last pass: load_skill
        calls refresh() on EVERY tool invocation, and with ~110+ skills (61
        built-in + HORNET emergence drafts) the unconditional rescan burned
        seconds of lock-verification file hashing per call — CPU pressure that
        stacked on top of the HORNET sweeps and froze turns. The fingerprint is
        cheap (one stat per SKILL.md) and any add/remove/edit trips it."""
        with self._lock:
            snap = self._scan_fingerprint()
            if self._scan_snapshot is not None and snap == self._scan_snapshot:
                return
            self._scan_snapshot = snap
            self._skills.clear()
            for directory in self._dirs:
                self._discover(directory)

    def _scan_fingerprint(self) -> tuple:
        marks: list[tuple[str, str, int, int]] = []
        for d in self._dirs:
            try:
                entries = sorted(d.iterdir())
            except OSError:
                continue
            for sub in entries:
                try:
                    st = (sub / "SKILL.md").stat()
                    marks.append((str(d), sub.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    marks.append((str(d), sub.name, 0, 0))
        return tuple(marks)

    def save_skill(
        self,
        name: str,
        description: str,
        body: str,
        *,
        version: str = "0.1.0",
        category: str = "general",
        author: str = "",
        tags: Optional[list[str]] = None,
        allowed_tools: Optional[list[str]] = None,
        draft: bool = False,
        source: str = "manual",
    ) -> Path:
        """Write a new skill to the FIRST writable dir (workspace-local preferred)
        and refresh the catalog so it is immediately loadable."""
        # Sanitize like import_skill does — keep '.' OUT of the final name and
        # reject '.'/'..' outright, else name=".." escapes the skill dir upward
        # (regression C4: no strip('.') meant target / '..' landed in the parent).
        name = re.sub(r"[^\w\-.]", "_", name).strip("_.") or "skill"
        if name in (".", ".."):
            name = "skill"
        target = next(
            (d for d in self._dirs if d not in self._readonly and self._writable(d)),
            self._dirs[-1],
        )
        skill_dir = target / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        md = skill_dir / "SKILL.md"
        lines = [f"name: {name}", f"description: {description}"]
        if version:
            lines.append(f"version: {version}")
        if category:
            lines.append(f"category: {category}")
        if author:
            lines.append(f"author: {author}")
        if tags:
            lines.append("tags: " + ", ".join(tags))
        if allowed_tools:
            lines.append("allowed-tools: " + ", ".join(allowed_tools))
        if draft:
            lines.append("draft: true")
        if source != "manual":
            lines.append(f"source: {source}")
        md.write_text(f"---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n", encoding="utf-8")
        # 13 Skill 信任基础: 保存后自动生成 skill.lock + 计算安全评分
        try:
            schemas = self._registry_tool_schemas() if self._registry_tool_schemas else None
            _skill_lock.auto_generate_lock(
                skill_dir,
                skill_name=name,
                skill_version=version,
                registry_tool_schemas=schemas,
            )
        except Exception:
            pass  # lock 失败不阻断主流程
        self.refresh()
        return md

    def update_skill(
        self,
        name: str,
        description: Optional[str] = None,
        body: Optional[str] = None,
        *,
        version: Optional[str] = None,
        category: Optional[str] = None,
        author: Optional[str] = None,
        tags: Optional[list[str]] = None,
        allowed_tools: Optional[list[str]] = None,
    ) -> Optional[Path]:
        """Patch an existing skill's frontmatter/body in place (returns its SKILL.md
        path, or None when the skill does not exist). S7: 更新前把旧版本快照
        到 .versions/ (变更可审计、可回退)。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        md = Path(skill.path) / "SKILL.md"
        # S7 工具/技能版本管理: 更新前快照旧版本 (回滚依据)。
        self._snapshot_version(skill)
        text = md.read_text(encoding="utf-8")
        end = text.find("\n---", 3)
        front = text[3:end] if text.startswith("---") and end != -1 else ""
        body_text = text[end + 4 :].lstrip("\n") if end != -1 else text

        def _set(key: str, value: str) -> str:
            pat = re.compile(rf"^{key}:\s*.*$", re.M)
            line = f"{key}: {value}"
            return pat.sub(line, front) if pat.search(front) else front + "\n" + line

        if description is not None:
            front = _set("description", description)
        if version is not None:
            front = _set("version", version)
        if category is not None:
            front = _set("category", category)
        if author is not None:
            front = _set("author", author)
        if tags is not None:
            front = _set("tags", ", ".join(tags))
        if allowed_tools is not None:
            front = _set("allowed-tools", ", ".join(allowed_tools))
        if body is not None:
            body_text = body
        md.write_text(f"---\n{front.lstrip(chr(10))}\n---\n\n{body_text}\n", encoding="utf-8")
        # 更新后重新生成 lock (schema 可能变化, 版本号可能变化)
        try:
            s = self.get(name)
            schemas = self._registry_tool_schemas() if self._registry_tool_schemas else None
            _skill_lock.auto_generate_lock(
                Path(skill.path),
                skill_name=s.name if s else None,
                skill_version=s.version if s else version,
                registry_tool_schemas=schemas,
            )
        except Exception:
            pass
        self.refresh()
        return md

    def delete_skill(self, name: str) -> bool:
        """Remove a skill folder entirely (all dirs; returns True when something was
        deleted). S7: 删除前把各目录副本快照到 .versions/ (误删可回退)。"""
        name = re.sub(r"[^\w\-.]", "_", name).strip("_").strip(".")
        removed = False
        for directory in self._dirs:
            target = directory / name
            if (target / "SKILL.md").is_file():
                import shutil

                # S7: 删除前快照 (回滚依据)
                try:
                    snap = target / ".versions" / "pre-delete"
                    snap.mkdir(parents=True, exist_ok=True)
                    for f in target.rglob("*"):
                        if f.is_file() and ".versions" not in f.parts:
                            rel = f.relative_to(target)
                            (snap / rel).parent.mkdir(parents=True, exist_ok=True)
                            (snap / rel).write_bytes(f.read_bytes())
                except Exception:
                    pass
                shutil.rmtree(target, ignore_errors=True)
                removed = True
        if removed:
            self.refresh()
        return removed

    # -- S7 工具/技能版本管理与回滚 (蜂群审计报告 G8) -------------------------
    def _snapshot_version(self, skill: "Skill") -> Optional[Path]:
        """把技能当前版本快照到 <skill>/.versions/<ts>-<version>/ (变更前调用)。
        返回快照路径; 失败返回 None (不阻断更新)。"""
        if skill is None or skill.path is None:
            return None
        try:
            import time as _t

            src = Path(skill.path)
            ts = _t.strftime("%Y%m%d%H%M%S", _t.gmtime())
            ver = (skill.version or "0.1.0").replace("/", "_")
            snap_dir = src / ".versions" / f"{ts}-{ver}"
            snap_dir.mkdir(parents=True, exist_ok=True)
            for f in src.rglob("*"):
                if f.is_file() and ".versions" not in f.parts:
                    rel = f.relative_to(src)
                    target = snap_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.read_bytes())
            return snap_dir
        except Exception:
            return None

    def skill_history(self, name: str) -> list[dict]:
        """版本历史: 该技能 .versions/ 下的快照列表 (旧->新, 含当前版本)。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return []
        versions_dir = Path(skill.path) / ".versions"
        if not versions_dir.is_dir():
            return []
        out = []
        for d in sorted(versions_dir.iterdir()):
            if d.is_dir():
                out.append(
                    {
                        "name": d.name,
                        "version": d.name.split("-", 1)[1] if "-" in d.name else d.name,
                        "path": str(d / "SKILL.md"),
                    }
                )
        out.append({"name": "current", "version": skill.version, "path": str(skill.path)})
        return out

    def rollback_skill(self, name: str, snapshot_name: str) -> bool:
        """回滚技能到指定历史快照: 用快照内容覆盖当前 SKILL.md (及资源),
        并把当前版本快照进 .versions/ (回滚本身可追溯)。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return False
        snap_dir = Path(skill.path) / ".versions" / snapshot_name
        snap_md = snap_dir / "SKILL.md"
        if not snap_md.is_file():
            return False
        try:
            import shutil

            # 先快照当前版本 (回滚可再回退)
            self._snapshot_version(skill)
            src = Path(skill.path)
            # 清理当前资源 (除 .versions), 再复制快照内容
            for f in list(src.rglob("*")):
                if f.is_file() and ".versions" not in f.parts:
                    f.unlink(missing_ok=True)
            for f in snap_dir.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(snap_dir)
                    target = src / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(f.read_bytes())
            self.refresh()
            return True
        except Exception:
            return False

    def export_skill(self, name: str, dest: Path) -> Optional[Path]:
        """Pack a skill folder (SKILL.md + any resources/scripts) into a zip file."""
        import shutil
        import zipfile

        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            root = Path(skill.path)
            for file in sorted(root.rglob("*")):
                # Never follow symlinks into the zip — a link inside the skill
                # could point at an arbitrary machine file (低危: rglob follows
                # links on Py≤3.12, packing the target's contents).
                if file.is_file() and not file.is_symlink():
                    zf.write(file, file.relative_to(root).as_posix())
        return dest

    def import_skill(self, zip_path: Path, *, target_dir: Optional[Path] = None) -> Optional[Skill]:
        """Install a skill from a zip (validates SKILL.md at the zip root or a single
        top-level folder). Returns the installed Skill, or None on invalid zip."""
        import zipfile

        target_dir = target_dir or next(
            (d for d in self._dirs if d not in self._readonly and self._writable(d)),
            self._dirs[-1],
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            # M5: zip-bomb guard — bound the entry count and the decompressed
            # size so a pathological zip can't exhaust disk/CPU on import.
            if len(names) > _IMPORT_MAX_ENTRIES:
                return None
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if total > _IMPORT_MAX_BYTES:
                    return None
            top = _zip_root(names)
            # SKILL.md must sit at the skill's top level — a nested
            # resources/SKILL.md (or an xSKILL.md suffix match) must not be
            # mistaken for the skill manifest (低危: endswith("SKILL.md") was
            # too wide).
            skill_md = next(
                (
                    n
                    for n in names
                    if n.endswith("SKILL.md")
                    and _is_under(n, top)
                    and n.count("/") == top.count("/")
                ),
                None,
            )
            if skill_md is None:
                return None
            skill_name = Path(skill_md).parent.name
            if not skill_name or skill_name == ".":
                # SKILL.md sits at the zip root — take its name from the frontmatter.
                fm_text = zf.read(skill_md).decode("utf-8", "replace")
                m = re.search(r"^name:\s*(.+)$", fm_text, re.M)
                skill_name = m.group(1).strip() if m else zip_path.stem
            skill_name = re.sub(r"[^\w\-.]", "_", skill_name).strip("_").strip(".")
            if skill_name in ("", ".", ".."):
                skill_name = "skill"
            base_dir = target_dir.resolve()
            out_dir = (base_dir / skill_name).resolve()
            if not out_dir.is_relative_to(base_dir):
                raise ValueError(f"invalid skill name: {skill_name!r}")
            for name in names:
                if not _is_under(name, top):
                    continue
                rel = name[len(top) :].lstrip("/")
                if not rel:
                    continue
                # zip-slip guard: no entry may escape the skill's own folder
                # (rejects "..", absolute paths, and backslash variants).
                target = (out_dir / rel).resolve()
                if not target.is_relative_to(out_dir):
                    raise ValueError(f"zip entry escapes skill folder: {name!r}")
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
        self.refresh()
        installed = self.get(skill_name)
        # 导入外部 skill 后: 1) 重新生成 lock (防止 zip 自带 lock 有后门)  2) 安全评分 + 脚本完整性检查
        if installed is not None and installed.path is not None:
            try:
                schemas = self._registry_tool_schemas() if self._registry_tool_schemas else None
                _skill_lock.auto_generate_lock(
                    installed.path,
                    skill_name=installed.name,
                    skill_version=installed.version,
                    registry_tool_schemas=schemas,
                )
            except Exception:
                pass
        return self.get(skill_name)

    @staticmethod
    def _writable(directory: Path) -> bool:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".qunwork-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    def _discover(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for sub in sorted(directory.iterdir()):
            md = sub / "SKILL.md"
            if md.is_file():
                skill = _parse_skill(md)
                # lock 信息 + 脚本完整性
                try:
                    skill.lock_meta = _skill_lock.load_lock(sub)
                    if skill.lock_meta is not None:
                        skill.scripts_integrity = _skill_lock.verify_scripts_integrity(
                            skill.lock_meta, sub
                        )
                except Exception:
                    skill.lock_meta = None
                    skill.scripts_integrity = None
                # 安全评分 (磁盘无缓存时实时算)
                if skill.security_score is None:
                    try:
                        rep = _skill_security.analyze_skill_dir(sub)
                        skill.security_score = rep["score"]
                        # 把 findings/recommendation 作为 transient 附加 (方便详情页读取)
                        # 注意: catalog_row 只返回 score, 详细信息通过 loader.security_report(name) 获取
                        skill._security_report_cache = rep  # type: ignore[attr-defined]
                    except Exception:
                        skill.security_score = None
                self._skills[skill.name] = skill

    def names(self) -> list[str]:
        with self._lock:
            return list(self._skills)

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            return self._skills.get(name)

    def catalog(self) -> list[dict]:
        with self._lock:
            return [s.catalog_row() for s in self._skills.values()]

    # -- S13 技能健康检查常态化 (蜂群审计报告 G8) -----------------------------
    def health_check(self) -> dict:
        """技能生态健康检查: 触发碰撞检测 (同名多目录) + 过期技能扫描
        (draft 未转正 / 长时间未更新) + 完整性 (缺 SKILL.md 的孤儿目录)。

        返回 {"collisions": [...], "stale": [...], "orphans": [...],
        "total": n, "healthy": bool}。供 UI 技能页与周期巡检展示。
        """
        import time as _t

        collisions: list[dict] = []
        stale: list[dict] = []
        orphans: list[dict] = []
        seen: dict[str, list[str]] = {}
        total = 0
        now = _t.time()

        # 1) 触发碰撞检测: 同名技能出现在多个目录 → 后者覆盖前者 (隐患)
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for sub in sorted(directory.iterdir()):
                md = sub / "SKILL.md"
                if md.is_file():
                    total += 1
                    name = _parse_skill(md).name
                    seen.setdefault(name, []).append(str(sub))
                elif sub.is_dir() and not sub.name.startswith("."):
                    # 2) 完整性: 目录存在但缺 SKILL.md → 孤儿目录
                    orphans.append({"path": str(sub), "reason": "missing SKILL.md"})
        for name, paths in seen.items():
            if len(paths) > 1:
                collisions.append({"name": name, "paths": paths})

        # 3) 过期技能扫描: draft 状态或 SKILL.md 久未更新 (>180 天)
        for skill in self._skills.values():
            if skill.path is None:
                continue
            md = Path(skill.path) / "SKILL.md"
            age_days = 0.0
            try:
                age_days = (now - md.stat().st_mtime) / 86400.0
            except OSError:
                pass
            if getattr(skill, "draft", False) or age_days > 180:
                stale.append(
                    {
                        "name": skill.name,
                        "draft": bool(getattr(skill, "draft", False)),
                        "age_days": round(age_days, 1),
                    }
                )

        return {
            "collisions": collisions,
            "stale": stale,
            "orphans": orphans,
            "total": total,
            "healthy": not collisions and not orphans,
        }

    def detail(self, name: str) -> Optional[dict]:
        """Full catalog row (metadata only — no instructions body)."""
        skill = self.get(name)
        return skill.catalog_row() if skill else None

    # -- Skill.lock 辅助 --------------------------------------------------
    def generate_lock_for(self, name: str) -> Optional[dict]:
        """对已存在的 skill (重新) 生成 skill.lock。返回 lock data。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        schemas = self._registry_tool_schemas() if self._registry_tool_schemas else None
        _, data = _skill_lock.auto_generate_lock(
            skill.path,
            skill_name=skill.name,
            skill_version=skill.version,
            registry_tool_schemas=schemas,
        )
        self.refresh()
        return data

    def lock_info(self, name: str) -> Optional[dict]:
        """某 skill 的 skill.lock 详情: {lock_exists, lock, integrity_ok, mismatched/missing_scripts, lock_path}。"""
        skill = self.get(name)
        if skill is None:
            return None
        lock_meta = skill.lock_meta
        lock_exists = lock_meta is not None
        lock_path = None
        if skill.path is not None:
            p = skill.path / "skill.lock"
            if p.exists():
                lock_path = str(p)
        mismatched: list[dict] = []
        missing: list[str] = []
        unregistered: list[str] = []
        integrity_ok = True
        if skill.scripts_integrity is not None and lock_exists:
            for item in skill.scripts_integrity:
                status = item.get("status")
                if status == "mismatch":
                    mismatched.append({
                        "path": item.get("path", ""),
                        "expected": item.get("expected_sha256", item.get("expected", "")),
                        "actual": item.get("actual_sha256", item.get("actual", "")),
                    })
                    integrity_ok = False
                elif status == "missing":
                    missing.append(item.get("path", ""))
                    integrity_ok = False
                elif status == "unregistered":
                    # C14: a script present on disk but not in the lock is a
                    # tamper signal too — count it against integrity.
                    unregistered.append(item.get("path", ""))
                    integrity_ok = False
        return {
            "lock": lock_meta,
            "lock_exists": lock_exists,
            "lock_path": lock_path,
            "integrity_ok": integrity_ok if (skill.scripts_integrity is not None and lock_exists) else None,
            "mismatched_scripts": mismatched,
            "missing_scripts": missing,
            "unregistered_scripts": unregistered,
            "scripts_integrity": skill.scripts_integrity,
        }

    # -- 安全评分 辅助 ----------------------------------------------------
    def security_report(self, name: str) -> Optional[dict]:
        """返回某 skill 的完整安全报告 (score + level + findings + recommendation)。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        try:
            cached = getattr(skill, "_security_report_cache", None)
            if cached is not None:
                return cached
        except Exception:
            pass
        report = _skill_security.analyze_skill_dir(skill.path)
        try:
            skill._security_report_cache = report  # type: ignore[attr-defined]
        except Exception:
            pass
        return report

    def rescan_security(self, name: str) -> Optional[dict]:
        """强制重新扫描安全评分。"""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        try:
            delattr(skill, "_security_report_cache")
        except Exception:
            pass
        rep = _skill_security.analyze_skill_dir(skill.path)
        skill.security_score = rep["score"]
        try:
            skill._security_report_cache = rep  # type: ignore[attr-defined]
        except Exception:
            pass
        return rep

    # -- 兼容性测试 辅助 --------------------------------------------------
    def verify_compatibility(self, name: str, current_tools: list[dict]) -> Optional[dict]:
        """对比 lock 和当前工具签名, 返回 {has_lock, compatible, missing/changed/new, recommendation}。"""
        from .compatibility import check_skill_compatibility

        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        return check_skill_compatibility(skill.path, current_tools)


def _zip_root(names: list[str]) -> str:
    """The common top-level prefix of zip entries ('' when files sit at the root)."""
    tops = {n.split("/", 1)[0] for n in names if n and not n.endswith("/")}
    if len(tops) == 1 and all(n.startswith(next(iter(tops)) + "/") for n in names if n):
        return next(iter(tops)) + "/"
    return ""


# M5: skill-import zip-bomb bounds — a skill zip is a handful of scripts and a
# markdown file; anything larger is pathological and must be rejected.
_IMPORT_MAX_ENTRIES = 500
_IMPORT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB decompressed


def _is_under(name: str, prefix: str) -> bool:
    if not prefix:
        return True
    if ".." in name.split("/"):
        return False
    return name == prefix.rstrip("/") or name.startswith(prefix)


def _parse_skill(md: Path) -> Skill:
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
    version, category, author, tags, updated_at = "0.1.0", "general", "", [], None
    draft, source = False, "manual"
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[3:end]
            body = text[end + 4 :].lstrip("\n")
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key, value = key.strip().lower(), value.strip()
                if key == "name" and value:
                    cleaned = re.sub(r"[^\w\-.]", "_", value).strip("_").strip(".")
                    if cleaned and cleaned not in (".", ".."):
                        name = cleaned
                elif key == "description":
                    description = value
                elif key in ("allowed-tools", "allowed_tools"):
                    allowed = [t.strip() for t in value.split(",") if t.strip()]
                elif key == "version":
                    version = value or "0.1.0"
                elif key == "category":
                    category = value or "general"
                elif key == "author":
                    author = value
                elif key == "tags":
                    tags = [t.strip() for t in value.split(",") if t.strip()]
                elif key in ("updated_at", "updated-at"):
                    updated_at = value or None
                elif key == "draft":
                    draft = value.lower() in ("true", "yes", "1")
                elif key == "source":
                    source = value or "manual"
    return Skill(
        name=name,
        description=description,
        instructions=body.strip(),
        path=str(md.parent),
        allowed_tools=allowed,
        version=version,
        category=category,
        author=author,
        tags=tags,
        updated_at=updated_at,
        draft=draft,
        source=source,
    )


def skill_catalog_text(loader: SkillLoader) -> str:
    catalog = loader.catalog()
    if not catalog:
        return ""
    lines = [f"- {c['name']}: {c['description']}" for c in catalog]
    return (
        "Available skills — call load_skill(name) to load one's full instructions when "
        "it's relevant to the task:\n" + "\n".join(lines)
    )


def skill_tools(loader: SkillLoader) -> list:
    def load_skill(name: str) -> dict:
        """Load a skill's full instructions + resources path by name. Call this when a
        skill from the catalog is relevant to the current task."""
        # Re-scan so skills imported via the API or written by another engine become
        # visible to this running session without a restart (fresh loader each call).
        loader.refresh()
        skill = loader.get(name)
        if skill is None:
            return {"error": f"unknown skill: {name}", "available": loader.names()}
        result: dict = {
            "name": skill.name,
            "instructions": skill.instructions,
            "resources_path": skill.path,
        }
        if skill.allowed_tools:
            # Advisory tool policy, surfaced in the loaded instructions: the skill author
            # declares which tools this skill needs; the model is steered to stick to them.
            # Not a hard security boundary — real tool gating stays in the permission engine.
            result["allowed_tools"] = skill.allowed_tools
            result["instructions"] = (
                f"{skill.instructions}\n\n"
                f"Tool policy (declared by this skill): use ONLY the following tools — "
                f"{', '.join(skill.allowed_tools)}. If the task genuinely needs something "
                f"else, explain why instead of reaching for it.\n"
            )
        return result

    def create_skill(name: str, description: str, body: str) -> dict:
        """Create a reusable skill and register it in the system catalog on the fly.

        Use this when you hit a missing capability or a recurring workaround (e.g. a
        tool misbehaving, a PowerShell quoting trick, a parsing helper, a project
        convention). Write the skill as markdown with a short 'how to' body; it is
        saved to the workspace/local skill catalog and becomes immediately loadable
        via load_skill in this and future runs.
        """
        name = name.strip()
        if not name or not body:
            return {"error": "name and body are required"}
        path = loader.save_skill(name, description or name, body)
        return {
            "ok": True,
            "name": name,
            "path": str(path),
            "note": "skill created and registered — load_skill('name') now resolves it",
        }

    return [
        ai.tool(
            load_skill,
            metadata=ai.ToolMetadata(
                category="skills", risk_level="low", capabilities=["load_skill"]
            ),
        ),
        ai.tool(
            create_skill,
            metadata=ai.ToolMetadata(
                category="skills", risk_level="medium", capabilities=["create_skill"]
            ),
        ),
    ]
