# -*- coding: utf-8 -*-
"""
DistillationEngine — 七层蒸馏流水线

将原始 AI 对话提炼为结构化 Wiki 知识页面的核心引擎。

七层架构：
  L1 噪音过滤   — 规则级，<1ms，复用 ingest_helpers.is_noise_message()
  L2 价值预判   — 规则 + 贝叶斯，CERTAINLY_YES / CERTAINLY_NO / MAYBE
  L3 LLM判断    — 宿主Agent调用，knowledge / skill / skip
  L4 知识提取   — LLM + assertion_extractor 验证，6种知识形态
  L5 自检       — 断言验证 / 代码语法 / 链接有效性 / 时间范围
  L6 跨Agent关联 — Jaccard关键词重叠，自动注入 [[反向链接]]
  L7 反馈循环   — AdaptiveScorer + 用户画像信号驱动

同源复用原则：Mnemos 不直接调用 LLM API，所有 LLM 工作委托给宿主 Agent。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from math import log1p
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config
from core.frontmatter import to_chinese_frontmatter, fm_get
from core.kia.ingest_helpers import is_noise_message



logger = logging.getLogger(__name__)
def _get_wiki_dir() -> Path:
    return get_config().wiki_dir


def _get_wiki_db() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


# ========== 数据模型 ==========

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
    # 结构化关联（ADR-019：关联上下文用于语义桥接）
    relations: List[Dict[str, str]]
    # 七层流水线扩展字段
    self_check_passed: bool = True
    self_check_issues: List[str] = field(default_factory=list)
    cross_agent_links: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    # AI 关联扩充（与原始内容严格区分）
    ai_expansion: str = ""

    def __init__(self, form: str, title: str, frontmatter: Dict[str, Any],
                 background: str, core_content: str, boundaries: Dict[str, str],
                 anti_patterns: List[str], related_concepts: List[str],
                 relations: List[Dict[str, str]] = None,
                 self_check_passed: bool = True,
                 self_check_issues: List[str] = None,
                 cross_agent_links: List[str] = None,
                 keywords: List[str] = None,
                 ai_expansion: str = ""):
        self.form = form
        self.title = title
        self.frontmatter = frontmatter
        self.background = background
        self.core_content = core_content
        self.boundaries = boundaries
        self.anti_patterns = anti_patterns
        self.related_concepts = related_concepts
        self.relations = relations or []
        self.self_check_passed = self_check_passed
        self.self_check_issues = self_check_issues or []
        self.cross_agent_links = cross_agent_links or []
        self.keywords = keywords or []
        self.ai_expansion = ai_expansion


@dataclass
class PipelineLayerResult:
    """流水线单层执行结果"""
    layer: int
    name: str
    passed: bool
    detail: Dict[str, Any] = field(default_factory=dict)


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
    needs_reconfirm: bool = False
    reconfirm_question: str = ""
    # 七层流水线追踪
    layer_results: List[PipelineLayerResult] = field(default_factory=list)
    prejudgment: str = ""  # CERTAINLY_YES / CERTAINLY_NO / MAYBE
    prejudgment_confidence: float = 0.0
    self_check_passed: bool = True
    self_check_issues: List[str] = field(default_factory=list)
    cross_agent_links: List[str] = field(default_factory=list)


# ========== 内容清洗 ==========

def clean_message_content(content: str) -> str:
    """清理消息内容"""
    if not content:
        return ""
    content = re.sub(r'\[thinking\].*?(?:\[/thinking\]|$)', '', content, flags=re.DOTALL)
    content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    content = re.sub(
        r'^(?!.*[\u4e00-\u9fff])\s*(curl|chmod|wget|npm|pip|pip3|docker|git|mkdir|cd|ls|cat|rm|mv|cp)\b.+$',
        '', content, flags=re.MULTILINE,
    )
    content = re.sub(r'^\s*\d+\.\s*$', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def build_session_text(messages: List[Dict], max_chars: int = 24000) -> str:
    """从消息列表构建对话文本。

    长会话处理策略（head-tail 截断）：
    - 保留开头 30% 的上下文（背景、问题设定）
    - 保留结尾 70% 的最新对话（关键决策、结论）
    - 中间用省略标记标注被跳过的 turn 数量
    - 单条消息仍限制 1000 字符，避免极端长消息撑爆上下文
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "").strip()
        if not content:
            continue
        content = clean_message_content(content)
        if not content:
            continue
        if len(content) > 1000:
            content = content[:1000] + "...(truncated)"
        lines.append(f"[{role}] {content}")

    full_text = "\n\n".join(lines)
    if len(full_text) <= max_chars:
        return full_text

    # head-tail 智能截断：保留会话两端，标注省略范围
    omission_marker_len = 80  # 预留省略标记长度
    usable = max_chars - omission_marker_len
    head_limit = int(usable * 0.3)
    tail_limit = usable - head_limit

    head_text = full_text[:head_limit]
    # 截断到完整的 turn 边界（最近的一个 \n\n）
    last_boundary = head_text.rfind("\n\n")
    if last_boundary > 0:
        head_text = head_text[:last_boundary]

    tail_text = full_text[-tail_limit:]
    first_boundary = tail_text.find("\n\n")
    if first_boundary >= 0:
        tail_text = tail_text[first_boundary + 2:]

    # 计算被省略的 turn 数（基于原始 lines）
    head_turns = head_text.count("\n\n") + 1 if head_text else 0
    tail_turns = tail_text.count("\n\n") + 1 if tail_text else 0
    omitted_turns = max(0, len(lines) - head_turns - tail_turns)

    return (
        f"{head_text}\n\n"
        f"[... {omitted_turns} turns omitted; showing head + tail ...]\n\n"
        f"{tail_text}"
    )


# ========== JSON 解析容错 ==========

def extract_json(text: str) -> Optional[Dict]:
    """从文本中提取 JSON，带容错处理（针对 DeepSeek-V3 等模型）"""
    if not text:
        return None

    # 尝试 1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试 2: 提取 markdown 代码块
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试 3: 找最外层平衡花括号（处理前后有说明文字的情况）
    candidates = []
    start = text.find("{")
    while start != -1:
        brace_count = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not in_string:
                in_string = True
            elif ch == '"' and in_string:
                in_string = False
            elif ch == "{" and not in_string:
                brace_count += 1
            elif ch == "}" and not in_string:
                brace_count -= 1
                if brace_count == 0:
                    candidates.append(text[start:i + 1])
                    break
        start = text.find("{", start + 1)

    # 按长度排序，优先尝试最长的（最可能是完整 JSON）
    for candidate in sorted(candidates, key=len, reverse=True):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 尝试 4: 自动修复常见语法错误
    def _fix_json(s: str) -> Optional[Dict]:
        fixes = [
            # 修复尾随逗号
            lambda x: re.sub(r',(\s*[}\]])', r'\1', x),
            # 单引号转双引号
            lambda x: x.replace("'", '"'),
            # 去除 BOM 和控制字符
            lambda x: x.strip('\ufeff\x00\x01\x02'),
            # 修复未转义的换行符（简单替换）
            lambda x: re.sub(r'(?<!\\)\n', '\\n', x),
        ]
        for fix in fixes:
            s = fix(s)
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
        return None

    # 对最长候选应用修复
    for candidate in sorted(candidates, key=len, reverse=True):
        result = _fix_json(candidate)
        if result is not None:
            return result

    # 尝试 5: 对整个文本应用修复
    result = _fix_json(text)
    if result is not None:
        return result

    return None


# ========== HostAgentCaller — 宿主 Agent 调用器 ==========

