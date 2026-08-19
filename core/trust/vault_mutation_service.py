"""Trusted proposal bridge for formal Markdown vault mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Protocol

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionTerminal,
    require_material_action,
    resolve_material_action_authorization,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json as cognitive_sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.trust.config import load_trusted_push_config
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import CandidateBundle, sha256_text, utc_now_iso
from core.trust.proposal_queue import ProposalQueue
from core.utils import atomic_write_text


TRUSTED_MARKDOWN_ACTION_TYPE = "formal_markdown_mutation"
TRUSTED_MARKDOWN_OWNER = "trusted_vault"
TRUSTED_MARKDOWN_EXECUTOR = "trusted_vault_mutation_service"


def trusted_markdown_material_action_binding(
    *,
    target_path: Path,
    content: str,
    proposed_action: str,
    expected_existing_hash: str | None = None,
    source_path: str | Path = "",
    source_content_hash: str = "",
) -> dict[str, str]:
    """Return the exact target and content mutation hash a permit must bind."""

    target = Path(target_path).expanduser().resolve(strict=False)
    source = (
        Path(source_path).expanduser().resolve(strict=False)
        if str(source_path or "")
        else None
    )
    payload = {
        "schema_version": "mnemos.trusted_markdown_material_input.v1",
        "target_path": str(target),
        "content_hash": sha256_text(content),
        "proposed_action": str(proposed_action or "update_markdown"),
        "expected_existing_hash": expected_existing_hash,
        "source_path": str(source) if source is not None else "",
        "source_content_hash": str(source_content_hash or ""),
    }
    return {
        "target_ref": f"markdown:{target}",
        "input_hash": cognitive_sha256_json(payload),
    }


MARKDOWN_EFFECT_INTENT_SQL = """
CREATE TABLE IF NOT EXISTS trusted_markdown_effect_intents (
    command_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE,
    decision_revision_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_path TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    desired_content_hash TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class TrustedMarkdownEffectOracle:
    """Observe an exact file mutation from its durable pre-effect intent."""

    owner = TRUSTED_MARKDOWN_OWNER
    executor_id = TRUSTED_MARKDOWN_EXECUTOR
    action_type = TRUSTED_MARKDOWN_ACTION_TYPE

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Observe the exact filesystem mutation bound to a durable intent."""

        if not self.db_path.is_file():
            return None
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            table = conn.execute(
                """SELECT 1 FROM sqlite_master WHERE type='table'
                   AND name='trusted_markdown_effect_intents'"""
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                """SELECT * FROM trusted_markdown_effect_intents
                   WHERE command_id=?""",
                (permit.command_id,),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        expected = {
            "command_id": permit.command_id,
            "effect_id": permit.effect_id,
            "decision_revision_id": permit.decision_revision_id,
            "action_id": permit.action_id,
            "owner": permit.owner,
            "executor_id": permit.executor_id,
            "action_type": permit.action_type,
            "target_ref": permit.target_ref,
            "input_hash": permit.input_hash,
        }
        if any(str(payload.get(key) or "") != value for key, value in expected.items()):
            raise RuntimeError(
                "trusted Markdown intent does not match its material command"
            )
        target = Path(str(payload["target_path"]))
        source = Path(str(payload["source_path"])) if payload["source_path"] else None
        desired_hash = str(payload["desired_content_hash"])
        operation = str(payload["operation"])
        if operation == "write":
            if not target.is_file() or sha256_text(
                read_markdown_text(target)
            ) != desired_hash:
                return None
            after_hash = _target_state_hash(target)
            evidence_ref = (
                f"target-oracle:markdown:{target.resolve(strict=False)}:{after_hash}"
            )
        elif operation == "delete":
            if target.exists():
                return None
            after_hash = _target_state_hash(target)
            evidence_ref = (
                "target-oracle:markdown-delete:"
                f"{target.resolve(strict=False)}:{after_hash}"
            )
        elif operation == "move" and source is not None:
            if (
                source.exists()
                or not target.is_file()
                or sha256_text(read_markdown_text(target)) != desired_hash
            ):
                return None
            after_hash = _move_state_hash(source, target)
            evidence_ref = (
                "target-oracle:markdown-move:"
                f"{source.resolve(strict=False)}:"
                f"{target.resolve(strict=False)}:{after_hash}"
            )
        else:
            raise RuntimeError("trusted Markdown intent operation is invalid")
        return MaterialActionObservation(
            status="committed",
            before_hash=str(payload["before_hash"]),
            after_hash=after_hash,
            evidence_refs=(f"target-after:{after_hash}", evidence_ref),
            outcome="formal Markdown target committed",
            observed_at=str(payload["created_at"]),
        )


def _record_markdown_effect_intent(
    *,
    db_path: Path,
    permit: MaterialActionPermit,
    operation: str,
    target_path: Path,
    source_path: Path | None,
    desired_content_hash: str,
    before_hash: str,
) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    values = (
        permit.command_id,
        permit.effect_id,
        permit.decision_revision_id,
        permit.action_id,
        permit.owner,
        permit.executor_id,
        permit.action_type,
        permit.target_ref,
        permit.input_hash,
        operation,
        str(target_path.resolve(strict=False)),
        str(source_path.resolve(strict=False)) if source_path is not None else "",
        desired_content_hash,
        before_hash,
        utc_now_iso(),
    )
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        conn.execute(MARKDOWN_EFFECT_INTENT_SQL)
        try:
            conn.execute(
                """INSERT INTO trusted_markdown_effect_intents (
                       command_id, effect_id, decision_revision_id, action_id,
                       owner, executor_id, action_type, target_ref, input_hash,
                       operation, target_path, source_path, desired_content_hash,
                       before_hash, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        except sqlite3.IntegrityError:
            existing = conn.execute(
                """SELECT * FROM trusted_markdown_effect_intents
                   WHERE command_id=?""",
                (permit.command_id,),
            ).fetchone()
            if existing is None or tuple(existing)[:-1] != values[:-1]:
                raise RuntimeError(
                    "trusted Markdown effect intent conflicts with its command"
                ) from None


def recover_pending_trusted_markdown_effects(
    *,
    db_path: Path,
    state_db_path: Path,
) -> tuple[str, ...]:
    """Close observed file effects from durable intents without rewriting files."""

    db_path = Path(db_path)
    state_db_path = Path(state_db_path)
    if not db_path.is_file() or not state_db_path.is_file():
        return ()
    with sqlite3.connect(
        f"file:{db_path.resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        table = conn.execute(
            """SELECT 1 FROM sqlite_master WHERE type='table'
               AND name='trusted_markdown_effect_intents'"""
        ).fetchone()
        if table is None:
            return ()
        command_ids = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT command_id FROM trusted_markdown_effect_intents"
            ).fetchall()
        )
    coordinator = MaterialActionCoordinator(CognitiveStateStore(state_db_path))
    oracle = TrustedMarkdownEffectOracle(db_path)
    recovered: list[str] = []
    for command_id in command_ids:
        with sqlite3.connect(
            f"file:{state_db_path.resolve(strict=True)}?mode=ro",
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
            executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            oracle=oracle,
        )
        if receipt is not None:
            recovered.append(command_id)
    return tuple(recovered)


