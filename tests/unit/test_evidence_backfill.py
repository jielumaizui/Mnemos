import argparse
import json
import sqlite3
from types import SimpleNamespace


def _write_page(wiki_dir, rel_path, frontmatter="---\n名称: 测试页面\n---\n\n# Body\n"):
    path = wiki_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter, encoding="utf-8")
    return path


def _create_knowledge_graph_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE document_wiki_link (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                source TEXT DEFAULT '',
                wiki_page_path TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                confidence REAL DEFAULT 0.5
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE relation_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relation_id INTEGER NOT NULL,
                evidence_type TEXT NOT NULL,
                content TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO document_wiki_link (session_id, source, wiki_page_path)
            VALUES (?, ?, ?)
            """,
            [
                ("sess-doc", "source.pdf", "00-Inbox/doc-page.md"),
                ("sess-doc", "source.pdf", "00-Inbox/doc-page.md"),
            ],
        )
        cur = conn.execute(
            """
            INSERT INTO relations (source, target, relation_type, confidence)
            VALUES (?, ?, ?, ?)
            """,
            ("00-Inbox/doc-page", "00-Inbox/relation-page", "co_occurs", 0.8),
        )
        relation_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO relation_evidence (relation_id, evidence_type, content)
            VALUES (?, ?, ?)
            """,
            (relation_id, "distill_extraction", "蒸馏提取出的关系证据"),
        )


