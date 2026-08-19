"""
7 个通用维度的提取器

每个提取器接收 SourceItem 列表，输出 Observation 列表。

设计原则：
- 只提取客观事实（频次、模式、趋势、偏差）
- 不生成洞察/解释
- 不依赖 LLM（纯统计 + 规则），确保低成本、可回溯
- 优雅处理少数据/空数据的情况

**内容分层处理规则：**

1. EXTERNAL_QUOTED（外部引用）→ 仅进入 Attention 维度
   - 外部文本不反映用户认知，但"用户选择读什么"是行为信号
   - 其他维度（Decisions/Actions/Time/Stress/Relationships/Growth）完全排除

2. LIKELY_PASTED（疑似复制粘贴）→ 全部维度保留，confidence 降级 ×0.7
   - 粘贴行为本身是信号，内容可提取认知
   - 但内容可能不是用户原话，置信度降低

3. SYSTEM_GENERATED → 已在 SourceReader 阶段排除，不会到达提取器

4. NATIVE_DIALOGUE / USER_NOTE → 正常提取，不降级
"""

import re
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime
from typing import Dict, Iterator, List, Tuple

from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.sources import ContentSource, ContentTier, SourceItem, UserIntent

# Constants extracted from magic numbers
CLEAN_CONTENT = 999999
SNIPPET = 90
CLEAN_ALL_TEXT = 999999


