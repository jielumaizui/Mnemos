#!/usr/bin/env python3
"""
Wiki Lint - Karpathy 风格健康扫描脚本

扫描 wiki/ 目录，检测知识健康问题：
1. 孤立页面（orphan）：无入链也无出链
2. 过短页面（stub）：内容 < 200 字符
3. 缺 frontmatter 的页面
4. 坏链接（broken link）：[[xxx]] 指向不存在的页面
5. 过旧页面（stale）：mtime > 30 天
6. 缺元数据（missing meta）：无 status / source_count / knowledge_stage
7. 未引用来源（no sources）：source_count == 0 或 sources 为空

用法:
  python3 scripts/wiki_lint.py           # 扫描并报告
  python3 scripts/wiki_lint.py --fix     # 自动修复简单问题
  python3 scripts/wiki_lint.py --json    # 输出 JSON 报告
"""

import sys
import re
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, List, Dict, Mapping, Set, Tuple, Optional
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config  # noqa: E402
from core.frontmatter import (  # noqa: E402
    fm_get,
    to_chinese_frontmatter_preserving_unknown,
)
from core.system_contracts import (  # noqa: E402
    ActionLedger,
    make_action_record,
)
from core.cognitive.state_contract import sha256_json  # noqa: E402
from core.ops.action_ledger import (  # noqa: E402
    authorize_primary_action_ledger_record,
)

WIKI_DIR = get_config().wiki_dir
WIKI_QUALITY_SCHEMA_VERSION = "mnemos.wiki_quality.v1"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# 健康阈值
STUB_THRESHOLD = 200  # 字符数，低于此值为 stub
STALE_DAYS = 30  # 超过此天数为陈旧

WIKI_ISSUE_POLICIES: Dict[str, Dict[str, Any]] = {
    "no_frontmatter": {
        "owner": "wiki_builder",
        "failure_class": "schema",
        "local_status": "blocked_manual",
        "lifecycle_status": "needs_user",
        "auto_fixable": False,
        "budget": 0,
        "strategy": "重建或人工补齐 frontmatter；不要在未知来源页面上盲写元数据",
        "repair_action": "重建 Wiki 后复跑 `python3 scripts/wiki_lint.py --summary --json --budget`",
    },
    "missing_meta": {
        "owner": "wiki_builder",
        "failure_class": "schema",
        "local_status": "auto_fixable",
        "lifecycle_status": "processing",
        "auto_fixable": True,
        "budget": 0,
        "strategy": "用 `--fix` 补齐 status/source_count/knowledge_stage/evidence_level，并写 Action Ledger",
        "repair_action": "python3 scripts/wiki_lint.py --fix --summary --json --budget",
    },
    "orphan": {
        "owner": "knowledge_architect",
        "failure_class": "quality",
        "local_status": "manual_review",
        "lifecycle_status": "needs_user",
        "auto_fixable": False,
        "budget": 0,
        "strategy": "生成人工确认清单，区分真实孤岛、缺入口 MOC、缺入链页面",
        "repair_action": "补入口页/MOC 或归档确认无价值页面",
    },
    "broken_link": {
        "owner": "knowledge_architect",
        "failure_class": "schema",
        "local_status": "blocked_manual",
        "lifecycle_status": "needs_user",
        "auto_fixable": False,
        "budget": 0,
        "strategy": "人工确认目标页是否重命名、未投影或链接拼写错误",
        "repair_action": "修正链接或重建缺失目标页",
    },
    "stub": {
        "owner": "content_reviewer",
        "failure_class": "quality",
        "local_status": "manual_review",
        "lifecycle_status": "needs_user",
        "auto_fixable": False,
        "budget": 0,
        "strategy": "扩写到最小可读内容，或确认其为索引/占位页后加入豁免预算",
        "repair_action": "扩写页面正文或在预算文件中给出 owner 与豁免理由",
    },
    "stale": {
        "owner": "content_reviewer",
        "failure_class": "quality",
        "local_status": "within_budget",
        "lifecycle_status": "degraded",
        "auto_fixable": False,
        "budget": 500,
        "strategy": "陈旧页不直接阻断发布，但必须有刷新预算和抽样复核计划",
        "repair_action": "按最近使用、搜索命中和入链优先级刷新",
    },
}


