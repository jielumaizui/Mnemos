"""Document distillation value judge."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from core.hephaestus.backend_bundle import backend_from_caller
from core.hephaestus.distill_backend import DistillBackend
from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.prompt_builder import TemplateRegistry

PREVIEW = 3000
_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "distill"
logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _load_document_prompt(name: str) -> str:
    registry = TemplateRegistry(_PROMPT_DIR)
    try:
        return registry.select("document", name)
    except FileNotFoundError:
        logger.warning("[DocPrompt] 模板文件不存在: document/%s", name)
        return ""


@dataclass
class DocumentJudgeResult:
    """文档价值判断结果"""

    judgment: str = "skip"
    doc_category: str = "reference"
    entity_type: str = "technology"
    key_topics: List[str] = field(default_factory=list)
    audience: str = ""
    why: str = ""
    confidence: float = 0.0


class DocumentLLMJudge:
    """文档价值判断器 — 判定文档是否值得索引，以及文档类别"""

    def __init__(
        self,
        backend: DistillBackend | None = None,
        caller: HttpApiHostAgentCaller | None = None,
    ):
        self._backend = backend or backend_from_caller(caller)

    def judge(
        self, title: str, doc_type: str, content: str, metadata: Dict, session_id: str = ""
    ) -> DocumentJudgeResult:
        preview = content[:PREVIEW]
        outline = self._extract_outline(content)
        page_count = metadata.get("pages", metadata.get("slides", metadata.get("chapters", 0)))

        prompt = (
            _load_document_prompt("judge")
            .replace("{title}", title)
            .replace("{doc_type}", doc_type)
            .replace("{page_count}", str(page_count))
            .replace("{outline}", outline or "无目录")
            .replace("{content_preview}", preview)
        )
        data = self._backend.call(prompt, expect_json=True).require_mapping()
        return DocumentJudgeResult(
            judgment=data.get("judgment", "skip"),
            doc_category=data.get("doc_category", "reference"),
            entity_type=data.get("entity_type", "technology"),
            key_topics=data.get("key_topics", []),
            audience=data.get("audience", ""),
            why=data.get("why", ""),
            confidence=0.85 if data else 0.5,
        )

    def _extract_outline(self, content: str) -> str:
        headings = re.findall(r"^#{1,3}\s+(.+)$", content, re.MULTILINE)
        return "\n".join(f"- {h}" for h in headings[:20]) if headings else ""