class TrustedMutationReceipt(Protocol):
    """Receipt capability required by the only fallback Markdown commit helper."""

    @property
    def intercepted(self) -> bool: ...

    @property
    def target_path(self) -> str: ...

    @property
    def content_hash(self) -> str: ...

    @property
    def expected_existing_hash(self) -> str | None: ...

    @property
    def source_path(self) -> str: ...

    @property
    def source_content_hash(self) -> str: ...

    @property
    def proposed_action(self) -> str:
        """Return the canonical proposed mutation action."""

        ...

    @property
    def material_command_id(self) -> str:
        """Return the backing material command identifier."""

        ...

    @property
    def material_target_ref(self) -> str:
        """Return the exact target reference bound by the command."""

        ...

    @property
    def material_input_hash(self) -> str:
        """Return the exact input hash bound by the command."""

        ...

    @property
    def material_action(self) -> MaterialActionAuthorization | None:
        """Return the live typed authorization capability, if available."""

        ...

    @property
    def material_effect_db_path(self) -> str:
        """Return the target-local effect-journal database path."""

        ...


@dataclass(frozen=True)
class TrustedVaultMutationResult:
    action: str
    mode: str
    proposal_id: str = ""
    status: str = ""
    gate_decision: str = ""
    target_path: str = ""
    content_hash: str = ""
    expected_existing_hash: str | None = None
    source_path: str = ""
    source_content_hash: str = ""
    proposed_action: str = "update_markdown"
    material_command_id: str = ""
    material_target_ref: str = ""
    material_input_hash: str = ""
    material_effect_db_path: str = ""
    material_action: MaterialActionAuthorization | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def intercepted(self) -> bool:
        return self.action == "intercept"

    def to_dict(self) -> dict[str, Any]:
        """Return the public receipt without exposing its in-process capability."""

        return {
            "action": self.action,
            "mode": self.mode,
            "proposal_id": self.proposal_id,
            "status": self.status,
            "gate_decision": self.gate_decision,
            "target_path": self.target_path,
            "content_hash": self.content_hash,
            "expected_existing_hash": self.expected_existing_hash,
            "source_path": self.source_path,
            "source_content_hash": self.source_content_hash,
            "proposed_action": self.proposed_action,
            "material_command_id": self.material_command_id,
            "material_target_ref": self.material_target_ref,
            "material_input_hash": self.material_input_hash,
            "material_effect_db_path": self.material_effect_db_path,
        }


