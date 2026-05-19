"""
Conflict Resolver - 多源知识冲突检测与仲裁引擎

职责：
- 在 Ingest 阶段（P2→P1 合并前）检测新知识与已有知识的冲突
- 语义级别断言对比（不是关键词匹配）
- 全自动仲裁：低冲突自动处理，中冲突自动仲裁，高冲突创建争议页面

设计原则：宁可漏检，不可误判；不删除旧知识，只添加边界或标记。
"""

import re
import math
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timedelta
from pathlib import Path

from core.kia.assertion_extractor import Assertion, KnowledgeForm


# ========== 数据结构 ==========

@dataclass
class Conflict:
    """检测到的冲突"""
    conflict_type: str            # temporal/contextual/authority/domain/self_ref
    strength: float               # 冲突强度 0-1
    new_assertion: Assertion
    existing_assertion: Assertion
    topic_overlap: float          # topic 语义重叠度 0-1
    direction_conflict: float     # 结论方向冲突度 0-1
    reason: str = ""              # 冲突原因描述


@dataclass
class Resolution:
    """仲裁结果"""
    action: str                   # update_boundary / supersede / create_dispute / no_action
    target: str                   # "new" | "existing" | "both"
    updates: Dict = field(default_factory=dict)   # 需要更新的字段
    reason: str = ""              # 仲裁理由
    dispute_page: str = ""        # 如果创建争议页面，记录页面 ID


@dataclass
class WikiPageMeta:
    """Wiki 页面的元数据（用于仲裁评分）"""
    page_id: str
    created_at: datetime
    updated_at: datetime
    evidence_level: str           # single-source / multi-source / curated
    verification_count: int
    verification_history: List[Dict] = field(default_factory=list)
    is_user_verified: bool = False
    source_type: str = ""         # chat / file / annotation


# ========== 冲突检测 ==========

# 反义词对（用于检测方向冲突）
ANTONYM_PAIRS = [
    ("用", "不用"), ("使用", "不使用"), ("应该", "不应该"),
    ("要", "不要"), ("可以", "不可以"), ("能", "不能"),
    ("有效", "无效"), ("正确", "错误"), ("好", "不好"),
    ("支持", "不支持"), ("兼容", "不兼容"),
    ("需要", "不需要"), ("必须", "不必"),
]

# 版本号模式
VERSION_PATTERN = re.compile(r'v?(\d+)\.?(\d+)?')


def _calculate_topic_overlap(a1: Assertion, a2: Assertion) -> float:
    """
    计算两个断言的 topic 重叠度
    简单实现：基于关键词重叠
    """
    # 提取关键词（中文2-4字词 + 英文3+字母词）
    def extract_keywords(text: str) -> set:
        zh = set(re.findall(r'[一-鿿]{2,4}', text))
        en = set(re.findall(r'[a-zA-Z]{3,}', text.lower()))
        return zh | en

    k1 = extract_keywords(a1.claim)
    k2 = extract_keywords(a2.claim)

    if not k1 or not k2:
        return 0.0

    intersection = k1 & k2
    union = k1 | k2

    if not union:
        return 0.0

    return len(intersection) / len(union)


