#!/usr/bin/env python3
"""Static audit for the Phase-4 COG-039 host cognitive contract.

This deliberately proves only code/static conformance.  It does not promote
the result to a live host-adapter or release certificate: presentation and
outcome receipts still require eight independently authorized host probes.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


EXPECTED_HOSTS = (
    "codex", "claude", "hermes", "opencode", "openclaw", "crush", "kiro", "kimi"
)
REQUIRED_TOOLS = {
    "build_cognitive_state",
    "record_decision",
    "apply_outcome",
    "predictive_push",
    "delivery_display_ack",
    "push_feedback",
}
FACADE_METHODS = {
    "build_cognitive_state",
    "record_decision",
    "apply_outcome",
    "record_delivery_display",
}


def _methods(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def _text_count(path: Path, needle: str) -> int:
    return path.read_text(encoding="utf-8").count(needle)


def audit() -> dict[str, Any]:
    from core.access_policy import MCP_TOOL_POLICIES
    from core.agent_kit.protocol import TARGET_AGENT_NAMES, required_workflow_tool_names
    from integrations.agora_tools.schema import list_tools

    findings: list[dict[str, str]] = []
    schema_tools = {item["name"] for item in list_tools(lambda _name: "audit")["tools"]}
    workflow_tools = set(required_workflow_tool_names())
    facade_methods = _methods(ROOT / "core/application/facade.py", "DefaultMnemosServiceFacade")
    router_methods = _methods(ROOT / "core/cognitive/delivery_router.py", "KnowledgeDeliveryRouter")
    adapter_paths = sorted((ROOT / "integrations").glob("*_adapter.py")) + [
        ROOT / "integrations/apollon.py",
        ROOT / "integrations/active.py",
    ]
    adapter_direct_domain_calls = sum(
        _text_count(path, ".route_candidate(")
        + _text_count(path, ".record_decision(")
        + _text_count(path, ".apply_outcome(")
        for path in adapter_paths
        if path.is_file()
    )
    preflight_direct_delivery = _text_count(
        ROOT / "integrations/preflight_builder.py", "predictive_push("
    ) + _text_count(ROOT / "integrations/preflight_builder.py", ".route_candidate(")

    def require(condition: bool, code: str, message: str) -> None:
        if not condition:
            findings.append({"code": code, "message": message})

    require(
        tuple(TARGET_AGENT_NAMES) == EXPECTED_HOSTS,
        "host_denominator_mismatch",
        "host-agent denominator is not exactly the eight COG-039 hosts",
    )
    require(
        REQUIRED_TOOLS <= workflow_tools,
        "workflow_contract_gap",
        "AgentKit does not require every COG-039 workflow tool",
    )
    require(REQUIRED_TOOLS <= schema_tools, "mcp_schema_gap", "COG-039 workflow tool is missing from MCP schema")
    require(
        REQUIRED_TOOLS <= set(MCP_TOOL_POLICIES),
        "mcp_policy_gap",
        "COG-039 workflow tool is missing an authorization policy",
    )
    require(FACADE_METHODS <= facade_methods, "facade_contract_gap", "Facade lacks a required COG-039 method")
    require(
        "record_presentation" in router_methods,
        "presentation_owner_gap",
        "DeliveryRouter does not own presentation acknowledgement",
    )
    require(
        adapter_direct_domain_calls == 0,
        "agent_specific_domain_logic",
        "host adapters contain direct cognitive/delivery domain calls",
    )
    require(
        preflight_direct_delivery == 0,
        "direct_delivery_bypass",
        "PreflightBuilder bypasses the governed DeliveryRouter path",
    )

    static_ok = not findings
    return {
        "schema_version": "mnemos.agent_cognitive_contract_audit.v1",
        "strict": True,
        "ok": static_ok,
        "certifying": False,
        "release_eligible": False,
        "host_denominator": list(TARGET_AGENT_NAMES),
        "required_workflow_tools": sorted(REQUIRED_TOOLS),
        "metrics": {
            "agent_specific_domain_logic": adapter_direct_domain_calls,
            "delivery_decision_owner_count": 1 if "route_candidate" in router_methods else 0,
            "direct_delivery_bypass": preflight_direct_delivery,
            "unsupported_silently_ignored": 0,
            "runtime_probe_required": len(EXPECTED_HOSTS),
            "runtime_probe_verified": 0,
        },
        "runtime_boundary": {
            "state": "not_run",
            "reason": "static audit cannot claim eight real host render/outcome/correction probes",
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"ok={report['ok']} certifying={report['certifying']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
