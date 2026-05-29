"""
Knowledge Profile - 个人知识画像

基于知识库数据生成用户的知识画像：
- 领域分布：技术/产品/运营/管理 等占比
- 知识形态分布：问题-解决/经验法则/决策记录 等
- 技术栈画像：最常用的工具/框架
- 学习模式：问题导向 vs 方法论导向
- 知识增长曲线：入库速度、活跃趋势
- 质量画像：高置信度知识占比、效果评分分布

输出格式：Markdown 报告（可用于 Obsidian）
"""
# Metis — 智慧女神 — 知识画像，知识体系的深层刻画
# 原模块: knowledge_profile.py



import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import Counter
from core.config import get_config
import logging

logger = logging.getLogger(__name__)
try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency fallback
    yaml = None


EXCLUDED_DIRS = {".obsidian", ".git", "99-Archive", "templates", "__pycache__"}
PROFILE_DB_NAME = "profiles.db"


@dataclass
class KnowledgeProfile:
    """知识画像"""
    generated_at: str = ""
    total_knowledge: int = 0
    domain_distribution: Dict[str, int] = field(default_factory=dict)
    form_distribution: Dict[str, int] = field(default_factory=dict)
    tool_stack: List[Tuple[str, int]] = field(default_factory=list)
    scenario_tags: List[Tuple[str, int]] = field(default_factory=list)
    complexity_distribution: Dict[str, int] = field(default_factory=dict)
    confidence_distribution: Dict[str, int] = field(default_factory=dict)
    temporal_distribution: Dict[str, int] = field(default_factory=dict)
    growth_trend: List[Dict] = field(default_factory=list)
    learning_mode: Any = ""
    quality_score: float = 0.0
    activity_heatmap: Dict[str, Dict] = field(default_factory=dict)
    effect_distribution: Dict[str, int] = field(default_factory=dict)
    task_distribution: Dict[str, int] = field(default_factory=dict)
    health_trend: Dict[str, Any] = field(default_factory=dict)
    blindspot_distribution: Dict[str, int] = field(default_factory=dict)
    frontmatter_completeness: float = 0.0
    update_ratio: float = 0.0
    domain_entropy: float = 0.0


