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
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config import get_config

logger = logging.getLogger(__name__)


# ========== 数据模型 ==========

@dataclass
class TokenBudget:
    """Token 预算配置"""
    total_limit: int = 16000
    system_pct: float = 0.10
    context_pct: float = 0.25
    content_pct: float = 0.55
    output_reserve: int = 2000

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
class WikiPage:
    """Wiki 页面引用"""
    path: Path
    title: str
    content: str = ""

    def read_content(self) -> str:
        if self.content:
            return self.content
        try:
            return self.path.read_text(encoding="utf-8")
        except Exception:
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

    @property
    def content(self) -> str:
        return "\n".join(m.get("content", "") for m in self.messages)


@dataclass
class DistillTask:
    """蒸馏任务定义 — PromptBuilder 的唯一输入"""
    task_type: str           # value_judge | extract | incremental | backlink | merge
    session: Optional[Session] = None
    session_type: str = "general"  # coding | marketing | analysis | strategy | writing | review | general
    target_wiki_page: Optional[WikiPage] = None
    related_pages: List[WikiPage] = field(default_factory=list)
    backlog_items: List[DeferredRecord] = field(default_factory=list)
    budget_config: TokenBudget = field(default_factory=lambda: TokenBudget())


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
        (r'\[thinking\].*?\[/thinking\]', '', re.DOTALL),
        (r'(?:让我试一下|现在修改|我来测试|好的，我)[^\n]*\n?', '', 0),
        (r'(?:帮我|能否|怎么|请|麻烦你)[^?\n]*[?？]\s*', '', 0),
    ]

    def format_session(self, session: Session, max_chars: int = 8000,
                       keep_code: bool = False) -> str:
        """格式化会话内容"""
        sections = []
        for i, msg in enumerate(session.messages, 1):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            content = self._clean(content, keep_code=keep_code)
            if not content.strip():
                continue
            sections.append(f"### Message {i} ({role})\n\n{content}")

        full = "\n---\n\n".join(sections)

        if len(full) > max_chars:
            head_len = int(max_chars * 0.3)
            tail_len = int(max_chars * 0.7) - 100
            marker = f"\n\n[... {len(full) - head_len - tail_len} 字符已截断 ...]\n\n"
            full = full[:head_len] + marker + full[-tail_len:]

        return full

    def _clean(self, content: str, keep_code: bool = False) -> str:
        for pattern, replacement, flags in self.CLEANING_RULES:
            content = re.sub(pattern, replacement, content, flags=flags)
        if not keep_code:
            content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content = re.sub(r'\n{3,}', '\n\n', content)
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

        merged = {}
        for rp in by_entity + by_jaccard:
            key = str(rp.page_path)
            if key not in merged or rp.relevance > merged[key].relevance:
                merged[key] = rp

        results = sorted(merged.values(), key=lambda r: r.relevance, reverse=True)
        return results[:self.MAX_RELATED_PAGES]

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
            results.append(RelatedPage(
                page_path=md_file,
                title=fm.get("title", md_file.stem),
                summary=self._extract_summary(md_file),
                relevance=relevance,
                match_type="entity",
            ))
        return results

    def _find_by_jaccard(self, content: str) -> List[RelatedPage]:
        """Jaccard 关键词重叠检索"""
        content_kw = self._extract_keywords(content)
        if not content_kw:
            return []

        results = []
        for md_file in self._scan_wiki_pages():
            fm = self._read_frontmatter(md_file)
            page_kw = set()
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
                results.append(RelatedPage(
                    page_path=md_file,
                    title=fm.get("title", md_file.stem) if fm else md_file.stem,
                    summary=self._extract_summary(md_file),
                    relevance=jaccard,
                    match_type="jaccard",
                ))
        return results

    def _scan_wiki_pages(self) -> List[Path]:
        """扫描 Wiki 目录下所有 .md 文件"""
        pages = []
        if not self.wiki_dir.exists():
            return pages
        for subdir in ["00-Inbox", "01-Projects", "02-Areas", "03-Tech", "04-Concepts", "05-MOCs"]:
            d = self.wiki_dir / subdir
            if d.exists():
                pages.extend(d.glob("*.md"))
        return pages

    @staticmethod
    def _read_frontmatter(md_file: Path) -> Optional[Dict]:
        """解析 Markdown 文件的 YAML frontmatter"""
        try:
            text = md_file.read_text(encoding="utf-8")[:2000]
            if not text.startswith("---"):
                return None
            end = text.find("---", 3)
            if end == -1:
                return None
            fm_text = text[3:end].strip()
            fm = {}
            for line in fm_text.split("\n"):
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if val.startswith("["):
                        try:
                            val = json.loads(val)
                        except json.JSONDecodeError:
                            pass
                    fm[key] = val
            return fm
        except Exception:
            return None

    @staticmethod
    def _extract_summary(md_file: Path) -> str:
        """提取页面摘要（前 300 字，跳过 frontmatter）"""
        try:
            text = md_file.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    text = text[end + 3:]
            return text.strip()[:300]
        except Exception:
            return ""

    @staticmethod
    def _extract_entities(text: str) -> set:
        from core.hephaestus.distillation_engine import build_session_text
        words = set()
        words.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        words.update(re.findall(r'[一-龥]{2,6}', text))
        return words

    @staticmethod
    def _extract_keywords(text: str) -> set:
        kw = set()
        kw.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        kw.update(re.findall(r'[一-龥]{2,4}', text))
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
            "task_type": task.task_type,
            "session_type": task.session_type,
            "conversation_text": "",
            "target_page_content": "",
            "backlog_summary": "",
            "related_wiki_pages": "",
            "session_id": "",
            "message_count": "0",
            "source": "unknown",
        }

        if task.session:
            context["conversation_text"] = self.content_formatter.format_session(
                task.session,
                keep_code=(task.task_type == "extract"),
            )
            context["session_id"] = task.session.id
            context["message_count"] = str(len(task.session.messages))
            context["source"] = task.session.agent_name

            related = self.context_retriever.retrieve(task.session)
            context["related_wiki_pages"] = self.context_retriever.format_for_prompt(related)

        if task.target_wiki_page:
            context["target_page_content"] = task.target_wiki_page.read_content()

        if task.backlog_items:
            context["backlog_summary"] = self._format_backlog(task.backlog_items)

        return context

    def _format_backlog(self, items: List[DeferredRecord]) -> str:
        """格式化延迟蒸馏记录"""
        lines = [f"## 待合并的 {len(items)} 条记录", ""]
        for i, item in enumerate(items, 1):
            lines.append(f"### 记录 {i}（来源: {item.agent_name} {item.session_id[:8]}）")
            lines.append(item.content[:2000])
            lines.append("")
        return "\n".join(lines)


