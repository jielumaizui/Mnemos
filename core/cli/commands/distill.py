"""Distill command for Mnemos CLI."""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


from core.cli.helpers import _get_config
from core.cognitive.state_contract import sha256_json
from core.frontmatter import fm_get, parse_frontmatter, write_frontmatter
from core.hephaestus.distillation_prompts import PROMPT_VERSION
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.privacy.redaction import redact_path
from core.ops.durable_io import read_native_bytes
from core.trust.formal_markdown import (
    TrustedMarkdownDecisionPolicy,
    submit_or_write_markdown_with_decision,
)
from core.trust.models import sha256_text

logger = logging.getLogger(__name__)

DISTILL_METADATA_BACKFILL_MARKDOWN_POLICY = TrustedMarkdownDecisionPolicy(
    contract_id="project-contract:distill-metadata-backfill",
    contract_revision_id="mnemos.distill_metadata_backfill.v1",
    contract_text=(
        "The distill metadata backfill command may add only the exact canonical "
        "legacy metadata fields computed for one reviewed distilled-page preimage."
    ),
    source_namespace="distill-metadata-backfill",
    producer="distill-metadata-backfill-cli",
    producer_code_hash=sha256_json(
        {
            "module": "core.cli.commands.distill",
            "producer": "_cmd_distill_backfill_metadata",
            "version": "mnemos.distill_metadata_backfill.v1",
        }
    ),
    evaluator_id="distill-metadata-backfill-evaluator",
    constraints=(
        "Target, page preimage, computed updates, and rendered bytes remain exact.",
        "The command may not classify a page that fails the distilled-page predicate.",
    ),
    approved_candidate_key="apply_exact_distill_metadata",
    approved_candidate_summary="Apply the exact computed legacy distillation metadata.",
    rejected_candidate_key="retain_legacy_distill_metadata",
    rejected_candidate_summary="Retain the page when classification or bytes drift.",
    approved_reason_code="distill_metadata_binding_verified",
    rejected_reason_code="distill_metadata_binding_rejected",
    committed_metric="distill_metadata_backfill_committed",
    rejected_metric="unbound_distill_metadata_backfill_count",
)


def cmd_distill(args):
    """蒸馏层管理"""
    if args.distill_cmd == "status":
        _cmd_distill_status(args)
    elif args.distill_cmd == "drain":
        _cmd_distill_drain(args)
    elif args.distill_cmd == "audit":
        _cmd_distill_audit(args)
    elif args.distill_cmd == "backfill-metadata":
        _cmd_distill_backfill_metadata(args)
    elif args.distill_cmd == "evidence-backfill":
        _cmd_distill_evidence_backfill(args)
    elif args.distill_cmd == "actions":
        _cmd_distill_actions(args)
    elif args.distill_cmd == "retry-failed":
        return _cmd_distill_retry_failed(args)
    elif args.distill_cmd == "archive-failed":
        return _cmd_distill_archive_failed(args)
    elif args.distill_cmd == "reset-timeouts":
        return _cmd_distill_reset_timeouts(args)
    else:
        print(
            "用法: mnemos distill "
            "{status|drain|audit|backfill-metadata|evidence-backfill|actions|"
            "retry-failed|archive-failed|reset-timeouts}"
        )
        return 1
    return 0


def _amphora_status_counts(db_path: Path) -> dict[str, object]:
    statuses = (
        "pending",
        "processing",
        "done",
        "committed",
        "intentional_skip",
        "proposal_pending",
        "partial",
        "retryable_failed",
        "reconciliation_required",
        "failed",
        "archived",
    )
    empty: dict[str, object] = {status: 0 for status in statuses}
    empty["total"] = 0
    try:
        kind = inspect_path_kind(db_path)
    except DurableIOError:
        return {**empty, "state": "unavailable", "error": "distill_queue_path_unavailable"}
    if kind == "missing":
        return {**empty, "state": "uninitialized", "error": ""}
    if kind != "file":
        return {**empty, "state": "unavailable", "error": "distill_queue_path_not_regular"}
    try:
        with connect_readonly_sqlite(db_path, timeout_seconds=5) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='distillation_tasks'"
            ).fetchone()
            if not table:
                return {**empty, "state": "uninitialized", "error": ""}
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM distillation_tasks GROUP BY status"
            ).fetchall()
        counts = dict(empty)
        for status, count in rows:
            if status in counts:
                counts[str(status)] = int(count)
        counts["total"] = sum(int(count) for _status, count in rows)
        counts["state"] = "current"
        counts["error"] = ""
        return counts
    except (DurableIOError, OSError, ValueError, TypeError, sqlite3.Error) as exc:
        logger.warning("读取 amphora 队列统计失败: %s", exc)
        return {**empty, "state": "unavailable", "error": "distill_queue_unreadable"}


