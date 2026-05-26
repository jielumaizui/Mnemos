"""
QuestionAnswerSearch — 问答式检索引擎

【E14 全库修复】A11 问答式检索缺失子模块
支持自然语言问答形式的知识检索。
"""
from typing import List, Dict, Optional


class QuestionAnswerSearch:
    """问答式检索：将用户问题转换为结构化查询并返回答案片段"""

    def __init__(self, retriever=None):
        self.retriever = retriever

    def search(self, question: str, top_k: int = 5) -> List[Dict]:
        """
        问答式检索

        Args:
            question: 自然语言问题
            top_k: 返回结果数

        Returns:
            [{"answer_snippet": str, "source": str, "confidence": float}]
        """
        # TODO: 实现问题解析 → 检索 → 答案抽取流水线
        return []

    def extract_question_type(self, question: str) -> str:
        """提取问题类型：what/why/how/who/when/compare"""
        q = question.lower().strip()
        if q.startswith(("what", "什么是", "啥是")):
            return "definition"
        elif q.startswith(("why", "为什么", "为啥")):
            return "causation"
        elif q.startswith(("how", "怎么", "如何")):
            return "procedure"
        elif q.startswith(("who", "谁")):
            return "entity"
        elif q.startswith(("when", "什么时候", "何时")):
            return "temporal"
        elif any(w in q for w in ["vs", "versus", "对比", "区别", "比较"]):
            return "comparison"
        return "general"
