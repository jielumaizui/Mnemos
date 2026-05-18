"""
Knowledge Immune System - 知识免疫系统

自动检测知识库中的质量问题：
1. 冲突检测 — 矛盾的知识（利用 KnowledgeGraph）
2. 过时检测 — 时效性过期的知识
3. 孤知识检测 — 无关联的孤立页面
4. 低置信度检测 — 证据薄弱的知识
5. 重复检测 — 疑似重复入库（利用 KnowledgeDNA）
6. 内容质量检测 — 内容过短、结构缺失
7. 循环依赖检测 — A→B→A 的循环

设计原则：
- 利用已有的 Graph 和 DNA 能力，不做重复计算
- 每个检测器独立运行，可单独调用
- 输出结构化报告，支持自动修复建议
- 严重问题优先展示
"""

import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class ImmuneIssue:
    """免疫问题"""
    issue_type: str              # 问题类型标识
    severity: str                # critical / high / medium / low
    page: str                    # 问题页面路径
    related_pages: List[str] = field(default_factory=list)  # 关联页面
    description: str = ""        # 问题描述
    suggestion: str = ""         # 修复建议
    auto_fixable: bool = False   # 是否可自动修复
    auto_fix_action: str = ""    # 自动修复动作


@dataclass
class HealthReport:
    """健康报告"""
    scanned_pages: int = 0
    issues: List[ImmuneIssue] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)
    auto_fixable_count: int = 0
    critical_count: int = 0

    @property
    def health_score(self) -> float:
        """健康分数 0-100"""
        if self.scanned_pages == 0:
            return 100.0
        # 基础分 100，按问题扣分
        penalty = 0
        for issue in self.issues:
            if issue.severity == "critical":
                penalty += 15
            elif issue.severity == "high":
                penalty += 8
            elif issue.severity == "medium":
                penalty += 3
            elif issue.severity == "low":
                penalty += 1
        return max(0.0, 100.0 - penalty)


