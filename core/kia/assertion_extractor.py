import logging
logger = logging.getLogger(__name__)
"""
Assertion Extractor - 从文本中提取可验证断言

职责：
- 从对话/文档中提取可验证的 factual claims
- 为冲突检测提供原子级知识单元
- 纯规则+启发式，无外部依赖

设计原则：宁可漏提，不可错提。
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum


class KnowledgeForm(Enum):
    PROBLEM_SOLUTION = "problem-solution"
    DECISION_LOG = "decision-log"
    HEURISTIC = "heuristic"
    ANTI_PATTERN = "anti-pattern"
    METHODOLOGY = "methodology"
    INSIGHT = "insight"
    UNKNOWN = "unknown"


@dataclass
class Assertion:
    """单个可验证断言"""
    claim: str                      # 断言文本（一句话）
    form: KnowledgeForm             # 所属知识形态
    confidence: float = 0.5         # 提取置信度 0-1
    context: str = ""               # 原始上下文（前后几句话）
    source: str = ""                # 来源标记
    evidence_level: str = "single-source"  # single-source/multi-source/curated
    temporal_scope: str = ""        # permanent/stable/version-bound/contextual
    boundary_hint: str = ""         # 边界条件提示（从否定句/条件句提取）
    is_negated: bool = False        # 是否是否定断言（反模式）
    tags: List[str] = field(default_factory=list)  # domain/主题标签，用于基础领域冲突判断


# ========== 启发式规则 ==========

# 主观感受词（这些开头的句子不太可能是 factual claim）
SUBJECTIVE_PREFIXES = {
    '我觉得', '我认为', '我想', '我感觉', '我猜', '我估计',
    'i think', 'i feel', 'i guess', 'i believe', 'in my opinion',
    'maybe', 'perhaps', 'probably', '可能', '也许', '大概',
}

# 疑问词开头（不是断言）
QUESTION_PREFIXES = {'?', '？', '什么', '怎么', '为什么', '多少', 'who', 'what',
                     'how', 'why', 'when', 'where', 'which', '是否', '能不能'}

# 祈使句模式
IMPERATIVE_PATTERNS = re.compile(
    r'^(请|建议|需要|应该|必须|可以|试试|用一下|运行|执行|打开|关闭|检查|查看|'
    r'please|try|run|execute|open|close|check|use|make sure|ensure|don\'t|do not)',
    re.IGNORECASE
)

# 高置信度断言信号
HIGH_CONFIDENCE_SIGNALS = [
    r'(原因|根因|本质|实质)(是|在于)',
    r'(解决|修复|处理)(方案|办法|方式)(是|为)',
    r'(结论|结果|效果)(是|为|表明)',
    r'(因为|由于).+(所以|因此|导致)',
    r'(如果|当).+(就|则|会).+(否则|不然)',
    r'(建议|推荐).+(使用|采用|选择)',
    r'(不要|避免|切忌).+(因为|否则)',
    r'\d+[%％倍个次]?\s*(的|以上|以下|以内)',
    r'(v\d+\.\d+|version\s+\d+)',
    # 新增：决策信号
    r'(选|选择|决定|定了).+(而非|不是|放弃|舍弃|优于|胜过|打败)',
    r'(用|采用|使用).+(而非|不是|代替|替代).+(因为|由于|理由是)',
    r'(选|选择|决定|采用|使用).{0,5}(了|定|为|作为)',
    # 新增：技术判断信号
    r'(瓶颈|限制|约束|短板|劣势|缺陷|不足|问题)(是|在于|为)',
    r'.+(是|在于|为)(瓶颈|限制|约束|短板|劣势|缺陷|不足|问题)',
    r'(优于|胜过|强于|快于|打败|超过|碾压).+(因为|由于|在于)',
    r'(步骤|流程|方法)(是|为)?(：|:)?\s*(先|第一步|首先)',
    # 新增：因果/条件信号
    r'.+(通常|一般|大概率|往往|经常).+(是|会|导致|引起)',
    r'.+(标志|信号|表征|特征)(是|为|：)',
]

# 边界条件信号
BOUNDARY_SIGNALS = [
    r'(但是|不过|然而|除非|除了).+(不适用|无效|失败|错误)',
    r'(只在|仅在|除非|如果|当).+(才|适用|有效)',
    r'(不适用于|不适合|不能在).+(情况|场景|条件下)',
    r'(注意|警告|小心| caveat ).+(不要|避免|切记)',
]

# 否定断言信号（反模式）
NEGATION_SIGNALS = [
    r'^(不要|避免|切忌|禁止|千万别|千万别|never|don\'t|do not|avoid)',
    r'(错误|误区|陷阱|坑|anti-pattern).+(是|在于|为)',
]

# AI 行为反模式信号（从 AI 思考链中提取）
AI_BEHAVIOR_ANTI_PATTERNS = [
    r'(陷入循环|重复读取|反复分析|分析 paralysis|thinking loop).+(是|在于|为)',
    r'(同一文件|同一问题|同一模块).+(读取|分析|查看).+(超过|大于|多于).*[23]',
    r'(思考|分析).+(过长|太久|过多).+(没有|无).+(行动|修复|提交|修改)',
    r'(用户说|用户要求).+(修复|修改|提交).+(但|然而|还在).+(分析|思考|查看)',
]


def _split_sentences(text: str) -> List[str]:
    """按句子分割文本"""
    # 中文句子分隔符
    sentences = re.split(r'(?<=[。！？\.\!\?])\s+', text)
    return [s.strip() for s in sentences if s.strip()]


def _extract_objective_part(sentence: str) -> Optional[str]:
    """从混合主观+客观的句子中提取客观部分

    处理模式："我觉得...，但实际上/不过/然而..."
    返回客观部分，如果没有则返回 None
    """
    # 转折词后的客观部分
    contrast_patterns = [
        r'(?:我觉得|我认为|我想|我感觉|我猜|我估计).{0,20}[,，。]\s*但(?:是|实际上|)?[,，]?\s*(.+)',
        r'(?:可能|也许|大概).{0,15}[,，。]\s*但(?:是|实际上|)?[,，]?\s*(.+)',
        r'(?:i think|i feel|i guess|i believe).{0,30}[,，.。]\s*but[,，]?\s*(.+)',
    ]
    for pattern in contrast_patterns:
        match = re.search(pattern, sentence, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _is_likely_assertion(sentence: str) -> tuple:
    """
    判断句子是否可能是 factual claim
    返回: (is_assertion, confidence)
    """
    s = sentence.strip()
    if len(s) < 10 or len(s) > 300:
        return False, 0.0

    lower_s = s.lower()

    # 过滤主观感受 —— 但先检查是否有转折后的客观部分
    for prefix in SUBJECTIVE_PREFIXES:
        if lower_s.startswith(prefix.lower()):
            # 尝试提取转折后的客观部分
            obj_part = _extract_objective_part(s)
            if obj_part and len(obj_part) >= 10:
                # 用客观部分替换原句继续判断
                s = obj_part
                lower_s = s.lower()
                break
            else:
                return False, 0.0

    # 过滤疑问句
    for prefix in QUESTION_PREFIXES:
        if lower_s.startswith(prefix.lower()):
            return False, 0.0

    # 过滤祈使句
    if IMPERATIVE_PATTERNS.match(s):
        return False, 0.0

    # 计算置信度
    confidence = 0.3  # 基础分

    # 高置信度信号
    for pattern in HIGH_CONFIDENCE_SIGNALS:
        if re.search(pattern, s, re.IGNORECASE):
            confidence += 0.25
            break

    # 包含具体数据增加置信度
    if re.search(r'\d+', s):
        confidence += 0.15

    # 包含技术术语增加置信度
    if re.search(r'[a-zA-Z_]{3,}', s):
        confidence += 0.1

    # 包含因果关系增加置信度
    if re.search(r'(因为|由于|所以|因此|导致|if|because|so|therefore)', s, re.IGNORECASE):
        confidence += 0.1

    confidence = min(confidence, 0.95)
    return confidence >= 0.4, confidence


def _detect_boundary(sentence: str) -> str:
    """从句子中提取边界条件提示"""
    for pattern in BOUNDARY_SIGNALS:
        match = re.search(pattern, sentence, re.IGNORECASE)
        if match:
            return sentence[match.start():].strip()
    return ""


def _detect_negation(sentence: str) -> bool:
    """检测是否是否定断言（反模式）"""
    for pattern in NEGATION_SIGNALS:
        if re.search(pattern, sentence, re.IGNORECASE):
            return True
    return False


def _classify_form(assertions: List[Assertion], full_text: str) -> None:
    """
    根据上下文对断言进行知识形态分类
    这是一个启发式分类，不是精确的
    """
    text_lower = full_text.lower()

    # 检测文本整体主题
    has_problem_solution = bool(re.search(r'(问题|bug|错误|故障|解决|修复|排查|根因|方案)',
                                          text_lower))
    has_decision = bool(re.search(r'(选择|决定|决策|选了|用.+而不是|对比|优劣|权衡)',
                                  text_lower))
    has_methodology = bool(re.search(r'(框架|流程|步骤|方法论|模式|体系|标准|规范)',
                                     text_lower))
    has_insight = bool(re.search(r'(原来|本质上|其实是|等价于|类似于|就像| analogy )',
                                 text_lower))

    for assertion in assertions:
        claim_lower = assertion.claim.lower()

        # 否定断言 → 反模式
        if assertion.is_negated:
            assertion.form = KnowledgeForm.ANTI_PATTERN
            continue

        # 包含"选择/决定/采用...而非" → 决策记录
        if re.search(r'(选|选择|决定|定了|采用|使用).+(而非|不是|放弃|舍弃|优于|胜过|打败|替代|代替)', claim_lower):
            assertion.form = KnowledgeForm.DECISION_LOG
            continue
        # 包含"原因是..."且整体文本有决策主题 → 决策记录
        if has_decision and re.search(r'(原因|理由|考虑)(是|在于|为)', claim_lower):
            assertion.form = KnowledgeForm.DECISION_LOG
            continue

        # 包含"步骤/流程/框架/方法是" → 方法论
        if re.search(r'(步骤|流程|框架|方法论|模式|方法)(是|为|：|:)\s*(先|第一步|首先|第\d+步)', claim_lower):
            assertion.form = KnowledgeForm.METHODOLOGY
            continue
        if re.search(r'(先|第一步|首先).+(再|然后|接着|最后).+(最后|最终)', claim_lower):
            assertion.form = KnowledgeForm.METHODOLOGY
            continue

        # 包含"原来/本质上/等价于" → 洞察
        if re.search(r'(原来|本质上|其实是|等价于|类似于|类似于|就像| analogy )', claim_lower):
            assertion.form = KnowledgeForm.INSIGHT
            continue

        # 包含"如果...就/建议/优先" → 启发式
        if re.search(r'(如果|当|建议|推荐|优先|尽量|最好|优先).+(就|用|选|做|应该|可以)', claim_lower):
            assertion.form = KnowledgeForm.HEURISTIC
            continue

        # 包含问题+解决 → 问题-解决对
        if has_problem_solution and re.search(r'(问题|bug|错误|故障|根因).+(解决|修复|方案|方法|原因是)',
                                              claim_lower):
            assertion.form = KnowledgeForm.PROBLEM_SOLUTION
            continue
        # 包含"瓶颈/短板/缺陷" → 问题-解决对（属于问题分析）
        if re.search(r'(瓶颈|短板|缺陷|不足|问题|限制|约束)(是|在于|为)', claim_lower):
            assertion.form = KnowledgeForm.PROBLEM_SOLUTION
            continue

        # 默认
        if assertion.form == KnowledgeForm.UNKNOWN:
            if has_methodology:
                assertion.form = KnowledgeForm.METHODOLOGY
            elif has_decision:
                assertion.form = KnowledgeForm.DECISION_LOG
            elif has_problem_solution:
                assertion.form = KnowledgeForm.PROBLEM_SOLUTION
            else:
                assertion.form = KnowledgeForm.HEURISTIC


def extract_assertions(text: str, source: str = "") -> List[Assertion]:
    """
    从文本中提取可验证断言

    Args:
        text: 原始文本（对话内容或文档段落）
        source: 来源标记（如 session_id 或文件路径）

    Returns:
        Assertion 列表
    """
    sentences = _split_sentences(text)
    assertions = []

    for i, sentence in enumerate(sentences):
        is_assertion, confidence = _is_likely_assertion(sentence)
        if not is_assertion:
            continue

        # 提取上下文（前后各1句）
        context_start = max(0, i - 1)
        context_end = min(len(sentences), i + 2)
        context = " ".join(sentences[context_start:context_end])

        assertion = Assertion(
            claim=sentence,
            form=KnowledgeForm.UNKNOWN,
            confidence=confidence,
            context=context,
            source=source,
            boundary_hint=_detect_boundary(sentence),
            is_negated=_detect_negation(sentence),
        )
        assertions.append(assertion)

    # 第二遍：形态分类
    _classify_form(assertions, text)

    return assertions


def extract_from_messages(messages: List[Dict], session_id: str = "") -> List[Assertion]:
    """
    从对话消息列表中提取断言

    Args:
        messages: 消息列表，每项包含 role 和 content
        session_id: 会话 ID

    Returns:
        Assertion 列表
    """
    all_assertions = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        # 只从 assistant 和 user 的消息中提取（跳过 system、tool）
        if role not in ("assistant", "user"):
            continue

        # 过滤掉回流内容
        if "<wiki-context" in content or "</wiki-context>" in content:
            continue

        source = f"{session_id}/{role}"
        assertions = extract_assertions(content, source=source)

        # 根据角色调整证据级别和置信度
        for a in assertions:
            if role == "assistant":
                # assistant 的回答：可能包含未验证的推测，但包含核心知识
                a.evidence_level = "single-source"
                a.confidence *= 0.9
            else:
                # user 的输入：通常是问题/需求/背景描述，不是 factual claim
                # 大幅降低置信度，如果低于门槛则丢弃
                a.evidence_level = "anecdote"
                a.confidence *= 0.5  # user 消息的断言可信度大幅降低
                # 额外过滤：用户消息中的祈使句/请求句即使被提取也应丢弃
                if _is_user_request(a.claim):
                    a.confidence = 0.0

        all_assertions.extend(assertions)

    return all_assertions


def _is_user_request(sentence: str) -> bool:
    """检测是否是用户的请求/疑问/求助句（非 factual claim）"""
    s = sentence.strip().lower()

    # 求助/请求开头
    request_prefixes = {
        '帮', '请', '能否', '能不能', '可以', '麻烦', '请教',
        '求助', '帮忙', '有没有', '有没有谁', '谁可以', '哪位',
        'hi', 'hello', '你好', '在吗', '求助',
    }
    for prefix in request_prefixes:
        if s.startswith(prefix):
            return True

    # 祈使句（用户请求操作）
    if IMPERATIVE_PATTERNS.match(s):
        # 但保留像"建议是..."这种包含断言的
        if not re.search(r'(是|为|在于|原因|因为|由于)', s):
            return True

    # 明确的问题标记
    if '？' in s or '?' in s:
        return True
    if re.search(r'^(怎么|如何|什么|为什么|哪里|哪个|哪些|谁|何时|是否|能不能|可不可以)', s):
        return True

    # 英文问题
    if re.search(r'^(how|what|why|where|which|who|when|can|could|would|will|do|does|is|are)', s):
        return True

    # 请求帮忙的模式
    if re.search(r'(帮我|帮我一下|帮我看看|帮忙|求助|请教|问一下|问下|请问)', s):
        return True

    return False


def merge_similar_assertions(assertions: List[Assertion],
                              similarity_threshold: float = 0.85) -> List[Assertion]:
    """
    合并相似的断言（简单的字符串包含检测）

    Args:
        assertions: 断言列表
        similarity_threshold: 相似度门槛（这里用简单启发式）

    Returns:
        去重后的断言列表
    """
    if not assertions:
        return []

    # 按置信度排序
    sorted_a = sorted(assertions, key=lambda x: x.confidence, reverse=True)

    merged = []
    for a in sorted_a:
        is_duplicate = False
        for m in merged:
            # 简单相似度：如果一个是另一个的子串，或者共享 60%+ 字符
            if a.claim in m.claim or m.claim in a.claim:
                is_duplicate = True
                break
            # 计算共享字符比例
            a_chars = set(a.claim)
            m_chars = set(m.claim)
            if len(a_chars) > 0:
                overlap = len(a_chars & m_chars) / len(a_chars)
                if overlap > similarity_threshold:
                    is_duplicate = True
                    break

        if not is_duplicate:
            merged.append(a)

    return merged


# ========== CLI ==========

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Assertion Extractor")
    parser.add_argument("--text", help="要分析的文本")
    parser.add_argument("--file", help="要分析的文件路径")
    args = parser.parse_args()

    text = ""
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    else:
        # 示例文本
        text = """
        Codex 404 问题的根因是千帆 API 的 endpoint 与 OpenAI 不兼容。
        解决方法是使用 /v1/chat/completions 而非 /codex/v1/...。
        但是要注意，这个解法仅针对百度千帆，官方 OpenAI 不存在此问题。
        不要直接用 /codex/v1/...，否则会导致请求失败。
        我建议遇到 404 时先检查 endpoint 映射，再查版本兼容性。
        """

    assertions = extract_assertions(text)
    logger.info(f"提取到 {len(assertions)} 条断言:\n")

    for i, a in enumerate(assertions, 1):
        logger.info(f"[{i}] {a.form.value}")
        logger.info(f"    断言: {a.claim}")
        logger.info(f"    置信度: {a.confidence:.2f}")
        if a.boundary_hint:
            logger.info(f"    边界: {a.boundary_hint}")
        if a.is_negated:
            logger.info(f"    [否定断言]")
        logger.info()


if __name__ == "__main__":
    main()