def _calculate_direction_conflict(a1: Assertion, a2: Assertion) -> float:
    """
    计算两个断言的结论方向冲突度
    """
    c1 = a1.claim
    c2 = a2.claim

    # 1. 直接反义词检测
    for pos, neg in ANTONYM_PAIRS:
        if (pos in c1 and neg in c2) or (neg in c1 and pos in c2):
            return 0.9

    # 2. 否定断言 vs 正面断言
    if a1.is_negated != a2.is_negated:
        # 检查是否指向同一对象
        overlap = _calculate_topic_overlap(a1, a2)
        if overlap > 0.5:
            return 0.8 * overlap

    # 3. "A 比 B 好" vs "B 比 A 好"
    comparison_pattern = re.compile(r'(\S+).*(比|优于|好于|胜于|更适合).*(\S+)')
    m1 = comparison_pattern.search(c1)
    m2 = comparison_pattern.search(c2)
    if m1 and m2:
        # 如果比较对象互换，则是冲突
        obj1_a, obj1_b = m1.group(1), m1.group(3)
        obj2_a, obj2_b = m2.group(1), m2.group(3)
        if obj1_a == obj2_b and obj1_b == obj2_a:
            return 0.85

    # 4. 版本号冲突检测
    v1 = VERSION_PATTERN.findall(c1)
    v2 = VERSION_PATTERN.findall(c2)
    if v1 and v2:
        # 如果同一 topic 但版本号不同
        overlap = _calculate_topic_overlap(a1, a2)
        if overlap > 0.6:
            return 0.6

    # 5. 数值冲突检测（如 "0.1%" vs "1%"）
    nums1 = re.findall(r'\d+\.?\d*%?', c1)
    nums2 = re.findall(r'\d+\.?\d*%?', c2)
    if nums1 and nums2 and _calculate_topic_overlap(a1, a2) > 0.5:
        # 有数值差异且 topic 重叠
        return 0.5

    # 6. "用 A" vs "用 B" 冲突检测
    # 检测模式: [可选副词][动词] + [具体值/路径/方法]
    # 支持: "使用 /v1/..." 或 "应该使用 /v1/..."
    usage_pattern = re.compile(
        r'(?:应该|建议|推荐|要|选)?\s*(用|使用|采用)\s+([\w\-/\.:@#$%&*()+=\[\]{}|\\<>~`]+)',
        re.IGNORECASE
    )
    u1 = usage_pattern.findall(c1)
    u2 = usage_pattern.findall(c2)

    if u1 and u2 and _calculate_topic_overlap(a1, a2) > 0.3:
        # 提取推荐的具体值
        vals1 = [match[1].strip() for match in u1 if len(match[1].strip()) > 3]
        vals2 = [match[1].strip() for match in u2 if len(match[1].strip()) > 3]

        # 如果推荐的具体值不同，但 topic 重叠，则冲突
        if vals1 and vals2:
            for v1 in vals1:
                for v2 in vals2:
                    # 具体值不同（且不是简单的子串关系）
                    if v1 != v2:
                        # 检查是否是同一概念的不同表述（如 /api/v1 和 /v1）
                        # 按 / 分割后如果段不完全相同，则认为是冲突
                        segs1 = [s for s in v1.split("/") if s]
                        segs2 = [s for s in v2.split("/") if s]
                        if segs1 != segs2:
                            return 0.7

    return 0.0


def _classify_conflict_type(a1: Assertion, a2: Assertion,
                            topic_overlap: float,
                            direction_conflict: float) -> str:
    """分类冲突类型"""

    # 自我引用检测
    if "[[" in a1.claim or "[[" in a2.claim:
        return "self_ref"

    # 时间演化：同一 topic，版本号或时间标记不同
    if VERSION_PATTERN.search(a1.claim) or VERSION_PATTERN.search(a2.claim):
        return "temporal"

    # 领域差异：不同 domain 标签
    # 注意：这里需要外部传入 domain 信息，简化处理

    # 权威差异：evidence_level 不同
    evidence_levels = {"curated": 3, "multi-source": 2, "single-source": 1, "anecdote": 0}
    e1 = evidence_levels.get(a1.evidence_level, 1)
    e2 = evidence_levels.get(a2.evidence_level, 1)
    if abs(e1 - e2) >= 2 and direction_conflict > 0.5:
        return "authority"

    # 上下文差异：有边界条件提示
    if a1.boundary_hint or a2.boundary_hint:
        return "contextual"

    # 默认
    if direction_conflict > 0.5:
        return "contextual"

    return "temporal"


def detect_conflicts(new_assertions: List[Assertion],
                     existing_assertions: List[Assertion],
                     min_topic_overlap: float = 0.3) -> List[Conflict]:
    """
    检测新断言与已有断言之间的冲突

    Args:
        new_assertions: 新提取的断言
        existing_assertions: 已有知识库中的断言
        min_topic_overlap: 最小 topic 重叠度阈值

    Returns:
        Conflict 列表
    """
    conflicts = []

    for new_a in new_assertions:
        for exist_a in existing_assertions:
            # 1. 计算 topic 重叠
            topic_overlap = _calculate_topic_overlap(new_a, exist_a)
            if topic_overlap < min_topic_overlap:
                continue

            # 2. 计算方向冲突
            direction_conflict = _calculate_direction_conflict(new_a, exist_a)
            if direction_conflict < 0.3:
                continue

            # 3. 计算总冲突强度
            strength = (topic_overlap * 0.4 + direction_conflict * 0.6)

            # 4. 分类冲突类型
            conflict_type = _classify_conflict_type(
                new_a, exist_a, topic_overlap, direction_conflict
            )

            conflicts.append(Conflict(
                conflict_type=conflict_type,
                strength=strength,
                new_assertion=new_a,
                existing_assertion=exist_a,
                topic_overlap=topic_overlap,
                direction_conflict=direction_conflict,
                reason=f"{conflict_type}: '{new_a.claim[:50]}...' vs '{exist_a.claim[:50]}...'"
            ))

    return conflicts


# ========== 仲裁评分 ==========

EVIDENCE_WEIGHTS = {
    "curated": 1.0,
    "multi-source": 0.8,
    "single-source": 0.5,
    "anecdote": 0.3,
}