class DimensionExtractor(ABC):
    """维度提取器基类"""

    dimension: Dimension

    # 是否接受 EXTERNAL_QUOTED 内容（默认 False，只有 Attention 为 True）
    accepts_external_quoted: bool = False

    def _filter_items(self, items: List[SourceItem]) -> Tuple[List[SourceItem], bool]:
        """
        根据内容来源过滤 items

        Returns:
            (filtered_items, has_pasted_content)
        """
        filtered = []
        has_pasted = False

        for item in items:
            # 系统生成：理论上已被 SourceReader 排除，双重保险
            if item.content_tier == ContentTier.SYSTEM_GENERATED:
                continue

            # 外部引用：只有 Attention 维度接受
            if item.content_tier == ContentTier.EXTERNAL_QUOTED:
                if self.accepts_external_quoted:
                    filtered.append(item)
                continue

            # 复制粘贴：标记但不排除
            if item.content_tier == ContentTier.LIKELY_PASTED:
                has_pasted = True

            # 其他：正常进入
            filtered.append(item)

        return filtered, has_pasted

    def _adjust_confidence(self, base_confidence: float, has_pasted: bool) -> float:
        """
        根据内容来源调整置信度
        - 包含 LIKELY_PASTED 内容 → confidence × 0.7
        """
        if has_pasted:
            return round(base_confidence * 0.7, 2)
        return base_confidence

    def _extract_signal_snippets(
        self,
        items: List[SourceItem],
        signals: List[str],
        snippet_max_chars: int,
        snippet_max_length: int,
        clean_max_length: int = CLEAN_CONTENT,
    ) -> Tuple[int, List[str]]:
        """从 items 中提取信号匹配次数和去重后的典型情境片段。"""
        total = 0
        snippets = []
        for item in items:
            clean_content = self._clean_evidence(
                item.content, max_length=clean_max_length, strict=False
            )
            if not clean_content:
                continue
            for signal in signals:
                matches = list(re.finditer(signal, clean_content, re.IGNORECASE))
                total += len(matches)
                for m in matches:
                    sentence = self._extract_sentence(
                        clean_content, m.start(), max_chars=snippet_max_chars
                    )
                    snippet = self._clean_evidence(
                        sentence,
                        max_length=snippet_max_length,
                        strict=True,
                    )
                    if (
                        snippet
                        and self._is_clean_snippet(snippet)
                        and snippet not in snippets
                    ):
                        snippets.append(snippet)
        return total, snippets

    def _extract_sentence(self, text: str, pos: int, max_chars: int = 100) -> str:
        """
        提取包含 pos 位置的完整句子，而非固定字符窗口。

        避免截取截在列表项、代码块或元数据标记中间。
        """
        if not text or pos >= len(text):
            return ""

        # 句子边界字符
        sentence_end = "。！？.!?；;\n"

        # 向前找句子开头
        start = 0
        for i in range(pos, max(0, pos - max_chars * 2), -1):
            if text[i] in sentence_end:
                start = i + 1
                break

        # 向后找句子结尾
        end = len(text)
        for i in range(pos, min(len(text), pos + max_chars * 2)):
            if text[i] in sentence_end:
                end = i + 1
                break

        return text[start:end].strip()

    def _is_clean_snippet(self, text: str) -> bool:
        """
        质量门：判断 snippet 是否足够干净，可以作为典型情境。

        拒绝包含以下特征的内容：
        - 编号列表（1. / 2. / (1)）
        - wiki 系统元数据（蒸馏时间、token 数、轮数）
        - 过多特殊符号（→、⇒、|、/）
        - 日期时间标记
        """
        if not text or len(text) < 15:
            return False

        # 拒绝编号列表
        if re.search(r"^\s*\d+[\.、]\s", text):
            return False

        # 拒绝 wiki 系统元数据 / 系统生成标记 / 文档结构标记
        dirty_keywords = [
            "蒸馏时间",
            "token",
            "轮",
            "会话覆盖",
            "保留开头",
            "来源追踪",
            "自动生成",
            "Observation Engine",
            "本文件由",
            "系统生成",
            "自动校准",
            "来源文件",
            "confirmed",
            "questionable",
            "refuted",
            "验证方法",
            "对于每个维度",
            "对于每个",
            "背景",
            "核心内容",
            "反例",
            "不适用于",
            "扩展性：",
            "适用场景",
            "原文引用",
            "核心发现",
            "怎么用",
            "按步骤执行",
        ]
        if any(kw in text for kw in dirty_keywords):
            return False

        # 拒绝编号列表（出现在任何位置，如 "1. xxx" 或 "xxx 3."）
        if re.search(r"\d+[\.、](?:\s|$)", text):
            return False

        # 拒绝过多特殊符号（非自然语言特征）
        special_chars = sum(1 for c in text if c in "→⇒|/\\{}[]<>@#$")
        if special_chars > 3:
            return False

        # 拒绝日期时间模式（如 2026-06-04、15:30）
        if re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", text):
            return False

        # 拒绝数字箭头（如 1528→1000）
        if re.search(r"\d+\s*[→⇒]\s*\d+", text):
            return False

        return True

    def _clean_evidence(self, text: str, max_length: int = 120, strict: bool = True) -> str:
        """
        清洗证据片段，去除元数据残留。

        原始内容中可能包含 markdown 标记、代码块、omitted 占位符、
        wiki 链接等，直接截取会让证据看起来是"脏数据"。

        Args:
            text: 待清洗文本
            max_length: 最大长度限制
            strict: 是否执行脏标记兜底检测。清洗整篇 content 时设为 False，
                    避免整篇因一处残留而被丢弃；最终 snippet 清洗时设为 True。
        """
        if not text:
            return ""

        # 1. 去除代码块（多行和单行），处理截断残留
        text = re.sub(r"```[\s\S]*?```", " ", text)
        text = re.sub(r"`[^`]+`", " ", text)
        text = re.sub(r"```", " ", text)  # 任何残留的 ```

        # 2. 去除 markdown 标题标记
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

        # 3. 去除 wiki 链接 [[...]] 和 markdown 链接 [...](...)
        text = re.sub(r"\[\[.*?\]\]", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

        # 4. 去除 markdown 格式标记
        text = re.sub(r"\*\*|\*|__|_|~~", "", text)

        # 5. 去除 "30 lines omitted" 等元数据残留
        text = re.sub(r"\d+\s*lines?\s*omitted", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"lines?\s*omitted", " ", text, flags=re.IGNORECASE)

        # 6. 去除 HTML/XML 标签
        text = re.sub(r"<[^>]+>", " ", text)

        # 7. 去除 URL
        text = re.sub(r"https?://\S+", "", text)

        # 8. 去除模板语法残留 {{...}}、{%...%}、残留的 }}
        text = re.sub(r"\{\{.*?\}\}", " ", text)
        text = re.sub(r"\{%.*?%\}", " ", text)
        text = re.sub(r"\}\}", " ", text)
        text = re.sub(r"\{\{", " ", text)

        # 9. 去除文档结构标记（更全面的覆盖）
        text = re.sub(
            r"字段说明|输入[:：]|输出[:：]|##?\s*(结论|摘要|总结|说明|备注|输出|结构|定义|要求)",
            " ",
            text,
        )
        # 去除作为章节标题的独立文档结构词（前后都是空白/标点）
        text = re.sub(
            r"(?<=[\s，。；：])\s*(结论|摘要|总结|说明|备注)\s*(?=[\s，。；：])",
            " ",
            text,
        )

        # 10. 去除残留的 markdown 列表符号包裹的标题（如 "- ## 输出"）
        text = re.sub(r"[-*]\s*##?\s+", "", text)

        # 11. 去除 wiki 系统追踪标记
        text = re.sub(r"来源追踪\s*[-–—]\s*来源会话[:：].*?(?=\s|$)", " ", text)
        text = re.sub(r"来源\s*Agent[:：]\s*\w+", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"来源会话[:：]\s*\w+", " ", text)
        text = re.sub(r"会话覆盖[:：].*?保留开头.*?\)", " ", text)
        text = re.sub(r"\d+\s*轮", " ", text)

        # 12. 去除列表标记（如 "- Python源码："）
        text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)

        # 13. 去除常见的技术文档结构词
        text = re.sub(
            r"源码类型|Plugin|Hook|二进制|编译后|反编译|官方接口|扩展方式",
            " ",
            text,
        )

        # 14. 合并空白
        text = re.sub(r"\s+", " ", text).strip()

        # 12. 脏标记兜底检测（仅 strict 模式）
        # 清洗整篇 content 时不应因一处残留而丢弃全文
        if strict:
            dirty_markers = ["}}", "{{", "omitted", "## ", "JSON", "json", "```"]
            if any(marker in text for marker in dirty_markers):
                return ""

        # 13. 长度控制：在语义边界截断
        if len(text) > max_length:
            truncated = text[:max_length]
            # 优先在标点处截断
            for delim in ("，", ", ", "。", ". ", "；", "; ", "？", "? ", "！", "! "):
                last_pos = truncated.rfind(delim)
                if last_pos > max_length * 0.5:
                    truncated = truncated[: last_pos + len(delim)].rstrip()
                    break
            else:
                # 其次在空格处截断
                last_space = truncated.rfind(" ")
                if last_space > max_length * 0.4:
                    truncated = truncated[:last_space]
            text = truncated.strip() + "..."

        return text

    @abstractmethod
    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        """从 SourceItem 列表中提取观察"""

    def _make_obs(
        self,
        obs_type: ObservationType,
        value,
        unit: str = "",
        confidence: float = 1.0,
        evidence: List[str] | None = None,
        source_type: SourceType = SourceType.WIKI,
        source_path: str = "",
        source_id: str = "",
        observed_at: datetime | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        content_source: ContentSource = ContentSource.UNKNOWN,
        user_intent_signal: UserIntent = UserIntent.UNKNOWN,
    ) -> Observation:
        """辅助方法：创建 Observation"""
        return Observation(
            dimension=self.dimension,
            observation_type=obs_type,
            value=value,
            unit=unit,
            confidence=confidence,
            source_type=source_type,
            source_path=source_path,
            source_id=source_id,
            evidence=evidence or [],
            observed_at=observed_at,
            period_start=period_start,
            period_end=period_end,
            content_source=content_source,
            user_intent_signal=user_intent_signal,
        )


