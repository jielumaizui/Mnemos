"""Append-only native Raw contract observations and effective certification state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import Any, Mapping


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _decompress_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    return zlib.decompress(blob).decode("utf-8")


def _initial_confidence(status: str) -> float:
    if status == "complete":
        return 1.0
    if status == "derived":
        return 0.65
    return 0.4


def _observation_id(
    logical_event_id: str,
    revision_id: str,
    contract_state: str,
    contract_errors: list[str],
    observed_at: str,
) -> str:
    payload = json.dumps(
        {
            "logical_event_id": logical_event_id,
            "revision_id": revision_id,
            "contract_state": contract_state,
            "contract_errors": contract_errors,
            "observed_at": observed_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "rawcontract-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


class NativeRawContractLedger:
    """Own native-source verdict history without replacing observed Raw bytes."""

    _CURRENT_EVENT_COLUMNS = frozenset(
        {
            "m.event_id",
            "raw_turns.event_id",
            "t.event_id",
        }
    )

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        """Create the append-only observation table and lookup index if absent."""
        script = """
            CREATE TABLE IF NOT EXISTS raw_native_contract_observations (
                observation_id TEXT PRIMARY KEY,
                logical_event_id TEXT NOT NULL,
                observed_revision_id TEXT NOT NULL,
                support_manifest_hash TEXT NOT NULL,
                contract_state TEXT NOT NULL,
                contract_errors_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                FOREIGN KEY(logical_event_id)
                    REFERENCES raw_turns(event_id) ON DELETE CASCADE,
                FOREIGN KEY(observed_revision_id)
                    REFERENCES raw_turn_revisions(revision_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_raw_native_contract_observations_latest
                ON raw_native_contract_observations(logical_event_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_raw_native_contract_observations_by_event
                ON raw_native_contract_observations(logical_event_id);
            """
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(statement)

    @staticmethod
    def _contract_errors(metadata: Mapping[str, Any]) -> list[str]:
        supplied = metadata.get("support_raw_contract_errors")
        if not isinstance(supplied, list) or not all(isinstance(item, str) for item in supplied):
            return []
        return list(dict.fromkeys(supplied))

    @classmethod
    def current_event_visibility_predicate(cls, event_id_sql: str) -> str:
        """Return the fail-closed SQL predicate for normal current-data reads.

        Direct forensic reads deliberately do not use this predicate.  It is
        restricted to fixed internal column references so that callers cannot
        interpolate user-controlled SQL into a current-data query.
        """
        if event_id_sql not in cls._CURRENT_EVENT_COLUMNS:
            raise ValueError("unsupported Raw logical-event SQL reference")
        return f"""
            AND NOT EXISTS (
                SELECT 1
                FROM raw_native_contract_observations AS latest_contract
                WHERE latest_contract.logical_event_id={event_id_sql}
                  AND latest_contract.rowid=(
                      SELECT prior_contract.rowid
                      FROM raw_native_contract_observations AS prior_contract
                      WHERE prior_contract.logical_event_id={event_id_sql}
                      ORDER BY prior_contract.rowid DESC
                      LIMIT 1
                  )
                  AND (
                      TRIM(COALESCE(latest_contract.observation_id, '')) = ''
                      OR TRIM(COALESCE(latest_contract.observed_revision_id, '')) = ''
                      OR COALESCE(latest_contract.contract_state, '') != 'conformant'
                      OR TRIM(COALESCE(latest_contract.support_manifest_hash, '')) = ''
                      OR TRIM(COALESCE(latest_contract.observed_at, '')) = ''
                      OR json_valid(COALESCE(latest_contract.contract_errors_json, '')) != 1
                      OR json_type(latest_contract.contract_errors_json) != 'array'
                      OR (
                          latest_contract.contract_state = 'conformant'
                          AND json_array_length(
                              CASE
                                  WHEN json_valid(
                                      COALESCE(latest_contract.contract_errors_json, '')
                                  ) = 1
                                  AND json_type(latest_contract.contract_errors_json) = 'array'
                                  THEN latest_contract.contract_errors_json
                                  ELSE '[]'
                              END
                          ) != 0
                      )
                      OR EXISTS (
                          SELECT 1
                          FROM json_each(
                              CASE
                                  WHEN json_valid(
                                      COALESCE(latest_contract.contract_errors_json, '')
                                  ) = 1
                                  AND json_type(latest_contract.contract_errors_json) = 'array'
                                  THEN latest_contract.contract_errors_json
                                  ELSE '[]'
                              END
                          ) AS contract_error
                          WHERE contract_error.type != 'text'
                             OR TRIM(COALESCE(contract_error.value, '')) = ''
                      )
                      OR NOT EXISTS (
                          SELECT 1
                          FROM raw_turn_revisions AS observed_revision
                          WHERE observed_revision.revision_id=
                                latest_contract.observed_revision_id
                            AND observed_revision.logical_event_id={event_id_sql}
                      )
                      OR (
                          latest_contract.contract_state = 'nonconforming'
                          AND json_array_length(
                              CASE
                                  WHEN json_valid(
                                      COALESCE(latest_contract.contract_errors_json, '')
                                  ) = 1
                                  AND json_type(latest_contract.contract_errors_json) = 'array'
                                  THEN latest_contract.contract_errors_json
                                  ELSE '[]'
                              END
                          ) = 0
                      )
                  )
            )
        """

    def record_explicit(
        self,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
        support_manifest_hash: str,
        contract_state: str,
        contract_errors: list[str],
        observed_at: str,
    ) -> dict[str, Any]:
        """Append a verified verdict for an existing immutable Raw revision.

        Reconciliation can establish a new fact about a persisted revision,
        but it must not manufacture a replacement revision or an unbound
        ledger row.  The revision-to-logical-event check keeps the observation
        tied to the evidence it certifies.
        """
        if not isinstance(logical_event_id, str) or not logical_event_id:
            raise ValueError("native raw observation requires a logical event id")
        if not isinstance(revision_id, str) or not revision_id:
            raise ValueError("native raw observation requires an immutable revision id")
        if contract_state not in {"conformant", "nonconforming"}:
            raise ValueError("native raw observation has an invalid contract state")
        if not isinstance(contract_errors, list) or not all(
            isinstance(error, str) and error for error in contract_errors
        ):
            raise ValueError("native raw observation requires string contract errors")
        normalized_errors = list(dict.fromkeys(contract_errors))
        if (contract_state == "conformant") != (not normalized_errors):
            raise ValueError("native raw observation state and errors disagree")
        manifest_hash = str(support_manifest_hash or "").strip()
        if not manifest_hash:
            raise ValueError("native raw observation requires a support manifest hash")
        if not isinstance(observed_at, str) or not observed_at.strip():
            raise ValueError("native raw observation requires an observed timestamp")
        revision = conn.execute(
            "SELECT logical_event_id FROM raw_turn_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        if not revision or str(revision[0]) != logical_event_id:
            raise ValueError("native raw observation revision does not bind the logical event")
        observation_id = _observation_id(
            logical_event_id,
            revision_id,
            contract_state,
            normalized_errors,
            observed_at,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO raw_native_contract_observations (
                observation_id, logical_event_id, observed_revision_id,
                support_manifest_hash, contract_state, contract_errors_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                logical_event_id,
                revision_id,
                manifest_hash,
                contract_state,
                _json_dumps(normalized_errors),
                observed_at,
            ),
        )
        return {
            "observation_id": observation_id,
            "contract_state": contract_state,
            "contract_errors": normalized_errors,
            "observed_at": observed_at,
        }

    def record(
        self,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
        metadata: Mapping[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        """Append a native contract verdict for an already-persisted revision."""
        contract_state = str(metadata.get("support_raw_contract_state") or "")
        if contract_state not in {"conformant", "nonconforming"}:
            raise ValueError("native raw receipt has no valid support contract state")
        contract_errors = self._contract_errors(metadata)
        return self.record_explicit(
            conn,
            logical_event_id=logical_event_id,
            revision_id=revision_id,
            support_manifest_hash=str(metadata.get("support_manifest_hash") or ""),
            contract_state=contract_state,
            contract_errors=contract_errors,
            observed_at=observed_at,
        )

    @staticmethod
    def _validated_observation(
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        row: tuple[Any, ...],
    ) -> dict[str, Any]:
        """Decode one append-only observation and prove its revision owner."""
        try:
            contract_errors = json.loads(str(row[4] or "[]"))
        except (TypeError, json.JSONDecodeError):
            raise ValueError("native raw contract observation is invalid") from None
        contract_state = str(row[3] or "")
        if (
            not str(row[0] or "")
            or not str(row[1] or "")
            or not str(row[2] or "")
            or contract_state not in {"conformant", "nonconforming"}
            or not str(row[5] or "")
            or not isinstance(contract_errors, list)
            or any(not isinstance(item, str) or not item for item in contract_errors)
            or (contract_state == "conformant") != (not contract_errors)
        ):
            raise ValueError("native raw contract observation is invalid")
        revision = conn.execute(
            """
            SELECT logical_event_id
            FROM raw_turn_revisions
            WHERE revision_id=?
            """,
            (str(row[1]),),
        ).fetchone()
        if not revision or str(revision[0] or "") != logical_event_id:
            raise ValueError("native raw contract observation is invalid")
        return {
            "observation_id": str(row[0]),
            "observed_revision_id": str(row[1]),
            "support_manifest_hash": str(row[2]),
            "contract_state": contract_state,
            "contract_errors": list(contract_errors),
            "observed_at": str(row[5]),
        }

    @classmethod
    def latest(
        cls,
        conn: sqlite3.Connection,
        logical_event_id: str,
    ) -> dict[str, Any] | None:
        """Return the last valid observation, independent of producer clock skew."""
        row = conn.execute(
            """
            SELECT observation_id, observed_revision_id, support_manifest_hash,
                   contract_state, contract_errors_json, observed_at
            FROM raw_native_contract_observations
            WHERE logical_event_id=?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (logical_event_id,),
        ).fetchone()
        if not row:
            return None
        return cls._validated_observation(
            conn,
            logical_event_id=logical_event_id,
            row=row,
        )

    @classmethod
    def latest_for_current(
        cls,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        current_revision_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest observation only when it binds the exact current revision."""
        return cls.latest_for_revision(
            conn,
            logical_event_id=logical_event_id,
            revision_id=current_revision_id,
        )

    @classmethod
    def latest_for_revision(
        cls,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
    ) -> dict[str, Any] | None:
        """Return the logical latest verdict only when it binds this revision.

        Automatic consumers must not resurrect an older conformant verdict
        after a later observation quarantines or supersedes the logical event.
        Historical per-revision verdicts remain available through
        ``list_for_event`` for forensic inspection.
        """
        latest = cls.latest(conn, logical_event_id)
        if latest is None:
            return None
        if latest["observed_revision_id"] != str(revision_id or ""):
            raise ValueError(
                "native raw contract observation does not bind requested revision"
            )
        return latest

    def refresh_effective_state(
        self,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        observed_at: str,
    ) -> None:
        """Surface the latest verdict on the logical row and its lifecycle metrics."""
        row = conn.execute(
            """
            SELECT t.current_revision_id, t.completeness_status, r.snapshot_blob
            FROM raw_turns t
            LEFT JOIN raw_turn_revisions r ON r.revision_id=t.current_revision_id
            WHERE t.event_id=?
            """,
            (logical_event_id,),
        ).fetchone()
        if not row:
            return
        latest = self.latest(conn, logical_event_id)
        if latest is None:
            return
        effective_observed_at = str(latest["observed_at"])
        base_status = str(row[1] or "partial")
        base_metadata: dict[str, Any] = {}
        if row[2] is None:
            raise ValueError("native raw current snapshot is invalid")
        try:
            snapshot = json.loads(_decompress_text(row[2]))
        except (
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
            zlib.error,
        ):
            raise ValueError("native raw current snapshot is invalid") from None
        if not isinstance(snapshot, dict):
            raise ValueError("native raw current snapshot is invalid")
        base_status = str(snapshot.get("completeness_status") or base_status)
        supplied_metadata = snapshot.get("metadata")
        if isinstance(supplied_metadata, dict):
            base_metadata = dict(supplied_metadata)
        base_metadata.update(
            {
                "support_current_revision_raw_contract_state": base_metadata.get(
                    "support_raw_contract_state", ""
                ),
                "support_current_revision_raw_contract_errors": base_metadata.get(
                    "support_raw_contract_errors", []
                ),
                "support_raw_contract_state": latest["contract_state"],
                "support_raw_contract_errors": latest["contract_errors"],
                "support_latest_native_contract_observation_id": latest["observation_id"],
                "support_latest_native_contract_state": latest["contract_state"],
                "support_latest_native_contract_errors": latest["contract_errors"],
                "support_latest_native_contract_observed_at": latest["observed_at"],
                "support_native_contract_certifying": latest["contract_state"] == "conformant",
            }
        )
        effective_status = (
            "partial" if latest["contract_state"] != "conformant" else base_status
        )
        conn.execute(
            """
            UPDATE raw_turns
            SET completeness_status=?, metadata_json=?, updated_at=?
            WHERE event_id=?
            """,
            (
                effective_status,
                _json_dumps(base_metadata),
                effective_observed_at,
                logical_event_id,
            ),
        )
        self._sync_effective_metrics(
            conn,
            logical_event_id=logical_event_id,
            effective_status=effective_status,
            observed_at=effective_observed_at,
        )

    @staticmethod
    def _sync_effective_metrics(
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        effective_status: str,
        observed_at: str,
    ) -> None:
        """Lower or restore metrics immediately; schedule a full lifecycle recompute."""
        baseline = _initial_confidence(effective_status)
        conn.execute(
            """
            UPDATE raw_metrics
            SET confidence=?,
                survival_score=CASE
                    WHEN ?='partial' THEN MIN(COALESCE(survival_score, ?), ?)
                    ELSE MAX(COALESCE(survival_score, ?), ?)
                END,
                retention_state=CASE
                    WHEN ?!='partial' THEN 'active'
                    ELSE retention_state
                END,
                next_survival_recalc_at=?,
                updated_at=?
            WHERE event_id=?
            """,
            (
                baseline,
                effective_status,
                baseline,
                baseline,
                baseline,
                baseline,
                effective_status,
                observed_at,
                observed_at,
                logical_event_id,
            ),
        )

    def decorate_current_revision(
        self,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
        revision_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach effective logical contract state to a read of the current revision."""
        row = conn.execute(
            "SELECT current_revision_id FROM raw_turns WHERE event_id=?",
            (logical_event_id,),
        ).fetchone()
        if not row or str(row[0] or "") != revision_id:
            return data
        try:
            latest = self.latest(conn, logical_event_id)
        except ValueError:
            metadata = data.get("metadata")
            effective_metadata = (
                dict(metadata) if isinstance(metadata, dict) else {}
            )
            current_errors = effective_metadata.get(
                "support_raw_contract_errors",
                [],
            )
            effective_metadata.update(
                {
                    "support_current_revision_raw_contract_state": str(
                        effective_metadata.get("support_raw_contract_state") or ""
                    ),
                    "support_current_revision_raw_contract_errors": (
                        list(current_errors)
                        if isinstance(current_errors, list)
                        else []
                    ),
                    "support_raw_contract_state": "nonconforming",
                    "support_raw_contract_errors": [
                        "native_contract_observation_invalid"
                    ],
                    "support_latest_native_contract_observation_id": "",
                    "support_latest_native_contract_state": "nonconforming",
                    "support_latest_native_contract_errors": [
                        "native_contract_observation_invalid"
                    ],
                    "support_latest_native_contract_observed_at": "",
                    "support_native_contract_certifying": False,
                }
            )
            data["metadata"] = effective_metadata
            data["native_contract_observation_failure"] = {
                "code": "native_contract_observation_invalid",
                "certifying": False,
            }
            data["completeness_status"] = "partial"
            return data
        if latest is None:
            return data
        metadata = data.get("metadata")
        effective_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        effective_metadata.update(
            {
                "support_current_revision_raw_contract_state": effective_metadata.get(
                    "support_raw_contract_state", ""
                ),
                "support_current_revision_raw_contract_errors": effective_metadata.get(
                    "support_raw_contract_errors", []
                ),
                "support_raw_contract_state": latest["contract_state"],
                "support_raw_contract_errors": latest["contract_errors"],
                "support_latest_native_contract_observation_id": latest["observation_id"],
                "support_latest_native_contract_state": latest["contract_state"],
                "support_latest_native_contract_errors": latest["contract_errors"],
                "support_latest_native_contract_observed_at": latest["observed_at"],
                "support_native_contract_certifying": latest["contract_state"] == "conformant",
            }
        )
        data["metadata"] = effective_metadata
        data["native_contract_observation"] = latest
        if latest["contract_state"] != "conformant":
            data["completeness_status"] = "partial"
        return data

    def list_for_event(
        self,
        conn: sqlite3.Connection,
        *,
        logical_event_id: str,
    ) -> list[dict[str, Any]]:
        """Return all append-only native contract observations for one Raw event."""
        rows = conn.execute(
            """
            SELECT observation_id, observed_revision_id, support_manifest_hash,
                   contract_state, contract_errors_json, observed_at
            FROM raw_native_contract_observations
            WHERE logical_event_id=?
            ORDER BY rowid
            """,
            (logical_event_id,),
        ).fetchall()
        return [
            self._validated_observation(
                conn,
                logical_event_id=logical_event_id,
                row=row,
            )
            for row in rows
        ]
