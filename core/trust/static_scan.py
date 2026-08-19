"""AST checks for trusted-push and formal-vault write bypasses."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Sequence, TypedDict

from core.ops.durable_io import read_native_bytes


REQUIRED_GUARDS = {
    "core/hephaestus/distillation_engine.py": (
        "submit_distillation_page_candidate",
        "trusted_push",
    ),
    "core/hephaestus/document_pipeline.py": (
        "submit_document_page_candidate",
        "trusted_push",
    ),
    "core/hephaestus/trusted_push_bridge.py": (
        "load_trusted_push_config",
        "ProposalQueue",
        "submit_candidate",
    ),
    "core/trust/proposal_queue.py": (
        "PushDecisionGate",
        "evaluate",
    ),
    "core/trust/vault_mutation_service.py": (
        "load_trusted_push_config",
        "ProposalQueue",
        "submit_candidate",
    ),
}

REGISTRY_REL_PATH = "core/trust/static_sink_registry.json"
INLINE_CLASS_RE = re.compile(
    r"trusted-scan:\s*(?P<category>[a-z_]+)\s+"
    r"owner=(?P<owner>[A-Za-z0-9_.-]+)\s+"
    r"target=(?P<target>[A-Za-z0-9_.-]+)\s+"
    r"expires=(?P<expires>[A-Za-z0-9_.-]+)"
    r"(?:\s+(?P<reason>.+))?"
)

METHOD_SINKS = {
    "touch",
    "write_text",
    "write_bytes",
    "rename",
    "unlink",
    "rmdir",
}
QUALIFIED_SINKS = {
    "atomic_write_text",
    "commit_trusted_markdown",
    "commit_trusted_markdown_delete",
    "commit_trusted_markdown_move",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.unlink",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
}
CENTRAL_WRITER_PATHS = {
    "core/trust/markdown_adapter.py": "Journal-approved MarkdownAdapter",
    "core/trust/vault_mutation_service.py": "TrustedMutationReceipt",
}
DERIVED_PROJECTION_SINK_GUARDS = {
    (
        "core/wiki_derived_projection.py",
        "DerivedProjectionLifecycle._atomic_publish",
        "atomic_write_text",
    ): "assert_upsert",
    (
        "core/wiki_derived_projection.py",
        "DerivedProjectionLifecycle._atomic_delete",
        "unlink",
    ): "assert_delete",
}
PRIMITIVE_WRITER_PATHS = {
    "core/file_ops.py",
    "core/ops/durable_io.py",
}
SECURE_HELPER_SINKS = {
    "secure_atomic_write_bytes",
    "secure_atomic_write_text",
    "secure_publish_immutable_bytes",
    "secure_publish_immutable_text",
    "secure_remove_directory_tree",
    "secure_remove_regular_file",
}

PASS_CATEGORIES = {
    "artifact",
    "backup",
    "cache",
    "config",
    "diagnostic",
    "guarded_trusted_push",
    "guarded_projection_lifecycle",
    "manual_repair",
    "report",
    "shadow",
    "system_state",
    "test_fixture",
    "trusted_writer",
    "write_primitive",
}
FAIL_CATEGORIES = {"formal_knowledge"}
DEBT_CATEGORIES = {"known_bypass"}
REGISTRY_CATEGORIES = PASS_CATEGORIES - {
    "guarded_trusted_push",
    "guarded_projection_lifecycle",
    "trusted_writer",
    "write_primitive",
}


@dataclass(frozen=True)
class DirectWriteSite:
    sink_id: str
    rel_path: str
    line_no: int
    line: str
    call_kind: str
    function: str
    category: str
    reason: str
    target_class: str
    guard_dominates: bool
    receipt_type: str
    waiver_expiry: str


class DirectWriteReport(TypedDict):
    schema_version: str
    site_count: int
    counts: Dict[str, int]
    sites: List[DirectWriteSite]
    registry_count: int
    registry_stale_count: int
    unknown_count: int


@dataclass(frozen=True)
class _Classification:
    category: str
    reason: str
    target_class: str = "unknown"
    guard_dominates: bool = False
    receipt_type: str = ""
    waiver_expiry: str = ""


@dataclass(frozen=True)
class _SinkCall:
    node: ast.Call
    kind: str
    function: str
    ordinal: int


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.calls: list[tuple[ast.Call, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _sink_kind(node):
            self.calls.append((node, ".".join(self.function_stack) or "<module>"))
        self.generic_visit(node)


def scan_trusted_push_guards(repo_root: Path | None = None) -> List[str]:
    root = Path(repo_root or Path(__file__).resolve().parents[2])
    issues = _scan_required_guard_markers(root)
    report = scan_direct_writes(root)
    if report["registry_stale_count"]:
        issues.append(
            f"trusted sink registry has {report['registry_stale_count']} stale callsite(s)"
        )
    for site in report["sites"]:
        if site.category == "unclassified":
            detail = site.reason
            if "guard" in detail:
                detail += f" guard_dominates={str(site.guard_dominates).lower()}"
            issues.append(
                f"{site.rel_path}:{site.line_no} unclassified direct write "
                f"{site.call_kind}: {detail}: {site.line}"
            )
        elif site.category in FAIL_CATEGORIES:
            issues.append(
                f"{site.rel_path}:{site.line_no} forbidden formal direct write: {site.line}"
            )
        elif site.category not in PASS_CATEGORIES:
            issues.append(
                f"{site.rel_path}:{site.line_no} unsupported direct write category "
                f"{site.category}: {site.line}"
            )
    return issues


def scan_direct_writes(repo_root: Path | None = None) -> DirectWriteReport:
    root = Path(repo_root or Path(__file__).resolve().parents[2])
    registry = _load_registry(root)
    sites = list(_iter_direct_write_sites(root, registry))
    current_sink_ids = {site.sink_id for site in sites}
    stale_registry_ids = set(registry) - current_sink_ids
    counts: Dict[str, int] = {}
    for site in sites:
        counts[site.category] = counts.get(site.category, 0) + 1
    return {
        "schema_version": "mnemos.trusted_push_static_scan.v4",
        "site_count": len(sites),
        "counts": counts,
        "sites": sites,
        "registry_count": len(registry),
        "registry_stale_count": len(stale_registry_ids),
        "unknown_count": counts.get("unclassified", 0),
    }


def _scan_required_guard_markers(root: Path) -> List[str]:
    issues: List[str] = []
    for rel_path, markers in REQUIRED_GUARDS.items():
        path = root / rel_path
        if not path.exists():
            issues.append(f"missing guarded file: {rel_path}")
            continue
        text = _read_source(path)
        for marker in markers:
            if marker not in text:
                issues.append(f"{rel_path} missing trusted push marker: {marker}")
    return issues


def _iter_direct_write_sites(
    root: Path,
    registry: Mapping[str, Mapping[str, str]],
) -> Iterable[DirectWriteSite]:
    for path in sorted((root / "core").rglob("*.py")):
        rel_path = path.relative_to(root).as_posix()
        text = _read_source(path)
        lines = text.splitlines()
        try:
            tree = ast.parse(text, filename=rel_path)
        except SyntaxError as exc:
            line_no = int(exc.lineno or 1)
            yield DirectWriteSite(
                sink_id=f"{rel_path}:syntax-error",
                rel_path=rel_path,
                line_no=line_no,
                line=lines[line_no - 1].strip() if 0 < line_no <= len(lines) else "",
                call_kind="syntax_error",
                function="<module>",
                category="unclassified",
                reason="source cannot be parsed for sink analysis",
                target_class="unknown",
                guard_dominates=False,
                receipt_type="",
                waiver_expiry="",
            )
            continue
        for sink in _collect_sink_calls(tree):
            node = sink.node
            line_no = int(node.lineno)
            sink_id = _sink_id(rel_path, sink)
            classification = _classify_callsite(
                rel_path=rel_path,
                lines=lines,
                tree=tree,
                sink=sink,
                sink_id=sink_id,
                registry=registry,
            )
            yield DirectWriteSite(
                sink_id=sink_id,
                rel_path=rel_path,
                line_no=line_no,
                line=lines[line_no - 1].strip(),
                call_kind=sink.kind,
                function=sink.function,
                category=classification.category,
                reason=classification.reason,
                target_class=classification.target_class,
                guard_dominates=classification.guard_dominates,
                receipt_type=classification.receipt_type,
                waiver_expiry=classification.waiver_expiry,
            )


def _collect_sink_calls(tree: ast.AST) -> Iterator[_SinkCall]:
    collector = _CallCollector()
    collector.visit(tree)
    ordinals: dict[tuple[str, str], int] = {}
    for node, function in collector.calls:
        kind = _sink_kind(node)
        if not kind:
            continue
        key = (function, _call_digest(node))
        ordinal = ordinals.get(key, 0) + 1
        ordinals[key] = ordinal
        yield _SinkCall(node=node, kind=kind, function=function, ordinal=ordinal)


def _sink_kind(node: ast.Call) -> str:
    name = _qualified_name(node.func)
    leaf = name.rsplit(".", 1)[-1]
    if leaf in METHOD_SINKS:
        return leaf
    if leaf in SECURE_HELPER_SINKS:
        return leaf
    if leaf == "replace" and isinstance(node.func, ast.Attribute):
        if len(node.args) == 1 and _looks_path_like(node.func.value):
            return "path.replace"
        return ""
    if leaf.startswith("commit_trusted_markdown"):
        return "trusted_commit"
    if name in QUALIFIED_SINKS or leaf == "atomic_write_text":
        return name
    if leaf == "open" and _is_write_mode(node):
        return "open_write"
    return ""


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _qualified_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _looks_path_like(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and _qualified_name(node.func).endswith("Path"):
        return True
    name = _qualified_name(node).lower()
    return any(token in name for token in ("path", "file", "target", "source", "temp"))


def _is_write_mode(node: ast.Call) -> bool:
    mode_node: ast.AST | None = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    elif isinstance(node.func, ast.Attribute) and node.args:
        mode_node = node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
    if not isinstance(mode_node, ast.Constant) or not isinstance(mode_node.value, str):
        return False
    mode = mode_node.value
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _call_digest(node: ast.Call) -> str:
    payload = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _sink_id(rel_path: str, sink: _SinkCall) -> str:
    return f"{rel_path}::{sink.function}::{sink.kind}::{_call_digest(sink.node)}::{sink.ordinal}"


def _classify_callsite(
    *,
    rel_path: str,
    lines: Sequence[str],
    tree: ast.AST,
    sink: _SinkCall,
    sink_id: str,
    registry: Mapping[str, Mapping[str, str]],
) -> _Classification:
    inline = _inline_classification(lines, int(sink.node.lineno))
    if inline is not None:
        return inline
    projection_validator = DERIVED_PROJECTION_SINK_GUARDS.get(
        (rel_path, sink.function, sink.kind)
    )
    if projection_validator is not None:
        dominated, receipt_type = _projection_guard_dominance(
            tree,
            sink.node,
            validator=projection_validator,
        )
        if dominated:
            return _Classification(
                category="guarded_projection_lifecycle",
                reason="exact typed projection mutation authorization dominates this sink",
                target_class="derived_markdown_projection",
                guard_dominates=True,
                receipt_type=receipt_type,
                waiver_expiry="never",
            )
        return _Classification(
            category="unclassified",
            reason="projection sink lacks an exact dominating typed authorization validator",
            target_class="derived_markdown_projection",
        )
    if rel_path in CENTRAL_WRITER_PATHS:
        return _Classification(
            category="trusted_writer",
            reason="central typed-receipt Markdown writer",
            target_class="formal_markdown",
            guard_dominates=True,
            receipt_type=CENTRAL_WRITER_PATHS[rel_path],
            waiver_expiry="never",
        )
    if rel_path in PRIMITIVE_WRITER_PATHS:
        return _Classification(
            category="write_primitive",
            reason="low-level atomic write primitive; callers are scanned independently",
            target_class="filesystem_primitive",
            guard_dominates=False,
            waiver_expiry="never",
        )
    if sink.kind == "trusted_commit":
        receipt_type = _trusted_commit_receipt_type(tree, sink.node)
        if receipt_type:
            return _Classification(
                category="guarded_trusted_push",
                reason="typed receipt is passed to the only fallback commit helper",
                target_class="formal_markdown",
                guard_dominates=True,
                receipt_type=receipt_type,
                waiver_expiry="never",
            )
        return _Classification(
            category="unclassified",
            reason="trusted commit helper lacks a local typed submission receipt",
            target_class="formal_markdown",
        )
    dominated, receipt_type = _trusted_guard_dominance(tree, sink.node)
    if dominated:
        return _Classification(
            category="guarded_trusted_push",
            reason="typed submission receipt dominates this sink",
            target_class="formal_markdown",
            guard_dominates=True,
            receipt_type=receipt_type,
            waiver_expiry="never",
        )
    entry = registry.get(sink_id)
    if entry is not None:
        return _classification_from_registry(entry)
    if _function_mentions_trusted_submission(tree, sink.node):
        return _Classification(
            category="unclassified",
            reason="trusted submission marker exists but guard does not dominate sink",
            target_class="formal_markdown",
            guard_dominates=False,
        )
    return _Classification(category="unclassified", reason="no exact callsite classification")


def _inline_classification(lines: Sequence[str], line_no: int) -> _Classification | None:
    candidates = []
    if 0 < line_no <= len(lines):
        candidates.append(lines[line_no - 1])
    if line_no >= 2:
        candidates.append(lines[line_no - 2])
    for raw in candidates:
        if "trusted-scan:" not in raw:
            continue
        match = INLINE_CLASS_RE.search(raw)
        if not match:
            return _Classification(
                category="unclassified",
                reason="invalid callsite classification; require category owner target expires",
            )
        category = match.group("category")
        if category not in PASS_CATEGORIES | FAIL_CATEGORIES | DEBT_CATEGORIES:
            return _Classification(
                category="unclassified",
                reason=f"invalid callsite classification category: {category}",
            )
        expiry = match.group("expires")
        if not _valid_expiry(expiry, category):
            return _Classification(
                category="unclassified",
                reason=f"invalid callsite classification expiry: {expiry}",
            )
        return _Classification(
            category=category,
            reason=(match.group("reason") or "inline exact callsite classification").strip(),
            target_class=match.group("target"),
            guard_dominates=False,
            receipt_type="",
            waiver_expiry=expiry,
        )
    return None


def _trusted_guard_dominance(tree: ast.AST, sink: ast.Call) -> tuple[bool, str]:
    function = _enclosing_function(tree, sink)
    if function is None:
        return False, ""
    for statements, sink_index, containing_statement in _statement_blocks(function, sink):
        receipt_vars = _direct_receipt_assignments(statements[:sink_index])
        for statement in statements[:sink_index]:
            if not isinstance(statement, ast.If):
                continue
            receipt_name = _intercepted_receipt_name(statement.test)
            if receipt_name in receipt_vars and _block_terminates(statement.body):
                return True, receipt_vars[receipt_name]
        if isinstance(containing_statement, ast.If):
            receipt_name = _not_intercepted_receipt_name(containing_statement.test)
            if receipt_name in receipt_vars and any(
                _node_contains(statement, sink) for statement in containing_statement.body
            ):
                return True, receipt_vars[receipt_name]
    return False, ""


def _projection_guard_dominance(
    tree: ast.AST,
    sink: ast.Call,
    *,
    validator: str,
) -> tuple[bool, str]:
    """Prove an exact typed path/hash validator dominates one projection sink."""

    function = _enclosing_function(tree, sink)
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False, ""
    authorization_names = {
        argument.arg
        for argument in [
            *function.args.posonlyargs,
            *function.args.args,
            *function.args.kwonlyargs,
        ]
        if argument.annotation is not None
        and _qualified_name(argument.annotation).endswith(
            "DerivedProjectionMutationAuthorization"
        )
    }
    if not authorization_names:
        return False, ""
    for statements, sink_index, _containing_statement in _statement_blocks(function, sink):
        for statement in statements[:sink_index]:
            if not isinstance(statement, ast.Expr) or not isinstance(
                statement.value,
                ast.Call,
            ):
                continue
            call = statement.value
            if not isinstance(call.func, ast.Attribute) or call.func.attr != validator:
                continue
            if not isinstance(call.func.value, ast.Name):
                continue
            if call.func.value.id not in authorization_names:
                continue
            if _projection_validator_matches_sink(call, sink, validator=validator):
                return True, "DerivedProjectionMutationAuthorization"
    return False, ""


def _projection_validator_matches_sink(
    validator_call: ast.Call,
    sink: ast.Call,
    *,
    validator: str,
) -> bool:
    if validator == "assert_upsert":
        expected = list(sink.args[:2])
    elif validator == "assert_delete" and isinstance(sink.func, ast.Attribute):
        expected = [sink.func.value]
    else:
        return False
    if len(validator_call.args) != len(expected):
        return False
    return all(
        ast.dump(actual, include_attributes=False)
        == ast.dump(required, include_attributes=False)
        for actual, required in zip(validator_call.args, expected)
    )


def _statement_blocks(
    function: ast.AST,
    sink: ast.AST,
) -> Iterator[tuple[Sequence[ast.stmt], int, ast.stmt]]:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return

    def walk_block(statements: Sequence[ast.stmt]) -> Iterator[tuple[Sequence[ast.stmt], int, ast.stmt]]:
        for index, statement in enumerate(statements):
            if not _node_contains(statement, sink):
                continue
            yield statements, index, statement
            for child_block in _child_statement_blocks(statement):
                if any(_node_contains(child, sink) for child in child_block):
                    yield from walk_block(child_block)
            return

    yield from walk_block(function.body)


def _child_statement_blocks(statement: ast.stmt) -> Iterator[Sequence[ast.stmt]]:
    for field in ("body", "orelse", "finalbody"):
        value = getattr(statement, field, None)
        if isinstance(value, list) and all(isinstance(item, ast.stmt) for item in value):
            yield value
    if isinstance(statement, ast.Try):
        for handler in statement.handlers:
            yield handler.body
    if isinstance(statement, ast.Match):
        for case in statement.cases:
            yield case.body


def _direct_receipt_assignments(statements: Sequence[ast.stmt]) -> dict[str, str]:
    receipts: dict[str, str] = {}
    for statement in statements:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = statement.value
        if not isinstance(value, ast.Call):
            continue
        call_name = _qualified_name(value.func)
        if not _is_trusted_submission_name(call_name):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            for name in _assigned_names(target):
                receipts[name] = call_name
    return receipts


def _block_terminates(statements: Sequence[ast.stmt]) -> bool:
    if not statements:
        return False
    terminal = statements[-1]
    if isinstance(terminal, (ast.Return, ast.Raise, ast.Continue)):
        return True
    if isinstance(terminal, ast.If):
        return _block_terminates(terminal.body) and _block_terminates(terminal.orelse)
    return False


def _trusted_commit_receipt_type(tree: ast.AST, sink: ast.Call) -> str:
    if not sink.args or not isinstance(sink.args[0], ast.Name):
        return ""
    receipt_name = sink.args[0].id
    function = _enclosing_function(tree, sink)
    if function is None:
        return ""
    if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
            if argument.arg != receipt_name or argument.annotation is None:
                continue
            annotation = _qualified_name(argument.annotation)
            if annotation.endswith(("TrustedPushResult", "TrustedVaultMutationResult", "TrustedMutationReceipt")):
                return annotation
    for node in ast.walk(function):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.lineno >= sink.lineno:
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        call_name = _qualified_name(value.func)
        if not _is_trusted_submission_name(call_name):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(receipt_name in set(_assigned_names(target)) for target in targets):
            return call_name
    return ""


def _function_mentions_trusted_submission(tree: ast.AST, sink: ast.Call) -> bool:
    function = _enclosing_function(tree, sink)
    if function is None:
        return False
    return any(
        isinstance(node, ast.Call) and _is_trusted_submission_name(_qualified_name(node.func))
        for node in ast.walk(function)
    )


def _is_trusted_submission_name(name: str) -> bool:
    leaf = name.rsplit(".", 1)[-1]
    return leaf == "submit_markdown" or leaf.startswith(("submit_", "_submit_")) and (
        "candidate" in leaf
        or "wiki_write" in leaf
        or "application" in leaf
        or "trusted_mutation" in leaf
        or "wiki_mutation" in leaf
    )


def _assigned_names(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for child in node.elts:
            yield from _assigned_names(child)


def _intercepted_receipt_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute) and node.attr == "intercepted" and isinstance(node.value, ast.Name):
        return node.value.id
    return ""


def _not_intercepted_receipt_name(node: ast.AST) -> str:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _intercepted_receipt_name(node.operand)
    return ""


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _node_contains(node, target):
            nested = [
                child
                for child in ast.walk(node)
                if child is not node
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _node_contains(child, target)
            ]
            if not nested:
                return node
    return None


def _node_contains(container: ast.AST, target: ast.AST) -> bool:
    return any(node is target for node in ast.walk(container))


def _load_registry(root: Path) -> dict[str, Mapping[str, str]]:
    path = root / REGISTRY_REL_PATH
    if not path.exists():
        return {}
    raw = json.loads(_read_source(path))
    if not isinstance(raw, dict) or raw.get("schema_version") != "mnemos.trusted_sink_registry.v1":
        raise ValueError(f"invalid trusted sink registry schema: {path}")
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise ValueError(f"trusted sink registry entries must be an object: {path}")
    return {str(key): value for key, value in entries.items() if isinstance(value, dict)}


def _classification_from_registry(entry: Mapping[str, str]) -> _Classification:
    required = {"category", "owner", "target_class", "expires", "reason"}
    missing = sorted(required - set(entry))
    if missing:
        return _Classification(
            category="unclassified",
            reason=f"invalid registry classification missing={','.join(missing)}",
        )
    if any(not str(entry[key]).strip() for key in required):
        return _Classification(
            category="unclassified",
            reason="invalid registry classification contains blank metadata",
        )
    category = str(entry["category"])
    if category not in REGISTRY_CATEGORIES | DEBT_CATEGORIES:
        return _Classification(
            category="unclassified",
            reason=f"invalid registry category: {category}",
        )
    expiry = str(entry["expires"])
    if not _valid_expiry(expiry, category):
        return _Classification(
            category="unclassified",
            reason=f"invalid registry expiry: {expiry}",
        )
    return _Classification(
        category=category,
        reason=str(entry["reason"]),
        target_class=str(entry["target_class"]),
        guard_dominates=False,
        receipt_type=str(entry.get("receipt_type", "")),
        waiver_expiry=expiry,
    )


def _valid_expiry(value: str, category: str) -> bool:
    if value == "never":
        return category not in DEBT_CATEGORIES
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


def _read_source(path: Path) -> str:
    return read_native_bytes(path).decode("utf-8")


def main() -> int:
    issues = scan_trusted_push_guards()
    report = scan_direct_writes()
    counts = report["counts"]
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print(
        "trusted push static scan passed: "
        f"direct_write_sites={report['site_count']} registry={report['registry_count']} "
        f"stale_registry={report['registry_stale_count']} "
        f"unknown={report['unknown_count']} classified={counts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