# ───────────────────────────────────────────────
# 1. Attention（关注分布）
# ───────────────────────────────────────────────


class AttentionExtractor(DimensionExtractor):
    """
    提取用户长期关注的话题/概念分布

    特殊规则：
    - 接受 EXTERNAL_QUOTED 内容（行为信号：用户选择关注什么外部信息）
    - 为 EXTERNAL_QUOTED 单独统计"外部信息关注点"
    - 统计行为信号：复制粘贴次数、外部文件引用次数、意图分布
    """

    dimension = Dimension.ATTENTION
    accepts_external_quoted = True

    # 关注的概念类别（从内容中匹配）
    CONCEPT_PATTERNS = {
        "ai": r"\bAI\b|人工智能|大模型|LLM|GPT|Claude|Kimi",
        "coding": r"\b代码\b|编程|重构|技术债|架构|设计模式",
        "product": r"产品|需求|用户|体验|功能|迭代",
        "business": r"业务|GMV|订单|收入|客户|运营",
        "career": r"职业|成长|晋升|管理|领导|团队",
        "health": r"健康|运动|睡眠|饮食|身体",
        "family": r"家庭|孩子|父母|伴侣|家",
        "learning": r"学习|读书|课程|知识|技能",
        "decision": r"决策|选择|优先级|权衡|判断",
        "system": r"系统|流程|自动化|效率|工具",
    }

    ATTENTION_STOP_WORDS = {
        "the",
        "and",
        "for",
        "are",
        "but",
        "not",
        "you",
        "all",
        "can",
        "her",
        "was",
        "one",
        "our",
        "out",
        "day",
        "get",
        "has",
        "him",
        "his",
        "how",
        "its",
        "may",
        "new",
        "now",
        "old",
        "see",
        "two",
        "way",
        "who",
        "boy",
        "did",
        "she",
        "use",
        "way",
        "many",
        "oil",
        "sit",
        "set",
        "run",
        "eat",
        "far",
        "sea",
        "eye",
        "ago",
        "off",
        "too",
        "any",
        "say",
        "man",
        "try",
        "ask",
        "end",
        "why",
        "let",
        "put",
        "say",
        "she",
        "try",
        "way",
        "own",
        "say",
        "too",
        "old",
        "tell",
        "very",
        "when",
        "much",
        "would",
        "there",
        "their",
        "what",
        "said",
        "each",
        "which",
        "will",
        "about",
        "could",
        "other",
        "after",
        "first",
        "never",
        "these",
        "think",
        "where",
        "being",
        "every",
        "great",
        "might",
        "shall",
        "still",
        "those",
        "while",
        "this",
        "that",
        "with",
        "have",
        "from",
        "they",
        "know",
        "want",
        "been",
        "good",
        "much",
        "some",
        "time",
        "very",
        "when",
        "come",
        "here",
        "just",
        "like",
        "long",
        "make",
        "many",
        "over",
        "such",
        "take",
        "than",
        "them",
        "well",
        "were",
        # 中文停用词
        "可以",
        "需要",
        "进行",
        "使用",
        "通过",
        "根据",
        "作为",
        "没有",
        "一个",
        "我们",
        "问题",
        "方法",
        "方式",
        "结果",
        "情况",
        "时候",
        "现在",
        "今天",
        "明天",
        "昨天",
        "然后",
        "但是",
        "因为",
        "所以",
        "如果",
        "虽然",
        "还是",
        "或者",
        "并且",
        "不过",
        "只是",
        "已经",
        "正在",
        "将会",
        "应该",
        "必须",
        "可能",
        "一定",
        "非常",
        "比较",
        "觉得",
        "认为",
        "看到",
        "听到",
        "说到",
        "想到",
        "用到",
    }

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        # 不过滤 EXTERNAL_QUOTED，但区分处理
        filtered_items, has_pasted = self._filter_items(items)

        # 分离原生内容和外部引用内容
        native_items = [i for i in filtered_items if i.content_tier != ContentTier.EXTERNAL_QUOTED]
        external_items = [
            i for i in filtered_items if i.content_tier == ContentTier.EXTERNAL_QUOTED
        ]

        # ── 1. 原生内容的关注分布 ──
        if native_items:
            yield from self._extract_concept_distribution(
                native_items, has_pasted, source_label="native"
            )

        # ── 2. 外部引用内容的关注分布（行为信号）──
        if external_items:
            yield from self._extract_external_attention_signals(external_items)

        # ── 3. 行为信号统计（粘贴、意图等）──
        yield from self._extract_behavior_signals(filtered_items)

    def _extract_concept_distribution(
        self, items: List[SourceItem], has_pasted: bool, source_label: str = "native"
    ) -> Iterator[Observation]:
        """提取概念分布（原生内容）"""
        all_text = "\n".join(item.content for item in items)

        concept_counts = self._count_concepts(all_text)
        if concept_counts:
            yield self._build_concept_observation(concept_counts, items, has_pasted, source_label)

        top_words = self._count_top_words(all_text, self.ATTENTION_STOP_WORDS)
        if top_words:
            yield self._build_top_words_observation(top_words, items, has_pasted, source_label)

    def _count_concepts(self, all_text: str) -> Dict[str, int]:
        """统计概念出现频次。"""
        concept_counts = {}
        for concept, pattern in self.CONCEPT_PATTERNS.items():
            matches = len(re.findall(pattern, all_text, re.IGNORECASE))
            if matches > 0:
                concept_counts[concept] = matches
        return concept_counts

    def _build_concept_observation(
        self,
        concept_counts: Dict[str, int],
        items: List[SourceItem],
        has_pasted: bool,
        source_label: str,
    ) -> Observation:
        """根据概念计数构建 Observation。"""
        total_mentions = sum(concept_counts.values())
        sorted_concepts = sorted(concept_counts.items(), key=lambda x: x[1], reverse=True)

        base_conf = 0.7 if len(items) > 5 else 0.5
        if source_label == "external":
            base_conf = 0.5  # 外部引用内容的置信度更低

        return self._make_obs(
            obs_type=ObservationType.FREQUENCY,
            value={
                "concepts": {k: v for k, v in sorted_concepts},
                "total_mentions": total_mentions,
                "dominant": sorted_concepts[0][0] if sorted_concepts else None,
                "source": source_label,
            },
            unit="mentions",
            confidence=self._adjust_confidence(base_conf, has_pasted),
            evidence=[f"'{k}' 出现 {v} 次" for k, v in sorted_concepts[:3]],
            period_start=min((item.timestamp for item in items if item.timestamp), default=None),
            period_end=max((item.timestamp for item in items if item.timestamp), default=None),
        )

    def _count_top_words(
        self, all_text: str, stop_words: set
    ) -> List[Tuple[str, int]]:
        """统计非停用词的高频词汇。"""
        words = re.findall(r"[一-鿿]{2,}|[a-zA-Z]{3,}", all_text)
        filtered = [w.lower() for w in words if w.lower() not in stop_words and len(w) >= 2]
        return Counter(filtered).most_common(10)

    def _build_top_words_observation(
        self,
        top_words: List[Tuple[str, int]],
        items: List[SourceItem],
        has_pasted: bool,
        source_label: str,
    ) -> Observation:
        """根据高频词汇构建 Observation。"""
        return self._make_obs(
            obs_type=ObservationType.FREQUENCY,
            value={"top_words": dict(top_words), "source": source_label},
            unit="occurrences",
            confidence=self._adjust_confidence(0.6, has_pasted),
            evidence=[f"'{w}' ×{c}" for w, c in top_words[:5]],
            period_start=min((item.timestamp for item in items if item.timestamp), default=None),
            period_end=max((item.timestamp for item in items if item.timestamp), default=None),
        )

    def _extract_external_attention_signals(self, items: List[SourceItem]) -> Iterator[Observation]:
        """提取外部引用内容中的关注信号（行为信号）"""
        if not items:
            return

        # 外部内容的主题分类（简化：按目录/来源判断）
        source_types = Counter()  # type: ignore[var-annotated]
        for item in items:
            if item.content_source == ContentSource.EXTERNAL_FILE:
                source_types["external_file"] += 1
            elif item.content_source == ContentSource.LIKELY_PASTED:
                source_types["likely_pasted"] += 1
            else:
                source_types["unknown_external"] += 1

        if source_types:
            yield self._make_obs(
                obs_type=ObservationType.FREQUENCY,
                value={
                    "external_sources": dict(source_types),
                    "total_external_items": len(items),
                },
                unit="items",
                confidence=0.5,
                evidence=[f"{k}: {v} 次" for k, v in source_types.most_common()],
            )

        # 外部内容的主题词提取（降低权重）
        yield from self._extract_concept_distribution(
            items, has_pasted=False, source_label="external"
        )

    def _extract_behavior_signals(self, items: List[SourceItem]) -> Iterator[Observation]:
        """提取行为信号统计"""
        # 复制粘贴行为统计
        pasted_items = [i for i in items if i.content_source == ContentSource.LIKELY_PASTED]
        if pasted_items:
            # 统计粘贴内容的意图分布
            intent_counts = Counter()  # type: ignore[var-annotated]
            for item in pasted_items:
                if item.user_intent != UserIntent.UNKNOWN:
                    intent_counts[item.user_intent.value] += 1

            value = {
                "pasted_count": len(pasted_items),
                "pasted_ratio": round(len(pasted_items) / max(len(items), 1), 2),
            }
            if intent_counts:
                value["intent_distribution"] = dict(intent_counts)  # type: ignore[assignment]

            yield self._make_obs(
                obs_type=ObservationType.PATTERN,
                value=value,
                unit="signals",
                confidence=0.6,
                evidence=[f"检测到 {len(pasted_items)} 次疑似复制粘贴行为"],
                content_source=ContentSource.LIKELY_PASTED,
            )


