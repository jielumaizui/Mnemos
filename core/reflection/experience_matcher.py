"""
Experience Similarity — 历史经验匹配

让 Reflection 能说出"这是你第四次尝试解决同一个问题"。

召回源：
- 历史 ReflectionRecord
- CognitiveShift
- 06-Retrospectives/ 页面
- Knowledge Graph（可选）

匹配策略：
1. 优先使用 embedding 语义相似度（如果 embedding 客户端可用）
2. 否则回退到关键词重叠 + 角色/场景/维度加权
"""

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
from core.reflection.models import CognitiveShift, ReflectionRecord
from core.reflection.reflection_store import ReflectionStore

logger = logging.getLogger(__name__)


@dataclass
class ExperienceMatch:
    """匹配到的历史经验"""

    source_type: str  # "reflection" | "cognitive_shift" | "retrospective" | "knowledge"
    source_id: str  # record id / file path / knowledge id
    title: str  # 一句话标题
    summary: str  # 摘要
    score: float  # 综合相似度分数 0-1
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "score": round(self.score, 4),
            "metadata": self.metadata,
        }


class ExperienceMatcher:
    """历史经验匹配器"""

    def __init__(
        self,
        reflection_store: Optional[ReflectionStore] = None,
        wiki_dir: Optional[str] = None,
        embedding_client: Optional[SiliconFlowEmbeddingClient] = None,
    ):
        self.store = reflection_store or ReflectionStore()
        self.wiki_dir = Path(wiki_dir) if wiki_dir else None
        self.embedding_client = embedding_client

    def find_similar(
        self,
        query: str,
        scene: Optional[str] = None,
        role: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        top_k: int = 5,
        *,
        subject_scope: tuple[str, str] | None = None,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> List[ExperienceMatch]:
        """
        查找与当前场景最相似的历史经验

        Args:
            query: 用户当前查询
            scene: 当前场景标签
            role: 当前角色标签
            dimensions: 相关观察维度
            top_k: 返回条数

        Returns:
            List[ExperienceMatch]，按 score 降序
        """
        if principal is None:
            return []
        candidates = self._gather_candidates(
            principal=principal,
            narrowing=narrowing,
        )
        if not candidates:
            return []

        # 文本表示
        candidate_texts = [self._candidate_text(c) for _, c in candidates]
        candidate_subject_scopes = [
            self._candidate_subject_scopes(candidate) for _, candidate in candidates
        ]

        # 语义相似度
        semantic_scores = self._semantic_scores(
            query,
            candidate_texts,
            candidate_subject_scopes=candidate_subject_scopes,
            query_subject_scope=subject_scope,
        )

        # 关键词重叠
        keyword_scores = self._keyword_scores(query, candidate_texts)

        matches = []
        for (source_type, candidate), sem, key in zip(candidates, semantic_scores, keyword_scores):
            # 基础分：语义 + 关键词混合
            base_score = sem * 0.6 + key * 0.4

            # 加权：角色/场景/维度匹配
            boost = self._compute_boost(candidate, scene, role, dimensions)
            score = min(1.0, base_score * (1 + boost))

            title, summary = self._candidate_summary(candidate)
            source_id = self._candidate_id(candidate)

            matches.append(
                ExperienceMatch(
                    source_type=source_type,
                    source_id=source_id,
                    title=title,
                    summary=summary,
                    score=score,
                    metadata=self._candidate_metadata(candidate),
                )
            )

        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:top_k]

    def _gather_candidates(
        self,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> List[Tuple[str, object]]:
        """收集所有候选经验"""
        candidates: List[Tuple[str, object]] = []
        if principal is None:
            return candidates

        # 1. 历史 ReflectionRecord
        try:
            records, _summary = self.store.authorized_get_latest(
                principal=principal,
                narrowing=narrowing,
                purpose="reflection_experience_read",
                limit=200,
            )
            for record in records:
                candidates.append(("reflection", record))
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning("Failed to load reflection records: %s", e)

        # 2. 认知变迁
        try:
            shifts, _summary = self.store.authorized_get_shifts(
                principal=principal,
                narrowing=narrowing,
                purpose="reflection_experience_read",
                limit=200,
            )
            for shift in shifts:
                candidates.append(("cognitive_shift", shift))
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning("Failed to load cognitive shifts: %s", e)

        # Retrospective Markdown has no Reflection object ACL.  Do not read it
        # directly into an LLM prompt; it must first enter the Wiki retrieval
        # seam that validates its own metadata and body authorization.

        return candidates

    @staticmethod
    def _candidate_text(candidate: object) -> str:
        """生成候选的文本表示用于匹配"""
        if isinstance(candidate, ReflectionRecord):
            parts = [
                candidate.trigger_event,
                candidate.user_query,
            ]
            if candidate.insight:
                parts.append(candidate.insight.summary)
                parts.extend(candidate.insight.key_points)
            return " ".join(p for p in parts if p)

        if isinstance(candidate, CognitiveShift):
            return " ".join(
                [
                    candidate.dimension,
                    candidate.shift_type,
                    candidate.from_state,
                    candidate.to_state,
                    *candidate.evidence,
                ]
            )

        if isinstance(candidate, dict) and "text" in candidate:
            text = candidate["text"]
            # 只取前 800 字符降低开销
            return text[:800]  # type: ignore[no-any-return]

        return str(candidate)

    @staticmethod
    def _keyword_similarity(a: str, b: str) -> float:
        """embedding 不可用时，用关键词 Jaccard 重叠作为语义分回退"""

        def tokens(text: str) -> set:
            words = set(re.findall(r"[a-zA-Z]+", text.lower()))
            chars = set(re.findall(r"[\u4e00-\u9fa5]", text))
            return words | chars

        ta, tb = tokens(a), tokens(b)
        if not ta or not tb:
            return 0.0
        union = ta | tb
        if not union:
            return 0.0
        return len(ta & tb) / len(union)

    @staticmethod
    def _candidate_summary(candidate: object) -> Tuple[str, str]:
        """生成标题和摘要"""
        if isinstance(candidate, ReflectionRecord):
            title = f"Reflection {candidate.id}"
            summary = candidate.insight.summary if candidate.insight else candidate.trigger_event
            return title, summary[:300]

        if isinstance(candidate, CognitiveShift):
            title = f"Cognitive Shift: {candidate.dimension}"
            summary = f"{candidate.from_state} → {candidate.to_state}"
            return title, summary[:300]

        if isinstance(candidate, dict) and "path" in candidate:
            title = Path(candidate["path"]).stem
            summary = candidate["text"][:300]
            return title, summary

        return "Unknown", str(candidate)[:300]

    @staticmethod
    def _candidate_id(candidate: object) -> str:
        if isinstance(candidate, ReflectionRecord):
            return candidate.id
        if isinstance(candidate, CognitiveShift):
            # 认知变迁无稳定 id，用内容哈希
            content = f"{candidate.dimension}:{candidate.shift_type}:{candidate.from_state}:{candidate.to_state}:{candidate.confidence}"  # noqa: E501
            return f"shift:{hash(content) & 0x7fffffff}"
        if isinstance(candidate, dict) and "path" in candidate:
            return candidate["path"]  # type: ignore[no-any-return]
        return str(id(candidate))

    @staticmethod
    def _candidate_metadata(candidate: object) -> Dict:
        if isinstance(candidate, ReflectionRecord):
            return {
                "trigger": candidate.trigger.value,
                "dimensions": candidate.mirror_dimensions,
                "created_at": candidate.created_at.isoformat(),
            }
        if isinstance(candidate, CognitiveShift):
            return {
                "dimension": candidate.dimension,
                "shift_type": candidate.shift_type,
                "confidence": candidate.confidence,
            }
        if isinstance(candidate, dict) and "path" in candidate:
            return {"path": candidate["path"]}
        return {}

    @staticmethod
    def _candidate_subject_scopes(candidate: object) -> tuple[tuple[str, str], ...]:
        """Return a concrete asset identity when this matcher has one.

        Retrospective candidates originate from a local Markdown asset and can
        therefore be deleted by path.  Reflection and shift rows do not carry
        a session/project owner in this surface, so they deliberately use a
        fixed controlled source rather than inventing a per-user identity.
        """
        if isinstance(candidate, dict) and str(candidate.get("path") or "").strip():
            return (("path", str(Path(str(candidate["path"])).expanduser().resolve(strict=False))),)
        if isinstance(candidate, ReflectionRecord):
            return (("source", "experience_matcher_reflection_store"),)
        if isinstance(candidate, CognitiveShift):
            return (("source", "experience_matcher_cognitive_shift"),)
        return (("source", "experience_matcher_unknown_candidate"),)

    def _uses_remote_embedding_client(self) -> bool:
        return isinstance(self.embedding_client, SiliconFlowEmbeddingClient)

    def _semantic_scores(
        self,
        query: str,
        candidate_texts: List[str],
        *,
        candidate_subject_scopes: List[tuple[tuple[str, str], ...]] | None = None,
        query_subject_scope: tuple[str, str] | None = None,
    ) -> List[float]:
        """语义相似度（无 embedding 时回退到关键词 Jaccard 重叠）"""
        if not self.embedding_client:
            return [self._keyword_similarity(query, t) for t in candidate_texts]

        try:
            all_texts = [query] + candidate_texts
            if self._uses_remote_embedding_client():
                effective_query_scope = query_subject_scope
                if effective_query_scope is None:
                    from core.telemetry.prompt_call_log import current_model_call_run

                    if current_model_call_run() is None:
                        effective_query_scope = ("source", "experience_matcher_query")
                entry_subject_scopes = [
                    (effective_query_scope,) if effective_query_scope is not None else (),
                    *(candidate_subject_scopes or [
                        (("source", "experience_matcher_candidate"),)
                        for _ in candidate_texts
                    ]),
                ]
                embeddings = self.embedding_client.embed(
                    all_texts,
                    subject_scopes=entry_subject_scopes,
                )
            else:
                embeddings = self.embedding_client.embed(all_texts)
            query_emb = embeddings[0]
            candidate_embs = embeddings[1:]

            scores = []
            for emb in candidate_embs:
                score = self._cosine_similarity(query_emb, emb)
                scores.append(max(0.0, score))
            return scores
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, ArithmeticError) as e:
            logger.warning("Embedding similarity failed, fallback to keyword: %s", e)
            return [self._keyword_similarity(query, t) for t in candidate_texts]

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)  # type: ignore[no-any-return]

    @staticmethod
    def _keyword_scores(query: str, candidate_texts: List[str]) -> List[float]:
        """关键词重叠分数"""
        query_tokens = set(ExperienceMatcher._tokenize(query))
        if not query_tokens:
            return [0.0] * len(candidate_texts)

        scores = []
        for text in candidate_texts:
            text_tokens = set(ExperienceMatcher._tokenize(text))
            if not text_tokens:
                scores.append(0.0)
                continue
            overlap = len(query_tokens & text_tokens)
            union = len(query_tokens | text_tokens)
            scores.append(overlap / union if union > 0 else 0.0)
        return scores

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """轻量分词：中文单字 + 英文单词"""
        text = text.lower()
        # 英文单词
        words = re.findall(r"[a-z]{3,}", text)
        # 中文字符
        chars = re.findall(r"[一-鿿]", text)
        return words + chars

    @staticmethod
    def _compute_boost(
        candidate: object,
        scene: Optional[str],
        role: Optional[str],
        dimensions: Optional[List[str]],
    ) -> float:
        """根据角色、场景、维度匹配计算加权"""
        boost = 0.0
        text = ExperienceMatcher._candidate_text(candidate).lower()

        if scene and scene.lower() in text:
            boost += 0.1

        if role and role.lower() in text:
            boost += 0.1

        if dimensions:
            for dim in dimensions:
                if dim.lower() in text:
                    boost += 0.05

        # ReflectionRecord 中显式维度匹配
        if isinstance(candidate, ReflectionRecord) and dimensions:
            for dim in dimensions:
                if dim in (candidate.mirror_dimensions or []):
                    boost += 0.1

        return min(boost, 0.5)
