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

    def save_skill(self, name: str, description: str, body: str) -> Path:
        """Write a new skill to the FIRST writable dir (workspace-local preferred)
        and refresh the catalog so it is immediately loadable."""
        name = re.sub(r"[^\w\-.]", "_", name).strip("_") or "skill"
        target = next((d for d in self._dirs if self._writable(d)), self._dirs[-1])
        skill_dir = target / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        md = skill_dir / "SKILL.md"
        md.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
            encoding="utf-8",
        )
        self.refresh()
        return md

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
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]


def _parse_skill(md: Path) -> Skill:
    text = md.read_text(encoding="utf-8")
    name, description, allowed, body = md.parent.name, "", [], text
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
    return Skill(
        name=name,
        description=description,
        instructions=body.strip(),
        path=str(md.parent),
        allowed_tools=allowed,
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