# ───────────────────────────────────────────────
# 2. Decisions（决策模式）
# ───────────────────────────────────────────────


class DecisionsExtractor(DimensionExtractor):
    """提取用户的决策模式"""

    dimension = Dimension.DECISIONS
    accepts_external_quoted = False

    DECISION_SIGNALS = [
        r"决定[：:：]",
        r"选择[：:：]",
        r"放弃[了]?",
        r"优先[级]?",
        r"权衡",
        r"取舍",
        r"判断[：:：]?",
        r"结论[：:：]?",
        r"方案[：:：]?",
        r"采用[了]?",
        r"否决[了]?",
        r"通过[了]?",
        r"不[要|做|选]",
        r"还是[要|选|做]",
        r"最终",
        r"确定[了]?",
    ]

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        total_signals, decision_snippets = self._collect_decision_signals(items)
        if total_signals > 0:
            yield self._build_decision_observation(
                total_signals, decision_snippets, items, has_pasted
            )

        topics = self._collect_decision_topics(items)
        if topics:
            yield self._build_topic_observation(topics, has_pasted)

    def _collect_decision_signals(self, items: List[SourceItem]) -> Tuple[int, List[str]]:
        """收集决策信号次数和去重后的典型情境片段。"""
        return self._extract_signal_snippets(
            items, self.DECISION_SIGNALS, snippet_max_chars=100, snippet_max_length=100
        )

    def _build_decision_observation(
        self,
        total_signals: int,
        snippets: List[str],
        items: List[SourceItem],
        has_pasted: bool,
    ) -> Observation:
        """根据决策信号构建 Observation。"""
        evidence = [f"决策信号 {total_signals} 次"]
        if snippets:
            evidence.append(f"典型情境: {snippets[0]}")
        return self._make_obs(
            obs_type=ObservationType.FREQUENCY,
            value={
                "decision_signals": total_signals,
                "snippets_count": len(snippets),
            },
            unit="signals",
            confidence=self._adjust_confidence(
                min(0.9, 0.5 + len(snippets) * 0.02), has_pasted
            ),
            evidence=evidence,
            period_start=min((item.timestamp for item in items if item.timestamp), default=None),
            period_end=max((item.timestamp for item in items if item.timestamp), default=None),
        )

    def _collect_decision_topics(self, items: List[SourceItem]) -> Counter:
        """收集决策相关话题频次。"""
        topics: Counter[str] = Counter()
        for item in items:
            if "优先" in item.content or "优先级" in item.content:
                topics["priority"] += 1
            if "选择" in item.content:
                topics["choice"] += 1
            if "放弃" in item.content:
                topics["abandon"] += 1
            if "权衡" in item.content or "取舍" in item.content:
                topics["tradeoff"] += 1
        return topics

    def _build_topic_observation(
        self, topics: Counter, has_pasted: bool
    ) -> Observation:
        """根据决策话题构建 Observation。"""
        return self._make_obs(
            obs_type=ObservationType.PATTERN,
            value=dict(topics),
            unit="mentions",
            confidence=self._adjust_confidence(0.6, has_pasted),
            evidence=[f"{k}: {v} 次" for k, v in topics.most_common()],
        )