VERIFICATION_WEIGHTS = {
    0: 0.7,
    1: 1.0,
    2: 1.1,
    3: 1.2,
}

HALF_LIFE_DAYS = {
    "insight": 120,
    "knowledge": 90,
    "code": 60,
    "business": 45,
}

PERSONALIZATION_WEIGHTS = {
    "user_verified": 1.3,
    "ai_derived": 1.0,
    "external": 0.9,
}


def _calculate_arbitration_score(assertion: Assertion,
                                  meta: Optional[WikiPageMeta] = None) -> float:
    """
    计算断言的仲裁评分
    score = evidence_level × verification_count × recency × personalization
    """
    # 1. 证据级别
    evidence = EVIDENCE_WEIGHTS.get(assertion.evidence_level, 0.5)

    # 2. 验证次数
    verification_count = meta.verification_count if meta else 0
    verification = VERIFICATION_WEIGHTS.get(min(verification_count, 3), 0.7)

    # 3. 时效性
    recency = 1.0
    if meta and meta.updated_at:
        days_old = (datetime.now() - meta.updated_at).days
        half_life = 90  # 默认
        recency = math.exp(-days_old / half_life)

    # 4. 个性化
    personalization = 1.0
    if meta:
        if meta.is_user_verified:
            personalization = PERSONALIZATION_WEIGHTS["user_verified"]
        elif meta.source_type == "chat":
            personalization = PERSONALIZATION_WEIGHTS["ai_derived"]
        else:
            personalization = PERSONALIZATION_WEIGHTS["external"]

    score = evidence * verification * recency * personalization
    return round(score, 3)


# ========== 仲裁策略 ==========

def arbitrate(conflict: Conflict,
              new_meta: Optional[WikiPageMeta] = None,
              existing_meta: Optional[WikiPageMeta] = None) -> Resolution:
    """
    对单个冲突进行仲裁

    Args:
        conflict: 冲突对象
        new_meta: 新断言的元数据
        existing_meta: 已有断言的元数据

    Returns:
        Resolution 仲裁结果
    """
    strength = conflict.strength

    # 低冲突：自动处理
    if strength < 0.3:
        return _auto_resolve_low(conflict)

    # 中冲突：自动仲裁
    if strength < 0.7:
        return _auto_arbitrate_medium(conflict, new_meta, existing_meta)

    # 高冲突：创建争议页面
    return _create_dispute_high(conflict)


def _auto_resolve_low(conflict: Conflict) -> Resolution:
    """低冲突：加边界条件"""
    new_a = conflict.new_assertion
    exist_a = conflict.existing_assertion

    # 如果新断言有边界提示，加到已有断言
    if new_a.boundary_hint:
        return Resolution(
            action="update_boundary",
            target="existing",
            updates={"boundary_hint": new_a.boundary_hint},
            reason=f"低冲突：新断言提供边界条件 '{new_a.boundary_hint[:50]}...'"
        )

    # 如果已有断言有边界提示，加到新断言
    if exist_a.boundary_hint:
        return Resolution(
            action="update_boundary",
            target="new",
            updates={"boundary_hint": exist_a.boundary_hint},
            reason=f"低冲突：已有断言的边界条件适用于新断言"
        )

    # 默认：无操作
    return Resolution(
        action="no_action",
        target="both",
        reason="低冲突，无显著影响"
    )


def _auto_arbitrate_medium(conflict: Conflict,
                            new_meta: Optional[WikiPageMeta],
                            existing_meta: Optional[WikiPageMeta]) -> Resolution:
    """中冲突：按评分公式仲裁"""
    new_score = _calculate_arbitration_score(conflict.new_assertion, new_meta)
    exist_score = _calculate_arbitration_score(conflict.existing_assertion, existing_meta)

    if new_score > exist_score * 1.2:
        # 新断言显著优于旧断言
        return Resolution(
            action="supersede",
            target="existing",
            updates={
                "status": "deprecated",
                "superseded_by": conflict.new_assertion.claim[:50],
                "reason": f"被更高评分的新知识替代 (score: {new_score} vs {exist_score})"
            },
            reason=f"中冲突自动仲裁：新断言评分更高 ({new_score} > {exist_score})"
        )
    elif exist_score > new_score * 1.2:
        # 旧断言显著优于新断言
        return Resolution(
            action="update_boundary",
            target="new",
            updates={
                "boundary_hint": f"与已有知识冲突，已有知识评分更高 ({exist_score} vs {new_score})"
            },
            reason=f"中冲突自动仲裁：已有断言评分更高 ({exist_score} > {new_score})"
        )
    else:
        # 评分接近，合并边界条件
        return Resolution(
            action="update_boundary",
            target="both",
            updates={
                "note": f"两个断言评分接近，需进一步验证 (new: {new_score}, existing: {exist_score})"
            },
            reason=f"中冲突自动仲裁：评分接近，添加备注"
        )


