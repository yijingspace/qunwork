"""Skill marketplace backend tests — metadata, CRUD, zip export/import, stats."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from coworker.skills.base import SkillLoader
from coworker.skills.market import SkillMarketStore


@pytest.fixture()
def loader(tmp_path: Path) -> SkillLoader:
    return SkillLoader([tmp_path / "skills"])


# -- metadata & CRUD --------------------------------------------------------


def test_save_skill_writes_extended_frontmatter(loader: SkillLoader):
    md = loader.save_skill(
        "pdf-tools",
        "Extract text from PDFs",
        "Use pypdf...",
        version="1.2.0",
        category="document",
        author="qunwork",
        tags=["pdf", "extract"],
        allowed_tools=["shell", "read_file"],
    )
    text = md.read_text(encoding="utf-8")
    assert "version: 1.2.0" in text
    assert "category: document" in text
    assert "author: qunwork" in text
    assert "tags: pdf, extract" in text
    assert "allowed-tools: shell, read_file" in text

    skill = loader.get("pdf-tools")
    assert skill is not None
    assert skill.version == "1.2.0"
    assert skill.category == "document"
    assert skill.author == "qunwork"
    assert skill.tags == ["pdf", "extract"]
    assert skill.allowed_tools == ["shell", "read_file"]


def test_update_skill_patches_frontmatter_in_place(loader: SkillLoader):
    loader.save_skill("s", "old desc", "old body", version="0.1.0")
    md = loader.update_skill("s", description="new desc", version="0.2.0")
    assert md is not None
    text = md.read_text(encoding="utf-8")
    assert "description: new desc" in text
    assert "version: 0.2.0" in text
    assert "old body" in text  # body untouched


def test_delete_skill_removes_folder(loader: SkillLoader, tmp_path: Path):
    loader.save_skill("gone", "d", "b")
    assert loader.get("gone") is not None
    assert loader.delete_skill("gone") is True
    assert loader.get("gone") is None
    assert loader.delete_skill("gone") is False


def test_catalog_contains_extended_fields(loader: SkillLoader):
    loader.save_skill("a", "desc A", "body", version="2.0.0", category="dev", author="me")
    row = loader.catalog()[0]
    assert row == {
        "name": "a",
        "description": "desc A",
        "version": "2.0.0",
        "category": "dev",
        "author": "me",
        "tags": [],
        "updated_at": None,
    }


# -- export / import --------------------------------------------------------


def test_export_import_zip_roundtrip(loader: SkillLoader, tmp_path: Path):
    loader.save_skill(
        "zip-skill",
        "Zippy",
        "body text",
        version="0.3.0",
        category="tools",
        author="author",
        tags=["zip"],
    )
    # add a resource file inside the skill folder
    from pathlib import Path as P

    P(loader.get("zip-skill").path).joinpath("helper.py").write_text("print(1)", encoding="utf-8")

    zip_path = tmp_path / "out" / "zip-skill.zip"
    assert loader.export_skill("zip-skill", zip_path) is not None
    assert zip_path.exists()

    # install into a fresh loader
    fresh = SkillLoader([tmp_path / "fresh"])
    skill = fresh.import_skill(zip_path)
    assert skill is not None
    assert skill.name == "zip-skill"
    assert skill.version == "0.3.0"
    assert skill.category == "tools"
    assert skill.author == "author"
    assert skill.tags == ["zip"]
    assert (Path(skill.path) / "helper.py").exists()
    assert "body text" in skill.instructions


def test_import_rejects_zip_without_skill_md(loader: SkillLoader, tmp_path: Path):
    bogus = tmp_path / "bogus.zip"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("random.txt", "not a skill")
    assert loader.import_skill(bogus) is None


# -- market stats -----------------------------------------------------------


def test_market_store_install_and_rating(tmp_path: Path):
    store = SkillMarketStore(tmp_path / "market.db")
    assert store.stats("nope")["install_count"] == 0

    store.record_install("tool-a")
    store.record_install("tool-a")
    assert store.stats("tool-a")["install_count"] == 2

    store.rate("tool-a", 4)
    store.rate("tool-a", 5)
    st = store.stats("tool-a")
    assert st["rating"] == 4.5
    assert st["rating_count"] == 2

    # rating clamps to 1..5
    store.rate("tool-a", 99)
    assert store.stats("tool-a")["rating_count"] == 3

    all_stats = store.all_stats()
    assert "tool-a" in all_stats
    store.close()
