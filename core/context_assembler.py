"""
ContextAssembler — 上下文智能组装器

【E14 全库修复】从 Wiki Vault 检索相关上下文，组装到 LLM prompt 中。
支持实体匹配 + Jaccard 关键词重叠 + Token 预算截断。
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Set
import logging

logger = logging.getLogger(__name__)


class ContextAssembler:
    """上下文智能组装器"""

    def __init__(self, wiki_dir: Path = None, max_context_chars: int = 8000):
        self.wiki_dir = wiki_dir or Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        self.max_context_chars = max_context_chars

    def assemble(self, query_text: str, top_k: int = 5) -> str:
        """
        根据查询文本组装相关上下文

        Args:
            query_text: 查询/任务文本
            top_k: 最多返回多少个相关页面

        Returns:
            组装后的上下文文本
        """
        if not self.wiki_dir.exists():
            return ""

        # 1. 提取查询中的关键词和实体
        query_keywords = self._extract_keywords(query_text)

        # 2. 检索相关页面
        candidates = self._retrieve_candidates(query_keywords, top_k * 3)

        # 3. 按相关性排序
        scored = self._score_relevance(candidates, query_keywords)
        scored.sort(key=lambda x: x["score"], reverse=True)

        # 4. 选取 top_k 并在 Token 预算内组装
        selected = self._select_within_budget(scored[:top_k])

        # 5. 格式化输出
        return self._format_context(selected)

    def _extract_keywords(self, text: str) -> Set[str]:
        """提取文本中的关键词"""
        # 中文词（2字以上）+ 英文词
        zh_words = set(re.findall(r'[\u4e00-\u9fa5]{2,}', text))
        en_words = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', text))
        return zh_words | en_words

    def _extract_entities(self, text: str) -> Set[str]:
        """提取技术实体（大写缩写、CamelCase）"""
        entities = set()
        # 大写缩写
        for m in re.finditer(r'\b[A-Z]{2,10}\b', text):
            entities.add(m.group(0))
        # CamelCase
        for m in re.finditer(r'\b[A-Z][a-z]+[A-Z]\w+\b', text):
            entities.add(m.group(0))
        return entities

    def _retrieve_candidates(self, query_keywords: Set[str],
                             limit: int) -> List[Dict]:
        """从 Wiki Vault 检索候选页面"""
        candidates = []

        if not self.wiki_dir.exists():
            return candidates

        for md_file in self.wiki_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                # 跳过 frontmatter
                body = content
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        body = parts[2]

                # 提取页面关键词
                page_keywords = self._extract_keywords(body)
                page_entities = self._extract_entities(body)

                candidates.append({
                    "path": str(md_file.relative_to(self.wiki_dir)),
                    "title": md_file.stem,
                    "content": body[:2000],  # 预截断
                    "keywords": page_keywords,
                    "entities": page_entities,
                })
            except Exception:
                continue

            if len(candidates) >= limit:
                break

        return candidates

    def _score_relevance(self, candidates: List[Dict],
                         query_keywords: Set[str]) -> List[Dict]:
        """计算候选页面与查询的相关性分数"""
        query_entities = set()
        for kw in query_keywords:
            if kw.isupper() or re.match(r'^[A-Z][a-z]+[A-Z]', kw):
                query_entities.add(kw)

        for c in candidates:
            scores = []

            # 1. Jaccard 关键词重叠
            intersection = len(query_keywords & c["keywords"])
            union = len(query_keywords | c["keywords"])
            jaccard = intersection / union if union > 0 else 0
            scores.append(("jaccard", jaccard * 0.4))

            # 2. 实体精确匹配（权重更高）
            entity_match = len(query_entities & c["entities"])
            scores.append(("entity", min(1.0, entity_match * 0.3)))

            # 3. 标题匹配
            title_match = 0.0
            for kw in query_keywords:
                if kw.lower() in c["title"].lower():
                    title_match += 0.2
            scores.append(("title", min(1.0, title_match)))

            c["score"] = round(sum(s[1] for s in scores), 3)
            c["score_breakdown"] = {k: round(v, 3) for k, v in scores}

        return candidates

    def _select_within_budget(self, scored: List[Dict]) -> List[Dict]:
        """在 Token/字符预算内选取页面"""
        selected = []
        total_chars = 0

        for c in scored:
            content_len = len(c["content"])
            if total_chars + content_len > self.max_context_chars:
                # 尝试截取部分
                remaining = self.max_context_chars - total_chars
                if remaining > 200:
                    c["content"] = c["content"][:remaining] + "\n\n[...内容截断...]"
                    selected.append(c)
                    total_chars += remaining + 30
                break
            selected.append(c)
            total_chars += content_len

        return selected

    def _format_context(self, selected: List[Dict]) -> str:
        """格式化上下文文本"""
        if not selected:
            return ""

        lines = ["## 相关上下文", ""]
        for c in selected:
            lines.append(f"### {c['title']}")
            lines.append(f"> 来源: {c['path']} | 相关度: {c['score']}")
            lines.append("")
            lines.append(c["content"][:1500])
            lines.append("")

        return "\n".join(lines)

    def assemble_for_distill(self, session_text: str,
                             existing_fragments: List[Dict] = None) -> str:
        """
        为蒸馏任务组装上下文（专用接口）

        不仅搜索 Vault，还考虑已有片段的关联上下文。
        """
        base = self.assemble(session_text, top_k=3)

        if existing_fragments:
            # 从已有片段中提取额外关键词
            extra_keywords = set()
            for frag in existing_fragments:
                for field in ["title", "background", "core_content"]:
                    text = frag.get(field, "")
                    extra_keywords |= self._extract_keywords(text)

            if extra_keywords:
                extra = self.assemble(" ".join(extra_keywords), top_k=2)
                if extra and extra != base:
                    base += "\n\n## 关联上下文（来自已有片段）\n\n" + extra

        return base
