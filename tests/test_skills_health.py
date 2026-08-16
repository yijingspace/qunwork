"""S13 技能市场与健康检查常态化 (蜂群审计报告 G8).

契约:
  * 碰撞检测: 同名技能出现在多个目录 → 报告 (后者覆盖前者隐患);
  * 完整性: 有目录但缺 SKILL.md → 孤儿目录报告;
  * 过期扫描: draft 状态或 SKILL.md 久未更新 (>180 天) → 报告;
  * healthy = 无碰撞 + 无孤儿。
"""

from __future__ import annotations

import time

from coworker.skills.base import SkillLoader


def test_health_check_clean(tmp_path):
    d1 = tmp_path / "skills1"
    d1.mkdir()
    loader = SkillLoader([d1])
    loader.save_skill("good-skill", "desc", "body", version="1.0.0")
    h = loader.health_check()
    assert h["healthy"] is True
    assert h["collisions"] == [] and h["orphans"] == []


def test_health_check_collision(tmp_path):
    d1 = tmp_path / "skills1"
    d2 = tmp_path / "skills2"
    d1.mkdir()
    d2.mkdir()
    loader = SkillLoader([d1, d2])
    loader.save_skill("dup", "desc", "v1")
    # 第二个目录同名技能
    (d2 / "dup").mkdir(parents=True)
    (d2 / "dup" / "SKILL.md").write_text(
        "---\nname: dup\ndescription: another\n---\n\nbody", encoding="utf-8"
    )
    loader.refresh()
    h = loader.health_check()
    assert h["healthy"] is False
    assert any(c["name"] == "dup" for c in h["collisions"])


def test_health_check_orphan_dir(tmp_path):
    d1 = tmp_path / "skills1"
    d1.mkdir()
    # 孤儿目录: 无 SKILL.md
    (d1 / "broken-skill").mkdir()
    loader = SkillLoader([d1])
    h = loader.health_check()
    assert h["healthy"] is False
    assert any("broken-skill" in str(o["path"]) for o in h["orphans"])


def test_health_check_draft_flagged(tmp_path):
    d1 = tmp_path / "skills1"
    d1.mkdir()
    loader = SkillLoader([d1])
    # 用 save_skill 的 draft 参数创建草稿技能
    loader.save_skill("draft-skill", "desc", "body", draft=True)
    h = loader.health_check()
    stale_names = [s["name"] for s in h["stale"]]
    assert "draft-skill" in stale_names  # draft 状态被标记


def test_health_check_stale_by_age(tmp_path):
    d1 = tmp_path / "skills1"
    d1.mkdir()
    loader = SkillLoader([d1])
    loader.save_skill("old-skill", "desc", "body")
    md = d1 / "old-skill" / "SKILL.md"
    old = time.time() - 200 * 86400  # 200 天前
    import os

    os.utime(md, (old, old))
    h = loader.health_check()
    assert any(s["name"] == "old-skill" and s["age_days"] > 180 for s in h["stale"])
