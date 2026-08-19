# -*- coding: utf-8 -*-
"""
PromptBuilder — 蒸馏 Prompt 构造系统

三大支柱：
  TemplateRegistry      — 文件系统模板，支持继承回退
  ContextAssembler      — 组装模板变量（原始内容 + 相关上下文 + 系统指令）
  TokenBudgetManager    — 16k Token 预算，截断优先级：相关上下文 > 对话中间 > 系统指令

5 种任务类型：
  value_judge  — 会话 → knowledge/skill/skip 判断
  extract      — 会话 + 相关 Wiki → 知识片段（JSON）
  incremental  — 新会话 + 目标页面 → 追加/替换/冲突更新
  backlink     — 目标页面 + 反向链接页面 → 关联概述（Markdown）
  merge        — 积压项目 → 合并片段（JSON）
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from core.config import get_config
from core.hephaestus.distillation_prompts import PROMPT_VERSION
from core.hephaestus.behavior_intent import (
    format_behavior_intent_context,
    infer_behavior_intent_signal,
)
from core.hephaestus.tokenizer import get_tokenizer

# 默认回退值，仅用于配置未设置时。
DEFAULT_TOKEN_BUDGET_TOTAL = 16000
DEFAULT_CONTENT_FORMATTER_MAX_TOKENS = 8000


# ========== 数据模型 ==========

logger = logging.getLogger(__name__)


@dataclass
class TokenBudget:
    """Token 预算配置

    优先从 config.distill.* 读取，未配置时使用默认值。
    """

    total_limit: int = DEFAULT_TOKEN_BUDGET_TOTAL
    system_pct: float = 0.10
    context_pct: float = 0.25
    content_pct: float = 0.55
    output_reserve: int = 2000

    @classmethod
    def from_config(cls) -> "TokenBudget":
        """从全局配置构建 TokenBudget。"""
        cfg = get_config()
        return cls(
            total_limit=int(
                cfg.get("distill.token_budget_total", DEFAULT_TOKEN_BUDGET_TOTAL)
                or DEFAULT_TOKEN_BUDGET_TOTAL
            ),
            system_pct=float(cfg.get("distill.token_budget_system_pct", 0.10) or 0.10),
            context_pct=float(cfg.get("distill.token_budget_context_pct", 0.25) or 0.25),
            content_pct=float(cfg.get("distill.token_budget_content_pct", 0.55) or 0.55),
            output_reserve=int(cfg.get("distill.token_budget_output_reserve", 2000) or 2000),
        )

    @property
    def available_for_input(self) -> int:
        return self.total_limit - self.output_reserve

    @property
    def system_limit(self) -> int:
        return int(self.available_for_input * self.system_pct)

    @property
    def context_limit(self) -> int:
        return int(self.available_for_input * self.context_pct)

    @property
    def content_limit(self) -> int:
        return int(self.available_for_input * self.content_pct)


@dataclass
class PromptWikiPage:
    """Wiki 页面引用（蒸馏 Prompt 上下文）"""

    path: Path
    title: str
    content: str = ""

    def read_content(self) -> str:
        if self.content:
            return self.content
        try:
            return self.path.read_text(encoding="utf-8")
        except (OSError, IOError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at prompt_builder.py", exc_info=True
            )
            return ""


@dataclass
class DeferredRecord:
    """延迟蒸馏队列记录"""

    session_id: str
    agent_name: str
    content: str


@dataclass
class Session:
    """蒸馏会话"""

    id: str
    messages: List[Dict]
    agent_name: str = "unknown"
    # Immutable non-content fields the extraction output must echo exactly.
    input_contract: Dict[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> str:
        return "\n".join(m.get("content", "") for m in self.messages)


@dataclass
class DistillTask:
    """蒸馏任务定义 — PromptBuilder 的唯一输入"""

    task_type: str  # value_judge | extract | incremental | backlink | merge | skill_suggestion
    session: Optional[Session] = None
    session_type: str = (
        "general"  # coding | marketing | analysis | strategy | writing | review | general
    )
    target_wiki_page: Optional[PromptWikiPage] = None
    related_pages: List[PromptWikiPage] = field(default_factory=list)
    backlog_items: List[DeferredRecord] = field(default_factory=list)
    budget_config: TokenBudget = field(default_factory=TokenBudget.from_config)
    preformatted: bool = False  # True 表示 session.messages 已是格式化文本，跳过 ContentFormatter


@dataclass
class RelatedPage:
    """相关 Wiki 页面"""

    page_path: Path
    title: str
    summary: str
    relevance: float
    match_type: str  # "entity" | "jaccard"


# ========== ContentFormatter ==========


class ContentFormatter:
    """内容格式化器 — 清洗 + 截断"""

    CLEANING_RULES: List[Tuple[str, str, int]] = [
        (r"\[thinking\].*?\[/thinking\]", "", re.DOTALL),
        (r"(?:让我试一下|现在修改|我来测试|好的，我)[^\n]*\n?", "", 0),
        (r"(?:帮我|能否|怎么|请|麻烦你)[^?\n]*[?？]\s*", "", 0),
    ]

    def format_session(
        self,
        session: Session,
        max_tokens: int | None = None,
        keep_code: bool = False,
    ) -> str:
        """格式化会话内容，按 token 预算截断。

        Args:
            max_tokens: Token 上限（默认读取 distill.content_formatter_max_tokens，
                       未配置时使用 8000）
            keep_code: 是否保留代码块
        """
        if max_tokens is None:
            cfg = get_config()
            max_tokens = int(
                cfg.get(
                    "distill.content_formatter_max_tokens", DEFAULT_CONTENT_FORMATTER_MAX_TOKENS
                )
                or DEFAULT_CONTENT_FORMATTER_MAX_TOKENS
            )
        tokenizer = get_tokenizer()
        sections = []
        for i, msg in enumerate(session.messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content = self._clean(content, keep_code=keep_code)
            if not content.strip():
                continue
            sections.append(f"### Message {i} ({role})\n\n{content}")

        full = "\n---\n\n".join(sections)

        # Token-based 截断
        current_tokens = tokenizer.estimate(full)
        if current_tokens > max_tokens:
            full = tokenizer.truncate_to_tokens(full, max_tokens)
            # 追加截断标记（如果 truncate_to_tokens 没有在边界处截断）
            if not full.endswith("...(truncated)"):
                full += "\n\n[... 对话已按 token 预算截断 ...]\n\n"

        return full

    def _clean(self, content: str, keep_code: bool = False) -> str:
        for pattern, replacement, flags in self.CLEANING_RULES:
            content = re.sub(pattern, replacement, content, flags=flags)
        if not keep_code:
            content = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()


# ========== RelatedContextRetriever ==========


class RelatedContextRetriever:
    """相关上下文检索器 — 实体匹配 + Jaccard 关键词重叠"""

    MAX_RELATED_PAGES = 5
    MAX_CHARS_PER_PAGE = 500
    JACCARD_THRESHOLD = 0.3

    def __init__(self, wiki_dir: Path):
        self.wiki_dir = wiki_dir

    def retrieve(self, session: Session) -> List[RelatedPage]:
        """检索与 session 相关的 Wiki 页面"""
        if not session or not session.content:
            return []

        by_entity = self._find_by_entities(session.content)
        by_jaccard = self._find_by_jaccard(session.content)

        merged = {}  # type: ignore[var-annotated]
        for rp in by_entity + by_jaccard:
            key = str(rp.page_path)
            if key not in merged or rp.relevance > merged[key].relevance:
                merged[key] = rp

        results = sorted(merged.values(), key=lambda r: r.relevance, reverse=True)
        return results[: self.MAX_RELATED_PAGES]

    def format_for_prompt(self, pages: List[RelatedPage]) -> str:
        """格式化相关页面为 Prompt 片段"""
        if not pages:
            return "（暂无相关已有知识）"

        lines = ["## 相关已有知识（请避免重复创建，优先补充或关联）", ""]
        for page in pages:
            lines.append(f"### {page.title}")
            lines.append(f"- 路径: {page.page_path}")
            lines.append(f"- 摘要: {page.summary[:self.MAX_CHARS_PER_PAGE]}")
            lines.append("")
        return "\n".join(lines)

    def _find_by_entities(self, content: str) -> List[RelatedPage]:
        """实体匹配检索"""
        entities = self._extract_entities(content)
        if not entities:
            return []

        results = []
        for md_file in self._scan_wiki_pages():
            fm = self._read_frontmatter(md_file)
            if not fm:
                continue
            page_entities = set()
            if "entities" in fm:
                page_entities.update(fm["entities"] if isinstance(fm["entities"], list) else [])
            kw = fm.get("关键词", {})
            if isinstance(kw, dict):
                core = kw.get("核心概念", [])
                if isinstance(core, list):
                    page_entities.update(core)

            if not page_entities:
                continue
            overlap = entities & page_entities
            if not overlap:
                continue
            relevance = len(overlap) / len(entities) if entities else 0
            results.append(
                RelatedPage(
                    page_path=md_file,
                    title=fm.get("title", md_file.stem),
                    summary=self._extract_summary(md_file),
                    relevance=relevance,
                    match_type="entity",
                )
            )
        return results

    def _find_by_jaccard(self, content: str) -> List[RelatedPage]:
        """Jaccard 关键词重叠检索"""
        content_kw = self._extract_keywords(content)
        if not content_kw:
            return []

        results = []
        for md_file in self._scan_wiki_pages():
            fm = self._read_frontmatter(md_file)
            page_kw = set()  # type: ignore[var-annotated]
            if fm:
                kw = fm.get("关键词", {})
                if isinstance(kw, dict):
                    for layer in ("核心概念", "场景标签", "工具实体"):
                        words = kw.get(layer, [])
                        if isinstance(words, list):
                            page_kw.update(w.lower() for w in words if isinstance(w, str))
                if "entities" in fm:
                    ents = fm["entities"]
                    if isinstance(ents, list):
                        page_kw.update(e.lower() for e in ents if isinstance(e, str))

            if not page_kw:
                page_kw = self._extract_keywords(md_file.stem)

            jaccard = self._jaccard(content_kw, page_kw)
            if jaccard >= self.JACCARD_THRESHOLD:
                results.append(
                    RelatedPage(
                        page_path=md_file,
                        title=fm.get("title", md_file.stem) if fm else md_file.stem,
                        summary=self._extract_summary(md_file),
                        relevance=jaccard,
                        match_type="jaccard",
                    )
                )
        return results

    def _scan_wiki_pages(self) -> List[Path]:
        """扫描 Wiki 目录下所有 .md 文件"""
        pages = []  # type: ignore[var-annotated]
        if not self.wiki_dir.exists():
            return pages
        for subdir in [
            "00-Inbox",
            "01-People",
            "02-Projects",
            "03-Tech",
            "04-Concepts",
            "05-MOCs",
            "06-Retrospectives",
            "08-Reminders",
            "99-Reports",
        ]:
            d = self.wiki_dir / subdir
            if d.exists():
                pages.extend(d.glob("*.md"))
        return pages

    @staticmethod
    def _read_frontmatter(md_file: Path) -> Optional[Dict]:
        """解析 Markdown 文件的 YAML frontmatter"""
        try:
            text = md_file.read_text(encoding="utf-8")
            if not text.startswith("---"):
                return None
            parts = text.split("---", 2)
            if len(parts) < 3:
                return None
            fm_text = parts[1].strip()
            if not fm_text:
                return {}
            return yaml.safe_load(fm_text) or {}
        except (OSError, ValueError, yaml.YAMLError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at prompt_builder.py", exc_info=True
            )
            return None

    @staticmethod
    def _extract_summary(md_file: Path) -> str:
        """提取页面摘要（前 300 字，跳过 frontmatter）"""
        try:
            text = md_file.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3 :]
            return text.strip()[:300]
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at prompt_builder.py", exc_info=True
            )
            return ""

    @staticmethod
    def _extract_entities(text: str) -> set:
        pass

        words = set()  # type: ignore[var-annotated]
        words.update(w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", text))
        words.update(re.findall(r"[一-龥]{2,6}", text))
        return words

    @staticmethod
    def _extract_keywords(text: str) -> set:
        kw = set()  # type: ignore[var-annotated]
        kw.update(w.lower() for w in re.findall(r"[a-zA-Z_]{3,}", text))
        kw.update(re.findall(r"[一-龥]{2,4}", text))
        return kw

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


# ========== ContextAssembler ==========


class ContextAssembler:
    """上下文组装器 — 原始内容 + 相关上下文 + 系统指令"""

    def __init__(self, wiki_dir: Path):
        self.wiki_dir = wiki_dir
        self.content_formatter = ContentFormatter()
        self.context_retriever = RelatedContextRetriever(wiki_dir)

    def assemble(self, task: DistillTask) -> Dict[str, str]:
        """组装完整模板变量"""
        context: Dict[str, str] = {
            "current_date": datetime.now().strftime("%Y-%m-%d"),
            "prompt_version": PROMPT_VERSION,
            "task_type": task.task_type,
            "session_type": task.session_type,
            "conversation_text": "",
            "target_page_content": "",
            "backlog_summary": "",
            "related_wiki_pages": "",
            "session_id": "",
            "message_count": "0",
            "source": "unknown",
            "input_spec_hash": "",
            "cognition_context_hash": "",
            "cognition_context_json": "{}",
            "gate_decision_id": "",
            "source_event_ids_json": "[]",
            "raw_completeness": "unknown",
            "artifact_catalog_json": "{}",
            "source_authority_catalog_json": "{}",
            "behavior_intent_context": "## 用户行为/意图输入信号（系统预判，供 LLM 校正）\n\n- none",
            "cognitive_profile_context": "## 用户认知画像 v2（供蒸馏判断消费）\n\n- none",
        }

        if task.session:
            context["session_id"] = task.session.id
            context["message_count"] = str(len(task.session.messages))
            context["source"] = task.session.agent_name
            input_contract = dict(task.session.input_contract or {})
            context["input_spec_hash"] = str(input_contract.get("input_spec_hash") or "")
            context["cognition_context_hash"] = str(
                input_contract.get("cognition_context_hash") or ""
            )
            context["cognition_context_json"] = json.dumps(
                input_contract.get("cognition_context") or {},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            context["gate_decision_id"] = str(input_contract.get("gate_decision_id") or "")
            event_ids = input_contract.get("source_event_ids") or []
            context["source_event_ids_json"] = json.dumps(
                [str(item) for item in event_ids if str(item)], ensure_ascii=False
            )
            context["raw_completeness"] = str(input_contract.get("raw_completeness") or "unknown")
            context["artifact_catalog_json"] = json.dumps(
                input_contract.get("artifact_catalog") or {},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            context["source_authority_catalog_json"] = json.dumps(
                input_contract.get("source_authority_catalog") or {},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            behavior_signal = infer_behavior_intent_signal(
                task.session.messages,
                session_id=task.session.id,
                source_agent=task.session.agent_name,
            )
            context["behavior_intent_context"] = format_behavior_intent_context(behavior_signal)
            # Distillation tasks do not currently carry a server-resolved
            # principal. Keep profile context empty instead of traversing the
            # former unscoped profile repository path.
            context["cognitive_profile_context"] = self._build_cognitive_profile_context()

            # 兼容 DistillationEngine 主链路：它已经用 build_session_text 格式化好完整对话文本，
            # 直接透传避免 ContentFormatter 二次清洗/截断导致格式破坏。
            if task.preformatted and len(task.session.messages) == 1:
                context["conversation_text"] = task.session.messages[0].get("content", "")
            else:
                context["conversation_text"] = self.content_formatter.format_session(
                    task.session,
                    keep_code=(task.task_type == "extract"),
                )

            related = self.context_retriever.retrieve(task.session)
            context["related_wiki_pages"] = self.context_retriever.format_for_prompt(related)

        if task.target_wiki_page:
            context["target_page_content"] = task.target_wiki_page.read_content()

        if task.backlog_items:
            context["backlog_summary"] = self._format_backlog(task.backlog_items)

        return context

    def _build_cognitive_profile_context(
        self,
    ) -> str:
        """Keep Distill disabled until its task carries a sealed read decision.

        ``DistillTask`` currently contains source/session metadata, not a
        server-resolved principal or an assertion-read authorization token.
        Those fields cannot be promoted into a user identity.  This method is
        intentionally body-free and has no private principal bypass.
        """

        return "## 用户认知画像 v2（供蒸馏判断消费）\n\n- none"

    def _format_backlog(self, items: List[DeferredRecord]) -> str:
        """格式化延迟蒸馏记录"""
        lines = [f"## 待合并的 {len(items)} 条记录", ""]
        for i, item in enumerate(items, 1):
            lines.append(f"### 记录 {i}（来源: {item.agent_name} {item.session_id[:8]}）")
            lines.append(item.content[:2000])
            lines.append("")
        return "\n".join(lines)


# ========== TokenBudgetManager ==========


class TokenBudgetManager:
    """Token 预算管理器 — 分配 + 截断（基于真实 tokenizer）"""

    def __init__(self, tokenizer=None):
        self.tokenizer = tokenizer or get_tokenizer()

    def apply(self, context: Dict[str, str], budget: TokenBudget) -> Dict[str, str]:
        """应用 Token 预算，返回截断后的 context"""
        result = dict(context)
        total = sum(self.tokenizer.estimate(v) for v in result.values())
        available = budget.available_for_input

        if total <= available:
            return result

        excess = total - available

        # Step A: 截断相关上下文（最低优先级）
        context_tokens = self.tokenizer.estimate(result.get("related_wiki_pages", ""))
        if context_tokens > budget.context_limit and excess > 0:
            result["related_wiki_pages"] = self._trim_related_context(
                result["related_wiki_pages"],
                budget.context_limit,
            )
            excess = sum(self.tokenizer.estimate(v) for v in result.values()) - available

        # Step B: 截断对话内容（保留头 30% + 尾 70%）
        if excess > 0:
            content_tokens = self.tokenizer.estimate(result.get("conversation_text", ""))
            if content_tokens > budget.content_limit:
                result["conversation_text"] = self._trim_conversation(
                    result["conversation_text"],
                    budget.content_limit,
                )
                excess = sum(self.tokenizer.estimate(v) for v in result.values()) - available

        # Step C: 极端情况，完全移除相关上下文
        if excess > 0:
            logger.warning("Token 预算严重不足，移除全部相关上下文")
            result["related_wiki_pages"] = ""

        return result

    def _trim_related_context(self, text: str, target_limit: int) -> str:
        """截断相关上下文：按页分割，从末尾移除低相关度页面"""
        if not text:
            return text
        pages = text.split("### ")
        if len(pages) <= 2:
            return text

        header = pages[0]
        page_sections = pages[1:]

        while page_sections and self.tokenizer.estimate(text) > target_limit:
            page_sections.pop()
            text = header + "### " + "### ".join(page_sections)

        return text

    def _trim_conversation(self, text: str, target_limit: int) -> str:
        """截断对话：保留头 30% + 尾 70%（基于真实 token 计数）"""
        if not text:
            return text
        current = self.tokenizer.estimate(text)
        if current <= target_limit:
            return text

        # 使用 tokenizer 的精确截断能力
        head_budget = int(target_limit * 0.3)
        tail_budget = target_limit - head_budget

        # 尝试在段落边界截断头/尾
        lines = text.split("\n\n")
        head_lines = []
        head_tokens = 0
        for line in lines:
            t = self.tokenizer.estimate(line)
            if head_tokens + t > head_budget:
                break
            head_lines.append(line)
            head_tokens += t

        tail_lines = []  # type: ignore[var-annotated]
        tail_tokens = 0
        for line in reversed(lines):
            t = self.tokenizer.estimate(line)
            if tail_tokens + t > tail_budget:
                break
            tail_lines.insert(0, line)
            tail_tokens += t

        # 避免头尾重叠
        head_text = "\n\n".join(head_lines)
        tail_text = "\n\n".join(tail_lines)
        if len(head_lines) + len(tail_lines) >= len(lines) or (not head_lines and not tail_lines):
            # 没有足够内容省略，直接截断
            # type: ignore[no-any-return]
            return self.tokenizer.truncate_to_tokens(text, target_limit)  # type: ignore[no-any-return]  # noqa: E501

        marker = "\n\n[... 对话中间部分已截断 ...]\n\n"
        return head_text + marker + tail_text


# ========== TemplateRegistry ==========


class TemplateRegistry:
    """模板注册表 — 文件系统模板，支持继承回退"""

    def __init__(self, template_dir: Path):
        self.template_dir = template_dir
        self._cache: Dict[str, str] = {}
        self._load_all()

    def _load_all(self) -> None:
        """递归加载所有 .md 模板"""
        if not self.template_dir.exists():
            logger.warning("模板目录不存在: %s", self.template_dir)
            return
        for md_file in self.template_dir.rglob("*.md"):
            rel = md_file.relative_to(self.template_dir)
            key = str(rel.with_suffix(""))
            try:
                self._cache[key] = md_file.read_text(encoding="utf-8")
            except (OSError, IOError) as e:
                logger.warning("加载模板失败 %s: %s", key, e)

    def select(self, task_type: str, session_type: str) -> str:
        """选择模板，优先级：{task_type}/{session_type} > {task_type}/base > _base"""
        candidates = [
            f"{task_type}/{session_type}",
            f"{task_type}/base",
            f"{task_type}",
            "_base",
        ]
        for key in candidates:
            if key in self._cache:
                return self._cache[key]

        raise FileNotFoundError(
            f"未找到模板: task_type={task_type}, session_type={session_type}. "
            f"已查找: {candidates}"
        )

    def render_schema(self, schema_name: str) -> str:
        """渲染 JSON Schema 为 Markdown"""
        schema_path = self.template_dir / "_output_schemas" / f"{schema_name}.json"
        if not schema_path.exists():
            return ""
        try:
            schema = self.load_json_schema(schema_path)
            return self._schema_to_markdown(schema)
        except (json.JSONDecodeError, ValueError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at prompt_builder.py", exc_info=True
            )
            return ""

    @staticmethod
    def load_json_schema(schema_path: Path) -> Dict[str, Any]:
        """Load one bundled output schema through the prompt-asset authority.

        Runtime validators and prompt rendering must deserialize the same file via
        this single asset-loading path.  Keeping the read here also prevents
        validators from growing their own prompt-file IO authority.
        """
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("output schema must be a JSON object")
        return loaded

    def _schema_to_markdown(self, schema: dict, indent: int = 0) -> str:
        """将 JSON Schema 转为保留组合与条件语义的 Markdown。

        这里不是一个只列出 ``properties`` 的字段清单。提取契约的
        ``oneOf``、``allOf`` 与 ``if/then/else`` 会决定 skip 是否可被正式接纳，
        因此必须在 Prompt 中把分支、必填字段和数组上下限完整呈现出来。
        """
        return "\n".join(self._render_schema_node(schema, indent))

    def _render_schema_node(self, schema: dict, indent: int) -> List[str]:
        """递归渲染一个 schema 节点，保留组合/条件约束。"""
        if not isinstance(schema, dict):
            return []

        lines: List[str] = []
        prefix = "  " * indent

        for keyword, label in (("oneOf", "分支"), ("anyOf", "可选分支")):
            variants = schema.get(keyword, [])
            if not isinstance(variants, list):
                continue
            for index, variant in enumerate(variants, start=1):
                if not isinstance(variant, dict):
                    continue
                title = str(variant.get("title") or f"{label} {index}")
                lines.append(f"{prefix}- **{label}：{title}**")
                lines.extend(self._render_schema_node(variant, indent + 1))

        all_of = schema.get("allOf", [])
        if isinstance(all_of, list):
            for index, clause in enumerate(all_of, start=1):
                if not isinstance(clause, dict):
                    continue
                condition = clause.get("if")
                if isinstance(condition, dict):
                    lines.append(f"{prefix}- **条件约束（allOf {index}）**")
                    lines.append(f"{prefix}  - **当 {self._condition_to_text(condition)} 时**")
                    lines.extend(
                        self._render_conditional_branch(clause.get("then"), indent + 2, "满足时")
                    )
                    else_label = self._else_branch_label(condition)
                    if isinstance(clause.get("else"), dict):
                        lines.append(f"{prefix}  - **{else_label}**")
                        lines.extend(
                            self._render_conditional_branch(
                                clause.get("else"), indent + 2, else_label
                            )
                        )
                else:
                    lines.append(f"{prefix}- **组合约束（allOf {index}）**")
                    lines.extend(self._render_schema_node(clause, indent + 1))

        properties = schema.get("properties", {})
        required = schema.get("required", [])
        required_names = {str(name) for name in required if isinstance(name, (str, int, float))}
        if isinstance(properties, dict):
            for name, prop in properties.items():
                if isinstance(prop, dict):
                    lines.extend(
                        self._render_schema_property(
                            str(name), prop, str(name) in required_names, indent
                        )
                    )

        return lines

    def _render_conditional_branch(self, schema: Any, indent: int, label: str) -> List[str]:
        """渲染 if 分支的必填集合及分支内细化约束。"""
        if not isinstance(schema, dict):
            return []

        lines: List[str] = []
        prefix = "  " * indent
        required = schema.get("required", [])
        required_names = [str(name) for name in required if isinstance(name, (str, int, float))]
        if required_names:
            fields = "、".join(f"`{name}`" for name in required_names)
            lines.append(f"{prefix}- **{label}必填字段**：{fields}")

        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            required_set = set(required_names)
            for name, prop in properties.items():
                if isinstance(prop, dict):
                    lines.extend(
                        self._render_schema_property(
                            str(name), prop, str(name) in required_set, indent
                        )
                    )
        return lines

    def _render_schema_property(
        self, name: str, schema: dict, required: bool, indent: int
    ) -> List[str]:
        """渲染单个字段，并继续渲染对象、数组与组合约束。"""
        prefix = "  " * indent
        type_name = self._schema_type_name(schema)
        constraints = self._schema_constraints(schema)
        signature = f"`{type_name}`"
        if constraints:
            signature = f"{signature}; {'；'.join(constraints)}"
        required_mark = " (必填)" if required else ""
        line = f"{prefix}- **{name}** ({signature}){required_mark}"
        description = schema.get("description")
        if isinstance(description, str) and description:
            line += f": {description}"

        lines = [line]
        if type_name == "array":
            items = schema.get("items")
            if isinstance(items, dict) and self._has_renderable_schema_content(items):
                lines.append(f"{prefix}  - **数组元素**")
                lines.extend(self._render_schema_node(items, indent + 2))
        if type_name == "object" or self._has_renderable_schema_content(schema):
            lines.extend(self._render_schema_node(schema, indent + 1))
        return lines

    @staticmethod
    def _has_renderable_schema_content(schema: dict) -> bool:
        return bool(
            schema.get("properties")
            or schema.get("oneOf")
            or schema.get("anyOf")
            or schema.get("allOf")
        )

    @staticmethod
    def _has_composition(schema: dict) -> bool:
        return bool(schema.get("oneOf") or schema.get("anyOf") or schema.get("allOf"))

    @staticmethod
    def _schema_type_name(schema: dict) -> str:
        """给 const/enum 缺失显式 type 的 schema 推断可读类型。"""
        schema_type = schema.get("type")
        if isinstance(schema_type, str):
            return schema_type
        if isinstance(schema_type, list):
            return " | ".join(str(item) for item in schema_type)
        values = [schema.get("const")] if "const" in schema else schema.get("enum", [])
        if isinstance(values, list) and values:
            if all(isinstance(value, str) for value in values):
                return "string"
            if all(isinstance(value, bool) for value in values):
                return "boolean"
            if all(isinstance(value, (int, float)) for value in values):
                return "number"
        return "any"

    @staticmethod
    def _schema_constraints(schema: dict) -> List[str]:
        """提取会影响输出合法性的常见 JSON Schema 约束。"""
        constraints: List[str] = []
        if "const" in schema:
            constraints.append(f"固定为 `{TemplateRegistry._schema_value(schema['const'])}`")
        elif isinstance(schema.get("enum"), list):
            values = "、".join(
                f"`{TemplateRegistry._schema_value(value)}`" for value in schema["enum"]
            )
            constraints.append(f"可选值：{values}")
        for key, label in (
            ("minItems", "最少项数"),
            ("maxItems", "最多项数"),
            ("minLength", "最小长度"),
            ("maxLength", "最大长度"),
            ("minimum", "最小值"),
            ("maximum", "最大值"),
        ):
            if key in schema:
                constraints.append(f"{label}：{schema[key]}")
        if isinstance(schema.get("pattern"), str):
            constraints.append(f"匹配模式：`{schema['pattern']}`")
        if isinstance(schema.get("format"), str):
            constraints.append(f"格式：`{schema['format']}`")
        return constraints

    @staticmethod
    def _schema_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _condition_to_text(self, schema: dict) -> str:
        """把常见的 if 条件转成能直接放进 Prompt 的语义文本。"""
        conditions: List[str] = []
        properties = schema.get("properties", {})
        property_names = set()
        if isinstance(properties, dict):
            for name, constraint in properties.items():
                if not isinstance(constraint, dict):
                    continue
                property_names.add(str(name))
                rendered = self._condition_constraint_to_text(str(name), constraint)
                if rendered:
                    conditions.append(rendered)

        required = schema.get("required", [])
        if isinstance(required, list) and required:
            additional_required = [name for name in required if str(name) not in property_names]
            if additional_required:
                fields = "、".join(f"`{name}`" for name in additional_required)
                conditions.append(f"字段 {fields} 存在")
        return "且".join(conditions) or "该条件成立"

    def _condition_constraint_to_text(self, name: str, constraint: dict) -> str:
        """Render nested ``if.properties`` constraints instead of hiding them.

        Claim policies commonly key off ``relation_to_existing.type``.  A
        shallow renderer would display only “the condition holds”, which
        silently removes the most important branch cue from the model prompt.
        """
        if "const" in constraint:
            return f"`{name}` 固定为 `{self._schema_value(constraint['const'])}`"
        if isinstance(constraint.get("enum"), list):
            options = "、".join(f"`{self._schema_value(value)}`" for value in constraint["enum"])
            return f"`{name}` 为以下之一：{options}"
        if isinstance(constraint.get("not"), dict) and "const" in constraint["not"]:
            return f"`{name}` 不得为 `{self._schema_value(constraint['not']['const'])}`"
        children = constraint.get("properties")
        if isinstance(children, dict):
            rendered_children = [
                self._condition_constraint_to_text(f"{name}.{child_name}", child_constraint)
                for child_name, child_constraint in children.items()
                if isinstance(child_constraint, dict)
            ]
            rendered_children = [item for item in rendered_children if item]
            if rendered_children:
                return "且".join(rendered_children)
        return ""

    def _else_branch_label(self, condition: dict) -> str:
        """为最常见的 skip 条件提供明确的非 skip 文案。"""
        properties = condition.get("properties", {})
        if isinstance(properties, dict):
            intent = properties.get("distill_intent")
            if isinstance(intent, dict) and intent.get("const") == "skip":
                return "否则（非 skip）"
        return "否则"


# ========== PromptBuilder ==========


class PromptBuilder:
    """Prompt 构造器 — 选择模板 → 组装上下文 → 预算控制 → 渲染 → 验证"""

    def __init__(
        self, template_dir: Path | None = None, wiki_dir: Path | None = None, tokenizer=None
    ):
        config = get_config()
        self.template_registry = TemplateRegistry(
            template_dir or Path(__file__).parent.parent.parent / "prompts" / "distill",
        )
        self.context_assembler = ContextAssembler(
            wiki_dir or config.wiki_dir,
        )
        self.token_budget = TokenBudgetManager(tokenizer)

    def build(self, task: DistillTask) -> str:
        """完整流水线：选模板 → 组装上下文 → 预算控制 → 渲染 → 验证"""
        # 1. 选择模板
        template = self.template_registry.select(task.task_type, task.session_type)

        # 2. 组装上下文变量
        context = self.context_assembler.assemble(task)

        # 3. 应用 Token 预算
        context = self.token_budget.apply(context, task.budget_config)

        # 4. 注入输出 Schema（如果有）
        schema = self.template_registry.render_schema(task.task_type)
        if schema:
            context["output_schema"] = schema

        # 5. 渲染模板
        prompt = self._render(template, context)

        # 6. 验证输出格式
        self._validate_output_format(prompt, task.task_type)

        return prompt

    def _render(self, template: str, context: Dict[str, str]) -> str:
        """渲染模板，替换 {variable} 占位符"""
        result = template
        for key, value in context.items():
            result = result.replace(f"{{{key}}}", value)

        # 清理未替换的占位符
        result = re.sub(r"\{[a-z_]+\}", "", result)
        return result

    def _validate_output_format(self, prompt: str, task_type: str):
        """验证 Prompt 包含必要的输出格式指示"""
        if task_type in ("value_judge", "extract", "incremental", "merge"):
            if "JSON" not in prompt.upper() and "json" not in prompt:
                logger.warning("Prompt for %s 缺少 JSON 输出格式指示", task_type)


# ========== 便捷函数 ==========


def build_distill_prompt(
    session_id: str,
    messages: List[Dict],
    task_type: str = "extract",
    session_type: str = "general",
    wiki_dir: Path | None = None,
    budget_config: TokenBudget | None = None,
) -> str:
    """便捷函数：构建蒸馏 Prompt"""
    session = Session(id=session_id, messages=messages)
    task = DistillTask(
        task_type=task_type,
        session=session,
        session_type=session_type,
        budget_config=budget_config or TokenBudget.from_config(),
    )
    builder = PromptBuilder(wiki_dir=wiki_dir)
    return builder.build(task)