def _create_dispute_high(conflict: Conflict) -> Resolution:
    """高冲突：创建争议页面"""
    dispute_id = f"dispute-{hash(conflict.new_assertion.claim) % 10000:04d}"

    return Resolution(
        action="create_dispute",
        target="both",
        dispute_page=dispute_id,
        updates={
            "status": "pending_user_review",
            "dispute_page": dispute_id,
        },
        reason=f"高冲突 (strength={conflict.strength:.2f})：'{conflict.new_assertion.claim[:50]}...' vs '{conflict.existing_assertion.claim[:50]}...'"
    )


# ========== 批量处理 ==========

def resolve_all_conflicts(conflicts: List[Conflict],
                          new_metas: Dict[str, WikiPageMeta] = None,
                          existing_metas: Dict[str, WikiPageMeta] = None) -> List[Resolution]:
    """
    批量仲裁所有冲突

    Args:
        conflicts: 冲突列表
        new_metas: 新断言的元数据映射 {claim_hash: meta}
        existing_metas: 已有断言的元数据映射 {claim_hash: meta}

    Returns:
        Resolution 列表
    """
    resolutions = []
    new_metas = new_metas or {}
    existing_metas = existing_metas or {}

    for conflict in conflicts:
        new_key = hash(conflict.new_assertion.claim)
        exist_key = hash(conflict.existing_assertion.claim)

        resolution = arbitrate(
            conflict,
            new_metas.get(new_key),
            existing_metas.get(exist_key)
        )
        resolutions.append(resolution)

    return resolutions


# ========== 争议页面生成 ==========

def generate_dispute_page(conflict: Conflict, resolution: Resolution) -> str:
    """生成争议页面的 Markdown 内容"""
    lines = []
    lines.append("---")
    lines.append(f"type: dispute")
    lines.append(f"status: pending_user_review")
    lines.append(f"conflict_type: {conflict.conflict_type}")
    lines.append(f"strength: {conflict.strength:.2f}")
    lines.append(f"created: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("---")
    lines.append("")
    lines.append(f"# 争议: {conflict.conflict_type}")
    lines.append("")
    lines.append("## 新断言")
    lines.append(f"> {conflict.new_assertion.claim}")
    lines.append(f"- 形态: {conflict.new_assertion.form.value}")
    lines.append(f"- 证据级别: {conflict.new_assertion.evidence_level}")
    lines.append("")
    lines.append("## 已有断言")
    lines.append(f"> {conflict.existing_assertion.claim}")
    lines.append(f"- 形态: {conflict.existing_assertion.form.value}")
    lines.append(f"- 证据级别: {conflict.existing_assertion.evidence_level}")
    lines.append("")
    lines.append("## 冲突分析")
    lines.append(f"- 强度: {conflict.strength:.2f}")
    lines.append(f"- Topic 重叠: {conflict.topic_overlap:.2f}")
    lines.append(f"- 方向冲突: {conflict.direction_conflict:.2f}")
    lines.append("")
    lines.append("## 请选择")
    lines.append("- [ ] 新断言正确，替换已有断言")
    lines.append("- [ ] 已有断言正确，拒绝新断言")
    lines.append("- [ ] 两者都正确，但适用场景不同")
    lines.append("- [ ] 两者都部分正确，需要合并")
    lines.append("")

    return "\n".join(lines)


# ========== CLI ==========

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Conflict Resolver")
    parser.add_argument("--test", action="store_true", help="运行测试示例")
    args = parser.parse_args()

    if args.test:
        # 测试示例
        existing = [
            Assertion(
                claim="Codex 用 /codex/v1/chat/completions 作为 endpoint",
                form=KnowledgeForm.PROBLEM_SOLUTION,
                evidence_level="single-source",
                source="old-session"
            )
        ]
        new = [
            Assertion(
                claim="Codex 应该使用 /v1/chat/completions 而非 /codex/v1/...",
                form=KnowledgeForm.PROBLEM_SOLUTION,
                evidence_level="multi-source",
                source="new-session"
            )
        ]

        conflicts = detect_conflicts(new, existing)
        print(f"检测到 {len(conflicts)} 个冲突:\n")

        for i, c in enumerate(conflicts, 1):
            print(f"[{i}] 类型: {c.conflict_type}, 强度: {c.strength:.2f}")
            print(f"    新: {c.new_assertion.claim}")
            print(f"    旧: {c.existing_assertion.claim}")

            resolution = arbitrate(c)
            print(f"    仲裁: {resolution.action} -> {resolution.target}")
            print(f"    理由: {resolution.reason}")
            print()


if __name__ == "__main__":
    main()