class TrustedVaultMutationService:
    """Submit formal Markdown mutations to trusted push when enabled."""

    def __init__(self, *, wiki_base: Path, config: Any | None = None):
        self.wiki_base = Path(wiki_base).expanduser()
        self.config = config or load_trusted_push_config(wiki_base=self.wiki_base)

    def submit_markdown(
        self,
        *,
        target_path: Path,
        content: str,
        source: str,
        actor: str = "system",
        source_session_id: str = "",
        evidence_refs: Iterable[str] = (),
        proposed_action: str = "update_markdown",
        expected_existing_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ) -> TrustedVaultMutationResult:
        """Return ``intercept`` in enforce mode; callers must then skip direct writes."""
        target = Path(target_path).expanduser()
        content_hash = sha256_text(content)
        source_path = str((metadata or {}).get("source_path", ""))
        source_content_hash = str((metadata or {}).get("source_content_hash", ""))
        binding = trusted_markdown_material_action_binding(
            target_path=target,
            content=content,
            proposed_action=proposed_action,
            expected_existing_hash=expected_existing_hash,
            source_path=source_path,
            source_content_hash=source_content_hash,
        )
        material_action, permit = resolve_material_action_authorization(
            material_action,
            owner=TRUSTED_MARKDOWN_OWNER,
            executor_id=TRUSTED_MARKDOWN_EXECUTOR,
            action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.config.db_path.parent / "producer_consumer_ledger.db",
        )
        if not self.config.enabled:
            return TrustedVaultMutationResult(
                action="write",
                mode=self.config.mode,
                target_path=str(target),
                content_hash=content_hash,
                expected_existing_hash=expected_existing_hash,
                source_path=source_path,
                source_content_hash=source_content_hash,
                proposed_action=proposed_action,
                material_command_id=permit.command_id,
                material_target_ref=binding["target_ref"],
                material_input_hash=binding["input_hash"],
                material_effect_db_path=str(self.config.db_path),
                material_action=material_action,
            )
        payload = {
            "content": content,
            "target_path": str(target),
            "expected_existing_hash": expected_existing_hash,
            "material_before_hash": _target_state_hash(target),
            "mutation_source": source,
            "actor": actor,
            "metadata": dict(metadata or {}),
            "material_action": {
                "command_id": permit.command_id,
                "decision_revision_id": permit.decision_revision_id,
                "action_id": permit.action_id,
                "effect_id": permit.effect_id,
                "executor_id": permit.executor_id,
                "target_ref": binding["target_ref"],
                "input_hash": binding["input_hash"],
            },
        }
        refs = [str(ref) for ref in evidence_refs if str(ref)]
        if not refs:
            refs = [f"target:{target}"]
        candidate = CandidateBundle.from_payload(
            source=source,
            source_agent=actor,
            source_session_id=source_session_id or None,
            target_kind="markdown",
            target_path=str(target),
            payload=payload,
            evidence_refs=refs,
            confidence_score=0.7,
            risk_level="medium",
            proposed_actions=[proposed_action],
        )
        proposal = ProposalQueue(
            self.config.db_path,
            wiki_base=self.wiki_base,
            config=self.config,
        ).submit_candidate(candidate, shadow=self.config.shadow)
        if self.config.enforce and proposal.status == "rejected":
            record_trusted_markdown_no_effect_terminal(
                material_action,
                target_path=target,
                status="rejected",
                reason_code="trusted_push_gate_rejected",
                evidence_ref=f"target-journal:trusted-gate-reject:{proposal.proposal_id}",
            )
        return TrustedVaultMutationResult(
            action="intercept" if self.config.enforce else "write",
            mode=self.config.mode,
            proposal_id=proposal.proposal_id,
            status=proposal.status,
            gate_decision=proposal.gate_decision,
            target_path=str(target),
            content_hash=content_hash,
            expected_existing_hash=expected_existing_hash,
            source_path=source_path,
            source_content_hash=source_content_hash,
            proposed_action=proposed_action,
            material_command_id=permit.command_id,
            material_target_ref=binding["target_ref"],
            material_input_hash=binding["input_hash"],
            material_effect_db_path=str(self.config.db_path),
            material_action=material_action,
        )


