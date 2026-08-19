"""
RuleScorer - 硬编码规则评分器

从现有散落的规则逻辑中提取、统一为独立模块。
作为 AdaptiveScorerV2 的 COLD 阶段兜底方案，也是 BayesianScorer 的基线对比。

设计原则：
- 纯规则，零 ML，零 API 调用
- 每条规则独立、可开关、可调权重
- 输出 0-1 分数 + 可解释的理由列表
- 不替代任何现有流程，只提供评分出口

提取来源：
- is_noise_message() → noise_penalty 规则
- score_message_quality() → quality_score 规则
- DistillSelfCheck（设计意图）→ completeness_penalty 规则
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Tuple

from core.config import get_config  # compatibility seam for isolated test fixtures
from core.frontmatter import normalize_frontmatter
from core.kia._quality_scoring import (
    _compute_density_score,
    _compute_length_score,
    _compute_richness_score,
    _extract_words,
)

# Constants extracted from magic numbers

logger = logging.getLogger(__name__)


# ==================== 共享规则权重存储 ====================


class RuleWeightStore:
    """Historical rule-weight API boundary.

    COG-048 makes ``rule_weights.db`` a migration-only historical asset. A
    caller cannot load or publish optimizer-derived weights without a governed
    training run and reciprocal receipt, so every pre-cutover operation fails
    closed and construction is strictly read-only.
    """

    DB_NAME = "rule_weights.db"

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else None

    @staticmethod
    def _retired(operation: str) -> NoReturn:
        raise PermissionError(f"training_admission_receipt_required:{operation}")

    def load_rules(self) -> Dict[str, float]:
        """Reject loading ungoverned historical rule weights."""

        self._retired("load_rule_weights")

    def save_rules(self, weights: Dict[str, float]) -> None:
        """Reject publishing weights outside a governed run."""

        del weights
        self._retired("save_rule_weights")

    def load_dimensions(self) -> Dict[str, Dict]:
        """Reject loading ungoverned Layer5 dimension weights."""

        self._retired("load_layer5_dimension_weights")

    def save_dimensions(
        self,
        weights: Dict[str, float],
        stats: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Reject publishing Layer5 weights outside a governed run."""

        del weights, stats
        self._retired("save_layer5_dimension_weights")


_rule_weight_store_instance: Optional[RuleWeightStore] = None


def get_rule_weight_store(db_path: Optional[Path] = None) -> RuleWeightStore:
    """进程级 RuleWeightStore 单例。"""
    global _rule_weight_store_instance
    if _rule_weight_store_instance is None:
        _rule_weight_store_instance = RuleWeightStore(db_path=db_path)
    return _rule_weight_store_instance


# ==================== 常量定义（从 ingest_helpers 提取）====================

_NOISE_RE_PATTERNS = [
    re.compile(r'^[\s\uff0c\u3002\uff01\uff1f,.!?；：:""' "()（）[]{}【】]+$"),
    re.compile(r"^[\u2764\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf\s]+$"),
]

_NOISE_PHRASES = {
    "好的",
    "好",
    "ok",
    "okay",
    "继续",
    "嗯",
    "啊",
    "哦",
    "行",
    "可以",
    "明白",
    "知道了",
    "了解",
    "收到",
    "对的",
    "没错",
    "是的",
    "嗯嗯",
    "谢谢",
    "多谢",
    "辛苦了",
    "拜托",
    "麻烦了",
    "yes",
    "yep",
    "yeah",
    "no",
    "nope",
    "nah",
    "go on",
    "go ahead",
    "next",
    "proceed",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "got it",
    "gotcha",
    "understood",
    "roger",
    "开始吧",
    "来吧",
    "动手吧",
    "搞起",
}

# frontmatter type 枚举（来自接口契约）
_VALID_TYPES = {"concept", "person", "project", "technology", "MOC", "retrospective"}

# frontmatter 必填字段
_REQUIRED_FRONTMATTER_FIELDS = {"type", "name", "domain"}


@dataclass
class RuleResult:
    """单条规则的评分结果"""

    rule_name: str
    score: float  # 0.0 ~ 1.0
    weight: float  # 该规则在总评分中的权重
    reasons: List[str] = field(default_factory=list)


# ==================== 独立规则函数 ====================


