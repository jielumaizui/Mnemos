"""
Persona Store - 画像存储与wiki frontmatter反写

职责：
- 将画像数据持久化到wiki（方案A：frontmatter）
- 全量扫描wiki，计算知识-画像匹配度
- 反写匹配度字段到知识条目
- 版本控制（保留历史，标注迭代）

核心设计：
- 画像即知识：用户画像存储为wiki页面
- 每条知识自带匹配度字段
- 老字段保留，标注superseded
"""
# Delphi — 德尔斐神庙 — 画像存储，神谕/画像的持久化
# 原模块: persona_store.py



import os
import re
import json
import yaml
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime

from .psyche import SignalStore, get_signal_store
from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
from .hamartia import BlindSpotProfile
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


# ========== 配置 ==========

def _get_wiki_dir():
    """Lazy-load wiki directory to avoid side effects at import time."""
    return get_config().wiki_dir


class _LazyPath:
    """Lazy path that evaluates get_config() only when accessed."""
    __slots__ = ('_segments',)
    def __init__(self, *segments):
        self._segments = segments
    def __truediv__(self, other):
        return _LazyPath(*self._segments, other)
    def __rtruediv__(self, other):
        raise NotImplementedError
    def _resolve(self):
        result = _get_wiki_dir()
        for seg in self._segments:
            result = result / seg
        return result
    def __str__(self):
        return str(self._resolve())
    def __repr__(self):
        return f"LazyPath({'/'.join(self._segments)})"
    def __fspath__(self):
        return str(self._resolve())
    def __getattr__(self, name):
        return getattr(self._resolve(), name)
    def __hash__(self):
        return hash(self._resolve())
    def __eq__(self, other):
        return self._resolve() == other


WIKI_DIR = _LazyPath()
PERSONA_PAGE_PATH = _LazyPath("01-People", "user-persona.md")
PERSONA_HISTORY_DIR = _LazyPath("01-People", "user-persona-history")


# ========== 知识匹配度计算 ==========

