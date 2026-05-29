from datetime import datetime, timedelta
import sqlite3


def _write_page(path, frontmatter="", body="# Skill Flow\n1. 第一步\n2. 第二步\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_scan_wiki_for_skills_covers_entire_vault(tmp_path):
    from core.kia.ixion import SkillWikiFlywheel

    page = tmp_path / "03-Tech" / "flow.md"
    _write_page(page, "类型: 方法论\n置信度: 0.9\n触发场景:\n  - 重构\n")
    flywheel = SkillWikiFlywheel(wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db"))
    flywheel.log_wiki_usage(str(page), "quote")
    flywheel.log_wiki_usage(str(page), "modify")

    insights = flywheel.scan_wiki_for_skills()

    assert insights
    assert insights[0].source == str(page)


def test_behavior_driven_repeated_tasks_generate_skill_insight(tmp_path):
    from core.kia.ixion import SkillWikiFlywheel

    flywheel = SkillWikiFlywheel(wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db"))
    for _ in range(3):
        flywheel.record_task_completed("coding", "test", ["a.md"])

    insights = flywheel.behavior_generator.analyze()

    assert len(insights) == 1
    assert insights[0].direction == "behavior_to_skill"
    assert insights[0].auto_applicable is True


def test_skill_record_version_fields_and_history_are_persisted(tmp_path):
    from core.kia.ixion import SkillRecord, SkillWikiFlywheel

    flywheel = SkillWikiFlywheel(wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db"))
    flywheel.create_skill(SkillRecord(
        skill_name="RefactorHelper",
        description="desc",
        version=2,
        generation_source="behavior",
        created_by="auto",
    ))

    skill = flywheel.get_skill("RefactorHelper")
    assert skill.version == 2
    assert skill.generation_source == "behavior"
    with sqlite3.connect(tmp_path / "flywheel.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='RefactorHelper'").fetchone()[0] == 1


def test_cleanup_deprecates_behavior_skills_and_archives(tmp_path):
    from core.kia.ixion import SkillRecord, SkillWikiFlywheel

    flywheel = SkillWikiFlywheel(wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db"))
    flywheel.create_skill(SkillRecord(
        skill_name="OldAuto",
        status="auto_generated",
        generation_source="behavior",
        updated_at=(datetime.now() - timedelta(days=90)).isoformat()[:19],
    ))
    with sqlite3.connect(tmp_path / "flywheel.db") as conn:
        conn.execute(
            "UPDATE skills SET updated_at=?, last_used='' WHERE skill_name='OldAuto'",
            ((datetime.now() - timedelta(days=90)).isoformat()[:19],),
        )

    archived = flywheel.cleanup_stale_skills(cleanup_days=60)

    assert archived == ["OldAuto"]
    assert flywheel.get_skill("OldAuto").status == "deprecated"
    assert (tmp_path / "03-Archive" / "Skills" / "OldAuto-归档.md").exists()


def test_run_cycle_uses_metis_fallback_when_persona_unavailable(tmp_path):
    from core.kia.ixion import SkillWikiFlywheel

    _write_page(tmp_path / "03-Tech" / "a.md", "领域: 技术\n类型: 方法论\n复杂度: 入门\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n")
    result = SkillWikiFlywheel(wiki_base=str(tmp_path), db_path=str(tmp_path / "flywheel.db")).run_cycle()

    assert "flywheel_params" in result["persona_driven"]
