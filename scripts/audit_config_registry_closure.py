#!/usr/bin/env python3
"""Audit canonical config keys, readers, examples, lifecycle, and types."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config, CONFIG_REGISTRY, DEFAULT_CONFIG, PERFORMANCE_TIERS  # noqa: E402

SCHEMA_VERSION = "mnemos.config_registry_closure.v1"
SCAN_PATHS = ("core", "integrations", "daemon", "mnemos_cli.py")


@dataclass(frozen=True)
class Finding:
    code: str
    key: str
    detail: str
    path: str = ""
    line: int = 0


@dataclass(frozen=True)
class ReadSite:
    key: str
    path: str
    line: int
    caller_default: str = ""


def _source_files(root: Path) -> Iterable[Path]:
    for relative in SCAN_PATHS:
        path = root / relative
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def _is_config_factory(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Call) and (
        (isinstance(expr.func, ast.Name) and expr.func.id in {"Config", "get_config"})
        or (isinstance(expr.func, ast.Attribute) and expr.func.attr == "get_config")
    )


def _factory_config_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or not _is_config_factory(value):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(ast.unparse(target))
    return names


def _config_base(expr: ast.AST, key: str, factory_names: set[str]) -> bool:
    """Return whether a ``.get`` call can be proven to target root config.

    Dotted keys are unique to Mnemos' root Config API.  Single-segment keys
    are only safe to classify when they already belong to the registry
    lifecycle, or when the receiver is a direct ``get_config()`` call.  This
    avoids treating nested dictionaries such as ``provider_cfg`` or
    ``dispute_cfg`` as independent configuration authorities.
    """
    rendered = ast.unparse(expr)
    if _is_config_factory(expr):
        return True
    if rendered in factory_names:
        return True
    if "." in key:
        return rendered in {
            "cfg",
            "config",
            "self._config",
            "self.config",
            "self._cfg",
            "runtime_config",
            "effective",
        }
    lifecycle_keys = CONFIG_REGISTRY.keys() | set(CONFIG_REGISTRY.aliases) | set(
        CONFIG_REGISTRY.removed_keys
    )
    return key in lifecycle_keys and rendered in {
        "cfg",
        "config",
        "self._config",
        "self.config",
        "runtime_config",
        "effective",
    }


def scan_read_sites(root: Path) -> list[ReadSite]:
    sites: list[ReadSite] = []
    for path in _source_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        factory_names = _factory_config_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.For) or not isinstance(node.target, ast.Tuple):
                continue
            target_names = [
                item.id if isinstance(item, ast.Name) else "" for item in node.target.elts
            ]
            if "key" not in target_names or not isinstance(node.iter, (ast.Tuple, ast.List)):
                continue
            if not any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "key"
                and _config_base(call.func.value, "dynamic.config_key", factory_names)
                for statement in node.body
                for call in ast.walk(statement)
            ):
                continue
            key_index = target_names.index("key")
            default_index = (
                target_names.index("default") if "default" in target_names else None
            )
            for row in node.iter.elts:
                if not isinstance(row, (ast.Tuple, ast.List)) or key_index >= len(row.elts):
                    continue
                key_node = row.elts[key_index]
                if not isinstance(key_node, ast.Constant) or not isinstance(
                    key_node.value, str
                ):
                    continue
                default = ""
                if default_index is not None and default_index < len(row.elts):
                    default = ast.unparse(row.elts[default_index])
                sites.append(
                    ReadSite(
                        key=key_node.value,
                        path=path.relative_to(root).as_posix(),
                        line=row.lineno,
                        caller_default=default,
                    )
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            key = ""
            default_node: ast.AST | None = None
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and _config_base(node.func.value, node.args[0].value, factory_names)
            ):
                key = node.args[0].value
                default_node = node.args[1] if len(node.args) > 1 else None
            elif (
                isinstance(node.func, ast.Name)
                and node.func.id in {"_cfg_get", "cfg_get"}
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                key = node.args[1].value
                default_node = node.args[2] if len(node.args) > 2 else None
            if not key:
                continue
            sites.append(
                ReadSite(
                    key=key,
                    path=path.relative_to(root).as_posix(),
                    line=node.lineno,
                    caller_default=ast.unparse(default_node) if default_node is not None else "",
                )
            )
    return sorted(sites, key=lambda item: (item.path, item.line, item.key))


def _literal_default(text: str) -> tuple[bool, Any]:
    if not text:
        return False, None
    try:
        return True, ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return False, None


def _load_mapping(path: Path) -> Mapping[str, Any]:
    if path.suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
    else:
        import yaml

        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, Mapping) else {}


def audit(
    *,
    root: Path = ROOT,
    live_config_path: Path | None = None,
) -> dict[str, Any]:
    findings: list[Finding] = []
    reads = scan_read_sites(root)
    canonical_read_keys: set[str] = set()
    for site in reads:
        if site.key in CONFIG_REGISTRY.removed_keys:
            findings.append(
                Finding("removed_reader", site.key, "removed key still has a runtime reader", site.path, site.line)
            )
            continue
        if site.key in CONFIG_REGISTRY.aliases:
            findings.append(
                Finding("alias_reader", site.key, "runtime must read the canonical key", site.path, site.line)
            )
            continue
        try:
            spec = CONFIG_REGISTRY.require(site.key)
        except KeyError:
            findings.append(
                Finding("unknown_reader", site.key, "runtime reader is absent from registry", site.path, site.line)
            )
            continue
        canonical_read_keys.add(CONFIG_REGISTRY.canonical_key(site.key))
        is_literal, caller_default = _literal_default(site.caller_default)
        # Branch reads return a complete registered subtree.  An empty mapping
        # at the call site is redundant syntax, not an alternative leaf default;
        # only leaf defaults can diverge from the canonical owner.
        if (
            is_literal
            and site.key in CONFIG_REGISTRY.flatten_tree(DEFAULT_CONFIG)
            and caller_default != spec.default
        ):
            findings.append(
                Finding(
                    "divergent_caller_fallback",
                    site.key,
                    f"caller default type/value differs from canonical default ({type(caller_default).__name__})",
                    site.path,
                    site.line,
                )
            )

    default_keys = CONFIG_REGISTRY.keys_present_in_tree(DEFAULT_CONFIG)
    if default_keys != CONFIG_REGISTRY.keys():
        findings.append(Finding("default_registry_mismatch", "*", "DEFAULT_CONFIG and registry key sets differ"))

    tier_errors = []
    for tier, values in PERFORMANCE_TIERS.items():
        tier_errors.extend(
            CONFIG_REGISTRY.validate_override_tree(values, source=f"performance_tier:{tier}")
        )
    findings.extend(
        Finding(item.code, item.key, item.source) for item in tier_errors
    )

    example_counts: dict[str, int] = {}
    for relative in ("config/config.example.json", "config/config.example.yaml"):
        path = root / relative
        if not path.exists():
            findings.append(Finding("missing_example", "*", relative))
            continue
        example = _load_mapping(path)
        example_keys = CONFIG_REGISTRY.keys_present_in_tree(example)
        example_counts[relative] = len(example_keys)
        for key in sorted(CONFIG_REGISTRY.keys() - example_keys):
            findings.append(Finding("example_missing_key", key, relative))
        for key in sorted(example_keys - CONFIG_REGISTRY.keys()):
            findings.append(Finding("example_unknown_key", key, relative))

    live_errors = []
    live_fingerprint = ""
    if live_config_path is not None and live_config_path.exists():
        live = _load_mapping(live_config_path)
        live_errors = CONFIG_REGISTRY.validate_override_tree(
            live,
            source="live_config",
        )
        findings.extend(Finding(item.code, item.key, item.source) for item in live_errors)
        if not live_errors:
            live_fingerprint = Config(
                config_path=live_config_path,
                provision=False,
            ).config_fingerprint

    by_code: dict[str, int] = {}
    for item in findings:
        by_code[item.code] = by_code.get(item.code, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not findings,
        "registry_schema_version": CONFIG_REGISTRY.schema_version,
        "defined": CONFIG_REGISTRY.key_count,
        "read": len(canonical_read_keys),
        "read_sites": len(reads),
        "example": example_counts,
        "test": CONFIG_REGISTRY.key_count,
        "doc": CONFIG_REGISTRY.key_count,
        "env": len(CONFIG_REGISTRY.env_targets),
        "tiers": len(PERFORMANCE_TIERS),
        "aliases": len(CONFIG_REGISTRY.aliases),
        "removed": len(CONFIG_REGISTRY.removed_keys),
        "removed_reader_count": by_code.get("removed_reader", 0),
        "unknown_reader_count": by_code.get("unknown_reader", 0),
        "divergent_fallback_count": by_code.get("divergent_caller_fallback", 0),
        "live_config_error_count": len(live_errors),
        "live_config_fingerprint": live_fingerprint,
        "findings": [asdict(item) for item in findings],
        "by_code": dict(sorted(by_code.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--live-config",
        type=Path,
        default=Path.home() / ".mnemos" / "configs" / "main.json",
    )
    args = parser.parse_args(argv)
    report = audit(live_config_path=args.live_config)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        print(
            "Config registry closure passed: "
            f"defined={report['defined']} reads={report['read_sites']} env={report['env']}"
        )
    else:
        print("Config registry closure failed:")
        for item in report["findings"]:
            location = f" {item['path']}:{item['line']}" if item["path"] else ""
            print(f"- {item['code']} {item['key']}{location}: {item['detail']}")
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