class KnowledgeAligner:
    """
    计算知识与用户画像的匹配度。

    三维匹配：
    1. preference_match: 偏好匹配（用户喜欢这种呈现方式吗）
    2. capability_match: 能力匹配（这条知识在用户的学习区吗）
    3. context_match: 情境匹配（当前session需要这条知识吗）
    """

    # 知识类型-偏好兼容性矩阵
    TYPE_PREFERENCE_MATRIX = {
        "decision": {
            "feasibility_first": 1.0,
            "cost_first": 0.3,
            "risk_averse": 0.7,
            "risk_seeking": 0.8,
        },
        "snippet": {
            "code_first": 1.0,
            "explanation_first": 0.2,
        },
        "pattern": {
            "methodology_oriented": 1.0,
            "problem_oriented": 0.6,
        },
        "pitfall": {
            "risk_averse": 1.0,
            "risk_seeking": 0.5,
        },
        "reference": {
            "depth_first": 0.8,
            "breadth_first": 1.0,
        },
        "todo": {
            "action_oriented": 1.0,
            "plan_oriented": 0.7,
        },
    }

    def __init__(self, persona: PreferenceProfile):
        self.persona = persona

    def calculate_alignment(self, wiki_page: Dict, session_context: Dict = None) -> Dict[str, float]:
        """
        计算单条知识与画像的三维匹配度。

        Args:
            wiki_page: 知识页面数据 {path, frontmatter, content_snippet}
            session_context: 当前session上下文

        Returns:
            {preference_match, capability_match, context_match, total}
        """
        alignment = {
            "preference_match": 0.5,
            "capability_match": 0.5,
            "context_match": 0.5,
            "total": 0.5,
        }

        frontmatter = wiki_page.get("frontmatter", {})
        page_type = frontmatter.get("type", "unknown")

        # 1. 偏好匹配
        alignment["preference_match"] = self._calc_preference_match(page_type, frontmatter)

        # 2. 能力匹配（i+1学习区理论）
        alignment["capability_match"] = self._calc_capability_match(frontmatter)

        # 3. 情境匹配
        if session_context:
            alignment["context_match"] = self._calc_context_match(wiki_page, session_context)

        # 综合（权重可调）
        weights = self._get_alignment_weights()
        alignment["total"] = (
            alignment["preference_match"] * weights["preference"] +
            alignment["capability_match"] * weights["capability"] +
            alignment["context_match"] * weights["context"]
        )

        return alignment

    def _calc_preference_match(self, page_type: str, frontmatter: Dict) -> float:
        """计算偏好匹配度"""
        # 从画像中提取偏好标签（简化版）
        value = self.persona.value

        # 根据页面类型和偏好兼容性
        matrix = self.TYPE_PREFERENCE_MATRIX.get(page_type, {})

        # 推断用户的偏好关键词（简化版）
        user_prefs = []
        if value.correctness_vs_efficiency > 0.6:
            user_prefs.append("feasibility_first")
        elif value.correctness_vs_efficiency < 0.4:
            user_prefs.append("cost_first")

        if value.innovation_vs_safety > 0.6:
            user_prefs.append("risk_seeking")
        elif value.innovation_vs_safety < 0.4:
            user_prefs.append("risk_averse")

        if not user_prefs:
            return 0.5

        # 取平均匹配度
        scores = [matrix.get(pref, 0.5) for pref in user_prefs]
        return sum(scores) / len(scores)

    def _calc_capability_match(self, frontmatter: Dict) -> float:
        """
        计算能力匹配度（i+1学习区理论）。

        - 用户已远超知识 → boredom（无聊区）→ 低分
        - 刚好在用户能力边缘 → sweet spot（学习区）→ 高分
        - 有点难但可触及 → stretch zone（拉伸区）→ 中高分
        - 太难 → panic zone（恐慌区）→ 低分
        """
        # 简化版：基于知识level和画像的复杂度推断
        level = frontmatter.get("level", "L2")
        try:
            level_num = int(re.search(r'L(\d+)', str(level)).group(1))
        except (AttributeError, ValueError):
            level_num = 2

        # 推断用户能力等级（从能量和认知雷达）
        user_level = self._estimate_user_level()

        gap = level_num - user_level

        if gap < -1:       # 知识太简单
            return 0.3
        elif gap == -1:    # 略简单，可复习
            return 0.6
        elif gap == 0:     # 学习区 sweet spot
            return 1.0
        elif gap == 1:     # 拉伸区
            return 0.7
        elif gap == 2:     # 有挑战
            return 0.4
        else:              # 太难了
            return 0.1

    def _calc_context_match(self, wiki_page: Dict, session_context: Dict) -> float:
        """计算情境匹配度"""
        score = 0.5

        frontmatter = wiki_page.get("frontmatter", {})
        page_tags = frontmatter.get("tags", [])

        # 任务类型匹配
        session_task = session_context.get("task_type", "")
        if session_task:
            task_parts = session_task.split("/")
            if any(tag in page_tags for tag in task_parts):
                score += 0.3

        # 工作目录匹配
        session_dir = session_context.get("working_dir", "")
        page_path = wiki_page.get("path", "")
        if session_dir and page_path:
            # 简单字符串匹配
            if any(part in page_path for part in session_dir.split("/")):
                score += 0.2

        # 最近查询历史匹配
        recent_queries = session_context.get("recent_queries", [])
        if recent_queries:
            if any(q in page_tags or q in page_path for q in recent_queries):
                score += 0.2

        return min(1.0, score)

    def _estimate_user_level(self) -> int:
        """估算用户能力等级（1-9）"""
        # 简化：基于能量雷达的专注深度和认知雷达的抽象能力
        energy_score = self.persona.energy.focus_depth
        cognitive_score = self.persona.cognitive.abstraction

        # 综合估算
        avg = (energy_score + cognitive_score) / 2
        return int(1 + avg * 8)  # 映射到1-9

    def _get_alignment_weights(self) -> Dict[str, float]:
        """获取匹配度权重"""
        # 可从画像中读取用户偏好的权重
        return {
            "preference": 0.3,
            "capability": 0.4,
            "context": 0.3,
        }


# ========== PersonaStore 类 ==========

