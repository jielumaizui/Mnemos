from __future__ import annotations

from pathlib import Path

from core.privacy.redaction import (
    redact_key_source,
    redact_path,
    redact_sensitive_data,
    redact_text,
    redact_url,
)


def test_redact_url_keeps_endpoint_shape():
    assert redact_url("https://api.siliconflow.cn/v1") == "https://****/v1"
    assert redact_url("https://gateway.example.test/v1/rerank") == "https://****/v1/rerank"


def test_redact_path_replaces_home_and_repo():
    home_path = Path.home() / ".mnemos" / "config.json"

    assert redact_path(home_path).startswith("<HOME>/")
    assert redact_path(Path.cwd() / "README.md").startswith("<REPO>/")


def test_redact_text_removes_user_paths_urls_and_key_refs():
    home_db_path = "/" + "Users/alice/.mnemos/raw_events.db"
    text = (
        f"send https://api.openai.com/v1 and {home_db_path} "
        "plus /tmp/mnemos/config.json "
        "with keyring:mnemos-db"
    )

    redacted = redact_text(text)

    assert "api.openai.com" not in redacted
    assert "/" + "Users/alice" not in redacted
    assert "/tmp/mnemos" not in redacted
    assert "keyring:mnemos-db" not in redacted
    assert "https://****/v1" in redacted
    assert "<HOME>/.mnemos/raw_events.db" in redacted
    assert "<PATH>/config.json" in redacted
    assert "keyring:****" in redacted


def test_redact_sensitive_data_uses_field_context():
    payload = {
        "base_url": "https://api.siliconflow.cn/v1",
        "endpoint_status": "skipped",
        "plain_path": "/" + "Users/alice/.mnemos/raw_events.db",
        "key_source": "env:MNEMOS_DB_KEY",
        "nested": [{"source": "keyring:mnemos-db"}],
    }

    redacted = redact_sensitive_data(payload)

    assert redacted["base_url"] == "https://****/v1"
    assert redacted["endpoint_status"] == "skipped"
    assert redacted["plain_path"] == "<HOME>/.mnemos/raw_events.db"
    assert redacted["key_source"] == "env:****"
    assert redacted["nested"][0]["source"] == "keyring:****"


def test_redact_sensitive_data_redacts_paths_embedded_in_backup_reference_text():
    local_path = "/" + "Users/alice/.mnemos/backups/state.db"
    payload = {"backup_ref": f'[{{"path": "{local_path}"}}]'}

    redacted = redact_sensitive_data(payload)

    assert local_path not in redacted["backup_ref"]
    assert "<HOME>/.mnemos/backups/state.db" in redacted["backup_ref"]


def test_redact_key_source_leaves_non_references():
    assert redact_key_source("missing") == "missing"
    assert redact_key_source("env:MNEMOS_LLM_API_KEY") == "env:****"