# ========== TokenBudgetManager ==========

def _default_tokenizer(text: str) -> int:
    """简单 Token 估算：中文 ~1.5 token/字，英文 ~0.25 token/word"""
    chinese = len(re.findall(r'[一-龥]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    return int(chinese * 1.5 + english_words * 1.3)


class TokenBudgetManager:
    """Token 预算管理器 — 分配 + 截断"""

    def __init__(self, tokenizer: Callable[[str], int] = None):
        self.tokenizer = tokenizer or _default_tokenizer

    def apply(self, context: Dict[str, str], budget: TokenBudget) -> Dict[str, str]:
        """应用 Token 预算，返回截断后的 context"""
        result = dict(context)
        total = sum(self.tokenizer(v) for v in result.values())
        available = budget.available_for_input

        if total <= available:
            return result

        excess = total - available

        # Step A: 截断相关上下文（最低优先级）
        context_tokens = self.tokenizer(result.get("related_wiki_pages", ""))
        if context_tokens > budget.context_limit and excess > 0:
            result["related_wiki_pages"] = self._trim_related_context(
                result["related_wiki_pages"], budget.context_limit, excess,
            )
            excess = sum(self.tokenizer(v) for v in result.values()) - available

        # Step B: 截断对话内容（保留头 30% + 尾 70%）
        if excess > 0:
            content_tokens = self.tokenizer(result.get("conversation_text", ""))
            if content_tokens > budget.content_limit:
                result["conversation_text"] = self._trim_conversation(
                    result["conversation_text"], budget.content_limit, excess,
                )
                excess = sum(self.tokenizer(v) for v in result.values()) - available

        # Step C: 极端情况，完全移除相关上下文
        if excess > 0:
            logger.warning("Token 预算严重不足，移除全部相关上下文")
            result["related_wiki_pages"] = ""

        return result

    def _trim_related_context(self, text: str, target_limit: int,
                              excess: int) -> str:
        """截断相关上下文：按页分割，从末尾移除低相关度页面"""
        if not text:
            return text
        pages = text.split("### ")
        if len(pages) <= 2:
            return text

        # 第一段是标题，保留；后续每段是一个页面
        header = pages[0]
        page_sections = pages[1:]

        while page_sections and self.tokenizer(text) > target_limit:
            page_sections.pop()
            text = header + "### " + "### ".join(page_sections)

        return text

    def _trim_conversation(self, text: str, target_limit: int,
                           excess: int) -> str:
        """截断对话：保留头 30% + 尾 70%"""
        if not text:
            return text
        current = self.tokenizer(text)
        if current <= target_limit:
            return text

        keep_ratio = target_limit / current
        head_ratio = 0.3 * keep_ratio
        tail_ratio = 0.7 * keep_ratio

        # 按字符比例截断（近似 token 比例）
        head_chars = int(len(text) * head_ratio)
        tail_chars = int(len(text) * tail_ratio)
        marker = "\n\n[... 对话中间部分已截断 ...]\n\n"
        return text[:head_chars] + marker + text[-tail_chars:]


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
            logger.warning(f"模板目录不存在: {self.template_dir}")
            return
        for md_file in self.template_dir.rglob("*.md"):
            rel = md_file.relative_to(self.template_dir)
            key = str(rel.with_suffix(""))
            try:
                self._cache[key] = md_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning(f"加载模板失败 {key}: {e}")

    def select(self, task_type: str, session_type: str) -> str:
        """选择模板，优先级：{task_type}/{session_type} > {task_type}/base > _base"""
        candidates = [
            f"{task_type}/{session_type}",
            f"{task_type}/base",
            "_base",
        ]
        for key in candidates:
            if key in self._cache:
                return self._cache[key]

        # 回退：使用 distillation_prompts 中的硬编码 prompt
        return self._fallback_prompt(task_type)

    def render_schema(self, schema_name: str) -> str:
        """渲染 JSON Schema 为 Markdown"""
        schema_path = self.template_dir / "_output_schemas" / f"{schema_name}.json"
        if not schema_path.exists():
            return ""
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            return self._schema_to_markdown(schema)
        except Exception:
            return ""

    def _schema_to_markdown(self, schema: dict, indent: int = 0) -> str:
        """将 JSON Schema 转为人类可读 Markdown"""
        lines = []
        prefix = "  " * indent
        props = schema.get("properties", {})
        for name, prop in props.items():
            ptype = prop.get("type", "any")
            desc = prop.get("description", "")
            required = name in schema.get("required", [])
            req_mark = " (必填)" if required else ""
            lines.append(f"{prefix}- **{name}** (`{ptype}`){req_mark}: {desc}")
            if ptype == "object" and "properties" in prop:
                lines.append(self._schema_to_markdown(prop, indent + 1))
            elif ptype == "array" and "items" in prop:
                items = prop["items"]
                if isinstance(items, dict) and "properties" in items:
                    lines.append(self._schema_to_markdown(items, indent + 1))
        return "\n".join(lines)

    def _fallback_prompt(self, task_type: str) -> str:
        """无文件模板时的硬编码回退"""
        try:
            from .distillation_prompts import DISTILLATION_PROMPT, STAGE1_FILTER_PROMPT
            if task_type == "value_judge":
                return STAGE1_FILTER_PROMPT
            return DISTILLATION_PROMPT
        except Exception:
            return "请分析以下内容并提取知识：\n\n{conversation_text}"


# ========== PromptBuilder ==========

class PromptBuilder:
    """Prompt 构造器 — 选择模板 → 组装上下文 → 预算控制 → 渲染 → 验证"""

    def __init__(self, template_dir: Path = None, wiki_dir: Path = None,
                 tokenizer: Callable[[str], int] = None):
        config = get_config()
        self.template_registry = TemplateRegistry(
            template_dir or Path(__file__).parent.parent.parent / "prompts" / "distill",
        )
        self.context_assembler = ContextAssembler(
            wiki_dir or config.wiki_dir,
        )
        self.token_budget = TokenBudgetManager(tokenizer or _default_tokenizer)

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
        result = re.sub(r'\{[a-z_]+\}', '', result)
        return result

    def _validate_output_format(self, prompt: str, task_type: str):
        """验证 Prompt 包含必要的输出格式指示"""
        if task_type in ("value_judge", "extract", "incremental", "merge"):
            if "JSON" not in prompt.upper() and "json" not in prompt:
                logger.warning(f"Prompt for {task_type} 缺少 JSON 输出格式指示")


# ========== 便捷函数 ==========

def build_distill_prompt(session_id: str, messages: List[Dict],
                         task_type: str = "extract",
                         session_type: str = "general",
                         wiki_dir: Path = None) -> str:
    """便捷函数：构建蒸馏 Prompt"""
    session = Session(id=session_id, messages=messages)
    task = DistillTask(task_type=task_type, session=session, session_type=session_type)
    builder = PromptBuilder(wiki_dir=wiki_dir)
    return builder.build(task)
