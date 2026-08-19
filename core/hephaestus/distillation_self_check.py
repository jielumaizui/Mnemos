# -*- coding: utf-8 -*-
"""Self-check stage for distillation fragments."""

from __future__ import annotations

import ast
import logging
import re
from typing import Dict, List, Tuple

from core.config import get_config
from core.hephaestus.distillation_models import KnowledgeFragment

logger = logging.getLogger(__name__)

FRAGMENT_BOUNDARY_CHARS = 8000

SELF_CHECK_FATAL_MARKERS = (
    "标题缺失或无效",
    "标题过短",
    "核心内容过短或缺失",
    "内容过长且缺少边界定义",
    "Python代码块可能存在语法错误",
    "检测到断言内部冲突",
    "检测到回流内容",
    "前提验证未通过",
)


def classify_self_check_issue(issue: str) -> str:
    """Classify a self-check issue into fatal or warning."""
    if any(marker in issue for marker in SELF_CHECK_FATAL_MARKERS):
        return "fatal"
    return "warning"


def max_self_check_severity(issues: List[str]) -> str:
    if not issues:
        return "ok"
    if any(classify_self_check_issue(issue) == "fatal" for issue in issues):
        return "fatal"
    return "warning"


class DistillSelfCheck:
    """第5层：自检 — 规则验证"""

    def __init__(self, link_probe_worker=None):
        self._link_probe = link_probe_worker

    def check(
        self, fragments: List[KnowledgeFragment], messages: List[Dict]
    ) -> Tuple[bool, List[str]]:
        """自检，返回 (是否全部通过, 问题列表)"""
        all_issues = []
        for frag in fragments:
            issues = self._check_fragment(frag, messages)
            frag.self_check_issues = issues
            frag.self_check_severity = max_self_check_severity(issues)
            frag.self_check_passed = frag.self_check_severity == "ok"
            all_issues.extend(issues)

        overall_passed = len(all_issues) == 0
        if not overall_passed:
            for frag in fragments:
                if not frag.self_check_passed:
                    frag.frontmatter["verification"] = "pending-verification"
                    frag.frontmatter["verification_severity"] = frag.self_check_severity
        return overall_passed, all_issues

    def _check_title(self, frag: KnowledgeFragment, issues: List[str]) -> None:
        if not frag.title or frag.title in ("无标题", "未知"):
            issues.append("标题缺失或无效")
        elif len(frag.title) < 5:
            issues.append("标题过短，缺乏可搜索性")

    def _check_core_content(self, frag: KnowledgeFragment, issues: List[str]) -> None:
        cfg = get_config()
        fragment_boundary_chars = int(
            cfg.get("distill.fragment_boundary_chars", FRAGMENT_BOUNDARY_CHARS)
            or FRAGMENT_BOUNDARY_CHARS
        )
        if not frag.core_content or len(frag.core_content) < 20:
            issues.append("核心内容过短或缺失")
        elif len(frag.core_content) > fragment_boundary_chars and not frag.boundaries:
            issues.append("内容过长且缺少边界定义")

    def _check_assertion_support(self, content: str, issues: List[str]) -> None:
        has_specific_data = bool(
            re.search(
                r"\d+\.?\d*[%％]|v\d+\.\d+|version\s+\d+|>=|<=|!=|==",
                content,
            )
        )
        has_assertion_words = bool(
            re.search(
                r"(必须|一定|never|always|应该|should|导致|因为|由于)",
                content,
                re.I,
            )
        )
        if has_assertion_words and not has_specific_data:
            issues.append("包含断言但缺少具体数据支撑")

    def _check_python_code_blocks(self, frag: KnowledgeFragment, issues: List[str]) -> None:
        code_blocks = re.findall(r"```(\w*)\n(.*?)```", frag.core_content, re.DOTALL)
        for lang, code in code_blocks:
            if lang in ("python", "py") and self._check_python_syntax(code):
                issues.append("Python代码块可能存在语法错误")

    def _check_wiki_links(self, content: str, issues: List[str]) -> None:
        for link in re.findall(r"\[\[([^\]]+)\]\]", content):
            if len(link) < 2 or link.startswith("待"):
                issues.append(f"可疑的Wiki链接: [[{link}]]")

    def _check_temporal(self, frag: KnowledgeFragment, content: str, issues: List[str]) -> None:
        temporal = frag.frontmatter.get("时效性", "")
        if temporal == "version-bound" and not frag.frontmatter.get("版本标记"):
            issues.append("标记为版本绑定但未指定版本标记")
        if not temporal and self._looks_contextual(content):
            frag.frontmatter["时效性"] = "contextual"
            issues.append("包含当前性表述，已标记为 contextual")

    def _check_reflow(self, content: str, issues: List[str]) -> None:
        if "<wiki-context" in content or "skip-distill" in content:
            issues.append("检测到回流内容，不应再次蒸馏")

    def _check_urls(self, frag: KnowledgeFragment, content: str, issues: List[str]) -> None:
        for url in re.findall(r'https?://[^\s)\]\>"]+', content):
            if "." not in url.split("://", 1)[1]:
                issues.append(f"可疑URL，待验证: {url}")
                continue
            frag.frontmatter.setdefault("external_links_pending_verification", True)
            if self._link_probe is not None:
                page_path = frag.frontmatter.get("wiki_page_path", frag.title)
                self._link_probe.enqueue(url, page_path)

    def _check_premise(self, frag: KnowledgeFragment, content: str, issues: List[str]) -> None:
        try:
            from core.kia.premise_validator import PremiseValidator

            validator = PremiseValidator()
            result = validator.validate(
                premise=frag.core_content[:500],
                current_context=content,
            )
            if not result.get("valid", True):
                issues.append(
                    f"前提验证未通过: {result.get('reason', 'unknown')} "
                    f"(置信度 {result.get('confidence', 0):.2f})"
                )
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.warning("[Distillation] 前提验证失败", exc_info=True)

    def _check_decision_dependencies(self, frag: KnowledgeFragment, content: str) -> None:
        try:
            from core.kia.decision_dependency_extractor import DecisionDependencyExtractor

            extractor = DecisionDependencyExtractor()
            decision_keywords = (
                "选择",
                "决定",
                "决策",
                "如果",
                "则",
                "否则",
                "option",
                "decide",
                "choose",
            )
            if any(kw in content.lower() for kw in decision_keywords):
                graph = extractor.extract(content)
                if graph.nodes:
                    frag.frontmatter["decision_graph"] = {
                        "nodes": len(graph.nodes),
                        "edges": len(graph.edges),
                        "roots": len(graph.get_root_decisions()),
                    }
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.warning("[Distillation] 决策依赖提取失败", exc_info=True)

    def _check_fragment(self, frag: KnowledgeFragment, messages: List[Dict]) -> List[str]:
        issues: List[str] = []
        content = frag.core_content + frag.background

        self._check_title(frag, issues)
        self._check_core_content(frag, issues)
        self._check_assertion_support(content, issues)
        self._check_python_code_blocks(frag, issues)
        self._check_wiki_links(content, issues)
        self._check_temporal(frag, content, issues)
        self._check_reflow(content, issues)
        self._check_urls(frag, content, issues)
        issues.extend(self._check_internal_conflicts(content))
        self._check_premise(frag, content, issues)
        self._check_decision_dependencies(frag, content)

        return issues

    def _check_python_syntax(self, code: str) -> bool:
        """简单 Python 语法检查，返回 True 表示有错误。"""
        try:
            ast.parse(code, mode="exec")
            return False
        except SyntaxError:
            return True

    def _looks_contextual(self, content: str) -> bool:
        return bool(
            re.search(r"(最新|目前|现在|当前|recently|currently|latest|as of)", content, re.I)
        )

    def _check_internal_conflicts(self, content: str) -> List[str]:
        try:
            from core.kia.assertion_extractor import extract_assertions
            from core.kia.conflict_resolver import detect_conflicts
        except ImportError:
            logging.getLogger(__name__).warning("冲突检测模块不可用", exc_info=True)
            return []

        assertions = extract_assertions(content, source="distill_self_check")
        if len(assertions) < 2:
            return []

        issues = []
        for i, assertion in enumerate(assertions):
            conflicts = detect_conflicts([assertion], assertions[i + 1 :], min_topic_overlap=0.2)
            for conflict in conflicts[:2]:
                issues.append(f"检测到断言内部冲突: {conflict.reason or conflict.conflict_type}")
        return issues
