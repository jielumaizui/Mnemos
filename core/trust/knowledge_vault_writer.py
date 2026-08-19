"""Trusted writer from approved proposals to NativeStore and Markdown."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionTerminal,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.state_contract import sha256_json as cognitive_sha256_json
from core.trust.config import TrustedPushConfig, load_trusted_push_config
from core.trust.markdown_adapter import MarkdownAdapter, read_markdown_text
from core.trust.models import JournalEventInput, UserDecision, sha256_text, utc_now_iso
from core.trust.proposal_queue import ProposalQueue
from core.trust.write_journal import WriteJournal
from core.utils import atomic_write_text

logger = logging.getLogger(__name__)

KNOWLEDGE_VAULT_ACTION_TYPE = "knowledge_vault_write"
KNOWLEDGE_VAULT_OWNER = "trusted_vault"
KNOWLEDGE_VAULT_EXECUTOR = "knowledge_vault_writer"


def knowledge_vault_material_action_binding(
    *,
    proposal_id: str,
    target_uri: str,
    content: str,
    expected_existing_hash: str | None = None,
) -> dict[str, str]:
    """Return the exact approved proposal target and payload hash."""

    target = Path(target_uri).expanduser().resolve(strict=False)
    payload = {
        "schema_version": "mnemos.knowledge_vault_material_input.v1",
        "proposal_id": str(proposal_id),
        "target_uri": str(target),
        "content_hash": sha256_text(content),
        "expected_existing_hash": expected_existing_hash,
    }
    return {
        "target_ref": f"knowledge-vault:{target}",
        "input_hash": cognitive_sha256_json(payload),
    }


class KnowledgeVaultEffectOracle:
    """Read-only proof for one fully committed trusted-vault proposal."""

    owner = KNOWLEDGE_VAULT_OWNER
    executor_id = KNOWLEDGE_VAULT_EXECUTOR
    action_type = KNOWLEDGE_VAULT_ACTION_TYPE

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def recover_pending(
        self,
        *,
        state_db_path: Path,
        proposal_id: str,
    ) -> bool:
        """Close a committed proposal's pending command without re-executing it."""

        if not self.db_path.is_file() or not Path(state_db_path).is_file():
            return False
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            rows = conn.execute(
                """SELECT metadata_json FROM journal_events
                   WHERE proposal_id=? AND event_type='prepare'""",
                (proposal_id,),
            ).fetchall()
        command_ids: list[str] = []
        for row in rows:
            try:
                metadata = json.loads(str(row[0] or "{}"))
            except json.JSONDecodeError:
                continue
            command_id = str(metadata.get("material_command_id") or "")
            if command_id:
                command_ids.append(command_id)
        if not command_ids:
            return False
        coordinator = MaterialActionCoordinator(CognitiveStateStore(state_db_path))
        recovered = False
        for command_id in command_ids:
            with sqlite3.connect(
                f"file:{Path(state_db_path).resolve(strict=True)}?mode=ro",
                uri=True,
            ) as conn:
                terminal = conn.execute(
                    """SELECT 1 FROM cognitive_state_effect_receipts
                       WHERE command_id=?""",
                    (command_id,),
                ).fetchone()
            if terminal is not None:
                continue
            receipt = coordinator.recover(
                command_id,
                executor_id=self.executor_id,
                oracle=self,
            )
            recovered = recovered or receipt is not None
        return recovered

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Recover an exact committed vault write from the append-only journal."""

        if not self.db_path.is_file():
            return None
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            prepares = conn.execute(
                """SELECT * FROM journal_events
                   WHERE event_type='prepare' ORDER BY rowid"""
            ).fetchall()
            matches = []
            for row in prepares:
                try:
                    metadata = json.loads(str(row["metadata_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                if metadata.get("material_command_id") == permit.command_id:
                    matches.append((row, metadata))
            if not matches:
                return None
            if len(matches) != 1:
                raise RuntimeError(
                    "material command maps to multiple knowledge-vault prepares"
                )
            prepare, metadata = matches[0]
            proposal_id = str(prepare["proposal_id"])
            proposal = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            native = conn.execute(
                "SELECT * FROM native_store WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            latest = conn.execute(
                """SELECT * FROM journal_events WHERE proposal_id=?
                   ORDER BY rowid DESC LIMIT 1""",
                (proposal_id,),
            ).fetchone()
        if (
            proposal is None
            or native is None
            or latest is None
            or str(proposal["status"]) != "committed"
            or str(latest["event_type"]) != "commit"
        ):
            return None
        try:
            payload = json.loads(str(native["payload_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("knowledge-vault native payload is invalid") from exc
        target_path = Path(str(native["target_uri"])).expanduser()
        content = str(payload.get("content") or "")
        expected_existing_hash = payload.get("expected_existing_hash")
        binding = knowledge_vault_material_action_binding(
            proposal_id=proposal_id,
            target_uri=str(target_path),
            content=content,
            expected_existing_hash=(
                str(expected_existing_hash)
                if expected_existing_hash is not None
                else None
            ),
        )
        if (
            binding["target_ref"] != permit.target_ref
            or binding["input_hash"] != permit.input_hash
            or str(native["content_hash"]) != sha256_text(content)
            or not target_path.is_file()
            or sha256_text(read_markdown_text(target_path)) != sha256_text(content)
        ):
            raise RuntimeError(
                "knowledge-vault committed state does not match its material command"
            )
        after_hash = _knowledge_vault_state_hash(
            proposal_id,
            target_path,
            native,
        )
        before_hash = str(metadata.get("before_hash") or "")
        if not before_hash.startswith("sha256:"):
            raise RuntimeError("knowledge-vault prepare lacks its exact before hash")
        return MaterialActionObservation(
            status="committed",
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"target-after:{after_hash}",
                f"target-journal:trusted-write:{proposal_id}:{after_hash}",
            ),
            outcome="native store and formal Markdown target committed",
            observed_at=str(latest["created_at"]),
        )


class KnowledgeVaultWriter:
    """The only P0 trusted write entrypoint."""

    def __init__(
        self,
        *,
        wiki_base: Path,
        db_path: Path | None = None,
        config: TrustedPushConfig | None = None,
    ):
        self._config = config or load_trusted_push_config(wiki_base=wiki_base)
        self.db_path = Path(db_path or self._config.db_path)
        self._queue = ProposalQueue(self.db_path, wiki_base=wiki_base, config=self._config)
        self._journal = WriteJournal(self.db_path, config=self._config)
        self._markdown = MarkdownAdapter(wiki_base)
        self._init_native_store()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_native_store(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS native_store (
                    proposal_id TEXT PRIMARY KEY,
                    target_uri TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    written_at TEXT NOT NULL
                )
                """
            )

    def write_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "user",
        allow_high_risk: bool = False,
        material_action: MaterialActionAuthorization | None = None,
    ) -> Dict[str, Any]:
        proposal = self._queue.get(proposal_id)
        oracle = KnowledgeVaultEffectOracle(self.db_path)
        if proposal.status == "committed" and oracle.recover_pending(
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            proposal_id=proposal_id,
        ):
            self._recover_origin_markdown_terminal(proposal)
            return {
                "status": "committed",
                "path": str(Path(proposal.candidate.target_path or "").expanduser()),
            }
        if proposal.status not in {
            "validated",
            "needs_manual_review",
            "committed",
        }:
            raise ValueError(f"proposal status cannot be approved: {proposal.status}")
        if proposal.risk_level == "high" and not allow_high_risk:
            raise ValueError("high risk proposal requires allow_high_risk")

        target_uri = proposal.candidate.target_path or ""
        content = str(proposal.candidate.payload.get("content", ""))
        content_hash = sha256_text(content)
        expected_existing_hash = proposal.candidate.payload.get(
            "expected_existing_hash"
        )
        binding = knowledge_vault_material_action_binding(
            proposal_id=proposal_id,
            target_uri=target_uri,
            content=content,
            expected_existing_hash=expected_existing_hash,
        )
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=KNOWLEDGE_VAULT_OWNER,
            executor_id=KNOWLEDGE_VAULT_EXECUTOR,
            action_type=KNOWLEDGE_VAULT_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        if material_action.recover(oracle) is not None:
            if oracle.observe(permit) is None:
                raise RuntimeError(
                    "terminal knowledge-vault receipt lacks exact target evidence"
                )
            self._recover_origin_markdown_terminal(proposal)
            return {"status": "committed", "path": str(Path(target_uri).expanduser())}
        if proposal.status == "committed":
            raise RuntimeError(
                "committed knowledge-vault proposal lacks recoverable effect evidence"
            )
        permit = require_material_action(
            material_action,
            owner=KNOWLEDGE_VAULT_OWNER,
            executor_id=KNOWLEDGE_VAULT_EXECUTOR,
            action_type=KNOWLEDGE_VAULT_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        from core.trust.vault_mutation_service import (
            bind_trusted_markdown_candidate_action,
            record_trusted_markdown_observed_terminal,
            trusted_markdown_target_state_hash,
        )

        origin_material_action = bind_trusted_markdown_candidate_action(
            proposal.candidate,
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
        )
        target_path = Path(target_uri).expanduser()
        markdown_before_hash = trusted_markdown_target_state_hash(target_path)
        candidate_before_hash = str(
            proposal.candidate.payload.get("material_before_hash") or ""
        )
        if (
            origin_material_action is not None
            and candidate_before_hash != markdown_before_hash
        ):
            raise PermissionError(
                "trusted Markdown target changed after its material decision"
            )
        before_exists = target_path.is_file()
        before_content = read_markdown_text(target_path) if before_exists else ""
        before_hash = self._effect_state_hash(proposal_id, target_path)
        try:
            self._queue.record_decision(
                UserDecision(proposal_id=proposal_id, decision="approve", actor=actor)
            )
            self._queue.update_status(proposal_id, "write_prepared")
            self._journal.append_event(
                JournalEventInput(
                    proposal_id=proposal_id,
                    event_type="prepare",
                    target_uri=target_uri,
                    content_hash=content_hash,
                    actor=actor,
                    metadata={
                        "source": proposal.candidate.source,
                        "material_command_id": permit.command_id,
                        "decision_revision_id": permit.decision_revision_id,
                        "before_hash": before_hash,
                    },
                )
            )
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO native_store (
                        proposal_id, target_uri, content_hash, payload_json, written_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        proposal_id,
                        target_uri,
                        content_hash,
                        json.dumps(proposal.candidate.payload, ensure_ascii=False),
                        utc_now_iso(),
                    ),
                )
            markdown_result = self._markdown.write(
                Path(target_uri),
                content,
                expected_existing_hash=expected_existing_hash,
                conflict_metadata={"proposal_id": proposal_id},
            )
            if markdown_result.status == "conflict":
                self._delete_native_store_record(proposal_id)
                self._journal.append_event(
                    JournalEventInput(
                        proposal_id=proposal_id,
                        event_type="rollback",
                        target_uri=target_uri,
                        content_hash=content_hash,
                        actor=actor,
                        metadata={
                            "reason": "markdown_conflict",
                            "conflict_path": str(markdown_result.conflict_path),
                        },
                    )
                )
                self._queue.update_status(
                    proposal_id,
                    "failed",
                    error_message=f"Markdown conflict: {markdown_result.conflict_path}",
                )
                after_hash = self._effect_state_hash(proposal_id, target_path)
                material_action.record_terminal(
                    MaterialActionTerminal(
                        status="failed_terminal",
                        target_effect_id=permit.effect_id,
                        before_hash=before_hash,
                        after_hash=after_hash,
                        evidence_refs=(
                            f"material-command:{permit.command_id}",
                            f"decision-revision:{permit.decision_revision_id}",
                            f"material-effect:{permit.effect_id}",
                            f"attempted-effect:{permit.effect_id}",
                            f"target-oracle:knowledge-vault:{after_hash}",
                            f"conflict-artifact:{markdown_result.conflict_path}",
                        ),
                        reason_code="markdown_conflict",
                        outcome="canonical target unchanged; conflict artifact retained",
                        created_at=utc_now_iso(),
                    )
                )
                if origin_material_action is not None:
                    markdown_after_hash = trusted_markdown_target_state_hash(
                        target_path
                    )
                    record_trusted_markdown_observed_terminal(
                        origin_material_action,
                        status="failed_terminal",
                        before_hash=markdown_before_hash,
                        after_hash=markdown_after_hash,
                        reason_code="markdown_conflict",
                        evidence_refs=(
                            f"target-oracle:trusted-markdown:{markdown_after_hash}",
                            f"conflict-artifact:{markdown_result.conflict_path}",
                        ),
                    )
                return {
                    "status": "failed",
                    "reason": "markdown_conflict",
                    "conflict_path": str(markdown_result.conflict_path),
                }
            self._journal.append_event(
                JournalEventInput(
                    proposal_id=proposal_id,
                    event_type="commit",
                    target_uri=target_uri,
                    content_hash=content_hash,
                    actor=actor,
                    metadata={"path": str(markdown_result.path)},
                )
            )
            self._queue.update_status(proposal_id, "committed")
        except (OSError, sqlite3.Error, TypeError, ValueError, RuntimeError) as exc:
            self._delete_native_store_record(proposal_id)
            self._restore_markdown_target(
                target_path,
                existed=before_exists,
                content=before_content,
            )
            self._journal.append_event(
                JournalEventInput(
                    proposal_id=proposal_id,
                    event_type="abort",
                    target_uri=target_uri,
                    content_hash=content_hash,
                    actor=actor,
                    metadata={"error": str(exc)},
                )
            )
            self._queue.update_status(proposal_id, "failed", error_message=str(exc))
            after_hash = self._effect_state_hash(proposal_id, target_path)
            if after_hash != before_hash:
                raise RuntimeError(
                    "knowledge vault rollback did not restore the exact target state"
                ) from exc
            material_action.record_terminal(
                MaterialActionTerminal(
                    status="failed_terminal",
                    target_effect_id=permit.effect_id,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"material-command:{permit.command_id}",
                        f"decision-revision:{permit.decision_revision_id}",
                        f"material-effect:{permit.effect_id}",
                        f"attempted-effect:{permit.effect_id}",
                        f"target-oracle:knowledge-vault:{after_hash}",
                        f"rollback:knowledge-vault:{after_hash}",
                    ),
                    reason_code="knowledge_vault_write_failed",
                    outcome="target rolled back to its exact before state",
                    created_at=utc_now_iso(),
                )
            )
            if origin_material_action is not None:
                markdown_after_hash = trusted_markdown_target_state_hash(target_path)
                record_trusted_markdown_observed_terminal(
                    origin_material_action,
                    status="failed_terminal",
                    before_hash=markdown_before_hash,
                    after_hash=markdown_after_hash,
                    reason_code="knowledge_vault_write_failed",
                    evidence_refs=(
                        f"target-oracle:trusted-markdown:{markdown_after_hash}",
                        f"rollback:trusted-markdown:{markdown_after_hash}",
                    ),
                )
            raise

        after_hash = self._effect_state_hash(proposal_id, target_path)
        material_action.record_terminal(
            MaterialActionTerminal(
                status="committed",
                target_effect_id=permit.effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"material-command:{permit.command_id}",
                    f"decision-revision:{permit.decision_revision_id}",
                    f"material-effect:{permit.effect_id}",
                    f"target-after:{after_hash}",
                    f"target-journal:trusted-write:{proposal_id}:{after_hash}",
                ),
                outcome="native store and formal Markdown target committed",
                created_at=utc_now_iso(),
            )
        )
        if origin_material_action is not None:
            markdown_after_hash = trusted_markdown_target_state_hash(target_path)
            record_trusted_markdown_observed_terminal(
                origin_material_action,
                status="committed",
                before_hash=markdown_before_hash,
                after_hash=markdown_after_hash,
                evidence_refs=(
                    f"target-journal:trusted-write:{proposal_id}:{markdown_after_hash}",
                ),
            )
        try:
            from core.mnemos_bus import get_event_bus

            get_event_bus().resume_deferred(proposal_id)
        except (OSError, sqlite3.Error, RuntimeError, ImportError):
            logger.warning(
                "Failed to resume events for committed proposal %s",
                proposal_id,
                exc_info=True,
            )
        return {"status": "committed", "path": str(markdown_result.path)}

    def _recover_origin_markdown_terminal(self, proposal: Any) -> None:
        """Close an approved origin command from the committed proposal target."""

        from core.trust.vault_mutation_service import (
            bind_trusted_markdown_candidate_action,
            record_trusted_markdown_observed_terminal,
            trusted_markdown_target_state_hash,
        )

        authorization = bind_trusted_markdown_candidate_action(
            proposal.candidate,
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
        )
        if authorization is None:
            return
        before_hash = str(
            proposal.candidate.payload.get("material_before_hash") or ""
        )
        if not before_hash.startswith("sha256:") or len(before_hash) != 71:
            raise RuntimeError(
                "committed trusted proposal lacks its origin before hash"
            )
        target_path = Path(proposal.candidate.target_path or "").expanduser()
        after_hash = trusted_markdown_target_state_hash(target_path)
        record_trusted_markdown_observed_terminal(
            authorization,
            status="committed",
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"target-journal:trusted-write:{proposal.proposal_id}:{after_hash}",
            ),
        )

    def _delete_native_store_record(self, proposal_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM native_store WHERE proposal_id = ?", (proposal_id,))

    def _effect_state_hash(self, proposal_id: str, target_path: Path) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT target_uri, content_hash, payload_json, written_at
                FROM native_store WHERE proposal_id=?
                """,
                (proposal_id,),
            ).fetchone()
        return _knowledge_vault_state_hash(proposal_id, target_path, row)

    @staticmethod
    def _restore_markdown_target(
        target_path: Path,
        *,
        existed: bool,
        content: str,
    ) -> None:
        if existed:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target_path, content, encoding="utf-8")
        elif target_path.is_file():
            target_path.unlink()


def _knowledge_vault_state_hash(
    proposal_id: str,
    target_path: Path,
    native_row: sqlite3.Row | None,
) -> str:
    exists = target_path.is_file()
    content = read_markdown_text(target_path) if exists else ""
    native_state = (
        {
            key: native_row[key]
            for key in (
                "target_uri",
                "content_hash",
                "payload_json",
                "written_at",
            )
        }
        if native_row is not None
        else None
    )
    return cognitive_sha256_json(
        {
            "proposal_id": proposal_id,
            "native_store": native_state,
            "markdown": {
                "path": str(target_path.resolve(strict=False)),
                "exists": exists,
                "content_hash": sha256_text(content),
            },
        }
    )
