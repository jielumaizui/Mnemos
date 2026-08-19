"""Migration CLI commands."""

from __future__ import annotations

import json
from typing import Any


def add_model_call_ledger_migration_args(apply_parser: Any, rollback_parser: Any) -> None:
    """Attach the ledger migration's explicit review and recovery arguments."""
    apply_parser.add_argument(
        "--expected-plan-hash",
        help="已审阅 dry-run 中 database.model_call_ledger.v1 的 execution_plan_hash",
    )
    apply_parser.add_argument(
        "--execute-wrapped",
        action="store_true",
        help="允许执行已包装的迁移脚本",
    )
    apply_parser.add_argument(
        "--discard-unattributable-legacy",
        action="store_true",
        help="明确丢弃未保留原始主体归属的历史模型调用记录",
    )
    apply_parser.add_argument(
        "--discard-unrecoverable-run-tombstone-history",
        action="store_true",
        help="明确确认已退役级联 run tombstone 模式中不可恢复的预算历史（仍不可发布）",
    )
    rollback_parser.add_argument(
        "--recovery-manifest",
        help="database.model_call_ledger.v1 的 sealed v3 recovery manifest",
    )
    rollback_parser.add_argument(
        "--apply",
        action="store_true",
        help="实际执行已预览的回滚；省略时只读预览",
    )
    rollback_parser.add_argument(
        "--execute-wrapped",
        action="store_true",
        help="确认执行包装的 database.model_call_ledger.v1 恢复流程",
    )


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_migrate(args) -> int:
    from core.config import Config
    from core.migrations.registry import (
        MODEL_CALL_LEDGER_MIGRATION_ID,
        MigrationRegistry,
        public_migration_record,
    )

    # Migration must be able to inspect and repair a pre-registry config that
    # the normal strict runtime correctly refuses to load.
    cmd = getattr(args, "migrate_cmd", "") or "status"
    # ``status``, ``plan`` and ``verify`` are diagnostics.  They must not
    # create MNEMOS_DIR, rewrite an older config, or initialise migrations.db
    # merely because an operator asked what would happen.  The model-call
    # ledger migration has an additional zero-write contract: clean/no-receipt
    # outcomes must return before Config provisioning as well.
    migration_id = getattr(args, "migration_id", "") or ""
    is_model_call_ledger_mutation = (
        cmd in {"apply", "rollback"} and migration_id == MODEL_CALL_LEDGER_MIGRATION_ID
    )
    cfg = Config(
        strict=False,
        provision=cmd in {"apply", "rollback"} and not is_model_call_ledger_mutation,
    )
    registry = MigrationRegistry()
    json_output = bool(getattr(args, "json", False))

    if cmd == "status":
        payload = registry.status(cfg, read_only=True)
    elif cmd == "plan":
        payload = registry.plan(cfg).as_dict()
    elif cmd == "apply":
        if not migration_id:
            payload = {"status": "failed", "error": "migration_id is required"}
        else:
            payload = public_migration_record(
                registry.apply(
                    cfg,
                    migration_id,
                    execute_wrapped=bool(getattr(args, "execute_wrapped", False)),
                    expected_plan_hash=getattr(args, "expected_plan_hash", None),
                    discard_unattributable_legacy=bool(
                        getattr(args, "discard_unattributable_legacy", False)
                    ),
                    discard_unrecoverable_run_tombstone_history=bool(
                        getattr(args, "discard_unrecoverable_run_tombstone_history", False)
                    ),
                ),
                config=cfg,
            )
    elif cmd == "rollback":
        if not migration_id:
            payload = {"status": "failed", "error": "migration_id is required"}
        else:
            payload = public_migration_record(
                registry.rollback(
                    cfg,
                    migration_id,
                    recovery_manifest=getattr(args, "recovery_manifest", None),
                    apply=bool(getattr(args, "apply", False)),
                    execute_wrapped=bool(getattr(args, "execute_wrapped", False)),
                ),
                config=cfg,
            )
    else:
        payload = registry.verify(cfg)

    _emit(payload, json_output=json_output)
    return 0 if payload.get("status") not in {"failed", "blocked"} else 1