def _cmd_distill_status(args):
    """Print distillation queue status and actionable next commands."""
    from core.cli.helpers import _daemon_processes
    from core.config import Config
    from core.ops.config_scope import use_config

    config = Config(provision=False)
    with use_config(config):
        stats = _distill_status_snapshot(config)
        counts = _amphora_status_counts(
            Path(config.database_dir) / "distill_queue.db"
        )
    daemon_count = len(_daemon_processes())
    show_paths = bool(
        getattr(args, "unsafe_debug", False) or getattr(args, "show_paths", False)
    )

    def _display_path(value):
        return str(value) if show_paths else redact_path(value)

    print("蒸馏队列状态:")
    print(f"  pending: {stats.get('pending', 0)}")
    print(f"  queue_dir: {_display_path(stats.get('queue_dir'))}")
    print(f"  inbox_dir: {_display_path(stats.get('inbox_dir'))}")
    if counts.get("state") == "unavailable":
        print("  amphora: unavailable")
    elif counts:
        print(
            "  amphora: "
            f"total={counts['total']}, pending={counts['pending']}, "
            f"processing={counts['processing']}, done={counts['done']}, "
            f"failed={counts['failed']}, archived={counts['archived']}"
        )
    print(f"  daemon_running: {daemon_count > 0} ({daemon_count})")

    pending = int(stats.get("pending", 0) or counts.get("pending", 0) or 0)
    if pending > 0 and daemon_count == 0:
        print("  建议: 后台未运行，先执行 `python3 mnemos_cli.py daemon start`")
        print("  或手动处理: `python3 mnemos_cli.py distill drain --limit 5`")
    if counts.get("failed", 0) > 0:
        print("  修复失败任务: `python3 mnemos_cli.py distill retry-failed --all`")
        print("  归档不可重试任务: `python3 mnemos_cli.py distill archive-failed --all --reason ...`")
    if counts.get("processing", 0) > 0:
        print(
            "  检查卡住任务: "
            "`python3 mnemos_cli.py distill reset-timeouts --minutes 30 --json`"
        )


def _distill_status_snapshot(config) -> dict:
    """Read file-queue paths and counts without constructing a worker."""
    queue_dir = Path(config.database_dir) / "distill_queue"
    return {
        "pending": sum(1 for _ in queue_dir.glob("*.json")) if queue_dir.is_dir() else 0,
        "queue_dir": str(queue_dir),
        "inbox_dir": str(Path(config.wiki_dir) / "00-Inbox"),
        "archive_dir": str(Path(config.database_dir) / "distill_archive"),
    }


def _cmd_distill_drain(args):
    """Manually process a bounded number of distillation tasks."""
    from core.hephaestus_worker import HephaestusWorker

    limit = int(getattr(args, "limit", 5) or 5)
    if limit <= 0:
        print("--limit 必须大于 0")
        return

    worker = HephaestusWorker()
    stats = worker.get_stats()
    pending = int(stats.get("pending", 0) or 0)
    to_process = min(limit, pending)

    print("蒸馏队列手动处理:")
    print(f"  pending: {pending}")
    print(f"  limit: {limit}")
    if getattr(args, "dry_run", False):
        print(f"  dry-run: 将处理最多 {to_process} 个任务，不调用 LLM，不写 Wiki")
        return

    processed = worker.process_all(max_tasks=limit)
    print(f"  processed: {processed}")
    remaining = max(0, pending - processed)
    print(f"  estimated_remaining: {remaining}")


