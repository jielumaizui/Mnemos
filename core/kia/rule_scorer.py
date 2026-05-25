"""
RuleScorer - 硬编码规则评分器

从现有散落的规则逻辑中提取、统一为独立模块。
作为 AdaptiveScorer 的 COLD 阶段兜底方案，也是 BayesianScorer 的基线对比。

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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ==================== 常量定义（从 ingest_helpers 提取）====================

_NOISE_RE_PATTERNS = [
    re.compile(r'^[\s\uff0c\u3002\uff01\uff1f,.!?；：:""''()（）[]{}【】]+$'),
    re.compile(r'^[\u2764\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf\s]+$'),
]

_NOISE_PHRASES = {
    '好的', '好', 'ok', 'okay', '继续', '嗯', '啊', '哦', '行', '可以',
    '明白', '知道了', '了解', '收到', '对的', '没错', '是的', '嗯嗯',
    '谢谢', '多谢', '辛苦了', '拜托', '麻烦了',
    'yes', 'yep', 'yeah', 'no', 'nope', 'nah',
    'go on', 'go ahead', 'next', 'proceed',
    'thanks', 'thank you', 'thx', 'ty',
    'got it', 'gotcha', 'understood', 'roger',
    '开始吧', '来吧', '动手吧', '搞起',
}

_STOPWORDS_ZH = {
    '的', '了', '在', '是', '我', '有', '和', '就', '不', '人',
    '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去',
    '你', '会', '着', '没有', '看', '好', '自己', '这', '那', '啊',
    '嗯', '哦', '呢', '吧', '吗', '哈', '这个', '那个', '然后', '就是',
    '还是', '但是', '因为', '所以', '如果', '虽然', '不过', '其实',
    '可能', '应该', '觉得', '认为', '知道', '想要', '需要',
}

_VALUE_SIGNALS = {
    '代码', '函数', '方法', '类', '模块', '接口', 'API', '配置',
    '部署', '调试', '测试', '优化', '重构', '版本', '提交', '分支',
    '数据库', '查询', '缓存', '队列', '服务', '容器', '集群',
    'python', 'javascript', 'typescript', 'java', 'go', 'rust',
    'docker', 'kubernetes', 'linux', 'nginx', 'redis',
    '问题', '错误', 'bug', '异常', '崩溃', '失败', '超时',
    '解决', '修复', '方案', '思路', '分析', '排查', '定位',
    '设计', '架构', '方案', '策略', '选择', '对比', '优缺点',
    '建议', '推荐', '决定', '结论', '原因', '理由',
    '原理', '机制', '流程', '步骤', '指南', '文档', '规范',
    '标准', '模式', '算法', '数据结构', '协议', '格式',
}

# frontmatter type 枚举（来自接口契约）
_VALID_TYPES = {
    'concept', 'person', 'project', 'technology', 'MOC', 'retrospective'
}

# frontmatter 必填字段
_REQUIRED_FRONTMATTER_FIELDS = {'type', 'name', 'domain'}


@dataclass
class RuleResult:
    """单条规则的评分结果"""
    rule_name: str
    score: float          # 0.0 ~ 1.0
    weight: float         # 该规则在总评分中的权重
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
    core = re.sub(r'[^\w\u4e00-\u9fa5]', '', stripped).lower()
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
    
    # === 1. 长度评分 (0-30) → 归一化 0-1 ===
    if char_count < 20:
        length_score = char_count  # 0-20
    elif char_count < 100:
        length_score = 20 + (char_count - 20) * 0.125  # 20-30
    elif char_count < 500:
        length_score = 30
    elif char_count < 1000:
        length_score = 30 - (char_count - 500) * 0.01
    else:
        length_score = max(15, 25 - (char_count - 1000) * 0.005)
    length_score = max(0, min(30, length_score))
    
    # === 2. 信息密度 (0-35) → 归一化 0-1 ===
    words = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}', stripped)
    if not words:
        return RuleResult("quality_score", 0.0, 0.30, ["无有效词汇"])
    
    total_words = len(words)
    stopwords = [w for w in words if w.lower() in _STOPWORDS_ZH]
    valid_words = total_words - len(stopwords)
    valid_ratio = valid_words / total_words if total_words > 0 else 0
    
    has_zh = any(re.search(r'[\u4e00-\u9fa5]', w) for w in words)
    has_en = any(re.search(r'[a-zA-Z]', w) for w in words)
    mixed_bonus = 0.05 if has_zh and has_en else 0
    
    density_score_raw = (valid_ratio + mixed_bonus) * 35
    density_score_raw = max(0, min(35, density_score_raw))
    
    # === 3. 语义丰富度 (0-35) → 归一化 0-1 ===
    unique_words = set(w.lower() for w in words)
    unique_ratio = len(unique_words) / total_words if total_words > 0 else 0
    
    content_lower = stripped.lower()
    value_signals = sum(1 for sig in _VALUE_SIGNALS if sig.lower() in content_lower)
    value_signal_score = min(value_signals * 3, 15)
    
    struct_signals = 0
    if re.search(r'^\s*[-*\d]\s+', content, re.M):
        struct_signals += 3
    if '`' in content or '```' in content:
        struct_signals += 5
    if re.search(r'https?://', content):
        struct_signals += 3
    struct_signals = min(struct_signals, 10)
    
    richness_score_raw = (unique_ratio * 10 + value_signal_score + struct_signals)
    richness_score_raw = max(0, min(35, richness_score_raw))
    
    # === 总分归一化 (0-100) → (0-1) ===
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
    
    # 检查必填字段
    missing = _REQUIRED_FRONTMATTER_FIELDS - set(frontmatter.keys())
    if missing:
        penalty += len(missing) * 0.25
        reasons.append(f"缺失字段: {', '.join(missing)}")
    
    # 检查 type 合法性
    page_type = frontmatter.get('type', '')
    if page_type and page_type not in _VALID_TYPES:
        penalty += 0.3
        reasons.append(f"非法 type: {page_type}")
    elif not page_type:
        penalty += 0.2
        reasons.append("type 为空")
    
    # 检查 name
    name = frontmatter.get('name', '')
    if not name or len(name.strip()) < 2:
        penalty += 0.15
        reasons.append("name 过短或为空")
    
    # 检查 domain
    domain = frontmatter.get('domain', '')
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
    code_blocks = len(re.findall(r'```[\s\S]*?```', content))
    if code_blocks > 0:
        signals += min(code_blocks * 2, 6)
        reasons.append(f"代码块x{code_blocks}")
    
    # 行内代码
    inline_codes = len(re.findall(r'`[^`]+`', content))
    if inline_codes > 0:
        signals += min(inline_codes, 3)
        reasons.append(f"行内代码x{inline_codes}")
    
    # URL
    urls = len(re.findall(r'https?://\S+', content))
    if urls > 0:
        signals += min(urls * 2, 4)
        reasons.append(f"链接x{urls}")
    
    # 列表项
    list_items = len(re.findall(r'^\s*[-*\d]\s+', content, re.M))
    if list_items > 0:
        signals += min(list_items, 4)
        reasons.append(f"列表项x{list_items}")
    
    # 技术术语密度（简单版本：大驼峰命名）
    camel_cases = len(re.findall(r'\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b', content))
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
    steps = len(re.findall(r'(?:步骤|step|第[一二三四五六七八九十\d]+步|第一步)', content, re.I))
    if steps > 0:
        signals += min(steps * 2, 6)
        reasons.append(f"步骤标记x{steps}")
    
    # 命令行代码块
    shell_blocks = len(re.findall(r'```(?:bash|sh|shell|zsh)[\s\S]*?```', content, re.I))
    if shell_blocks > 0:
        signals += min(shell_blocks * 3, 6)
        reasons.append(f"命令块x{shell_blocks}")
    
    # 配置示例
    configs = len(re.findall(r'```(?:json|yaml|yml|toml|ini|conf)[\s\S]*?```', content, re.I))
    if configs > 0:
        signals += min(configs * 2, 4)
        reasons.append(f"配置示例x{configs}")
    
    # 行动动词
    action_verbs = len(re.findall(
        r'(?:运行|执行|安装|配置|设置|修改|创建|删除|更新|部署|启动|停止|检查|验证|测试)',
        content
    ))
    if action_verbs > 0:
        signals += min(action_verbs, 4)
        reasons.append(f"行动动词x{action_verbs}")
    
    score = min(signals / 10.0, 1.0)
    
    if not reasons:
        reasons.append("无明确行动指引")
    
    return RuleResult("actionability", round(score, 2), 0.20, reasons)


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
    
    def __init__(self, rules: Optional[List[Tuple]] = None):
        """
        Args:
            rules: 自定义规则列表，None 使用默认规则
        """
        self.rules = rules or self.DEFAULT_RULES.copy()
        self._history: List[Dict] = []  # 评分历史，用于后续分析
    
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
        self._history.append({
            "content_preview": content[:100] if content else "",
            "score": round(final_score, 3),
            "rule_results": [
                {"name": r.rule_name, "score": r.score, "weight": w, "enabled": e}
                for r, w, e in results
            ],
        })
        
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
    
    def _run_rules(self, content: str, frontmatter: Optional[Dict]) -> List[Tuple[RuleResult, float, bool]]:
        """运行所有规则，返回结果列表"""
        results = []
        for rule_func, weight, enabled in self.rules:
            try:
                if rule_func.__name__ == "completeness_penalty":
                    result = rule_func(frontmatter or {}, content)
                else:
                    result = rule_func(content)
            except Exception as e:
                # 规则异常不中断，记录错误并给中等分数
                result = RuleResult(
                    rule_func.__name__, 0.5, weight, [f"规则异常: {str(e)}"]
                )
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


# ==================== 向后兼容包装 ====================

def is_noise_message(content: str, **kwargs) -> bool:
    """
    向后兼容：调用 rule_scorer.noise_penalty，返回 bool
    
    注意：此函数保留是为了不破坏现有调用方。
    新代码应直接使用 RuleScorer。
    """
    result = noise_penalty(content, **kwargs)
    return result.score < 0.5  # 分数 < 0.5 认为是噪声


def score_message_quality(content: str) -> Dict[str, float]:
    """
    向后兼容：调用 rule_scorer.quality_score，返回原格式
    
    注意：此函数保留是为了不破坏现有调用方。
    新代码应直接使用 RuleScorer。
    """
    result = quality_score(content)
    # 将 0-1 分数还原为 0-100 格式
    score_100 = result.score * 100
    return {
        "total_score": round(score_100, 1),
        "length_score": round(score_100 * 0.3, 1),
        "density_score": round(score_100 * 0.35, 1),
        "richness_score": round(score_100 * 0.35, 1),
        "details": {"char_count": len(content) if content else 0},
    }


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
            print(f"  {status} {rule['name']}: {rule['score']:.2f} (权重{rule['weight']}) {rule['reasons']}")
