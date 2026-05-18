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
import re
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import Counter
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


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
    learning_mode: str = ""
    quality_score: float = 0.0


class ProfileGenerator:
    """画像生成器"""

    def __init__(self, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.inbox = self.wiki_base / "00-Inbox"

    def generate(self) -> KnowledgeProfile:
        """生成完整画像"""
        profile = KnowledgeProfile(generated_at=datetime.now().isoformat()[:10])

        if not self.inbox.exists():
            return profile

        pages = list(self.inbox.glob("*.md"))
        profile.total_knowledge = len(pages)

        # 收集所有 frontmatter
        all_frontmatters = []
        for page in pages:
            fm = self._extract_frontmatter(page)
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
        profile.learning_mode = self._detect_learning_mode(profile.form_distribution)

        # 10. 质量评分
        profile.quality_score = self._calculate_quality_score(
            confidences, all_frontmatters
        )

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
        lines.append(f"**{profile.learning_mode}**")

        lines.extend(["", "## 质量评分", ""])
        lines.append(f"综合质量分: **{profile.quality_score:.1f}** / 10")

        if profile.growth_trend:
            lines.extend(["", "## 增长趋势", ""])
            for point in profile.growth_trend[-6:]:
                lines.append(f"- {point['month']}: +{point['count']} 条")

        return "\n".join(lines)

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
        """计算月度增长趋势"""
        monthly = Counter()
        for page in pages:
            fm = self._extract_frontmatter(page)
            if fm:
                created = fm.get("创建日期", "")
                if created and isinstance(created, str):
                    try:
                        month = created[:7]  # YYYY-MM
                        monthly[month] += 1
                    except Exception:
                        continue

        return [
            {"month": m, "count": c}
            for m, c in sorted(monthly.items())
        ]

    def _detect_learning_mode(self, form_dist: Dict[str, int]) -> str:
        """检测学习模式"""
        problem_solving = form_dist.get("问题-解决", 0)
        anti_pattern = form_dist.get("反模式", 0)
        methodology = form_dist.get("方法论", 0)
        heuristic = form_dist.get("经验法则", 0)
        decision = form_dist.get("决策记录", 0)

        reactive = problem_solving + anti_pattern
        proactive = methodology + heuristic + decision

        if proactive > reactive * 2:
            return "方法论导向型 — 偏好沉淀可复用的流程和原则"
        elif reactive > proactive * 2:
            return "问题导向型 — 主要在遇到问题时记录解决方案"
        else:
            return "平衡型 — 既有问题驱动也有主动沉淀"

    def _calculate_quality_score(self, confidences: List[float],
                                  frontmatters: List[Dict]) -> float:
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

        # 时效性加分（永久/稳定知识占比高更好）
        temporal_scores = {"永久": 1.0, "稳定": 0.8, "版本绑定": 0.5, "上下文相关": 0.3}
        temporal_bonus = sum(
            temporal_scores.get(fm.get("时效性"), 0.5)
            for fm in frontmatters
        ) / max(len(frontmatters), 1)
        score += temporal_bonus * 2

        return min(score, 10.0)

    def _has_complete_keywords(self, fm: Dict) -> bool:
        """检查关键词是否完整"""
        keywords = fm.get("关键词", {})
        if isinstance(keywords, dict):
            layers = ["核心概念", "场景标签", "工具实体", "动作标签"]
            return sum(1 for l in layers if keywords.get(l)) >= 3
        return False

    @staticmethod
    def _extract_frontmatter(page: Path) -> Optional[Dict]:
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