def extract_frontmatter(content: str) -> Tuple[Optional[Dict], str]:
    """提取 frontmatter 和正文"""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return None, content
    try:
        import yaml

        return yaml.safe_load(match.group(1)) or {}, content[match.end() :]
    except ImportError:
        return None, content


def extract_wiki_links(content: str) -> Set[str]:
    """提取 [[...]] 链接的目标页面名"""
    links = set()
    for match in WIKI_LINK_RE.finditer(content):
        link = match.group(1)
        page_name = link.split("|")[0].strip()
        links.add(page_name)
    return links


def scan_all_pages() -> List[Dict]:
    """扫描所有 wiki 页面"""
    pages: List[Dict] = []
    if not WIKI_DIR.exists():
        print(f"[Lint] Wiki 目录不存在: {WIKI_DIR}")
        return pages

    # 收集所有 markdown 文件
    md_files = list(WIKI_DIR.rglob("*.md"))
    # 排除隐藏目录
    md_files = [
        f for f in md_files if not any(p.startswith(".") for p in f.relative_to(WIKI_DIR).parts)
    ]

    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        frontmatter, body = extract_frontmatter(content)
        links = extract_wiki_links(content)
        stat = md_file.stat()

        pages.append(
            {
                "path": str(md_file),
                "rel_path": str(md_file.relative_to(WIKI_DIR)),
                "name": md_file.stem,
                "frontmatter": frontmatter,
                "body": body,
                "content": content,
                "links": links,
                "size": len(content),
                "body_size": len(body.strip()),
                "mtime": datetime.fromtimestamp(stat.st_mtime),
                "line_count": content.count("\n") + 1,
            }
        )

    return pages


def build_page_index(pages: List[Dict]) -> Dict[str, Dict]:
    """建立页面名到页面的索引"""
    index = {}
    for p in pages:
        index[p["name"]] = p
        # 也按相对路径索引（用于链接解析）
        index[p["rel_path"].removesuffix(".md")] = p
    return index


def check_orphan(
    page: Dict,
    all_pages: List[Dict],
    page_index: Dict[str, Dict],
    incoming_pages: Set[str] | None = None,
) -> Tuple[bool, str]:
    """检查是否为孤立页面"""
    # 入链：谁引用了这个页面
    if incoming_pages is not None:
        has_incoming = page["rel_path"] in incoming_pages
    else:
        has_incoming = False
        name = page["name"]
        for p in all_pages:
            if name in p["links"]:
                has_incoming = True
                break

    # 出链：这个页面引用了谁
    has_outgoing = len(page["links"]) > 0

    if not has_incoming and not has_outgoing:
        return True, "无入链也无出链"
    if not has_incoming:
        return True, "无入链（无人引用）"
    return False, ""


def check_broken_links(
    page: Dict,
    page_index: Dict[str, Dict],
    target_aliases: Set[str] | None = None,
) -> List[str]:
    """检查坏链接"""
    from core.vaults.link_audit import canonical_wiki_target_key

    broken = []
    for link in page["links"]:
        if target_aliases is not None and canonical_wiki_target_key(link) in target_aliases:
            continue
        # 支持多种路径格式
        possible_keys = [
            link,
            link.replace(" ", "_"),
            f"concepts/{link}",
            f"entities/{link}",
            f"sources/{link}",
        ]
        if not any(k in page_index for k in possible_keys):
            broken.append(link)
    return broken


