"""Command-line adapter for the registered model-call-ledger reconciler."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from core.config import Config


_PUBLIC_PATH_FIELDS = frozenset(
    {
        "path",
        "canonical_path",
        "source",
        "backup_ref",
        "rollback_ref",
        "recovery_manifest",
        "sealed_recovery_manifest",
        "backup_root",
    }
)
_CALLER_INPUT_FIELDS = frozenset({"expected_plan_hash", "reviewed_plan_hash"})


def _public_runtime_path(config: Any, value: Any) -> str:
    """Render a runtime path without returning a workstation-specific prefix."""
    raw = str(value or "")
    if not raw or raw.startswith("<MNEMOS_DIR>/") or raw.startswith("<DATABASE_DIR>/"):
        return raw
    try:
        candidate = Path(raw).expanduser().absolute()
    except (OSError, RuntimeError, ValueError):
        return "protected_runtime_path"
    roots = (
        ("<MNEMOS_DIR>", Path(getattr(config, "mnemos_dir", None) or config.data_dir)),
        ("<DATABASE_DIR>", Path(config.database_dir)),
    )
    for label, root in roots:
        try:
            relative = candidate.relative_to(root.expanduser().absolute())
        except (OSError, RuntimeError, ValueError):
            continue
        return f"{label}/{relative.as_posix()}" if relative.parts else label
    return "protected_runtime_path"


def _jsonable(value: Any, *, config: Any, field_name: str = "") -> Any:
    if isinstance(value, Path):
        return _public_runtime_path(config, value)
    if isinstance(value, dict):
        rendered: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name in _CALLER_INPUT_FIELDS:
                rendered[f"{name}_present"] = bool(child)
                continue
            rendered[name] = _jsonable(child, config=config, field_name=name)
        return rendered
    if isinstance(value, list):
        return [_jsonable(child, config=config, field_name=field_name) for child in value]
    if field_name in _PUBLIC_PATH_FIELDS:
        return _public_runtime_path(config, value)
    return value


def render_public_reconciliation_result(result: dict[str, Any], *, config: Any) -> dict[str, Any]:
    """Project diagnostic output without exposing local runtime path prefixes."""
    rendered = _jsonable(result, config=config)
    if not isinstance(rendered, dict):
        raise TypeError("reconciliation_public_result_invalid")
    return rendered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect retired prompt-call storage or request registered reconciliation."
    )
    parser.add_argument("--apply", action="store_true", help="back up, import, and remove retired tables")
    parser.add_argument("--backup-dir", type=Path, help="required by --apply")
    parser.add_argument(
        "--expected-plan-hash",
        help="exact plan_fingerprint emitted by the reviewed dry-run; required by --apply",
    )
    parser.add_argument(
        "--discard-unattributable-legacy",
        action="store_true",
        help="explicitly discard legacy rows whose original data subject was not retained",
    )
    parser.add_argument(
        "--discard-unrecoverable-run-tombstone-history",
        action="store_true",
        help=(
            "explicitly acknowledge unrecoverable budget history from the retired "
            "cascading run-tombstone schema; this remains release-ineligible"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    config_factory: Callable[..., Any] = Config,
) -> int:
    """Run the standalone diagnostic adapter without granting apply authority."""
    from . import reconcile_model_call_ledger

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and args.backup_dir is None:
        parser.error("--apply requires --backup-dir")
    if args.apply and not args.expected_plan_hash:
        parser.error("--apply requires --expected-plan-hash from a reviewed dry-run")
    if args.discard_unattributable_legacy and not args.apply:
        parser.error("--discard-unattributable-legacy requires --apply")
    if args.discard_unrecoverable_run_tombstone_history and not args.apply:
        parser.error("--discard-unrecoverable-run-tombstone-history requires --apply")
    # Diagnostics remain read-only and do not materialize ~/.mnemos merely to
    # inspect storage.  Direct script apply still lacks the registry-issued
    # capability and is blocked by the core executor.
    config = config_factory(strict=False, provision=False)
    result = reconcile_model_call_ledger(
        config,
        apply=bool(args.apply),
        backup_dir=args.backup_dir,
        expected_plan_hash=args.expected_plan_hash,
        discard_unattributable_legacy=bool(args.discard_unattributable_legacy),
        discard_unrecoverable_run_tombstone_history=bool(
            args.discard_unrecoverable_run_tombstone_history
        ),
    )
    rendered = json.dumps(
        render_public_reconciliation_result(result, config=config),
        ensure_ascii=False,
        sort_keys=True,
    )
    print(rendered if args.json else json.dumps(json.loads(rendered), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1
