"""Skill loading — Anthropic SKILL.md format with progressive disclosure.

A skill is a folder containing `SKILL.md` (YAML frontmatter: name, description,
optional allowed-tools) + a markdown body of instructions + optional resources/scripts.

Progressive disclosure: at session start only the catalog (name + description) is injected
into the agent's context; the full body is loaded on demand via the `load_skill` tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aisuite as ai


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

    def catalog_row(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "category": self.category,
            "author": self.author,
            "tags": self.tags,
            "updated_at": self.updated_at,
        }


class SkillLoader:
    def __init__(self, dirs: list[str | Path]) -> None:
        self._dirs = [Path(d) for d in dirs]
        self._skills: dict[str, Skill] = {}
        self.refresh()

    def refresh(self) -> None:
        """(Re)scan every skill directory — call after saving a new skill so it is
        immediately available to the running engine."""
        self._skills.clear()
        for directory in self._dirs:
            self._discover(directory)

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
    ) -> Path:
        """Write a new skill to the FIRST writable dir (workspace-local preferred)
        and refresh the catalog so it is immediately loadable."""
        name = re.sub(r"[^\w\-.]", "_", name).strip("_") or "skill"
        target = next((d for d in self._dirs if self._writable(d)), self._dirs[-1])
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
        md.write_text(f"---\n" + "\n".join(lines) + "\n---\n\n" + body + "\n", encoding="utf-8")
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
        path, or None when the skill does not exist)."""
        skill = self.get(name)
        if skill is None or skill.path is None:
            return None
        md = Path(skill.path) / "SKILL.md"
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
        self.refresh()
        return md

    def delete_skill(self, name: str) -> bool:
        """Remove a skill folder entirely (all dirs; returns True when something was
        deleted)."""
        removed = False
        for directory in self._dirs:
            target = directory / name
            if (target / "SKILL.md").is_file():
                import shutil

                shutil.rmtree(target, ignore_errors=True)
                removed = True
        if removed:
            self.refresh()
        return removed

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
                if file.is_file():
                    zf.write(file, file.relative_to(root).as_posix())
        return dest

    def import_skill(self, zip_path: Path, *, target_dir: Optional[Path] = None) -> Optional[Skill]:
        """Install a skill from a zip (validates SKILL.md at the zip root or a single
        top-level folder). Returns the installed Skill, or None on invalid zip."""
        import zipfile

        target_dir = target_dir or next(
            (d for d in self._dirs if self._writable(d)), self._dirs[-1]
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            top = _zip_root(names)
            skill_md = next((n for n in names if n.endswith("SKILL.md") and _is_under(n, top)), None)
            if skill_md is None:
                return None
            skill_name = Path(skill_md).parent.name
            if not skill_name or skill_name == ".":
                # SKILL.md sits at the zip root — take its name from the frontmatter.
                fm_text = zf.read(skill_md).decode("utf-8", "replace")
                m = re.search(r"^name:\s*(.+)$", fm_text, re.M)
                skill_name = m.group(1).strip() if m else zip_path.stem
            skill_name = re.sub(r"[^\w\-.]", "_", skill_name).strip("_") or "skill"
            out_dir = target_dir / skill_name
            for name in names:
                if not _is_under(name, top):
                    continue
                rel = name[len(top) :].lstrip("/")
                if not rel:
                    continue
                target = out_dir / rel
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
        self.refresh()
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
                self._skills[skill.name] = skill

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def catalog(self) -> list[dict]:
        return [s.catalog_row() for s in self._skills.values()]

    def detail(self, name: str) -> Optional[dict]:
        """Full catalog row (metadata only — no instructions body)."""
        skill = self.get(name)
        return skill.catalog_row() if skill else None


def _zip_root(names: list[str]) -> str:
    """The common top-level prefix of zip entries ('' when files sit at the root)."""
    tops = {n.split("/", 1)[0] for n in names if n and not n.endswith("/")}
    if len(tops) == 1 and all(n.startswith(next(iter(tops)) + "/") for n in names if n):
        return next(iter(tops)) + "/"
    return ""


def _is_under(name: str, prefix: str) -> bool:
    if not prefix:
        return True
    return name == prefix.rstrip("/") or name.startswith(prefix)


def _parse_skill(md: Path) -> Skill:
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
    version, category, author, tags, updated_at = "0.1.0", "general", "", [], None
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
                    name = value
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
        skill = loader.get(name)
        if skill is None:
            return {"error": f"unknown skill: {name}", "available": loader.names()}
        return {
            "name": skill.name,
            "instructions": skill.instructions,
            "resources_path": skill.path,
        }

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