# ───────────────────────────────────────────────
# 3. Actions（行动模式）
# ───────────────────────────────────────────────


class ActionsExtractor(DimensionExtractor):
    """提取用户的行动模式"""

    dimension = Dimension.ACTIONS
    accepts_external_quoted = False

    COMPLETION_SIGNALS = [
        r"完成[了]?",
        r"搞定[了]?",
        r"结束[了]?",
        r"关闭[了]?",
        r"发布[了]?",
        r"上线[了]?",
        r"交付[了]?",
        r"解决[了]?",
    ]
    START_SIGNALS = [
        r"开始[了]?",
        r"启动[了]?",
        r"新建[了]?",
        r"创建[了]?",
        r"着手[了]?",
        r"立项[了]?",
    ]
    BLOCKED_SIGNALS = [
        r"卡住[了]?",
        r"阻塞[了]?",
        r"暂停[了]?",
        r"搁置[了]?",
        r"等待[中]?",
    ]

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        completed = 0
        started = 0
        blocked = 0
        all_text = "\n".join(item.content for item in items)

        for pattern in self.COMPLETION_SIGNALS:
            completed += len(re.findall(pattern, all_text))
        for pattern in self.START_SIGNALS:
            started += len(re.findall(pattern, all_text))
        for pattern in self.BLOCKED_SIGNALS:
            blocked += len(re.findall(pattern, all_text))

        total_actions = completed + started + blocked
        if total_actions > 0:
            yield self._make_obs(
                obs_type=ObservationType.RATIO,
                value={
                    "started": started,
                    "completed": completed,
                    "blocked": blocked,
                    "completion_rate": round(completed / total_actions, 2),
                },
                unit="actions",
                confidence=self._adjust_confidence(0.6 if total_actions >= 3 else 0.4, has_pasted),
                evidence=[
                    f"启动 {started} 次",
                    f"完成 {completed} 次",
                    f"阻塞 {blocked} 次",
                ],
                period_start=min(
                    (item.timestamp for item in items if item.timestamp), default=None
                ),
                period_end=max((item.timestamp for item in items if item.timestamp), default=None),
            )


