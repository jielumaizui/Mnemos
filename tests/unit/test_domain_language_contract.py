from core.system_contracts import DOMAIN_TERMS, audit_domain_glossary


def test_domain_glossary_is_strictly_valid():
    assert audit_domain_glossary(strict=True) == []


def test_skill_flywheel_is_deprecated_alias_only():
    term = DOMAIN_TERMS["cognitive_decision_flywheel"]

    assert "Skill 飞轮" in term.deprecated_aliases
    assert "Automation skills are optional derivatives" in term.migration_note