def _is_distilled_page(frontmatter: dict) -> bool:
    """Return True for pages generated from distillation."""
    return bool(
        fm_get(frontmatter, "source_session")
        or fm_get(frontmatter, "distilled_at")
        or frontmatter.get("蒸馏时间")
    )


def _distill_metadata_updates(frontmatter: dict) -> dict:
    """Build the minimal metadata patch needed for historical distill pages."""
    updates = {}

    if fm_get(frontmatter, "distill_prompt_version") != PROMPT_VERSION:
        updates["distill_prompt_version"] = PROMPT_VERSION
    if not fm_get(frontmatter, "source_coverage"):
        updates["source_coverage"] = "legacy_unknown"
    if not fm_get(frontmatter, "distill_input_mode"):
        updates["distill_input_mode"] = "legacy_unknown"

    return updates


def _cmd_distill_audit(args):
    """蒸馏完整性审计：报告截断、缺失 prompt_version、缺失 source_coverage"""

    config = _get_config()
    wiki_dir = config.wiki_dir

    if not wiki_dir.exists():
        print("Wiki 目录不存在")
        return

    md_files = list(wiki_dir.rglob("*.md"))
    total = len(md_files)
    pages_with_truncated_source = 0
    pages_without_prompt_version = 0
    pages_without_source_coverage = 0
    pages_with_old_prompt_version = 0

    for md_file in md_files:
        try:
            content = read_native_bytes(md_file).decode("utf-8")
            fm, _body = parse_frontmatter(content)
            if fm is None:
                continue

            # 只统计蒸馏生成的页面（有 source_session 或 distilled_at）
            if not _is_distilled_page(fm):
                continue

            if fm_get(fm, "truncated") is True:
                pages_with_truncated_source += 1
            prompt_version = fm_get(fm, "distill_prompt_version")
            if not prompt_version:
                pages_without_prompt_version += 1
            elif str(prompt_version) != PROMPT_VERSION:
                pages_with_old_prompt_version += 1
            if not fm_get(fm, "source_coverage"):
                pages_without_source_coverage += 1
        except (OSError, ValueError):
            continue

    print("蒸馏完整性审计结果:")
    print(f"  Wiki 页面总数: {total}")
    print(f"  截断输入页面: {pages_with_truncated_source}")
    print(f"  缺少 prompt_version: {pages_without_prompt_version}")
    print(f"  旧 prompt_version: {pages_with_old_prompt_version}")
    print(f"  缺少 source_coverage: {pages_without_source_coverage}")


def _cmd_distill_backfill_metadata(args):
    """Backfill traceability metadata for historical distilled wiki pages."""
    config = _get_config()
    wiki_dir = config.wiki_dir

    if not wiki_dir.exists():
        print("Wiki 目录不存在")
        return

    dry_run = bool(getattr(args, "dry_run", False))
    limit = getattr(args, "limit", None)

    scanned = 0
    distilled = 0
    changed = 0
    skipped = 0
    errors = 0

    for md_file in sorted(wiki_dir.rglob("*.md")):
        scanned += 1
        try:
            content = read_native_bytes(md_file).decode("utf-8")
            fm, body = parse_frontmatter(content)
            if fm is None:
                skipped += 1
                continue
            if not _is_distilled_page(fm):
                skipped += 1
                continue

            distilled += 1
            updates = _distill_metadata_updates(fm)
            if not updates:
                continue

            changed += 1
            if not dry_run:
                updated_fm = dict(fm)
                updated_fm.update(updates)
                evidence_refs = [f"wiki_page:{md_file.relative_to(wiki_dir)}"]
                updated_content = write_frontmatter(updated_fm, body)
                submit_or_write_markdown_with_decision(
                    decision_policy=DISTILL_METADATA_BACKFILL_MARKDOWN_POLICY,
                    decision_facts={
                        "schema_version": "mnemos.distill_metadata_backfill_facts.v1",
                        "updates": updates,
                        "prompt_version": PROMPT_VERSION,
                    },
                    decision_task=f"Backfill distillation metadata for {md_file.name}",
                    decision_goal="Make legacy distilled-page metadata explicit and auditable.",
                    decision_created_at=datetime.now(timezone.utc).isoformat(),
                    wiki_base=wiki_dir,
                    target_path=md_file,
                    content=updated_content,
                    source="distill_metadata_backfill",
                    actor="cli",
                    evidence_refs=evidence_refs,
                    proposed_action="backfill_distill_metadata",
                    expected_existing_hash=sha256_text(content),
                    metadata={"updates": sorted(updates.keys())},
                )

            if limit and changed >= limit:
                break
        except (OSError, ValueError) as exc:
            errors += 1
            logger.warning("蒸馏 metadata 回填失败: %s: %s", md_file, exc)

    action = "将回填" if dry_run else "已回填"
    print("蒸馏 metadata 回填结果:")
    print(f"  扫描页面: {scanned}")
    print(f"  蒸馏页面: {distilled}")
    print(f"  {action}: {changed}")
    print(f"  跳过页面: {skipped}")
    print(f"  错误: {errors}")