# ───────────────────────────────────────────────
# 4. Time（时间模式）
# ───────────────────────────────────────────────


class TimeExtractor(DimensionExtractor):
    """提取用户的时间模式"""

    dimension = Dimension.TIME
    accepts_external_quoted = False

    ESTIMATE_SIGNALS = [
        r"预计[：:：]?",
        r"计划[：:：]?",
        r"目标[：:：]?",
        r"预期[：:：]?",
        r"大约[：:：]?",
        r"大概[：:：]?",
        r"应该[能|可以]",
        r"需要[：:：]?",
    ]
    DELAY_SIGNALS = [
        r"延期[了]?",
        r"推迟[了]?",
        r"延后[了]?",
        r"来不及[了]?",
        r"超时[了]?",
        r"花了[更]?多",
        r"比预期[：:：]?",
    ]
    # 只匹配明确的时间单位，排除哈希/UUID 误匹配
    TIME_UNITS = re.compile(
        r"(?<![a-fA-F0-9])"  # 前面不是十六进制字符（排除哈希）
        r"(\d+)\s*"  # 数字
        r"(天|小时|分钟|周|个月|年|"  # 中文单位
        r"hours?|hrs?|mins?|days?|weeks?|months?|years?)"  # 英文单位（完整单词）
        r"(?![a-zA-Z0-9])",  # 后面不是字母数字
        re.IGNORECASE,
    )

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        all_text = "\n".join(item.content for item in items)

        estimates = len(re.findall("|".join(self.ESTIMATE_SIGNALS), all_text))
        delays = len(re.findall("|".join(self.DELAY_SIGNALS), all_text))

        if estimates > 0 or delays > 0:
            yield self._make_obs(
                obs_type=ObservationType.FREQUENCY,
                value={
                    "estimates": estimates,
                    "delays": delays,
                    "delay_ratio": round(delays / max(estimates, 1), 2),
                },
                unit="mentions",
                confidence=self._adjust_confidence(0.5, has_pasted),
                evidence=[
                    f"时间估算提及 {estimates} 次",
                    f"延期/偏差提及 {delays} 次",
                ],
                period_start=min(
                    (item.timestamp for item in items if item.timestamp), default=None
                ),
                period_end=max((item.timestamp for item in items if item.timestamp), default=None),
            )

        # 提取时间数值（如 "3天", "2小时"）
        time_mentions = self.TIME_UNITS.findall(all_text)
        if time_mentions:
            yield self._make_obs(
                obs_type=ObservationType.FREQUENCY,
                value={"time_mentions": len(time_mentions), "examples": time_mentions[:5]},
                unit="mentions",
                confidence=self._adjust_confidence(0.5, has_pasted),
                evidence=[f"{n}{u}" for n, u in time_mentions[:5]],
            )


