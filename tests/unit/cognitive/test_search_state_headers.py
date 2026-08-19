import json
import sqlite3

import pytest

from core.access_policy import AccessNarrowing
from core.cognitive.access_control import (
    cognitive_access_hash,
    make_cognitive_access_envelope,
)
from core.cognitive.search import COGNITIVE_SEARCH_PURPOSES
from core.cognitive.search_state_headers import (
    inspect_state_search_headers,
    reconcile_state_search_headers,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from tests.unit.cognitive.test_cognitive_search import _commit, _principal, _scoped_revision


def _legacy_without_headers(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-reconcile",
        claim_text="state search header reconciliation evidence",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-reconcile")
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER typed_search_state_revision_bindings_no_update")
        conn.execute("DROP TRIGGER typed_search_state_revision_bindings_no_delete")
        conn.execute("DROP TRIGGER typed_search_state_headers_no_update")
        conn.execute("DROP TRIGGER typed_search_state_headers_no_delete")
        conn.execute("DROP TRIGGER typed_search_state_exclusions_no_update")
        conn.execute("DROP TRIGGER typed_search_state_exclusions_no_delete")
        conn.execute("DROP TABLE typed_search_state_headers")
        conn.execute("DROP TABLE typed_search_state_revision_bindings")
        conn.execute("DROP TABLE typed_search_state_exclusions")
        conn.execute("DROP TABLE typed_search_state_header_registry")
        conn.commit()
    return db_path, revision


def _downgrade_projection_to_exact_v3(conn: sqlite3.Connection) -> None:
    bindings = conn.execute(
        "SELECT revision_id, object_type, object_id, scope_type, scope_id, "
        "access_control_hash, revision_payload_hash, created_at "
        "FROM typed_search_state_revision_bindings ORDER BY revision_id"
    ).fetchall()
    conn.executescript("""
        DROP TRIGGER typed_search_state_headers_revision_binding;
        DROP TABLE typed_search_state_revision_bindings;
        CREATE TABLE typed_search_state_revision_bindings (
            revision_id TEXT PRIMARY KEY,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL,
            access_control_hash TEXT NOT NULL CHECK(length(trim(access_control_hash)) > 0),
            revision_payload_hash TEXT NOT NULL CHECK(length(trim(revision_payload_hash)) > 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
                ON DELETE RESTRICT,
            UNIQUE(object_type, object_id, revision_id)
        );
        CREATE TRIGGER typed_search_state_revision_bindings_no_update
        BEFORE UPDATE ON typed_search_state_revision_bindings BEGIN
            SELECT RAISE(ABORT, 'typed search state revision bindings are immutable');
        END;
        CREATE TRIGGER typed_search_state_revision_bindings_no_delete
        BEFORE DELETE ON typed_search_state_revision_bindings BEGIN
            SELECT RAISE(ABORT, 'typed search state revision bindings are immutable');
        END;
        CREATE TRIGGER typed_search_state_revision_bindings_revision_binding
        BEFORE INSERT ON typed_search_state_revision_bindings BEGIN
            SELECT CASE WHEN
                NEW.revision_payload_hash <> COALESCE((
                    SELECT payload_hash FROM cognitive_state_revisions
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.object_type <> COALESCE((
                    SELECT object_type FROM cognitive_state_revisions
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.object_id <> COALESCE((
                    SELECT object_id FROM cognitive_state_revisions
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.scope_type <> COALESCE((
                    SELECT scope_type FROM cognitive_state_revisions
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.scope_id <> COALESCE((
                    SELECT scope_id FROM cognitive_state_revisions
                    WHERE revision_id=NEW.revision_id
                ), '')
            THEN RAISE(ABORT, 'typed search revision binding mismatch') END;
        END;
        CREATE TRIGGER typed_search_state_headers_revision_binding
        BEFORE INSERT ON typed_search_state_headers BEGIN
            SELECT CASE WHEN
                NEW.access_control_hash <> COALESCE((
                    SELECT access_control_hash FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.revision_payload_hash <> COALESCE((
                    SELECT revision_payload_hash FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.object_type <> COALESCE((
                    SELECT object_type FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.object_id <> COALESCE((
                    SELECT object_id FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.scope_type <> COALESCE((
                    SELECT scope_type FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
                OR NEW.scope_id <> COALESCE((
                    SELECT scope_id FROM typed_search_state_revision_bindings
                    WHERE revision_id=NEW.revision_id
                ), '')
            THEN RAISE(ABORT, 'typed search header revision binding mismatch') END;
        END;
        """)
    conn.executemany(
        "INSERT INTO typed_search_state_revision_bindings " "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        bindings,
    )
    conn.execute(
        "UPDATE typed_search_state_header_registry "
        "SET schema_version=?, ddl_hash=? WHERE component=?",
        (
            "mnemos.cognitive_search_state_headers.v3",
            "sha256:dda4e6dad82755e5533ca1bb31f1189a3c216369d85efdff52846e90d4250595",
            "cognitive_search_state_headers",
        ),
    )
    conn.commit()


def test_fresh_state_write_commits_exact_search_header(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-fresh",
        claim_text="fresh state search header evidence",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-fresh")

    with sqlite3.connect(db_path) as conn:
        report = inspect_state_search_headers(conn)
    assert report["ok"] is True
    assert report["revision_count"] == report["header_count"] == 1

    revisions, access = store.authorized_current_revisions_by_purpose(
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        purposes_by_type=COGNITIVE_SEARCH_PURPOSES,
    )
    assert [item.revision_id for item in revisions] == [revision.revision_id]
    assert access["authorized_count"] == 1


def test_mismatched_header_acl_fails_audit_and_runtime_then_reconciles(tmp_path):
    from core.cognitive.search_state_headers import StateSearchHeaderSchemaError

    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-mismatch",
        claim_text="header ACL binding must match the immutable revision",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-mismatch")
    wrong_access = make_cognitive_access_envelope(
        owner_principal_id="attacker",
        owner_agent="state-store-test",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=("forged-header",),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:forged-header",),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TRIGGER typed_search_state_headers_no_update")
        conn.execute(
            "UPDATE typed_search_state_headers "
            "SET access_control=?, access_control_hash=? WHERE revision_id=?",
            (
                json.dumps(wrong_access, sort_keys=True, separators=(",", ":")),
                cognitive_access_hash(wrong_access),
                revision.revision_id,
            ),
        )
        conn.execute(
            "CREATE TRIGGER typed_search_state_headers_no_update "
            "BEFORE UPDATE ON typed_search_state_headers BEGIN "
            "SELECT RAISE(ABORT, 'typed search state headers are immutable'); END"
        )
        conn.commit()
        inspection = inspect_state_search_headers(conn)
        assert inspection["ok"] is False
        assert inspection["hash_mismatch_count"] == 1

    with pytest.raises(StateSearchHeaderSchemaError, match="reconciliation required"):
        store.authorized_current_revisions_by_purpose(
            principal=_principal("attacker"),
            narrowing=AccessNarrowing(project="mnemos"),
            purposes_by_type=COGNITIVE_SEARCH_PURPOSES,
        )

    with sqlite3.connect(db_path) as conn:
        repaired = reconcile_state_search_headers(conn, apply=True)
        assert repaired["after"]["ok"] is True

    revisions, access = store.authorized_current_revisions_by_purpose(
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        purposes_by_type=COGNITIVE_SEARCH_PURPOSES,
    )
    assert [item.revision_id for item in revisions] == [revision.revision_id]
    assert access["authorized_count"] == 1


def test_binding_insert_rejects_acl_not_present_in_canonical_revision(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-forged-pair",
        claim_text="binding insert must prove the canonical revision ACL",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-forged-pair")
    forged_access = make_cognitive_access_envelope(
        owner_principal_id="attacker",
        owner_agent="state-store-test",
        scope_type=revision.scope_type,
        scope_id=revision.scope_id,
        project="mnemos",
        purposes=("cognitive_state_read",),
        consent_provenance_refs=("forged-pair",),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:forged-pair",),
    )

    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
            DROP TRIGGER typed_search_state_headers_no_delete;
            DROP TRIGGER typed_search_state_revision_bindings_no_delete;
            DELETE FROM typed_search_state_headers;
            DELETE FROM typed_search_state_revision_bindings;
            CREATE TRIGGER typed_search_state_headers_no_delete
            BEFORE DELETE ON typed_search_state_headers BEGIN
                SELECT RAISE(ABORT, 'typed search state headers are immutable');
            END;
            CREATE TRIGGER typed_search_state_revision_bindings_no_delete
            BEFORE DELETE ON typed_search_state_revision_bindings BEGIN
                SELECT RAISE(
                    ABORT, 'typed search state revision bindings are immutable'
                );
            END;
            """)
        conn.commit()
        canonical = conn.execute(
            "SELECT object_type, object_id, scope_type, scope_id, payload_hash, created_at "
            "FROM cognitive_state_revisions WHERE revision_id=?",
            (revision.revision_id,),
        ).fetchone()
        assert canonical is not None
        with pytest.raises(sqlite3.IntegrityError, match="revision binding mismatch"):
            conn.execute(
                """
                INSERT INTO typed_search_state_revision_bindings(
                    revision_id, object_type, object_id, scope_type, scope_id,
                    access_control, access_control_hash, revision_payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision.revision_id,
                    *canonical[:4],
                    json.dumps(forged_access, sort_keys=True, separators=(",", ":")),
                    cognitive_access_hash(forged_access),
                    canonical[4],
                    canonical[5],
                ),
            )


def test_header_reconciliation_is_atomic_and_idempotent(tmp_path):
    db_path, revision = _legacy_without_headers(tmp_path)
    with sqlite3.connect(db_path) as conn:
        preview = reconcile_state_search_headers(conn, apply=False)
        assert preview["before"]["missing_header_count"] == 1
        first = reconcile_state_search_headers(conn, apply=True)
        second = reconcile_state_search_headers(conn, apply=True)

    assert first["inserted_count"] == 1
    assert first["after"]["ok"] is True
    assert second["inserted_count"] == 0
    assert second["after"]["header_count"] == 1
    assert revision.revision_id


def test_v1_header_schema_is_explicitly_upgraded_to_revision_binding_v4(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-v1-upgrade",
        claim_text="legacy header schema requires explicit v4 reconciliation",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-v1-upgrade")
    with sqlite3.connect(db_path) as conn:
        legacy_header = conn.execute(
            "SELECT revision_id, object_type, object_id, scope_type, scope_id, "
            "access_control, access_control_hash, created_at "
            "FROM typed_search_state_headers WHERE revision_id=?",
            (revision.revision_id,),
        ).fetchone()
        assert legacy_header is not None
        conn.executescript("""
            DROP TABLE typed_search_state_header_registry;
            DROP TABLE typed_search_state_exclusions;
            DROP TABLE typed_search_state_headers;
            DROP TABLE typed_search_state_revision_bindings;
            CREATE TABLE typed_search_state_headers (
                revision_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                access_control TEXT NOT NULL CHECK(
                    json_valid(access_control) AND json_type(access_control)='object'
                ),
                access_control_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
                    ON DELETE RESTRICT,
                UNIQUE(object_type, object_id, revision_id)
            );
            CREATE INDEX idx_typed_search_state_headers_object
                ON typed_search_state_headers(object_type, object_id);
            CREATE INDEX idx_typed_search_state_headers_scope
                ON typed_search_state_headers(scope_type, scope_id, object_type);
            CREATE TABLE typed_search_state_exclusions (
                revision_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                reason_code TEXT NOT NULL CHECK(
                    reason_code='legacy_noncurrent_acl_unavailable'
                ),
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(revision_id) REFERENCES cognitive_state_revisions(revision_id)
                    ON DELETE RESTRICT
            );
            CREATE TABLE typed_search_state_header_registry (
                component TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                ddl_hash TEXT NOT NULL
            );
            CREATE TRIGGER typed_search_state_headers_no_update
            BEFORE UPDATE ON typed_search_state_headers BEGIN
                SELECT RAISE(ABORT, 'typed search state headers are immutable');
            END;
            CREATE TRIGGER typed_search_state_headers_no_delete
            BEFORE DELETE ON typed_search_state_headers BEGIN
                SELECT RAISE(ABORT, 'typed search state headers are immutable');
            END;
            CREATE TRIGGER typed_search_state_exclusions_no_update
            BEFORE UPDATE ON typed_search_state_exclusions BEGIN
                SELECT RAISE(ABORT, 'typed search state exclusions are immutable');
            END;
            CREATE TRIGGER typed_search_state_exclusions_no_delete
            BEFORE DELETE ON typed_search_state_exclusions BEGIN
                SELECT RAISE(ABORT, 'typed search state exclusions are immutable');
            END;
            """)
        conn.execute(
            "INSERT INTO typed_search_state_headers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(legacy_header),
        )
        conn.execute(
            "INSERT INTO typed_search_state_header_registry VALUES (?, ?, ?)",
            (
                "cognitive_search_state_headers",
                "mnemos.cognitive_search_state_headers.v1",
                "sha256:693ba1ffc6a70227844fc11fb06437653f5100389aba5019c7c882ba8ce52ae4",
            ),
        )
        conn.commit()
        before = inspect_state_search_headers(conn)
        assert before["schema_upgrade_required"] is True
        upgraded = reconcile_state_search_headers(conn, apply=True)

    assert upgraded["inserted_count"] == 1
    assert upgraded["after"]["schema_version"].endswith(".v4")
    assert upgraded["after"]["ok"] is True


def test_exact_v3_header_schema_is_explicitly_upgraded_to_v4(tmp_path):
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-v3-upgrade",
        claim_text="registered v3 physical schema upgrades to canonical v4",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-v3-upgrade")
    with sqlite3.connect(db_path) as conn:
        _downgrade_projection_to_exact_v3(conn)
        before = inspect_state_search_headers(conn)
        assert before["schema_upgrade_required"] is True
        assert before["binding_preimage_missing_count"] == 1
        upgraded = reconcile_state_search_headers(conn, apply=True)

    assert upgraded["inserted_count"] == 1
    assert upgraded["after"]["schema_version"].endswith(".v4")
    assert upgraded["after"]["ok"] is True


def test_unknown_registry_identity_fails_closed_without_rebuild(tmp_path):
    from core.cognitive.search_state_headers import StateSearchHeaderSchemaError

    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-registry-unknown",
        claim_text="unknown registered schema identities require manual review",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-registry-unknown")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE typed_search_state_header_registry "
            "SET schema_version='mnemos.cognitive_search_state_headers.v999', "
            "ddl_hash='sha256:unknown'"
        )
        conn.commit()
        with pytest.raises(StateSearchHeaderSchemaError, match="unknown .* schema"):
            reconcile_state_search_headers(conn, apply=True)
        registry = conn.execute(
            "SELECT schema_version, ddl_hash FROM typed_search_state_header_registry"
        ).fetchone()
        counts = (
            conn.execute("SELECT COUNT(*) FROM typed_search_state_headers").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM typed_search_state_revision_bindings").fetchone()[0],
        )

    assert tuple(registry or ()) == (
        "mnemos.cognitive_search_state_headers.v999",
        "sha256:unknown",
    )
    assert counts == (1, 1)


def test_unknown_binding_schema_fails_closed_without_destructive_rebuild(tmp_path):
    from core.cognitive.search_state_headers import StateSearchHeaderSchemaError

    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    store = CognitiveStateStore(db_path)
    revision = _scoped_revision(
        "header-binding-unknown",
        claim_text="unknown binding schemas must not be destroyed automatically",
        owner_principal_id="test:state-store",
    )
    _commit(store, revision, "header-binding-unknown")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE typed_search_state_revision_bindings " "ADD COLUMN unknown_extension TEXT"
        )
        conn.commit()
        assert inspect_state_search_headers(conn)["ok"] is False
        with pytest.raises(StateSearchHeaderSchemaError, match="unknown .* schema"):
            reconcile_state_search_headers(conn, apply=True)
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(typed_search_state_revision_bindings)"
            ).fetchall()
        }
        counts = (
            conn.execute("SELECT COUNT(*) FROM typed_search_state_headers").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM typed_search_state_revision_bindings").fetchone()[0],
        )

    assert "unknown_extension" in columns
    assert counts == (1, 1)


@pytest.mark.parametrize("failpoint", ["after_schema", "after_copy"])
def test_header_reconciliation_rolls_back_every_failure(tmp_path, failpoint):
    db_path, _revision = _legacy_without_headers(tmp_path)
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(RuntimeError, match="injected"):
            reconcile_state_search_headers(conn, apply=True, failpoint=failpoint)
        report = inspect_state_search_headers(conn)

    assert report["schema_present"] is False
    assert report["missing_header_count"] == 1


def test_header_reconciliation_cli_requires_backup_and_stopped_writers(tmp_path, monkeypatch):
    from scripts.reconcile_cognitive_search_state_headers import main

    db_path, _revision = _legacy_without_headers(tmp_path)
    assert main(["--db-path", str(db_path), "--apply", "--json"]) == 2

    monkeypatch.setattr(
        "scripts.reconcile_cognitive_search_state_headers.runtime_writers_are_inactive",
        lambda _path: True,
    )
    backup_dir = tmp_path / "backup"
    assert (
        main(
            [
                "--db-path",
                str(db_path),
                "--apply",
                "--backup-dir",
                str(backup_dir),
                "--json",
            ]
        )
        == 0
    )
    backups = list(backup_dir.glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
