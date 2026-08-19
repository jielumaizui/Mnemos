"""
Shared quality scoring helpers for rule_scorer and ingest_helpers.

Extracted to avoid duplicating length/density/richness logic across modules.
All functions are private (module-level) by convention.
"""

import re
from typing import Any, Dict, List, Set, Tuple

# Shared constants
_STOPWORDS_ZH: Set[str] = {
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "一个",
    "上",
    "也",
    "很",
    "到",
    "说",
    "要",
    "去",
    "你",
    "会",
    "着",
    "没有",
    "看",
    "好",
    "自己",
    "这",
    "那",
    "啊",
    "嗯",
    "哦",
    "呢",
    "吧",
    "吗",
    "哈",
    "这个",
    "那个",
    "然后",
    "就是",
    "还是",
    "但是",
    "因为",
    "所以",
    "如果",
    "虽然",
    "不过",
    "其实",
    "可能",
    "应该",
    "觉得",
    "认为",
    "知道",
    "想要",
    "需要",
}

_VALUE_SIGNALS: Set[str] = {
    "代码",
    "函数",
    "方法",
    "类",
    "模块",
    "接口",
    "API",
    "配置",
    "部署",
    "调试",
    "测试",
    "优化",
    "重构",
    "版本",
    "提交",
    "分支",
    "数据库",
    "查询",
    "缓存",
    "队列",
    "服务",
    "容器",
    "集群",
    "python",
    "javascript",
    "typescript",
    "java",
    "go",
    "rust",
    "docker",
    "kubernetes",
    "linux",
    "nginx",
    "redis",
    "问题",
    "错误",
    "bug",
    "异常",
    "崩溃",
    "失败",
    "超时",
    "解决",
    "修复",
    "方案",
    "思路",
    "分析",
    "排查",
    "定位",
    "设计",
    "架构",
    "方案",
    "策略",
    "选择",
    "对比",
    "优缺点",
    "建议",
    "推荐",
    "决定",
    "结论",
    "原因",
    "理由",
    "原理",
    "机制",
    "流程",
    "步骤",
    "指南",
    "文档",
    "规范",
    "标准",
    "模式",
    "算法",
    "数据结构",
    "协议",
    "格式",
}

_MAX_LENGTH_SCORE = 30
_MAX_DENSITY_SCORE = 35
_MAX_RICHNESS_SCORE = 35

_WORD_RE = re.compile(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}")
_ZH_RE = re.compile(r"[\u4e00-\u9fa5]")
_EN_RE = re.compile(r"[a-zA-Z]")
_LIST_RE = re.compile(r"^\s*[-*\d]\s+", re.M)
_URL_RE = re.compile(r"https?://")


def _extract_words(stripped: str) -> List[str]:
    """Extract Chinese (2+ chars) and English (2+ letters) words."""
    return _WORD_RE.findall(stripped)


def _compute_length_score(char_count: int) -> float:
    """Compute the length dimension score in [0, _MAX_LENGTH_SCORE]."""
    if char_count < 20:
        score: float = char_count  # 0-20
    elif char_count < 100:
        score = 20 + (char_count - 20) * 0.125  # 20-30
    elif char_count < 500:
        score = _MAX_LENGTH_SCORE
    elif char_count < 1000:
        score = _MAX_LENGTH_SCORE - (char_count - 500) * 0.01
    else:
        score = max(15, 25 - (char_count - 1000) * 0.005)
    return max(0.0, min(_MAX_LENGTH_SCORE, score))


def _compute_density_score(
    words: List[str], stopwords: Set[str]
) -> Tuple[float, int, int]:
    """
    Compute the density dimension score in [0, _MAX_DENSITY_SCORE].

    Returns:
        (score, valid_word_count, total_word_count)
    """
    total_words = len(words)
    if total_words == 0:
        return 0.0, 0, 0

    valid_words = sum(1 for w in words if w.lower() not in stopwords)
    valid_ratio = valid_words / total_words

    has_zh = any(_ZH_RE.search(w) for w in words)
    has_en = any(_EN_RE.search(w) for w in words)
    mixed_bonus = 0.05 if has_zh and has_en else 0.0

    score = (valid_ratio + mixed_bonus) * _MAX_DENSITY_SCORE
    return max(0.0, min(_MAX_DENSITY_SCORE, score)), valid_words, total_words


def _compute_richness_score(
    stripped: str, words: List[str], total_words: int
) -> Tuple[float, int, float]:
    """
    Compute the richness dimension score in [0, _MAX_RICHNESS_SCORE].

    Returns:
        (score, value_signal_count, unique_ratio)
    """
    if total_words == 0:
        return 0.0, 0, 0.0

    unique_words = {w.lower() for w in words}
    unique_ratio = len(unique_words) / total_words

    content_lower = stripped.lower()
    value_signals = sum(1 for sig in _VALUE_SIGNALS if sig.lower() in content_lower)
    value_signal_score = min(value_signals * 3, 15)

    struct_signals = 0
    if _LIST_RE.search(stripped):
        struct_signals += 3
    if "`" in stripped or "```" in stripped:
        struct_signals += 5
    if _URL_RE.search(stripped):
        struct_signals += 3
    struct_signals = min(struct_signals, 10)

    score = unique_ratio * 10 + value_signal_score + struct_signals
    return max(0.0, min(_MAX_RICHNESS_SCORE, score)), value_signals, unique_ratio


def _build_quality_result(
    length: float,
    density: float,
    richness: float,
    valid_words: int,
    total_words: int,
    value_signals: int,
    unique_ratio: float,
    char_count: int,
) -> Dict[str, Any]:
    """Build the 0-100 quality result dict used by score_message_quality."""
    total_score = length + density + richness
    return {
        "total_score": round(total_score, 1),
        "length_score": round(length, 1),
        "density_score": round(density, 1),
        "richness_score": round(richness, 1),
        "details": {
            "char_count": char_count,
            "valid_word_count": valid_words,
            "stopword_count": total_words - valid_words,
            "unique_ratio": round(unique_ratio, 3),
            "value_signals": value_signals,
        },
    }
