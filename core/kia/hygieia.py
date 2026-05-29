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
# Hygieia — 健康女神 — 知识免疫系统，过滤错误与污染
# 原模块: knowledge_immune.py



import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from core.config import get_config
from core.pluggable import PluggableModule
import logging

logger = logging.getLogger(__name__)
try:
    import yaml
except ImportError:  # pragma: no cover - exercised when PyYAML is absent
    yaml = None



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


class KnowledgeImmuneSystem(PluggableModule):
    """知识免疫系统 — 实现 PluggableModule 热插拔接口"""

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
        self.excluded_dirs = {"99-Reports", "07-Shadow", ".git", ".kg", "__pycache__"}

        # 懒加载依赖
        self._graph = graph
        self._dna_engine = dna_engine
        self._enabled = True

    # ---- PluggableModule 接口 ----

    def enable(self) -> None:
        self._enabled = True
        logger.info("KnowledgeImmuneSystem enabled")

    def disable(self) -> None:
        self._enabled = False
        logger.info("KnowledgeImmuneSystem disabled")

    def configure(self, cfg: Dict[str, Any]) -> None:
        if "temporal_expiry" in cfg:
            self.TEMPORAL_EXPIRY.update(cfg["temporal_expiry"])
        if "excluded_dirs" in cfg:
            self.excluded_dirs = set(cfg["excluded_dirs"])

    def handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        if not self._enabled:
            return
        if event_type == "knowledge.ingested":
            page_path = data.get("page_path")
            if page_path:
                self._on_knowledge_ingested(Path(page_path))
        elif event_type == "scheduler.daily":
            if data.get("task_name") == "immune_scan":
                self.full_scan()
        elif event_type == "entropy.suggestions":
            # 熵减引擎的合并建议可作为免疫输入：标记重复/相似页面
            candidates = data.get("candidates", [])
            for candidate in candidates:
                if candidate.get("merge_strategy") == "delete_duplicate":
                    logger.info(
                        f"熵减建议删除重复: {candidate.get('page_a')} ↔ {candidate.get('page_b')}"
                    )

    def _on_knowledge_ingested(self, page_path: Path) -> None:
        """新知识入库后的增量检测"""
        issues = []
        for detector in [self.detect_content_quality, self.detect_low_confidence]:
            try:
                issues.extend(detector([page_path]))
            except Exception:
                logger.warning(f"增量检测失败: {detector.__name__}", exc_info=True)
        if issues:
            logger.info(f"新知识 '{page_path.name}' 检测到 {len(issues)} 个问题")

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
                from .genos import DNAEngine
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
                logging.getLogger(__name__).warning(f"Caught unexpected error at hygieia.py", exc_info=True)
                continue

            temporal = self._fm_get(fm, "temporal_scope", "时效性", default="上下文相关")
            created = self._fm_get(fm, "created_at", "创建日期", "created", default="")
            version_tag = self._fm_get(fm, "version_tag", "版本标记", default="")

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
                logging.getLogger(__name__).warning(f"Caught unexpected error at hygieia.py", exc_info=True)
                continue

            confidence = float(self._fm_get(fm, "confidence", "置信度", default=0.5))
            evidence = self._fm_get(fm, "evidence_level", "证据级别", default="single-source")

            if confidence < 0.4:
                issues.append(ImmuneIssue(
                    issue_type="low_confidence",
                    severity="high",
                    page=str(page),
                    description=f"置信度仅 {confidence}，低于安全阈值 0.4",
                    suggestion="请补充验证来源，或明确标注为假设/待验证",
                ))
            elif confidence < 0.6 and evidence in {"单源", "single-source"}:
                issues.append(ImmuneIssue(
                    issue_type="weak_evidence",
                    severity="medium",
                    page=str(page),
                    description=f"置信度 {confidence} 且证据级别为单源，可靠性不足",
                    suggestion="寻找更多验证来源，或降低适用范围声明",
                ))

        return issues

    def detect_duplicates(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测疑似重复：委托熵减引擎，避免重复实现相似度扫描。"""
        issues = []
        try:
            from .eris import EntropyEngine
            engine = EntropyEngine(wiki_base=str(self.wiki_base))
            if self._dna_engine is not None:
                engine._dna_engine = self._dna_engine
            report = engine.scan()
        except Exception as e:
            logger.warning(f"熵减引擎重复检测失败: {e}")
            return issues

        for candidate in report.candidates:
            if candidate.merge_strategy not in {"delete_duplicate", "merge_into_one"}:
                continue
            severity = "high" if candidate.merge_strategy == "delete_duplicate" else "medium"
            issues.append(ImmuneIssue(
                issue_type="duplicate",
                severity=severity,
                page=candidate.page_a,
                related_pages=[candidate.page_b],
                description=f"与 '{Path(candidate.page_b).name}' 相似度 {candidate.similarity:.0%}，疑似重复或高度相似",
                suggestion=candidate.recommended_action,
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
                logging.getLogger(__name__).warning(f"Caught unexpected error at hygieia.py", exc_info=True)
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

    def detect_knowledge_gaps(self, pages: List[Path] = None) -> List[ImmuneIssue]:
        """检测知识盲区：某些 topic / domain / 形态下的覆盖度不足。

        检测维度：
        1. 形态缺失 — 某些 topic 下缺少关键形态（decision、problem-solution、heuristic）
        2. Domain 稀疏 — 某个 domain 标签下页面数过少
        3. 时间断层 — 某个高频 topic 长期（>180天）无更新
        4. 关联稀疏 — 高价值页面（decision/heuristic）入度/出度过低
        """
        issues = []
        pages = pages or self._list_pages()
        if not pages:
            return issues

        # ---- 收集元数据 ----
        topic_forms: Dict[str, Set[str]] = {}      # topic -> {forms}
        topic_last_update: Dict[str, datetime] = {}  # topic -> last update
        domain_counts: Dict[str, int] = {}           # domain -> page count
        form_counts: Dict[str, int] = {}             # form -> page count
        high_value_pages: List[Tuple[Path, str, float]] = []  # (page, form, confidence)

        CRITICAL_FORMS = {"decision", "problem-solution", "heuristic", "insight"}
        MIN_DOMAIN_PAGES = 3
        MIN_HIGH_VALUE_DEGREE = 2

        for page in pages:
            try:
                content = page.read_text(encoding="utf-8")
                fm = self._extract_frontmatter(content)
            except Exception:
                logging.getLogger(__name__).warning(
                    f"Caught unexpected error at hygieia.py", exc_info=True
                )
                continue

            tags = self._fm_get(fm, "tags", "标签", default=[])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            forms = self._fm_get(fm, "form", "形态", default=[])
            if isinstance(forms, str):
                forms = [forms]
            forms = set(f.lower() for f in forms if f)
            created = self._fm_get(fm, "created_at", "创建日期", "created", default="")
            confidence = float(self._fm_get(fm, "confidence", "置信度", default=0.5))

            # domain 统计（用 tags 的前缀或特定 domain 标签）
            domains = [t for t in tags if isinstance(t, str) and t.startswith("domain/")]
            if not domains:
                domains = tags[:1]  # fallback 用第一个 tag 作为 domain
            for d in domains:
                domain_counts[d] = domain_counts.get(d, 0) + 1

            # topic 统计（去掉 domain/ 前缀的标签）
            topics = [t for t in tags if isinstance(t, str) and not t.startswith("domain/")]
            if not topics:
                topics = [page.stem]
            for t in topics:
                topic_forms.setdefault(t, set()).update(forms)
                # 时间断层追踪
                if created:
                    try:
                        dt = datetime.strptime(str(created), "%Y-%m-%d")
                        if t not in topic_last_update or dt > topic_last_update[t]:
                            topic_last_update[t] = dt
                    except ValueError:
                        pass

            # 形态统计
            for f in forms:
                form_counts[f] = form_counts.get(f, 0) + 1

            # 高价值页面追踪
            if forms & CRITICAL_FORMS and confidence >= 0.6:
                high_value_pages.append((page, next(iter(forms & CRITICAL_FORMS)), confidence))

        # ---- 1. 形态缺失检测 ----
        for topic, forms in topic_forms.items():
            missing = CRITICAL_FORMS - forms
            if missing and len(forms) >= 2:  # 只有当该 topic 已有一定积累时才报缺失
                for m in missing:
                    issues.append(ImmuneIssue(
                        issue_type="knowledge_gap",
                        severity="medium",
                        page=topic,
                        description=f"topic '{topic}' 缺少 '{m}' 形态的知识（当前仅有 {forms}）",
                        suggestion=f"补充该 topic 下的 {m} 形态知识，完善知识覆盖",
                    ))

        # ---- 2. Domain 稀疏检测 ----
        for domain, count in domain_counts.items():
            if count < MIN_DOMAIN_PAGES:
                issues.append(ImmuneIssue(
                    issue_type="knowledge_gap",
                    severity="low",
                    page=domain,
                    description=f"domain '{domain}' 仅 {count} 个页面，知识覆盖稀疏",
                    suggestion="扩展该 domain 下的知识积累，或考虑合并到相关 domain",
                ))

        # ---- 3. 时间断层检测 ----
        now = datetime.now()
        for topic, last_dt in topic_last_update.items():
            days_since = (now - last_dt).days
            if days_since > 180 and topic in topic_forms and len(topic_forms[topic]) >= 2:
                issues.append(ImmuneIssue(
                    issue_type="knowledge_gap",
                    severity="medium",
                    page=topic,
                    description=f"topic '{topic}' 已 {days_since} 天未更新，可能知识已陈旧",
                    suggestion="回顾该 topic 的最新进展，更新或补充相关知识",
                ))

        # ---- 4. 高价值页面关联稀疏 ----
        if self.graph:
            for page, form, confidence in high_value_pages:
                page_str = str(page)
                out_degree = len(self.graph.get_relations(page_str))
                in_degree = len(self.graph.get_incoming_relations(page_str))
                total_degree = out_degree + in_degree
                if total_degree < MIN_HIGH_VALUE_DEGREE:
                    issues.append(ImmuneIssue(
                        issue_type="knowledge_gap",
                        severity="medium",
                        page=page_str,
                        description=(
                            f"高价值页面（{form}, 置信度 {confidence}）"
                            f"关联度仅 {total_degree}，知识网络未充分连接"
                        ),
                        suggestion="发现更多相关页面并建立关系，提升知识网络密度",
                        auto_fixable=True,
                        auto_fix_action="run_relation_discovery",
                    ))

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
            ("知识盲区", self.detect_knowledge_gaps),
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

        # 发布免疫报告事件
        self._emit_event("immune.report", {
            "scanned_pages": report.scanned_pages,
            "issue_count": len(report.issues),
            "critical_count": report.critical_count,
            "health_score": report.health_score,
        })

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

        # 发布自动修复事件
        if logs:
            self._emit_event("immune.auto_fix", {"actions": logs})

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
        """列出所有 wiki 页面，扫描整个 Vault 并排除报告/影子/系统目录。"""
        if not self.wiki_base.exists():
            return []
        pages = []
        for page in self.wiki_base.rglob("*.md"):
            rel_parts = page.relative_to(self.wiki_base).parts
            if any(part in self.excluded_dirs or part.startswith(".") for part in rel_parts):
                continue
            pages.append(page)
        return pages

    @staticmethod
    def _fm_get(fm: Dict, *keys, default=None):
        for key in keys:
            if key in fm:
                return fm.get(key)
        return default

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if yaml is None:
            return {}
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