class KnowledgeImmuneSystem:
    """知识免疫系统"""

    # 过时阈值
    TEMPORAL_EXPIRY = {
        "版本绑定": timedelta(days=90),
        "上下文相关": timedelta(days=180),
        "稳定": timedelta(days=365),
        "永久": None,
    }

    def __init__(self, wiki_base: str = None,
                 graph=None, dna_engine=None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"

        # 懒加载依赖
        self._graph = graph
        self._dna_engine = dna_engine

    @property
    def graph(self):
        if self._graph is None:
            try:
                from .knowledge_graph import KnowledgeGraph
                self._graph = KnowledgeGraph(wiki_base=str(self.wiki_base))
            except ImportError:
                self._graph = None
        return self._graph

    @property
    def dna_engine(self):
        if self._dna_engine is None:
            try:
                from .knowledge_dna import DNAEngine
                self._dna_engine = DNAEngine(wiki_base=str(self.wiki_base))
            except ImportError:
                self._dna_engine = None
        return self._dna_engine

    # ========== 检测器 ==========

    def detect_conflicts(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测知识冲突（矛盾关系）"""
        issues = []
        if not self.graph:
            return issues

        conflicts = self.graph.detect_conflicts()
        for rel1, rel2, desc in conflicts:
            issues.append(ImmuneIssue(
                issue_type="conflict",
                severity="critical",
                page=rel1.source,
                related_pages=[rel1.target],
                description=desc,
                suggestion="请检查两个知识是否确实矛盾，如果是，标注适用边界区分场景；如果不是，删除错误的 contradicts 关系",
            ))
        return issues

    def detect_outdated(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测过时知识"""
        issues = []
        pages = pages or self._list_pages()

        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
            except Exception:
                continue

            temporal = fm.get("时效性", "上下文相关")
            created = fm.get("创建日期", "")
            version_tag = fm.get("版本标记", "")

            # 检查时效性过期
            expiry = self.TEMPORAL_EXPIRY.get(temporal)
            if expiry and created:
                try:
                    created_date = datetime.strptime(str(created), "%Y-%m-%d")
                    if datetime.now() - created_date > expiry:
                        days_old = (datetime.now() - created_date).days
                        issues.append(ImmuneIssue(
                            issue_type="outdated",
                            severity="high",
                            page=str(page),
                            description=f"知识已创建 {days_old} 天，超过 '{temporal}' 类型的建议有效期 ({expiry.days} 天)",
                            suggestion="请验证知识是否仍然有效，更新内容或调整时效性标记",
                        ))
                except ValueError:
                    pass

            # 版本绑定检查（如果有版本标记）
            if temporal == "版本绑定" and version_tag:
                # 简单启发式：如果版本标记包含具体版本号，提示检查新版本
                if any(v in str(version_tag) for v in ["3.10", "3.11", "1.19", "2023", "2024"]):
                    issues.append(ImmuneIssue(
                        issue_type="version_check",
                        severity="medium",
                        page=str(page),
                        description=f"版本标记为 '{version_tag}'，请确认是否有新版本发布",
                        suggestion="检查相关工具的官方文档，更新版本标记和内容",
                    ))

        return issues

    def detect_orphans(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测孤知识（无关联页面）"""
        issues = []
        if not self.graph:
            return issues

        pages = pages or self._list_pages()
        for page in pages:
            page_str = str(page)
            outgoing = self.graph.get_relations(page_str)
            incoming = self.graph.get_incoming_relations(page_str)

            total = len(outgoing) + len(incoming)
            if total == 0:
                issues.append(ImmuneIssue(
                    issue_type="orphan",
                    severity="medium",
                    page=page_str,
                    description="该知识页面没有任何关联，可能是孤立入库的",
                    suggestion="检查是否有相关页面可以建立关系，或考虑与其他知识合并",
                    auto_fixable=True,
                    auto_fix_action="run_relation_discovery",
                ))
            elif total == 1:
                issues.append(ImmuneIssue(
                    issue_type="weakly_connected",
                    severity="low",
                    page=page_str,
                    description=f"该页面仅有 {total} 个关联，连接度较低",
                    suggestion="尝试发现更多相关知识和建立关系",
                ))

        return issues

    def detect_low_confidence(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测低置信度知识"""
        issues = []
        pages = pages or self._list_pages()

        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
            except Exception:
                continue

            confidence = float(fm.get("置信度", 0.5))
            evidence = fm.get("证据级别", "单源")

            if confidence < 0.4:
                issues.append(ImmuneIssue(
                    issue_type="low_confidence",
                    severity="high",
                    page=str(page),
                    description=f"置信度仅 {confidence}，低于安全阈值 0.4",
                    suggestion="请补充验证来源，或明确标注为假设/待验证",
                ))
            elif confidence < 0.6 and evidence == "单源":
                issues.append(ImmuneIssue(
                    issue_type="weak_evidence",
                    severity="medium",
                    page=str(page),
                    description=f"置信度 {confidence} 且证据级别为单源，可靠性不足",
                    suggestion="寻找更多验证来源，或降低适用范围声明",
                ))

        return issues

    def detect_duplicates(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测疑似重复"""
        issues = []
        if not self.dna_engine:
            return issues

        pages = pages or self._list_pages()

        for page in pages:
            dna = self.dna_engine.compute_dna(page)
            if not dna:
                continue
            self.dna_engine.save_dna(dna)

            duplicates = self.dna_engine.find_duplicates(dna)
            for dup in duplicates:
                issues.append(ImmuneIssue(
                    issue_type="duplicate",
                    severity="high",
                    page=str(page),
                    related_pages=[dup.target_page],
                    description=f"与 '{Path(dup.target_page).name}' 相似度 {dup.overall_score:.0%}，疑似重复入库",
                    suggestion="请确认是否为同一知识，如果是，合并或删除较旧版本；如果不是，调整关键词区分",
                ))

        return issues

    def detect_content_quality(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测内容质量问题"""
        issues = []
        pages = pages or self._list_pages()

        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
                body = self._extract_body(content)
            except Exception:
                continue

            # 内容过短
            body_text = re.sub(r'[#\-\*\|\[\]\(\)\`]', '', body)
            char_count = len(body_text.strip())
            if char_count < 100:
                issues.append(ImmuneIssue(
                    issue_type="too_short",
                    severity="medium",
                    page=str(page),
                    description=f"正文仅 {char_count} 字符，内容过于简短，难以构成完整知识",
                    suggestion="补充背景、核心内容、适用边界和反模式",
                ))

            # 结构缺失检查
            required_sections = ["核心内容"]
            for section in required_sections:
                if section not in body:
                    issues.append(ImmuneIssue(
                        issue_type="missing_section",
                        severity="low",
                        page=str(page),
                        description=f"缺少 '{section}' 章节",
                        suggestion=f"补充 '{section}' 章节，确保知识完整性",
                    ))

            # 边界缺失
            if "适用边界" not in body:
                issues.append(ImmuneIssue(
                    issue_type="missing_boundaries",
                    severity="medium",
                    page=str(page),
                    description="未声明适用边界，容易导致误用",
                    suggestion="补充 '适用边界' 章节，明确适用范围和不适用范围",
                ))

            # 关键词稀疏
            keywords = fm.get("关键词", {})
            total_kws = sum(len(v) for v in keywords.values() if isinstance(v, list))
            if total_kws < 4:
                issues.append(ImmuneIssue(
                    issue_type="sparse_keywords",
                    severity="low",
                    page=str(page),
                    description=f"关键词仅 {total_kws} 个，不利于检索和关联",
                    suggestion="补充更多关键词，覆盖核心概念、场景、工具和动作",
                ))

        return issues

    def detect_circular_dependencies(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测循环依赖（A→B→...→A）"""
        issues = []
        if not self.graph:
            return issues

        pages = pages or self._list_pages()
        page_set = {str(p) for p in pages}

        for page in pages:
            page_str = str(page)
            # BFS 找环
            visited = {page_str}
            path = [page_str]
            queue = [(page_str, [page_str])]

            while queue:
                current, current_path = queue.pop(0)
                rels = self.graph.get_relations(current)
                for rel in rels:
                    if rel.relation_type.value in ("depends_on", "prerequisite_for", "builds_on"):
                        nxt = rel.target
                        if nxt == page_str and len(current_path) > 1:
                            # 发现环
                            cycle = " → ".join(current_path + [nxt])
                            issues.append(ImmuneIssue(
                                issue_type="circular_dependency",
                                severity="high",
                                page=page_str,
                                related_pages=current_path[1:],
                                description=f"发现循环依赖: {cycle}",
                                suggestion="检查依赖关系是否正确，打破循环（通常将某个依赖改为 references 或 alternative_to）",
                            ))
                            queue = []  # 找到一个环就跳出
                            break
                        if nxt not in visited and nxt in page_set:
                            visited.add(nxt)
                            queue.append((nxt, current_path + [nxt]))

        return issues

    # ========== 综合扫描 ==========

    def full_scan(self, pages: List[Path] = None) -> HealthReport:
        """全量扫描，运行所有检测器"""
        pages = pages or self._list_pages()
        report = HealthReport(scanned_pages=len(pages))

        detectors = [
            ("冲突", self.detect_conflicts),
            ("过时", self.detect_outdated),
            ("孤立", self.detect_orphans),
            ("低置信度", self.detect_low_confidence),
            ("重复", self.detect_duplicates),
            ("内容质量", self.detect_content_quality),
            ("循环依赖", self.detect_circular_dependencies),
        ]

        for name, detector in detectors:
            try:
                issues = detector(pages)
                report.issues.extend(issues)
                report.summary[name] = len(issues)
            except Exception as e:
                report.summary[f"{name}_error"] = str(e)

        # 统计
        report.critical_count = sum(1 for i in report.issues if i.severity == "critical")
        report.auto_fixable_count = sum(1 for i in report.issues if i.auto_fixable)

        # 按严重程度排序
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        report.issues.sort(key=lambda x: severity_order.get(x.severity, 99))

        return report

    def auto_fix(self, report: HealthReport) -> List[str]:
        """
        自动修复可修复的问题

        Returns:
            修复动作的日志列表
        """
        logs = []

        for issue in report.issues:
            if not issue.auto_fixable:
                continue

            if issue.issue_type == "orphan" and issue.auto_fix_action == "run_relation_discovery":
                if self.graph:
                    try:
                        page_path = Path(issue.page)
                        relations = self.graph.discover_relations(page_path)
                        count = self.graph.apply_discovered(relations, min_confidence=0.5)
                        if count > 0:
                            logs.append(f"为 '{page_path.name}' 自动发现 {count} 个关系")
                        else:
                            logs.append(f"'{page_path.name}' 未找到可自动建立的关系")
                    except Exception as e:
                        logs.append(f"'{issue.page}' 自动修复失败: {e}")

        return logs

    # ========== 报告生成 ==========

    def generate_report_markdown(self, report: HealthReport) -> str:
        """生成 Markdown 格式的健康报告"""
        lines = [
            "# 知识库健康报告",
            "",
            f"**扫描页面**: {report.scanned_pages}",
            f"**发现问题**: {len(report.issues)}",
            f"**严重问题**: {report.critical_count}",
            f"**健康分数**: {report.health_score:.0f}/100",
            f"**可自动修复**: {report.auto_fixable_count}",
            "",
            "## 问题分布",
            "",
        ]

        for name, count in report.summary.items():
            lines.append(f"- {name}: {count}")

        if report.issues:
            lines.extend(["", "## 详细问题", ""])
            current_severity = None
            for issue in report.issues:
                if issue.severity != current_severity:
                    current_severity = issue.severity
                    emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(current_severity, "⚪")
                    lines.append(f"### {emoji} {current_severity.upper()}")
                    lines.append("")

                page_name = Path(issue.page).name
                lines.append(f"**[{issue.issue_type}]** `{page_name}`")
                lines.append(f"- 描述: {issue.description}")
                lines.append(f"- 建议: {issue.suggestion}")
                if issue.auto_fixable:
                    lines.append("- 状态: ✅ 可自动修复")
                lines.append("")
        else:
            lines.extend(["", "## ✅ 知识库健康，未发现问题", ""])

        return "\n".join(lines)

    # ========== 辅助方法 ==========

    def _list_pages(self) -> List[Path]:
        """列出所有 wiki 页面"""
        if not self.inbox.exists():
            return []
        return list(self.inbox.glob("*.md"))

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
        return {}

    @staticmethod
    def _extract_body(content: str) -> str:
        """提取正文"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2]
        return content


# ========== 便捷函数 ==========

def run_health_check(wiki_base: str = None) -> HealthReport:
    """便捷函数：运行健康检查"""
    immune = KnowledgeImmuneSystem(wiki_base=wiki_base)
    return immune.full_scan()


def run_and_report(wiki_base: str = None) -> str:
    """便捷函数：运行检查并生成报告"""
    immune = KnowledgeImmuneSystem(wiki_base=wiki_base)
    report = immune.full_scan()
    return immune.generate_report_markdown(report)
