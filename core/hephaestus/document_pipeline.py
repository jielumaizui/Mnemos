# -*- coding: utf-8 -*-
"""
DocumentDistillationPipeline — 外部文档深度处理管道

将用户主动导入的文件（PDF/PPT/Excel/Book/Word/HTML）
蒸馏为结构化 wiki 知识页面。

设计原则：
- 对话走 DistillationEngine（L1-L7），文档走本管道
- 书籍蒸馏为通用方法论（不绑定工作场景）
- 数据类文档提取数据洞察
- 方案/报告类提取决策与策略
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.hephaestus.backend_bundle import backend_from_caller
from core.hephaestus.document_failure_evidence import build_document_failure_evidence, record_document_provider_failure  # noqa: E501
from core.hephaestus.distill_backend import DistillBackend
from core.hephaestus.distillation_engine import (
    DistillSelfCheck,
    generate_wiki_page,
    KnowledgeFragment,
    HttpApiHostAgentCaller,
    PROMPT_VERSION,
    DistillationAPIError,
    _save_failed_distill,
    _emit_knowledge_distilled,
    _auto_remediate_fragment,
    _validate_fragment,
    publish_wiki_page_updated,
)
from core.hephaestus.link_probe_worker import get_link_probe_worker
from core.config import get_config
from core.hephaestus.table_artifacts import (
    DocumentTableArtifactStore,
    attach_table_artifacts,
    preprocess_large_tables,
)
from core.hephaestus.document_judge import (
    DocumentJudgeResult,
    DocumentLLMJudge,
    _load_document_prompt,
)
from core.frontmatter import parse_frontmatter, fm_get
from core.hephaestus.trusted_push_bridge import submit_document_page_candidate
from core.vaults.page_routing import allocate_routed_title_path

DOCUMENT_PIPELINE_OPERATION_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)

PREVIEW = 3000
PROMPT = 10000
TARGET_SIZE = 50000
CHUNK_SIZE = 8000

logger = logging.getLogger(__name__)


@dataclass
class DocumentDistillResult:
    """文档蒸馏结果"""

    session_id: str = ""
    judgment: str = "skip"
    doc_category: str = ""
    fragments: List[KnowledgeFragment] = field(default_factory=list)
    book_meta: Optional[Dict] = None
    data_insights: Optional[Dict] = None
    strategy_items: Optional[Dict] = None
    report_items: Optional[Dict] = None
    self_check_issues: List[str] = field(default_factory=list)
    cross_agent_links: List[str] = field(default_factory=list)
    source_coverage: str = "full"
    distill_input_mode: str = "single"
    covered_turn_range: str = ""
    truncated: bool = False
    table_artifacts: List[Dict[str, Any]] = field(default_factory=list)
    raw_response: str = ""
    failure_parse_metadata: Dict[str, Any] = field(default_factory=dict)


class DocumentKnowledgeExtractor:
    """文档知识提取器 — 按文档类别使用不同策略提取结构化知识"""

    def __init__(
        self,
        caller: HttpApiHostAgentCaller | None = None,
        wiki_base: Path | None = None,
        backend: DistillBackend | None = None,
    ):
        if backend is None:
            backend = backend_from_caller(caller)
        self._backend = backend
        self._wiki_base = wiki_base
        self._embedding_index = None
        self._last_table_artifacts: List[Dict[str, Any]] = []
        self._failure_prompts: List[str] = []
        self._failure_responses: List[Any] = []
        self._table_artifact_store = DocumentTableArtifactStore(wiki_base=wiki_base)

    def _call_backend(self, prompt: str, **kwargs: Any) -> Any:
        response = self._backend.call(prompt, **kwargs)
        self._failure_prompts.append(prompt)
        self._failure_responses.append(response)
        return response

    def failure_evidence(self, content: str, source_event_refs: List[str]) -> tuple[str, Dict]:
        return build_document_failure_evidence(
            backend=self._backend,
            prompts=self._failure_prompts,
            responses=self._failure_responses,
            content=content,
            source_event_refs=source_event_refs,
        )

    @property
    def last_table_artifacts(self) -> List[Dict[str, Any]]:
        """Return table artifacts created during the latest preprocessing pass."""
        return list(self._last_table_artifacts)

    def _get_embedding_index(self):
        """懒加载 EmbeddingIndexManager"""
        if self._embedding_index is None and self._wiki_base is not None:
            try:
                from core.embeddings import EmbeddingIndexManager
                from core.config import get_config

                cfg = get_config()
                if cfg.get("embedding.enabled", True):
                    self._embedding_index = EmbeddingIndexManager(wiki_base=self._wiki_base)
            except DOCUMENT_PIPELINE_OPERATION_ERRORS as e:
                logger.warning(
                    "[DocExtractor] EmbeddingIndexManager 加载失败: %s", e, exc_info=True
                )
        return self._embedding_index

    def _fetch_related_pages(self, content: str, top_k: int = 3) -> str:
        """检索与文档内容最相似的已有 Wiki 页面，返回格式化的上下文字符串"""
        idx = self._get_embedding_index()
        if idx is None:
            return ""

        query = content[:800].strip()
        if len(query) < 50:
            return ""

        try:
            results = idx.search(query, top_k=top_k, similarity_threshold=0.5, use_rerank=False)
        except DOCUMENT_PIPELINE_OPERATION_ERRORS as e:
            logger.warning("[DocExtractor] 关联页面检索失败: %s", e)
            return ""

        if not results:
            return ""

        lines = ["## 已有相关知识页面（供你建立关联）"]
        for rel_path, score in results:
            page_path = self._wiki_base / rel_path
            if not page_path.exists():
                continue
            try:
                text = page_path.read_text(encoding="utf-8")
                # 提取 frontmatter
                fm, body = parse_frontmatter(text)
                if fm:
                    name = fm_get(fm, "name", Path(rel_path).stem)
                    summary = fm_get(fm, "summary", "")[:120]
                else:
                    name = Path(rel_path).stem
                    summary = (
                        text.split("\n# ", 1)[-1].split("\n", 1)[0][:120] if "# " in text else ""
                    )
                lines.append(f"- [[{name}]]: {summary}")
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                continue

        return "\n".join(lines)

    def _preprocess_large_tables(
        self,
        content: str,
        session_id: str = "",
        max_rows: int = 12,
        max_cols: int = 8,
    ) -> str:
        """预处理超大 Markdown 表格：prompt 采样，完整表格保存为可回放 artifact。"""
        processed, artifacts = preprocess_large_tables(
            content,
            artifact_store=self._table_artifact_store,
            session_id=session_id,
            max_rows=max_rows,
            max_cols=max_cols,
        )
        self._last_table_artifacts = artifacts
        return processed

    def extract(
        self, content: str, judge_result: DocumentJudgeResult, session_id: str = ""
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """按文档类别提取知识片段和结构化数据"""
        self._failure_prompts = []
        self._failure_responses = []
        content = self._preprocess_large_tables(content, session_id=session_id)
        category = judge_result.doc_category
        if category == "book":
            fragments, data = self._extract_book(content, judge_result)
            return self._finalize_extract(fragments, data, judge_result)
        handlers = {
            "data": self._extract_data,
            "strategy": self._extract_strategy,
            "report": self._extract_report,
        }
        handler = handlers.get(category, self._extract_generic)
        if len(content) > PROMPT:
            fragments, data = self._extract_full_document_chunks(content, judge_result, handler)
        else:
            fragments, data = handler(content, judge_result)
        return self._finalize_extract(fragments, data, judge_result)

    def _finalize_extract(
        self,
        fragments: List[KnowledgeFragment],
        data: Dict,
        judge_result: DocumentJudgeResult,
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        fragments = self._apply_judge_metadata(fragments, judge_result)
        data = self._attach_table_artifacts(fragments, data or {})
        return fragments, dict(data)

    def _attach_table_artifacts(
        self,
        fragments: List[KnowledgeFragment],
        data: Dict,
    ) -> Dict:
        return attach_table_artifacts(fragments, data, self.last_table_artifacts)

    @staticmethod
    def _apply_judge_metadata(
        fragments: List[KnowledgeFragment], judge: DocumentJudgeResult
    ) -> List[KnowledgeFragment]:
        """Attach document-level judge metadata to extracted fragments."""
        if not judge.audience:
            return fragments
        for frag in fragments:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("受众", judge.audience)
        return fragments

    def _extract_book(
        self, content: str, judge: DocumentJudgeResult
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取书籍中的核心概念 — 深度分析，每 chunk 生成 1-3 个完整概念页"""
        chunks = self._chunk_by_chapters(content)
        logger.info("[DocExtractor] 书籍共 %s 章，开始全量蒸馏（深度模式）...", len(chunks))

        all_fragments = []
        all_ai_expansions = []

        for i, chunk in enumerate(chunks):
            logger.info("[DocExtractor] 蒸馏第 %s/%s 章...", i + 1, len(chunks))
            # 不截断，给 LLM 完整 chunk（API 模式下上下文窗口足够）
            related = self._fetch_related_pages(chunk)
            prompt = (
                _load_document_prompt("book_methodology")
                .replace("{book_content}", chunk)
                .replace("{related_pages}", related)
            )
            response = self._call_backend(prompt, expect_json=True, max_retries=2)
            data = response.require_mapping()

            concepts = data.get("concepts")
            if not isinstance(concepts, list):
                raise ValueError("book_methodology output must contain concepts[]")

            for concept in concepts:
                if not isinstance(concept, dict):
                    raise ValueError("book_methodology concepts[] entries must be objects")
                frag = self._concept_to_fragment(concept, judge)
                if frag:
                    # 每个 concept 的 AI 扩充独立保留，不全局合并
                    frag.ai_expansion = concept.get("ai_expansion", "")  # noqa
                    all_fragments.append(frag)
                    if concept.get("ai_expansion"):
                        all_ai_expansions.append(concept["ai_expansion"])

        # 去重
        seen = set()
        unique_fragments = []
        for f in all_fragments:
            if f.title not in seen:
                seen.add(f.title)
                unique_fragments.append(f)

        logger.info("[DocExtractor] 书籍蒸馏完成：%s 个深度概念页", len(unique_fragments))
        book_meta = {
            "chapter_count": len(chunks),
            "concept_count": len(unique_fragments),
            "key_topics": list(judge.key_topics or []),
            "ai_expansion_count": len(all_ai_expansions),
        }
        if judge.audience:
            book_meta["audience"] = judge.audience
        return unique_fragments, {
            "book_meta": book_meta,
            "concepts": [f.title for f in unique_fragments],
            "ai_expansions": all_ai_expansions,
        }

    def _extract_full_document_chunks(
        self,
        content: str,
        judge: DocumentJudgeResult,
        handler,
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """对非 book 长文档做全文分块提取，避免只看前 10000 字符。"""
        chunks = self._chunk_by_chapters(content)
        if len(chunks) <= 1:
            return handler(content, judge)  # type: ignore[no-any-return]

        all_fragments: List[KnowledgeFragment] = []
        chunk_results: List[Dict] = []
        logger.info(
            "[DocExtractor] %s 长文档共 %s 个 chunk，开始全文蒸馏...",
            judge.doc_category or "generic",
            len(chunks),
        )

        for idx, chunk in enumerate(chunks):
            fragments, data = handler(chunk, judge)
            chunk_results.append(
                {
                    "chunk_index": idx,
                    "chunk_count": len(chunks),
                    "char_start_approx": sum(len(c) for c in chunks[:idx]),
                    "char_count": len(chunk),
                    "data": data,
                }
            )
            for frag in fragments:
                frag.frontmatter = dict(frag.frontmatter or {})
                frag.frontmatter.setdefault("source_chunk", f"{idx + 1}/{len(chunks)}")
                all_fragments.append(frag)

        unique_fragments = self._dedupe_fragments(all_fragments)
        return unique_fragments, {
            "chunks": chunk_results,
            "source_coverage": {
                "mode": "full_chunked",
                "chunk_count": len(chunks),
                "covered_chars": len(content),
            },
        }

    @staticmethod
    def _dedupe_fragments(fragments: List[KnowledgeFragment]) -> List[KnowledgeFragment]:
        """按标题和正文去重，保留不同 chunk 中真正不同的片段。"""
        seen = set()
        unique = []
        for frag in fragments:
            key = (frag.title or "", frag.core_content or "")
            if key in seen:
                continue
            seen.add(key)
            unique.append(frag)
        return unique

    def _concept_to_fragment(
        self, concept: Dict, judge: DocumentJudgeResult
    ) -> Optional[KnowledgeFragment]:
        """将 concept 转换为 KnowledgeFragment"""
        title = concept.get("title", "").strip()
        content = concept.get("content", "").strip()
        if not title or not content:
            return None

        concept_fm = concept.get("frontmatter")
        if not isinstance(concept_fm, dict):
            raise ValueError("book concept must contain frontmatter object")
        concept_boundaries = concept_fm.get("boundaries")
        concept_anti_patterns = concept_fm.get("anti_patterns")
        if not isinstance(concept_boundaries, dict):
            raise ValueError("book concept frontmatter.boundaries must be an object")
        if not isinstance(concept_anti_patterns, list):
            raise ValueError("book concept frontmatter.anti_patterns must be a list")
        concept_keywords = concept_fm.get("关键词", [])
        concept_triggers = concept_fm.get("触发器", [])
        concept_aliases = concept_fm.get("别名", [])

        return KnowledgeFragment(
            form=concept.get("form", "concept"),
            title=title,
            frontmatter={
                "类型": "concept",
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "影响力, 心理学",
                "摘要": f"{title} — {content[:80].replace(chr(10), ' ')}...",
                "关键词": concept_keywords,
                "触发器": concept_triggers,
                "别名": concept_aliases,
            },
            background="",
            core_content=content,
            boundaries=concept_boundaries,
            anti_patterns=concept_anti_patterns,
            related_concepts=[],
            relations=concept.get("relations", []),
            keywords=judge.key_topics,
        )

    def _extract_data(
        self, content: str, judge: DocumentJudgeResult
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取数据洞察"""
        prompt = _load_document_prompt("data_insight").replace("{data_content}", content)
        data = self._call_backend(prompt, expect_json=True).require_mapping()

        fragments = []
        ai_expansions = []

        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 数据画像
        profile = data.get("data_profile", {})

        # 提取文档级 frontmatter（新格式）
        doc_frontmatter = data.get("frontmatter", {})
        doc_boundaries = doc_frontmatter.get("boundaries", {})
        doc_anti_patterns = doc_frontmatter.get("anti_patterns", [])
        doc_keywords = doc_frontmatter.get("关键词", [])
        doc_triggers = doc_frontmatter.get("触发器", [])
        doc_aliases = doc_frontmatter.get("别名", [])

        # 洞察 → 知识片段
        for ins in data.get("insights", []):
            frag = KnowledgeFragment(
                form="data-insight",
                title=ins.get("observation", "数据洞察")[:60],
                frontmatter={
                    "领域": "数据分析",
                    "证据级别": ins.get("confidence", "中"),
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=f"数据来源：{profile.get('scope', '未知')}",
                core_content=f"**观察**：{ins.get('observation', '')}\n\n"
                f"**证据**：{ins.get('evidence', '')}\n\n"
                f"**含义**：{ins.get('implication', '')}",
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 异常 → 反模式片段
        for anom in data.get("anomalies", []):
            frag = KnowledgeFragment(
                form="反模式",
                title=f"异常：{anom.get('description', '')[:50]}",
                frontmatter={
                    "领域": "数据分析",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=anom.get("description", ""),
                core_content=f"**数据点**：{anom.get('data_point', '')}\n\n"
                f"**可能原因**：{anom.get('possible_cause', '')}",
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 建议 → 经验法则
        for rec in data.get("recommendations", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=f"建议：{rec[:50]}",
                frontmatter={
                    "领域": "数据分析",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background="基于数据分析的建议",
                core_content=rec,
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 关联上下文（ADR-019）
        doc_relations = data.get("relations", [])
        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai  # noqa
            frag.relations = doc_relations
        return fragments, dict(data)

    def _extract_strategy(
        self, content: str, judge: DocumentJudgeResult
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取策略/方案中的决策和方法论"""
        related = self._fetch_related_pages(content)
        prompt = (
            _load_document_prompt("strategy_extract")
            .replace("{strategy_content}", content)
            .replace("{related_pages}", related)
        )
        data = self._call_backend(prompt, expect_json=True).require_mapping()

        fragments = []
        ai_expansions = []

        obj = data.get("objective_extraction")
        if not isinstance(obj, dict):
            raise ValueError("strategy_extract output must contain objective_extraction object")
        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 提取文档级 frontmatter（新格式）
        doc_frontmatter = data.get("frontmatter", {})
        doc_boundaries = doc_frontmatter.get("boundaries", {})
        doc_anti_patterns = doc_frontmatter.get("anti_patterns", [])
        doc_keywords = doc_frontmatter.get("关键词", [])
        doc_triggers = doc_frontmatter.get("触发器", [])
        doc_aliases = doc_frontmatter.get("别名", [])

        # 决策 → 决策记录
        for dec in obj.get("key_decisions", []):
            frag = KnowledgeFragment(
                form="决策记录",
                title=dec.get("decision", "决策")[:60],
                frontmatter={
                    "领域": "策略规划",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=dec.get("rationale", ""),
                core_content=f"**决策**：{dec.get('decision', '')}\n\n"
                f"**理由**：{dec.get('rationale', '')}\n\n"
                f"**替代方案**：{dec.get('alternatives_considered', '')}\n\n"
                f"**风险**：{', '.join(dec.get('risks', []) or [])}",
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 方法论
        for meth in obj.get("methodologies", []):
            frag = KnowledgeFragment(
                form="方法论",
                title=meth.get("name", "方法论")[:60],
                frontmatter={
                    "领域": "策略规划",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=meth.get("how_applied", ""),
                core_content=meth.get("how_applied", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 经验教训
        for lesson in obj.get("lessons_learned", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=lesson[:60],
                frontmatter={
                    "领域": "策略规划",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background="",
                core_content=lesson,
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 关联上下文（ADR-019）
        doc_relations = data.get("relations", [])
        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai  # noqa
            frag.relations = doc_relations
        return fragments, dict(data)

    def _extract_report(
        self, content: str, judge: DocumentJudgeResult
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取报告/总结中的经验教训"""
        related = self._fetch_related_pages(content)
        prompt = (
            _load_document_prompt("report_summary")
            .replace("{report_content}", content)
            .replace("{related_pages}", related)
        )
        data = self._call_backend(prompt, expect_json=True).require_mapping()

        fragments = []
        ai_expansions = []

        obj = data
        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 提取文档级 frontmatter（新格式）
        doc_frontmatter = data.get("frontmatter", {})
        doc_boundaries = doc_frontmatter.get("boundaries", {})
        doc_anti_patterns = doc_frontmatter.get("anti_patterns", [])
        doc_keywords = doc_frontmatter.get("关键词", [])
        doc_triggers = doc_frontmatter.get("触发器", [])
        doc_aliases = doc_frontmatter.get("别名", [])

        # 成果 → 经验法则
        for ach in obj.get("key_achievements", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=ach.get("achievement", "成果")[:60],
                frontmatter={
                    "领域": "复盘总结",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=f"成功因素：{ach.get('factors', '')}",
                core_content=f"**成果**：{ach.get('achievement', '')}\n\n"
                f"**数据**：{ach.get('metrics', '')}\n\n"
                f"**成功因素**：{ach.get('factors', '')}",
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 挑战 → 反模式
        for chal in obj.get("key_challenges", []):
            frag = KnowledgeFragment(
                form="反模式",
                title=chal.get("challenge", "挑战")[:60],
                frontmatter={
                    "领域": "复盘总结",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=chal.get("root_cause", ""),
                core_content=f"**挑战**：{chal.get('challenge', '')}\n\n"
                f"**根因**：{chal.get('root_cause', '')}\n\n"
                f"**教训**：{chal.get('lesson', '')}",
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 可复用方法
        for method in obj.get("reusable_methods", []):
            frag = KnowledgeFragment(
                form="方法论",
                title=method.get("method", "方法")[:60],
                frontmatter={
                    "领域": "复盘总结",
                    "关键词": doc_keywords,
                    "触发器": doc_triggers,
                    "别名": doc_aliases,
                },
                background=f"适用场景：{method.get('context', '')}",
                core_content=method.get("method", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            fragments.append(frag)

        # 关联上下文（ADR-019）
        doc_relations = data.get("relations", [])
        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai  # noqa
            frag.relations = doc_relations
        return fragments, dict(data)

    def _extract_generic(
        self, content: str, judge: DocumentJudgeResult
    ) -> Tuple[List[KnowledgeFragment], Dict]:
        """通用文档提取 — 通过 LLM 提取关键知识"""
        prompt = (
            _load_document_prompt("generic_extract")
            .replace("{content}", content)
            .replace("{judge_category}", judge.doc_category or "reference")
            .replace("{judge_entity}", judge.entity_type or "technology")
        )
        data = self._call_backend(prompt, expect_json=True).require_mapping()

        fragments = []
        ai_expansions = []

        obj = data.get("objective_extraction")
        if not isinstance(obj, dict):
            raise ValueError("generic_extract output must contain objective_extraction object")
        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        doc_frontmatter = data.get("frontmatter", {})
        doc_boundaries = doc_frontmatter.get("boundaries", {})
        doc_anti_patterns = doc_frontmatter.get("anti_patterns", [])
        doc_keywords = doc_frontmatter.get("关键词", [])
        doc_triggers = doc_frontmatter.get("触发器", [])
        doc_aliases = doc_frontmatter.get("别名", [])

        # 成果 → 经验法则
        for ach in obj.get("key_achievements", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=ach.get("achievement", "知识点")[:60],
                frontmatter={
                    "类型": "通用知识",
                    "摘要": ach.get("metrics", ""),
                    "触发器": doc_triggers,
                    "关键词": doc_keywords,
                    "别名": doc_aliases,
                    "boundaries": doc_boundaries,
                    "anti_patterns": doc_anti_patterns,
                },
                background=ach.get("factors", ""),
                core_content=ach.get("achievement", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            frag.ai_expansion = ai_expansions  # type: ignore[assignment]  # noqa
            fragments.append(frag)

        # 挑战 → 陷阱
        for ch in obj.get("key_challenges", []):
            frag = KnowledgeFragment(
                form="陷阱",
                title=ch.get("challenge", "风险")[:60],
                frontmatter={
                    "类型": "风险/限制",
                    "摘要": ch.get("root_cause", ""),
                    "触发器": doc_triggers,
                    "关键词": doc_keywords,
                },
                background=ch.get("lesson", ""),
                core_content=ch.get("challenge", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            frag.ai_expansion = ai_expansions  # type: ignore[assignment]  # noqa
            fragments.append(frag)

        # 决策 → 决策记录
        for dec in obj.get("decisions_made", []):
            frag = KnowledgeFragment(
                form="决策记录",
                title=dec.get("decision", "决策")[:60],
                frontmatter={
                    "类型": "决策",
                    "摘要": dec.get("outcome", ""),
                    "触发器": doc_triggers,
                    "关键词": doc_keywords,
                },
                background=dec.get("retrospective", ""),
                core_content=dec.get("decision", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            frag.ai_expansion = ai_expansions  # type: ignore[assignment]  # noqa
            fragments.append(frag)

        # 模式
        for pat in obj.get("patterns_identified", []):
            frag = KnowledgeFragment(
                form="pattern",
                title=pat[:60],
                frontmatter={
                    "类型": "模式",
                    "触发器": doc_triggers,
                    "关键词": doc_keywords,
                },
                background="",
                core_content=pat,
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            frag.ai_expansion = ai_expansions  # type: ignore[assignment]  # noqa
            fragments.append(frag)

        # 方法
        for method in obj.get("reusable_methods", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=method.get("method", "方法")[:60],
                frontmatter={
                    "类型": "方法",
                    "摘要": method.get("context", ""),
                    "触发器": doc_triggers,
                    "关键词": doc_keywords,
                },
                background=method.get("effectiveness", ""),
                core_content=method.get("method", ""),
                boundaries=doc_boundaries,
                anti_patterns=doc_anti_patterns,
                related_concepts=[],
            )
            frag.ai_expansion = ai_expansions  # type: ignore[assignment]  # noqa
            fragments.append(frag)

        doc_relations = data.get("relations", [])
        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai  # noqa
            frag.relations = doc_relations
        return fragments, dict(data)

    # ===== 辅助方法 =====

    def _chunk_by_chapters(self, content: str) -> List[str]:
        """按章节分块（匹配 Markdown ## 标题）

        对于 PDF 按页提取的内容（大量 '## 第 X 页'），会智能合并页面为合理大小的 chunk。
        """
        # 按二级标题分割
        parts = re.split(r"\n(?=##\s)", content)
        if len(parts) <= 1:
            parts = re.split(r"\n(?=###\s)", content)

        # 检测是否是 PDF 按页模式（大量 "## 第 X 页" 标题）
        page_title_count = sum(1 for p in parts if re.match(r"^##\s+第\s*\d+\s*页", p.strip()))
        is_pdf_page_mode = page_title_count > len(parts) * 0.5

        if is_pdf_page_mode and len(parts) > 20:
            # PDF 按页模式：合并页面为大小适中的 chunk，避免单个 chunk 超过 LLM prompt 容量
            # 目标：每个 chunk 不超过 PROMPT，确保 handler 中的 content 不会被截断
            merged = []
            current_chunk = ""
            target_size = PROMPT
            for part in parts:
                if not part.strip():
                    continue
                if len(current_chunk) + len(part) + 1 > target_size and current_chunk:
                    merged.append(current_chunk.strip())
                    current_chunk = part
                else:
                    current_chunk = (current_chunk + "\n" + part).strip() if current_chunk else part
            if current_chunk.strip():
                merged.append(current_chunk.strip())
            return merged

        if len(parts) <= 1:
            # 按字数硬分
            chunk_size = CHUNK_SIZE
            parts = [content[i : i + chunk_size] for i in range(0, len(content), chunk_size)]

        return [p for p in parts if p.strip()]

    _FIELD_TITLES = (
        ("related_concepts", "### 相关概念"),
        ("potential_blindspots", "### 盲区提醒"),
        ("practice_suggestions", "### 实践建议"),
        ("critical_questions", "### 值得思考的问题"),
    )

    def _merge_ai_expansions(self, expansions: List[Any]) -> str:
        """Merge the two prompt-declared AI expansion variants.

        Book concepts emit Markdown strings. Strategy extraction emits
        structured related-concept, blindspot, suggestion, and question lists.
        Mixed or unknown variants are rejected as schema drift.
        """
        if not expansions:
            return ""
        if all(isinstance(expansion, str) for expansion in expansions):
            return self._merge_string_expansions(expansions)
        if all(isinstance(expansion, dict) for expansion in expansions):
            return self._merge_structured_expansions(expansions)
        raise ValueError("AI expansion variants must be homogeneous strings or objects")

    def _merge_string_expansions(self, expansions: List) -> str:
        """新格式：直接拼接非空字符串。"""
        parts = [exp.strip() for exp in expansions if isinstance(exp, str) and exp.strip()]
        return "\n\n".join(parts)

    def _merge_structured_expansions(self, expansions: List[Dict[str, Any]]) -> str:
        """Render structured strategy expansions as deduplicated Markdown."""
        sections: List[str] = []
        for field_name, title in self._FIELD_TITLES:
            items = self._collect_field_values(expansions, field_name)
            section = self._format_field_section(title, items)
            if section:
                sections.append(section)
        return "\n".join(sections)

    @staticmethod
    def _collect_field_values(expansions: List, field: str) -> List[str]:
        """从所有 expansion dict 中收集某个字段的去重值。"""
        values: set = set()
        for exp in expansions:
            if isinstance(exp, dict):
                for value in exp.get(field, []):
                    values.add(value)
        return sorted(values)

    @staticmethod
    def _format_field_section(title: str, items: List[str]) -> str:
        """把字段值格式化为 Markdown 小节。"""
        if not items:
            return ""
        lines = [title, ""]
        lines.extend(f"- {item}" for item in items)
        lines.append("")
        return "\n".join(lines)


# ========== 主管道 ==========


class DocumentDistillationPipeline:
    """文档蒸馏管道 — 外部文档深度处理的主入口"""

    def __init__(
        self,
        wiki_base: str | None = None,
        caller: HttpApiHostAgentCaller | None = None,
        database_dir: str | Path | None = None,
    ):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else self._get_wiki_dir()
        self.database_dir = (Path(database_dir).expanduser() if database_dir is not None
                             else get_config().database_dir)
        self.inbox_dir = self.wiki_base / "00-Inbox"
        self._caller = caller
        backend = backend_from_caller(caller)
        self._judge = DocumentLLMJudge(backend=backend)
        self._extractor = DocumentKnowledgeExtractor(
            backend=backend,
            wiki_base=self.wiki_base,
        )
        self._self_check = DistillSelfCheck(link_probe_worker=get_link_probe_worker())
        self._cross_linker = None

    def _get_wiki_dir(self) -> Path:
        from core.config import get_config

        return get_config().wiki_dir

    @staticmethod
    def _slugify(name: str) -> str:
        """将名称转为 URL/文件安全的 slug"""
        import re

        slug = name.lower().strip()
        slug = re.sub(r"[^\w\u4e00-\u9fa5-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:64] if slug else "untitled"

    @staticmethod
    def _session_short(session_id: str | None) -> str:
        """从 session_id 提取简短前缀，避免文件名出现 ``session__`` 双下划线。"""
        raw = session_id or "unknown"
        short = re.sub(r"^session[_-]?", "", raw)
        return (short[:8] or "unknown").strip("-_")

    def _extract_header_and_content(self, messages: list, meta: dict) -> Tuple[str, str, str, str]:
        """从消息与 meta 中提取内容、标题、文档类型和文件名。"""
        content = messages[0].get("content", "")
        title, doc_type = self._parse_doc_header(content)
        filename = meta.get("filename", title)
        return content, title, doc_type, filename

    def _record_document_signal(
        self,
        sid: str,
        filename: str,
        doc_type: str,
        title: str,
        judge_result: DocumentJudgeResult,
        meta: dict,
    ) -> None:
        """将文档元数据写入画像信号库。"""
        try:
            from core.persona.psyche import get_signal_store
            from datetime import datetime

            store = get_signal_store()
            store.insert_document_signal(
                session_id=sid,
                filename=filename,
                doc_type=doc_type,
                doc_category=judge_result.doc_category,
                title=title,
                key_topics=json.dumps(judge_result.key_topics, ensure_ascii=False),
                entity_type=judge_result.entity_type,
                page_count=meta.get("pages", meta.get("slides", meta.get("chapters", 0))),
                import_timestamp=datetime.now().isoformat(),
                import_source=meta.get("import_source", ""),
                confidence=judge_result.confidence,
            )
            logger.info("[DocPipeline] 文档信号已写入画像系统")
        except DOCUMENT_PIPELINE_OPERATION_ERRORS as e:
            logger.debug("[DocPipeline] 文档信号写入失败: %s", e)

    def _derive_coverage_meta(self, content: str, structured_data: Dict) -> Tuple[str, str]:
        """根据内容和结构化数据推断 source_coverage 与 distill_input_mode。"""
        coverage_meta = structured_data.get("source_coverage", {}) if structured_data else {}
        source_coverage = coverage_meta.get("mode") or (
            "full_chunked" if len(content) > PROMPT else "full"
        )
        distill_input_mode = "chunked" if source_coverage == "full_chunked" else "single"
        return source_coverage, distill_input_mode

    def _run_self_check(self, fragments: List[KnowledgeFragment]) -> List[str]:
        """对片段执行 L5 自检，返回所有 issue。"""
        self_check_issues: List[str] = []
        for frag in fragments:
            # 文档蒸馏没有对话 messages，传入空列表
            issues = self._self_check._check_fragment(frag, [])
            self_check_issues.extend(issues)
            frag.self_check_passed = len(issues) == 0
            frag.self_check_issues = issues
        return self_check_issues

    def _run_cross_linking(self, fragments: List[KnowledgeFragment]) -> List[str]:
        """为片段执行 L6 跨 Agent 关联，返回所有链接。"""
        cross_links: List[str] = []
        if not fragments:
            return cross_links
        try:
            linker = self._get_cross_linker()
            for frag in fragments:
                # 为每个 fragment 生成临时页面路径进行关联
                links = linker.link_after_distill_for_fragment(frag)
                frag.cross_agent_links = [str(line.to_page) for line in links]
                cross_links.extend(frag.cross_agent_links)
        except DOCUMENT_PIPELINE_OPERATION_ERRORS as e:
            logger.debug("[DocPipeline] 跨 Agent 关联失败: %s", e, exc_info=True)
        return cross_links

    def _build_distill_result(
        self,
        sid: str,
        judge_result: DocumentJudgeResult,
        fragments: List[KnowledgeFragment],
        structured_data: Dict,
        self_check_issues: List[str],
        cross_links: List[str],
        source_coverage: str,
        distill_input_mode: str,
    ) -> DocumentDistillResult:
        """组装最终的文档蒸馏结果对象。"""
        category = judge_result.doc_category
        return DocumentDistillResult(
            session_id=sid,
            judgment=judge_result.judgment,
            doc_category=category,
            fragments=fragments,
            book_meta=structured_data.get("book_meta") if category == "book" else None,
            data_insights=structured_data if category == "data" else None,
            strategy_items=structured_data if category == "strategy" else None,
            report_items=structured_data if category == "report" else None,
            self_check_issues=self_check_issues,
            cross_agent_links=cross_links,
            source_coverage=source_coverage,
            distill_input_mode=distill_input_mode,
            truncated=False,
            table_artifacts=list(structured_data.get("table_artifacts") or []),
        )

    def process(self, sid: str, messages: list, meta: dict) -> DocumentDistillResult:
        """处理文档 session，返回蒸馏结果"""
        if not messages:
            return DocumentDistillResult(session_id=sid, judgment="skip")

        content, title, doc_type, filename = self._extract_header_and_content(messages, meta)
        if not content:
            return DocumentDistillResult(session_id=sid, judgment="skip")

        logger.info("[DocPipeline] 开始处理: %s (%s)", title, doc_type)

        # Step 1: LLM 价值判断
        judge_result = self._judge.judge(
            title=title, doc_type=doc_type, content=content, metadata=meta, session_id=sid
        )
        logger.info(
            "[DocPipeline] 判断结果: %s / %s / %s",
            judge_result.judgment,
            judge_result.doc_category,
            judge_result.entity_type,
        )

        self._record_document_signal(sid, filename, doc_type, title, judge_result, meta)

        if judge_result.judgment == "skip":
            return DocumentDistillResult(
                session_id=sid, judgment="skip", doc_category=judge_result.doc_category
            )

        try:
            fragments, structured_data = self._extractor.extract(
                content, judge_result, session_id=sid
            )
        except DistillationAPIError as e:
            logger.error("[DocPipeline] API 故障，蒸馏暂停: %s", e)
            record_document_provider_failure(
                session_id=sid, content=content, metadata=meta,
                database_dir=self.database_dir, source=str(meta.get("source") or filename),
                error=e,
            )
            from core.hephaestus.distillation_engine import pause_distillation

            pause_distillation(
                reason=f"LLM API 故障: {e}", api_chain_desc=e.chain_desc, last_error=str(e)
            )
            try:
                from core.hephaestus.distillation_engine import generate_distillation_error_report

                generate_distillation_error_report(e)
            except DOCUMENT_PIPELINE_OPERATION_ERRORS as report_err:
                logger.warning("[DocPipeline] 生成错误报告失败: %s", report_err)
            return DocumentDistillResult(
                session_id=sid,
                judgment="error",
                doc_category=judge_result.doc_category,
            )
        logger.info("[DocPipeline] 提取 %s 个知识片段", len(fragments))

        source_coverage, distill_input_mode = self._derive_coverage_meta(content, structured_data)

        self_check_issues = self._run_self_check(fragments)
        cross_links = self._run_cross_linking(fragments)
        result = self._build_distill_result(
            sid,
            judge_result,
            fragments,
            structured_data,
            self_check_issues,
            cross_links,
            source_coverage,
            distill_input_mode,
        )
        source_ref_keys = ("raw_event_id", "source_event_id", "provenance_id", "asset_id")
        source_refs = [str(meta[key]) for key in source_ref_keys if str(meta.get(key) or "").strip()]
        result.raw_response, result.failure_parse_metadata = (
            self._extractor.failure_evidence(content, source_refs)
        )
        return result

    def _filter_valid_fragments(
        self, result: DocumentDistillResult, source: str
    ) -> Optional[List[KnowledgeFragment]]:
        """根因修复并校验片段；返回合法片段列表，整体拒绝时返回 None。"""
        fragments = list(result.fragments)
        for frag in fragments:
            _auto_remediate_fragment(frag)

        # 片段级校验，收集失败索引
        frag_errors = [(i, _validate_fragment(frag)) for i, frag in enumerate(fragments)]
        all_errors = [f"片段[{i}] {err}" for i, errs in frag_errors for err in errs]
        failed_indices = {i for i, errs in frag_errors if errs}

        if not failed_indices:
            return fragments

        total = len(fragments)
        passed_count = total - len(failed_indices)
        ratio = passed_count / total
        from core.kia.policy import get_shadowed_value

        min_ratio = float(
            get_shadowed_value(
                "distill.min_session_fragment_pass_ratio",
                get_config().get("distill.min_session_fragment_pass_ratio", 0.5),
            )
        )  # noqa: E501
        if ratio < min_ratio:
            _save_failed_distill(
                result.session_id,
                fragments,
                all_errors,
                source=source,
                raw_response=result.raw_response,
                parse_metadata=result.failure_parse_metadata,
                database_dir=self.database_dir,
                producer="document_distillation",
            )
            logger.warning(
                "[DocPipeline] Session %s 未通过硬校验 (%s 项错误)，已保存事故证据并安排诊断",
                result.session_id,
                len(all_errors),
            )
            return None

        # 部分失败：写入合法片段，失败片段单独保存以便排查
        failed_fragments = [frag for i, frag in enumerate(fragments) if i in failed_indices]
        _save_failed_distill(
            result.session_id,
            failed_fragments,
            all_errors,
            source=source,
            raw_response=result.raw_response,
            parse_metadata=result.failure_parse_metadata,
            database_dir=self.database_dir,
            severity="medium",
            producer="document_distillation",
        )
        logger.warning(
            "[DocPipeline] Session %s 部分片段未通过硬校验（%d/%d 通过），已写入合法片段并保存失败片段",
            result.session_id,
            passed_count,
            total,
        )
        return [frag for i, frag in enumerate(fragments) if i not in failed_indices]

    def _write_single_page(
        self,
        frag: KnowledgeFragment,
        sid: str,
        source: str,
        result: DocumentDistillResult,
        seen_slugs: set[str],
    ) -> Optional[Path]:
        """生成并原子写入单个 wiki 页面，处理 slug 去重与磁盘冲突。"""
        self._attach_result_frontmatter(frag, result)
        title = frag.title or fm_get(frag.frontmatter, "name") or "untitled"
        page_id, path = allocate_routed_title_path(
            wiki_base=self.wiki_base,
            inbox_dir=self.inbox_dir,
            title=str(title),
            frontmatter=frag.frontmatter,
            source_id=self._session_short(sid),
            seen_slugs=seen_slugs,
        )
        md = generate_wiki_page(
            frag,
            sid,
            source=source,
            session_coverage=result.source_coverage,
            distill_input_mode=result.distill_input_mode,
            distill_prompt_version=PROMPT_VERSION,
            covered_turn_range=result.covered_turn_range,
            truncated=result.truncated,
        )
        trusted = submit_document_page_candidate(
            wiki_base=self.wiki_base,
            fragment=frag,
            session_id=sid,
            source=source,
            page_id=page_id,
            file_path=path,
            page_content=md,
        )
        if trusted.proposal_id:
            logger.info(
                "[trusted_push] %s document proposal %s status=%s target=%s",
                trusted.mode,
                trusted.proposal_id,
                trusted.status,
                path,
            )
        if trusted.intercepted:
            logger.info("[DocPipeline] trusted_push enforce 已拦截 wiki 写入: %s", path.name)
            return None
        from core.trust.vault_mutation_service import commit_trusted_markdown

        commit_trusted_markdown(
            trusted,
            target_path=path,
            content=md,
            material_action=trusted.material_action,
        )
        logger.info("[DocPipeline] 已写入 wiki: %s", path.name)
        return path

    @staticmethod
    def _attach_result_frontmatter(frag: KnowledgeFragment, result: DocumentDistillResult) -> None:
        """Attach result-level document metadata before rendering a page."""
        if result.book_meta:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("book_meta", result.book_meta)
        if result.data_insights:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("data_insights", result.data_insights)
        if result.strategy_items:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("strategy_items", result.strategy_items)
        if result.report_items:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("report_items", result.report_items)
        if result.table_artifacts:
            frag.frontmatter = dict(frag.frontmatter or {})
            frag.frontmatter.setdefault("table_artifacts", result.table_artifacts)

    def _update_wiki_metrics(self, written: List[Path]) -> None:
        """更新 Wiki metrics 与首页。"""
        try:
            from core.wiki_metrics import WikiMetrics, write_mnemos_home

            metrics = WikiMetrics(wiki_dir=str(self.wiki_base))
            for path in written:
                rel_path = (
                    str(path.relative_to(self.wiki_base))
                    if self.wiki_base in path.parents
                    else str(path)
                )
                content = path.read_text(encoding="utf-8", errors="ignore")
                metrics.assess_quality(rel_path, content)
                metrics.upsert_page(
                    rel_path, title=path.stem, source_count=1, heat_score=1.0, heat_level="warm"
                )
            write_mnemos_home(str(self.wiki_base))
        except (
            OSError, ValueError, TypeError, KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("文档 Wiki metrics/dashboard 更新失败", exc_info=True)

    def _emit_distilled_event(
        self, sid: str, result: DocumentDistillResult, written: List[Path]
    ) -> None:
        """发射 knowledge_distilled 事件，供 KG / embedding 索引消费。"""
        if written:
            _emit_knowledge_distilled(
                sid, result, [str(p) for p in written]  # type: ignore[arg-type]
            )  # type: ignore[arg-type]

    def _emit_page_events(
        self, sid: str, page_fragment_pairs: List[Tuple[Path, KnowledgeFragment]]
    ) -> None:
        """发射 per-page distill_complete 与 wiki_page_updated 事件。"""
        if not page_fragment_pairs:
            return

        try:
            from core.mnemos_bus import publish_event

            for path, frag in page_fragment_pairs:
                publish_event(
                    "distill_complete",
                    "distill",
                    {
                        "page_path": str(path),
                        "title": frag.title,
                        "session_id": sid,
                        "form": frag.form,
                    },
                )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.debug("distill_complete event emit failed", exc_info=True)

        for path, _ in page_fragment_pairs:
            publish_wiki_page_updated(path, update_type="create")

    def write_to_wiki(self, result: DocumentDistillResult, source: str = "") -> List[Path]:
        """将蒸馏结果写入 wiki Inbox，并记录来源追踪。

        与 DistillationEngine.write_pages 保持一致：先根因修复，再片段级校验，
        仅当失败占比超过阈值时才整体拒绝。
        """
        written: List[Path] = []
        if not result.fragments:
            return written

        fragments = self._filter_valid_fragments(result, source)
        if fragments is None:
            return written

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        sid = result.session_id

        seen_slugs: set[str] = set()
        page_fragment_pairs: List[Tuple[Path, KnowledgeFragment]] = []
        for frag in fragments:
            path = self._write_single_page(frag, sid, source, result, seen_slugs)
            if path is None:
                continue
            written.append(path)
            page_fragment_pairs.append((path, frag))

        # 部分降级后，result.fragments 只保留实际写入的合法片段
        result.fragments = fragments

        # 记录来源追踪（文档蒸馏路径）
        self._record_source_links(sid, source, written)
        self._update_wiki_metrics(written)
        self._emit_distilled_event(sid, result, written)
        self._emit_page_events(sid, page_fragment_pairs)

        return written

    def _record_source_links(self, session_id: str, source: str, paths: List[Path]) -> None:
        """记录文档→Wiki 的来源追踪到知识图谱数据库"""
        try:
            from core.config import get_config
            import sqlite3

            db_path = get_config().database_dir / "knowledge_graph.db"
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS document_wiki_link (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        source TEXT DEFAULT '',
                        wiki_page_path TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                for p in paths:
                    rel_path = (
                        str(p.relative_to(self.wiki_base))
                        if self.wiki_base in p.parents
                        else str(p)
                    )
                    conn.execute(
                        """INSERT INTO document_wiki_link (session_id, source, wiki_page_path)
                           VALUES (?, ?, ?)""",
                        (session_id, source, rel_path),
                    )
                conn.commit()
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ):
            logger.debug("来源追踪记录失败", exc_info=True)

    def _parse_doc_header(self, content: str) -> Tuple[str, str]:
        """从内容第一行解析文档标题和类型"""
        match = re.search(r"^#\s+[^\s]+\s+(\w+):\s*(.+)$", content, re.MULTILINE)
        if match:
            return match.group(2).strip(), match.group(1).strip().lower()
        return "未命名文档", "unknown"

    def _get_cross_linker(self):
        """懒加载跨 Agent 关联器"""
        if self._cross_linker is None:
            from core.kia.cross_agent_linker import CrossAgentLinker

            self._cross_linker = CrossAgentLinker(wiki_root=self.wiki_base)
        return self._cross_linker
