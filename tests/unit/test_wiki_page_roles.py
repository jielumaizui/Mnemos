"""Exact role classification for lifecycle-owned derived Wiki pages."""

from core.wiki_page_roles import classify_wiki_page_role, source_exempt_reason


def test_explicit_derived_role_precedes_legacy_root_classification():
    content = """---
projection_schema: "mnemos.derived_projection.v1"
page_role: "formal_derived:observation"
---

# Observation
"""

    role = classify_wiki_page_role(content, "L3-Observations/decisions.md")

    assert role == "formal_derived:observation"
    assert source_exempt_reason("L3-Observations/decisions.md", role) == role


def test_explicit_report_role_is_preserved():
    content = """---
page_role: derived_report:reflection_weekly
---

# Weekly
"""

    assert classify_wiki_page_role(
        content,
        "L4-Reflections/Reports/weekly-2026-07-21.md",
    ) == "derived_report:reflection_weekly"