def bind_trusted_markdown_candidate_action(
    candidate: CandidateBundle,
    *,
    state_db_path: Path,
) -> MaterialActionAuthorization | None:
    """Rehydrate and validate the exact Markdown command carried by a proposal.

    Generic proposals created outside ``TrustedVaultMutationService`` have no
    upstream Markdown command and return ``None``.  A proposal that claims to
    carry one must match every immutable field in the canonical permit before
    the target can be approved, rejected, or superseded.
    """

    payload = candidate.payload
    serialized = payload.get("material_action")
    if serialized is None:
        return None
    if not isinstance(serialized, Mapping):
        raise PermissionError("trusted proposal material action is malformed")
    command_id = str(serialized.get("command_id") or "").strip()
    if not command_id:
        raise PermissionError("trusted proposal material command is missing")
    coordinator = MaterialActionCoordinator(CognitiveStateStore(state_db_path))
    authorization = coordinator.bind_for_recovery(
        command_id,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
    )
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    proposed_action = str(
        candidate.proposed_actions[0]
        if candidate.proposed_actions
        else "update_markdown"
    )
    binding = trusted_markdown_material_action_binding(
        target_path=Path(candidate.target_path or ""),
        content=str(payload.get("content", "")),
        proposed_action=proposed_action,
        expected_existing_hash=(
            str(payload["expected_existing_hash"])
            if payload.get("expected_existing_hash") is not None
            else None
        ),
        source_path=str(payload.get("source_path") or metadata.get("source_path") or ""),
        source_content_hash=str(
            payload.get("source_content_hash")
            or metadata.get("source_content_hash")
            or ""
        ),
    )
    authorization, permit = resolve_material_action_recovery_authorization(
        authorization,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=state_db_path,
    )
    serialized_identity = {
        "command_id": permit.command_id,
        "decision_revision_id": permit.decision_revision_id,
        "action_id": permit.action_id,
        "effect_id": permit.effect_id,
        "executor_id": permit.executor_id,
        "target_ref": permit.target_ref,
        "input_hash": permit.input_hash,
    }
    if any(serialized.get(key) != value for key, value in serialized_identity.items()):
        raise PermissionError(
            "trusted proposal material action does not match its canonical command"
        )
    return authorization


def trusted_markdown_target_state_hash(path: Path) -> str:
    """Return the independent canonical state hash for one Markdown target."""

    return _target_state_hash(path)


def record_trusted_markdown_no_effect_terminal(
    authorization: MaterialActionAuthorization,
    *,
    target_path: Path,
    status: str,
    reason_code: str,
    evidence_ref: str,
) -> None:
    """Close an intercepted exact write with proof that its target did not run."""

    if status not in {"rejected", "revoked"}:
        raise ValueError("trusted Markdown no-effect status must be rejected or revoked")
    state_hash = _target_state_hash(target_path)
    permit = authorization.permit
    authorization.record_terminal(
        MaterialActionTerminal(
            status=status,
            target_effect_id=permit.effect_id,
            before_hash=state_hash,
            after_hash=state_hash,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"no-effect-oracle:{permit.effect_id}:{state_hash}",
                evidence_ref,
            ),
            reason_code=reason_code,
            outcome="formal Markdown target was not mutated",
            created_at=utc_now_iso(),
        )
    )


