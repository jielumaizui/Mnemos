"""
QuestionAnswerSearch — 问答式检索引擎

【E14 全库修复】A11 问答式检索完整实现。
支持自然语言问答形式的知识检索，接入 Vault 检索 + 答案片段抽取。
"""

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

    @staticmethod
    def _results_to_context(results: List[SearchResult]) -> str:
        lines = []
        for result in results:
            lines.append(f"### {result.title}")
            lines.append(f"> 来源: {result.page_path}")
            if result.freshness_alert:
                lines.append(f"> 新鲜度提醒: {result.freshness_alert.message}")
            lines.append(result.snippet)
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
        """将上下文分割为段落"""
        paragraphs = []
        current_source = ""

        for line in context.split("\n"):
            line = line.strip()
            if not line:
                continue

            # 检测来源标记
            if line.startswith("> 来源:") or line.startswith("### "):
                current_source = line.replace("> 来源:", "").replace("### ", "").strip()
                continue

            if len(line) > 8:
                paragraphs.append({
                    "text": line,
                    "source": current_source,
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
        p_keywords = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', text))

        if not q_keywords:
            return 0.0

        # 基础匹配分：Jaccard
        intersection = q_keywords & p_keywords
        union = q_keywords | p_keywords
        base_score = len(intersection) / len(union) if union else 0

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
            if any(p in text for p in ["区别", "差异", "优势", "劣势", "对比", "vs", "difference", "compared", "advantage", "disadvantage"]):
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

        # 长度惩罚：过长或过短的段落降低分数
        length = len(para["text"])
        length_factor = 1.0
        if length < 30:
            length_factor = 0.7
        elif length > 300:
            length_factor = 0.9

        return min(1.0, (base_score * 0.5 + type_bonus) * length_factor)

    def answer(self, question: str) -> Optional[Dict]:
        """
        直接返回答案（取最高置信度结果）

        Returns:
            {"answer": str, "source": str, "confidence": float} 或 None
        """
        results = self.search(question, top_k=3)
        if not results:
            return None

        best = max(results, key=lambda x: x["confidence"])
        return {
            "answer": best["answer_snippet"],
            "source": best["source"],
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
