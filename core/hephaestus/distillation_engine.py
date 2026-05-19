"""
Distillation Engine - LLM 直接蒸馏原始对话为结构化 wiki 页面

职责：
- 从 Memos 会话中读取原始对话
- 调用 LLM 进行阶段1（价值判断）+ 阶段2（知识提取）
- 解析 JSON 输出，生成 wiki 页面
- 写入 Obsidian 目录

设计原则：
- LLM 调用可插拔（支持 OpenAI/Anthropic/本地模型）
- 失败隔离：单个 session 失败不影响其他 session
- JSON 解析容错：LLM 输出不合法时尝试修复
"""

import os
import sys
import json
import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

# 导入 prompts
from .distillation_prompts import DISTILLATION_PROMPT, RECONFIRM_PROMPT

# 配置系统
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.config import get_config


def _get_wiki_dir() -> Path:
    """Lazy-load wiki directory from config"""
    return get_config().wiki_dir


def _get_inbox_dir() -> Path:
    """Lazy-load inbox directory from config"""
    return _get_wiki_dir() / "00-Inbox"


# ========== 数据模型 ==========

@dataclass
class KnowledgeFragment:
    """知识片段"""
    form: str
    title: str
    frontmatter: Dict[str, Any]
    background: str
    core_content: str
    boundaries: Dict[str, str]
    anti_patterns: List[str]
    related_concepts: List[str]


@dataclass
class DistillationResult:
    """蒸馏结果"""
    session_id: str
    judgment: str = "skip"  # knowledge / skill / skip
    judgment_reason: str = ""
    skill_suggestion: str = ""
    analysis_type: str = "standard"  # standard / data_distillation
    data_profile: Optional[Dict] = None
    anomalies: List[Dict] = field(default_factory=list)
    fragments: List[KnowledgeFragment] = field(default_factory=list)
    raw_response: str = ""
    error: str = ""
    # 二次确认相关字段
    needs_reconfirm: bool = False
    reconfirm_question: str = ""


# ========== LLM 调用接口 ==========

class LLMProvider:
    """LLM 调用抽象基类"""

    def call(self, prompt: str, max_tokens: int = 8000) -> str:
        raise NotImplementedError


class AgentDelegateProvider(LLMProvider):
    """
    【同源复用】委托本地 Agent 执行蒸馏

    将蒸馏任务委托给用户本地的 AI Agent。
    委托方式：通过 AgentDelegate 写入任务文件，由 Agent 后台处理。
    """

    def __init__(self, timeout: int = 300):
        self.timeout = timeout

    def call(self, prompt: str, max_tokens: int = 8000) -> str:
        from core.prometheus_fire import AgentDelegate, DistillTask

        delegate = AgentDelegate()
        task = DistillTask(
            session_id=f"distill-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            messages=[{"role": "user", "content": prompt}],
            meta={
                "source": "distillation-engine",
                "task_type": "knowledge_distill",
                "full_prompt": prompt,
            },
        )
        output_path = Path.home() / ".mnemos" / "distill_output" / f"{task.session_id}.md"
        ok = delegate.delegate(task, output_path)
        if not ok:
            raise RuntimeError("无可用 Agent 执行蒸馏任务")

        result = delegate.wait_for_result(output_path, timeout=self.timeout)
        if not result:
            raise RuntimeError("蒸馏任务超时或 Agent 未返回结果")
        return result


def create_llm_provider(provider_type: str = None, **kwargs) -> LLMProvider:
    """[已废弃] 创建 LLM 提供者

    ⚠️ 警告：此函数已废弃。Mnemos 遵循同源复用原则，不直接调用任何 LLM API。
    所有蒸馏任务应通过 core.hephaestus_worker.HephaestusWorker 异步委托给宿主 Agent。

    保留此函数仅作兼容性提示，调用将抛出 RuntimeError。
    """
    raise RuntimeError(
        "create_llm_provider is deprecated. "
        "Use HephaestusWorker to delegate distillation tasks asynchronously. "
        "See core/hephaestus_worker.py:process_all() for the correct entry point."
    )


# ========== 内容清洗 ==========