def _cmd_distill_evidence_backfill(args):
    """Backfill page-level source refs from provenance tables."""
    from core.ops.evidence_backfill import (
        dumps_report,
        format_evidence_backfill_text,
        run_evidence_backfill,
    )

    report = run_evidence_backfill(
        _get_config(),
        apply=bool(getattr(args, "apply", False)),
        limit=getattr(args, "limit", None),
        max_refs_per_page=getattr(args, "max_refs_per_page", None),
        frontmatter_ref_limit=getattr(args, "frontmatter_ref_limit", None),
        unresolved_sample_limit=getattr(args, "unresolved_sample_limit", None),
        change_sample_limit=getattr(args, "change_sample_limit", None),
        include_relation_evidence=not bool(getattr(args, "skip_relation_evidence", False)),
        relation_evidence_types=getattr(args, "relation_evidence_types", None),
        write_frontmatter=not bool(getattr(args, "no_frontmatter", False)),
        write_report=not bool(getattr(args, "no_report", False)),
        report_dir=getattr(args, "report_dir", None),
    )
    if getattr(args, "json", False):
        print(dumps_report(report))
    else:
        print(format_evidence_backfill_text(report))


def _cmd_distill_actions(args):
    """Inspect distill action router logs."""
    import json

    from core.hephaestus.distill_action_router import (
        DistillActionRouter,
        DistillActionRouterOptions,
    )
    from core.hephaestus.distill_cognitive_action_worker import (
        DistillCognitiveActionWorker,
    )

    cfg = _get_config()
    router = DistillActionRouter(DistillActionRouterOptions.from_config(cfg), ensure_db=False)
    if getattr(args, "process_queued", False):
        worker = DistillCognitiveActionWorker(router.db_path, database_dir=router.database_dir)
        process_limit = getattr(args, "process_limit", None)
        if process_limit is None:
            process_limit = getattr(args, "limit", 20)
        result = worker.process_queued(limit=int(process_limit))
        result["cognitive_action_counts"] = router.cognitive_action_counts()
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return
        print(
            "Processed cognitive actions: "
            f"{result['processed']} applied={result['applied']} "
            f"retry={result['retry']} dead={result['dead']}"
        )
        return
    action_id = getattr(args, "action_id", None)
    session_id = getattr(args, "session_id", None)
    if action_id:
        action = router.get_action(action_id)
        result = {
            "action": action,
            "knowledge_actions": router.list_knowledge_actions(action_id) if action else [],
            "cognitive_actions": router.list_cognitive_actions(action_id) if action else [],
        }
    elif session_id:
        result = {
            "actions": router.list_actions_for_session(session_id),
            "cognitive_action_counts": router.cognitive_action_counts(),
        }
    else:
        result = {
            "actions": router.list_recent_actions(getattr(args, "limit", 20)),
            "cognitive_action_counts": router.cognitive_action_counts(),
        }

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    actions = [result["action"]] if action_id and result.get("action") else result.get("actions", [])
    print("Mnemos distill actions")
    if not actions:
        print("No distill actions found.")
        return
    for row in actions:
        print(
            f"- {row.get('action_id')} {row.get('action')} "
            f"{row.get('result_status')} -> {row.get('target_page')}"
        )
    if action_id:
        for row in result.get("knowledge_actions", []):
            print(f"  * {row.get('change_type')} {row.get('target_page')}")
        for row in result.get("cognitive_actions", []):
            print(f"  * cognitive:{row.get('cognitive_action')} {row.get('target_kind')}")