def check_missing_meta(page: Dict) -> List[str]:
    """检查缺失的元数据字段"""
    missing = []
    fm = page["frontmatter"]
    if fm is None:
        missing.append("缺少 frontmatter")
        return missing

    required_fields = ["status", "source_count", "knowledge_stage", "evidence_level"]
    for field in required_fields:
        if fm_get(fm, field) is None:
            missing.append(f"缺少 {field}")

    # source_count 检查
    if fm_get(fm, "source_count", 0) == 0:
        sources = fm.get("sources", fm.get("来源", []))
        if not sources:
            missing.append("source_count 为 0 且无 sources")

    return missing


def lint_page(
    page: Dict,
    all_pages: List[Dict],
    page_index: Dict[str, Dict],
    stale_days: int = 30,
    stub_threshold: int = 200,
    target_aliases: Set[str] | None = None,
    incoming_pages: Set[str] | None = None,
) -> Dict:
    """对单个页面执行 lint 检查"""
    issues = []
    severity = "ok"  # ok / warning / error

    # 1. 缺少 frontmatter
    if page["frontmatter"] is None:
        issues.append({"type": "no_frontmatter", "msg": "缺少 YAML frontmatter"})
        severity = "error"
    else:
        # 2. 缺元数据
        missing_meta = check_missing_meta(page)
        for m in missing_meta:
            issues.append({"type": "missing_meta", "msg": m})
            if severity == "ok":
                severity = "warning"

    # 3. 过短页面
    if page["body_size"] < stub_threshold:
        issues.append(
            {"type": "stub", "msg": f"内容过短（{page['body_size']} 字符，阈值 {stub_threshold}）"}
        )
        if severity == "ok":
            severity = "warning"

    # 4. 孤立页面
    is_orphan, orphan_reason = check_orphan(page, all_pages, page_index, incoming_pages)
    if is_orphan:
        issues.append({"type": "orphan", "msg": orphan_reason})
        if severity == "ok":
            severity = "warning"

    # 5. 坏链接
    broken = check_broken_links(page, page_index, target_aliases)
    for b in broken:
        issues.append({"type": "broken_link", "msg": f"坏链接: [[{b}]]"})
        severity = "error"

    # 6. 过旧页面
    age_days = (datetime.now() - page["mtime"]).days
    if age_days > stale_days:
        issues.append({"type": "stale", "msg": f"过旧（{age_days} 天未更新，阈值 {stale_days}）"})
        if severity == "ok":
            severity = "warning"

    return {
        "page": page["rel_path"],
        "severity": severity,
        "issues": issues,
        "age_days": age_days,
        "body_size": page["body_size"],
    }


def _summarize_severity(results: List[Dict]) -> Tuple[int, int, int, int]:
    """汇总 severity 计数。"""
    total = len(results)
    errors = sum(1 for r in results if r["severity"] == "error")
    warnings = sum(1 for r in results if r["severity"] == "warning")
    ok = sum(1 for r in results if r["severity"] == "ok")
    return total, errors, warnings, ok