def noise_penalty(content: str, min_length: int = 4) -> RuleResult:
    """
    噪声惩罚规则（改造自 is_noise_message）
    原函数返回 bool（是/否噪声），现在返回 0-1 的惩罚分数：
    - 1.0 = 完全不是噪声（高质量内容）
    - 0.0 = 完全是噪声（应跳过）
    """
    reasons = []

    if not content or not isinstance(content, str):
        return RuleResult("noise_penalty", 0.0, 0.15, ["空内容或非字符串"])

    stripped = content.strip()

    # 空内容
    if not stripped:
        return RuleResult("noise_penalty", 0.0, 0.15, ["空内容"])

    penalty = 0.0

    # 过短
    if len(stripped) < min_length:
        penalty += 0.4
        reasons.append(f"过短({len(stripped)} < {min_length})")
    elif len(stripped) < 20:
        penalty += 0.1
        reasons.append(f"偏短({len(stripped)} < 20)")

    # 纯标点 / 纯 emoji
    for pattern in _NOISE_RE_PATTERNS:
        if pattern.match(stripped):
            penalty += 0.5
            reasons.append("纯标点/纯emoji")
            break

    # 敷衍短语
    core = re.sub(r"[^\w\u4e00-\u9fa5]", "", stripped).lower()
    if core in _NOISE_PHRASES:
        penalty += 0.4
        reasons.append(f"敷衍短语: {core}")

    # 重复字符
    if len(set(stripped)) <= 3 and len(stripped) >= 6:
        penalty += 0.3
        reasons.append("重复字符")

    score = max(0.0, 1.0 - min(penalty, 1.0))

    if not reasons:
        reasons.append("无明显噪声特征")

    return RuleResult("noise_penalty", round(score, 2), 0.15, reasons)


def quality_score(content: str) -> RuleResult:
    """
    内容质量评分规则（改造自 score_message_quality）
    原函数返回 0-100，现在归一化为 0-1
    """
    if not content or not isinstance(content, str):
        return RuleResult("quality_score", 0.0, 0.30, ["空内容"])

    stripped = content.strip()
    char_count = len(stripped)
    words = _extract_words(stripped)
    if not words:
        return RuleResult("quality_score", 0.0, 0.30, ["无有效词汇"])

    from core.kia._quality_scoring import _STOPWORDS_ZH

    length_score = _compute_length_score(char_count)
    density_score_raw, valid_words, total_words = _compute_density_score(words, _STOPWORDS_ZH)
    richness_score_raw, value_signals, _ = _compute_richness_score(stripped, words, total_words)

    total_raw = length_score + density_score_raw + richness_score_raw
    total_normalized = total_raw / 100.0

    reasons = [
        f"长度{length_score:.0f}/30",
        f"密度{density_score_raw:.0f}/35",
        f"丰富度{richness_score_raw:.0f}/35",
        f"有效词{valid_words}/{total_words}",
        f"价值信号{value_signals}",
    ]

    return RuleResult("quality_score", round(total_normalized, 2), 0.30, reasons)


def completeness_penalty(frontmatter: Dict, content: str = "") -> RuleResult:
    """
    蒸馏产物完整性惩罚规则（基于 DistillSelfCheck 设计意图）
    检查 frontmatter 必填字段和 type 合法性
    返回 0-1：1.0 = 完整合法，0.0 = 严重缺失
    """
    reasons = []
    penalty = 0.0

    if not frontmatter or not isinstance(frontmatter, dict):
        return RuleResult("completeness", 0.0, 0.20, ["无 frontmatter"])
    frontmatter = normalize_frontmatter(frontmatter)

    # 检查必填字段
    missing = _REQUIRED_FRONTMATTER_FIELDS - set(frontmatter.keys())
    if missing:
        penalty += len(missing) * 0.25
        reasons.append(f"缺失字段: {', '.join(missing)}")

    # 检查 type 合法性
    page_type = frontmatter.get("type", "")
    if page_type and page_type not in _VALID_TYPES:
        penalty += 0.3
        reasons.append(f"非法 type: {page_type}")
    elif not page_type:
        penalty += 0.2
        reasons.append("type 为空")

    # 检查 name
    name = frontmatter.get("name", "")
    if not name or len(name.strip()) < 2:
        penalty += 0.15
        reasons.append("name 过短或为空")

    # 检查 domain
    domain = frontmatter.get("domain", "")
    if not domain:
        penalty += 0.1
        reasons.append("domain 为空")

    # 内容非空检查
    if not content or len(content.strip()) < 50:
        penalty += 0.2
        reasons.append("正文过短(<50字符)")

    score = max(0.0, 1.0 - min(penalty, 1.0))

    if not reasons:
        reasons.append("frontmatter 完整")

    return RuleResult("completeness", round(score, 2), 0.20, reasons)


