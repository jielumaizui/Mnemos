import json

from scripts.review_curator import ReviewCurator


def test_curate_records_conflicts_with_on_curated_items(tmp_path, capsys):
    findings = [
        {
            "id": "R1",
            "severity": "warning",
            "dimension": "logic",
            "file": "core/search.py",
            "title": "拆分搜索函数",
            "description": "函数过长。",
            "suggestion": "建议拆分为小函数",
            "effort": "s",
        },
        {
            "id": "R2",
            "severity": "warning",
            "dimension": "logic",
            "file": "core/search.py",
            "title": "合并搜索函数",
            "description": "调用链分散。",
            "suggestion": "建议合并为大函数",
            "effort": "s",
        },
    ]
    input_path = tmp_path / "findings.json"
    input_path.write_text(json.dumps(findings), encoding="utf-8")

    curator = ReviewCurator()
    report = curator.curate(str(input_path))
    capsys.readouterr()

    items_by_id = {item.id: item for item in curator.items}
    assert items_by_id["R1"].conflicts_with == ["R2"]
    assert items_by_id["R2"].conflicts_with == ["R1"]
    assert "- **冲突**: 与 R2 冲突" in report


def test_curated_contract_fields_restore_priority_score_for_report_input():
    findings = [
        {
            "id": "R1",
            "severity": "critical",
            "dimension": "logic",
            "file": "core/config.py",
            "title": "修复配置风险",
            "description": "配置风险需要优先处理。",
            "suggestion": "建议调大保护阈值",
            "effort": "xs",
        }
    ]

    curator = ReviewCurator()
    sorted_findings = curator.sort_findings(findings)
    curator.items = curator._build_curated_items(sorted_findings, conflicts={})
    sorted_findings[0].pop("priority_score")

    curator._attach_curated_contract_fields(sorted_findings)

    assert curator.items[0].priority_score == 280.0
    assert sorted_findings[0]["priority_score"] == 280.0


def test_curated_contract_fields_restore_related_findings_for_report_input():
    findings = [
        {
            "id": "R1",
            "severity": "warning",
            "dimension": "logic",
            "file": "core/search.py",
            "title": "合并重复发现",
            "description": "多个审查维度指向同一问题。",
            "suggestion": "保留主发现并展示关联发现",
            "effort": "s",
            "related_findings": ["R2", "R3"],
        }
    ]

    curator = ReviewCurator()
    sorted_findings = curator.sort_findings(findings)
    curator.items = curator._build_curated_items(sorted_findings, conflicts={})
    sorted_findings[0].pop("related_findings")

    curator._attach_curated_contract_fields(sorted_findings)

    assert curator.items[0].related_ids == ["R2", "R3"]
    assert sorted_findings[0]["related_findings"] == ["R2", "R3"]