# ───────────────────────────────────────────────
# 5. Stress（压力信号）
# ───────────────────────────────────────────────


class StressExtractor(DimensionExtractor):
    """提取压力相关的信号"""

    dimension = Dimension.STRESS
    accepts_external_quoted = False

    STRESS_SIGNALS = [
        r"紧急",
        r"赶[不上]?",
        r"来不及",
        r"焦虑",
        r"压力[大]?",
        r" overwhelm",
        r"崩溃[了]?",
        r"烦[死]?[了]?",
        r"累[死]?[了]?",
        r"加班",
        r"熬夜",
        r"通宵",
        r" deadline",
        r"死线",
        r"忙[死]?[了]?",
    ]

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        stress_count, stress_snippets = self._collect_stress_signals(items)
        if stress_count > 0:
            affected = self._count_affected_sessions(items)
            yield self._build_stress_observation(
                stress_count, affected, stress_snippets, items, has_pasted
            )

    def _collect_stress_signals(self, items: List[SourceItem]) -> Tuple[int, List[str]]:
        """收集压力信号次数和去重后的典型情境片段。"""
        return self._extract_signal_snippets(
            items, self.STRESS_SIGNALS, snippet_max_chars=80, snippet_max_length=SNIPPET
        )

    def _count_affected_sessions(self, items: List[SourceItem]) -> int:
        """统计涉及压力信号的会话数。"""
        return len(
            set(
                s.session_id
                for s in items
                if s.session_id
                and any(re.search(sig, s.content, re.IGNORECASE) for sig in self.STRESS_SIGNALS)
            )
        )

    def _build_stress_observation(
        self,
        stress_count: int,
        affected: int,
        snippets: List[str],
        items: List[SourceItem],
        has_pasted: bool,
    ) -> Observation:
        """根据压力信号构建 Observation。"""
        evidence = [f"压力信号 {stress_count} 次（涉及 {affected} 个会话）"]
        if snippets:
            evidence.append(f"典型情境: {snippets[0]}")
        return self._make_obs(
            obs_type=ObservationType.FREQUENCY,
            value={
                "stress_signals": stress_count,
                "affected_sessions": affected,
            },
            unit="signals",
            confidence=self._adjust_confidence(min(0.8, 0.4 + stress_count * 0.05), has_pasted),
            evidence=evidence,
            period_start=min((item.timestamp for item in items if item.timestamp), default=None),
            period_end=max((item.timestamp for item in items if item.timestamp), default=None),
        )


# ───────────────────────────────────────────────
# 6. Relationships（关系模式）
# ───────────────────────────────────────────────


class RelationshipsExtractor(DimensionExtractor):
    """提取与人的互动模式"""

    dimension = Dimension.RELATIONSHIPS
    accepts_external_quoted = False

    # 人物提及模式（中文名、英文名、@提及）
    PERSON_PATTERNS = [
        r"@[a-zA-Z0-9_一-鿿]+",  # @username
        r"[一-鿿]{2,3}[哥|姐|总|老师|同学|同事]",  # 小名+称谓
    ]

    INTERACTION_SIGNALS = [
        (r"讨论[了]?", "讨论"),
        (r"沟通[了]?", "沟通"),
        (r"反馈[了]?", "反馈"),
        (r"协调[了]?", "协调"),
        (r"合作[了]?", "合作"),
        (r"冲突[了]?", "冲突"),
        (r"共识[了]?", "共识"),
        (r"对齐[了]?", "对齐"),
        (r"确认[了]?", "确认"),
        (r"催促[了]?", "催促"),
    ]

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        all_text = "\n".join(item.content for item in items)

        period_start = min((item.timestamp for item in items if item.timestamp), default=None)
        period_end = max((item.timestamp for item in items if item.timestamp), default=None)

        # 互动信号统计
        interactions = Counter()  # type: ignore[var-annotated]
        for pattern, label in self.INTERACTION_SIGNALS:
            count = len(re.findall(pattern, all_text))
            if count > 0:
                interactions[label] = count

        if interactions:
            yield self._make_obs(
                obs_type=ObservationType.FREQUENCY,
                value=dict(interactions.most_common()),
                unit="mentions",
                confidence=self._adjust_confidence(0.5, has_pasted),
                evidence=[f"{k}: {v} 次" for k, v in interactions.most_common()],
                period_start=period_start,
                period_end=period_end,
            )

        # 人物/关系提及
        person_mentions = sum(
            len(re.findall(pattern, all_text)) for pattern in self.PERSON_PATTERNS
        )
        if person_mentions:
            yield self._make_obs(
                obs_type=ObservationType.PATTERN,
                value={"person_mentions": person_mentions},
                unit="mentions",
                confidence=self._adjust_confidence(0.5, has_pasted),
                evidence=[f"人物/关系提及 {person_mentions} 次"],
                period_start=period_start,
                period_end=period_end,
            )

        # 不同 Agent 的使用分布（从 raw 文件）
        agent_counts = Counter()  # type: ignore[var-annotated]
        for item in items:
            if isinstance(item.source_agent, str) and item.source_agent:
                agent_counts[item.source_agent] += 1

        if len(agent_counts) > 1:
            yield self._make_obs(
                obs_type=ObservationType.RATIO,
                value=dict(agent_counts),
                unit="sessions",
                confidence=0.8,
                evidence=[f"{k}: {v} 次" for k, v in agent_counts.most_common()],
            )