def entity_density_score(content: str) -> RuleResult:
    """
    实体密度评分规则
    检测内容中是否包含足够多的命名实体（技术术语、人名、项目名等）
    基于代码块、URL、特定标记等间接推断
    """
    if not content:
        return RuleResult("entity_density", 0.0, 0.15, ["空内容"])

    signals = 0
    reasons = []

    # 代码块
    code_blocks = len(re.findall(r"```[\s\S]*?```", content))
    if code_blocks > 0:
        signals += min(code_blocks * 2, 6)
        reasons.append(f"代码块x{code_blocks}")

    # 行内代码
    inline_codes = len(re.findall(r"`[^`]+`", content))
    if inline_codes > 0:
        signals += min(inline_codes, 3)
        reasons.append(f"行内代码x{inline_codes}")

    # URL
    urls = len(re.findall(r"https?://\S+", content))
    if urls > 0:
        signals += min(urls * 2, 4)
        reasons.append(f"链接x{urls}")

    # 列表项
    list_items = len(re.findall(r"^\s*[-*\d]\s+", content, re.M))
    if list_items > 0:
        signals += min(list_items, 4)
        reasons.append(f"列表项x{list_items}")

    # 技术术语密度（简单版本：大驼峰命名）
    camel_cases = len(re.findall(r"\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b", content))
    if camel_cases > 0:
        signals += min(camel_cases, 4)
        reasons.append(f"技术术语x{camel_cases}")

    score = min(signals / 10.0, 1.0)

    if not reasons:
        reasons.append("低密度")

    return RuleResult("entity_density", round(score, 2), 0.15, reasons)


def actionability_score(content: str) -> RuleResult:
    """
    可操作性评分规则
    检测内容是否包含可执行的建议、步骤、命令等
    """
    if not content:
        return RuleResult("actionability", 0.0, 0.20, ["空内容"])

    signals = 0
    reasons = []

    # 步骤标记
    steps = len(re.findall(r"(?:步骤|step|第[一二三四五六七八九十\d]+步|第一步)", content, re.I))
    if steps > 0:
        signals += min(steps * 2, 6)
        reasons.append(f"步骤标记x{steps}")

    # 命令行代码块
    shell_blocks = len(re.findall(r"```(?:bash|sh|shell|zsh)[\s\S]*?```", content, re.I))
    if shell_blocks > 0:
        signals += min(shell_blocks * 3, 6)
        reasons.append(f"命令块x{shell_blocks}")

    # 配置示例
    configs = len(re.findall(r"```(?:json|yaml|yml|toml|ini|conf)[\s\S]*?```", content, re.I))
    if configs > 0:
        signals += min(configs * 2, 4)
        reasons.append(f"配置示例x{configs}")

    # 行动动词
    action_verbs = len(
        re.findall(
            r"(?:运行|执行|安装|配置|设置|修改|创建|删除|更新|部署|启动|停止|检查|验证|测试)",
            content,
        )
    )
    if action_verbs > 0:
        signals += min(action_verbs, 4)
        reasons.append(f"行动动词x{action_verbs}")

    score = min(signals / 10.0, 1.0)

    if not reasons:
        reasons.append("无明确行动指引")

    return RuleResult("actionability", round(score, 2), 0.20, reasons)


# ==================== RuleWeightOptimizer 权重自适应优化器 ====================