class ProfileGenerator:
    """画像生成器"""

    DOMAIN_TEMPORAL_WEIGHTS = {
        "技术": {"永久": 1.0, "稳定": 0.9, "版本绑定": 0.8, "上下文相关": 0.5},
        "产品": {"永久": 1.0, "稳定": 0.8, "版本绑定": 0.6, "上下文相关": 0.7},
        "管理": {"永久": 1.0, "稳定": 0.9, "版本绑定": 0.4, "上下文相关": 0.6},
        "运营": {"永久": 1.0, "稳定": 0.8, "版本绑定": 0.5, "上下文相关": 0.8},
    }
    DEFAULT_TEMPORAL_WEIGHTS = {"永久": 1.0, "稳定": 0.8, "版本绑定": 0.5, "上下文相关": 0.3}
    REQUIRED_FRONTMATTER_FIELDS = ["领域", "类型", "复杂度", "置信度", "时效性", "创建日期"]

    def __init__(
        self,
        wiki_base: str = None,
        db_path: str | Path | None = None,
        trail=None,
        immune=None,
        task_db: str | Path | None = None,
    ):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"
        self.profile_db = Path(db_path).expanduser() if db_path else (
            self.wiki_base / ".kg" / PROFILE_DB_NAME
        )
        self.trail = trail
        self.immune = immune
        self.task_db = Path(task_db).expanduser() if task_db else (
            self.wiki_base / ".kg" / "task_classifier.db"
        )
        self._init_db()

    def generate(self) -> KnowledgeProfile:
        """生成完整画像"""
        profile = KnowledgeProfile(generated_at=datetime.now().isoformat()[:10])

        if not self.wiki_base.exists():
            return profile

        pages = self._list_pages()
        profile.total_knowledge = len(pages)

        # 收集所有 frontmatter
        all_frontmatters = []
        page_frontmatters = []
        for page in pages:
            fm = self._extract_frontmatter(page)
            page_frontmatters.append((page, fm or {}))
            if fm:
                all_frontmatters.append(fm)

        # 1. 领域分布
        domains = [fm.get("领域", "其他") for fm in all_frontmatters]
        profile.domain_distribution = dict(Counter(domains).most_common())

        # 2. 知识形态分布
        forms = [fm.get("类型", "未知") for fm in all_frontmatters]
        profile.form_distribution = dict(Counter(forms).most_common())

        # 3. 技术栈（工具实体）
        all_tools = []
        for fm in all_frontmatters:
            tools = self._get_keywords(fm, "工具实体")
            all_tools.extend(tools)
        profile.tool_stack = Counter(all_tools).most_common(10)

        # 4. 场景标签
        all_scenes = []
        for fm in all_frontmatters:
            scenes = self._get_keywords(fm, "场景标签")
            all_scenes.extend(scenes)
        profile.scenario_tags = Counter(all_scenes).most_common(10)

        # 5. 复杂度分布
        complexities = [fm.get("复杂度", "入门") for fm in all_frontmatters]
        profile.complexity_distribution = dict(Counter(complexities).most_common())

        # 6. 置信度分布（分桶）
        confidences = []
        for fm in all_frontmatters:
            try:
                c = float(fm.get("置信度", 0.5))
                confidences.append(c)
            except (ValueError, TypeError):
                continue
        profile.confidence_distribution = self._bucket_confidence(confidences)

        # 7. 时效性分布
        temporals = [fm.get("时效性", "上下文相关") for fm in all_frontmatters]
        profile.temporal_distribution = dict(Counter(temporals).most_common())

        # 8. 增长趋势（按月）
        profile.growth_trend = self._calculate_growth(pages)

        # 9. 学习模式
        profile.learning_mode = self._detect_learning_mode(profile.form_distribution, page_frontmatters)

        # 10. 质量评分
        profile.quality_score = self._calculate_quality_score(
            confidences, all_frontmatters, profile.domain_distribution
        )

        # 11-16. 多源画像维度，可用则融合，不可用则降级为空。
        profile.activity_heatmap = self._calculate_activity_heatmap(pages)
        profile.effect_distribution = self._calculate_effect_distribution(profile.activity_heatmap)
        profile.task_distribution = self._calculate_task_distribution()
        profile.health_trend = self._calculate_health_trend()
        profile.blindspot_distribution = dict(profile.health_trend.get("issue_breakdown", {}))
        profile.frontmatter_completeness = self._calculate_frontmatter_completeness(all_frontmatters)
        profile.update_ratio = self._calculate_update_ratio(pages)
        profile.domain_entropy = self._calculate_domain_entropy(profile.domain_distribution)

        self._persist_profile(profile)

        return profile

    def generate_report(self, profile: KnowledgeProfile = None) -> str:
        """生成 Markdown 报告"""
        if profile is None:
            profile = self.generate()

        lines = [
            f"# 个人知识画像",
            f"生成时间: {profile.generated_at}",
            f"知识总量: **{profile.total_knowledge}** 条",
            "",
            "## 领域分布",
            "",
        ]

        for domain, count in list(profile.domain_distribution.items())[:8]:
            pct = count / max(profile.total_knowledge, 1) * 100
            bar = "█" * int(pct / 5)
            lines.append(f"- {domain}: {count} ({pct:.0f}%) {bar}")

        lines.extend(["", "## 知识形态", ""])
        for form, count in profile.form_distribution.items():
            lines.append(f"- {form}: {count}")

        lines.extend(["", "## 技术栈 TOP10", ""])
        for tool, count in profile.tool_stack:
            lines.append(f"- {tool}: {count} 次提及")

        lines.extend(["", "## 高频场景", ""])
        for scene, count in profile.scenario_tags[:5]:
            lines.append(f"- {scene}: {count} 次")

        lines.extend(["", "## 复杂度分布", ""])
        for comp, count in profile.complexity_distribution.items():
            lines.append(f"- {comp}: {count}")

        lines.extend(["", "## 置信度分布", ""])
        for bucket, count in profile.confidence_distribution.items():
            lines.append(f"- {bucket}: {count}")

        lines.extend(["", "## 学习模式", ""])
        if isinstance(profile.learning_mode, dict):
            lines.append(f"**{profile.learning_mode.get('simple_mode', '数据不足')}**")
            lines.append(f"- 转化路径: {profile.learning_mode.get('conversion_paths', 0)} 条")
            lines.append(f"- 效果驱动: {profile.learning_mode.get('effect_driven_mode', '数据不足')}")
        else:
            lines.append(f"**{profile.learning_mode}**")

        lines.extend(["", "## 质量评分", ""])
        lines.append(f"综合质量分: **{profile.quality_score:.1f}** / 10")
        lines.append(f"Frontmatter 完整度: **{profile.frontmatter_completeness:.0%}**")

        if profile.growth_trend:
            lines.extend(["", "## 增长趋势", ""])
            for point in profile.growth_trend[-6:]:
                lines.append(
                    f"- {point['month']}: 新增 {point.get('created', point.get('count', 0))} 条，"
                    f"更新 {point.get('updated', 0)} 条"
                )

        return "\n".join(lines)

    def generate_and_report(self) -> str:
        """生成画像并返回 Markdown 报告，供调度器调用。"""
        profile = self.generate()
        return self.generate_report(profile)

    def _bucket_confidence(self, confidences: List[float]) -> Dict[str, int]:
        """置信度分桶"""
        buckets = {"高 (≥0.8)": 0, "中 (0.5-0.8)": 0, "低 (<0.5)": 0}
        for c in confidences:
            if c >= 0.8:
                buckets["高 (≥0.8)"] += 1
            elif c >= 0.5:
                buckets["中 (0.5-0.8)"] += 1
            else:
                buckets["低 (<0.5)"] += 1
        return buckets

    def _calculate_growth(self, pages: List[Path]) -> List[Dict]:
        """计算月度创建与更新趋势"""
        monthly_created = Counter()
        monthly_updated = Counter()
        for page in pages:
            fm = self._extract_frontmatter(page)
            if fm:
                created = fm.get("创建日期", "")
                if created:
                    month = str(created)[:7]
                    if re.match(r"^\d{4}-\d{2}$", month):
                        monthly_created[month] += 1

                updated = fm.get("updated_at", "") or fm.get("修改日期", "")
                if updated:
                    month = str(updated)[:7]
                    if re.match(r"^\d{4}-\d{2}$", month):
                        monthly_updated[month] += 1

        all_months = sorted(set(monthly_created.keys()) | set(monthly_updated.keys()))

        return [
            {
                "month": month,
                "created": monthly_created.get(month, 0),
                "updated": monthly_updated.get(month, 0),
                "active": monthly_updated.get(month, 0) > 0,
            }
            for month in all_months[-12:]
        ]

    def _detect_learning_mode(self, form_dist: Dict[str, int], page_frontmatters: List[Tuple[Path, Dict]] = None) -> Dict:
        """检测学习模式"""
        problem_solving = form_dist.get("问题-解决", 0)
        anti_pattern = form_dist.get("反模式", 0)
        methodology = form_dist.get("方法论", 0)
        heuristic = form_dist.get("经验法则", 0)
        decision = form_dist.get("决策记录", 0)

        reactive = problem_solving + anti_pattern
        proactive = methodology + heuristic + decision

        if proactive > reactive * 2:
            simple_mode = "方法论导向型 — 偏好沉淀可复用的流程和原则"
        elif reactive > proactive * 2:
            simple_mode = "问题导向型 — 主要在遇到问题时记录解决方案"
        else:
            simple_mode = "平衡型 — 既有问题驱动也有主动沉淀"

        page_frontmatters = page_frontmatters or []
        conversion_paths = self._detect_conversion_paths(page_frontmatters)
        effect_mode = self._effect_driven_mode(page_frontmatters)

        return {
            "simple_mode": simple_mode,
            "reactive_ratio": round(reactive / max(reactive + proactive, 1), 2),
            "proactive_ratio": round(proactive / max(reactive + proactive, 1), 2),
            "conversion_paths": len(conversion_paths),
            "conversion_rate": round(len(conversion_paths) / max(problem_solving, 1), 2),
            "effect_driven_mode": effect_mode,
        }

    def _calculate_quality_score(self, confidences: List[float],
                                  frontmatters: List[Dict],
                                  domain_dist: Optional[Dict[str, int]] = None) -> float:
        """计算质量评分 0-10"""
        if not confidences:
            return 0.0

        # 基础分：平均置信度 * 5
        avg_confidence = sum(confidences) / len(confidences)
        score = avg_confidence * 5

        # 多样性加分
        domains = set(fm.get("领域") for fm in frontmatters if fm.get("领域"))
        if len(domains) >= 3:
            score += 1

        # 关键词完整度加分
        complete_keywords = sum(
            1 for fm in frontmatters
            if self._has_complete_keywords(fm)
        )
        keyword_ratio = complete_keywords / max(len(frontmatters), 1)
        score += keyword_ratio * 2

        temporal_bonus = sum(
            self.DOMAIN_TEMPORAL_WEIGHTS.get(
                fm.get("领域", "其他"), self.DEFAULT_TEMPORAL_WEIGHTS
            ).get(fm.get("时效性"), 0.5)
            for fm in frontmatters
        ) / max(len(frontmatters), 1)
        score += temporal_bonus * 2

        return min(round(score, 1), 10.0)

    def _has_complete_keywords(self, fm: Dict) -> bool:
        """检查关键词是否完整"""
        keywords = fm.get("关键词", {})
        if isinstance(keywords, dict):
            layers = ["核心概念", "场景标签", "工具实体", "动作标签"]
            return sum(1 for l in layers if keywords.get(l)) >= 3
        return False

    def _list_pages(self) -> List[Path]:
        pages = []
        for page in self.wiki_base.rglob("*.md"):
            relative_parts = set(page.relative_to(self.wiki_base).parts[:-1])
            if relative_parts & EXCLUDED_DIRS:
                continue
            if any(part.startswith(".") for part in page.relative_to(self.wiki_base).parts[:-1]):
                continue
            pages.append(page)
        return sorted(pages)

    def _detect_conversion_paths(self, page_frontmatters: List[Tuple[Path, Dict]]) -> List[Tuple[str, str]]:
        type_by_stem = {page.stem: fm.get("类型", "未知") for page, fm in page_frontmatters}
        paths = []
        for page, fm in page_frontmatters:
            if fm.get("类型") != "问题-解决":
                continue
            try:
                content = page.read_text(encoding="utf-8")
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at metis.py", exc_info=True)
                continue
            for target in re.findall(r"\[\[([^\]|#]+)", content):
                if type_by_stem.get(Path(target).stem) == "方法论":
                    paths.append((page.stem, Path(target).stem))
        return paths

    def _effect_driven_mode(self, page_frontmatters: List[Tuple[Path, Dict]]) -> str:
        activity = self._calculate_activity_heatmap([page for page, _ in page_frontmatters])
        if not activity:
            return "数据不足"

        problem_scores = []
        methodology_queries = 0
        for page, fm in page_frontmatters:
            stats = activity.get(page.stem, {})
            if fm.get("类型") == "问题-解决" and "effect_score" in stats:
                problem_scores.append(float(stats.get("effect_score") or 0.0))
            if fm.get("类型") == "方法论":
                methodology_queries += int(stats.get("query_count") or 0)

        avg_problem_effect = sum(problem_scores) / max(len(problem_scores), 1)
        if avg_problem_effect >= 0.7:
            return "解决效果驱动型"
        if methodology_queries >= max(3, len(problem_scores)):
            return "沉淀复用驱动型"
        return "效果数据平衡型"

    def _calculate_activity_heatmap(self, pages: List[Path]) -> Dict[str, Dict]:
        if self.trail is None:
            try:
                from core.kia.ariadne import KnowledgeTrail
                self.trail = KnowledgeTrail(wiki_base=str(self.wiki_base))
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at metis.py", exc_info=True)
                return {}

        heatmap = {}
        for page in pages:
            stats = self._get_page_stats(page)
            if stats:
                heatmap[page.stem] = {
                    "query_count": int(stats.get("total_queries") or stats.get("access_count") or 0),
                    "modify_count": int(stats.get("total_modifications") or 0),
                    "effect_score": float(stats.get("effect_score") or 0.0),
                    "last_accessed": stats.get("last_accessed") or "",
                }
        return heatmap

    def _get_page_stats(self, page: Path) -> Dict:
        if hasattr(self.trail, "get_page_stats"):
            try:
                return dict(self.trail.get_page_stats(str(page)) or {})
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at metis.py", exc_info=True)
                return {}
        db_path = getattr(self.trail, "db_path", self.wiki_base / ".kg" / "trail.db")
        if not Path(db_path).exists():
            return {}
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM page_stats WHERE page_path=?", (str(page),)).fetchone()
                return dict(row) if row else {}
        except sqlite3.Error:
            return {}

    @staticmethod
    def _calculate_effect_distribution(activity_heatmap: Dict[str, Dict]) -> Dict[str, int]:
        buckets = {"高效果": 0, "中效果": 0, "低效果": 0}
        for stats in activity_heatmap.values():
            score = float(stats.get("effect_score") or 0.0)
            if score >= 0.7:
                buckets["高效果"] += 1
            elif score >= 0.4:
                buckets["中效果"] += 1
            else:
                buckets["低效果"] += 1
        return buckets

    def _calculate_task_distribution(self) -> Dict[str, int]:
        if not self.task_db.exists():
            return {}
        try:
            with sqlite3.connect(str(self.task_db)) as conn:
                rows = conn.execute(
                    "SELECT actual, predicted FROM classification_feedback"
                ).fetchall()
        except sqlite3.Error:
            return {}

        counter = Counter((actual or predicted or "unknown") for actual, predicted in rows)
        return dict(counter.most_common())

    def _calculate_health_trend(self) -> Dict[str, Any]:
        if self.immune is None:
            try:
                from core.kia.hygieia import KnowledgeImmuneSystem
                self.immune = KnowledgeImmuneSystem(wiki_base=str(self.wiki_base))
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at metis.py", exc_info=True)
                return {}
        try:
            report = self.immune.full_scan()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at metis.py", exc_info=True)
            return {}

        issue_breakdown = Counter(issue.issue_type for issue in getattr(report, "issues", []))
        critical_count = sum(1 for issue in getattr(report, "issues", []) if issue.severity == "critical")
        return {
            "current_score": getattr(report, "health_score", 100.0),
            "issue_count": len(getattr(report, "issues", [])),
            "issue_breakdown": dict(issue_breakdown),
            "critical_count": critical_count,
        }

    def _calculate_frontmatter_completeness(self, frontmatters: List[Dict]) -> float:
        if not frontmatters:
            return 0.0
        total = len(frontmatters) * len(self.REQUIRED_FRONTMATTER_FIELDS)
        present = sum(
            1
            for fm in frontmatters
            for field_name in self.REQUIRED_FRONTMATTER_FIELDS
            if fm.get(field_name) not in (None, "", [])
        )
        return round(present / max(total, 1), 3)

    @staticmethod
    def _calculate_update_ratio(pages: List[Path]) -> float:
        cutoff = datetime.now() - timedelta(days=30)
        updated = sum(1 for page in pages if datetime.fromtimestamp(page.stat().st_mtime) > cutoff)
        return round(updated / max(len(pages), 1), 3)

    @staticmethod
    def _calculate_domain_entropy(domain_dist: Dict[str, int]) -> float:
        total = sum(domain_dist.values())
        if total == 0 or len(domain_dist) <= 1:
            return 0.0
        entropy = -sum((count / total) * math.log(count / total) for count in domain_dist.values() if count > 0)
        return round(entropy / math.log(len(domain_dist)), 3)

    def _init_db(self):
        self.profile_db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.profile_db), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generated_at TEXT,
                    total_knowledge INTEGER,
                    domain_distribution TEXT,
                    form_distribution TEXT,
                    tool_stack TEXT,
                    scenario_tags TEXT,
                    complexity_distribution TEXT,
                    confidence_distribution TEXT,
                    temporal_distribution TEXT,
                    growth_trend TEXT,
                    learning_mode TEXT,
                    quality_score REAL,
                    activity_heatmap TEXT,
                    effect_distribution TEXT,
                    task_distribution TEXT,
                    health_trend TEXT,
                    blindspot_distribution TEXT,
                    frontmatter_completeness REAL,
                    update_ratio REAL,
                    domain_entropy REAL
                )
            """)

    def _persist_profile(self, profile: KnowledgeProfile):
        try:
            with sqlite3.connect(str(self.profile_db), timeout=10) as conn:
                conn.execute(
                    """INSERT INTO knowledge_profiles
                       (generated_at, total_knowledge, domain_distribution, form_distribution,
                        tool_stack, scenario_tags, complexity_distribution, confidence_distribution,
                        temporal_distribution, growth_trend, learning_mode, quality_score,
                        activity_heatmap, effect_distribution, task_distribution, health_trend,
                        blindspot_distribution, frontmatter_completeness, update_ratio, domain_entropy)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        profile.generated_at,
                        profile.total_knowledge,
                        json.dumps(profile.domain_distribution, ensure_ascii=False),
                        json.dumps(profile.form_distribution, ensure_ascii=False),
                        json.dumps(profile.tool_stack, ensure_ascii=False),
                        json.dumps(profile.scenario_tags, ensure_ascii=False),
                        json.dumps(profile.complexity_distribution, ensure_ascii=False),
                        json.dumps(profile.confidence_distribution, ensure_ascii=False),
                        json.dumps(profile.temporal_distribution, ensure_ascii=False),
                        json.dumps(profile.growth_trend, ensure_ascii=False),
                        json.dumps(profile.learning_mode, ensure_ascii=False),
                        profile.quality_score,
                        json.dumps(profile.activity_heatmap, ensure_ascii=False),
                        json.dumps(profile.effect_distribution, ensure_ascii=False),
                        json.dumps(profile.task_distribution, ensure_ascii=False),
                        json.dumps(profile.health_trend, ensure_ascii=False),
                        json.dumps(profile.blindspot_distribution, ensure_ascii=False),
                        profile.frontmatter_completeness,
                        profile.update_ratio,
                        profile.domain_entropy,
                    ),
                )
        except sqlite3.Error as exc:
            logger.warning(f"画像持久化失败: {exc}")

    def incremental_update(self, page_path: str, operation: str = "created"):
        fm = self._extract_frontmatter(Path(page_path))
        if not fm:
            return
        try:
            with sqlite3.connect(str(self.profile_db), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM knowledge_profiles ORDER BY generated_at DESC, id DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return
                domains = json.loads(row["domain_distribution"] or "{}")
                domain = fm.get("领域", "其他")
                total_delta = 1 if operation == "created" else 0
                if operation == "created":
                    domains[domain] = domains.get(domain, 0) + 1
                conn.execute(
                    """UPDATE knowledge_profiles
                       SET domain_distribution=?, total_knowledge=total_knowledge + ?
                       WHERE id=?""",
                    (json.dumps(domains, ensure_ascii=False), total_delta, row["id"]),
                )
        except sqlite3.Error as exc:
            logger.warning(f"画像增量更新失败: {exc}")

    @staticmethod
    def _extract_frontmatter(page: Path) -> Optional[Dict]:
        if yaml is None:
            return {}
        try:
            content = page.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1]) or {}
        except Exception as e:
            logger.warning(f"忽略异常: {e}")
        return {}

    @staticmethod
    def _get_keywords(frontmatter: Dict, layer: str) -> List[str]:
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            return keywords.get(layer, []) or []
        return []


# ========== 便捷函数 ==========

def generate_profile(wiki_base: str = None) -> str:
    """便捷函数：生成知识画像报告"""
    generator = ProfileGenerator(wiki_base=wiki_base)
    profile = generator.generate()
    return generator.generate_report(profile)