# ───────────────────────────────────────────────
# 7. Growth（成长轨迹）
# ───────────────────────────────────────────────


class GrowthExtractor(DimensionExtractor):
    """提取长期成长信号"""

    dimension = Dimension.GROWTH
    accepts_external_quoted = False

    GROWTH_SIGNALS = [
        r"学会[了]?",
        r"掌握[了]?",
        r"理解[了]?",
        r"领悟[了]?",
        r"突破[了]?",
        r"提升[了]?",
        r"改进[了]?",
        r"优化[了]?",
        r"新技能",
        r"新知识",
        r"新方法",
        r"新工具",
        r"第一次",
        r"以前不会",
        r"终于会[了]?",
        r"从[^。]+到[^。]+",  # "从...到..." 变化模式
    ]

    ROLE_SIGNALS = [
        r"管理者",
        r"领导者",
        r"架构师",
        r"负责人",
        r"创始人",
        r"合伙人",
        r"导师",
        r"专家",
    ]

    def extract(self, items: List[SourceItem]) -> Iterator[Observation]:
        if not items:
            return

        items, has_pasted = self._filter_items(items)
        if not items:
            return

        all_text = "\n".join(item.content for item in items)

        growth_count, growth_snippets = self._collect_growth_signals(items)
        if growth_count > 0:
            yield self._build_growth_observation(
                growth_count, growth_snippets, items, has_pasted
            )

        roles = self._collect_role_mentions(all_text)
        if roles:
            yield self._build_roles_observation(roles, has_pasted)

    def _collect_growth_signals(self, items: List[SourceItem]) -> Tuple[int, List[str]]:
        """收集成长信号次数和去重后的典型情境片段。"""
        all_text = "\n".join(item.content for item in items)
        clean_all_text = self._clean_evidence(all_text, max_length=CLEAN_ALL_TEXT, strict=False)
        if not clean_all_text:
            return 0, []
        return self._extract_signal_snippets(
            [SourceItem(source_type="wiki", file_path="", content=clean_all_text)],
            self.GROWTH_SIGNALS,
            snippet_max_chars=80,
            snippet_max_length=100,
            clean_max_length=CLEAN_ALL_TEXT,
        )

    def _collect_role_mentions(self, all_text: str) -> Counter:
        """统计角色/身份提及频次。"""
        roles: Counter[str] = Counter()
        for role_pattern in self.ROLE_SIGNALS:
            count = len(re.findall(role_pattern, all_text))
            if count > 0:
                roles[role_pattern] = count
        return roles

    def _build_growth_observation(
        self,
        growth_count: int,
        growth_snippets: List[str],
        items: List[SourceItem],
        has_pasted: bool,
    ) -> Observation:
        """根据成长信号构建 Observation。"""
        evidence = [f"成长信号 {growth_count} 次"]
        if growth_snippets:
            evidence.append(f"典型情境: {growth_snippets[0]}")
        return self._make_obs(
            obs_type=ObservationType.FREQUENCY,
            value={
                "growth_signals": growth_count,
                "unique_snippets": len(growth_snippets),
            },
            unit="signals",
            confidence=self._adjust_confidence(min(0.7, 0.4 + growth_count * 0.05), has_pasted),
            evidence=evidence,
            period_start=min((item.timestamp for item in items if item.timestamp), default=None),
            period_end=max((item.timestamp for item in items if item.timestamp), default=None),
        )

    def _build_roles_observation(self, roles: Counter, has_pasted: bool) -> Observation:
        """根据角色/身份提及构建 Observation。"""
        return self._make_obs(
            obs_type=ObservationType.PATTERN,
            value=dict(roles),
            unit="mentions",
            confidence=self._adjust_confidence(0.5, has_pasted),
            evidence=[f"{k}: {v} 次" for k, v in roles.most_common()],
        )


# ───────────────────────────────────────────────
# 提取器注册表
# ───────────────────────────────────────────────

ALL_EXTRACTORS: List[DimensionExtractor] = [
    AttentionExtractor(),
    DecisionsExtractor(),
    ActionsExtractor(),
    TimeExtractor(),
    StressExtractor(),
    RelationshipsExtractor(),
    GrowthExtractor(),
]