class RuleWeightOptimizer:
    """Historical optimizer identity retained as a fail-closed API boundary."""

    MIN_SAMPLES_FOR_OPTIMIZE = 50
    MAX_WEIGHT_CHANGE_RATIO = 0.30

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_config().database_dir / "rule_weight_optimizer.db"

    @staticmethod
    def _retired(operation: str) -> NoReturn:
        raise PermissionError(f"training_admission_receipt_required:rule_optimizer:{operation}")

    def record_outcome(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self._retired("record_outcome")

    def get_rule_accuracy(self, *args: Any, **kwargs: Any) -> Optional[float]:
        del args, kwargs
        self._retired("get_rule_accuracy")

    def get_total_samples(self, *args: Any, **kwargs: Any) -> int:
        del args, kwargs
        self._retired("get_total_samples")

    def optimize_weights(self, *args: Any, **kwargs: Any) -> Dict[str, float]:
        del args, kwargs
        self._retired("optimize_weights")

    def get_weight_history(self, *args: Any, **kwargs: Any) -> List[Dict]:
        del args, kwargs
        self._retired("get_weight_history")

    def get_recent_outcomes(self, *args: Any, **kwargs: Any) -> List[Tuple[float, bool]]:
        del args, kwargs
        self._retired("get_recent_outcomes")

    def detect_misalignment(self, *args: Any, **kwargs: Any) -> List[Dict]:
        del args, kwargs
        self._retired("detect_misalignment")

    def get_stats(self) -> Dict:
        self._retired("get_stats")


# ==================== RuleScorer 统一入口 ====================


class RuleScorer:
    """
    硬编码规则评分器

    所有规则独立运行，加权求和得到最终 0-1 分数。
    每条规则可以独立开关、独立调权重。
    """

    # 默认规则列表：(规则函数, 权重, 是否启用)
    DEFAULT_RULES = [
        (noise_penalty, 0.15, True),
        (quality_score, 0.30, True),
        (completeness_penalty, 0.20, True),
        (entity_density_score, 0.15, True),
        (actionability_score, 0.20, True),
    ]

    def __init__(
        self,
        rules: Optional[List[Tuple]] = None,
        optimizer: Optional[RuleWeightOptimizer] = None,
        load_shared_weights: bool = False,
        weight_store: Optional[RuleWeightStore] = None,
    ):
        """
        Args:
            rules: 自定义规则列表，None 使用默认规则
            optimizer: 可选的 RuleWeightOptimizer，启用权重自适应进化
            load_shared_weights: 是否从 RuleWeightStore 加载共享权重
            weight_store: 指定权重存储实例，None 使用全局单例
        """
        self.rules = rules or self.DEFAULT_RULES.copy()
        self._history: List[Dict] = []  # 评分历史，用于后续分析
        self.optimizer = optimizer  # 权重优化器（可选）
        self.weight_store = weight_store
        if load_shared_weights:
            raise PermissionError("training_admission_receipt_required:shared_rule_weights")

    def _load_shared_weights(self) -> None:
        """Reject optimizer-derived shared weights."""

        raise PermissionError("training_admission_receipt_required:load_shared_rule_weights")

    def refresh_weights(self) -> None:
        """重新从共享存储加载权重（用于外部更新后手动刷新）。"""
        raise PermissionError("training_admission_receipt_required:refresh_rule_weights")

    def score(self, content: str, frontmatter: Optional[Dict] = None) -> float:
        """
        对内容进行规则评分

        Args:
            content: 内容文本
            frontmatter: 可选的 frontmatter 字典（用于 completeness 规则）

        Returns:
            0.0 ~ 1.0 的综合评分
        """
        results = self._run_rules(content, frontmatter)

        # 加权求和
        total_weight = 0.0
        weighted_score = 0.0
        for result, weight, enabled in results:
            if enabled:
                total_weight += weight
                weighted_score += result.score * weight

        final_score = weighted_score / total_weight if total_weight > 0 else 0.0
        final_score = max(0.0, min(1.0, final_score))

        # 记录历史
        self._history.append(
            {
                "content_preview": content[:100] if content else "",
                "score": round(final_score, 3),
                "rule_results": [
                    {"name": r.rule_name, "score": r.score, "weight": w, "enabled": e}
                    for r, w, e in results
                ],
            }
        )

        return round(final_score, 3)

    def explain(self, content: str, frontmatter: Optional[Dict] = None) -> Dict:
        """
        返回详细的评分解释（用于调试和人工抽查）

        Returns:
            {
                "final_score": float,
                "rules": [
                    {
                        "name": str,
                        "score": float,
                        "weight": float,
                        "enabled": bool,
                        "reasons": [str],
                    }
                ]
            }
        """
        results = self._run_rules(content, frontmatter)

        total_weight = sum(w for _, w, e in results if e)
        weighted_score = sum(r.score * w for r, w, e in results if e)
        final_score = weighted_score / total_weight if total_weight > 0 else 0.0

        return {
            "final_score": round(final_score, 3),
            "rules": [
                {
                    "name": result.rule_name,
                    "score": result.score,
                    "weight": weight,
                    "enabled": enabled,
                    "reasons": result.reasons,
                }
                for result, weight, enabled in results
            ],
        }

    def _run_rules(
        self, content: str, frontmatter: Optional[Dict]
    ) -> List[Tuple[RuleResult, float, bool]]:
        """运行所有规则，返回结果列表"""
        results = []
        for rule_func, weight, enabled in self.rules:
            try:
                if rule_func.__name__ == "completeness_penalty":
                    result = rule_func(frontmatter or {}, content)
                else:
                    result = rule_func(content)
            except (RuntimeError, ValueError, TypeError, KeyError, ArithmeticError) as e:
                # 规则异常不中断，记录错误并给中等分数
                result = RuleResult(rule_func.__name__, 0.5, weight, [f"规则异常: {str(e)}"])
            results.append((result, weight, enabled))
        return results

    def set_rule_weight(self, rule_name: str, weight: float):
        """动态调整某条规则的权重"""
        for i, (func, _, enabled) in enumerate(self.rules):
            if func.__name__ == rule_name:
                self.rules[i] = (func, weight, enabled)
                return
        raise ValueError(f"规则不存在: {rule_name}")

    def enable_rule(self, rule_name: str):
        """启用某条规则"""
        for i, (func, w, _) in enumerate(self.rules):
            if func.__name__ == rule_name:
                self.rules[i] = (func, w, True)
                return
        raise ValueError(f"规则不存在: {rule_name}")

    def disable_rule(self, rule_name: str):
        """禁用某条规则"""
        for i, (func, w, _) in enumerate(self.rules):
            if func.__name__ == rule_name:
                self.rules[i] = (func, w, False)
                return
        raise ValueError(f"规则不存在: {rule_name}")

    def get_history(self, limit: int = 100) -> List[Dict]:
        """获取最近的评分历史"""
        return self._history[-limit:]

    def clear_history(self):
        """清空评分历史"""
        self._history.clear()

    # ---- Retired adaptive-weight boundary ----

    @staticmethod
    def _reject_adaptive_weight_effect(operation: str) -> NoReturn:
        raise PermissionError(f"training_admission_receipt_required:rule_scorer:{operation}")

    def record_rule_outcome(
        self,
        rule_name: str,
        predicted_score: float,
        actual_label: bool,
    ) -> None:
        del rule_name, predicted_score, actual_label
        self._reject_adaptive_weight_effect("record_rule_outcome")

    def record_full_outcome(
        self,
        actual_label: bool,
        content: str = "",
        frontmatter: Optional[Dict] = None,
    ) -> int:
        del actual_label, content, frontmatter
        self._reject_adaptive_weight_effect("record_full_outcome")

    def auto_optimize(self, force: bool = False) -> Optional[Dict[str, float]]:
        del force
        self._reject_adaptive_weight_effect("auto_optimize")

    def get_optimizer_stats(self) -> Optional[Dict]:
        self._reject_adaptive_weight_effect("get_optimizer_stats")

    def apply_pattern_adjustments(self) -> List[Dict]:
        self._reject_adaptive_weight_effect("apply_pattern_adjustments")


# ==================== 共享 RuleScorer 单例 ====================

_shared_rule_scorer_instance: Optional[RuleScorer] = None


def get_shared_rule_scorer() -> RuleScorer:
    """Process-wide cold rule scorer with no pre-cutover adaptive state."""
    global _shared_rule_scorer_instance
    if _shared_rule_scorer_instance is None:
        _shared_rule_scorer_instance = RuleScorer(
            optimizer=None,
            load_shared_weights=False,
        )
    return _shared_rule_scorer_instance


if __name__ == "__main__":
    scorer = RuleScorer()

    test_cases = [
        ("好的", None),
        ("这是一个关于 Redis 连接池配置的技术讨论。", None),
        ("步骤1：安装 Docker。步骤2：编写 Dockerfile。", None),
        ("哈哈哈哈哈哈", None),
        ("", None),
        ("OK", None),
        ("我分析了系统的性能瓶颈。", None),
        ("运行 npm install 然后 npm run build", None),
        ("这个问题涉及到分布式事务的一致性保证。", None),
        ("x", None),
        ("谢谢", None),
    ]

    for i, (content, fm) in enumerate(test_cases, 1):
        score = scorer.score(content, fm)
        explanation = scorer.explain(content, fm)
        print(f"\n--- 测试 {i} ---")
        print(f"内容: {content[:60]}...")
        print(f"评分: {score}")
        for rule in explanation["rules"]:
            status = "✓" if rule["enabled"] else "✗"
            print(
                f"  {status} {rule['name']}: {rule['score']:.2f} (权重{rule['weight']}) {rule['reasons']}"  # noqa: E501
            )