def clean_message_content(content: str) -> str:
    """清理消息内容"""
    if not content:
        return ""

    # 移除 thinking 块
    content = re.sub(r'\[thinking\].*?(?:\[/thinking\]|$)', '', content, flags=re.DOTALL)

    # 移除代码块
    content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)

    # 移除单行 shell 命令
    content = re.sub(
        r'^(curl|chmod|wget|npm|pip|pip3|docker|git|mkdir|cd|ls|cat|rm|mv|cp)\s+.+$',
        '', content, flags=re.MULTILINE
    )

    # 移除纯数字/编号行
    content = re.sub(r'^\s*\d+\.\s*$', '', content, flags=re.MULTILINE)

    # 清理多余空行
    content = re.sub(r'\n{3,}', '\n\n', content)

    return content.strip()


def build_session_text(messages: List[Dict]) -> str:
    """从消息列表构建对话文本"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "").strip()
        if not content:
            continue
        content = clean_message_content(content)
        if not content or len(content) < 10:
            continue
        # 截断超长消息
        if len(content) > 1000:
            content = content[:1000] + "...(truncated)"
        lines.append(f"[{role}] {content}")

    full_text = "\n\n".join(lines)

    # 如果总长度超过 12000，截断到 12000
    if len(full_text) > 12000:
        full_text = full_text[:12000] + "\n\n...(session truncated)"

    return full_text


# ========== JSON 解析容错 ==========

def extract_json(text: str) -> Optional[Dict]:
    """从文本中提取 JSON，带容错处理"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 markdown 代码块中的 JSON
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试提取最外层的大括号内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # 尝试修复常见的 JSON 错误
    # 1. 尾部多余的逗号
    fixed = re.sub(r',(\s*[}\]])', r'\1', text)
    # 2. 单引号替换为双引号（简单处理）
    fixed = fixed.replace("'", '"')
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    return None


# ========== Wiki 页面生成 ==========

def generate_wiki_page(fragment: KnowledgeFragment, session_id: str,
                       source: str = "") -> str:
    """生成 wiki 页面 Markdown"""

    fm = fragment.frontmatter

    # 构建 frontmatter
    frontmatter_lines = ["---"]
    frontmatter_lines.append(f"类型: {fm.get('类型', '未知')}")
    frontmatter_lines.append(f"领域: {fm.get('领域', '其他')}")

    roles = fm.get("适用角色", [])
    if roles:
        frontmatter_lines.append(f"适用角色: {json.dumps(roles, ensure_ascii=False)}")

    scenes = fm.get("触发场景", [])
    if scenes:
        frontmatter_lines.append(f"触发场景: {json.dumps(scenes, ensure_ascii=False)}")

    frontmatter_lines.append(f"复杂度: {fm.get('复杂度', '入门')}")
    frontmatter_lines.append(f"置信度: {fm.get('置信度', 0.5)}")
    frontmatter_lines.append(f"证据级别: {fm.get('证据级别', '单源')}")
    frontmatter_lines.append(f"时效性: {fm.get('时效性', '上下文相关')}")

    if fm.get("版本标记"):
        frontmatter_lines.append(f"版本标记: {fm['版本标记']}")

    frontmatter_lines.append(f"情感倾向: {fm.get('情感倾向', '中性')}")
    frontmatter_lines.append(f"创建日期: {fm.get('创建日期', datetime.now().strftime('%Y-%m-%d'))}")
    frontmatter_lines.append(f"来源会话: {session_id[:8]}")

    # 关键词
    keywords = fm.get("关键词", {})
    if keywords:
        frontmatter_lines.append("关键词:")
        for layer, words in keywords.items():
            if words:
                frontmatter_lines.append(f"  {layer}: {json.dumps(words, ensure_ascii=False)}")

    frontmatter_lines.append("---")

    # 构建 body
    body_lines = [f"# {fragment.title}", ""]

    if fragment.background:
        body_lines.append("## 背景")
        body_lines.append("")
        body_lines.append(fragment.background)
        body_lines.append("")

    if fragment.core_content:
        body_lines.append("## 核心内容")
        body_lines.append("")
        body_lines.append(fragment.core_content)
        body_lines.append("")

    if fragment.boundaries:
        body_lines.append("### 适用边界")
        body_lines.append("")
        if fragment.boundaries.get("applies"):
            body_lines.append(f"- 适用于：{fragment.boundaries['applies']}")
        if fragment.boundaries.get("not_applies"):
            body_lines.append(f"- 不适用于：{fragment.boundaries['not_applies']}")
        body_lines.append("")

    if fragment.anti_patterns:
        body_lines.append("### 反模式/注意事项")
        body_lines.append("")
        for ap in fragment.anti_patterns:
            body_lines.append(f"- {ap}")
        body_lines.append("")

    # 演化历史
    body_lines.append("## 演化历史")
    body_lines.append("")
    body_lines.append(f"- v1: 初始记录（{datetime.now().strftime('%Y-%m-%d')}）")
    body_lines.append("")

    # 相关链接
    if fragment.related_concepts:
        body_lines.append("## 相关链接")
        body_lines.append("")
        for concept in fragment.related_concepts:
            body_lines.append(f"- [[{concept}]]")
        body_lines.append("")

    return "\n".join(frontmatter_lines + [""] + body_lines)