def record_trusted_markdown_observed_terminal(
    authorization: MaterialActionAuthorization,
    *,
    status: str,
    before_hash: str,
    after_hash: str,
    reason_code: str = "",
    evidence_refs: Iterable[str] = (),
) -> None:
    """Record an approved proposal's observed Markdown effect on its origin command."""

    permit = authorization.permit
    refs = [
        f"material-command:{permit.command_id}",
        f"decision-revision:{permit.decision_revision_id}",
        f"material-effect:{permit.effect_id}",
    ]
    if status == "committed":
        refs.append(f"target-after:{after_hash}")
    elif status in {"failed_terminal", "dead_letter"}:
        refs.append(f"attempted-effect:{permit.effect_id}")
    refs.extend(str(ref) for ref in evidence_refs if str(ref))
    authorization.record_terminal(
        MaterialActionTerminal(
            status=status,
            target_effect_id=permit.effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=tuple(refs),
            reason_code=reason_code,
            retry_exhausted=status == "dead_letter",
            outcome=(
                "formal Markdown target committed through approved trusted proposal"
                if status == "committed"
                else "approved trusted proposal did not change the canonical target"
            ),
            created_at=utc_now_iso(),
        )
    )


def commit_trusted_markdown(
    receipt: TrustedMutationReceipt,
    *,
    target_path: Path,
    content: str,
    encoding: str = "utf-8",
    material_action: MaterialActionAuthorization | None = None,
) -> bool:
    """Commit only after a typed trusted-push receipt authorizes fallback writing."""

    if receipt.intercepted:
        return False
    target = Path(target_path).expanduser()
    _validate_receipt_binding(receipt, target_path=target, content=content)
    authorization = _require_receipt_material_action(
        receipt,
        material_action,
        target_path=target,
        content=content,
    )
    before_hash = _target_state_hash(target)
    effect_db = _material_effect_db_path(receipt, authorization)
    oracle = TrustedMarkdownEffectOracle(effect_db)
    if _recover_trusted_markdown_effect(authorization, oracle):
        return True
    permit = require_material_action(
        authorization,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=receipt.material_target_ref,
        input_hash=receipt.material_input_hash,
    )
    _record_markdown_effect_intent(
        db_path=effect_db,
        permit=permit,
        operation="write",
        target_path=target,
        source_path=None,
        desired_content_hash=sha256_text(content),
        before_hash=before_hash,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content, encoding=encoding)
    if not _recover_trusted_markdown_effect(authorization, oracle):
        raise RuntimeError("trusted Markdown write intent was not recoverable")
    return True


def commit_trusted_markdown_delete(
    receipt: TrustedMutationReceipt,
    *,
    target_path: Path,
    material_action: MaterialActionAuthorization | None = None,
) -> bool:
    """Delete a formal page only after the trusted submission permits mutation."""

    if receipt.intercepted:
        return False
    target = Path(target_path).expanduser()
    _validate_receipt_binding(receipt, target_path=target, content="")
    authorization = _require_receipt_material_action(
        receipt,
        material_action,
        target_path=target,
        content="",
    )
    before_hash = _target_state_hash(target)
    effect_db = _material_effect_db_path(receipt, authorization)
    oracle = TrustedMarkdownEffectOracle(effect_db)
    if _recover_trusted_markdown_effect(authorization, oracle):
        return True
    permit = require_material_action(
        authorization,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=receipt.material_target_ref,
        input_hash=receipt.material_input_hash,
    )
    _record_markdown_effect_intent(
        db_path=effect_db,
        permit=permit,
        operation="delete",
        target_path=target,
        source_path=None,
        desired_content_hash=sha256_text(""),
        before_hash=before_hash,
    )
    if target.is_file():
        target.unlink()
    if not _recover_trusted_markdown_effect(authorization, oracle):
        raise RuntimeError("trusted Markdown delete intent was not recoverable")
    return True


