"""
Ingest 引擎纯函数辅助模块

从 ingest_engine.py 抽离的无状态辅助函数（不依赖 IngestEngine 实例）。
全部为纯函数：相同输入恒得相同输出，无副作用，便于独立单测。
"""

import hashlib
import re
from typing import Any, Dict, List, Tuple

from core.kia._quality_scoring import (
    _STOPWORDS_ZH,
    _build_quality_result,
    _compute_density_score,
    _compute_length_score,
    _compute_richness_score,
    _extract_words,
)
from core.kia.ingest_lexicon import (
    _CONCEPT_TECH_TERMS,
    _ENTITY_ACRONYM_RE,
    _ENTITY_CAMEL_RE,
    _ENTITY_TECH_TERMS,
    _ENTITY_ZH_RE,
)

# Constants extracted from magic numbers
DURATION_BUCKET_MONTH_DAYS = 30

# 预编译正则，避免每次调用重新编译
_FINGERPRINT_RE = re.compile(r"[^\w\u4e00-\u9fa5]")
_WIKI_REF_RE = re.compile(r"\[\[([^\]]+)\]\]")

# ==================== 内容指纹与去重 ====================


def compute_content_fingerprint(content: str) -> str:
    """计算内容指纹（用于去重检测）"""
    cleaned = _FINGERPRINT_RE.sub("", content.lower())
    sample = cleaned[:200]
    # 加盐：内容长度信息防止前缀相同的长内容碰撞
    salt = f":{len(cleaned)}"
    return hashlib.md5((sample + salt).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def is_duplicate_content(existing_body: str, new_description: str, threshold: float = 0.8) -> bool:
    """检测内容是否重复

    Args:
        existing_body: 现有页面内容
        new_description: 新描述
        threshold: 相似度阈值（默认 0.8，当前实现以指纹前缀匹配 + 包含检测为主）

    Returns:
        是否重复
    """
    existing_descriptions = re.findall(
        r"### 新来源 - \d{4}-\d{2}-\d{2}\n\n(.+?)(?=\n###|\Z)",
        existing_body,
        re.DOTALL,
    )

    if not existing_descriptions:
        return False

    new_fp = compute_content_fingerprint(new_description)

    for existing_desc in existing_descriptions:
        existing_fp = compute_content_fingerprint(existing_desc)
        # 完整指纹匹配（前12字符碰撞概率对短内容过高）
        if new_fp == existing_fp:
            return True
        # 内容包含检测（增加长度相近限制，避免短描述被长描述误判）
        len_new = len(new_description)
        len_existing = len(existing_desc)
        if len_new > 0 and len_existing > 0:
            # 只有当长度差异不超过 30% 且一方包含另一方时才判定重复
            len_ratio = min(len_new, len_existing) / max(len_new, len_existing)
            if len_ratio >= 0.7:
                if new_description in existing_desc or existing_desc in new_description:
                    return True

    return False


# ==================== 噪声消息过滤 (P11) ====================

# 预编译噪声检测正则
_NOISE_RE_PATTERNS = [
    # 纯标点/空白
    re.compile(r'^[\s\uff0c\u3002\uff01\uff1f,.!?；：:""' "()（）[]{}【】]+$"),
    # 纯 emoji（常见范围）
    re.compile(r"^[\u2764\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf\s]+$"),
]

# 常见低价值短语（大小写不敏感）
_NOISE_PHRASES = {
    # 中文
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
    # 英文
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
    # 极简命令
    "开始吧",
    "来吧",
    "动手吧",
    "搞起",
}

# 预编译噪声短语正则（长模式优先，避免短模式吞掉有效内容）
_NOISE_PHRASES_RE = re.compile(
    "|".join(sorted(map(re.escape, _NOISE_PHRASES), key=len, reverse=True)),
    re.IGNORECASE,
)


def is_noise_message(
    content: str,
    min_length: int = 4,
    enable_phrase_match: bool = True,
    enable_regex_match: bool = True,
) -> bool:
    """判断消息是否为低价值噪声

    【P11 Noise Filtering】
    过滤不应进入知识库的低价值对话内容：
    - 过短消息（< min_length 字符）
    - 纯标点 / 纯 emoji
    - 常见敷衍短语（"好的", "ok", "继续" 等）

    Args:
        content: 消息内容
        min_length: 最小有效长度（默认 4，中文语境下 "继续"=6 会被过滤）
        enable_phrase_match: 启用短语匹配
        enable_regex_match: 启用正则匹配

    Returns:
        True = 是噪声，应跳过
    """
    if not content or not isinstance(content, str):
        return True

    stripped = content.strip()

    # 1. 空内容
    if not stripped:
        return True

    # 2. 长度检查
    if len(stripped) < min_length:
        return True

    # 3. 正则匹配（纯标点 / 纯 emoji）
    if enable_regex_match:
        for pattern in _NOISE_RE_PATTERNS:
            if pattern.match(stripped):
                return True

    # 4. 短语匹配（去除标点后精确匹配）
    if enable_phrase_match:
        # 提取核心文本（去除标点、空白、大小写）
        core = re.sub(r"[^\w\u4e00-\u9fa5]", "", stripped).lower()
        if core in _NOISE_PHRASES:
            return True

    # 5. 重复字符检测（如 "哈哈哈哈哈哈" / "oooooooo"）
    if len(set(stripped)) <= 3 and len(stripped) >= 6:
        return True

    # 6. 噪声短语占比过高（如 "好的 收到 谢谢" / "ok thanks got it"）
    if enable_phrase_match and len(stripped) <= 40:
        meaningful = re.sub(r"[^\w\u4e00-\u9fa5]", "", stripped)
        if meaningful:
            noise_chars = sum(len(m.group()) for m in _NOISE_PHRASES_RE.finditer(stripped))
            if noise_chars / len(meaningful) >= 0.8:
                return True

    return False


# ==================== 消息质量评分 (P13) ====================

def score_message_quality(content: str) -> Dict[str, float]:
    """轻量消息质量评分（纯规则，零 API 成本）

    【P13 Content Quality Score Before Ingest】
    针对聊天消息优化的快速评分：
    - 内容长度（适中为佳，过短/过长都扣分）
    - 信息密度（有效词 / 停用词比例）
    - 语义丰富度（词汇多样性 + 价值信号命中）

    返回分数 0-100，以及各维度明细。
    当前策略：只评分不拦截，记录后观察再设门槛。

    Returns:
        {
            "total_score": float,      # 总分 0-100
            "length_score": float,     # 长度维度 0-30
            "density_score": float,    # 密度维度 0-35
            "richness_score": float,   # 丰富度维度 0-35
            "details": {
                "char_count": int,
                "valid_word_count": int,
                "stopword_count": int,
                "unique_ratio": float,
                "value_signals": int,
            }
        }
    """
    if not content or not isinstance(content, str):
        return _empty_quality_result()

    stripped = content.strip()
    char_count = len(stripped)
    words = _extract_words(stripped)
    if not words:
        return _empty_quality_result(char_count=char_count)

    length_score = _compute_length_score(char_count)
    density_score, valid_words, total_words = _compute_density_score(words, _STOPWORDS_ZH)
    richness_score, value_signals, unique_ratio = _compute_richness_score(
        stripped, words, total_words
    )

    return _build_quality_result(
        length=length_score,
        density=density_score,
        richness=richness_score,
        valid_words=valid_words,
        total_words=total_words,
        value_signals=value_signals,
        unique_ratio=unique_ratio,
        char_count=char_count,
    )


def _empty_quality_result(char_count: int = 0) -> Dict[str, float]:
    """空内容质量结果"""
    return {
        "total_score": 0.0,
        "length_score": 0.0,
        "density_score": 0.0,
        "richness_score": 0.0,
        "details": {  # type: ignore[dict-item]
            "char_count": char_count,
            "valid_word_count": 0,
            "stopword_count": 0,
            "unique_ratio": 0.0,
            "value_signals": 0,
        },
    }


# ==================== 实体/概念回退提取 ====================


def _clean_wiki_refs(content: str) -> str:
    """移除 Wiki 引用标记 [[...]]，用于后续文本处理。"""
    return _WIKI_REF_RE.sub(r"\1", content)


def _word_match(text: str, term: str) -> bool:
    """检查 term 在 text 中是否出现。

    - 纯 ASCII 英文词：要求整词匹配（避免 SQL 从 PostgreSQL 误报）
    - 含中文的词：允许子串匹配（中文复合词是常态，如"函数"在"高阶函数"中合理）
    """
    if not term:
        return False

    # 判断 term 类型
    has_zh = bool(re.search(r"[\u4e00-\u9fa5]", term))
    text_lower = text.lower()
    term_lower = term.lower()

    if not has_zh:
        # 纯英文/数字：整词匹配
        idx = 0
        max_iter = len(text) + 1
        iteration = 0
        while iteration < max_iter:
            pos = text_lower.find(term_lower, idx)
            if pos == -1:
                return False
            prev_ok = pos == 0 or not re.match(r"[a-z0-9_]", text_lower[pos - 1])
            end = pos + len(term)
            next_ok = end >= len(text) or not re.match(r"[a-z0-9_]", text_lower[end])
            if prev_ok and next_ok:
                return True
            idx = pos + 1
            iteration += 1
        return False
    else:
        # 含中文：子串匹配即可
        return term_lower in text_lower


def extract_entities_fallback(content: str) -> List[str]:
    """实体提取回退方案（增强版）

    零 API 成本，通过技术词典 + 模式匹配识别：
    - 编程语言、框架、库、工具、平台（英文精确匹配）
    - 中文技术实体（xxx语言/框架/引擎/系统等）
    - CamelCase 标识符
    - 大写缩写（2-10 字母）
    """
    entities = set()
    clean = _clean_wiki_refs(content)

    # 1. 英文技术实体词典匹配（整词匹配，避免子串误报）
    for term in _ENTITY_TECH_TERMS:
        if _word_match(clean, term):
            entities.add(term)

    # 2. CamelCase 标识符
    for m in _ENTITY_CAMEL_RE.finditer(clean):
        name = m.group(1)
        if len(name) > 3:
            entities.add(name)

    # 3. 大写缩写（补充词典未覆盖的，如 HTTP 已在词典中）
    for m in _ENTITY_ACRONYM_RE.finditer(clean):
        # group(1) 是捕获组中的缩写，group(0) 包含前导分隔符
        acronym = m.group(1) if m.lastindex else m.group(0)
        # 清理前导空格/标点
        acronym = acronym.strip()
        if 2 <= len(acronym) <= 10:
            entities.add(acronym)

    # 4. 中文技术实体（xxx语言/框架/引擎/系统等）
    for m in _ENTITY_ZH_RE.finditer(clean):
        name = m.group(0)
        if len(name) >= 4:
            entities.add(name)

    # 5. 版本号关联：Python 3、React 18 → 提取基础名
    for m in re.finditer(r"([A-Za-z][A-Za-z0-9+#]{1,15})\s*[\d\.]+", clean):
        base = m.group(1)
        if base in _ENTITY_TECH_TERMS or len(base) > 3:
            entities.add(base)

    # 去重并限制数量（按字母序，优先保留词典命中的）
    result = sorted(entities, key=lambda x: (x not in _ENTITY_TECH_TERMS, x.lower()))
    return result[:12]


def extract_concepts_fallback(content: str) -> List[str]:
    """概念提取回退方案（增强版）

    零 API 成本，通过概念词典 + 模式匹配识别：
    - 编程概念、设计模式、算法、数据结构、架构思想（英文精确匹配）
    - 中文技术概念（xxx编程/设计/架构/原理/方法等）
    """
    concepts = set()
    clean = _clean_wiki_refs(content)
    clean.lower()

    # 1. 概念词典匹配
    for term in _CONCEPT_TECH_TERMS:
        if _word_match(clean, term):
            concepts.add(term)

    # 2. "xxx 是 yyy" 定义句式中提取概念词（前缀必须是明确的技术词）
    # 只匹配 2-4 字技术前缀 + 编程/设计/架构/方法/模式/原理/机制
    _concept_prefixes = {
        "面向对象",
        "函数式",
        "命令式",
        "声明式",
        "响应式",
        "并发",
        "异步",
        "同步",
        "事件驱动",
        "数据驱动",
        "领域驱动",
        "测试驱动",
        "行为驱动",
        "对象",
        "函数",
        "类",
        "模块",
        "组件",
        "服务",
        "接口",
        "协议",
        "装饰器",
        "生成器",
        "迭代器",
        "闭包",
        "回调",
        "代理",
        "适配器",
        "单例",
        "工厂",
        "观察者",
        "策略",
        "模板",
        "访问者",
        "快速",
        "归并",
        "堆",
        "冒泡",
        "插入",
        "选择",
        "计数",
        "桶",
        "基数",
        "广度优先",
        "深度优先",
        "二分",
        "线性",
        "动态规划",
        "贪心",
        "分治",
        "回溯",
    }
    for prefix in _concept_prefixes:
        for suffix in ("编程", "设计", "架构", "开发", "方法", "模式", "原理", "机制"):
            term = prefix + suffix
            if term in clean:
                concepts.add(term)

    # 去重排序，优先保留词典命中项
    result = sorted(concepts, key=lambda x: (x not in _CONCEPT_TECH_TERMS, x.lower()))
    return result[:12]


# ==================== 句子级抽取 ====================


def extract_entity_description(entity: str, content: str) -> str:
    """从内容中提取实体的描述（包含该实体的句子）"""
    sentences = re.split(r"[。！？\n]", content)
    for sent in sentences:
        if entity in sent and len(sent.strip()) > 10:
            return sent.strip()
    return f"涉及{entity}的相关记录"


def extract_concept_definition(concept: str, content: str) -> str:
    """从内容中提取概念的定义

    匹配 "xxx是..." / "xxx指的是..." 等定义模式。
    """
    patterns = [
        rf"{re.escape(concept)}是(.+?)[。；]",
        rf"{re.escape(concept)}指的是(.+?)[。；]",
        rf"{re.escape(concept)}(.+?)模式",
        rf"{re.escape(concept)}(.+?)方法",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return match.group(0)
    return f"关于{concept}的相关记录"


# ==================== 循环污染检测 ====================


def detect_wiki_reference_pollution(content: str, tags: List[str]) -> Tuple[bool, float, str]:
    """检测内容是否被 Wiki 引用污染（循环污染检测）

    【循环污染防护 - 第 3 层】
    检测信号：
    1. 内容中包含大量 [[Wiki引用]] 格式
    2. 标签表明内容来自 AI 对话（source=claude/hermes 等）
    3. 引用密度过高（>30% 的内容是引用）

    Returns:
        (是否污染, 污染指数 0-1, 原因)
    """
    wiki_refs = re.findall(r"\[\[([^\]]+)\]\]", content)
    ref_count = len(wiki_refs)

    if ref_count == 0:
        return False, 0.0, "No wiki references"

    # 引用密度
    ref_chars = sum(len(ref) for ref in wiki_refs)
    total_chars = len(content)
    density = ref_chars / total_chars if total_chars > 0 else 0

    # 来源标签
    source_tags = [t for t in tags if t.startswith("source=")]
    ai_sources = ["source=claude", "source=hermes", "source=openclaw", "source=ai"]
    is_ai_source = any(s in st for st in source_tags for s in ai_sources)

    # 显式 Wiki 引用标记
    has_wiki_ref_tag = any("wiki-ref" in t or t == "contains:wiki-refs" for t in tags)

    # 判定
    if has_wiki_ref_tag and is_ai_source:
        return True, density, "AI-generated content explicitly marked as containing wiki references"

    if is_ai_source and density > 0.3:
        return (
            True,
            density,
            f"AI-generated content with high wiki reference density ({density:.1%})",
        )

    if ref_count > 10:
        return True, density, f"Excessive wiki references ({ref_count})"

    return False, density, "Within acceptable range"


# ==================== Prompt Injection 检测 (H8 系统化威胁扫描) ====================

# 20+ 种威胁模式，按类别分组
_PI_PATTERNS = [
    # === Category 1: 指令覆盖 (prompt_injection) ===
    (
        re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
        "prompt_injection",
        0.95,
    ),
    (
        re.compile(r"forget\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
        "prompt_injection",
        0.95,
    ),
    (
        re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
        "prompt_injection",
        0.95,
    ),
    (
        re.compile(r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", re.I),
        "prompt_injection",
        0.95,
    ),
    # 中文指令覆盖
    (re.compile(r"忽略\s*(之前|以上|前面)\s*的?指令", re.I), "prompt_injection", 0.95),
    (re.compile(r"忘记\s*(之前|以上|前面)\s*的?指令", re.I), "prompt_injection", 0.95),
    # === Category 2: 角色劫持 (role_hijack) ===
    (
        re.compile(
            r"you\s+are\s+now\s+(a\s+)?(new\s+)?(role|assistant|bot|ai\s+model|expert|advisor|developer)",  # noqa: E501
            re.I,
        ),
        "role_hijack",
        0.70,
    ),
    (
        re.compile(
            r"from\s+now\s+on\s+you\s+are\s+(a\s+)?(new\s+)?(role|assistant|bot|ai|expert)", re.I
        ),
        "role_hijack",
        0.75,
    ),
    (
        re.compile(
            r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)",  # noqa: E501
            re.I,
        ),
        "role_hijack",
        0.80,
    ),
    # 中文角色劫持
    (
        re.compile(r"你现在(是|扮演|作为)(一个)?(新的)?(角色|助手|AI|专家|顾问)", re.I),
        "role_hijack",
        0.70,
    ),
    (
        re.compile(r"从现在开始你(是|扮演|作为)(一个)?(新的)?(角色|助手|AI|专家|顾问)", re.I),
        "role_hijack",
        0.75,
    ),
    (re.compile(r"(roleplay|角色扮演)\s*[:-]\s*(as|扮演)", re.I), "role_hijack", 0.60),
    # === Category 3: 系统提示操作 (sys_prompt_override) ===
    (re.compile(r"system\s*prompt", re.I), "sys_prompt_override", 0.80),
    (re.compile(r"system\s+prompt\s+override", re.I), "sys_prompt_override", 0.90),
    (re.compile(r"系统提示", re.I), "sys_prompt_override", 0.80),
    # 中文系统提示操作
    (re.compile(r"系统\s*提示\s*(覆盖|修改|替换)", re.I), "sys_prompt_override", 0.90),
    # === Category 4: 越狱/开发者模式 (jailbreak) ===
    (re.compile(r"jailbreak", re.I), "jailbreak", 0.90),
    (re.compile(r"developer\s*mode", re.I), "jailbreak", 0.85),
    (re.compile(r"D\.?A\.?N\.?", re.I), "jailbreak", 0.90),
    (re.compile(r"do\s+anything\s+now", re.I), "jailbreak", 0.85),
    (re.compile(r"越狱模式", re.I), "jailbreak", 0.90),
    (re.compile(r"开发者模式", re.I), "jailbreak", 0.85),
    # === Category 5: 欺骗/隐藏 (deception) ===
    (re.compile(r"do\s+not\s+tell\s+(the\s+)?user", re.I), "deception_hide", 0.85),
    (re.compile(r"不要告诉用户", re.I), "deception_hide", 0.85),
    (re.compile(r"对用户隐藏", re.I), "deception_hide", 0.80),
    # === Category 6: 分隔符滥用 (delimiter_abuse) ===
    (
        re.compile(r"[-=]{10,}\s*\n\s*(ignore|forget|you are|忽略|你现在)", re.I),
        "delimiter_abuse",
        0.85,
    ),
    # === Category 7: 隐藏内容注入 (hidden_content) ===
    (
        re.compile(r"<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->", re.I),
        "hidden_content",
        0.75,
    ),
    (
        re.compile(r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', re.I),
        "hidden_content",
        0.70,
    ),
    # === Category 8: 数据渗透 (exfiltration) ===
    (
        re.compile(r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.I),
        "exfiltration",
        0.90,
    ),
    (
        re.compile(r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.I),
        "exfiltration",
        0.90,
    ),
    (re.compile(r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)", re.I), "exfiltration", 0.85),
    # === Category 9: 持久化/后门 (persistence) ===
    (re.compile(r"authorized_keys", re.I), "persistence", 0.80),
    (re.compile(r"\$HOME/\.ssh|\~/\.ssh", re.I), "persistence", 0.80),
    # === Category 10: 翻译执行 (translate_execute) ===
    (
        re.compile(r"translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)", re.I),
        "translate_execute",
        0.75,
    ),
]

# 不可见字符（零宽字符、双向文本覆盖）
_PI_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",  # zero-width
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",  # bidirectional override
}

# 敏感关键词（用于组合检测）
_PI_SENSITIVE_KEYWORDS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "密码",
    "密钥",
    "令牌",
    "凭证",
}

# 类别组合乘数：某些类别同时出现 = 风险叠加
_CATEGORY_MULTIPLIERS = {
    # prompt_injection + exfiltration = 数据渗透指令覆盖（高危）
    frozenset({"prompt_injection", "exfiltration"}): 1.3,
    # role_hijack + deception_hide = 恶意角色扮演（高危）
    frozenset({"role_hijack", "deception_hide"}): 1.2,
    # sys_prompt_override + hidden_content = 隐蔽的系统提示修改（高危）
    frozenset({"sys_prompt_override", "hidden_content"}): 1.25,
    # jailbreak + persistence = 越狱后植入后门（高危）
    frozenset({"jailbreak", "persistence"}): 1.3,
}


def detect_prompt_injection(content: str) -> Tuple[bool, float, str, List[str], Dict]:
    """检测内容是否包含 Prompt Injection 攻击

    【H8 Prompt Injection 系统化威胁扫描】
    零 LLM 成本规则检测，识别 10 类威胁模式 + 不可见字符 + 组合风险。

    威胁类别：
    1. prompt_injection — 指令覆盖
    2. role_hijack — 角色劫持
    3. sys_prompt_override — 系统提示操作
    4. jailbreak — 越狱/开发者模式
    5. deception_hide — 欺骗/隐藏
    6. delimiter_abuse — 分隔符滥用
    7. hidden_content — 隐藏内容注入（HTML 注释、display:none）
    8. exfiltration — 数据渗透（curl/wget/cat 敏感文件）
    9. persistence — 持久化/后门（ssh authorized_keys）
    10. translate_execute — 翻译执行链

    组合检测：多类别同时命中时应用 risk multiplier
    不可见字符：检测零宽字符和双向文本覆盖

    Returns:
        (是否检测到, 风险分数 0-1, 原因, 匹配模式列表, 详细结果字典)
    """
    matched_patterns = []
    matched_categories = set()
    max_base_score = 0.0

    # 1. 规则匹配
    for pattern, category, score in _PI_PATTERNS:
        if pattern.search(content):
            matched_patterns.append(f"[{category}] {pattern.pattern[:40]}...")
            matched_categories.add(category)
            max_base_score = max(max_base_score, score)

    # 2. 不可见字符检测
    invisible_count = sum(1 for ch in content if ch in _PI_INVISIBLE_CHARS)
    if invisible_count > 0:
        matched_patterns.append(f"[invisible_chars] 发现 {invisible_count} 个不可见字符")
        matched_categories.add("invisible_chars")
        max_base_score = max(max_base_score, min(0.5 + invisible_count * 0.1, 0.90))

    # 3. 敏感关键词组合加分
    has_sensitive = any(kw in content.lower() for kw in _PI_SENSITIVE_KEYWORDS)
    if has_sensitive and max_base_score > 0.3:
        max_base_score = min(1.0, max_base_score + 0.08)

    # 4. 类别组合乘数
    multiplier = 1.0
    for cat_combo, mult in _CATEGORY_MULTIPLIERS.items():
        if cat_combo.issubset(matched_categories):
            multiplier = max(multiplier, mult)

    final_score = min(1.0, max_base_score * multiplier)

    # 5. 构建详细结果
    detail = {
        "base_score": max_base_score,
        "multiplier": multiplier,
        "final_score": final_score,
        "categories": sorted(matched_categories),
        "pattern_count": len(matched_patterns),
        "invisible_chars": invisible_count,
        "has_sensitive_keywords": has_sensitive,
    }

    # 6. 分级返回
    if final_score >= 0.85:
        return (
            True,
            final_score,
            f"High-risk threat detected: {', '.join(sorted(matched_categories))}",
            matched_patterns,
            detail,
        )
    elif final_score >= 0.60:
        return (
            True,
            final_score,
            f"Suspicious signal: {', '.join(sorted(matched_categories))}",
            matched_patterns,
            detail,
        )

    return False, final_score, "Clean", matched_patterns, detail


# ==================== 标签体系辅助 (v6.0) ====================

# 有效系统标签键
VALID_SYSTEM_TAG_KEYS = {
    "type",
    "status",
    "stage",
    "evidence",
    "level",
    "actionable",
    "temporal",
}

# 有效业务标签键
VALID_BUSINESS_TAG_KEYS = {
    "domain",
    "project",
    "source",
    "verify",
}


def parse_tags(tag_list: List[str]) -> Dict[str, str]:
    """解析 key=value 格式标签列表

    Args:
        tag_list: 标签字符串列表，如 ["type=heuristic", "stage=captured"]

    Returns:
        {key: value} 字典
    """
    result = {}
    for tag in tag_list:
        if "=" in tag:
            key, value = tag.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def format_tag(key: str, value: str) -> str:
    """格式化单个标签

    Args:
        key: 标签键
        value: 标签值

    Returns:
        "key=value" 格式字符串
    """
    return f"{key}={value}"


def validate_tag(tag: str) -> Tuple[bool, str]:
    """验证标签格式和键名是否有效

    Args:
        tag: 标签字符串

    Returns:
        (是否有效, 原因)
    """
    if "=" not in tag:
        return False, "标签必须使用 key=value 格式"

    key, _value = tag.split("=", 1)
    key = key.strip()

    if key in VALID_SYSTEM_TAG_KEYS or key in VALID_BUSINESS_TAG_KEYS:
        return True, ""

    # 允许自定义键（以 x- 前缀）
    if key.startswith("x-"):
        return True, ""

    return False, f"未知标签键 '{key}'，建议使用 x-{key} 前缀"


def _with_prompt_injection_tags(tags: List[str], content: str) -> List[str]:
    detected, score, _reason, _patterns, detail = detect_prompt_injection(content)
    if not detected:
        return tags

    enriched = list(tags)
    additions = [
        "x-security=prompt-injection",
        f"x-risk={'high' if score >= 0.85 else 'medium'}",
    ]
    categories = detail.get("categories") or []
    if categories:
        additions.append(f"x-threat={','.join(categories)}")
    for tag in additions:
        if tag not in enriched:
            enriched.append(tag)
    return enriched


def extract_tags_from_frontmatter(content: str) -> List[str]:
    """从 Markdown frontmatter 中提取 tags 字段

    Args:
        content: Markdown 内容

    Returns:
        标签列表
    """
    if not content.startswith("---"):
        return []

    parts = content.split("---", 2)
    if len(parts) < 3:
        return []

    frontmatter = parts[1]

    # 简单解析 tags 行
    # 支持: tags: [a, b, c] 或 tags:\n  - a\n  - b
    tags = []

    # 数组格式
    match = re.search(r"tags:\s*\[(.*?)\]", frontmatter, re.DOTALL)
    if match:
        items = match.group(1).split(",")
        tags = [t.strip().strip("\"'") for t in items if t.strip()]
        return _with_prompt_injection_tags(tags, content)

    # 列表格式
    in_tags = False
    for line in frontmatter.split("\n"):
        if line.strip().startswith("tags:"):
            in_tags = True
            continue
        if in_tags:
            if line.strip().startswith("-"):
                tag = line.strip()[1:].strip().strip("\"'")
                if tag:
                    tags.append(tag)
            elif line.strip() and not line.startswith(" "):
                break

    return _with_prompt_injection_tags(tags, content)


def build_tag_string(tags: Dict[str, str]) -> str:
    """将标签字典转换为 YAML 格式的 tags 数组字符串

    Args:
        tags: {key: value} 字典

    Returns:
        YAML 数组字符串，如 "tags:\n  - type=problem-solution\n  - stage=captured"
    """
    lines = ["tags:"]
    for key, value in tags.items():
        tag = format_tag(key, value)
        is_valid, reason = validate_tag(tag)
        if not is_valid:
            raise ValueError(reason)
        lines.append(f"  - {tag}")
    return "\n".join(lines)


# ==================== L2 价值预判定 (E14) ====================


def layer2_value_prejudge(
    content: str,
    rule_score: Dict[str, float] | None = None,
    v2_score=None,
) -> Dict[str, Any]:
    """L2 层价值预判定 —— 连接 RuleScorer / AdaptiveScorerV2 评分与蒸馏决策

    【E14 蒸馏层对齐 + 阶段二 V2 桥接】
    根据 quality score 将消息分为三类：
    - direct_distill (>=70): 高价值，直接进入蒸馏队列
    - skip (<=30): 低价值，跳过不处理
    - llm_judge (30-70): 中间值，交由 LLM 二次判断

    若传入 v2_score（ScoreCardV2），将 V2 的 distill_score（0-1 域）
    与 rule_score（0-100 域）融合后再判定。融合权重：rule 0.5 + V2 0.5。

    Args:
        content: 消息内容
        rule_score: score_message_quality() 的返回结果（可选，不传则自动评分）
        v2_score: AdaptiveScorerV2.score() 返回的 ScoreCardV2（可选）

    Returns:
        {
            "decision": "direct_distill" | "skip" | "llm_judge",
            "score": float,           # 总分（0-100 域）
            "threshold": int,         # 判定阈值
            "reason": str,            # 判定原因
            "confidence": float,      # 判定置信度
            "sources": Dict,          # 各来源分数明细
        }
    """
    # 自动评分（如果未提供）
    if rule_score is None:
        rule_score = score_message_quality(content)

    rule_total = rule_score.get("total_score", 0.0)
    sources = {"rule": rule_total}

    # V2 评分融合（可选，零阻塞）
    v2_distill = None
    if v2_score is not None:
        try:
            v2_distill = v2_score.scores.get("distill")
            if v2_distill is not None:
                # V2 域为 0-1，映射到 0-100
                v2_mapped = v2_distill * 100.0
                sources["v2_distill"] = round(v2_mapped, 2)
                # 融合：等权重平均
                total_score = (rule_total + v2_mapped) / 2.0
            else:
                total_score = rule_total
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            total_score = rule_total
    else:
        total_score = rule_total

    # 阈值设定
    DIRECT_THRESHOLD = 70.0
    SKIP_THRESHOLD = 30.0

    if total_score >= DIRECT_THRESHOLD:
        return {
            "decision": "direct_distill",
            "score": total_score,
            "threshold": DIRECT_THRESHOLD,
            "reason": f"融合评分 {total_score:.1f} >= {DIRECT_THRESHOLD}，高价值内容直接进入蒸馏队列",
            "confidence": min(1.0, (total_score - DIRECT_THRESHOLD) / 30.0 + 0.7),
            "sources": sources,
        }

    if total_score <= SKIP_THRESHOLD:
        return {
            "decision": "skip",
            "score": total_score,
            "threshold": SKIP_THRESHOLD,
            "reason": f"融合评分 {total_score:.1f} <= {SKIP_THRESHOLD}，低价值内容跳过",
            "confidence": min(1.0, (SKIP_THRESHOLD - total_score) / 30.0 + 0.7),
            "sources": sources,
        }

    # 中间值：LLM 二次判断
    reason = f"融合评分 {total_score:.1f} 处于中间区间 ({SKIP_THRESHOLD}-{DIRECT_THRESHOLD})，需 LLM 二次判断"
    if v2_distill is not None:
        reason += f" (V2 distill={v2_distill:.2f})"
    return {
        "decision": "llm_judge",
        "score": total_score,
        "threshold": (SKIP_THRESHOLD, DIRECT_THRESHOLD),
        "reason": reason,
        "confidence": 0.5,
        "sources": sources,
    }