# ========== 蒸馏引擎 ==========

class DistillationEngine:
    """知识蒸馏引擎"""

    def __init__(self, llm_provider: LLMProvider = None, wiki_base: str = None):
        # [已废弃] DistillationEngine 不再直接调用 LLM
        # 同源复用：所有蒸馏任务通过 HephaestusWorker 异步委托给宿主 Agent
        if llm_provider is not None:
            raise RuntimeError(
                "DistillationEngine no longer accepts llm_provider. "
                "Use HephaestusWorker to delegate distillation tasks."
            )
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else _get_wiki_dir()
        self.inbox_dir = self.wiki_base / "00-Inbox"

    def process_session(self, session_id: str, messages: List[Dict],
                        meta: Dict = None) -> DistillationResult:
        """
        [已废弃] 处理单个 session

        ⚠️ 此方法已废弃。请使用 HephaestusWorker 异步委托蒸馏任务。
        """
        raise RuntimeError(
            "DistillationEngine.process_session is deprecated. "
            "Use HephaestusWorker.process_all() or core.kia.amphora.enqueue() instead."
        )

    def _parse_fragments(self, data: Dict) -> List[KnowledgeFragment]:
        """从解析后的 JSON 数据中提取知识片段（供 HephaestusWorker 复用）"""
        fragments = []
        for frag_data in data.get("fragments", []):
            try:
                fragment = KnowledgeFragment(
                    form=frag_data.get("form", "未知"),
                    title=frag_data.get("title", "无标题"),
                    frontmatter=frag_data.get("frontmatter", {}),
                    background=frag_data.get("background", ""),
                    core_content=frag_data.get("core_content", ""),
                    boundaries=frag_data.get("boundaries", {}),
                    anti_patterns=frag_data.get("anti_patterns", []),
                    related_concepts=frag_data.get("related_concepts", []),
                )
                fragments.append(fragment)
            except Exception as e:
                # 跳过解析失败的 fragment
                continue

        return fragments

    def _generate_reconfirm_question(self, result: DistillationResult,
                                      session_text: str) -> str:
        """生成二次确认的询问文案"""
        reason = result.judgment_reason
        preview = session_text[:500] + "..." if len(session_text) > 500 else session_text

        question = f"""## ⚠️ 这段内容被评估为「无需入库」

**跳过理由**：{reason}

**内容预览**：
```
{preview}
```

---

**如果你认为这段内容有价值，请告诉我：**

1. **你为什么要保存这个？**（比如："这是我和团队的对齐记录"、"我要追踪这个系统的变化"、"这是我反复参考的检查清单"）
2. **你希望它以后怎么被使用？**（比如："快速检索"、"AI 帮我分析规律"、"对比不同版本的变化"、"整理成可复用流程"）

**或者**：直接回复 "确认跳过"，我会丢弃这段内容并记录原因。
"""
        return question

    def reconfirm_skip(self, session_id: str, messages: List[Dict],
                       original_result: DistillationResult,
                       user_intent: str, expected_output: str = "") -> DistillationResult:
        """
        基于用户意图重新评估被跳过的内容

        Args:
            session_id: 会话 ID
            messages: 原始消息列表
            original_result: 第一次蒸馏结果
            user_intent: 用户补充的意图说明
            expected_output: 用户期望的输出效果

        Returns:
            重新评估后的 DistillationResult
        """
        result = DistillationResult(session_id=session_id)

        # 1. 构建对话文本
        session_text = build_session_text(messages)
        if not session_text:
            result.judgment = "skip"
            result.judgment_reason = "清洗后无有效内容"
            return result

        # 2. 构建二次确认 prompt
        prompt = RECONFIRM_PROMPT
        prompt = prompt.replace("{original_reason}", original_result.judgment_reason)
        prompt = prompt.replace("{session_content}", session_text)
        prompt = prompt.replace("{user_intent}", user_intent)
        prompt = prompt.replace("{expected_output}", expected_output or "用户未明确说明期望输出")

        # 3. 调用 LLM
        try:
            if self.llm is None:
                result.error = "LLM provider not configured"
                return result

            response = self.llm.call(prompt, max_tokens=8000)
            result.raw_response = response
        except Exception as e:
            result.error = f"LLM reconfirm call failed: {e}"
            return result

        # 4. 解析 JSON
        data = extract_json(response)
        if data is None:
            result.error = "Failed to parse JSON from reconfirm response"
            return result

        # 5. 填充重新评估结果
        result.judgment = data.get("rejudgment", "skip")
        result.judgment_reason = data.get("rejudgment_reason", "")

        # 6. 如果仍判 skip，保留详细分析
        if result.judgment == "skip":
            result.judgment_reason = data.get("why_still_skip", result.judgment_reason)
            result.analysis_type = "reconfirmed_skip"
            return result

        # 7. 如果判为 knowledge，解析 fragments
        for frag_data in data.get("fragments", []):
            try:
                # 将用户意图写入 background
                background = frag_data.get("background", "")
                if user_intent and "用户意图" not in background:
                    background = f"**用户意图**：{user_intent}\n\n{background}"

                # 标记为基于用户意图的条目
                frontmatter = frag_data.get("frontmatter", {})
                frontmatter["来源类型"] = "user_intent_guided"
                frontmatter["原始判断"] = "skip"
                frontmatter["重新评估理由"] = data.get("analysis", "")
                # 基于用户意图的条目，置信度适当降低
                frontmatter["置信度"] = min(frontmatter.get("置信度", 0.6), 0.7)

                fragment = KnowledgeFragment(
                    form=frag_data.get("form", "用户意图驱动"),
                    title=frag_data.get("title", "无标题"),
                    frontmatter=frontmatter,
                    background=background,
                    core_content=frag_data.get("core_content", ""),
                    boundaries=frag_data.get("boundaries", {}),
                    anti_patterns=frag_data.get("anti_patterns", []),
                    related_concepts=frag_data.get("related_concepts", []),
                )
                result.fragments.append(fragment)
            except Exception:
                continue

        return result

    def write_pages(self, result: DistillationResult) -> List[str]:
        """
        将蒸馏结果写入 wiki 页面

        Returns:
            写入的文件路径列表
        """
        written = []

        if result.judgment != "knowledge" or not result.fragments:
            return written

        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        for i, fragment in enumerate(result.fragments):
            # 生成页面 ID
            page_id = f"{result.session_id[:8]}_{fragment.form}_{i + 1}"

            # 生成页面内容
            page_content = generate_wiki_page(
                fragment, result.session_id
            )

            # 写入文件
            file_path = self.inbox_dir / f"{page_id}.md"
            try:
                file_path.write_text(page_content, encoding="utf-8")
                written.append(str(file_path))
            except Exception as e:
                continue

        return written