class HostAgentCaller:
    """宿主 Agent 调用器 — 同源复用

    优先级：claude -p → kimi --print → OpenAI 兼容 API → AgentDelegate 异步
    """

    MAX_RETRIES = 2
    TIMEOUT = 180  # 长 prompt 蒸馏需要更长时间

    def __init__(self, timeout: int = None, force_provider: str = None):
        self._timeout = timeout or self.TIMEOUT
        self._force_provider = force_provider  # "api" | "cli" | None

    def call(self, prompt: str, expect_json: bool = True,
             max_retries: int = None, timeout: int = None) -> Optional[Dict]:
        """调用宿主 Agent，返回解析后的 JSON 或原始文本"""
        retries = max_retries if max_retries is not None else self.MAX_RETRIES
        timeout = timeout or self._timeout

        for attempt in range(retries + 1):
            try:
                raw = self._invoke(prompt, timeout)
                if raw is None:
                    continue

                if expect_json:
                    parsed = extract_json(raw)
                    if parsed is not None:
                        self._log_call(prompt, raw, True)
                        return parsed
                    if attempt < retries:
                        continue
                else:
                    self._log_call(prompt, raw, True)
                    return {"raw": raw}
            except subprocess.TimeoutExpired:
                logger.warning(f"HostAgentCaller timeout (attempt {attempt + 1})")
            except Exception as e:
                logger.warning(f"HostAgentCaller error (attempt {attempt + 1}): {e}")

        self._log_call(prompt, "", False)
        return None

    def _invoke(self, prompt: str, timeout: int) -> Optional[str]:
        if self._force_provider == "api":
            raw = self._try_openai_compatible_api(prompt, timeout)
            if raw is not None:
                return raw
            return self._try_delegate(prompt, timeout)

        if self._force_provider == "cli":
            raw = self._try_cli("claude", ["-p", prompt], timeout)
            if raw is not None:
                return raw
            raw = self._try_cli("kimi", ["--print", prompt], timeout)
            if raw is not None:
                return raw
            return self._try_delegate(prompt, timeout)

        # 默认优先级：claude → kimi → api → delegate
        raw = self._try_cli("claude", ["-p", prompt], timeout)
        if raw is not None:
            return raw
        raw = self._try_cli("kimi", ["--print", prompt], timeout)
        if raw is not None:
            return raw
        raw = self._try_openai_compatible_api(prompt, timeout)
        if raw is not None:
            return raw
        return self._try_delegate(prompt, timeout)

    def _try_openai_compatible_api(self, prompt: str, timeout: int) -> Optional[str]:
        """尝试通过 OpenAI 兼容 API 调用 LLM（如 SiliconFlow、OpenAI、Azure 等）

        环境变量配置：
        - OPENAI_API_KEY: OpenAI API 密钥
        - SILICONFLOW_API_KEY: SiliconFlow API 密钥
        - OPENAI_BASE_URL: API 基础地址（默认 https://api.openai.com/v1）
        - OPENAI_MODEL: 模型名称（默认 gpt-4o-mini）
        """
        import urllib.request
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # 根据提供商选择默认模型
        if "siliconflow" in base_url.lower():
            default_model = "deepseek-ai/DeepSeek-V3"
        else:
            default_model = "gpt-4o-mini"
        model = os.environ.get("OPENAI_MODEL", default_model)

        # 根据 base_url 智能选择对应的 API key
        if "siliconflow" in base_url.lower():
            api_key = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("OPENAI_API_KEY")
        else:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")

        if not api_key:
            return None
        data = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000,
            "temperature": 0.2,
        }).encode()
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            body = json.loads(resp.read())
            content = body["choices"][0]["message"]["content"]
            return content.strip() if content else None
        except Exception:
            return None

    def _try_cli(self, cmd: str, args: List[str], timeout: int) -> Optional[str]:
        try:
            proc = subprocess.run(
                [cmd] + args,
                capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "NO_COLOR": "1"},
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            raise
        return None

    def _try_delegate(self, prompt: str, timeout: int) -> Optional[str]:
        from core.prometheus_fire import AgentDelegate, DistillTask
        delegate = AgentDelegate()
        task = DistillTask(
            session_id=f"distill-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
            messages=[{"role": "user", "content": prompt}],
            meta={"source": "distillation-engine", "task_type": "knowledge_distill",
                  "full_prompt": prompt},
        )
        output_path = Path.home() / ".mnemos" / "distill_output" / f"{task.session_id}.md"
        ok = delegate.delegate(task, output_path)
        if not ok:
            return None
        return delegate.wait_for_result(output_path, timeout=timeout)

    def _log_call(self, prompt: str, response: str, success: bool):
        try:
            db_path = _get_wiki_db()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS prompt_call_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT, prompt_preview TEXT, response_preview TEXT,
                        success INTEGER, duration_ms INTEGER
                    )
                """)
                conn.execute(
                    "INSERT INTO prompt_call_log (timestamp, prompt_preview, response_preview, success, duration_ms) VALUES (?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(), prompt[:500], response[:500], int(success), 0),
                )
                conn.commit()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
            pass


# ========== 第1层：噪音过滤 ==========

class NoiseFilter:
    """第1层：噪音过滤 — 规则级，<1ms

    复用 ingest_helpers.is_noise_message()，纯规则无 LLM。
    """

    def filter(self, messages: List[Dict]) -> Tuple[List[Dict], Dict]:
        filtered = []
        noise_count = 0
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "")
            if role == "system":
                filtered.append(msg)
                continue
            if is_noise_message(content):
                noise_count += 1
                continue
            filtered.append(msg)

        stats = {"total": len(messages), "noise": noise_count, "kept": len(filtered)}
        return filtered, stats


# ========== 第2层：价值预判 ==========

class ValuePrejudgment:
    """第2层：价值预判 — 规则 + 贝叶斯

    输出三种结论：
      CERTAINLY_YES — 高置信度有价值，可跳过 LLM 判断
      CERTAINLY_NO  — 高置信度无价值，直接跳过
      MAYBE         — 需 LLM 语义判断
    """

    CERTAINLY_YES = "CERTAINLY_YES"
    CERTAINLY_NO = "CERTAINLY_NO"
    MAYBE = "MAYBE"

    # 知识信号关键词（中文 + 英文）
    _KNOWLEDGE_SIGNALS = [
        "原来", "本质", "根因", "因为", "所以", "导致", "解决", "修复",
        "选", "决定", "采用", "而非", "避免", "不要", "切忌", "步骤",
        "方法", "原则", "经验", "教训", "踩坑", "最佳实践",
        "because", "therefore", "solution", "decided", "avoid",
        "best practice", "root cause", "lesson", "pitfall",
    ]

    _NOISE_SIGNALS = [
        "好的", "收到", "谢谢", "嗯", "哦", "了解",
        "ok", "thanks", "got it", "sure", "fine",
    ]

    def __init__(self):
        self._distill_scorer = None
        self._distill_scorer_v2 = None

    def _get_scorer(self):
        if self._distill_scorer is None:
            try:
                from core.scoring.scorers.distill_scorer import DistillScorer
                self._distill_scorer = DistillScorer()
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        return self._distill_scorer

    def _get_scorer_v2(self):
        """获取 V2 评分器（懒加载，失败静默回退）。"""
        if self._distill_scorer_v2 is None:
            try:
                from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2
                self._distill_scorer_v2 = DistillScorerV2()
            except Exception:
                logging.getLogger(__name__).debug("DistillScorerV2 not available, falling back to V1")
                pass
        return self._distill_scorer_v2

    def judge(self, messages: List[Dict]) -> Tuple[str, float]:
        """预判会话价值，返回 (结论, 置信度)

        【阶段二 V2 桥接】优先尝试 V2 评分器；若不可用则回退 V1；
        最终融合规则先验 + ML 后验得分。
        """
        session_text = build_session_text(messages)
        if not session_text:
            return self.CERTAINLY_NO, 0.9

        rule_score = self._rule_assessment(session_text)

        # V2 评分优先（阶段二桥接）
        v2_score = None
        scorer_v2 = self._get_scorer_v2()
        if scorer_v2:
            try:
                v2_score = scorer_v2.score(session_text)
            except Exception:
                logging.getLogger(__name__).debug("V2 scoring failed, falling back to V1/rule")
                pass

        if v2_score is not None:
            # V2 域 0-1，直接融合
            v2_distill = v2_score.scores.get("distill")
            if v2_distill is not None:
                combined = 0.4 * rule_score + 0.6 * v2_distill
            else:
                combined = rule_score
        else:
            # V1 回退路径
            scorer = self._get_scorer()
            if scorer:
                try:
                    cards = scorer.score(session_text)
                    distill_card = next(
                        (c for c in cards if c.dimension == "distill_score"), None,
                    )
                    if distill_card:
                        bayesian_score = distill_card.value
                        combined = 0.4 * rule_score + 0.6 * bayesian_score
                    else:
                        combined = rule_score
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
                    combined = rule_score
            else:
                combined = rule_score

        if combined >= 0.7:
            return self.CERTAINLY_YES, combined
        elif combined <= 0.3:
            return self.CERTAINLY_NO, 1.0 - combined
        return self.MAYBE, combined

    def _rule_assessment(self, text: str) -> float:
        """规则级快速评估"""
        lower = text.lower()
        score = 0.3

        # 知识信号检测
        knowledge_hits = sum(1 for sig in self._KNOWLEDGE_SIGNALS if sig in lower)
        score += min(0.4, knowledge_hits * 0.08)

        # 噪声信号检测
        noise_hits = sum(1 for sig in self._NOISE_SIGNALS if sig in lower)
        score -= min(0.2, noise_hits * 0.05)

        # 长度信号：太短 (<200) 降分，适中 (500-3000) 加分
        length = len(text)
        if length < 200:
            score -= 0.15
        elif 500 <= length <= 3000:
            score += 0.1

        # 代码/技术内容加分
        if re.search(r'```|def |class |import |function ', text):
            score += 0.1

        # 问答模式加分
        if re.search(r'\?.*\n.*\n', text) or re.search(r'？.*\n.*\n', text):
            score += 0.05

        return max(0.0, min(1.0, score))


# ========== 第3层：LLM 语义判断 ==========

class LLMValueJudge:
    """第3层：LLM 语义判断 — 宿主 Agent 调用

    输出 knowledge / skill / skip + 置信度。
    """

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()

    def judge(self, session_text: str, session_id: str = "") -> Tuple[str, str, float]:
        """LLM 价值判断，返回 (判断, 理由, 置信度)"""
        from .distillation_prompts import STAGE1_FILTER_PROMPT

        prompt = STAGE1_FILTER_PROMPT.replace("{session_content}", session_text)
        result = self._caller.call(prompt, expect_json=True)

        if result is None:
            return "skip", "LLM调用失败", 0.0

        judgment = result.get("judgment", "skip")
        reason = result.get("reason", "")
        confidence = 0.5
        if judgment == "knowledge":
            confidence = 0.7
        elif judgment == "skill":
            confidence = 0.6

        return judgment, reason, confidence


# ========== 第4层：知识提取 ==========

class KnowledgeExtractor:
    """第4层：知识提取 — LLM + assertion_extractor 验证

    六种知识形态：decision / pattern / pitfall / snippet / reference / todo
    """

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()

    def extract(self, session_text: str, session_id: str = "",
                analysis_type: str = "standard") -> List[KnowledgeFragment]:
        """提取知识片段"""
        prompt = self._build_prompt(session_text, session_id, analysis_type)
        result = self._caller.call(prompt, expect_json=True)

        if result is None:
            return self._fallback_extract(session_text, session_id)

        fragments = self._parse_fragments(result, session_id)

        # assertion_extractor 验证
        if fragments:
            fragments = self._validate_with_assertions(fragments, session_text)

        return fragments

    def _build_prompt(self, session_text: str, session_id: str,
                      analysis_type: str) -> str:
        from .distillation_prompts import DISTILLATION_PROMPT
        prompt = DISTILLATION_PROMPT
        prompt = prompt.replace("{session_id}", session_id or "unknown")
        prompt = prompt.replace("{session_content}", session_text)
        return prompt

    def _parse_fragments(self, data: Dict, session_id: str) -> List[KnowledgeFragment]:
        """从 LLM JSON 输出解析知识片段 — 兼容列表和旧分层对象格式"""
        fragments = []
        for frag_data in data.get("fragments", []):
            try:
                fm = frag_data.get("frontmatter", {})
                kw = fm.get("关键词", {})
                keywords = []
                if isinstance(kw, list):
                    # 新格式：简单列表
                    keywords = kw
                elif isinstance(kw, dict):
                    # 旧格式：分层对象 {核心概念: [...], 场景标签: [...]}
                    for layer_words in kw.values():
                        if isinstance(layer_words, list):
                            keywords.extend(layer_words)
                        elif isinstance(layer_words, str):
                            keywords.append(layer_words)

                fragment = KnowledgeFragment(
                    form=frag_data.get("form", "未知"),
                    title=frag_data.get("title", "无标题"),
                    frontmatter=fm,
                    background=frag_data.get("background", ""),
                    core_content=frag_data.get("core_content", ""),
                    boundaries=frag_data.get("boundaries", {}),
                    anti_patterns=frag_data.get("anti_patterns", []),
                    related_concepts=frag_data.get("related_concepts", []),
                    relations=frag_data.get("relations", []),
                    keywords=keywords,
                )
                fragments.append(fragment)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
                continue
        return fragments

    def _validate_with_assertions(self, fragments: List[KnowledgeFragment],
                                  session_text: str) -> List[KnowledgeFragment]:
        """用 assertion_extractor 交叉验证提取结果"""
        try:
            from core.kia.assertion_extractor import extract_assertions
            assertions = extract_assertions(session_text)
            if not assertions:
                return fragments

            assertion_claims = {a.claim[:60] for a in assertions if a.confidence >= 0.5}
            for frag in fragments:
                content_lower = frag.core_content.lower() + frag.title.lower()
                overlap = sum(1 for claim in assertion_claims if claim.lower() in content_lower)
                if overlap == 0 and len(assertion_claims) > 3:
                    frag.frontmatter["assertion_validated"] = False
                    frag.frontmatter["置信度"] = min(
                        frag.frontmatter.get("置信度", 0.6), 0.4,
                    )
                else:
                    frag.frontmatter["assertion_validated"] = True
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
            pass
        return fragments

    def _fallback_extract(self, session_text: str,
                          session_id: str) -> List[KnowledgeFragment]:
        """LLM 不可用时的规则级降级提取 — 对齐蓝图标准"""
        try:
            from core.kia.assertion_extractor import extract_assertions, merge_similar_assertions
            assertions = extract_assertions(session_text, source=session_id)
            assertions = merge_similar_assertions(assertions)
            assertions = [a for a in assertions if a.confidence >= 0.4]
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
            return []

        if not assertions:
            return []

        from collections import defaultdict
        by_form = defaultdict(list)
        for a in assertions:
            by_form[a.form.value].append(a)

        fragments = []
        for form_value, form_assertions in by_form.items():
            best = max(form_assertions, key=lambda a: a.confidence)
            # 生成摘要：用 context + 前3条 claim 组合
            summary_parts = []
            if best.context:
                summary_parts.append(best.context[:100])
            summary_parts.extend([a.claim[:60] for a in form_assertions[:2]])
            summary = "；".join(summary_parts)[:180] if summary_parts else best.claim[:150]

            # 提取关键词（中英文混合）
            keywords = []
            # 英文技术词汇
            keywords.extend(re.findall(r'[a-zA-Z_]{3,}', best.claim))
            # 中文关键词（2-6字名词）
            keywords.extend(re.findall(r'[\u4e00-\u9fff]{2,6}', best.claim))
            keywords = list(dict.fromkeys(keywords))[:12]  # 去重，最多12个

            fragments.append(KnowledgeFragment(
                form=form_value,
                title=best.claim[:80],
                frontmatter={
                    "类型": _map_form_to_type(form_value),
                    "摘要": summary,
                    "置信度": best.confidence,
                    "证据级别": best.evidence_level or "single",
                    "时效性": best.temporal_scope or "contextual",
                    "提取方式": "rule_fallback",
                },
                background=best.context[:300] if best.context else "",
                core_content="\n".join(f"- {a.claim}" for a in form_assertions[:10]),
                boundaries={"applies": best.boundary_hint} if best.boundary_hint else {},
                anti_patterns=[a.claim for a in form_assertions if a.is_negated],
                related_concepts=[],
                relations=[],
                keywords=keywords,
            ))
        return fragments


# ========== 第5层：自检 ==========

class DistillSelfCheck:
    """第5层：自检 — 规则验证

    检查项：
    1. 断言可验证性与内部冲突
    2. 代码语法正确性
    3. Wiki/URL 链接有效性（轻量格式校验 + 后台可达性探测 enqueue）
    4. 时间范围合理性与 contextual 自动标记
    不通过时标记 pending-verification，仍允许入库。
    """

    def __init__(self, link_probe_worker=None):
        """
        Args:
            link_probe_worker: 可选的 LinkProbeWorker 实例。
                               传入后，URL 检测时会将外部链接 enqueue 到后台探测队列。
        """
        self._link_probe = link_probe_worker

    def check(self, fragments: List[KnowledgeFragment],
              messages: List[Dict]) -> Tuple[bool, List[str]]:
        """自检，返回 (是否全部通过, 问题列表)"""
        all_issues = []
        for frag in fragments:
            issues = self._check_fragment(frag, messages)
            frag.self_check_issues = issues
            frag.self_check_passed = len(issues) == 0
            all_issues.extend(issues)

        overall_passed = len(all_issues) == 0
        if not overall_passed:
            for frag in fragments:
                if not frag.self_check_passed:
                    frag.frontmatter["verification"] = "pending-verification"
        return overall_passed, all_issues

    def _check_fragment(self, frag: KnowledgeFragment,
                        messages: List[Dict]) -> List[str]:
        issues = []
        # 1. 标题质量
        if not frag.title or frag.title in ("无标题", "未知"):
            issues.append("标题缺失或无效")
        elif len(frag.title) < 5:
            issues.append("标题过短，缺乏可搜索性")

        # 2. 核心内容质量
        if not frag.core_content or len(frag.core_content) < 20:
            issues.append("核心内容过短或缺失")
        elif len(frag.core_content) > 5000 and not frag.boundaries:
            issues.append("内容过长且缺少边界定义")

        # 3. 断言可验证性
        content = frag.core_content + frag.background
        has_specific_data = bool(re.search(
            r'\d+\.?\d*[%％]|v\d+\.\d+|version\s+\d+|>=|<=|!=|==', content,
        ))
        has_assertion_words = bool(re.search(
            r'(必须|一定|never|always|应该|should|导致|因为|由于)', content, re.I,
        ))
        if has_assertion_words and not has_specific_data:
            issues.append("包含断言但缺少具体数据支撑")

        # 4. 代码块语法检查
        code_blocks = re.findall(r'```(\w*)\n(.*?)```', frag.core_content, re.DOTALL)
        for lang, code in code_blocks:
            if lang in ("python", "py"):
                if self._check_python_syntax(code):
                    issues.append(f"Python代码块可能存在语法错误")

        # 5. Wiki 链接有效性
        wiki_links = re.findall(r'\[\[([^\]]+)\]\]', content)
        for link in wiki_links:
            if len(link) < 2 or link.startswith("待"):
                issues.append(f"可疑的Wiki链接: [[{link}]]")

        # 6. 时间范围合理性
        temporal = frag.frontmatter.get("时效性", "")
        if temporal == "version-bound" and not frag.frontmatter.get("版本标记"):
            issues.append("标记为版本绑定但未指定版本标记")
        if not temporal and self._looks_contextual(content):
            frag.frontmatter["时效性"] = "contextual"
            issues.append("包含当前性表述，已标记为 contextual")

        # 7. 回流检测
        if "<wiki-context" in content or "skip-distill" in content:
            issues.append("检测到回流内容，不应再次蒸馏")

        # 8. URL 轻量校验 + 后台可达性探测 enqueue
        for url in re.findall(r'https?://[^\s)\]>"]+', content):
            if "." not in url.split("://", 1)[1]:
                issues.append(f"可疑URL，待验证: {url}")
            else:
                frag.frontmatter.setdefault("external_links_pending_verification", True)
                # 将外部链接 enqueue 到后台探测队列（零阻塞）
                if self._link_probe is not None:
                    # 使用 fragment 标题作为页面标识（实际 wiki 路径由调用方决定）
                    page_path = frag.frontmatter.get("wiki_page_path", frag.title)
                    self._link_probe.enqueue(url, page_path)

        # 9. 断言内部冲突检测（复用 conflict_resolver 规则）
        issues.extend(self._check_internal_conflicts(content))

        # 10. 前提验证（PremiseValidator）
        try:
            from core.kia.premise_validator import PremiseValidator
            validator = PremiseValidator()
            result = validator.validate(
                premise=frag.core_content[:500],
                current_context=content,
            )
            if not result.get("valid", True):
                issues.append(
                    f"前提验证未通过: {result.get('reason', 'unknown')} "
                    f"(置信度 {result.get('confidence', 0):.2f})"
                )
        except Exception:
            pass

        # 11. 决策依赖提取（DecisionDependencyExtractor）
        try:
            from core.kia.decision_dependency_extractor import DecisionDependencyExtractor
            extractor = DecisionDependencyExtractor()
            decision_keywords = ["选择", "决定", "决策", "如果", "则", "否则", "option", "decide", "choose"]
            if any(kw in content.lower() for kw in decision_keywords):
                graph = extractor.extract(content)
                if graph.nodes:
                    frag.frontmatter["decision_graph"] = {
                        "nodes": len(graph.nodes),
                        "edges": len(graph.edges),
                        "roots": len(graph.get_root_decisions()),
                    }
        except Exception:
            pass

        return issues

    def _check_python_syntax(self, code: str) -> bool:
        """简单 Python 语法检查，返回 True 表示有错误"""
        try:
            compile(code, "<distill-check>", "exec")
            return False
        except SyntaxError:
            return True

    def _looks_contextual(self, content: str) -> bool:
        return bool(re.search(r'(最新|目前|现在|当前|recently|currently|latest|as of)', content, re.I))

    def _check_internal_conflicts(self, content: str) -> List[str]:
        try:
            from core.kia.assertion_extractor import extract_assertions
            from core.kia.conflict_resolver import detect_conflicts
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
            return []

        assertions = extract_assertions(content, source="distill_self_check")
        if len(assertions) < 2:
            return []

        issues = []
        for i, assertion in enumerate(assertions):
            conflicts = detect_conflicts([assertion], assertions[i + 1:], min_topic_overlap=0.2)
            for conflict in conflicts[:2]:
                issues.append(f"检测到断言内部冲突: {conflict.reason or conflict.conflict_type}")
        return issues


# ========== 第6层：跨 Agent 关联 ==========

class CrossAgentLinker:
    """第6层：跨 Agent 关联 — Jaccard 关键词重叠

    自动注入 [[反向链接]]，连接不同 Agent 产出的相关知识。
    """

    JACCARD_THRESHOLD = 0.45

    def __init__(self):
        self._db_path = _get_wiki_db()

    def link(self, fragments: List[KnowledgeFragment]) -> List[KnowledgeFragment]:
        """为每个 fragment 查找跨 Agent 关联并注入链接"""
        existing_pages = self._load_existing_pages()
        if not existing_pages:
            return fragments

        for frag in fragments:
            frag_keywords = self._extract_keywords(frag)
            if not frag_keywords:
                continue

            links = []
            for page_id, page_keywords, page_source in existing_pages:
                jaccard = self._jaccard(frag_keywords, page_keywords)
                if jaccard >= self.JACCARD_THRESHOLD:
                    links.append(page_id)
                    if page_id not in frag.related_concepts:
                        frag.related_concepts.append(page_id)

            frag.cross_agent_links = links

        return fragments

    def _load_existing_pages(self) -> List[Tuple[str, set, str]]:
        """从 Wiki 目录加载已有页面的关键词"""
        pages = []
        wiki_dir = _get_wiki_dir()

        for subdir in ["00-Inbox", "01-Projects", "02-Areas", "03-Tech", "04-Concepts"]:
            md_dir = wiki_dir / subdir
            if not md_dir.exists():
                continue
            for md_file in md_dir.glob("*.md"):
                try:
                    content = md_file.read_text(encoding="utf-8")[:2000]
                    source = ""
                    m = re.search(r'^source_agent:\s*(.+)$', content, re.MULTILINE)
                    if m:
                        source = m.group(1).strip()
                    keywords = self._text_to_keywords(content)
                    pages.append((md_file.stem, keywords, source))
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
                    continue
        return pages

    def _extract_keywords(self, frag: KnowledgeFragment) -> set:
        """从 fragment 提取关键词集合"""
        kw = set(frag.keywords) if frag.keywords else set()
        title_words = re.findall(r'[a-zA-Z_]{3,}', frag.title)
        kw.update(w.lower() for w in title_words)
        # 中文只取 3-6 字词组，避免 2 字碎片噪音；同时过滤纯虚词组合
        content_words = re.findall(r'[一-龥]{3,6}', frag.core_content[:500])
        kw.update(w for w in content_words if not self._is_stop_phrase(w))
        return kw

    def _text_to_keywords(self, text: str) -> set:
        """从文本提取关键词集合"""
        kw = set()
        kw.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        content_words = re.findall(r'[一-龥]{3,6}', text)
        kw.update(w for w in content_words if not self._is_stop_phrase(w))
        return kw

    _STOP_PHRASES = {
        "在系统", "系统中", "系统里", "在进程", "进程中", "过程里", "在实现",
        "实现中", "在运行", "运行时", "在代码", "代码中", "在配置", "配置中",
        "在部署", "部署中", "在服务", "服务中", "在应用", "应用中", "在模块",
        "模块中", "在函数", "函数中", "在方法", "方法中", "在类中", "在文件",
        "文件中", "在目录", "目录中", "在路径", "路径中", "在环境", "环境中",
        "在容器", "容器中", "在集群", "集群中", "在节点", "节点中", "在数据库",
        "数据库", "在缓存", "缓存中", "在网络", "网络中", "在协议", "协议中",
        "在接口", "接口中", "在框架", "框架中", "在工具", "工具中", "在平台",
        "平台中", "在版本", "版本中", "在分支", "分支中", "在主线", "主线中",
        "在开发", "开发中", "在测试", "测试中", "在生产", "生产中", "在线上",
        "线上下", "在本地", "本地中", "在远程", "远程中", "在客户端", "客户端",
        "在服务", "服务端", "在前后", "前后端", "在业务", "业务中", "在逻辑",
        "逻辑中", "在数据", "数据中", "在状态", "状态中", "在事件", "事件中",
        "在消息", "消息中", "在队列", "队列中", "在任务", "任务中", "在作业",
        "作业中", "在流程", "流程中", "在步骤", "步骤中", "在阶段", "阶段中",
        "在周期", "周期中", "在迭代", "迭代中", "在循环", "循环中", "在条件",
        "条件下", "在异常", "异常中", "在错误", "错误中", "在日志", "日志中",
        "在监控", "监控中", "在告警", "告警中", "在指标", "指标中", "在报表",
        "报表中", "在统计", "统计中", "在分析", "分析中", "在查询", "查询中",
        "在搜索", "搜索中", "在索引", "索引中", "在存储", "存储中", "在备份",
        "备份中", "在恢复", "恢复中", "在迁移", "迁移中", "在升级", "升级中",
        "在降级", "降级中", "在回滚", "回滚中", "在发布", "发布中", "在构建",
        "构建中", "在编译", "编译中", "在打包", "打包中", "在分发", "分发中",
        "在安装", "安装中", "在卸载", "卸载中", "在启动", "启动中", "在停止",
        "停止中", "在重启", "重启中", "在加载", "加载中", "在卸载", "保存中",
        "在读取", "读取中", "在写入", "写入中", "在删除", "删除中", "在更新",
        "更新中", "在创建", "创建中", "在销毁", "销毁中", "在初始化", "初始化",
        "在注册", "注册中", "在注销", "注销中", "在订阅", "订阅中", "在取消",
        "取消中", "在确认", "确认中", "在验证", "验证中", "在授权", "授权中",
        "在认证", "认证中", "在审计", "审计中", "在加密", "加密中", "在解密",
        "解密中", "在压缩", "压缩中", "在解压", "解压中", "在编码", "编码中",
        "在解码", "解码中", "在序列", "序列化", "在反序", "反序列", "在并发",
        "并发中", "在并行", "并行中", "在同步", "同步中", "在异步", "异步中",
        "在阻塞", "阻塞中", "在非阻", "非阻塞", "在缓冲", "缓冲中", "在缓存",
        "缓存中", "在池化", "池化中", "在复用", "复用中", "在共享", "共享中",
        "在隔离", "隔离中", "在限流", "限流中", "在降级", "降级中", "在熔断",
        "熔断中", "在重试", "重试中", "在超时", "超时中", "在负载", "负载中",
        "在均衡", "均衡中", "在路由", "路由中", "在代理", "代理中", "在转发",
        "转发中", "在网关", "网关中", "在防火墙", "防火墙", "在安", "安全中",
        "在防护", "防护中", "在攻击", "攻击中", "在漏洞", "漏洞中", "在风险",
        "风险中", "在问题", "问题中", "在解决", "解决中", "在方案", "方案中",
        "在计划", "计划中", "在策略", "策略中", "在规则", "规则中", "在规范",
        "规范中", "在标准", "标准中", "在指南", "指南中", "在手册", "手册中",
        "在文档", "文档中", "在注释", "注释中", "在说明", "说明中", "在描述",
        "描述中", "在定义", "定义中", "在命名", "命名中", "在约定", "约定中",
        "在最佳", "最佳实", "最佳实践", "在实践", "实践中", "在模式", "模式中",
        "在架构", "架构中", "在设计", "设计中", "在模型", "模型中", "在结构",
        "结构中", "在层次", "层次中", "在组件", "组件中", "在依赖", "依赖中",
        "在耦合", "耦合中", "在内聚", "内聚中", "在封装", "封装中", "在抽象",
        "抽象中", "在继承", "继承中", "在多态", "多态中", "在组合", "组合中",
        "在聚合", "聚合中", "在关联", "关联中", "在泛化", "泛化中", "在实现",
        "实现中", "在接口", "接口中", "在契约", "契约中", "在协议", "协议中",
        "在规范", "规范中", "在标准", "标准中", "在要求", "要求中", "在需求",
        "需求中", "在功能", "功能中", "在特性", "特性中", "在性能", "性能中",
        "在效率", "效率中", "在优化", "优化中", "在调优", "调优中", "在配置",
        "配置中", "在参数", "参数中", "在选项", "选项中", "在设置", "设置中",
        "在变量", "变量中", "在常量", "常量中", "在枚举", "枚举中", "在结构",
        "结构中", "在联合", "联合中", "在元组", "元组中", "在列表", "列表中",
        "在字典", "字典中", "在集合", "集合中", "在队列", "队列中", "在栈中",
        "栈中", "在堆中", "堆中", "在树中", "树中", "在图中", "图中", "在网",
        "网络中",
    }

    @classmethod
    def _is_stop_phrase(cls, phrase: str) -> bool:
        """判断是否为无意义的停用短语（中文切片噪音）"""
        if phrase in cls._STOP_PHRASES:
            return True
        # 以介词/虚词开头或结尾的 3-4 字短语大概率是切片
        if len(phrase) <= 4:
            prefix_stop = {"在", "的", "了", "和", "与", "或", "是", "有", "为", "以", "及", "对", "从", "到", "向", "把", "被", "让", "给", "比", "跟", "同", "当", "因", "于", "就", "都", "也", "还", "但", "而", "却", "若", "虽", "既", "即", "则", "乃", "且", "并", "又", "亦", "之", "其", "所", "这", "那", "哪", "什", "怎", "谁", "何", "如", "若", "倘", "假如", "假使", "若是", "若是", "即使", "即便", "尽管", "不管", "不论", "无论", "只要", "只有", "除非", "因为", "由于", "因此", "因而", "所以", "于是", "从而", "但是", "可是", "然而", "不过", "只是", "不料", "岂知", "虽然", "虽说", "尽管", "固然", "固然", "诚然", "纵然", "即使", "哪怕", "不管", "无论", "不论", "不要", "不能", "不会", "不可", "不得", "不必", "不用", "应该", "应当", "应", "该", "须", "需", "必须", "必需", "必要", "需要", "须要", "得", "须得", "定", "一定", "必定", "必然", "势必", "当然", "自然", "固然", "本来", "原来", "原本", "原先", "最初", "起先", "开始", "起初", "先", "首先", "其次", "再次", "最后", "最终", "终于", "结果", "后果", "成果", "然后", "而后", "之后", "后来", "随即", "随手", "随手", "立刻", "立即", "马上", "赶紧", "赶快", "连忙", "急忙", "匆忙", "仓促", "临时", "暂且", "暂时", "暂", "且", "姑且", "权且", "暂且", "慢说", "别说", "不但", "不仅", "不只", "不光", "不单", "不独", "而且", "并且", "况且", "何况", "再说", "再者", "否则", "不然", "要不", "要不然", "要么", "因为", "由于", "因此", "因而", "所以", "于是", "从而", "如果", "若是", "要是", "假如", "假使", "假若", "倘若", "倘使", "设若", "若是", "若", "即使", "即便", "纵然", "纵使", "纵然", "哪怕", "尽管", "虽然", "虽说", "固然", "诚然", "固然", "尽管", "不管", "无论", "不论", "不要", "别", "毋", "勿", "莫", "不", "没", "没有", "未", "无", "非", "勿", "别", "甭", "不必", "未必", "也许", "或许", "大概", "大约", "约", "差不多", "几乎", "简直", "根本", "决", "绝对", "完全", "全然", "统统", "通共", "通通", "一律", "一般", "一样", "同样", "也", "又", "还", "再", "更", "最", "太", "极", "非常", "十分", "相当", "比较", "稍微", "略", "较", "挺", "怪", "老", "好", "真", "实在", "确实", "的确", "确乎", "确然", "果然", "居然", "竟然", "竟", "偏偏", "偏", "岂", "难道", "莫非", "别是", "可是", "但是", "然而", "不过", "只是", "不料", "岂知", "固然", "虽然", "尽管", "纵然", "即使", "哪怕", "不管", "无论", "不论", "与其", "不如", "宁可", "宁愿", "宁肯", "情愿", "甘愿", "甘心", "宁愿", "最好", "不如", "何不", "干吗不", "为什么不", "为什么", "怎么", "怎样", "如何", "何以", "为何", "为什么", "干什么", "做什么", "怎么办", "怎么样", "好不好", "行不行", "能不能", "可不可以", "可不可以", "可以", "能", "能够", "会", "可能", "也许", "或许", "大概", "大约", "约莫", "约", "差不多", "几乎", "简直", "差点儿", "险些", "险些儿", "根本", "决", "绝对", "完全", "全然", "统统", "一律", "一般", "一样", "同样", "也", "又", "还", "再", "更", "最", "太", "极", "很", "非常", "十分", "相当", "比较", "较", "更", "最", "越", "越发", "越加", "愈加", "愈发", "尤其", "特别", "格外", "分外", "更加", "更为", "越", "越是", "愈", "愈发", "愈加", "越来越", "愈来愈", "一天比一天", "一年比一年", "越来越", "愈来愈", "格外", "分外", "特别", "尤其", "尤其", "尤为", "格外", "分外", "更加", "更为", "越", "越发", "越加", "愈加", "愈发", "尤其", "特别", "格外", "分外", "更加", "更为"}
            suffix_stop = {"的", "了", "和", "与", "或", "是", "有", "为", "以", "及", "对", "从", "到", "向", "把", "被", "让", "给", "比", "跟", "同", "当", "因", "于", "就", "都", "也", "还", "但", "而", "却", "若", "虽", "既", "即", "则", "乃", "且", "并", "又", "亦", "之", "其", "所", "中", "里", "上", "下", "内", "外", "间", "旁", "边", "面", "头", "底", "前", "后", "左", "右", "东", "西", "南", "北", "里", "内", "中", "间", "处", "方", "面", "头", "部", "边", "缘", "侧", "端", "顶", "底", "根", "源", "本", "末", "初", "始", "终", "结", "果", "尾", "后", "余", "剩", "残", "余", "剩", "余下", "剩下", "残余", "残留", "遗存", "遗迹", "式", "型", "类", "种", "样", "般", "等", "之类", "之流", "之辈", "之徒", "之属", "之俦", "之伦", "之曹", "之亚", "之群", "之类", "之属", "之俦", "之伦", "之曹", "之亚", "之群"}
            if phrase[:1] in prefix_stop or phrase[-1:] in suffix_stop:
                return True
        return False

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union > 0 else 0.0


# ========== 第7层：反馈循环 ==========

class DistillFeedbackLoop:
    """第7层：反馈循环 — 评分驱动

    基于蒸馏结果生成反馈信号，反馈给 AdaptiveScorer(V1) 和
    AdaptiveScorerV2 ground_truth_signals（阶段二桥接）：
    - 高价值但被跳过 → 修正预判阈值
    - 低价值但被提取 → 修正提取阈值
    - 自检失败 → 修正提取质量
    """

    def __init__(self):
        self._scorer = None
        self._scorer_v2 = None

    def _get_scorer(self):
        if self._scorer is None:
            try:
                from core.scoring.scorers.distill_scorer import DistillScorer
                self._scorer = DistillScorer()
            except Exception:
                logging.getLogger(__name__).warning("DistillScorer init failed", exc_info=True)
        return self._scorer

    def _get_scorer_v2(self):
        """获取 V2 评分器引用（用于 ground_truth 写入）。"""
        if self._scorer_v2 is None:
            try:
                from core.scoring.scorers.distill_scorer_v2 import DistillScorerV2
                self._scorer_v2 = DistillScorerV2()
            except Exception:
                logging.getLogger(__name__).debug("DistillScorerV2 not available")
        return self._scorer_v2

    def evaluate(self, result: DistillationResult) -> List[Dict]:
        """评估蒸馏结果，生成反馈信号并驱动模型进化

        信号分为三类：
        - 质量信号：自检通过率、片段丰富度
        - 校准信号：预判与LLM判断的偏差
        - 结构信号：跨Agent关联强度、知识形态多样性
        """
        signals = []
        logger = logging.getLogger(__name__)
        session_id = getattr(result, "session_id", "unknown")

        # ── 信号1-2：预判校准信号 ──
        if result.prejudgment == ValuePrejudgment.CERTAINLY_NO and result.judgment == "knowledge":
            signals.append({
                "type": "prejudgment_mismatch",
                "dimension": "distill_score",
                "expected": 0.3,
                "actual": 0.7,
                "reason": "预判为低价值但LLM判断为知识，应调高预判阈值",
            })
        if result.prejudgment == ValuePrejudgment.CERTAINLY_YES and result.judgment == "skip":
            signals.append({
                "type": "prejudgment_mismatch",
                "dimension": "distill_score",
                "expected": 0.7,
                "actual": 0.3,
                "reason": "预判为高价值但LLM判断为跳过，应调低预判阈值",
            })

        # ── 信号3：自检质量信号 ──
        if result.fragments:
            failed_count = sum(1 for f in result.fragments if not f.self_check_passed)
            fail_rate = failed_count / len(result.fragments)
            if fail_rate > 0.5:
                signals.append({
                    "type": "self_check_failure",
                    "dimension": "quality_score",
                    "expected": 0.7,
                    "actual": 1.0 - fail_rate,
                    "reason": f"自检失败率 {fail_rate:.0%}，提取质量需改善",
                })
            elif fail_rate == 0.0 and len(result.fragments) >= 3:
                # 正向信号：高质量提取
                signals.append({
                    "type": "high_quality_extraction",
                    "dimension": "quality_score",
                    "expected": 0.9,
                    "actual": 1.0,
                    "reason": f"高质量提取：{len(result.fragments)} 个片段，自检全部通过",
                })

        # ── 信号4：零提取 ──
        if result.judgment == "knowledge" and not result.fragments:
            signals.append({
                "type": "zero_extraction",
                "dimension": "distill_score",
                "expected": 0.6,
                "actual": 0.2,
                "reason": "判断为知识但提取无片段，提取逻辑需改善",
            })

        # ── 信号5：跨Agent关联强度 ──
        total_links = len(result.cross_agent_links)
        if result.judgment == "knowledge" and total_links >= 3:
            signals.append({
                "type": "cross_agent_link_strong",
                "dimension": "link_score",
                "expected": 0.8,
                "actual": 1.0,
                "reason": f"强跨Agent关联：{total_links} 条链接",
            })
        elif result.judgment == "knowledge" and total_links == 0 and result.fragments:
            signals.append({
                "type": "cross_agent_link_weak",
                "dimension": "link_score",
                "expected": 0.6,
                "actual": 0.2,
                "reason": "知识未关联到任何已有页面，关联逻辑需改善",
            })

        # ── 信号6：知识形态多样性 ──
        if result.fragments and len(result.fragments) >= 2:
            forms = [f.form for f in result.fragments]
            unique_forms = len(set(forms))
            if unique_forms == 1:
                signals.append({
                    "type": "fragment_diversity_low",
                    "dimension": "diversity_score",
                    "expected": 0.7,
                    "actual": 0.3,
                    "reason": f"所有片段均为同一形态 '{forms[0]}'，形态多样性不足",
                })
            elif unique_forms >= 3:
                signals.append({
                    "type": "fragment_diversity_high",
                    "dimension": "diversity_score",
                    "expected": 0.8,
                    "actual": 1.0,
                    "reason": f"高形态多样性：{unique_forms} 种形态",
                })

        # ── 信号7：提取效率 ──
        if result.judgment == "knowledge" and result.fragments:
            if len(result.fragments) >= 5:
                signals.append({
                    "type": "extraction_rich",
                    "dimension": "yield_score",
                    "expected": 0.8,
                    "actual": 1.0,
                    "reason": f"丰富提取：{len(result.fragments)} 个知识片段",
                })
            elif len(result.fragments) == 1:
                signals.append({
                    "type": "extraction_sparse",
                    "dimension": "yield_score",
                    "expected": 0.6,
                    "actual": 0.3,
                    "reason": "稀疏提取：仅 1 个知识片段",
                })

        # ── 可见性：记录信号摘要 ──
        if signals:
            sig_summary = ", ".join(f"{s['type']}({s['dimension']})" for s in signals)
            logger.info(f"[FeedbackLoop] {session_id} 生成 {len(signals)} 个反馈信号: {sig_summary}")
        else:
            logger.debug(f"[FeedbackLoop] {session_id} 无反馈信号")

        # ── 写入 V1 AdaptiveScorer ──
        scorer = self._get_scorer()
        if scorer and signals:
            v1_ok = 0
            try:
                from core.scoring.adaptive_scorer import Feedback
                for sig in signals:
                    fb = Feedback(
                        dimension=sig["dimension"],
                        expected=sig["expected"],
                        actual=sig["actual"],
                        source="self_observation",
                        context={"reason": sig["reason"], "type": sig["type"]},
                        weight=0.3,
                    )
                    scorer._scorer.feedback(fb)
                    v1_ok += 1
                logger.info(f"[FeedbackLoop] V1 写入 {v1_ok} 条反馈到 AdaptiveScorer")
            except Exception as e:
                logger.warning(f"[FeedbackLoop] V1 feedback dispatch failed: {e}")

        # ── 写入 V2 ground_truth_signals ──
        v2_ok = 0
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            for sig in signals:
                label = 1 if sig["expected"] > sig["actual"] else 0
                AdaptiveScorerV2.insert_ground_truth(
                    session_id=session_id,
                    signal_type=sig["type"],
                    label=label,
                    confidence=abs(sig["expected"] - sig["actual"]),
                )
                v2_ok += 1
            if v2_ok > 0:
                logger.info(f"[FeedbackLoop] V2 写入 {v2_ok} 条 ground_truth")
        except Exception as e:
            logger.warning(f"[FeedbackLoop] V2 ground_truth insert failed: {e}")

        # ── 发布反馈事件到 EventBus ──
        try:
            from core.mnemos_bus import publish_event
            publish_event("feedback_loop", "distill", {
                "session_id": session_id,
                "signal_count": len(signals),
                "signal_types": [s["type"] for s in signals],
                "judgment": result.judgment,
                "fragment_count": len(result.fragments) if result.fragments else 0,
                "cross_agent_links": len(result.cross_agent_links),
            })
        except Exception:
            pass

        return signals


# ========== Wiki 页面生成 ==========

def _yaml_safe(value):
    """对 frontmatter 字符串值进行 YAML 安全转义。

    若值包含 YAML 特殊字符（冒号、井号、引号等），用双引号包裹并转义内部引号。
    """
    if not isinstance(value, str):
        return str(value)
    special_chars = ":#{}[]|&*!?,-<>=%@'`\"\n\r\t"
    if any(c in value for c in special_chars):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


# 知识形态 → 实体类型 映射表
FORM_TO_ENTITY_TYPE = {
    # 中文知识形态
    "问题-解决": "concept",
    "决策记录": "project",
    "经验法则": "concept",
    "反模式": "concept",
    "方法论": "concept",
    "洞察关联": "concept",
    # 英文知识形态（assertion_extractor 及旧 LLM 输出）
    "problem-solution": "concept",
    "decision-log": "project",
    "decision": "project",
    "heuristic": "concept",
    "anti-pattern": "concept",
    "methodology": "concept",
    "insight": "concept",
    "pattern": "concept",
    "pitfall": "concept",
    "snippet": "technology",
    "reference": "technology",
    "todo": "project",
    "data-insight": "dataset",
}


def _map_form_to_type(form: str) -> str:
    """将知识形态映射为蓝图标准的实体类型"""
    return FORM_TO_ENTITY_TYPE.get(form, "concept")


def generate_wiki_page(fragment: KnowledgeFragment, session_id: str,
                       source: str = "") -> str:
    """生成 wiki 页面 Markdown — 对齐蓝图 32 字段规范"""
    # 实体类型优先从 LLM 输出获取，fallback 从知识形态映射
    entity_type = fm_get(fragment.frontmatter, "type", "")
    if not entity_type or entity_type in FORM_TO_ENTITY_TYPE:
        entity_type = _map_form_to_type(fragment.form)

    # 摘要优先从 LLM 输出获取，fallback 到 title + core_content 组合（截断至 150 字符）
    summary = fm_get(fragment.frontmatter, "summary", "")
    if not summary:
        parts = [p for p in (fragment.title, fragment.background) if p]
        summary = " — ".join(parts)[:150] if parts else (fragment.title or "")[:150]

    # 清理 fragment.frontmatter 中的旧类型字段，避免旧类型名覆盖代码映射的正确类型
    cleaned_fm = dict(fragment.frontmatter or {})
    for _k in ("类型", "type"):
        val = cleaned_fm.get(_k)
        if val in FORM_TO_ENTITY_TYPE:
            cleaned_fm.pop(_k, None)

    defaults = {
        "type": entity_type,
        "name": fragment.title,
        "domain": (fragment.frontmatter or {}).get("领域", "未分类"),
        "summary": summary,
        "status": "草稿",
        "knowledge_stage": "原始",
        "source_count": 1,
        "evidence_level": (fragment.frontmatter or {}).get("证据级别", "single"),
        "confidence": (fragment.frontmatter or {}).get("置信度", 0.5),
        "temporal_scope": (fragment.frontmatter or {}).get("时效性", "contextual"),
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "source": source or "unknown",
        "source_session": session_id,
        "distilled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    fm = to_chinese_frontmatter(cleaned_fm, defaults)
    lines = ["---"]
    ordered_keys = [
        "类型", "名称", "领域", "摘要", "状态", "知识阶段",
        "来源数量", "证据级别", "置信度", "时效性", "创建日期",
        "来源", "来源会话", "蒸馏时间", "关键词", "触发器", "别名", "版本标记", "决策摘要", "合并来源",
        "跨Agent关联",
    ]
    for key in ordered_keys:
        if key not in fm:
            continue
        value = fm[key]
        if isinstance(value, (list, dict)):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {_yaml_safe(value)}")

    # 仅保留提取方式标记（用于质量追踪），其余旧字段废弃
    extract_method = (fragment.frontmatter or {}).get("提取方式", "")
    if extract_method:
        lines.append(f"提取方式: {_yaml_safe(extract_method)}")

    if not fragment.self_check_passed:
        lines.append(f"验证状态: pending-verification")

    # 结构化关联上下文（ADR-019）
    if fragment.relations:
        lines.append(f"关联: {json.dumps(fragment.relations, ensure_ascii=False)}")

    lines.append("---")

    body = [f"# {fragment.title}", ""]

    if fragment.background:
        # 如果 background 已包含 Markdown 标题，直接渲染；否则包装在 ## 背景 下
        if fragment.background.strip().startswith("#"):
            body.extend([fragment.background, ""])
        else:
            body.extend(["## 背景", "", fragment.background, ""])

    if fragment.core_content:
        # 如果 core_content 已包含 Markdown 标题（深度模式），直接渲染
        if fragment.core_content.strip().startswith("#"):
            body.extend([fragment.core_content, ""])
        else:
            body.extend(["## 核心内容", "", fragment.core_content, ""])

    if fragment.boundaries and (fragment.boundaries.get("applies") or fragment.boundaries.get("not_applies")):
        body.extend(["### 适用边界", ""])
        if fragment.boundaries.get("applies"):
            body.append(f"- 适用于：{fragment.boundaries['applies']}")
        if fragment.boundaries.get("not_applies"):
            body.append(f"- 不适用于：{fragment.boundaries['not_applies']}")
        body.append("")

    if fragment.anti_patterns:
        body.extend(["### 反模式/注意事项", ""])
        for ap in fragment.anti_patterns:
            body.append(f"- {ap}")
        body.append("")

    if fragment.self_check_issues:
        body.extend(["### 待验证项", ""])
        for issue in fragment.self_check_issues:
            body.append(f"- ⚠️ {issue}")
        body.append("")

    body.extend(["## 演化历史", "",
                 f"- v1: 初始记录（{datetime.now().strftime('%Y-%m-%d')}）", ""])

    # 合并 related_concepts 与 cross_agent_links，过滤无效/噪音链接
    all_related = []
    seen = set()
    for concept in list(fragment.related_concepts) + list(fragment.cross_agent_links):
        if not concept or concept in seen:
            continue
        seen.add(concept)
        # 过滤明显的中文切片噪音和无效链接
        if len(concept) < 2 or concept.startswith("待"):
            continue
        # 过滤停用短语（避免 [[在系统演进过]] 这类切片）
        if CrossAgentLinker._is_stop_phrase(concept):
            continue
        # 如果链接包含路径分隔符，检查目标文件是否存在；纯概念名则保留
        if "/" in concept or "\\" in concept:
            target_path = _get_wiki_dir() / f"{concept}.md"
            if not target_path.exists():
                continue
        all_related.append(concept)

    if all_related:
        body.extend(["## 相关链接", ""])
        for concept in all_related:
            body.append(f"- [[{concept}]]")
        body.append("")

    # 结构化关联说明（ADR-019：含关联上下文，便于人类阅读）
    if fragment.relations:
        body.extend(["## 关联说明", ""])
        for rel in fragment.relations:
            target = rel.get("target", "")
            rel_type = rel.get("type", "related_to")
            context = rel.get("context", "")
            body.append(f"- **{target}**（`{rel_type}`）: {context}")
        body.append("")

    # AI 关联扩充（与原始内容严格区分）
    if fragment.ai_expansion:
        body.extend(["## AI 关联扩充", ""])
        body.append("> ⚠️ **此区域内容由 AI 根据原始文档生成，属于关联性补充和建议，"
                   "可能与作者原意存在偏差。请结合原始内容独立判断。**")
        body.append("")
        body.append(fragment.ai_expansion)
        body.append("")

    # 来源追踪区（L1→L2 可追溯性）
    body.extend(["## 来源追踪", ""])
    body.append(f"- 来源会话: `{session_id}`")
    if source:
        body.append(f"- 来源 Agent: {source}")
    # 从 frontmatter 中读取原始 Memos 来源（如果存在）
    original_source = (fragment.frontmatter or {}).get("来源", (fragment.frontmatter or {}).get("source", ""))
    if original_source and original_source != source:
        body.append(f"- 原始来源: {original_source}")
    body.append(f"- 蒸馏时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    # 若存在 memos_wiki_link 可追加 memos_id（由调用方在 frontmatter 中注入）
    memos_id = (fragment.frontmatter or {}).get("memos_id", "")
    if memos_id:
        body.append(f"- 原始 Memos ID: `{memos_id}`")
    body.append("")

    return "\n".join(lines + [""] + body)


def process_doc_session(sid: str, messages: list, meta: dict, inbox: Path) -> int:
    """处理外部文档 session：直接解析内容生成 wiki 页面，不走 LLM 蒸馏

    用于 DocumentProcessor 导入的 PPT/PDF/Excel/Book 等外部文档，
    这些文档已有结构化内容，无需 LLM 二次蒸馏。
    """
    if not messages:
        return 0
    content = messages[0].get("content", "")
    if not content:
        return 0

    # 解析标题和文档类型（从第一行，如 "# 📄 PDF: 文件名"）
    title_match = re.search(r'^#\s+[^\s]+\s+(\w+):\s*(.+)$', content, re.MULTILINE)
    doc_type = "document"
    title = sid
    if title_match:
        doc_type = title_match.group(1).strip().lower()
        title = title_match.group(2).strip()

    # 解析元数据块（如果存在）
    meta_block = re.search(r'## 元数据\n\n(.+?)(?=\n## |\Z)', content, re.DOTALL)
    summary = ""
    filename = ""
    if meta_block:
        for line in meta_block.group(1).split('\n'):
            if '文档类型' in line:
                doc_type = line.split(':')[-1].strip().lower()
            elif '内容摘要' in line:
                summary = line.split(':', 1)[-1].strip()
            elif '原始文件' in line:
                filename = line.split(':', 1)[-1].strip()

    # 解析 JSON 元数据
    json_match = re.search(r'```json\n(.+?)\n```', content, re.DOTALL)
    extra_meta = {}
    if json_match:
        try:
            extra_meta = json.loads(json_match.group(1))
        except Exception:
            pass

    # 提取正文内容（去掉元数据部分）
    body = content
    if '---\n\n## 元数据' in content:
        body = content.split('---\n\n## 元数据')[0].strip()

    # 映射文档类型到 form
    type_to_form = {
        "pdf": "reference",
        "ppt": "reference",
        "pptx": "reference",
        "xlsx": "reference",
        "xls": "reference",
        "csv": "reference",
        "book": "reference",
    }
    form = type_to_form.get(doc_type, "reference")

    # 提取关键词（从标题 + 正文）
    keywords = []
    text_for_kw = f"{title} {body}"
    keywords.extend(re.findall(r'[a-zA-Z_]{3,}', text_for_kw))
    keywords.extend(re.findall(r'[\u4e00-\u9fff]{2,6}', text_for_kw))
    keywords = list(dict.fromkeys(keywords))[:12]

    # 构建 KnowledgeFragment
    fragment = KnowledgeFragment(
        form=form,
        title=title,
        frontmatter={
            "领域": extra_meta.get("category", "外部文档"),
            "文档类型": doc_type,
            "来源文件": filename or extra_meta.get("filename", ""),
            "关键词": keywords,
        },
        background=summary or f"外部导入的 {doc_type.upper()} 文档",
        core_content=body,
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )

    md = generate_wiki_page(fragment, sid, source=meta.get("source", "unknown"))
    inbox_name = f"{sid[:8]}_{form}_1.md"
    (inbox / inbox_name).write_text(md, encoding="utf-8")
    return 1


# ========== 蒸馏引擎 ==========

class DistillationEngine:
    """七层蒸馏流水线引擎"""

    def __init__(self, wiki_base: str = None, caller: HostAgentCaller = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else _get_wiki_dir()
        self.inbox_dir = self.wiki_base / "00-Inbox"
        self._caller = caller or HostAgentCaller()
        self._noise_filter = NoiseFilter()
        self._value_prejudgment = ValuePrejudgment()
        self._llm_judge = LLMValueJudge(self._caller)
        self._extractor = KnowledgeExtractor(self._caller)
        self._self_check = DistillSelfCheck()
        self._cross_linker = CrossAgentLinker()          # 旧 Jaccard linker（L6 层保留）
        self._feedback_loop = DistillFeedbackLoop()
        self._kia_linker = None  # 懒加载：core.kia.cross_agent_linker.CrossAgentLinker

    def _get_kia_linker(self):
        """懒加载新的跨 Agent 关联器（阶段三接入）。"""
        if self._kia_linker is None:
            try:
                from core.kia.cross_agent_linker import CrossAgentLinker as KiaCrossAgentLinker
                self._kia_linker = KiaCrossAgentLinker(wiki_root=self.wiki_base)
            except Exception:
                logger.debug("KiaCrossAgentLinker not available", exc_info=True)
                self._kia_linker = False  # 标记为已尝试但失败
        return self._kia_linker if self._kia_linker is not False else None

    @staticmethod
    def _slugify(name: str) -> str:
        """将名称转为 URL/文件安全的 slug"""
        import re
        slug = name.lower().strip()
        # 保留中英文、数字、横线；其他字符替换为横线
        slug = re.sub(r"[^\w\u4e00-\u9fa5-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:64] if slug else "untitled"

    def process(self, session_id: str, messages: List[Dict],
                meta: Dict = None) -> DistillationResult:
        """运行七层蒸馏流水线

        Args:
            session_id: 会话 ID
            messages: 消息列表 [{role, content}, ...]
            meta: 元数据 {source, model, cwd, ...}

        Returns:
            DistillationResult 包含所有层的执行结果
        """
        result = DistillationResult(session_id=session_id)
        meta = meta or {}

        # ===== L1: 噪音过滤 =====
        filtered, noise_stats = self._noise_filter.filter(messages)
        result.layer_results.append(
            PipelineLayerResult(1, "noise_filter", True, noise_stats),
        )
        if not filtered:
            result.judgment = "skip"
            result.judgment_reason = "全部消息为噪声"
            return result

        # ===== L2: 价值预判 =====
        verdict, confidence = self._value_prejudgment.judge(filtered)
        result.prejudgment = verdict
        result.prejudgment_confidence = confidence
        result.layer_results.append(
            PipelineLayerResult(2, "value_prejudgment", True,
                                {"verdict": verdict, "confidence": round(confidence, 3)}),
        )

        if verdict == ValuePrejudgment.CERTAINLY_NO:
            result.judgment = "skip"
            result.judgment_reason = f"预判无价值 (confidence={confidence:.2f})"
            return result

        # ===== L3: LLM 语义判断 =====
        # CERTAINLY_YES 且高置信度时跳过 LLM，直接判 knowledge
        if verdict == ValuePrejudgment.CERTAINLY_YES and confidence > 0.85:
            judgment, judgment_reason = "knowledge", "预判高价值，跳过LLM判断"
            judgment_confidence = confidence
        else:
            session_text = build_session_text(filtered)
            judgment, judgment_reason, judgment_confidence = self._llm_judge.judge(
                session_text, session_id,
            )

        result.judgment = judgment
        result.judgment_reason = judgment_reason
        result.layer_results.append(
            PipelineLayerResult(3, "llm_value_judge", True,
                                {"judgment": judgment, "confidence": round(judgment_confidence, 3)}),
        )

        if judgment == "skill":
            result.skill_suggestion = self._extract_skill_suggestion(filtered)
            return result

        if judgment != "knowledge":
            return result

        # ===== L4: 知识提取 =====
        session_text = build_session_text(filtered)
        fragments = self._extractor.extract(session_text, session_id, result.analysis_type)
        result.fragments = fragments
        result.layer_results.append(
            PipelineLayerResult(4, "knowledge_extraction", bool(fragments),
                                {"fragment_count": len(fragments)}),
        )

        if not fragments:
            result.judgment = "skip"
            result.judgment_reason = "提取无有效知识片段"
            return result

        # ===== L5: 自检 =====
        check_passed, issues = self._self_check.check(fragments, filtered)
        result.self_check_passed = check_passed
        result.self_check_issues = issues
        result.layer_results.append(
            PipelineLayerResult(5, "self_check", check_passed,
                                {"issues": issues[:5]}),
        )

        # ===== L6: 跨 Agent 关联 =====
        linked_fragments = self._cross_linker.link(fragments)
        result.fragments = linked_fragments
        result.cross_agent_links = [
            link for f in linked_fragments for link in f.cross_agent_links
        ]
        result.layer_results.append(
            PipelineLayerResult(6, "cross_agent_linking", True,
                                {"links": len(result.cross_agent_links)}),
        )

        # ===== L7: 反馈循环 =====
        feedback_signals = self._feedback_loop.evaluate(result)
        result.layer_results.append(
            PipelineLayerResult(7, "feedback_loop", True,
                                {"signals": len(feedback_signals)}),
        )

        # 发射 knowledge_distilled 事件
        if result.judgment == "knowledge" and result.fragments:
            try:
                from core.mnemos_bus import publish_event
                for frag in result.fragments:
                    # NOTE: knowledge_distilled 事件改在 write_pages() 后由 distill_and_write() 统一发射
                    # 以确保 payload 包含完整的 wiki_pages 和 kg_input
                    pass
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        return result

    def write_pages(self, result: DistillationResult) -> List[str]:
        """将蒸馏结果写入 Wiki 页面，并触发跨 Agent 关联（阶段三）。"""
        written = []
        if result.judgment != "knowledge" or not result.fragments:
            return written

        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        file_fragments = []
        seen_slugs = set()
        for i, fragment in enumerate(result.fragments):
            # 用知识标题生成人类可读的文件名
            title = fragment.title or fragment.frontmatter.get("名称", "untitled")
            slug = self._slugify(title)
            # 去重：同名加序号
            original_slug = slug
            counter = 1
            while slug in seen_slugs:
                slug = f"{original_slug}-{counter}"
                counter += 1
            seen_slugs.add(slug)
            page_id = slug
            page_content = generate_wiki_page(fragment, result.session_id)
            file_path = self.inbox_dir / f"{page_id}.md"
            try:
                file_path.write_text(page_content, encoding="utf-8")
                written.append(str(file_path))
                file_fragments.append((file_path, fragment))
            except Exception:
                logger.warning("write_pages failed for %s", page_id, exc_info=True)
                continue

        # 阶段三：调用新 CrossAgentLinker 建立跨 Agent 关联
        linker = self._get_kia_linker()
        if linker and file_fragments:
            for file_path, fragment in file_fragments:
                try:
                    actions = linker.link_after_distill(file_path)
                    if actions:
                        # 将关联结果写入 frontmatter（只记录从当前页面出发的链接）
                        refs = [
                            {"page": str(a.to_page), "reason": a.reason,
                             "similarity": round(a.similarity, 4)}
                            for a in actions
                            if a.from_page == file_path
                        ]
                        fragment.frontmatter["cross_agent_refs"] = refs
                        # 更新文件 frontmatter（保留 body 中 linker 已注入的链接）
                        if refs:
                            self._update_frontmatter_field(
                                file_path, "cross_agent_refs", refs,
                            )
                except Exception:
                    logger.debug("Cross-agent linking failed for %s", file_path, exc_info=True)

        # 回写评分结果到 frontmatter（质量分/热度/使用次数）
        if file_fragments:
            try:
                from core.wiki_metrics import WikiMetrics
                metrics = WikiMetrics()
                for file_path, fragment in file_fragments:
                    rel_path = str(file_path.relative_to(self.wiki_base))
                    page = metrics.get_page(rel_path)
                    if page is None:
                        content = file_path.read_text(encoding="utf-8")
                        metrics.assess_quality(rel_path, content)
                        metrics.upsert_page(
                            rel_path,
                            title=fragment.title,
                            source_count=1,
                            heat_score=1.0,
                            heat_level="warm",
                        )
                        page = metrics.get_page(rel_path)
                    if page:
                        self._update_frontmatter_field(
                            file_path, "mnemos_quality_score", round(page.quality_score / 100, 2)
                        )
                        self._update_frontmatter_field(
                            file_path, "mnemos_heat_score", int(page.heat_score)
                        )
                        self._update_frontmatter_field(
                            file_path, "mnemos_usage_count", page.source_count
                        )
                        self._update_frontmatter_field(
                            file_path, "mnemos_last_scored", datetime.now().strftime("%Y-%m-%d")
                        )
            except Exception:
                logger.debug("Frontmatter metrics writeback failed", exc_info=True)

        # 发射 distill_complete 事件（阶段三：事件总线 wiring）
        if written:
            try:
                from core.mnemos_bus import publish_event
                for file_path, fragment in file_fragments:
                    publish_event("distill_complete", "distill", {
                        "page_path": str(file_path),
                        "title": fragment.title,
                        "session_id": result.session_id,
                        "form": fragment.form,
                    })
            except Exception:
                logger.debug("distill_complete event emit failed", exc_info=True)

        return written

    @staticmethod
    def _update_frontmatter_field(file_path: Path, key: str, value) -> None:
        """只更新 Markdown 文件的 YAML frontmatter 中指定字段，保留 body 不变。"""
        try:
            text = file_path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                return
            parts = text.split("---", 2)
            if len(parts) < 3:
                return
            import yaml
            fm = yaml.safe_load(parts[1]) or {}
            fm[key] = value
            fm = to_chinese_frontmatter(fm)
            new_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
            new_text = f"---\n{new_fm}---{parts[2]}"
            file_path.write_text(new_text, encoding="utf-8")
        except Exception:
            logger.debug("Frontmatter update failed for %s", file_path, exc_info=True)

    def _extract_skill_suggestion(self, messages: List[Dict]) -> str:
        """尝试从对话中提取 Skill 建议"""
        session_text = build_session_text(messages, max_chars=3000)
        prompt = (
            "从以下对话中，如果存在重复性任务，请给出一个简短的 Skill 名称和用途。\n"
            '输出 JSON: {"skill_name": "...", "skill_purpose": "..."}\n\n'
            f"对话内容：\n{session_text}"
        )
        result = self._caller.call(prompt, expect_json=True)
        if result and "skill_name" in result:
            return f"{result['skill_name']}: {result.get('skill_purpose', '')}"
        return ""

    # ---- 向后兼容接口 ----

    def process_session(self, session_id: str, messages: List[Dict],
                        meta: Dict = None) -> DistillationResult:
        """处理单个 session（兼容接口，委托给 process()）"""
        return self.process(session_id, messages, meta)

    def _parse_fragments(self, data: Dict) -> List[KnowledgeFragment]:
        """从解析后的 JSON 数据中提取知识片段（供外部复用）"""
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
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at distillation_engine.py", exc_info=True)
                continue
        return fragments


# ========== 向后兼容 ==========

class LLMProvider:
    """LLM 调用抽象基类（已废弃，保留兼容）"""
    def call(self, prompt: str, max_tokens: int = 8000) -> str:
        raise NotImplementedError


class AgentDelegateProvider(LLMProvider):
    """委托本地 Agent 执行蒸馏（已废弃，保留兼容）"""
    def __init__(self, timeout: int = 300):
        self.timeout = timeout

    def call(self, prompt: str, max_tokens: int = 8000) -> str:
        caller = HostAgentCaller(timeout=self.timeout)
        result = caller.call(prompt, expect_json=False)
        if result and "raw" in result:
            return result["raw"]
        raise RuntimeError("无可用 Agent 执行蒸馏任务")


def distill_session(session_id: str, messages: List[Dict],
                    wiki_base: str = None) -> DistillationResult:
    """便捷函数：蒸馏单个 session"""
    engine = DistillationEngine(wiki_base=wiki_base)
    return engine.process(session_id, messages)


def _record_memos_wiki_links(session_id: str, wiki_page_paths: List[str]) -> None:
    """将 Memos UID 与 Wiki 页面路径建立映射，写入 sync_log.db 的 memos_wiki_link 表"""
    from core.config import get_config
    db_path = get_config().data_dir / "sync_log.db"
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        # 1. 更新 sync_log 的 wiki_page_paths 和 distill_status
        conn.execute(
            """UPDATE sync_log
               SET wiki_page_paths = ?, distill_status = 'distilled', distilled_at = ?
               WHERE session_id = ?""",
            (json.dumps(wiki_page_paths), datetime.now().isoformat(), session_id),
        )
        # 2. 查询该 session 的所有 memos_uids
        cursor = conn.execute(
            "SELECT memos_uids FROM sync_log WHERE session_id = ?",
            (session_id,),
        )
        all_uids = set()
        for row in cursor.fetchall():
            if row[0]:
                try:
                    uids = json.loads(row[0])
                    if isinstance(uids, list):
                        all_uids.update(uids)
                except Exception:
                    pass
        # 3. 写入 memos_wiki_link
        if all_uids and wiki_page_paths:
            for uid in all_uids:
                for wpath in wiki_page_paths:
                    conn.execute(
                        """INSERT OR IGNORE INTO memos_wiki_link
                           (memos_uid, wiki_page_path, link_type, created_at)
                           VALUES (?, ?, 'distilled', ?)""",
                        (uid, wpath, datetime.now().isoformat()),
                    )
        conn.commit()
        conn.close()
    except Exception:
        logging.getLogger(__name__).warning(f"memos_wiki_link 记录失败", exc_info=True)


def _emit_knowledge_distilled(session_id: str, result: DistillationResult, written: List[str]) -> None:
    """发射 knowledge_distilled 事件（公共函数，供所有写页入口复用）"""
    if not written or not result.fragments:
        return
    try:
        from core.mnemos_bus import publish_event
        entities = []
        relations = []
        for frag in result.fragments:
            entities.extend(frag.keywords or [])
            entities.extend(frag.related_concepts or [])
            # cross_agent_links（传统反向链接）
            for link in frag.cross_agent_links or []:
                relations.append({
                    "source": frag.title,
                    "target": link,
                    "type": "related_to",
                    "confidence": 0.5,
                })
            # 结构化关联上下文（ADR-019）
            for rel in frag.relations or []:
                relations.append({
                    "source": frag.title,
                    "target": rel.get("target", "").strip("[]"),
                    "type": rel.get("type", "related_to"),
                    "context": rel.get("context", ""),
                    "confidence": 0.7,  # LLM 推断的关联，置信度高于规则提取
                })
        publish_event("knowledge_distilled", "distill", {
            "session_id": session_id,
            "wiki_pages": written,
            "kg_input": {
                "entities": list(set(entities)),
                "relations": relations,
            },
        })
    except Exception:
        logging.getLogger(__name__).warning(f"knowledge_distilled event emit failed", exc_info=True)


def distill_and_write(session_id: str, messages: List[Dict],
                      wiki_base: str = None) -> Tuple[DistillationResult, List[str]]:
    """便捷函数：蒸馏并写入 Wiki"""
    engine = DistillationEngine(wiki_base=wiki_base)
    result = engine.process(session_id, messages)
    written = engine.write_pages(result)

    if written and result.fragments:
        _emit_knowledge_distilled(session_id, result, written)
        _record_memos_wiki_links(session_id, written)

    return result, written
