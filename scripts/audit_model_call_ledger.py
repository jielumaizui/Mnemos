#!/usr/bin/env python3
"""Fail closed when a direct billable model provider call lacks ledger evidence.

This is a call-site AST audit, not a directory marker or allowlist.  Every
direct billable provider boundary under the runtime source roots must prove a
reservation before dispatch, dispatch marking, settlement (or explicit
handoff), and both pre-dispatch release plus post-dispatch incurred-cost
preservation.  The OpenAI SDK surface is an explicit resource allowlist: an
unrecognized resource call is a failure, never an ignored provider boundary.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SCHEMA_VERSION = "mnemos.model_call_ledger_static_audit.v1"
SOURCE_TARGETS = (
    ROOT / "core",
    ROOT / "scripts",
)
_OPENAI_ALLOWED_RESOURCE_CALLS = {
    ("chat", "completions", "create"): "chat_completion",
    ("embeddings", "create"): "embedding",
    ("responses", "create"): "response",
}
_PROVIDER_SINK_SUFFIXES = {
    **_OPENAI_ALLOWED_RESOURCE_CALLS,
    ("requests", "post"): "http_post",
    ("requests", "request"): "http_request",
}
_OPENAI_CLIENT_FACTORY_NAMES = frozenset({"OpenAI", "AsyncOpenAI", "AzureOpenAI"})
# These resource roots let the audit reject injected clients too, where a
# function parameter has no import/constructor provenance in the local scope.
# A client proven to come from the OpenAI SDK is stricter still: every
# multi-segment resource call must be in ``_OPENAI_ALLOWED_RESOURCE_CALLS``.
_OPENAI_UNPROVEN_RESOURCE_ROOTS = frozenset(
    {
        "assistants",
        "audio",
        "batches",
        "completions",
        "fine_tuning",
        "files",
        "images",
        "moderations",
        "realtime",
        "threads",
        "uploads",
        "vector_stores",
        "videos",
    }
)
_UNSUPPORTED_OPENAI_RESOURCE_KIND = "unsupported_openai_resource"
_UNBOUND = object()
_BUILTIN_EXCEPTION_NAMES = frozenset({"Exception", "BaseException", "OSError"})
_PROVENANCE_EXCEPTION_NAMES = _BUILTIN_EXCEPTION_NAMES | frozenset(
    {"OpenAIError", "RequestException", "HTTPError", "openai_error_type"}
)


@dataclass(frozen=True)
class _ProviderSymbols:
    requests_modules: frozenset[str] = frozenset()
    httpx_modules: frozenset[str] = frozenset()
    openai_modules: frozenset[str] = frozenset()
    openai_client_factory_aliases: frozenset[str] = frozenset()
    callable_aliases: tuple[tuple[str, str], ...] = ()
    session_factory_aliases: frozenset[str] = frozenset()
    global_exception_aliases: frozenset[str] = frozenset()
    global_openai_exception_aliases: frozenset[str] = frozenset()
    global_shadowed_names: frozenset[str] = frozenset()
    verified_exception_factories: frozenset[str] = frozenset()

    def alias_kind(self, name: str) -> str | None:
        return dict(self.callable_aliases).get(name)


@dataclass(frozen=True)
class Sink:
    path: Path
    function: str
    line: int
    kind: str


def _attribute_path(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _has_verified_smoke_exception_factory(tree: ast.AST, path: Path) -> bool:
    """Accept the one optional-SDK exception helper only with its contract.

    ``verify_installation`` has to defer importing ``OpenAIError`` until the
    smoke command actually runs.  A generic callable in an ``except`` clause
    cannot establish coverage, so this deliberately recognizes only that
    exact helper and proves its AST still includes both transport and SDK
    exception families before allowing it.
    """
    if path.resolve() != (ROOT / "scripts" / "verify_installation.py").resolve():
        return False
    for node in tree.body if isinstance(tree, ast.Module) else ():
        if not isinstance(node, ast.FunctionDef) or node.name != "_smoke_exception_types":
            continue
        has_oserror_seed = False
        has_openai_append = False
        has_tuple_return = False
        for child in ast.walk(node):
            assigned_value = (
                child.value if isinstance(child, (ast.Assign, ast.AnnAssign)) else None
            )
            if isinstance(assigned_value, ast.List):
                has_oserror_seed = has_oserror_seed or any(
                    isinstance(item, ast.Name) and item.id == "OSError"
                    for item in assigned_value.elts
                )
            elif isinstance(child, ast.Call) and _call_name(child) == "append":
                has_openai_append = has_openai_append or any(
                    isinstance(item, ast.Name) and item.id == "OpenAIError"
                    for item in child.args
                )
            elif isinstance(child, ast.Return) and isinstance(child.value, ast.Call):
                has_tuple_return = has_tuple_return or (
                    _call_name(child.value) == "tuple"
                    and len(child.value.args) == 1
                    and isinstance(child.value.args[0], ast.Name)
                    and child.value.args[0].id == "exceptions"
                )
        return has_oserror_seed and has_openai_append and has_tuple_return
    return False


def _provider_symbols(tree: ast.AST, path: Path) -> _ProviderSymbols:
    requests_modules: set[str] = set()
    httpx_modules: set[str] = set()
    openai_modules: set[str] = set()
    openai_client_factory_aliases: set[str] = set()
    callable_aliases: dict[str, str] = {}
    session_factory_aliases: set[str] = set()
    global_exception_aliases: set[str] = set()
    global_openai_exception_aliases: set[str] = set()
    global_shadowed_names: set[str] = set()
    module_nodes = tree.body if isinstance(tree, ast.Module) else ()
    for node in module_nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                bound = alias.asname or root
                if root == "requests":
                    requests_modules.add(bound)
                elif root == "httpx":
                    httpx_modules.add(bound)
                elif root == "openai":
                    openai_modules.add(bound)
        elif isinstance(node, ast.ImportFrom) and node.module in {"requests", "httpx", "openai", "builtins"}:
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "openai" and alias.name in _OPENAI_CLIENT_FACTORY_NAMES:
                    openai_client_factory_aliases.add(bound)
                if node.module in {"requests", "httpx"} and alias.name in {"post", "request"}:
                    callable_aliases[bound] = "http_post" if alias.name == "post" else "http_request"
                if (node.module == "requests" and alias.name == "Session") or (
                    node.module == "httpx" and alias.name in {"Client", "AsyncClient"}
                ):
                    session_factory_aliases.add(bound)
                if (node.module == "openai" and alias.name == "OpenAIError") or (
                    node.module in {"requests", "httpx"} and alias.name in {"RequestException", "HTTPError"}
                ) or (node.module == "builtins" and alias.name in _BUILTIN_EXCEPTION_NAMES):
                    global_exception_aliases.add(bound)
                if node.module == "openai" and alias.name == "OpenAIError":
                    global_openai_exception_aliases.add(bound)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id in _PROVENANCE_EXCEPTION_NAMES:
                    global_shadowed_names.add(target.id)
                    global_exception_aliases.discard(target.id)
                    global_openai_exception_aliases.discard(target.id)
                if target.id in openai_client_factory_aliases:
                    global_shadowed_names.add(target.id)
                    openai_client_factory_aliases.discard(target.id)
                if target.id in requests_modules | httpx_modules | openai_modules:
                    global_shadowed_names.add(target.id)
    return _ProviderSymbols(
        requests_modules=frozenset(requests_modules),
        httpx_modules=frozenset(httpx_modules),
        openai_modules=frozenset(openai_modules),
        openai_client_factory_aliases=frozenset(openai_client_factory_aliases),
        callable_aliases=tuple(sorted(callable_aliases.items())),
        session_factory_aliases=frozenset(session_factory_aliases),
        global_exception_aliases=frozenset(global_exception_aliases),
        global_openai_exception_aliases=frozenset(global_openai_exception_aliases),
        global_shadowed_names=frozenset(global_shadowed_names),
        verified_exception_factories=(
            frozenset({"_smoke_exception_types"})
            if _has_verified_smoke_exception_factory(tree, path)
            else frozenset()
        ),
    )


def _is_session_factory(
    call: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> bool:
    if not isinstance(call, ast.Call):
        return False
    if isinstance(call.func, ast.Name):
        if flow is not None:
            return call.func.id in flow.session_factories
        return call.func.id in symbols.session_factory_aliases
    path = _attribute_path(call.func)
    if len(path) < 2:
        return False
    return (
        path[0] in symbols.requests_modules and path[-1] == "Session"
    ) or (
        path[0] in symbols.httpx_modules and path[-1] in {"Client", "AsyncClient"}
    )


def _provider_resource_kind(node: ast.AST, flow: "_Flow | None" = None) -> str | None:
    if isinstance(node, ast.Name) and flow is not None:
        return flow.provider_resources.get(node.id)
    path = _attribute_path(node)
    if len(path) >= 2 and path[-2:] == ("chat", "completions"):
        return "chat_completion"
    if path and path[-1] == "embeddings":
        return "embedding"
    if path and path[-1] == "responses":
        return "response"
    return None


def _is_provider_module_reference(
    node: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> bool:
    if not isinstance(node, ast.Name):
        return False
    if flow is not None and node.id in flow.shadowed_names:
        return False
    return node.id in symbols.requests_modules | symbols.httpx_modules or (
        flow is not None and node.id in flow.provider_modules
    )


def _is_openai_module_reference(
    node: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> bool:
    if not isinstance(node, ast.Name):
        return False
    if flow is not None and node.id in flow.shadowed_names:
        return False
    return node.id in symbols.openai_modules or (flow is not None and node.id in flow.openai_modules)


def _is_openai_client_factory(
    node: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> bool:
    """Recognize constructors whose instances must use the resource allowlist."""
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        if flow is not None and node.func.id in flow.shadowed_names:
            return False
        return node.func.id in symbols.openai_client_factory_aliases or (
            flow is not None and node.func.id in flow.openai_client_factories
        )
    if not isinstance(node.func, ast.Attribute):
        return False
    return (
        node.func.attr in _OPENAI_CLIENT_FACTORY_NAMES
        and _is_openai_module_reference(node.func.value, symbols, flow)
    )


def _openai_resource_path(
    node: ast.AST, flow: "_Flow | None" = None
) -> tuple[str, ...] | None:
    """Return an OpenAI resource path when its provenance is sufficient.

    A known OpenAI client yields an exact resource path.  For dependency
    injection, retain a small set of unambiguous billable resource roots so a
    ``client.images.generate`` parameter call cannot evade the audit merely
    because construction happened outside this lexical scope.
    """
    if isinstance(node, ast.Name):
        if flow is None:
            return None
        if node.id in flow.openai_clients:
            return ()
        return flow.openai_resource_paths.get(node.id)
    if isinstance(node, ast.Attribute):
        receiver_path = _openai_resource_path(node.value, flow)
        if receiver_path is not None:
            return receiver_path + (node.attr,)
        path = _attribute_path(node)
        if not path:
            return None
        if flow is not None and path[0] in flow.openai_clients:
            return path[1:]
        if len(path) >= 2 and path[0] in (
            (flow.openai_modules if flow is not None else set())
        ):
            return path[1:]
        if len(path) >= 2 and path[-1] in _OPENAI_UNPROVEN_RESOURCE_ROOTS:
            return (path[-1],)
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
    ):
        receiver, member = node.args[:2]
        receiver_path = _openai_resource_path(receiver, flow)
        member_name = member.value if isinstance(member, ast.Constant) and isinstance(member.value, str) else None
        if receiver_path is not None:
            return receiver_path + ((member_name,) if member_name is not None else ("<dynamic>",))
        if member_name in _OPENAI_UNPROVEN_RESOURCE_ROOTS:
            return (member_name,)
    return None


def _openai_resource_call_kind(node: ast.AST, flow: "_Flow | None" = None) -> str | None:
    resource_path = _openai_resource_path(node, flow)
    if resource_path is None or len(resource_path) < 2:
        return None
    return _OPENAI_ALLOWED_RESOURCE_CALLS.get(resource_path, _UNSUPPORTED_OPENAI_RESOURCE_KIND)


def _is_openai_error_factory(
    node: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> bool:
    if not isinstance(node, ast.Call) or _call_name(node) != "getattr" or len(node.args) < 2:
        return False
    receiver, member = node.args[:2]
    return (
        _is_openai_module_reference(receiver, symbols, flow)
        and isinstance(member, ast.Constant)
        and member.value == "OpenAIError"
    )


def _sink_kind_for_path(path: tuple[str, ...]) -> str | None:
    for suffix, kind in _PROVIDER_SINK_SUFFIXES.items():
        if len(path) >= len(suffix) and path[-len(suffix) :] == suffix:
            return kind
    return None


def _provider_callable_kind(
    node: ast.AST, symbols: _ProviderSymbols, flow: "_Flow | None" = None
) -> str | None:
    if isinstance(node, ast.Name):
        if flow is not None and node.id in flow.provider_aliases:
            return flow.provider_aliases[node.id]
        return symbols.alias_kind(node.id)
    if isinstance(node, ast.Call):
        if _call_name(node) == "partial" and node.args:
            return _provider_callable_kind(node.args[0], symbols, flow)
        if _call_name(node) != "getattr" or len(node.args) < 2:
            return None
        receiver, member = node.args[:2]
        member_name = member.value if isinstance(member, ast.Constant) and isinstance(member.value, str) else None
        resource_kind = _provider_resource_kind(receiver, flow)
        if resource_kind is not None:
            return resource_kind if member_name in {None, "create"} else None
        openai_resource_kind = _openai_resource_call_kind(node, flow)
        if openai_resource_kind is not None:
            return openai_resource_kind
        if member_name is None:
            if _is_provider_module_reference(receiver, symbols, flow) or (
                isinstance(receiver, ast.Name) and flow is not None and receiver.id in flow.provider_sessions
            ):
                return "http_request"
            return None
        receiver_path = _attribute_path(receiver)
        kind = _sink_kind_for_path(receiver_path + (member_name,))
        if kind is not None:
            return kind
        if not isinstance(receiver, ast.Name):
            return None
        receiver_is_transport = _is_provider_module_reference(receiver, symbols, flow)
        receiver_is_session = flow is not None and receiver.id in flow.provider_sessions
        if not receiver_is_transport and not receiver_is_session:
            return None
        if member_name == "post":
            return "http_post"
        if member_name == "request":
            return "http_request"
        return None
    if not isinstance(node, ast.Attribute):
        return None
    resource_kind = _provider_resource_kind(node.value, flow) if node.attr == "create" else None
    if resource_kind is not None:
        return resource_kind
    openai_resource_kind = _openai_resource_call_kind(node, flow)
    if openai_resource_kind is not None:
        return openai_resource_kind
    if node.attr not in {"post", "request"}:
        return _sink_kind_for_path(_attribute_path(node))
    kind = "http_post" if node.attr == "post" else "http_request"
    path = _attribute_path(node)
    known_kind = _sink_kind_for_path(path)
    if known_kind is not None:
        return known_kind
    if len(path) >= 2 and path[0] in symbols.requests_modules | symbols.httpx_modules:
        return kind
    if isinstance(node.value, ast.Call) and _is_session_factory(node.value, symbols, flow):
        return kind
    if isinstance(node.value, ast.Name) and flow is not None and (
        node.value.id in flow.provider_sessions or node.value.id in flow.provider_modules
    ):
        return kind
    return None


def _is_provider_sink(
    call: ast.Call, symbols: _ProviderSymbols | None = None, flow: "_Flow | None" = None
) -> str | None:
    kind = _sink_kind_for_path(_attribute_path(call.func))
    if kind is not None:
        return kind
    return _provider_callable_kind(call.func, symbols or _ProviderSymbols(), flow)


def _call_name(call: ast.Call) -> str:
    path = _attribute_path(call.func)
    return path[-1] if path else ""


def _receiver_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        return call.func.value.id
    return ""


@dataclass(frozen=True)
class _ReservationState:
    """One local reservation on one symbolic control-flow path.

    ``used`` is deliberately separate from the lifecycle state.  A request
    source line may execute repeatedly (for example through a loop), but each
    dispatch needs a new reservation.  Reusing a dispatched entry for a second
    provider request is therefore a static failure even when a later settle is
    present.
    """

    lifecycle: str
    used: bool = False
    active_sinks: tuple[tuple[int, str], ...] = ()


@dataclass
class _Flow:
    bindings: dict[str, int | None]
    reservations: dict[int, _ReservationState]
    provider_aliases: dict[str, str] = field(default_factory=dict)
    provider_sessions: set[str] = field(default_factory=set)
    session_factories: set[str] = field(default_factory=set)
    provider_resources: dict[str, str] = field(default_factory=dict)
    provider_modules: set[str] = field(default_factory=set)
    openai_modules: set[str] = field(default_factory=set)
    openai_client_factories: set[str] = field(default_factory=set)
    openai_clients: set[str] = field(default_factory=set)
    openai_resource_paths: dict[str, tuple[str, ...]] = field(default_factory=dict)
    exception_aliases: set[str] = field(default_factory=set)
    openai_exception_aliases: set[str] = field(default_factory=set)
    shadowed_names: set[str] = field(default_factory=set)
    control: str = "normal"

    def clone(self) -> "_Flow":
        return _Flow(
            dict(self.bindings),
            dict(self.reservations),
            dict(self.provider_aliases),
            set(self.provider_sessions),
            set(self.session_factories),
            dict(self.provider_resources),
            set(self.provider_modules),
            set(self.openai_modules),
            set(self.openai_client_factories),
            set(self.openai_clients),
            dict(self.openai_resource_paths),
            set(self.exception_aliases),
            set(self.openai_exception_aliases),
            set(self.shadowed_names),
            self.control,
        )


@dataclass(frozen=True)
class _SinkContext:
    line: int
    kind: str
    reservation_name: str
    try_stack: tuple[ast.Try, ...]
    exception_aliases: frozenset[str]
    openai_exception_aliases: frozenset[str]
    transport_modules: frozenset[str]
    openai_modules: frozenset[str]
    shadowed_names: frozenset[str]


def _is_reservation_factory(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) in {"reserve", "_smoke_ledger"}


def _is_handoff_return(value: ast.AST | None, reservation_name: str) -> bool:
    """Return true only for an actually returned reservation handoff.

    The former audit walked every ``dict`` node in the function.  That let an
    unreachable or unrelated dictionary prove settlement.  A handoff is valid
    only when the function returns a payload that directly carries the same
    local reservation.
    """
    if isinstance(value, ast.Dict):
        for key, item in zip(value.keys, value.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "_ledger_reservation"
                and isinstance(item, ast.Name)
                and item.id == reservation_name
            ):
                return True
        return any(_is_handoff_return(item, reservation_name) for item in value.values)
    if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_handoff_return(item, reservation_name) for item in value.elts)
    return False


def _scope_has_provider_sink(statements: Sequence[ast.stmt], symbols: _ProviderSymbols) -> bool:
    """Cheaply skip scopes which cannot contribute a provider boundary.

    Most source functions are irrelevant to this audit.  More importantly,
    walking their ordinary conditionals would multiply symbolic paths without
    producing a sink to validate.  Nested scopes are intentionally excluded:
    the visitor audits each one as its own lexical boundary.
    """
    pending: list[ast.AST] = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call) and (
            _is_provider_sink(node, symbols) is not None or _is_session_factory(node, symbols)
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module in {"requests", "httpx"} and any(
            alias.name in {"post", "request", "Session", "Client", "AsyncClient"}
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "openai" and any(
            alias.name in _OPENAI_CLIENT_FACTORY_NAMES for alias in node.names
        ):
            return True
        if isinstance(node, ast.Import) and any(
            alias.name.split(".", 1)[0] in {"requests", "httpx", "openai"} for alias in node.names
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"post", "request"}:
            return True
        if _openai_resource_path(node) is not None:
            return True
        if (
            isinstance(node, ast.Call)
            and _call_name(node) == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"post", "request", "create"}
        ):
            return True
        if _provider_callable_kind(node, symbols) is not None:
            return True
        if _provider_resource_kind(node) is not None or _is_provider_module_reference(node, symbols):
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


class _PathAudit:
    """A deliberately small, conservative path interpreter for one scope.

    This is not a whole-program theorem prover.  It tracks the local values
    that originate in ``ledger.reserve`` and follows Python statement-level
    control flow.  Unknown conditions fork, loops run zero/one/two iterations,
    and an unresolved path is a finding.  That is enough to make reservation
    and dispatch dominate each provider sink rather than merely appear earlier
    in the source file.
    """

    _TERMINAL_STATES = {"settled", "preserved", "released", "handed_off"}

    def __init__(self, path: Path, function: str, symbols: _ProviderSymbols):
        self.path = path
        self.function = function
        self.symbols = symbols
        self._next_reservation_id = 0
        self._missing: dict[tuple[int, str], set[str]] = {}
        self._sink_contexts: list[_SinkContext] = []

    def audit(
        self, statements: Sequence[ast.stmt], *, initial_shadowed_names: Iterable[str] = ()
    ) -> list[dict[str, Any]]:
        initial = _Flow(
            {},
            {},
            session_factories=set(self.symbols.session_factory_aliases),
            openai_client_factories=set(self.symbols.openai_client_factory_aliases),
            exception_aliases=set(self.symbols.global_exception_aliases),
            openai_exception_aliases=set(
                self.symbols.global_openai_exception_aliases
            ),
            shadowed_names=set(self.symbols.global_shadowed_names) | set(initial_shadowed_names),
        )
        flows = self._block(statements, [initial], ())
        for flow in flows:
            for reservation in flow.reservations.values():
                for line, kind in reservation.active_sinks:
                    self._record(line, kind, "settlement_or_handoff")

        seen_contexts: set[tuple[Any, ...]] = set()
        for context in self._sink_contexts:
            key = (
                context.line,
                context.kind,
                context.reservation_name,
                tuple(id(item) for item in context.try_stack),
                context.exception_aliases,
                context.openai_exception_aliases,
                context.transport_modules,
                context.openai_modules,
                context.shadowed_names,
            )
            if key in seen_contexts:
                continue
            seen_contexts.add(key)
            if not self._has_exception_lifecycle(context):
                self._record(context.line, context.kind, "exception_terminal_lifecycle")

        findings: list[dict[str, Any]] = []
        for (line, kind), missing in sorted(self._missing.items()):
            findings.append(
                {
                    "file": _display_path(self.path),
                    "function": self.function,
                    "line": line,
                    "kind": kind,
                    "missing": sorted(missing),
                }
            )
        return findings

    def _record(self, line: int, kind: str, missing: str) -> None:
        self._missing.setdefault((line, kind), set()).add(missing)

    def _block(
        self,
        statements: Sequence[ast.stmt],
        incoming: Sequence[_Flow],
        try_stack: tuple[ast.Try, ...],
    ) -> list[_Flow]:
        active = list(incoming)
        completed: list[_Flow] = []
        for statement in statements:
            next_active: list[_Flow] = []
            for flow in active:
                for result in self._statement(statement, flow, try_stack):
                    if result.control == "normal":
                        next_active.append(result)
                    else:
                        completed.append(result)
            active = next_active
        return active + completed

    def _statement(
        self, statement: ast.stmt, flow: _Flow, try_stack: tuple[ast.Try, ...]
    ) -> list[_Flow]:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Nested scopes are audited independently by the visitor.  Their
            # reservations must never prove the enclosing scope's provider call.
            return [flow]
        if isinstance(statement, ast.Import):
            output = flow.clone()
            for alias in statement.names:
                root = alias.name.split(".", 1)[0]
                bound = alias.asname or root
                output.shadowed_names.discard(bound)
                if root in {"requests", "httpx"}:
                    output.provider_modules.add(bound)
                elif root == "openai":
                    output.openai_modules.add(bound)
            return [output]
        if isinstance(statement, ast.ImportFrom):
            output = flow.clone()
            for alias in statement.names:
                bound = alias.asname or alias.name
                output.shadowed_names.discard(bound)
                if statement.module in {"requests", "httpx"} and alias.name in {"post", "request"}:
                    output.provider_aliases[bound] = (
                        "http_post" if alias.name == "post" else "http_request"
                    )
                if (statement.module == "requests" and alias.name == "Session") or (
                    statement.module == "httpx" and alias.name in {"Client", "AsyncClient"}
                ):
                    output.session_factories.add(bound)
                if statement.module == "openai" and alias.name in _OPENAI_CLIENT_FACTORY_NAMES:
                    output.openai_client_factories.add(bound)
                if (statement.module == "openai" and alias.name == "OpenAIError") or (
                    statement.module in {"requests", "httpx"}
                    and alias.name in {"RequestException", "HTTPError"}
                ) or (statement.module == "builtins" and alias.name in _BUILTIN_EXCEPTION_NAMES):
                    output.exception_aliases.add(bound)
                if statement.module == "openai" and alias.name == "OpenAIError":
                    output.openai_exception_aliases.add(bound)
            return [output]
        if isinstance(statement, ast.Assign):
            outputs = self._expression(statement.value, flow, try_stack)
            return [self._assign(statement.targets, statement.value, output) for output in outputs]
        if isinstance(statement, ast.AnnAssign):
            if statement.value is None:
                return [flow]
            outputs = self._expression(statement.value, flow, try_stack)
            return [self._assign([statement.target], statement.value, output) for output in outputs]
        if isinstance(statement, ast.AugAssign):
            return self._expression(statement.value, flow, try_stack)
        if isinstance(statement, ast.Expr):
            return self._expression(statement.value, flow, try_stack)
        if isinstance(statement, ast.Return):
            outputs = self._expression(statement.value, flow, try_stack) if statement.value else [flow]
            for output in outputs:
                for name, reservation_id in list(output.bindings.items()):
                    if reservation_id is None or not _is_handoff_return(statement.value, name):
                        continue
                    reservation = output.reservations.get(reservation_id)
                    if reservation and reservation.active_sinks:
                        output.reservations[reservation_id] = _ReservationState("handed_off")
                output.control = "return"
            return outputs
        if isinstance(statement, ast.Raise):
            outputs = self._expression(statement.exc, flow, try_stack) if statement.exc else [flow]
            for output in outputs:
                output.control = "raise"
            return outputs
        if isinstance(statement, ast.If):
            outputs = self._expression(statement.test, flow, try_stack)
            if_result: list[_Flow] = []
            for output in outputs:
                decision = self._condition(statement.test, output)
                if decision is not False:
                    if_result.extend(self._block(statement.body, [output.clone()], try_stack))
                if decision is not True:
                    if_result.extend(self._block(statement.orelse, [output.clone()], try_stack))
            return if_result
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(statement, flow, try_stack)
        if isinstance(statement, ast.Try):
            return self._try(statement, flow, try_stack)
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            outputs = [flow]
            for item in statement.items:
                next_outputs: list[_Flow] = []
                for output in outputs:
                    next_outputs.extend(self._expression(item.context_expr, output, try_stack))
                outputs = next_outputs
            return self._block(statement.body, outputs, try_stack)
        if isinstance(statement, ast.Match):
            outputs = self._expression(statement.subject, flow, try_stack)
            match_result: list[_Flow] = []
            for output in outputs:
                for case in statement.cases:
                    match_result.extend(self._block(case.body, [output.clone()], try_stack))
            return match_result or outputs
        if isinstance(statement, ast.Break):
            output = flow.clone()
            output.control = "break"
            return [output]
        if isinstance(statement, ast.Continue):
            output = flow.clone()
            output.control = "continue"
            return [output]
        if isinstance(statement, ast.Assert):
            return self._expression(statement.test, flow, try_stack)
        return [flow]

    def _loop(
        self,
        statement: ast.For | ast.AsyncFor | ast.While,
        flow: _Flow,
        try_stack: tuple[ast.Try, ...],
    ) -> list[_Flow]:
        outputs = [flow]
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            outputs = self._expression(statement.iter, flow, try_stack)

        exits: list[_Flow] = [output.clone() for output in outputs]  # zero iterations
        iteration_inputs = [output.clone() for output in outputs]
        terminal: list[_Flow] = []
        for _ in range(2):
            iteration_outputs: list[_Flow] = []
            for item in iteration_inputs:
                body_input = item.clone()
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    self._bind_loop_target(statement.target, body_input)
                iteration_outputs.extend(self._block(statement.body, [body_input], try_stack))
            next_iteration: list[_Flow] = []
            for item in iteration_outputs:
                if item.control in {"normal", "continue"}:
                    item.control = "normal"
                    exits.append(item.clone())  # the loop may stop here
                    next_iteration.append(item)
                elif item.control == "break":
                    item.control = "normal"
                    exits.append(item)
                else:
                    terminal.append(item)
            iteration_inputs = next_iteration

        if statement.orelse:
            after_else: list[_Flow] = []
            for item in exits:
                after_else.extend(self._block(statement.orelse, [item], try_stack))
            exits = after_else
        return exits + terminal

    def _try(self, statement: ast.Try, flow: _Flow, try_stack: tuple[ast.Try, ...]) -> list[_Flow]:
        body = self._block(statement.body, [flow], try_stack + (statement,))
        normal: list[_Flow] = []
        raised: list[_Flow] = []
        other: list[_Flow] = []
        for item in body:
            if item.control == "normal":
                normal.append(item)
            elif item.control == "raise":
                raised.append(item)
            else:
                other.append(item)

        outcomes: list[_Flow] = []
        for item in normal:
            outcomes.extend(self._block(statement.orelse, [item], try_stack))
        for item in raised:
            if not statement.handlers:
                outcomes.append(item)
                continue
            # An explicit raise can match any syntactically visible handler.
            # Auditing all of them is conservative and prevents a narrow error
            # branch from silently leaking a dispatched reservation.
            for handler in statement.handlers:
                handled = item.clone()
                handled.control = "normal"
                if handler.name:
                    handled.bindings.pop(handler.name, None)
                outcomes.extend(self._block(handler.body, [handled], try_stack))
        outcomes.extend(other)

        if not statement.finalbody:
            return outcomes
        finalized: list[_Flow] = []
        for item in outcomes:
            original_control = item.control
            work = item.clone()
            work.control = "normal"
            for final in self._block(statement.finalbody, [work], try_stack):
                if final.control == "normal":
                    final.control = original_control
                finalized.append(final)
        return finalized

    def _expression(
        self, expression: ast.AST | None, flow: _Flow, try_stack: tuple[ast.Try, ...]
    ) -> list[_Flow]:
        if expression is None or isinstance(expression, (ast.Constant, ast.Name)):
            return [flow]
        if isinstance(expression, ast.Lambda):
            # Lambdas are independent execution scopes.  The enclosing scope
            # cannot use their lifecycle calls as evidence.
            return [flow]
        if isinstance(expression, ast.Call):
            outputs = [flow]
            for child in [expression.func, *expression.args, *(kw.value for kw in expression.keywords)]:
                next_outputs: list[_Flow] = []
                for output in outputs:
                    next_outputs.extend(self._expression(child, output, try_stack))
                outputs = next_outputs
            call_result: list[_Flow] = []
            for output in outputs:
                self._apply_reservation_action(expression, output, try_stack)
                call_result.append(output)
            return call_result
        if isinstance(expression, ast.IfExp):
            outputs = self._expression(expression.test, flow, try_stack)
            if_expression_result: list[_Flow] = []
            for output in outputs:
                decision = self._condition(expression.test, output)
                if decision is not False:
                    if_expression_result.extend(
                        self._expression(expression.body, output.clone(), try_stack)
                    )
                if decision is not True:
                    if_expression_result.extend(
                        self._expression(expression.orelse, output.clone(), try_stack)
                    )
            return if_expression_result

        outputs = [flow]
        for nested_node in ast.iter_child_nodes(expression):
            if isinstance(
                nested_node,
                (ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop),
            ):
                continue
            nested_outputs: list[_Flow] = []
            for output in outputs:
                nested_outputs.extend(self._expression(nested_node, output, try_stack))
            outputs = nested_outputs
        return outputs

    def _assign(self, targets: Sequence[ast.expr], value: ast.AST, flow: _Flow) -> _Flow:
        output = flow.clone()
        callable_kind = _provider_callable_kind(value, self.symbols, output)
        session_value = _is_session_factory(value, self.symbols, output) or (
            isinstance(value, ast.Name) and value.id in output.provider_sessions
        )
        session_factory_value = isinstance(value, ast.Name) and value.id in output.session_factories
        resource_kind = _provider_resource_kind(value, output)
        module_value = _is_provider_module_reference(value, self.symbols, output)
        openai_module_value = _is_openai_module_reference(value, self.symbols, output)
        openai_client_factory_value = isinstance(value, ast.Name) and (
            value.id in output.openai_client_factories
        )
        openai_client_value = _is_openai_client_factory(value, self.symbols, output) or (
            isinstance(value, ast.Name) and value.id in output.openai_clients
        )
        openai_resource_path = _openai_resource_path(value, output)
        exception_alias_value = (
            isinstance(value, ast.Name)
            and value.id in output.exception_aliases
            and value.id not in output.shadowed_names
        ) or _is_openai_error_factory(value, self.symbols, output)
        openai_exception_alias_value = (
            isinstance(value, ast.Name)
            and value.id in output.openai_exception_aliases
            and value.id not in output.shadowed_names
        ) or _is_openai_error_factory(value, self.symbols, output)
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if _is_reservation_factory(value):
                self._next_reservation_id += 1
                output.bindings[target.id] = self._next_reservation_id
                output.reservations[self._next_reservation_id] = _ReservationState("reserved")
            elif isinstance(value, ast.Name) and value.id in output.bindings:
                output.bindings[target.id] = output.bindings[value.id]
            elif isinstance(value, ast.Constant) and value.value is None:
                output.bindings[target.id] = None
            else:
                output.bindings.pop(target.id, None)
            if callable_kind is None:
                output.provider_aliases.pop(target.id, None)
            else:
                output.provider_aliases[target.id] = callable_kind
            if session_value:
                output.provider_sessions.add(target.id)
            else:
                output.provider_sessions.discard(target.id)
            if session_factory_value:
                output.session_factories.add(target.id)
            else:
                output.session_factories.discard(target.id)
            if resource_kind is None:
                output.provider_resources.pop(target.id, None)
            else:
                output.provider_resources[target.id] = resource_kind
            if module_value:
                output.provider_modules.add(target.id)
            else:
                output.provider_modules.discard(target.id)
            if openai_module_value:
                output.openai_modules.add(target.id)
            else:
                output.openai_modules.discard(target.id)
            if openai_client_factory_value:
                output.openai_client_factories.add(target.id)
            else:
                output.openai_client_factories.discard(target.id)
            if openai_client_value:
                output.openai_clients.add(target.id)
            else:
                output.openai_clients.discard(target.id)
            if openai_resource_path:
                output.openai_resource_paths[target.id] = openai_resource_path
            else:
                output.openai_resource_paths.pop(target.id, None)
            output.exception_aliases.discard(target.id)
            output.openai_exception_aliases.discard(target.id)
            if target.id in _PROVENANCE_EXCEPTION_NAMES:
                output.shadowed_names.add(target.id)
            if target.id in (
                self.symbols.requests_modules
                | self.symbols.httpx_modules
                | self.symbols.openai_modules
                | self.symbols.openai_client_factory_aliases
            ):
                output.shadowed_names.add(target.id)
            if exception_alias_value:
                output.exception_aliases.add(target.id)
                output.shadowed_names.discard(target.id)
            if openai_exception_alias_value:
                output.openai_exception_aliases.add(target.id)
        return output

    def _bind_loop_target(self, target: ast.expr, flow: _Flow) -> None:
        if isinstance(target, ast.Name):
            flow.bindings.pop(target.id, None)
            flow.provider_aliases.pop(target.id, None)
            flow.provider_sessions.discard(target.id)
            flow.session_factories.discard(target.id)
            flow.provider_resources.pop(target.id, None)
            flow.provider_modules.discard(target.id)
            flow.openai_modules.discard(target.id)
            flow.openai_client_factories.discard(target.id)
            flow.openai_clients.discard(target.id)
            flow.openai_resource_paths.pop(target.id, None)
            flow.exception_aliases.discard(target.id)
            flow.openai_exception_aliases.discard(target.id)
            flow.shadowed_names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._bind_loop_target(item, flow)

    def _condition(self, expression: ast.AST, flow: _Flow) -> bool | None:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, bool):
            return expression.value
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            result = self._condition(expression.operand, flow)
            return None if result is None else not result
        if isinstance(expression, ast.Attribute) and expression.attr == "dispatched":
            if isinstance(expression.value, ast.Name):
                reservation_id = flow.bindings.get(expression.value.id)
                reservation = flow.reservations.get(reservation_id) if reservation_id else None
                if reservation is not None:
                    if reservation.lifecycle == "dispatched":
                        return True
                    if reservation.lifecycle in self._TERMINAL_STATES | {"reserved"}:
                        return False
        if isinstance(expression, ast.Compare) and len(expression.ops) == len(expression.comparators) == 1:
            left = expression.left
            right = expression.comparators[0]
            if isinstance(left, ast.Name) and isinstance(right, ast.Constant) and right.value is None:
                bound = flow.bindings.get(left.id, _UNBOUND)
                if isinstance(expression.ops[0], ast.Is):
                    return bound is None if bound is not _UNBOUND else None
                if isinstance(expression.ops[0], ast.IsNot):
                    return bound is not None if bound is not _UNBOUND else None
        return None

    def _apply_reservation_action(
        self, call: ast.Call, flow: _Flow, try_stack: tuple[ast.Try, ...]
    ) -> None:
        kind = _is_provider_sink(call, self.symbols, flow)
        if kind is not None:
            self._provider_sink(call.lineno, kind, flow, try_stack)
            return
        receiver = _receiver_name(call)
        reservation_id = flow.bindings.get(receiver)
        if reservation_id is None:
            return
        reservation = flow.reservations.get(reservation_id)
        if reservation is None:
            return
        action = _call_name(call)
        if action == "mark_dispatched" and reservation.lifecycle == "reserved":
            flow.reservations[reservation_id] = _ReservationState("dispatched")
        elif action == "settle" and reservation.lifecycle == "dispatched":
            flow.reservations[reservation_id] = _ReservationState("settled")
        elif action == "preserve_incurred" and reservation.lifecycle == "dispatched":
            flow.reservations[reservation_id] = _ReservationState("preserved")
        elif action == "release" and reservation.lifecycle == "reserved":
            flow.reservations[reservation_id] = _ReservationState("released")

    def _provider_sink(
        self, line: int, kind: str, flow: _Flow, try_stack: tuple[ast.Try, ...]
    ) -> None:
        if kind == _UNSUPPORTED_OPENAI_RESOURCE_KIND:
            # The static gate cannot price an unallowlisted SDK surface safely.
            # Keep analyzing its lifecycle for adjacent evidence, but never let
            # a locally complete reservation contract grant it an implicit pass.
            self._record(line, kind, "unsupported_openai_resource")
        candidates = {
            reservation_id
            for reservation_id in flow.bindings.values()
            if reservation_id is not None
            and reservation_id in flow.reservations
            and flow.reservations[reservation_id].lifecycle == "dispatched"
        }
        if not candidates:
            if any(reservation_id is not None for reservation_id in flow.bindings.values()):
                self._record(line, kind, "dispatch_mark")
            else:
                self._record(line, kind, "pre_dispatch_reservation")
            return
        if len(candidates) != 1:
            self._record(line, kind, "reservation_ambiguity")
            return
        reservation_id = next(iter(candidates))
        reservation = flow.reservations[reservation_id]
        if reservation.used:
            self._record(line, kind, "reservation_reused")
            return
        names = sorted(name for name, item in flow.bindings.items() if item == reservation_id)
        if not names:
            self._record(line, kind, "pre_dispatch_reservation")
            return
        flow.reservations[reservation_id] = _ReservationState(
            "dispatched", used=True, active_sinks=((line, kind),)
        )
        self._sink_contexts.append(
            _SinkContext(
                line,
                kind,
                names[0],
                try_stack,
                frozenset(flow.exception_aliases),
                frozenset(flow.openai_exception_aliases),
                frozenset(
                    (self.symbols.requests_modules | self.symbols.httpx_modules | flow.provider_modules)
                    - flow.shadowed_names
                ),
                frozenset((self.symbols.openai_modules | flow.openai_modules) - flow.shadowed_names),
                frozenset(flow.shadowed_names),
            )
        )

    def _has_exception_lifecycle(self, context: _SinkContext) -> bool:
        # Every enclosing handler is a possible owner: a narrow inner handler
        # may swallow one provider failure, while another error can escape it
        # and be swallowed by an outer handler.  Do not let either level prove
        # the other.  In the absence of whole-program exception type inference,
        # fail closed unless every syntactically reachable handler proves both
        # sides of the reservation state split.
        for item in reversed(context.try_stack):
            if item.handlers:
                if not all(
                    self._handler_has_terminal_pair(handler, context.reservation_name)
                    for handler in item.handlers
                ):
                    return False
                if self._handlers_cover_provider_exception(item.handlers, context):
                    return True
        return False

    def _handlers_cover_provider_exception(
        self, handlers: Sequence[ast.ExceptHandler], context: _SinkContext
    ) -> bool:
        """Whether this handler set catches the direct provider's SDK error.

        We do not need a general exception hierarchy solver here.  The ledger
        boundary deliberately uses one of these canonical provider families:
        OpenAI-compatible SDK calls expose ``OpenAIError`` (or the local
        ``openai_error_type`` alias); requests calls expose ``RequestException``
        / ``OSError``.  A broad ``Exception`` is also explicit coverage.  A
        narrow inner ``ValueError`` must not hide an outer handler, because a
        transport failure can pass through it.
        """
        names: set[str] = set()
        for handler in handlers:
            exception_type = handler.type
            if exception_type is None:
                return True
            candidates = exception_type.elts if isinstance(exception_type, ast.Tuple) else [exception_type]
            for candidate in candidates:
                if (
                    isinstance(candidate, ast.Call)
                    and isinstance(candidate.func, ast.Name)
                    and candidate.func.id in self.symbols.verified_exception_factories
                ):
                    return True
                if isinstance(candidate, ast.Name):
                    if candidate.id in _BUILTIN_EXCEPTION_NAMES - context.shadowed_names:
                        names.add(candidate.id)
                    elif (
                        candidate.id in context.exception_aliases
                        and candidate.id not in context.shadowed_names
                    ):
                        names.add(
                            "OpenAIError"
                            if candidate.id in context.openai_exception_aliases
                            else candidate.id
                        )
                elif isinstance(candidate, ast.Attribute):
                    path = _attribute_path(candidate)
                    if (
                        len(path) >= 2
                        and path[-1] in {"RequestException", "HTTPError"}
                        and path[0] in context.transport_modules
                    ):
                        names.add(path[-1])
                    elif (
                        len(path) >= 2
                        and path[-1] == "OpenAIError"
                        and path[0] in context.openai_modules
                    ):
                        names.add("OpenAIError")
        if {"Exception", "BaseException"} & names:
            return True
        if context.kind in set(_OPENAI_ALLOWED_RESOURCE_CALLS.values()) | {
            _UNSUPPORTED_OPENAI_RESOURCE_KIND
        }:
            return "OpenAIError" in names
        return bool({"RequestException", "HTTPError", "OSError"} & names)

    def _handler_has_terminal_pair(self, handler: ast.ExceptHandler, reservation_name: str) -> bool:
        pre = _Flow(
            {reservation_name: -1},
            {-1: _ReservationState("reserved")},
        )
        post = _Flow(
            {reservation_name: -1},
            {-1: _ReservationState("dispatched", used=True, active_sinks=((-1, "unknown"),))},
        )
        pre_outcomes = self._block(handler.body, [pre], ())
        post_outcomes = self._block(handler.body, [post], ())
        return bool(pre_outcomes) and bool(post_outcomes) and all(
            item.reservations.get(-1, _ReservationState("unknown")).lifecycle == "released"
            for item in pre_outcomes
        ) and all(
            item.reservations.get(-1, _ReservationState("unknown")).lifecycle == "preserved"
            for item in post_outcomes
        )


def _audit_scope(
    path: Path,
    function: str,
    statements: Sequence[ast.stmt],
    symbols: _ProviderSymbols,
    *,
    initial_shadowed_names: Iterable[str] = (),
) -> list[dict[str, Any]]:
    if not _scope_has_provider_sink(statements, symbols):
        return []
    return _PathAudit(path, function, symbols).audit(
        statements, initial_shadowed_names=initial_shadowed_names
    )


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


class _AuditVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, symbols: _ProviderSymbols):
        self.path = path
        self.symbols = symbols
        self.findings: list[dict[str, Any]] = []

    def visit_Module(self, node: ast.Module) -> None:
        self.findings.extend(_audit_scope(self.path, "<module>", node.body, self.symbols))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.findings.extend(_audit_scope(self.path, f"<class {node.name}>", node.body, self.symbols))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.findings.extend(
            _audit_scope(
                self.path,
                node.name,
                node.body,
                self.symbols,
                initial_shadowed_names=_argument_names(node.args),
            )
        )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.findings.extend(
            _audit_scope(
                self.path,
                node.name,
                node.body,
                self.symbols,
                initial_shadowed_names=_argument_names(node.args),
            )
        )
        self.generic_visit(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # A lambda is an executable scope, not harmless expression metadata.
        # Audit its body independently so ``send = lambda: requests.post(...)``
        # cannot evade the lexical boundary check.
        self.findings.extend(
            _audit_scope(self.path, "<lambda>", [ast.Expr(value=node.body)], self.symbols)
        )
        self.generic_visit(node)


def _source_files(targets: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.suffix == ".py" and target.is_file():
            files.append(target)
    return files


def audit_model_call_ledger(targets: Iterable[Path] = SOURCE_TARGETS) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    parsed_files = 0
    parse_errors: list[dict[str, str]] = []
    for path in _source_files(targets):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            parse_errors.append({"file": _display_path(path), "error": type(exc).__name__})
            continue
        parsed_files += 1
        visitor = _AuditVisitor(path, _provider_symbols(tree, path))
        visitor.visit(tree)
        findings.extend(visitor.findings)
    findings.sort(key=lambda item: (item["file"], item["line"], item["function"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "parsed_file_count": parsed_files,
        "parse_error_count": len(parse_errors),
        "parse_errors": parse_errors,
        "billable_calls_without_ledger": len(findings),
        "violations": findings,
        "ok": not findings and not parse_errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--target",
        type=Path,
        action="append",
        help="override one audited source target (repeatable)",
    )
    args = parser.parse_args(argv)
    targets = tuple(args.target) if args.target else SOURCE_TARGETS
    result = audit_model_call_ledger(targets)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=None if args.json else 2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