# ========== 便捷函数 ==========

def distill_session(session_id: str, messages: List[Dict],
                    llm_provider: LLMProvider = None,
                    wiki_base: str = None) -> DistillationResult:
    """[已废弃] 便捷函数：蒸馏单个 session

    ⚠️ 警告：此函数已废弃。请使用 core.hephaestus_worker.HephaestusWorker 异步委托。
    """
    raise RuntimeError(
        "distill_session is deprecated. "
        "Use HephaestusWorker.process_all() or enqueue() to delegate distillation."
    )


def distill_and_write(session_id: str, messages: List[Dict],
                      llm_provider: LLMProvider = None,
                      wiki_base: str = None) -> Tuple[DistillationResult, List[str]]:
    """[已废弃] 便捷函数：蒸馏并写入 wiki

    ⚠️ 警告：此函数已废弃。请使用 core.hephaestus_worker.HephaestusWorker 异步委托。
    """
    raise RuntimeError(
        "distill_and_write is deprecated. "
        "Use HephaestusWorker.process_all() or enqueue() to delegate distillation."
    )


if __name__ == "__main__":
    # 测试
    print("Distillation Engine loaded.")
    print(f"Wiki base: {_get_wiki_dir()}")
    print(f"Prompt length: {len(DISTILLATION_PROMPT)} chars")
