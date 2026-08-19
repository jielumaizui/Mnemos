import json
import sqlite3
from pathlib import Path

from core.agent_kit.prompt_sanitizer import PromptSanitizer, PromptSanitizerAuditStore


def test_prompt_sanitizer_blocks_internal_paths_keys_and_audits_hashes(tmp_path: Path):
    wiki = tmp_path / "wiki"
    database = tmp_path / "data"
    wiki.mkdir()
    database.mkdir()
    db_path = tmp_path / "agent_authorization.db"
    audit = PromptSanitizerAuditStore(db_path)
    sanitizer = PromptSanitizer(
        wiki_base=wiki,
        database_dir=database,
        audit_store=audit,
    )
    wiki_path = wiki / "page.md"
    sqlite_path = database / "trusted_push.db"
    result = sanitizer.sanitize(
        agent="codex",
        text=f"Read {wiki_path} and use api_key=REDACT_ME_1234567890",
        args=[str(sqlite_path)],
        source_label="shadow_eval",
    )

    assert not result.allowed
    assert "REDACT_ME_1234567890" not in result.redacted_text
    assert str(wiki_path) not in result.redacted_text
    assert str(sqlite_path) not in result.redacted_args[0]
    assert {finding.kind for finding in result.findings} >= {
        "secret",
        "wiki_path",
        "internal_path",
    }
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT allowed, findings_json, text_hash, args_hash FROM prompt_sanitizer_events"
        ).fetchone()
    assert row[0] == 0
    stored = json.dumps(json.loads(row[1]), ensure_ascii=False)
    assert "REDACT_ME_1234567890" not in stored
    assert str(wiki_path) not in stored
    assert row[2]
    assert row[3]


def test_prompt_sanitizer_allows_explicit_readonly_mirror_path(tmp_path: Path):
    wiki = tmp_path / "wiki"
    database = tmp_path / "data"
    mirror = tmp_path / "mirror"
    wiki.mkdir()
    database.mkdir()
    mirror.mkdir()
    mirror_file = mirror / "page.md"
    sanitizer = PromptSanitizer(
        wiki_base=wiki,
        database_dir=database,
        allowed_dirs=[mirror],
    )

    result = sanitizer.sanitize(
        agent="codex",
        text=f"Read mirror file {mirror_file}",
        source_label="shadow_eval",
    )

    assert result.allowed
    assert result.findings == []