def commit_trusted_markdown_move(
    receipt: TrustedMutationReceipt,
    *,
    source_path: Path,
    target_path: Path,
    content: str,
    encoding: str = "utf-8",
    material_action: MaterialActionAuthorization | None = None,
) -> bool:
    """Materialize a proposed destination and remove its source under one receipt."""

    if receipt.intercepted:
        return False
    source = Path(source_path).expanduser()
    target = Path(target_path).expanduser()
    _validate_receipt_binding(
        receipt,
        target_path=target,
        content=content,
        source_path=source,
    )
    authorization = _require_receipt_material_action(
        receipt,
        material_action,
        target_path=target,
        content=content,
        source_path=source,
    )
    before_hash = _move_state_hash(source, target)
    effect_db = _material_effect_db_path(receipt, authorization)
    oracle = TrustedMarkdownEffectOracle(effect_db)
    if _recover_trusted_markdown_effect(authorization, oracle):
        return True
    permit = require_material_action(
        authorization,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=receipt.material_target_ref,
        input_hash=receipt.material_input_hash,
    )
    _record_markdown_effect_intent(
        db_path=effect_db,
        permit=permit,
        operation="move",
        target_path=target,
        source_path=source,
        desired_content_hash=sha256_text(content),
        before_hash=before_hash,
    )
    if source != target and target.exists():
        raise FileExistsError(f"trusted move target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content, encoding=encoding)
    try:
        if source != target and source.is_file():
            source.unlink()
    except OSError:
        if source != target and target.is_file():
            target.unlink()
        raise
    if not _recover_trusted_markdown_effect(authorization, oracle):
        raise RuntimeError("trusted Markdown move intent was not recoverable")
    return True


def _require_receipt_material_action(
    receipt: TrustedMutationReceipt,
    authorization: MaterialActionAuthorization | None,
    *,
    target_path: Path,
    content: str,
    source_path: Path | None = None,
) -> MaterialActionAuthorization:
    if authorization is None:
        authorization = receipt.material_action
    binding = trusted_markdown_material_action_binding(
        target_path=target_path,
        content=content,
        proposed_action=str(receipt.proposed_action),
        expected_existing_hash=receipt.expected_existing_hash,
        source_path=source_path or receipt.source_path,
        source_content_hash=receipt.source_content_hash,
    )
    authorization, permit = resolve_material_action_recovery_authorization(
        authorization,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    if receipt.material_command_id != permit.command_id:
        raise PermissionError(
            "trusted mutation receipt command does not match its material permit"
        )
    if (
        receipt.material_target_ref != binding["target_ref"]
        or receipt.material_input_hash != binding["input_hash"]
    ):
        raise PermissionError(
            "trusted mutation receipt binding does not match the commit effect"
        )
    return authorization


def _recover_trusted_markdown_effect(
    authorization: MaterialActionAuthorization,
    oracle: TrustedMarkdownEffectOracle,
) -> bool:
    receipt = authorization.recover(oracle)
    if receipt is None:
        return False
    if oracle.observe(authorization.permit) is None:
        raise RuntimeError(
            "terminal trusted Markdown receipt lacks its exact target evidence"
        )
    return True


def _material_effect_db_path(
    receipt: TrustedMutationReceipt,
    authorization: MaterialActionAuthorization,
) -> Path:
    configured = str(getattr(receipt, "material_effect_db_path", "") or "")
    if configured:
        return Path(configured).expanduser()
    return (
        Path(authorization.coordinator.state_store.db_path).parent
        / "trusted_push.db"
    )


def _target_state_hash(path: Path) -> str:
    target = Path(path).expanduser()
    exists = target.is_file()
    content = read_markdown_text(target) if exists else ""
    return str(
        cognitive_sha256_json(
            {
                "path": str(target.resolve(strict=False)),
                "exists": exists,
                "content_hash": sha256_text(content),
            }
        )
    )


def _move_state_hash(source: Path, target: Path) -> str:
    return str(
        cognitive_sha256_json(
            {
                "source": _target_state_hash(source),
                "target": _target_state_hash(target),
            }
        )
    )


def _validate_receipt_binding(
    receipt: TrustedMutationReceipt,
    *,
    target_path: Path,
    content: str,
    source_path: Path | None = None,
) -> None:
    target = target_path.resolve(strict=False)
    receipt_target = Path(receipt.target_path).expanduser().resolve(strict=False)
    if not receipt.target_path or receipt_target != target:
        raise ValueError("trusted mutation receipt target does not match commit target")
    if receipt.content_hash != sha256_text(content):
        raise ValueError("trusted mutation receipt content hash does not match commit content")
    if source_path is not None:
        receipt_source = Path(receipt.source_path).expanduser().resolve(strict=False)
        if not receipt.source_path or receipt_source != source_path.resolve(strict=False):
            raise ValueError("trusted mutation receipt source does not match move source")
        if not source_path.is_file():
            raise FileNotFoundError(f"trusted move source does not exist: {source_path}")
        source_content = read_markdown_text(source_path)
        if not receipt.source_content_hash or sha256_text(source_content) != receipt.source_content_hash:
            raise ValueError("trusted mutation move source changed after submission")
    if receipt.expected_existing_hash is not None:
        existing = read_markdown_text(target_path) if target_path.is_file() else ""
        if sha256_text(existing) != receipt.expected_existing_hash:
            raise ValueError("trusted mutation target changed after submission")
