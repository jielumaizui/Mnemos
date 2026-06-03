"""
QuestionAnswerSearch — 问答式检索引擎

【E14 全库修复】A11 问答式检索完整实现。
支持自然语言问答形式的知识检索，接入 Vault 检索 + 答案片段抽取。
"""

import math
import re
from typing import List, Dict, Optional
from pathlib import Path

from core.app.context_search import ContextAwareSearch, SearchResult


class QuestionAnswerSearch:
    """问答式检索：将用户问题转换为结构化查询并返回答案片段"""

    def __init__(self, wiki_dir: Path = None, retriever=None):
        self.wiki_dir = wiki_dir or Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        self.retriever = retriever or ContextAwareSearch(wiki_base=str(self.wiki_dir))

    def search(self, question: str, top_k: int = 5) -> List[Dict]:
        """
        问答式检索

        Args:
            question: 自然语言问题
            top_k: 返回结果数

        Returns:
            [{"answer_snippet": str, "source": str, "confidence": float, "question_type": str}]
        """
        qtype = self.extract_question_type(question)

        # 1. 复用画像感知搜索召回相关页面
        results = self._search_results(question, top_k=top_k * 2)

        if not results:
            return []

        context = self._results_to_context(results)
        snippets = self._extract_answer_snippets(context, question, qtype)

        # Fallback：如果搜索结果明显相关但 snippet 评分不够，至少返回 top result 摘要
        if not snippets and results:
            top = results[0]
            # 检查标题是否包含 query 核心 token（强相关信号）
            title_lower = (top.title or "").lower()
            q_core = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', self._normalize_question(question)))
            if q_core and any(kw in title_lower for kw in q_core):
                fallback_text = top.snippet or top.title or ""
                if len(fallback_text) > 500:
                    fallback_text = fallback_text[:500] + "..."
                snippets.append({
                    "text": fallback_text,
                    "source": str(top.page_path),
                    "score": 0.55,
                })

        # 4. 排序和格式化
        results = []
        for snippet in snippets[:top_k]:
            results.append({
                "answer_snippet": snippet["text"],
                "source": snippet.get("source", "unknown"),
                "confidence": round(snippet["score"], 3),
                "question_type": qtype,
            })

        return results

    def _search_results(self, question: str, top_k: int) -> List[SearchResult]:
        if hasattr(self.retriever, "search"):
            return self.retriever.search(question, limit=top_k)
        context = self.retriever.assemble(question, top_k=top_k)
        return [
            SearchResult(
                page_path="unknown",
                title="unknown",
                snippet=context,
                score=0.5,
            )
        ] if context else []

    def _results_to_context(self, results: List[SearchResult]) -> str:
        """将搜索结果转换为 QA 上下文，回读页面正文（过滤 frontmatter）"""
        lines = []
        for result in results:
            lines.append(f"### {result.title}")
            lines.append(f"> 来源: {result.page_path}")
            if result.freshness_alert:
                lines.append(f"> 新鲜度提醒: {result.freshness_alert.message}")
            # 优先使用 snippet，但如果 snippet 过短或像是 frontmatter，尝试回读正文
            snippet = result.snippet or ""
            if len(snippet) < 100 or snippet.startswith("---"):
                try:
                    page_path = self.wiki_dir / result.page_path
                    if page_path.exists():
                        full = page_path.read_text(encoding="utf-8", errors="ignore")
                        # 去掉 frontmatter
                        if full.startswith("---"):
                            parts = full.split("---", 2)
                            if len(parts) >= 3:
                                full = parts[2]
                        # 去掉 ## 来源追踪 等尾部区域
                        for marker in ["## 来源追踪", "## AI 关联扩充"]:
                            idx = full.find(marker)
                            if idx > 0:
                                full = full[:idx]
                        snippet = full.strip()
                except Exception:
                    pass
            lines.append(snippet)
            lines.append("")
        return "\n".join(lines)

    def extract_question_type(self, question: str) -> str:
        """提取问题类型：what/why/how/who/when/compare"""
        q = question.lower().strip()

        if q.startswith(("what", "什么是", "啥是", "什么是", " definition", "define")):
            return "definition"
        elif q.startswith(("why", "为什么", "为啥", "为何", "reason")):
            return "causation"
        elif q.startswith(("how", "怎么", "如何", "怎样", "步骤", "procedure", "steps")):
            return "procedure"
        elif q.startswith(("who", "谁", "which person", "作者", "负责人")):
            return "entity"
        elif q.startswith(("when", "什么时候", "何时", "时间", "date", "time")):
            return "temporal"
        elif any(w in q for w in ["vs", "versus", "对比", "区别", "比较", "difference", "compare"]):
            return "comparison"
        elif q.startswith(("where", "哪里", "位置", "location")):
            return "location"
        elif q.startswith(("which", "哪个", "哪一种")):
            return "selection"
        return "general"

    def _extract_answer_snippets(self, context: str, question: str,
                                 qtype: str) -> List[Dict]:
        """从上下文中抽取答案片段"""
        # 分割为段落
        paragraphs = self._split_into_paragraphs(context)

        scored_snippets = []
        for para in paragraphs:
            score = self._score_paragraph(para, question, qtype)
            if score > 0.2:
                scored_snippets.append({
                    "text": para["text"][:500],
                    "source": para.get("source", ""),
                    "score": score,
                })

        scored_snippets.sort(key=lambda x: x["score"], reverse=True)
        return scored_snippets

    def _split_into_paragraphs(self, context: str) -> List[Dict]:
        """将上下文分割为段落，source 固定为页面路径，heading 单独记录"""
        paragraphs = []
        current_source = ""
        current_heading = ""

        for line in context.split("\n"):
            line = line.strip()
            if not line:
                continue

            # 检测来源标记（页面路径）
            if line.startswith("> 来源:"):
                current_source = line.replace("> 来源:", "").strip()
                continue

            # 检测标题标记（章节标题，不是来源）
            if line.startswith("### "):
                current_heading = line.replace("### ", "").strip()
                # 标题本身也作为一个短段落，但后面会加惩罚
                continue

            if len(line) > 8:
                paragraphs.append({
                    "text": line,
                    "source": current_source,
                    "heading": current_heading,
                })

        return paragraphs

    # 中文问题停用词：这些词不应参与关键词匹配，避免稀释关键实体
    _QUESTION_STOP_WORDS = {
        "如何", "怎么", "怎样", "什么", "为什么", "为啥", "为何",
        "哪里", "哪个", "哪些", "谁", "多少", "几", "是否", "能不能",
        "可以", "应该", "需要", "必须", "一定", "可能", "也许",
        "解决", "处理", "应对", "处理", "办", "做", "用", "使用",
        "关于", "对于", "有关", "涉及", "针对", "按照", "根据",
        "时候", "时间", "时候", "情况", "场景", "问题", "冲突",
        "错误", "异常", "故障", "报错", "失败", "无法", "不能",
        "没有", "缺失", "缺少", "丢失", "遗漏", "忽略", "忘记",
        "知道", "了解", "明白", "理解", "清楚", "熟悉", "掌握",
        "建议", "推荐", "提示", "注意", "小心", "谨慎", "避免",
        "最好", "最优", "最佳", "合适", "适合", "适用", "恰当",
        "一般", "通常", "普遍", "常见", "经常", "往往", "大多",
        "请问", "请教", "咨询", "求助", "帮助", "帮忙", "协助",
        "一下", "一些", "一点", "一次", "一种", "一个", "一条",
        "给我", "给我", "帮我", "给我", "让我", "让我", "帮我",
        "详细", "具体", "详细", "深入", "全面", "完整", "系统",
        "简单", "容易", "方便", "快捷", "快速", "高效", "有效",
        "正确", "准确", "精确", "标准", "规范", "统一", "一致",
        "不同", "差异", "区别", "对比", "比较", "相比", "相对",
        "之前", "之后", "以前", "以后", "当时", "现在", "目前",
        "首先", "然后", "接着", "最后", "最终", "总之", "综上所述",
        "另外", "此外", "而且", "并且", "同时", "同样", "也一样",
        "虽然", "尽管", "但是", "可是", "然而", "不过", "只是",
        "因为", "由于", "因此", "所以", "因而", "从而", "于是",
        "如果", "假如", "假设", "要是", "若是", "倘若", "除非",
        "即使", "即便", "哪怕", "尽管", "不管", "无论", "不论",
        "不仅", "不只", "不但", "不光", "不单", "而且", "并且",
        "要么", "或者", "还是", "要么", "否则", "不然", "要不",
        "与其", "不如", "宁可", "宁愿", "宁肯", "情愿", "甘愿",
    }

    def _normalize_question(self, question: str) -> str:
        """中文问题归一化：过滤停用词，保留关键实体"""
        q = question.lower().strip()
        # 移除常见疑问前缀和停用词
        for w in sorted(self._QUESTION_STOP_WORDS, key=len, reverse=True):
            q = q.replace(w, " ")
        return q.strip()

    def _score_paragraph(self, para: Dict, question: str, qtype: str) -> float:
        """根据问题类型对段落评分"""
        text = para["text"].lower()
        question_normalized = self._normalize_question(question)
        q_keywords = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', question_normalized))

        if not q_keywords:
            return 0.0

        # 基础匹配分：BM25-like（query token 命中次数 / 段落长度归一化）
        # 比 Jaccard 更适合长段落
        total_hits = 0
        for kw in q_keywords:
            total_hits += text.count(kw)
        # 平均每个 query token 在段落中的命中次数，除以 log(段落长度) 做长度归一化
        avg_len = max(1, len(text))
        base_score = min(1.0, (total_hits / len(q_keywords)) / (math.log(avg_len + 10) / 3))

        # exact match 保护：关键 token 直接命中给予强 boost
        exact_bonus = 0.0
        core_terms = [kw for kw in q_keywords if len(kw) >= 3]
        for kw in core_terms:
            if kw in text:
                exact_bonus += 0.2
        exact_bonus = min(0.6, exact_bonus)

        # 位置加分：段落中包含 query 核心词的句子更靠前时加分
        position_bonus = 0.0
        sentences = re.split(r'[。！？\n]', text)
        for idx, sent in enumerate(sentences[:3]):
            if any(kw in sent for kw in core_terms):
                position_bonus += 0.05 * (3 - idx)
        position_bonus = min(0.15, position_bonus)

        # 问题类型加分
        type_bonus = 0.0

        if qtype == "definition":
            # 定义类问题：寻找 "是"、"指的是"、"定义为" 等句式
            if any(p in text for p in ["是", "指的是", "定义为", "meaning", "refers to", "is a"]):
                type_bonus = 0.3
            # 优先选择包含问题核心概念的句子
            core = next(iter(q_keywords), "")
            if core and core in text:
                type_bonus += 0.2

        elif qtype == "causation":
            # 因果类问题：寻找 "因为"、"由于"、"导致" 等
            if any(p in text for p in ["因为", "由于", "导致", "原因", "because", "since", "due to", "causes"]):
                type_bonus = 0.3

        elif qtype == "procedure":
            # 步骤类问题：寻找列表、数字序号、"首先"、"然后"
            if any(p in text for p in ["步骤", "首先", "然后", "最后", "1.", "2.", "step", "first", "then"]):
                type_bonus = 0.3
            if re.search(r'^\s*[-*\d]\s+', para["text"]):
                type_bonus += 0.2

        elif qtype == "comparison":
            # 对比类问题：寻找 "vs"、"区别"、"优势"、"劣势"
            patterns = ["区别", "差异", "优势", "劣势", "对比", "vs",
                        "difference", "compared", "advantage", "disadvantage"]
            if any(p in text for p in patterns):
                type_bonus = 0.3

        elif qtype == "temporal":
            # 时间类问题：寻找日期、时间表达
            if re.search(r'\d{4}[年/-]\d{1,2}[月/-]?\d{0,2}', para["text"]):
                type_bonus = 0.3
            if any(p in text for p in ["时间", "日期", "when", "date", "period", "duration"]):
                type_bonus += 0.2

        elif qtype == "entity":
            # 实体类问题：优先包含人名或组织名
            if re.search(r'[A-Z][a-z]+\s+[A-Z][a-z]+', para["text"]):  # 英文人名
                type_bonus = 0.2

        # 标题段落惩罚：纯标题（如 "## 结论"、"### 方案一"）不应作为答案
        heading_penalty = 0.0
        text_stripped = para["text"].strip()
        if text_stripped.startswith(("#", "**", "---")):
            heading_penalty = 0.3
        # 如果段落很短且看起来像列表标题或短句，也适当降权
        if len(text_stripped) < 40 and not re.search(r'[。！？\.\!\?]', text_stripped):
            heading_penalty = max(heading_penalty, 0.15)

        # 命令/代码/步骤段落加权
        code_bonus = 0.0
        if re.search(r'`[^`]+`|\$\s+\w|python\s+|docker\s+|git\s+|pip\s+|curl\s+|unset\s+|export\s+', para["text"]):
            code_bonus = 0.15
        if qtype == "procedure" and re.search(r'^\s*[-*\d]\s+', para["text"]):
            code_bonus += 0.1

        # 长度惩罚：过长或过短的段落降低分数
        length = len(para["text"])
        length_factor = 1.0
        if length < 30:
            length_factor = 0.7
        elif length > 300:
            length_factor = 0.9

        raw_score = (base_score * 0.4 + exact_bonus + position_bonus + type_bonus + code_bonus - heading_penalty) * length_factor
        return max(0.0, min(1.0, raw_score))

    def _extract_solution_blocks(self, source: str, question: str, max_blocks: int = 4) -> List[str]:
        """从命中页面中优先抽取可执行方案、步骤和命令。"""
        if not source or source == "unknown":
            return []
        page_path = self.wiki_dir / source
        if not page_path.exists():
            return []
        try:
            text = page_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return []
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]

        wanted_headings = (
            "解决", "方案", "操作", "步骤", "命令", "修复", "处理",
            "直接", "结论", "怎么做", "如何做",
        )
        blocks: List[str] = []
        current_heading = ""
        current_lines: List[str] = []

        def flush():
            if not current_lines:
                return
            block = "\n".join(current_lines).strip()
            if not block:
                return
            if len(block) > 700:
                block = block[:700].rstrip() + "..."
            blocks.append(block)

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            heading_match = re.match(r"^#{2,4}\s+(.+)$", line)
            if heading_match:
                if current_heading and any(k in current_heading for k in wanted_headings):
                    flush()
                current_heading = heading_match.group(1)
                current_lines = []
                continue
            if current_heading and any(k in current_heading for k in wanted_headings):
                if line.strip():
                    current_lines.append(line)
        if current_heading and any(k in current_heading for k in wanted_headings):
            flush()

        command_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if re.search(r"`[^`]+`|^(unset|export|git|python|pip|docker|curl|mnemos)\b", stripped):
                command_lines.append(stripped)
        if command_lines:
            blocks.insert(0, "\n".join(command_lines[:6]))

        # 去重并保序
        deduped = []
        seen = set()
        for block in blocks:
            key = re.sub(r"\s+", " ", block)[:120]
            if key not in seen:
                seen.add(key)
                deduped.append(block)
        return deduped[:max_blocks]

    def answer(self, question: str) -> Optional[Dict]:
        """
        直接返回答案。
        对 procedure 类问题做轻量合成（合并 top 步骤段落），
        其他类型取最高置信度结果。

        Returns:
            {"answer": str, "source": str, "heading": str, "confidence": float} 或 None
        """
        results = self.search(question, top_k=5)
        if not results:
            return None

        qtype = self.extract_question_type(question)
        best = max(results, key=lambda x: x["confidence"])
        solution_blocks = self._extract_solution_blocks(best["source"], question)
        if solution_blocks and qtype in ("procedure", "general", "causation"):
            return {
                "answer": "\n\n".join(solution_blocks),
                "source": best["source"],
                "heading": "可执行方案",
                "confidence": round(min(0.95, best["confidence"] + 0.08), 3),
                "question_type": qtype,
            }

        if qtype == "procedure" and len(results) >= 2:
            # 轻量合成：合并 top 3 步骤段落，保留来源页面路径
            parts = []
            seen_sources = set()
            for r in results[:3]:
                snippet = r["answer_snippet"].strip()
                if snippet and snippet not in parts:
                    parts.append(snippet)
                    seen_sources.add(r["source"])
            if parts:
                combined = "\n".join(parts)
                # confidence 取加权平均，避免饱和到 1.0
                avg_conf = sum(r["confidence"] for r in results[:3]) / len(results[:3])
                return {
                    "answer": combined,
                    "source": ", ".join(seen_sources) if seen_sources else results[0]["source"],
                    "heading": "",
                    "confidence": round(avg_conf, 3),
                    "question_type": qtype,
                }

        return {
            "answer": best["answer_snippet"],
            "source": best["source"],
            "heading": "",
            "confidence": best["confidence"],
            "question_type": best["question_type"],
        }

    def answer_markdown(self, question: str, context: Dict = None) -> str:
        """返回适合直接展示给用户的结构化 Markdown 答案。"""
        results = self.search(question, top_k=5)
        if not results:
            return "根据当前 Wiki，没有找到足够相关的答案。"

        lines = ["根据你的知识库：", ""]
        for item in results[:3]:
            lines.append(f"**{item['source'] or '未命名来源'}**")
            lines.append(f"- {item['answer_snippet'][:240]}")
            lines.append(f"- 置信度：{item['confidence']:.2f}")
            lines.append("")
        return "\n".join(lines).strip()
