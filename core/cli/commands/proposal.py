"""Trusted push proposal CLI."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.config import get_config
from core.cli.local_principal import local_cli_identity
from core.trust.config import load_trusted_push_config
from core.trust.dialog_push import DialogDecisionPush
from core.trust.proposal_queue import ProposalQueue
from core.trust.recovery import TrustedPushRecovery
from core.trust.write_journal import WriteJournal


def _paths(args: Any) -> tuple[Path, Path]:
    cfg = get_config()
    wiki_base = Path(getattr(args, "wiki_base", "") or cfg.wiki_dir)
    trusted = load_trusted_push_config(cfg, wiki_base=wiki_base)
    db_path = Path(getattr(args, "db_path", "") or trusted.db_path)
    return wiki_base, db_path


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if isinstance(payload, list):
            for item in payload:
                print(item)
        else:
            print(payload)


def _cmd_list(args: Any) -> int:
    wiki_base, db_path = _paths(args)
    statuses = [args.status] if getattr(args, "status", "") else None
    proposals = ProposalQueue(db_path, wiki_base=wiki_base).list(
        statuses=statuses,
        limit=int(getattr(args, "limit", 50) or 50),
    )
    if getattr(args, "json", False):
        _emit([p.to_dict() for p in proposals], as_json=True)
    else:
        lines = [
            f"{p.proposal_id} {p.status} risk={p.risk_level} target={p.candidate.target_path}"
            for p in proposals
        ]
        _emit(lines, as_json=False)
    return 0


def _cmd_show(args: Any) -> int:
    wiki_base, db_path = _paths(args)
    proposal = ProposalQueue(db_path, wiki_base=wiki_base).get(args.proposal_id)
    _emit(proposal.to_dict(), as_json=getattr(args, "json", False))
    return 0


def _confirm_approve(args: Any) -> int | None:
    if getattr(args, "yes", False):
        return None
    answer = input("Approve this proposal and write to vault? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        return 2
    return None


def _content_from_args(args: Any, current_content: str) -> str:
    if getattr(args, "content", None) is not None:
        return str(args.content)
    if getattr(args, "content_file", None):
        with Path(args.content_file).open("r", encoding="utf-8") as handle:
            return handle.read()

    editor = str(getattr(args, "editor", "") or "")
    if not editor:
        raise RuntimeError("--editor is required when --content/--content-file is not provided")
    with tempfile.NamedTemporaryFile(
        "w+",
        encoding="utf-8",
        suffix=".md",
        prefix="mnemos-proposal-",
        delete=False,
    ) as handle:
        handle.write(current_content)
        tmp_path = Path(handle.name)
    try:
        subprocess.run([editor, str(tmp_path)], check=True)
        with tmp_path.open("r", encoding="utf-8") as handle:
            return handle.read()
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _cmd_edit(args: Any) -> int:
    return _run_decision(args, "edit")


def _cmd_recover(args: Any) -> int:
    _, db_path = _paths(args)
    results = TrustedPushRecovery(db_path).recover(apply=bool(getattr(args, "apply", False)))
    _emit(results, as_json=getattr(args, "json", False))
    return 0


def _cmd_audit(args: Any) -> int:
    _, db_path = _paths(args)
    journal = WriteJournal(db_path)
    payload = {
        "hash_chain_ok": journal.verify_hash_chain(),
        "open_prepares": journal.open_prepares(),
    }
    _emit(payload, as_json=getattr(args, "json", False))
    return 0 if payload["hash_chain_ok"] and not payload["open_prepares"] else 1


def _cmd_push(args: Any) -> int:
    wiki_base, db_path = _paths(args)
    result = DialogDecisionPush(wiki_base=wiki_base, db_path=db_path).push(
        limit=int(getattr(args, "limit", 5) or 5),
        surface="whitebox",
    )
    _emit(result, as_json=getattr(args, "json", False))
    return 0


def _cmd_reject(args: Any) -> int:
    return _run_decision(args, "reject")


def _cmd_approve(args: Any) -> int:
    confirm_status = _confirm_approve(args)
    if confirm_status is not None:
        return confirm_status
    return _run_decision(args, "approve")


def _cmd_decide(args: Any) -> int:
    action = str(args.action)
    if action == "approve":
        confirm_status = _confirm_approve(args)
        if confirm_status is not None:
            return confirm_status
    return _run_decision(args, action)


def _run_decision(args: Any, action: str) -> int:
    wiki_base, db_path = _paths(args)
    proposal = ProposalQueue(db_path, wiki_base=wiki_base).get(args.proposal_id)
    content = None
    if action == "edit":
        content = _content_from_args(
            args,
            str(proposal.candidate.payload.get("content", "")),
        )
    principal, narrowing = local_cli_identity(
        project="mnemos",
        session_id=str(proposal.candidate.source_session_id or ""),
    )
    result = DialogDecisionPush(wiki_base=wiki_base, db_path=db_path).decide(
        args.proposal_id,
        action,
        content=content,
        reason=getattr(args, "reason", "") or "",
        allow_high_risk=bool(getattr(args, "allow_high_risk", False)),
        snooze_hours=int(getattr(args, "snooze_hours", 24) or 24),
        principal=principal,
        narrowing=narrowing,
    )
    _emit(result, as_json=getattr(args, "json", False))
    return 0 if result.get("status") not in {"failed"} else 1


def cmd_proposal(args: Any) -> int:
    handlers = {
        "list": _cmd_list,
        "show": _cmd_show,
        "approve": _cmd_approve,
        "reject": _cmd_reject,
        "edit": _cmd_edit,
        "recover": _cmd_recover,
        "audit": _cmd_audit,
        "push": _cmd_push,
        "decide": _cmd_decide,
    }
    handler = handlers.get(getattr(args, "proposal_cmd", ""))
    if handler is None:
        print("可用子命令: list, show, approve, reject, edit, recover, audit, push, decide")
        return 2
    return handler(args)