def _failed_task_action_args(args) -> tuple[str | None, int | None, str, bool, bool]:
    identifier = getattr(args, "task_id", None) or None
    limit = getattr(args, "limit", None)
    reason = getattr(args, "reason", "") or ""
    all_tasks = bool(getattr(args, "all", False))
    as_json = bool(getattr(args, "json", False))
    return identifier, limit, reason, all_tasks, as_json


def _print_distill_action_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if result.get("ok"):
        print(f"{result['message']}: {result['changed']}")
    else:
        print(result["message"])


def _cmd_distill_reset_timeouts(args) -> int:
    """Return stale processing Amphora tasks to pending."""
    from core.kia import amphora
    from core.kia.amphora_types import TIMEOUT_MINUTES

    as_json = bool(getattr(args, "json", False))
    minutes = int(
        getattr(args, "minutes", TIMEOUT_MINUTES) or TIMEOUT_MINUTES
    )
    if minutes <= 0:
        result = {
            "ok": False,
            "error": "invalid_timeout_minutes",
            "message": "--minutes 必须大于 0",
        }
        _print_distill_action_result(result, as_json)
        return 1

    changed = amphora.reset_timeouts(timeout_minutes=minutes)
    result = {
        "ok": True,
        "action": "reset_timeouts",
        "changed": changed,
        "timeout_minutes": minutes,
        "message": "已重置超时 processing 蒸馏任务",
    }
    _print_distill_action_result(result, as_json)
    return 0


def _cmd_distill_retry_failed(args) -> int:
    """Retry failed Amphora tasks."""
    from core.kia import amphora

    identifier, limit, reason, all_tasks, as_json = _failed_task_action_args(args)
    if not identifier and not all_tasks:
        result = {
            "ok": False,
            "error": "task_id_or_all_required",
            "message": "需要指定 --task-id 或显式 --all",
        }
        _print_distill_action_result(result, as_json)
        return 1

    try:
        changed = amphora.retry_failed(
            identifier,
            limit=limit,
            reason=reason or "cli retry",
        )
    except RuntimeError as exc:
        result = {
            "ok": False,
            "action": "retry_failed",
            "error": str(exc),
            "message": (
                "已终结任务不能原地重开；请由上游用新的 input revision "
                "创建新的 generation"
            ),
        }
        _print_distill_action_result(result, as_json)
        return 1
    result = {
        "ok": True,
        "action": "retry_failed",
        "changed": changed,
        "message": "已重试 failed 任务",
    }
    _print_distill_action_result(result, as_json)
    return 0


def _cmd_distill_archive_failed(args) -> int:
    """Archive failed Amphora tasks with an explicit reason."""
    from core.kia import amphora

    identifier, limit, reason, all_tasks, as_json = _failed_task_action_args(args)
    if not identifier and not all_tasks:
        result = {
            "ok": False,
            "error": "task_id_or_all_required",
            "message": "需要指定 --task-id 或显式 --all",
        }
        _print_distill_action_result(result, as_json)
        return 1

    try:
        changed = amphora.archive_failed(
            identifier,
            limit=limit,
            reason=reason or "cli archive",
            config=_get_config(),
        )
    except RuntimeError as exc:
        result = {
            "ok": False,
            "action": "archive_failed",
            "error": str(exc),
            "message": "failed-terminal receipt 闭合前禁止归档任务",
        }
        _print_distill_action_result(result, as_json)
        return 1
    result = {
        "ok": True,
        "action": "archive_failed",
        "changed": changed,
        "message": "已归档 failed 任务",
    }
    _print_distill_action_result(result, as_json)
    return 0