class PersonaStore:
    """画像存储管理器"""

    def __init__(self, wiki_dir: Path = None, signal_store: SignalStore = None):
        self.wiki_dir = wiki_dir or WIKI_DIR
        self.signal_store = signal_store or get_signal_store()
        self.persona_page = self.wiki_dir / "01-People" / "user-persona.md"
        self.history_dir = self.wiki_dir / "01-People" / "user-persona-history"
        self.history_dir.mkdir(parents=True, exist_ok=True)

    # ---- 画像读写 ----

    def save_persona(self, profile: PreferenceProfile, blindspot: BlindSpotProfile = None):
        """
        保存画像到wiki。

        1. 更新当前画像页面
        2. 备份旧版本到history
        3. 保存到数据库
        """
        # 1. 备份旧版本
        if self.persona_page.exists():
            self._backup_current_version()

        # 2. 生成画像页面内容
        content = self._generate_persona_page(profile, blindspot)

        # 3. 写入wiki
        self.persona_page.parent.mkdir(parents=True, exist_ok=True)
        self.persona_page.write_text(content, encoding="utf-8")

        # 4. 保存到数据库
        self.signal_store.save_persona_version(
            version=profile.version,
            period_start=profile.period_start,
            period_end=profile.period_end,
            energy=asdict(profile.energy),
            cognitive=asdict(profile.cognitive),
            value=asdict(profile.value),
            blindspot=self._blindspot_to_dict(blindspot) if blindspot else {},
            signal_count=profile.signal_count,
        )

    def load_persona(self) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """加载当前画像。先尝试wiki，失败则从数据库回退，最后返回默认冷启动模板。"""
        # 1. 尝试从wiki加载
        if self.persona_page.exists():
            try:
                content = self.persona_page.read_text(encoding="utf-8")
                profile, bs = self._parse_persona_page(content)
                if profile is not None:
                    return profile, bs
            except Exception as e:
                logger.warning(f"忽略异常: {e}")

        # 2. 从数据库回退
        profile, bs = self._load_persona_from_db()
        if profile is not None:
            return profile, bs

        # 3. 冷启动：返回默认模板
        logger.info("[画像] 无历史画像，返回默认冷启动模板")
        return self._create_default_persona(), None

    def _create_default_persona(self) -> PreferenceProfile:
        """创建默认冷启动画像模板

        所有维度中性（0.5），confidence=0.0，标记所有维度为数据不足。
        这是一个合法的画像对象，可以被 KIA 和其他模块安全使用。
        """
        from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        all_energy_dims = ["focus_depth", "startup_difficulty", "endurance_mode",
                          "switching_flexibility", "recovery_cycle"]
        all_cognitive_dims = ["abstraction", "system_view", "skepticism",
                             "creativity", "deduction"]
        all_value_dims = ["correctness_vs_efficiency", "depth_vs_breadth",
                         "perfection_vs_completion", "innovation_vs_safety",
                         "autonomy_vs_collaboration"]

        return PreferenceProfile(
            version=0,
            generated_at=now,
            period_start=now,
            period_end=now,
            energy=EnergyProfile(
                focus_depth=0.5,
                startup_difficulty=0.5,
                endurance_mode=0.5,
                switching_flexibility=0.5,
                recovery_cycle=0.5,
                confidence=0.0,
                insufficient_dimensions=all_energy_dims,
            ),
            cognitive=CognitiveProfile(
                abstraction=0.5,
                system_view=0.5,
                skepticism=0.5,
                creativity=0.5,
                deduction=0.5,
                confidence=0.0,
                insufficient_dimensions=all_cognitive_dims,
            ),
            value=ValueProfile(
                correctness_vs_efficiency=0.5,
                depth_vs_breadth=0.5,
                perfection_vs_completion=0.5,
                innovation_vs_safety=0.5,
                autonomy_vs_collaboration=0.5,
                confidence=0.0,
                insufficient_dimensions=all_value_dims,
            ),
            signal_count=0,
        )

    def _load_persona_from_db(self) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """从数据库重建画像"""
        from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
        from .hamartia import BlindSpotProfile, BlindSpot

        latest = self.signal_store.get_latest_persona_version()
        if not latest:
            return None, None

        try:
            energy_data = latest.get("energy_profile", {})
            cognitive_data = latest.get("cognitive_profile", {})
            value_data = latest.get("value_profile", {})

            profile = PreferenceProfile(
                version=latest.get("version", 0),
                generated_at=latest.get("generated_at", ""),
                period_start=latest.get("period_start", ""),
                period_end=latest.get("period_end", ""),
                energy=EnergyProfile(**{k: v for k, v in energy_data.items()
                                        if k in EnergyProfile.__dataclass_fields__}),
                cognitive=CognitiveProfile(**{k: v for k, v in cognitive_data.items()
                                              if k in CognitiveProfile.__dataclass_fields__}),
                value=ValueProfile(**{k: v for k, v in value_data.items()
                                      if k in ValueProfile.__dataclass_fields__}),
                signal_count=latest.get("signal_count_used", 0),
            )

            # 盲区画像
            bs_data = latest.get("blindspot_profile", {})
            blindspot = None
            if bs_data:
                blindspot = BlindSpotProfile(
                    confirmed=[BlindSpot(**b) for b in bs_data.get("confirmed", [])],
                    suspected=[BlindSpot(**b) for b in bs_data.get("suspected", [])],
                    dismissed=[BlindSpot(**b) for b in bs_data.get("dismissed", [])],
                    total_challenges=bs_data.get("total_challenges", 0),
                    accepted_count=bs_data.get("accepted_count", 0),
                    ignored_count=bs_data.get("ignored_count", 0),
                    rejected_count=bs_data.get("rejected_count", 0),
                    acceptance_rate=bs_data.get("acceptance_rate", 0.0),
                    challenge_credit=bs_data.get("challenge_credit", 10.0),
                )

            return profile, blindspot
        except Exception:
            return None, None

    def _generate_persona_page(self, profile: PreferenceProfile, blindspot: BlindSpotProfile = None) -> str:
        """生成画像页面Markdown"""
        data = profile.to_dict()

        # 防御性处理：generated_at 可能是 datetime.date 对象
        generated_at = profile.generated_at
        if hasattr(generated_at, "isoformat"):
            generated_at = generated_at.isoformat()
        generated_at_str = str(generated_at)[:10]

        lines = [
            "---",
            f"type: user-persona",
            f"version: {profile.version}",
            f"generated_at: {generated_at_str}",
            f"period: {profile.period_start} ~ {profile.period_end}",
            f"signal_count: {profile.signal_count}",
            f"confidence_energy: {profile.energy.confidence:.2f}",
            f"confidence_cognitive: {profile.cognitive.confidence:.2f}",
            f"confidence_value: {profile.value.confidence:.2f}",
            f"insufficient_energy: {json.dumps(profile.energy.insufficient_dimensions or [])}",
            f"insufficient_cognitive: {json.dumps(profile.cognitive.insufficient_dimensions or [])}",
            f"insufficient_value: {json.dumps(profile.value.insufficient_dimensions or [])}",
            "---",
            "",
            "# 用户画像",
            "",
            "> ⚠️ **AI生成声明**：此画像由AI基于你的行为信号自动推断，"
            "不等同于你的真实人格，也不具备临床或职业评估效力。"
            "画像中的每一项都应被视为假设而非事实。",
            "",
            "> 🔄 **动态性**：画像随时间演化，重大生活变化（换工作、搬迁、角色转变）"
            "可能导致短期失真。建议每季度审视一次。",
            "",
            f"> 📊 **数据基础**：基于{profile.signal_count}条信号，"
            f"整体置信度：能量{profile.energy.confidence:.0%}/"
            f"认知{profile.cognitive.confidence:.0%}/"
            f"价值{profile.value.confidence:.0%}。",
            "",
            "## 能量模式（Layer 1: How you work）",
            "",
        ]

        for key, val in data["energy"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        lines.extend([
            "",
            "## 认知模式（Layer 2: How you think）",
            "",
        ])

        for key, val in data["cognitive"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        lines.extend([
            "",
            "## 价值优先级（Layer 3: What you care）",
            "",
        ])

        for key, val in data["value"].items():
            if key == "confidence":
                continue
            score = val["score"]
            label = val["label"]
            if score == "—":
                lines.append(f"- **{key}**: {label} ❌")
            else:
                lines.append(f"- **{key}**: {label} ({score:.2f})")

        # 盲区画像
        if blindspot:
            lines.extend([
                "",
                "## 盲区画像",
                "",
            ])

            if blindspot.confirmed:
                lines.append("### 已确认的盲区")
                for bs in blindspot.confirmed:
                    lines.append(f"- **{bs.type}**: {bs.description}")
                    lines.append(f"  - 置信度: {bs.confidence:.2f}, 挑战次数: {bs.challenge_count}")

            if blindspot.suspected:
                lines.append("### 待验证的盲区")
                for bs in blindspot.suspected:
                    lines.append(f"- **{bs.type}**: {bs.description}")
                    lines.append(f"  - 置信度: {bs.confidence:.2f}")

            lines.extend([
                "",
                f"### 挑战统计",
                f"- 总挑战次数: {blindspot.total_challenges}",
                f"- 接受: {blindspot.accepted_count} | 忽略: {blindspot.ignored_count} | 拒绝: {blindspot.rejected_count}",
                f"- 接受率: {blindspot.acceptance_rate:.1%}",
                f"- 当前信用: {blindspot.challenge_credit:.1f}/{blindspot.credit_max}",
            ])

        lines.extend([
            "",
            "---",
            "",
            "*此画像由 AI 自动分析生成，每季度更新一次。*",
        ])

        return "\n".join(lines)

    def _parse_persona_page(self, content: str) -> Tuple[Optional[PreferenceProfile], Optional[BlindSpotProfile]]:
        """解析画像页面。从markdown中提取分数重建PreferenceProfile。"""
        from .pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
        from .hamartia import BlindSpotProfile

        if not content.startswith("---"):
            return None, None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None, None

        try:
            fm = yaml.safe_load(parts[1]) or {}
        except Exception:
            return None, None

        # 从markdown列表项提取分数，格式：- **key**: label (0.85)
        body = parts[2]
        scores = {}
        for match in re.finditer(r'\*\*(\w+)\*\*:.+\(([\d.]+)\)', body):
            scores[match.group(1)] = float(match.group(2))

        # 构建画像（yaml.safe_load 会把日期解析为 datetime.date，需要转回字符串）
        generated_at = fm.get("generated_at", "")
        if hasattr(generated_at, "isoformat"):
            generated_at = generated_at.isoformat()

        profile = PreferenceProfile(
            version=fm.get("version", 0),
            generated_at=str(generated_at),
            period_start=fm.get("period", "").split(" ~ ")[0] if "~" in fm.get("period", "") else "",
            period_end=fm.get("period", "").split(" ~ ")[1] if "~" in fm.get("period", "") else "",
            energy=EnergyProfile(
                focus_depth=scores.get("focus_depth", 0.5),
                startup_difficulty=scores.get("startup_difficulty", 0.5),
                endurance_mode=scores.get("endurance_mode", 0.5),
                switching_flexibility=scores.get("switching_flexibility", 0.5),
                recovery_cycle=scores.get("recovery_cycle", 0.5),
                confidence=fm.get("confidence_energy", 0.0),
                insufficient_dimensions=fm.get("insufficient_energy", []),
            ),
            cognitive=CognitiveProfile(
                abstraction=scores.get("abstraction", 0.5),
                system_view=scores.get("system_view", 0.5),
                skepticism=scores.get("skepticism", 0.5),
                creativity=scores.get("creativity", 0.5),
                deduction=scores.get("deduction", 0.5),
                confidence=fm.get("confidence_cognitive", 0.0),
                insufficient_dimensions=fm.get("insufficient_cognitive", []),
            ),
            value=ValueProfile(
                correctness_vs_efficiency=scores.get("correctness_vs_efficiency", 0.5),
                depth_vs_breadth=scores.get("depth_vs_breadth", 0.5),
                perfection_vs_completion=scores.get("perfection_vs_completion", 0.5),
                innovation_vs_safety=scores.get("innovation_vs_safety", 0.5),
                autonomy_vs_collaboration=scores.get("autonomy_vs_collaboration", 0.5),
                confidence=fm.get("confidence_value", 0.0),
                insufficient_dimensions=fm.get("insufficient_value", []),
            ),
            signal_count=fm.get("signal_count", 0),
        )

        return profile, None  # 盲区画像暂不从此解析

    def _backup_current_version(self):
        """备份当前画像版本"""
        if not self.persona_page.exists():
            return

        # 读取版本号
        try:
            content = self.persona_page.read_text(encoding="utf-8")
            version_match = re.search(r'version:\s*(\d+)', content)
            version = int(version_match.group(1)) if version_match else 0
        except Exception:
            version = 0

        backup_path = self.history_dir / f"user-persona-v{version}.md"
        backup_path.write_text(self.persona_page.read_text(encoding="utf-8"), encoding="utf-8")

    def _blindspot_to_dict(self, profile: BlindSpotProfile) -> Dict:
        """盲区画像转字典"""
        if not profile:
            return {}
        return {
            "confirmed": [asdict(b) for b in profile.confirmed],
            "suspected": [asdict(b) for b in profile.suspected],
            "dismissed": [asdict(b) for b in profile.dismissed],
            "total_challenges": profile.total_challenges,
            "accepted_count": profile.accepted_count,
            "ignored_count": profile.ignored_count,
            "rejected_count": profile.rejected_count,
            "acceptance_rate": profile.acceptance_rate,
            "challenge_credit": profile.challenge_credit,
        }

    # ---- 知识库反写 ----

    def align_all_wiki_pages(self, persona: PreferenceProfile, session_context: Dict = None,
                             dry_run: bool = False) -> Dict[str, int]:
        """
        全量扫描wiki，计算匹配度并反写frontmatter。

        Args:
            persona: 当前画像
            session_context: 可选的session上下文
            dry_run: 如果True，只计算不写入

        Returns:
            统计信息 {scanned, updated, skipped}
        """
        aligner = KnowledgeAligner(persona)
        stats = {"scanned": 0, "updated": 0, "skipped": 0}

        if not self.wiki_dir.exists():
            return stats

        for md_file in self.wiki_dir.rglob("*.md"):
            # 跳过画像页面本身
            if md_file.name == "user-persona.md":
                continue

            stats["scanned"] += 1

            try:
                content = md_file.read_text(encoding="utf-8")
                frontmatter = self._extract_frontmatter(content)

                if frontmatter is None:
                    stats["skipped"] += 1
                    continue

                # 计算匹配度
                wiki_page = {
                    "path": str(md_file.relative_to(self.wiki_dir)),
                    "frontmatter": frontmatter,
                    "content_snippet": content[:500],
                }
                alignment = aligner.calculate_alignment(wiki_page, session_context)

                if dry_run:
                    continue

                # 更新frontmatter
                new_content = self._update_persona_frontmatter(
                    content, frontmatter, alignment, persona.version
                )

                if new_content != content:
                    md_file.write_text(new_content, encoding="utf-8")
                    stats["updated"] += 1

            except Exception:
                stats["skipped"] += 1
                continue

        return stats

    def _extract_frontmatter(self, content: str) -> Optional[Dict]:
        """提取frontmatter"""
        if not content.startswith("---"):
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        try:
            return yaml.safe_load(parts[1]) or {}
        except Exception:
            return None

    def _update_persona_frontmatter(self, content: str, frontmatter: Dict,
                                    alignment: Dict, persona_version: int) -> str:
        """
        更新知识条目的画像frontmatter字段。

        策略：
        1. 如果已有persona_current，移到persona_history
        2. 写入新的persona_current
        """
        if not content.startswith("---"):
            return content

        parts = content.split("---", 2)
        if len(parts) < 3:
            return content

        # 处理旧的persona_current
        if "persona_current" in frontmatter:
            old_current = frontmatter["persona_current"]
            if "persona_history" not in frontmatter:
                frontmatter["persona_history"] = []

            # 标记为superseded
            old_current["status"] = "superseded"
            old_current["superseded_at"] = datetime.now().isoformat()[:10]
            old_current["superseded_by"] = persona_version

            frontmatter["persona_history"].append(old_current)

            # 限制历史记录数量（保留最近5个版本）
            if len(frontmatter["persona_history"]) > 5:
                frontmatter["persona_history"] = frontmatter["persona_history"][-5:]

        # 写入新的persona_current
        frontmatter["persona_current"] = {
            "version": persona_version,
            "updated_at": datetime.now().isoformat()[:10],
            "preference_alignment": {
                "score": round(alignment["preference_match"], 2),
            },
            "capability_alignment": {
                "score": round(alignment["capability_match"], 2),
                "difficulty_for_user": "boredom" if alignment["capability_match"] < 0.3 else
                                        "sweet_spot" if alignment["capability_match"] > 0.8 else
                                        "stretch_zone" if alignment["capability_match"] > 0.5 else
                                        "panic_zone",
            },
            "context_alignment": {
                "score": round(alignment["context_match"], 2),
            },
            "total_alignment": round(alignment["total"], 2),
        }

        # 重新生成frontmatter
        new_frontmatter = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
        return f"---\n{new_frontmatter}---{parts[2]}"


# ========== 便捷函数 ==========

def save_persona_to_wiki(profile: PreferenceProfile, blindspot: BlindSpotProfile = None):
    """便捷函数：保存画像到wiki"""
    store = PersonaStore()
    store.save_persona(profile, blindspot)
    print(f"✅ 画像已保存到 wiki: {store.persona_page}")


def align_wiki_with_persona(persona: PreferenceProfile, dry_run: bool = False) -> Dict:
    """便捷函数：全量反写wiki匹配度"""
    store = PersonaStore()
    stats = store.align_all_wiki_pages(persona, dry_run=dry_run)
    print(f"✅ Wiki扫描完成: {stats['scanned']} 条, 更新 {stats['updated']} 条, 跳过 {stats['skipped']} 条")
    return stats


# 便捷函数
_persona_store_instance = None

def get_persona_store() -> PersonaStore:
    """获取全局 PersonaStore 实例"""
    global _persona_store_instance
    if _persona_store_instance is None:
        _persona_store_instance = PersonaStore()
    return _persona_store_instance

# ========== Agent 适配画像策略 ==========

# A/B 测试状态：每个进程只确定一次
_ab_test_persona_driven: Optional[bool] = None


def _ensure_ab_test_group() -> bool:
    """确保 A/B 测试分组已确定"""
    global _ab_test_persona_driven
    if _ab_test_persona_driven is None:
        import random
        _ab_test_persona_driven = random.random() < 0.5
    return _ab_test_persona_driven


def _load_base_behavior_prompt() -> str:
    """加载基础画像策略（所有 Agent 通用）"""
    try:
        if not _ensure_ab_test_group():
            return "\n[Persona-Driven Behavior]\n- A/B 对照组：本次 session 不使用画像驱动策略"

        pstore = PersonaStore()
        profile, _ = pstore.load_persona()
        if not profile:
            return ""

        lines = ["\n[Persona-Driven Behavior]"]
        ins_energy = set(profile.energy.insufficient_dimensions or [])
        ins_cognitive = set(profile.cognitive.insufficient_dimensions or [])
        ins_value = set(profile.value.insufficient_dimensions or [])

        # 能量层映射
        if "focus_depth" not in ins_energy:
            fd = profile.energy.focus_depth
            if fd > 0.6:
                lines.append("- 用户专注深度高：提供结构化、层次化的深度回复，避免碎片化信息")
            elif fd < 0.4:
                lines.append("- 用户专注深度低：提供简短、可快速消化的信息，多用列表和要点")

        if "startup_difficulty" not in ins_energy:
            sd = profile.energy.startup_difficulty
            if sd > 0.6:
                lines.append("- 用户启动难度高：主动提供框架、模板或选项，降低决策成本")
            elif sd < 0.4:
                lines.append("- 用户启动容易：可以用开放性问题开场，给用户更多探索空间")

        if "switching_flexibility" not in ins_energy:
            sf = profile.energy.switching_flexibility
            if sf > 0.6:
                lines.append("- 用户切换弹性高：允许话题自然切换，不必强行锁定当前主题")
            elif sf < 0.4:
                lines.append("- 用户切换弹性低：坚持当前主线，切换话题时明确提示和确认")

        # 认知层映射
        if "abstraction" not in ins_cognitive:
            ab = profile.cognitive.abstraction
            if ab > 0.6:
                lines.append("- 用户偏抽象思维：先说原理/框架，再用案例佐证")
            elif ab < 0.4:
                lines.append("- 用户偏具象思维：先给具体案例，再归纳原理")

        if "system_view" not in ins_cognitive:
            sv = profile.cognitive.system_view
            if sv > 0.6:
                lines.append("- 用户偏好系统视角：先给全貌和关联，再深入细节")
            elif sv < 0.4:
                lines.append("- 用户偏好单点视角：聚焦当前问题，全局背景简要提及")

        if "skepticism" not in ins_cognitive:
            sk = profile.cognitive.skepticism
            if sk > 0.6:
                lines.append("- 用户质疑倾向强：主动展示推理过程、证据和局限性")
            elif sk < 0.4:
                lines.append("- 用户信任倾向强：直接给结论和建议，不必过度解释前提")

        # 价值层映射
        if "correctness_vs_efficiency" not in ins_value:
            ce = profile.value.correctness_vs_efficiency
            if ce > 0.6:
                lines.append("- 用户重视正确性：确保信息准确，不确定时明确说明")
            elif ce < 0.4:
                lines.append("- 用户重视效率：快速给出可行方案，不必追求完美")

        if "perfection_vs_completion" not in ins_value:
            pc = profile.value.perfection_vs_completion
            if pc > 0.6:
                lines.append("- 用户追求完美：提供详尽、完整的方案，考虑边界情况")
            elif pc < 0.4:
                lines.append("- 用户追求完成：先给 MVP 方案，细节后续补充")

        if "depth_vs_breadth" not in ins_value:
            db = profile.value.depth_vs_breadth
            if db > 0.6:
                lines.append("- 用户偏好深度：深入一个点，不必面面俱到")
            elif db < 0.4:
                lines.append("- 用户偏好广度：提供多种选择和视角，不必深入每个细节")

        if len(lines) > 1:
            return "\n".join(lines)
        return ""
    except Exception:
        return ""


def get_behavior_prompt(agent: str) -> str:
    """根据用户画像和 Agent 类型生成行为策略提示

    Args:
        agent: Agent 标识（claude/hermes/openclaw/opencode/codex）

    Returns:
        画像驱动的行为策略文本（含 Agent 特定策略）
    """
    base = _load_base_behavior_prompt()
    if not base:
        return ""

    lines = [base]

    # Agent 特定策略
    agent_notes = {
        "claude": "[Agent Note] 你当前使用 Claude Code，擅长深度技术讨论和代码分析。画像策略叠加：在技术讨论中优先提供深度分析，代码审查时关注架构设计和边界 case。",
        "hermes": "[Agent Note] 你当前使用 Hermes，擅长快速信息检索和多源搜索。画像策略叠加：在检索时优先返回高置信度来源，根据用户偏好调整信息密度（深度/广度）。",
        "openclaw": "[Agent Note] 你当前使用 OpenClaw，擅长分析和推理。画像策略叠加：在推理过程中主动展示思考链，根据用户质疑倾向调整论证详略。",
        "opencode": "[Agent Note] 你当前使用 OpenCode，擅长代码理解和生成。画像策略叠加：在代码生成时根据用户追求完美/完成的倾向调整详尽程度。",
        "codex": "[Agent Note] 你当前使用 Codex，专注代码生成任务。画像策略叠加：快速生成可运行代码，根据用户效率偏好提供简洁实现或完整方案。",
    }
    note = agent_notes.get(agent, "")
    if note:
        lines.append(note)
    return "\n".join(lines)


if __name__ == "__main__":
    # 测试
    store = PersonaStore()
    print(f"✅ PersonaStore initialized")
    print(f"   Wiki目录: {store.wiki_dir}")
    print(f"   画像页面: {store.persona_page}")