def _count_issues(results: List[Dict]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for result in results:
        for issue in result["issues"]:
            counts[str(issue["type"])] += 1
    return dict(counts)


def _load_budget_overrides(path: str | None) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("budget file must be a JSON object")

    raw_budgets = payload.get("budgets", payload)
    if not isinstance(raw_budgets, dict):
        raise ValueError("budget file budgets must be a JSON object")

    overrides: Dict[str, Dict[str, Any]] = {}
    for issue_type, raw_value in raw_budgets.items():
        if isinstance(raw_value, int):
            overrides[str(issue_type)] = {"limit": raw_value}
        elif isinstance(raw_value, dict):
            overrides[str(issue_type)] = dict(raw_value)
        else:
            raise ValueError(f"budget for {issue_type!r} must be int or object")
    return overrides


def _budget_lines(
    issue_counts: Mapping[str, int],
    budget_overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    budgets: List[Dict[str, Any]] = []
    overrides = budget_overrides or {}
    for issue_type, policy in sorted(WIKI_ISSUE_POLICIES.items()):
        override = dict(overrides.get(issue_type, {}))
        limit = int(override.get("limit", policy["budget"]))
        actual = int(issue_counts.get(issue_type, 0))
        waiver = str(override.get("waiver_reason", "") or "")
        budgets.append(
            {
                "issue_type": issue_type,
                "actual": actual,
                "limit": limit,
                "ok": actual <= limit or bool(waiver),
                "owner": str(override.get("owner", policy["owner"])),
                "strategy": str(override.get("strategy", policy["strategy"])),
                "waiver_reason": waiver,
                "lifecycle_status": policy["lifecycle_status"],
                "scorecard_dimension": "obsidian_experience",
            }
        )
    return budgets


def _manual_review_items(results: List[Dict], sample_limit: int) -> Dict[str, Any]:
    manual: Dict[str, Any] = {}
    manual_types = {
        issue_type
        for issue_type, policy in WIKI_ISSUE_POLICIES.items()
        if not policy["auto_fixable"]
    }
    for issue_type in sorted(manual_types):
        samples: List[Dict[str, str]] = []
        count = 0
        for result in results:
            for issue in result["issues"]:
                if issue["type"] != issue_type:
                    continue
                count += 1
                if len(samples) < sample_limit:
                    samples.append({"page": result["page"], "msg": issue["msg"]})
        if count:
            policy = WIKI_ISSUE_POLICIES[issue_type]
            manual[issue_type] = {
                "count": count,
                "owner": policy["owner"],
                "strategy": policy["strategy"],
                "samples": samples,
            }
    return manual


def _state_machine() -> Dict[str, Dict[str, Any]]:
    return {
        issue_type: {
            "local_status": policy["local_status"],
            "lifecycle_status": policy["lifecycle_status"],
            "failure_class": policy["failure_class"],
            "auto_fixable": policy["auto_fixable"],
            "owner": policy["owner"],
            "repair_action": policy["repair_action"],
        }
        for issue_type, policy in sorted(WIKI_ISSUE_POLICIES.items())
    }


def build_quality_report(
    results: List[Dict],
    *,
    vault_dir: Path | str = WIKI_DIR,
    stale_days: int = STALE_DAYS,
    stub_threshold: int = STUB_THRESHOLD,
    budget_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    sample_limit: int = 20,
    include_pages: bool = False,
    fix_report: Mapping[str, Any] | None = None,
    action_ledger_ref: str = "",
) -> Dict[str, Any]:
    """Build the stable mnemos.wiki_quality.v1 report."""
    total, errors, warnings, ok = _summarize_severity(results)
    issue_counts = _count_issues(results)
    budgets = _budget_lines(issue_counts, budget_overrides)
    budget_failures = [line for line in budgets if not line["ok"]]
    manual_review = _manual_review_items(results, sample_limit)
    status = "verified" if errors == 0 and not budget_failures else "needs_user"
    report: Dict[str, Any] = {
        "schema_version": WIKI_QUALITY_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(),
        "vault_dir": str(vault_dir),
        "thresholds": {
            "stale_days": stale_days,
            "stub_threshold": stub_threshold,
        },
        "summary": {
            "pages": total,
            "ok_pages": ok,
            "error_pages": errors,
            "warning_pages": warnings,
            "issue_counts": issue_counts,
            "status": status,
        },
        "state_machine": _state_machine(),
        "budgets": {
            "enabled": True,
            "ok": not budget_failures,
            "failures": budget_failures,
            "lines": budgets,
        },
        "manual_review": manual_review,
        "scorecard": {
            "dimension": "obsidian_experience",
            "schema_version": "mnemos.scorecard.v1",
            "status": status,
            "metrics": {
                "wiki_lint.errors": errors,
                "wiki_lint.warnings": warnings,
                "wiki_lint.budget_failures": len(budget_failures),
            },
        },
        "action_ledger_ref": action_ledger_ref,
    }
    if fix_report:
        report["auto_fix"] = dict(fix_report)
    if include_pages:
        report["pages"] = results
    return report


def format_quality_summary(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    budgets = report["budgets"]
    scorecard = report["scorecard"]
    lines = [
        "Wiki quality summary",
        "=" * 40,
        f"schema_version: {report['schema_version']}",
        f"vault_dir: {report['vault_dir']}",
        f"pages: {summary['pages']}",
        f"ok_pages: {summary['ok_pages']}",
        f"error_pages: {summary['error_pages']}",
        f"warning_pages: {summary['warning_pages']}",
        f"budget_ok: {budgets['ok']}",
        f"scorecard_status: {scorecard['status']}",
    ]
    issue_counts = summary.get("issue_counts") or {}
    if issue_counts:
        lines.extend(["", "[issue_counts]"])
        for issue_type, count in sorted(issue_counts.items(), key=lambda item: -item[1]):
            lines.append(f"{issue_type}: {count}")
    failures = budgets.get("failures") or []
    if failures:
        lines.extend(["", "[budget_failures]"])
        for item in failures:
            lines.append(
                f"{item['issue_type']}: actual={item['actual']} "
                f"limit={item['limit']} owner={item['owner']}"
            )
    manual_review = report.get("manual_review") or {}
    if manual_review:
        lines.extend(["", "[manual_review]"])
        for issue_type, item in manual_review.items():
            lines.append(f"{issue_type}: {item['count']} owner={item['owner']}")
    return "\n".join(lines)


def _render_issue_section(
    title: str,
    results: List[Dict],
    predicate,
    issue_filter=None,
) -> List[str]:
    """渲染单个 issue 区块（错误或警告）。"""
    lines: List[str] = []
    has_any = False
    for r in results:
        if not predicate(r):
            continue
        filtered = r["issues"]
        if issue_filter is not None:
            filtered = [issue for issue in filtered if issue_filter(issue)]
        if not filtered:
            continue
        if not has_any:
            lines.append(f"## {title}")
            lines.append("")
            has_any = True
        lines.append(f"- **{r['page']}**")
        for issue in filtered:
            lines.append(f"  - {issue['msg']}")
    if has_any:
        lines.append("")
    return lines


def _render_issue_counts(results: List[Dict]) -> List[str]:
    """渲染问题类型统计。"""
    issue_counts: Dict[str, int] = defaultdict(int)
    for r in results:
        for issue in r["issues"]:
            issue_counts[issue["type"]] += 1

    lines = ["---", "", "## 问题统计", ""]
    for issue_type, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- {issue_type}: {count}")
    return lines


def _render_recommendations(errors: int, warnings: int) -> List[str]:
    """渲染修复建议。"""
    lines = ["", "## 修复建议", ""]
    if errors > 0:
        lines.append("1. **优先修复错误**：补充 frontmatter、修复坏链接")
    if warnings > 0:
        lines.append("2. **处理警告**：扩充 stub 页面、建立页面间链接")
    lines.append("3. **定期运行**: `python3 scripts/wiki_lint.py`")
    return lines


def generate_report(results: List[Dict]) -> str:
    """生成 human-readable 报告"""
    total, errors, warnings, ok = _summarize_severity(results)

    lines = [
        "# Wiki Lint 报告",
        "",
        f"生成时间: {datetime.now().isoformat()}",
        f"总页面: {total}",
        f"  - 健康: {ok}",
        f"  - 警告: {warnings}",
        f"  - 错误: {errors}",
        "",
    ]

    lines.extend(
        _render_issue_section(
            "错误",
            results,
            lambda r: r["severity"] == "error",
            lambda issue: issue["type"] in ("no_frontmatter", "broken_link"),
        )
    )
    lines.extend(
        _render_issue_section(
            "警告",
            results,
            lambda r: r["severity"] == "warning",
        )
    )
    lines.extend(_render_issue_counts(results))
    lines.extend(_render_recommendations(errors, warnings))

    return "\n".join(lines)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _metadata_fields_to_add(fm: Optional[Dict]) -> List[str]:
    if fm is None:
        return []
    fields: List[str] = []
    if fm_get(fm, "knowledge_stage") is None:
        fields.append("knowledge_stage")
    if fm_get(fm, "evidence_level") is None:
        fields.append("evidence_level")
    if fm_get(fm, "status") is None:
        fields.append("status")
    if fm_get(fm, "source_count") is None:
        fields.append("source_count")
    return fields


def _auto_fix_candidates(results: List[Dict], page_index: Dict[str, Dict]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for result in results:
        if result["page"] in seen:
            continue
        page = page_index.get(str(Path(result["page"]).with_suffix("")))
        if not page:
            continue
        if not any(issue["type"] == "missing_meta" for issue in result["issues"]):
            continue
        fields = _metadata_fields_to_add(page.get("frontmatter"))
        if not fields:
            continue
        candidates.append(
            {
                "page": result["page"],
                "fields": fields,
                "before_hash": _content_hash(page["content"]),
            }
        )
        seen.add(result["page"])
    return candidates


def _record_auto_fix_action(
    *,
    db_path: Path,
    vault_dir: Path | str,
    action_id: str | None,
    status: str,
    planned_pages: List[Dict[str, Any]],
    fixed_pages: List[Dict[str, Any]] | None = None,
) -> str:
    ledger = ActionLedger(db_path, initialize=True)
    evidence_refs = ["scripts/wiki_lint.py", str(vault_dir)]
    if action_id:
        evidence_refs.append(f"action-ledger:{action_id}")
    record = make_action_record(
        actor="scripts.wiki_lint",
        action_type="wiki_quality_fix",
        target=str(vault_dir),
        evidence_refs=tuple(evidence_refs),
        status=status,
        before_ref=f"{len(planned_pages)} planned metadata fixes",
        after_ref=f"{len(fixed_pages or [])} applied metadata fixes",
        verification={
            "schema_version": WIKI_QUALITY_SCHEMA_VERSION,
            "planned_pages": planned_pages,
            "fixed_pages": fixed_pages or [],
            "previous_action_id": action_id or "",
            "command": "python3 scripts/wiki_lint.py --fix --summary --json --budget",
        },
    )
    planned_keys = {
        str(value.get("page") or "")
        for value in planned_pages
        if isinstance(value, Mapping)
    }
    fixed_keys = {
        str(value.get("page") or "")
        for value in (fixed_pages or [])
        if isinstance(value, Mapping)
    }
    material_action = authorize_primary_action_ledger_record(
        record,
        state_db_path=db_path.parent / "producer_consumer_ledger.db",
        contract_id="project-contract:wiki-quality-fix-action-ledger",
        contract_revision_id="mnemos.wiki_quality_fix_action_ledger.v1",
        contract_text=(
            "Append a Wiki quality-fix lifecycle row only when the exact plan, "
            "result set, status, and vault target are mutually consistent."
        ),
        source_namespace="wiki-quality-fix-action-ledger",
        source_facts={
            "vault": str(vault_dir),
            "status": status,
            "planned_count": len(planned_pages),
            "fixed_count": len(fixed_pages or []),
            "plan_hash": sha256_json(planned_pages),
            "result_hash": sha256_json(fixed_pages or []),
            "previous_action_id": action_id or "",
        },
        decision_checks={
            "plan_is_nonempty": bool(planned_keys),
            "status_is_lifecycle_state": status in {"processing", "verified"},
            "fixed_pages_are_planned": fixed_keys.issubset(planned_keys),
            "processing_has_no_fixed_claim": status != "processing" or not fixed_keys,
        },
        evidence_refs=tuple(str(value) for value in evidence_refs),
        task="Append the exact Wiki quality-fix lifecycle row",
        goal="Preserve a truthful, plan-bound Wiki fix receipt.",
        constraints=(
            "Do not claim fixes outside the reviewed plan.",
            "The authorization governs only this ActionLedger append.",
        ),
        producer="wiki-lint",
        producer_version="mnemos.wiki_quality_fix_action_ledger.v1",
        producer_code_hash=sha256_json(
            {
                "module": "scripts.wiki_lint",
                "contract": "mnemos.wiki_quality_fix_action_ledger.v1",
            }
        ),
        evaluator_id="wiki-quality-fix-action-ledger-evaluator",
        approved_candidate_key="append_bound_wiki_quality_fix_receipt",
        approved_candidate_summary="Append the plan-bound Wiki fix lifecycle row.",
        rejected_candidate_key="omit_unbound_wiki_quality_fix_receipt",
        rejected_candidate_summary="Do not append an inconsistent Wiki fix claim.",
        approved_reason_code="wiki_quality_fix_binding_verified",
        rejected_reason_code="wiki_quality_fix_binding_rejected",
        committed_metric="wiki_quality_fix_action_ledger_receipt",
        rejected_metric="unbound_wiki_quality_fix_ledger_count",
    )
    return ledger.record(record, material_action=material_action)


def auto_fix(
    results: List[Dict],
    all_pages: List[Dict],
    page_index: Dict[str, Dict],
    *,
    log: Callable[[str], None] | None = print,
) -> Dict[str, Any]:
    """自动修复简单问题，返回修复报告。"""
    fixed = 0
    fixed_pages: List[Dict[str, Any]] = []
    for r in results:
        page = page_index.get(str(Path(r["page"]).with_suffix("")))
        if not page:
            continue

        fm = page.get("frontmatter")
        if fm is None:
            continue

        # 修复缺元数据：写入中文展示字段，内部工具通过 alias 映射读取。
        modified = False
        fields_added: List[str] = []
        if fm_get(fm, "knowledge_stage") is None:
            fm["知识阶段"] = "原始"
            fields_added.append("knowledge_stage")
            modified = True
        if fm_get(fm, "evidence_level") is None:
            fm["证据级别"] = "单源"
            fields_added.append("evidence_level")
            modified = True
        if fm_get(fm, "status") is None:
            fm["状态"] = "草稿"
            fields_added.append("status")
            modified = True
        if fm_get(fm, "source_count") is None:
            fm["来源数量"] = 1
            fields_added.append("source_count")
            modified = True

        if modified:
            # 写回文件
            content = page["content"]
            fm_match = FRONTMATTER_RE.match(content)
            if fm_match:
                fm = to_chinese_frontmatter_preserving_unknown(fm)
                try:
                    import yaml

                    new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip()
                except ImportError:
                    new_fm = json.dumps(fm, ensure_ascii=False, indent=2)
                new_content = f"---\n{new_fm}\n---\n" + content[fm_match.end() :]
                Path(page["path"]).write_text(new_content, encoding="utf-8")
                fixed += 1
                fixed_pages.append(
                    {
                        "page": r["page"],
                        "fields_added": fields_added,
                        "before_hash": _content_hash(content),
                        "after_hash": _content_hash(new_content),
                    }
                )
                if log:
                    log(f"[Lint] 已修复元数据: {r['page']}")

    return {"fixed": fixed, "fixed_pages": fixed_pages}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Wiki Lint - 健康扫描脚本")
    parser.add_argument("--fix", action="store_true", help="自动修复简单问题")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--summary", action="store_true", help="只输出 mnemos.wiki_quality.v1 汇总")
    parser.add_argument("--full", action="store_true", help="JSON 中包含逐页 lint 结果")
    parser.add_argument("--budget", action="store_true", help="按预算线校验 wiki 质量")
    parser.add_argument("--budget-file", default="", help="读取 JSON 预算覆盖文件")
    parser.add_argument("--sample-limit", type=int, default=20, help="人工清单样本数量")
    parser.add_argument(
        "--stale-days", type=int, default=STALE_DAYS, help=f"陈旧阈值（默认 {STALE_DAYS}）"
    )
    parser.add_argument(
        "--stub-threshold",
        type=int,
        default=STUB_THRESHOLD,
        help=f"stub 阈值（默认 {STUB_THRESHOLD}）",
    )
    args = parser.parse_args()

    stale_days = args.stale_days
    stub_threshold = args.stub_threshold
    log_stream = sys.stderr if args.json else sys.stdout

    def log(message: str) -> None:
        print(message, file=log_stream)

    log(f"[Lint] 扫描 Wiki 目录: {WIKI_DIR}")
    pages = scan_all_pages()
    log(f"[Lint] 找到 {len(pages)} 个页面")

    if not pages:
        report = build_quality_report(
            [],
            vault_dir=WIKI_DIR,
            stale_days=stale_days,
            stub_threshold=stub_threshold,
            budget_overrides=_load_budget_overrides(args.budget_file),
            sample_limit=args.sample_limit,
            include_pages=args.full,
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            log("[Lint] 没有页面可扫描")
            print(format_quality_summary(report) if args.summary else "")
        return

    page_index = build_page_index(pages)
    from core.vaults.link_audit import (
        build_vault_target_aliases,
        build_vault_target_index,
        canonical_wiki_target_key,
    )

    target_aliases = build_vault_target_aliases(WIKI_DIR)
    target_index = build_vault_target_index(WIKI_DIR)
    incoming_pages: Set[str] = set()
    for page in pages:
        for link in page["links"]:
            targets = target_index.get(canonical_wiki_target_key(link), ())
            if len(targets) == 1:
                incoming_pages.add(targets[0])
    results = [
        lint_page(
            p,
            pages,
            page_index,
            stale_days,
            stub_threshold,
            target_aliases,
            incoming_pages,
        )
        for p in pages
    ]
    budget_overrides = _load_budget_overrides(args.budget_file)
    fix_report: Dict[str, Any] = {}
    action_ledger_ref = ""

    # 自动修复
    if args.fix:
        planned_pages = _auto_fix_candidates(results, page_index)
        ledger_db = Path(get_config().database_dir) / "action_ledger.db"
        if planned_pages:
            action_ledger_ref = _record_auto_fix_action(
                db_path=ledger_db,
                vault_dir=WIKI_DIR,
                action_id=None,
                status="processing",
                planned_pages=planned_pages,
            )
        fix_report = auto_fix(results, pages, page_index, log=log)
        if planned_pages:
            action_ledger_ref = _record_auto_fix_action(
                db_path=ledger_db,
                vault_dir=WIKI_DIR,
                action_id=action_ledger_ref,
                status="verified",
                planned_pages=planned_pages,
                fixed_pages=list(fix_report.get("fixed_pages", [])),
            )
            fix_report["action_ledger_ref"] = action_ledger_ref
        log(f"[Lint] 自动修复了 {fix_report.get('fixed', 0)} 个页面")
        # 修复后重新扫描
        pages = scan_all_pages()
        page_index = build_page_index(pages)
        target_aliases = build_vault_target_aliases(WIKI_DIR)
        target_index = build_vault_target_index(WIKI_DIR)
        incoming_pages = set()
        for page in pages:
            for link in page["links"]:
                targets = target_index.get(canonical_wiki_target_key(link), ())
                if len(targets) == 1:
                    incoming_pages.add(targets[0])
        results = [
            lint_page(
                p,
                pages,
                page_index,
                stale_days,
                stub_threshold,
                target_aliases,
                incoming_pages,
            )
            for p in pages
        ]

    quality_report = build_quality_report(
        results,
        vault_dir=WIKI_DIR,
        stale_days=stale_days,
        stub_threshold=stub_threshold,
        budget_overrides=budget_overrides,
        sample_limit=args.sample_limit,
        include_pages=args.full,
        fix_report=fix_report,
        action_ledger_ref=action_ledger_ref,
    )

    # 输出
    if args.json:
        print(json.dumps(quality_report, ensure_ascii=False, indent=2, default=str))
    elif args.summary:
        print(format_quality_summary(quality_report))
    else:
        report = generate_report(results)
        print()
        print(report)

    # 退出码
    errors = sum(1 for r in results if r["severity"] == "error")
    budget_failed = bool(quality_report["budgets"]["failures"]) if args.budget else False
    if errors > 0 or budget_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
