"""
Internal Validator — 内部一致性校验器

核心职责：
不依赖任何用户反馈，系统自检 Insight 的质量和一致性。
这是"零用户负担"的校验层。

校验维度（纯数值计算，不依赖语义理解）：
1. 证据覆盖度：Insight 涉及的维度在 Mirror 中是否有足够证据
2. 时效合理性：证据的平均时效权重是否支撑 Insight 的时效性声称
3. 置信度一致性：Insight 置信度与证据链质量是否匹配
4. 维度独立性：不同维度的证据置信度差异是否过大（可能暗示矛盾）

设计原则：
- 所有检查都是代码层数值计算
- 不依赖 LLM 语义理解
- 结果作为"系统自检分"融入校准
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.reflection.insight_generator import InsightResult
from core.reflection.mirror_engine import MirrorResult


@dataclass
class ValidationFinding:
    """单个校验发现"""

    check_name: str
    status: str  # "pass", "warn", "fail"
    score: float  # 0.0 - 1.0
    message: str


@dataclass
class ValidationResult:
    """校验结果"""

    overall_score: float  # 0.0 - 1.0，综合自检分
    findings: List[ValidationFinding] = field(default_factory=list)
    passed: bool = True

    def to_feedback_equivalent(self) -> Optional[str]:
        """
        将校验结果转换为等效反馈类型

        高分 → 相当于 ACCURATE
        低分 → 相当于 INACCURATE / IRRELEVANT
        """
        if self.overall_score >= 0.75:
            return "accurate"
        elif self.overall_score <= 0.35:
            return "inaccurate"
        return None


class InternalValidator:
    """内部一致性校验器"""

    # 校验配置
    MIN_EVIDENCE_PER_DIM = 2  # 每个维度最少证据数
    MIN_AVG_RECENCY = 0.3  # 平均时效权重最低要求
    MAX_CONFIDENCE_GAP = 0.5  # 维度间置信度最大允许差异
    MIN_OVERALL_SCORE = 0.5  # 整体通过阈值

    def validate(self, mirror: MirrorResult, insight: InsightResult) -> ValidationResult:
        """
        执行内部一致性校验

        Args:
            mirror: Mirror 证据链
            insight: 生成的 Insight

        Returns:
            ValidationResult
        """
        findings = []

        # 1. 证据覆盖度校验
        findings.append(self._check_evidence_coverage(mirror, insight))

        # 2. 时效合理性校验
        findings.append(self._check_recency_validity(mirror))

        # 3. 置信度一致性校验
        findings.append(self._check_confidence_consistency(mirror, insight))

        # 4. 维度独立性校验
        findings.append(self._check_dimension_independence(mirror))

        # 5. 证据数量校验
        findings.append(self._check_evidence_quantity(mirror))

        # 计算综合得分
        overall = sum(f.score for f in findings) / len(findings) if findings else 0.0
        passed = overall >= self.MIN_OVERALL_SCORE

        return ValidationResult(
            overall_score=round(overall, 2),
            findings=findings,
            passed=passed,
        )

    def _check_evidence_coverage(
        self, mirror: MirrorResult, insight: InsightResult
    ) -> ValidationFinding:
        """
        证据覆盖度：Insight 声称的维度是否有 Mirror 证据支撑
        """
        if not insight.dimensions_involved:
            return ValidationFinding(
                check_name="evidence_coverage",
                status="warn",
                score=0.5,
                message="Insight 未声明涉及维度，无法校验覆盖度",
            )

        mirror_dims = set(mirror.dimensions_involved)
        insight_dims = set(insight.dimensions_involved)

        covered = insight_dims & mirror_dims
        uncovered = insight_dims - mirror_dims

        if uncovered:
            coverage_ratio = len(covered) / len(insight_dims)
            return ValidationFinding(
                check_name="evidence_coverage",
                status="fail" if coverage_ratio < 0.5 else "warn",
                score=coverage_ratio,
                message=f"Insight 声称涉及 {len(insight_dims)} 个维度，"
                f"但 Mirror 中只有 {len(covered)} 个有证据"
                f"（缺失: {', '.join(uncovered)}）",
            )

        return ValidationFinding(
            check_name="evidence_coverage",
            status="pass",
            score=1.0,
            message=f"所有声称维度（{len(insight_dims)}个）均有证据支撑",
        )

    def _check_recency_validity(self, mirror: MirrorResult) -> ValidationFinding:
        """
        时效合理性：证据的平均时效权重是否足够
        """
        if not mirror.snapshots:
            return ValidationFinding(
                check_name="recency_validity",
                status="fail",
                score=0.0,
                message="Mirror 中无任何证据",
            )

        avg_recency = sum(s.recency_weight for s in mirror.snapshots) / len(mirror.snapshots)

        if avg_recency < self.MIN_AVG_RECENCY:
            return ValidationFinding(
                check_name="recency_validity",
                status="fail",
                score=avg_recency,
                message=f"证据平均时效权重 {avg_recency:.2f} 过低（阈值 {self.MIN_AVG_RECENCY}），"
                "Insight 可能基于过时数据",
            )

        return ValidationFinding(
            check_name="recency_validity",
            status="pass",
            score=min(1.0, avg_recency / 0.7),  # 0.7 视为满分
            message=f"证据平均时效权重 {avg_recency:.2f}，数据新鲜度良好",
        )

    def _check_confidence_consistency(
        self, mirror: MirrorResult, insight: InsightResult
    ) -> ValidationFinding:
        """
        置信度一致性：Insight 置信度不应显著高于证据质量
        """
        if not mirror.snapshots:
            return ValidationFinding(
                check_name="confidence_consistency",
                status="fail",
                score=0.0,
                message="Mirror 中无证据，无法校验置信度一致性",
            )

        avg_evidence_conf = sum(s.confidence for s in mirror.snapshots) / len(mirror.snapshots)
        insight_conf = insight.confidence

        # Insight 置信度不应比证据平均置信度高太多
        gap = insight_conf - avg_evidence_conf

        if gap > 0.3:
            return ValidationFinding(
                check_name="confidence_consistency",
                status="warn",
                score=max(0.0, 1.0 - gap),
                message=f"Insight 置信度 ({insight_conf:.2f}) 显著高于"
                f"证据平均置信度 ({avg_evidence_conf:.2f})，"
                "可能存在过度推断",
            )

        return ValidationFinding(
            check_name="confidence_consistency",
            status="pass",
            score=1.0,
            message=f"Insight 置信度 ({insight_conf:.2f}) 与证据质量 ({avg_evidence_conf:.2f}) 一致",
        )

    def _check_dimension_independence(self, mirror: MirrorResult) -> ValidationFinding:
        """
        维度独立性：不同维度的证据置信度差异不应过大
        （差异过大可能暗示维度间存在矛盾）
        """
        if not mirror.snapshots:
            return ValidationFinding(
                check_name="dimension_independence",
                status="pass",
                score=1.0,
                message="无证据，跳过独立性检查",
            )

        # 按维度分组计算平均置信度
        dim_confs: Dict[str, List[float]] = {}
        for snap in mirror.snapshots:
            dim_confs.setdefault(snap.dimension, []).append(snap.confidence)

        if len(dim_confs) < 2:
            return ValidationFinding(
                check_name="dimension_independence",
                status="pass",
                score=1.0,
                message="仅涉及单维度，跳过独立性检查",
            )

        avg_by_dim = {d: sum(c) / len(c) for d, c in dim_confs.items()}
        max_conf = max(avg_by_dim.values())
        min_conf = min(avg_by_dim.values())
        gap = max_conf - min_conf

        if gap > self.MAX_CONFIDENCE_GAP:
            return ValidationFinding(
                check_name="dimension_independence",
                status="warn",
                score=max(0.0, 1.0 - gap),
                message=f"维度间置信度差异过大 ({gap:.2f})，"
                f"最高: {max_conf:.2f}，最低: {min_conf:.2f}，"
                "可能存在维度间矛盾",
            )

        return ValidationFinding(
            check_name="dimension_independence",
            status="pass",
            score=1.0,
            message=f"维度间置信度分布均匀（差异 {gap:.2f}）",
        )

    def _check_evidence_quantity(self, mirror: MirrorResult) -> ValidationFinding:
        """
        证据数量：每个涉及的维度应有足够证据
        """
        if not mirror.snapshots:
            return ValidationFinding(
                check_name="evidence_quantity",
                status="fail",
                score=0.0,
                message="Mirror 中无任何证据",
            )

        dim_counts: Dict[str, int] = {}
        for snap in mirror.snapshots:
            dim_counts[snap.dimension] = dim_counts.get(snap.dimension, 0) + 1

        under_threshold = [
            dim for dim, count in dim_counts.items() if count < self.MIN_EVIDENCE_PER_DIM
        ]

        if under_threshold:
            min_count = min(dim_counts[d] for d in under_threshold)
            return ValidationFinding(
                check_name="evidence_quantity",
                status="warn",
                score=min_count / self.MIN_EVIDENCE_PER_DIM,
                message=f"以下维度证据不足（< {self.MIN_EVIDENCE_PER_DIM} 条）: "
                f"{', '.join(under_threshold)}",
            )

        return ValidationFinding(
            check_name="evidence_quantity",
            status="pass",
            score=1.0,
            message=f"所有 {len(dim_counts)} 个维度证据充足（≥ {self.MIN_EVIDENCE_PER_DIM} 条）",
        )