def _create_distill_queue_db(path, wiki_page, missing_raw_page):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE distillation_tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                output_path TEXT,
                meta TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO distillation_tasks (task_id, session_id, status, output_path, meta)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "task-1",
                "sess-distill",
                "done",
                str(wiki_page),
                json.dumps({"raw_event_refs": ["raw-1", "raw-2"]}),
            ),
        )
        conn.execute(
            """
            INSERT INTO distillation_tasks (task_id, session_id, status, output_path, meta)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("task_missing_raw", "sess-no-raw", "done", str(missing_raw_page), "{}"),
        )


def _fake_config(tmp_path, *, extra=None):
    database_dir = tmp_path / ".mnemos"
    wiki_dir = tmp_path / "wiki"
    database_dir.mkdir()
    wiki_dir.mkdir()
    _write_page(wiki_dir, "00-Inbox/doc-page.md")
    _write_page(wiki_dir, "00-Inbox/relation-page.md")
    _write_page(wiki_dir, "00-Inbox/gap-page.md")
    _write_page(
        wiki_dir,
        "00-Inbox/legacy-frontmatter-page.md",
        (
            "---\n"
            "名称: 历史蒸馏页面\n"
            "来源: hermes\n"
            "来源会话: session_legacy_123\n"
            "来源事件ID:\n"
            "  - raw-legacy-1\n"
            "蒸馏时间: '2026-06-23 08:02:48'\n"
            "---\n\n# Body\n"
        ),
    )

    from core.wiki_metrics import WikiMetrics

    metrics = WikiMetrics(
        db_path=str(database_dir / "wiki_metrics.db"),
        wiki_dir=str(wiki_dir),
    )
    metrics.upsert_page("00-Inbox/doc-page.md", source_count=0, source_refs=[])
    metrics.upsert_page("00-Inbox/relation-page.md", source_count=0, source_refs=[])
    metrics.upsert_page("00-Inbox/gap-page.md", source_count=0, source_refs=[])
    metrics.upsert_page(
        "00-Inbox/legacy-frontmatter-page.md",
        source_count=0,
        source_refs=[],
    )

    _create_knowledge_graph_db(database_dir / "knowledge_graph.db")
    _create_distill_queue_db(
        database_dir / "distill_queue.db",
        wiki_dir / "00-Inbox/doc-page.md",
        wiki_dir / "00-Inbox/relation-page.md",
    )

    values = {
        "evidence_backfill.max_refs_per_page": 20,
        "evidence_backfill.frontmatter_ref_limit": 10,
        "evidence_backfill.include_relation_evidence": True,
        "evidence_backfill.relation_evidence_types": [
            "anti_pattern_quote",
            "distill_extraction",
        ],
        "evidence_backfill.write_frontmatter": True,
        "evidence_backfill.write_report": True,
        "evidence_backfill.report_dir": "99-Reports/认知数据就绪度",
    }
    values.update(extra or {})

    return SimpleNamespace(
        database_dir=database_dir,
        wiki_dir=wiki_dir,
        obsidian_vault_path=tmp_path / "raw",
        get=lambda key, default=None: values.get(key, default),
    )


def test_evidence_backfill_dry_run_does_not_write(tmp_path):
    from core.ops.evidence_backfill import run_evidence_backfill
    from core.wiki_metrics import WikiMetrics

    config = _fake_config(tmp_path)
    report = run_evidence_backfill(config, apply=False)

    assert report["schema_version"] == "mnemos.evidence_backfill.v1"
    assert report["applied"] is False
    assert report["changed_pages"] == 3
    assert report["unresolved"]["source_count_zero"] == 1
    assert report["report_path"] == ""
    assert report["sources"]["document_wiki_link"]["refs"] == 2
    assert report["sources"]["distillation_tasks"]["refs"] == 4
    assert report["sources"]["relation_evidence"]["refs"] == 2
    assert report["sources"]["frontmatter"]["refs"] == 3
    assert report["diagnostics"]["distill_missing_raw_event_refs"]["count"] == 1

    metrics = WikiMetrics(
        db_path=str(config.database_dir / "wiki_metrics.db"),
        wiki_dir=str(config.wiki_dir),
    )
    assert metrics.get_page("00-Inbox/doc-page.md").source_refs == []
    assert "证据引用" not in (config.wiki_dir / "00-Inbox/doc-page.md").read_text(
        encoding="utf-8"
    )


def test_evidence_backfill_apply_updates_metrics_and_frontmatter(
    tmp_path,
):
    from core.ops.evidence_backfill import run_evidence_backfill
    from core.wiki_metrics import WikiMetrics, compute_evidence_level

    config = _fake_config(tmp_path)
    report = run_evidence_backfill(config, apply=True)

    assert report["applied"] is True
    assert report["changed_pages"] == 3
    assert report["report_path"]
    doc_change = next(
        item for item in report["changes"] if item["wiki_path"] == "00-Inbox/doc-page.md"
    )
    assert doc_change["after_source_count"] == 6

    metrics = WikiMetrics(
        db_path=str(config.database_dir / "wiki_metrics.db"),
        wiki_dir=str(config.wiki_dir),
    )
    page = metrics.get_page("00-Inbox/doc-page.md")
    assert page.source_count == 6
    assert page.evidence_level == compute_evidence_level(6)
    assert "document_wiki_link:session:sess-doc" in page.source_refs
    assert "document_wiki_link:source:source.pdf" in page.source_refs
    assert "distill_task:task-1:session:sess-distill" in page.source_refs
    assert "kg_relation:1:distill_extraction" in page.source_refs
    assert "raw_event:raw-1" in page.source_refs
    assert "raw_event:raw-2" in page.source_refs
    legacy = metrics.get_page("00-Inbox/legacy-frontmatter-page.md")
    assert legacy.source_count == 3
    assert "raw_event:raw-legacy-1" in legacy.source_refs
    assert "frontmatter:source_session:session_legacy_123" in legacy.source_refs
    assert "frontmatter:source_agent:hermes" in legacy.source_refs

    content = (config.wiki_dir / "00-Inbox/doc-page.md").read_text(encoding="utf-8")
    assert "来源数量: 6" in content
    assert "证据级别: 4" in content
    assert "证据引用:" in content

    report_path = config.wiki_dir / "99-Reports/认知数据就绪度"
    report_files = list(report_path.glob("认知数据就绪度-evidence-backfill-*.md"))
    assert len(report_files) == 1
    report_content = report_files[0].read_text(encoding="utf-8")
    assert "evidence_gap" in report_content
    assert "00-Inbox/gap-page.md" in report_content
    assert "distill_task_missing_raw_event_refs" in report_content


def test_evidence_backfill_respects_configured_ref_cap(tmp_path):
    from core.ops.evidence_backfill import run_evidence_backfill
    from core.wiki_metrics import WikiMetrics

    config = _fake_config(
        tmp_path,
        extra={"evidence_backfill.max_refs_per_page": 2},
    )

    report = run_evidence_backfill(config, apply=True)

    doc_change = next(
        item for item in report["changes"] if item["wiki_path"] == "00-Inbox/doc-page.md"
    )
    assert doc_change["after_source_count"] == 2

    metrics = WikiMetrics(
        db_path=str(config.database_dir / "wiki_metrics.db"),
        wiki_dir=str(config.wiki_dir),
    )
    assert len(metrics.get_page("00-Inbox/doc-page.md").source_refs) == 2
    assert report["config"]["max_refs_per_page"] == 2


def test_distill_evidence_backfill_cli_branch(tmp_path, monkeypatch, capsys):
    import mnemos_cli

    config = _fake_config(tmp_path)
    monkeypatch.setattr("core.cli.commands.distill._get_config", lambda: config)

    args = argparse.Namespace(
        distill_cmd="evidence-backfill",
        apply=True,
        json=True,
        limit=None,
        max_refs_per_page=None,
        frontmatter_ref_limit=None,
        change_sample_limit=None,
        relation_evidence_types=None,
        skip_relation_evidence=False,
        no_frontmatter=False,
        no_report=False,
        report_dir=None,
    )

    mnemos_cli.cmd_distill(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["schema_version"] == "mnemos.evidence_backfill.v1"
    assert payload["applied"] is True
    assert payload["changed_pages"] == 3


def test_distill_parser_accepts_evidence_backfill_flags():
    import mnemos_cli

    args = mnemos_cli.build_parser().parse_args(
        [
            "distill",
            "evidence-backfill",
            "--apply",
            "--json",
            "--limit",
            "5",
            "--max-refs-per-page",
            "3",
            "--frontmatter-ref-limit",
            "2",
            "--change-sample-limit",
            "4",
            "--relation-evidence-type",
            "distill_extraction",
            "--skip-relation-evidence",
            "--no-frontmatter",
            "--no-report",
            "--report-dir",
            "99-Reports/custom",
        ]
    )

    assert args.command == "distill"
    assert args.distill_cmd == "evidence-backfill"
    assert args.apply is True
    assert args.limit == 5
    assert args.max_refs_per_page == 3
    assert args.frontmatter_ref_limit == 2
    assert args.change_sample_limit == 4
    assert args.relation_evidence_types == ["distill_extraction"]
    assert args.skip_relation_evidence is True
    assert args.no_frontmatter is True
    assert args.no_report is True
    assert args.report_dir == "99-Reports/custom"
