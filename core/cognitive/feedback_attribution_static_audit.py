"""Independent AST audit for canonical feedback ownership and readers."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any

from core.utils import read_text_value


def audit_feedback_static(repo_root: Path) -> dict[str, Any]:
    """Audit formal feedback seams and direct owner or barrier bypasses."""

    files = sorted(
        path
        for root in ("core", "integrations", "daemon")
        for path in (repo_root / root).rglob("*.py")
    )
    cli = repo_root / "mnemos_cli.py"
    if cli.is_file():
        files.append(cli)
    owner_bypasses: list[str] = []
    guarded: dict[tuple[str, str], tuple[str, ...]] = {
        ("core/cognitive/feedback_attribution.py", "record_reaction"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_attribution.py", "record_objective_outcome"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_attribution.py", "process_command"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_attribution.py", "reconcile_subject"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_attribution.py", "correct_reaction"): ("record_reaction",),
        ("core/cognitive/feedback_attribution.py", "replay_pending"): ("process_command",),
        ("core/cognitive/feedback_domain_proposal.py", "apply"): (
            "assert_feedback_writes_enabled",
            "proposal_gate_factory",
            "validate",
            "record_committed",
        ),
        ("core/cognitive/feedback_domain_proposal.py", "neutralize"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_predictive_feedback"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_context_search_feedback"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_reflection_feedback"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_recap_feedback"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_dialog_decision_feedback"): (
            "_record_displayed_object_feedback",
        ),
        ("core/cognitive/feedback_entrypoints.py", "record_dialog_reminder_feedback"): (
            "_record_displayed_object_feedback",
        ),
        ("core/cognitive/feedback_entrypoints.py", "_record_displayed_object_feedback"): (
            "assert_feedback_writes_enabled",
        ),
        ("core/cognitive/feedback_proposal_gate.py", "__init__"): (
            "PushDecisionGate",
            "evaluate",
            "_authorize",
        ),
        ("core/cognitive/feedback_proposal_gate.py", "_authorize"): (
            "authorize_exact_project_contract_action",
        ),
        ("core/cognitive/feedback_proposal_gate.py", "record_committed"): ("record_terminal",),
    }
    seen_guards: set[tuple[str, str]] = set()
    legacy_sql = re.compile(
        r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+"
        r"(?:feedback_events|feedback_receipts|feedback_signals|cognitive_outcomes)\b",
        re.IGNORECASE,
    )
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        source = read_text_value(path)
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            owner_bypasses.append(f"{relative}:{exc.lineno}:syntax_error")
            continue
        restricted_receipt_calls = {
            "_record_feedback_effect_receipt": {
                ("core/cognitive/feedback_attribution.py", "_process_command_once"),
                (
                    "core/cognitive/feedback_attribution.py",
                    "_process_neutralization_command",
                ),
            },
            "_record_feedback_ineligible_receipt": {
                ("core/cognitive/feedback_attribution.py", "_process_command_once"),
            },
            "_record_feedback_terminal_failure": {
                (
                    "core/cognitive/feedback_command_failure.py",
                    "_record_permanent_feedback_failure",
                ),
            },
            "_record_effect_receipt": {
                ("core/cognitive/state_effect_receipts.py", "record_effect_receipt"),
                (
                    "core/cognitive/state_effect_receipts.py",
                    "record_cognition_episode_projection_receipt",
                ),
                (
                    "core/cognitive/state_effect_receipts.py",
                    "record_cognition_episode_omission_receipt",
                ),
                (
                    "core/cognitive/state_effect_receipts.py",
                    "_record_feedback_effect_receipt",
                ),
                (
                    "core/cognitive/state_effect_receipts.py",
                    "_record_feedback_ineligible_receipt",
                ),
                (
                    "core/cognitive/state_effect_receipts.py",
                    "_record_feedback_terminal_failure",
                ),
            },
            "_start_feedback_command_attempt": {
                ("core/cognitive/feedback_attribution.py", "_process_command_once"),
                (
                    "core/cognitive/feedback_attribution.py",
                    "_process_neutralization_command",
                ),
            },
            "_record_permanent_feedback_failure": {
                (
                    "core/cognitive/feedback_attribution.py",
                    "_record_permanent_failure",
                ),
            },
            "invoke_target_adapter": {
                ("core/cognitive/feedback_attribution.py", "_process_command_once"),
                (
                    "core/cognitive/feedback_attribution.py",
                    "_process_neutralization_command",
                ),
            },
            "_bind_feedback_owner_capability": {
                ("core/cognitive/feedback_attribution.py", "__init__"),
            },
            "_feedback_terminal_capability_matches": {
                (
                    "core/cognitive/feedback_owner_identity.py",
                    "feedback_failure_context_matches_state",
                ),
                (
                    "core/cognitive/feedback_attribution.py",
                    "_feedback_failure_context_matches",
                ),
            },
        }
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _executable_nodes(function):
                if not isinstance(call, ast.Call):
                    continue
                name = _call_name(call)
                allowed = restricted_receipt_calls.get(name)
                if allowed is not None and (relative, function.name) not in allowed:
                    owner_bypasses.append(f"{relative}:{call.lineno}:{name}:restricted_callsite")
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "set"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "_ACTIVE_FEEDBACK_FAILURE_CONTEXT"
                    and (relative, function.name)
                    != ("core/cognitive/feedback_attribution.py", "process_command")
                ):
                    owner_bypasses.append(
                        f"{relative}:{call.lineno}:feedback_context_set:restricted_callsite"
                    )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (relative, node.name)
                expected = guarded.get(key)
                call_names = {
                    _call_name(call) for call in ast.walk(node) if isinstance(call, ast.Call)
                }
                if expected and all(name in call_names for name in expected):
                    seen_guards.add(key)
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in {
                "FeedbackSignalRouter",
                "FeedbackEventLedger",
                "OutcomeRecorder",
            } and relative not in {
                "core/cognitive/feedback_signal_router.py",
                "core/cognitive/feedback_event.py",
                "core/app/outcome_recorder.py",
            }:
                owner_bypasses.append(f"{relative}:{node.lineno}:{name}")
            if name == "record_feedback_signal" and relative != "core/scoring/feedback_channel.py":
                owner_bypasses.append(f"{relative}:{node.lineno}:{name}")
            if name == "submit_feedback" and relative in {
                "core/reflection/reflection_engine.py",
                "core/reflection/feedback_collector.py",
            }:
                owner_bypasses.append(f"{relative}:{node.lineno}:legacy_submit_feedback_call")
            if (
                relative == "core/cognitive/feedback_attribution.py"
                and name == "record_effect_receipt"
            ):
                owner_bypasses.append(f"{relative}:{node.lineno}:generic_feedback_receipt_bypass")
        if relative not in {
            "core/cognitive/feedback_history_migration.py",
        }:
            for match in legacy_sql.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                owner_bypasses.append(f"{relative}:{line}:legacy_feedback_sql_write")
    barrier_bypasses = [
        f"{path}:{name}:barrier_guard_missing"
        for (path, name) in guarded
        if (path, name) not in seen_guards
    ]
    state_path = repo_root / "core/cognitive/state_effect_receipts.py"
    state_source = read_text_value(state_path) if state_path.is_file() else ""
    try:
        state_tree = ast.parse(state_source, filename=str(state_path))
    except SyntaxError:
        owner_bypasses.append("core/cognitive/state_effect_receipts.py:syntax_error")
    else:
        verification_functions = [
            node
            for node in ast.walk(state_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_record_feedback_effect_receipt"
        ]
        verification_calls = (
            {
                _call_name(call)
                for call in _executable_nodes(verification_functions[0])
                if isinstance(call, ast.Call)
            }
            if len(verification_functions) == 1
            else set()
        )
        verification_args = (
            {
                arg.arg
                for arg in (
                    *verification_functions[0].args.args,
                    *verification_functions[0].args.kwonlyargs,
                )
            }
            if len(verification_functions) == 1
            else set()
        )
        if (
            len(verification_functions) != 1
            or "adapter" in verification_args
            or not {
                "_feedback_target_verifier",
                "verify_command_effect",
            }
            <= verification_calls
        ):
            owner_bypasses.append(
                "core/cognitive/state_effect_receipts.py:" "feedback_receipt_self_signable"
            )
    formal_callsites = {
        ("core/application/decision_inbox.py", "_act_delivery"): ("record_predictive_feedback",),
        ("core/application/intelligence.py", "push_feedback"): ("record_predictive_feedback",),
        ("core/app/context_search_feedback.py", "record_search_click"): (
            "record_context_search_feedback",
        ),
        ("core/app/context_search_feedback.py", "record_search_ignore"): (
            "record_context_search_feedback",
        ),
        ("core/application/reflection.py", "reflection_feedback"): ("record_reflection_feedback",),
        ("core/application/recap_feedback_service.py", "route_authenticated_recap_feedback"): (
            "record_recap_feedback",
        ),
        ("core/trust/dialog_push.py", "decide"): ("record_dialog_decision_feedback",),
        ("core/kia/dialog_reminder.py", "record_user_response"): (
            "record_dialog_reminder_feedback",
        ),
        ("core/cli/commands/proposal.py", "_run_decision"): (
            "local_cli_identity",
            "decide",
        ),
        ("core/cli/commands/reminder.py", "cmd_reminder"): (
            "local_cli_identity",
            "record_user_response",
        ),
        ("core/app/outcome_recorder.py", "record_reaction"): ("record_reaction",),
        ("core/app/outcome_recorder.py", "record_objective_outcome"): ("record_objective_outcome",),
    }
    formal_user_seam_bypasses: list[str] = []
    formal_covered = 0
    for (relative, function_name), required_calls in formal_callsites.items():
        source_path = repo_root / relative
        source = read_text_value(source_path) if source_path.is_file() else ""
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            tree = ast.Module(body=[], type_ignores=[])
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ]
        calls = (
            [call for call in _executable_nodes(functions[0]) if isinstance(call, ast.Call)]
            if len(functions) == 1
            else []
        )
        call_names = {_call_name(call) for call in calls if call.args or call.keywords}
        covered = len(functions) == 1 and set(required_calls) <= call_names
        if covered:
            formal_covered += 1
        else:
            formal_user_seam_bypasses.append(
                f"{relative}:{function_name}:missing:{','.join(required_calls)}"
            )
    context_search_path = repo_root / "core/app/context_search.py"
    context_search_source = (
        read_text_value(context_search_path) if context_search_path.is_file() else ""
    )
    try:
        context_search_tree = ast.parse(
            context_search_source,
            filename="core/app/context_search.py",
        )
    except SyntaxError:
        context_search_tree = ast.Module(body=[], type_ignores=[])
    mixin_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "core.app.context_search_feedback"
        and any(alias.name == "ContextSearchFeedbackMixin" for alias in node.names)
        for node in context_search_tree.body
    )
    mixin_bound = any(
        isinstance(node, ast.ClassDef)
        and node.name == "ContextAwareSearch"
        and any(
            isinstance(base, ast.Name) and base.id == "ContextSearchFeedbackMixin"
            for base in node.bases
        )
        for node in context_search_tree.body
    )
    if not mixin_imported or not mixin_bound:
        formal_user_seam_bypasses.append(
            "core/app/context_search.py:ContextAwareSearch:missing:" "ContextSearchFeedbackMixin"
        )
    recap_router = repo_root / "core/app/retrospective_consumption_router.py"
    if recap_router.is_file() and "apply_external_proposal" in read_text_value(recap_router):
        formal_user_seam_bypasses.append(
            "core/app/retrospective_consumption_router.py:recap_skip_direct_proposal"
        )
    owner_path = repo_root / "core/cognitive/feedback_attribution.py"
    owner_source = read_text_value(owner_path) if owner_path.is_file() else ""
    try:
        owner_tree = ast.parse(owner_source, filename=str(owner_path))
    except SyntaxError:
        owner_bypasses.append("core/cognitive/feedback_attribution.py:syntax_error")
    else:
        for function_name, effect_call in (
            ("_process_command_once", "invoke_target_adapter"),
            ("_process_neutralization_command", "invoke_target_adapter"),
        ):
            functions = [
                node
                for node in ast.walk(owner_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function_name
            ]
            calls = (
                [node for node in _executable_nodes(functions[0]) if isinstance(node, ast.Call)]
                if len(functions) == 1
                else []
            )
            started = [
                node.lineno
                for node in calls
                if _call_name(node) == "_start_feedback_command_attempt"
            ]
            effects = [node.lineno for node in calls if _call_name(node) == effect_call]
            if not started or not effects or min(started) >= min(effects):
                owner_bypasses.append(
                    "core/cognitive/feedback_attribution.py:"
                    f"{function_name}:attempt_not_before_{effect_call}"
                )
    legacy_active_readers = _historical_active_reader_gaps(repo_root)
    return {
        "python_file_count": len(files),
        "owner_bypasses": sorted(set(owner_bypasses)),
        "barrier_bypasses": sorted(barrier_bypasses),
        "formal_user_seam_bypasses": sorted(set(formal_user_seam_bypasses)),
        "legacy_active_readers": sorted(set(legacy_active_readers)),
        "formal_user_entrypoint_expected_count": len(formal_callsites),
        "formal_user_entrypoint_covered_count": formal_covered,
    }


def _historical_active_reader_gaps(repo_root: Path) -> list[str]:
    """Inspect active scorer/reflection readers structurally, not by token count."""

    gaps: list[str] = []
    scorer_path = repo_root / "core/scoring/adaptive_scorer_v2.py"
    scorer_source = read_text_value(scorer_path) if scorer_path.is_file() else ""
    try:
        scorer_tree = ast.parse(scorer_source, filename=str(scorer_path))
    except SyntaxError:
        return ["core/scoring/adaptive_scorer_v2.py:syntax_error"]
    active_readers = {
        "_get_training_samples": {
            "SCORER_TRAINING_QUEUE": "_QUARANTINED_FEEDBACK_QUEUE_SQL",
            "GROUND_TRUTH_SIGNALS": "_QUARANTINED_FEEDBACK_GROUND_TRUTH_SQL",
        },
        "_normalize_pending_queue_dimensions": {
            "SCORER_TRAINING_QUEUE": "_QUARANTINED_FEEDBACK_QUEUE_SQL",
        },
        "refresh_bayesian_priors_from_ground_truth": {
            "GROUND_TRUTH_SIGNALS": "_QUARANTINED_FEEDBACK_GROUND_TRUTH_SQL",
        },
        "_count_ready_samples": {
            "SCORER_TRAINING_QUEUE": "_QUARANTINED_FEEDBACK_QUEUE_SQL",
        },
        "_count_signal_samples": {
            "SCORER_TRAINING_QUEUE": "_QUARANTINED_FEEDBACK_QUEUE_SQL",
            "GROUND_TRUTH_SIGNALS": "_QUARANTINED_FEEDBACK_GROUND_TRUTH_SQL",
        },
    }
    for function in ast.walk(scorer_tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        requirements = active_readers.get(function.name)
        if requirements is None:
            continue
        query_facts = _executed_query_facts(function)
        for table, required_name in requirements.items():
            table_queries = [
                names for sql, names, _line in query_facts if table in sql and "SELECT" in sql
            ]
            if table_queries and any(required_name not in names for names in table_queries):
                gaps.append(
                    "core/scoring/adaptive_scorer_v2.py:"
                    f"{function.lineno}:{function.name}:"
                    f"unfiltered_{table.lower()}_reader"
                )
    analytics_path = repo_root / "core/reflection/feedback_analytics.py"
    analytics_source = read_text_value(analytics_path) if analytics_path.is_file() else ""
    try:
        analytics_tree = ast.parse(analytics_source, filename=str(analytics_path))
    except SyntaxError:
        return [*gaps, "core/reflection/feedback_analytics.py:syntax_error"]
    readers = [
        node
        for node in ast.walk(analytics_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_get_records_with_any_feedback"
    ]
    if (
        len(readers) != 1
        or any(isinstance(node, ast.Call) for node in ast.walk(readers[0]))
        or not any(
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.List)
            and not node.value.elts
            for node in ast.walk(readers[0])
        )
    ):
        gaps.append("core/reflection/feedback_analytics.py:legacy_feedback_reader_active")
    rule_path = repo_root / "core/kia/rule_scorer.py"
    rule_source = read_text_value(rule_path) if rule_path.is_file() else ""
    try:
        rule_tree = ast.parse(rule_source, filename=str(rule_path))
    except SyntaxError:
        gaps.append("core/kia/rule_scorer.py:syntax_error")
    else:
        rule_readers = {
            "get_rule_accuracy",
            "get_total_samples",
            "get_recent_outcomes",
            "get_stats",
        }
        found_rule_readers: set[str] = set()
        for function in ast.walk(rule_tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if function.name not in rule_readers:
                continue
            found_rule_readers.add(function.name)
            query_facts = _executed_query_facts(function)
            outcome_queries = [
                names
                for sql, names, _line in query_facts
                if "RULE_OUTCOMES" in sql and "SELECT" in sql
            ]
            retired = any(
                isinstance(node, ast.Call) and _call_name(node) == "_retired"
                for node in _executable_nodes(function)
            )
            if (
                outcome_queries
                and any("ACTIVE_RULE_OUTCOME_SQL" not in names for names in outcome_queries)
            ) or (not outcome_queries and not retired):
                gaps.append(
                    "core/kia/rule_scorer.py:"
                    f"{function.lineno}:{function.name}:unfiltered_rule_outcomes"
                )
        for missing in sorted(rule_readers - found_rule_readers):
            gaps.append(f"core/kia/rule_scorer.py:{missing}:missing_active_reader")
    reflection_path = repo_root / "core/reflection/reflection_store.py"
    reflection_source = read_text_value(reflection_path) if reflection_path.is_file() else ""
    try:
        reflection_tree = ast.parse(
            reflection_source,
            filename=str(reflection_path),
        )
    except SyntaxError:
        gaps.append("core/reflection/reflection_store.py:syntax_error")
    else:
        reflection_readers = {
            "get_experiences": "ACTIVE_LAYER5_EXPERIENCE_SQL",
            "authorized_get_experiences": "ACTIVE_LAYER5_EXPERIENCE_SQL",
            "get_shifts": "ACTIVE_COGNITIVE_SHIFT_SQL",
            "authorized_get_shifts": "ACTIVE_COGNITIVE_SHIFT_SQL",
        }
        found_reflection_readers: set[str] = set()
        for function in ast.walk(reflection_tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            required = reflection_readers.get(function.name)
            if required is None:
                continue
            found_reflection_readers.add(function.name)
            query_facts = _executed_query_facts(function)
            table = (
                "LAYER5_EXPERIENCES"
                if required == "ACTIVE_LAYER5_EXPERIENCE_SQL"
                else "COGNITIVE_SHIFTS"
            )
            history_queries = [
                names for sql, names, _line in query_facts if table in sql and "SELECT" in sql
            ]
            if not history_queries or not any(required in names for names in history_queries):
                gaps.append(
                    "core/reflection/reflection_store.py:"
                    f"{function.lineno}:{function.name}:unfiltered_feedback_history"
                )
        for missing in sorted(set(reflection_readers) - found_reflection_readers):
            gaps.append("core/reflection/reflection_store.py:" f"{missing}:missing_active_reader")
    daemon_path = repo_root / "daemon/adaptive_service.py"
    daemon_source = read_text_value(daemon_path) if daemon_path.is_file() else ""
    try:
        daemon_tree = ast.parse(daemon_source, filename=str(daemon_path))
    except SyntaxError:
        gaps.append("daemon/adaptive_service.py:syntax_error")
    else:
        collectors = [
            node
            for node in ast.walk(daemon_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "collect_metrics"
        ]
        metric_calls = (
            [
                node
                for node in _executable_nodes(collectors[0])
                if isinstance(node, ast.Call)
                and _call_name(node) == "_record_single_metric"
                and any(
                    keyword.arg == "metric"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "feedback_rate"
                    for keyword in node.keywords
                )
            ]
            if len(collectors) == 1
            else []
        )
        metric_names: set[str] = set()
        metric_sql = ""
        if len(metric_calls) == 1:
            queries = next(
                (keyword.value for keyword in metric_calls[0].keywords if keyword.arg == "queries"),
                None,
            )
            if queries is not None:
                metric_sql, metric_names = _expression_facts(queries, {})
        legacy_guarded = (
            "SCORER_TRAINING_QUEUE" in metric_sql
            and "_QUARANTINED_FEEDBACK_QUEUE_SQL" in metric_names
        )
        governed_guarded = all(
            fragment in metric_sql
            for fragment in (
                "FROM GOVERNED_TRAINING_SAMPLES AS S",
                "FROM GOVERNED_TRAINING_SAMPLE_ACTIONS AS A",
                "A.SAMPLE_ID=S.SAMPLE_ID",
                ")='ADMIT'",
            )
        )
        if len(metric_calls) != 1 or not (legacy_guarded or governed_guarded):
            gaps.append(
                "daemon/adaptive_service.py:collect_metrics:" "unfiltered_training_feedback_rate"
            )
    return gaps


def _call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _executable_nodes(root: ast.AST):
    """Yield reachable AST nodes while excluding nested/dormant decoys."""

    yield root
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)):
        yield from _executable_block(root.body)
        return
    if isinstance(root, ast.If):
        known, value = _constant_value(root.test)
        if known:
            yield from _executable_block(root.body if bool(value) else root.orelse)
            return
    for child in ast.iter_child_nodes(root):
        if child is not root and isinstance(
            child,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
        ):
            continue
        yield from _executable_nodes(child)


def _executable_block(statements: list[ast.stmt]):
    for statement in statements:
        yield from _executable_nodes(statement)
        if _statement_always_terminates(statement):
            break


def _statement_always_terminates(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(statement, ast.If):
        known, value = _constant_value(statement.test)
        if known:
            branch = statement.body if bool(value) else statement.orelse
            return _block_always_terminates(branch)
        return bool(statement.body and statement.orelse) and (
            _block_always_terminates(statement.body) and _block_always_terminates(statement.orelse)
        )
    return False


def _block_always_terminates(statements: list[ast.stmt]) -> bool:
    return any(_statement_always_terminates(statement) for statement in statements)


def _constant_value(node: ast.AST) -> tuple[bool, Any]:
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, ast.UnaryOp):
        known, value = _constant_value(node.operand)
        if not known:
            return False, None
        if isinstance(node.op, ast.Not):
            return True, not value
        if isinstance(node.op, ast.UAdd):
            return True, +value
        if isinstance(node.op, ast.USub):
            return True, -value
    if isinstance(node, ast.BoolOp):
        values: list[Any] = []
        for item in node.values:
            known, value = _constant_value(item)
            if not known:
                return False, None
            values.append(value)
        if isinstance(node.op, ast.And):
            result = values[0]
            for value in values[1:]:
                if not result:
                    break
                result = value
            return True, result
        result = values[0]
        for value in values[1:]:
            if result:
                break
            result = value
        return True, result
    if isinstance(node, ast.Compare):
        known, left = _constant_value(node.left)
        if not known:
            return False, None
        for operator, comparator in zip(node.ops, node.comparators):
            known, right = _constant_value(comparator)
            if not known:
                return False, None
            if isinstance(operator, ast.Eq):
                matched = left == right
            elif isinstance(operator, ast.NotEq):
                matched = left != right
            elif isinstance(operator, ast.Lt):
                matched = left < right
            elif isinstance(operator, ast.LtE):
                matched = left <= right
            elif isinstance(operator, ast.Gt):
                matched = left > right
            elif isinstance(operator, ast.GtE):
                matched = left >= right
            elif isinstance(operator, ast.Is):
                matched = left is right
            elif isinstance(operator, ast.IsNot):
                matched = left is not right
            else:
                return False, None
            if not matched:
                return True, False
            left = right
        return True, True
    return False, None


def _executed_query_facts(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, set[str], int]]:
    nodes = tuple(_executable_nodes(function))
    assignments: dict[str, ast.AST] = {}
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    assignments[target.id] = value
    facts: list[tuple[str, set[str], int]] = []
    for node in nodes:
        if isinstance(node, ast.Call) and _call_name(node) == "execute" and node.args:
            sql, names = _expression_facts(node.args[0], assignments)
            facts.append((sql, names, node.lineno))
    return facts


def _expression_facts(
    expression: ast.AST,
    assignments: dict[str, ast.AST],
    seen: frozenset[str] = frozenset(),
) -> tuple[str, set[str]]:
    strings: list[str] = []
    names: set[str] = set()

    def visit(node: ast.AST, active: frozenset[str]) -> None:
        if isinstance(node, ast.IfExp):
            known, value = _constant_value(node.test)
            visit(node.test, active)
            if known:
                visit(node.body if bool(value) else node.orelse, active)
            else:
                _body_sql, body_names = _expression_facts(
                    node.body,
                    assignments,
                    active,
                )
                _else_sql, else_names = _expression_facts(
                    node.orelse,
                    assignments,
                    active,
                )
                names.update(body_names & else_names)
            return
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                visit(value, active)
                known, constant = _constant_value(value)
                if not known:
                    break
                if isinstance(node.op, ast.And) and not bool(constant):
                    break
                if isinstance(node.op, ast.Or) and bool(constant):
                    break
            return
        if isinstance(node, ast.Name):
            names.add(node.id)
            if node.id in assignments and node.id not in active:
                visit(assignments[node.id], active | {node.id})
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value.upper())
        for child in ast.iter_child_nodes(node):
            visit(child, active)

    visit(expression, seen)
    return " ".join(strings), names
