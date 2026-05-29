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
from core.frontmatter import to_chinese_frontmatter
from core.kia.ingest_helpers import is_noise_message



logger = logging.getLogger(__name__)
def _get_wiki_dir() -> Path:
    return get_config().wiki_dir


def _get_wiki_db() -> Path:
    return Path.home() / ".mnemos" / "wiki_state.db"


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
    # 七层流水线扩展字段
    self_check_passed: bool = True
    self_check_issues: List[str] = field(default_factory=list)
    cross_agent_links: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


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


def build_session_text(messages: List[Dict], max_chars: int = 12000) -> str:
    """从消息列表构建对话文本"""
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
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n...(session truncated)"
    return full_text


# ========== JSON 解析容错 ==========

def extract_json(text: str) -> Optional[Dict]:
    """从文本中提取 JSON，带容错处理"""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    fixed = re.sub(r',(\s*[}\]])', r'\1', text)
    fixed = fixed.replace("'", '"')
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    return None


# ========== HostAgentCaller — 宿主 Agent 调用器 ==========

class HostAgentCaller:
    """宿主 Agent 调用器 — 同源复用

    优先级：claude -p → kimi --print → AgentDelegate 异步
    """

    MAX_RETRIES = 2
    TIMEOUT = 60

    def __init__(self, timeout: int = None):
        self._timeout = timeout or self.TIMEOUT

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
        raw = self._try_cli("claude", ["-p", prompt], timeout)
        if raw is not None:
            return raw
        raw = self._try_cli("kimi", ["--print", prompt], timeout)
        if raw is not None:
            return raw
        return self._try_delegate(prompt, timeout)

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
        """从 LLM JSON 输出解析知识片段"""
        fragments = []
        for frag_data in data.get("fragments", []):
            try:
                fm = frag_data.get("frontmatter", {})
                kw = fm.get("关键词", {})
                keywords = []
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
        """LLM 不可用时的规则级降级提取"""
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
            fragments.append(KnowledgeFragment(
                form=form_value,
                title=best.claim[:80],
                frontmatter={
                    "类型": form_value,
                    "置信度": best.confidence,
                    "证据级别": best.evidence_level,
                    "时效性": best.temporal_scope or "contextual",
                    "提取方式": "rule_fallback",
                },
                background=best.context[:300] if best.context else "",
                core_content="\n".join(f"- {a.claim}" for a in form_assertions[:10]),
                boundaries={"applies": best.boundary_hint} if best.boundary_hint else {},
                anti_patterns=[a.claim for a in form_assertions if a.is_negated],
                related_concepts=[],
                keywords=re.findall(r'[a-zA-Z_]{3,}', best.claim),
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

    JACCARD_THRESHOLD = 0.3

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
        content_words = re.findall(r'[一-龥]{2,4}', frag.core_content[:500])
        kw.update(content_words)
        return kw

    def _text_to_keywords(self, text: str) -> set:
        """从文本提取关键词集合"""
        kw = set()
        kw.update(w.lower() for w in re.findall(r'[a-zA-Z_]{3,}', text))
        kw.update(re.findall(r'[一-龥]{2,4}', text))
        return kw

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
        """评估蒸馏结果，生成反馈信号"""
        signals = []

        # 信号1：预判与最终判断不一致
        if result.prejudgment == ValuePrejudgment.CERTAINLY_NO and result.judgment == "knowledge":
            signals.append({
                "type": "prejudgment_mismatch",
                "dimension": "distill_score",
                "expected": 0.3,
                "actual": 0.7,
                "reason": "预判为低价值但LLM判断为知识，应调高预判阈值",
            })

        # 信号2：预判为高价值但LLM跳过
        if result.prejudgment == ValuePrejudgment.CERTAINLY_YES and result.judgment == "skip":
            signals.append({
                "type": "prejudgment_mismatch",
                "dimension": "distill_score",
                "expected": 0.7,
                "actual": 0.3,
                "reason": "预判为高价值但LLM判断为跳过，应调低预判阈值",
            })

        # 信号3：自检失败率
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

        # 信号4：零提取（有价值判断但无片段）
        if result.judgment == "knowledge" and not result.fragments:
            signals.append({
                "type": "zero_extraction",
                "dimension": "distill_score",
                "expected": 0.6,
                "actual": 0.2,
                "reason": "判断为知识但提取无片段，提取逻辑需改善",
            })

        # ── 写入 V1 AdaptiveScorer（保留向后兼容）──
        scorer = self._get_scorer()
        if scorer and signals:
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
            except Exception:
                logging.getLogger(__name__).warning("V1 feedback dispatch failed", exc_info=True)

        # ── 写入 V2 ground_truth_signals（阶段二桥接）──
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            for sig in signals:
                # 信号转为二元标签：expected > actual → 正例（模型低估了）
                label = 1 if sig["expected"] > sig["actual"] else 0
                AdaptiveScorerV2.insert_ground_truth(
                    session_id=getattr(result, "session_id", "unknown"),
                    signal_type=sig["type"],
                    label=label,
                    confidence=abs(sig["expected"] - sig["actual"]),
                )
        except Exception:
            logging.getLogger(__name__).debug("V2 ground_truth insert failed", exc_info=True)

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


def generate_wiki_page(fragment: KnowledgeFragment, session_id: str,
                       source: str = "") -> str:
    """生成 wiki 页面 Markdown"""
    defaults = {
        "type": fragment.form or "knowledge",
        "name": fragment.title,
        "domain": "未分类",
        "summary": (fragment.title or fragment.core_content or "")[:80],
        "status": "草稿",
        "knowledge_stage": "原始",
        "source_count": 1,
        "evidence_level": "单源",
        "confidence": 0.5,
        "temporal_scope": "上下文相关",
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "source_session": session_id[:8],
    }
    fm = to_chinese_frontmatter(fragment.frontmatter, defaults)
    lines = ["---"]
    ordered_keys = [
        "类型", "名称", "领域", "摘要", "状态", "知识阶段",
        "来源数量", "证据级别", "置信度", "时效性", "创建日期", "来源会话",
        "关键词", "触发器", "别名", "版本标记", "决策摘要", "合并来源",
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

    # 保留少量历史中文字段，避免旧 Prompt 输出的信息丢失。
    for key in ("适用角色", "触发场景", "复杂度", "情感倾向", "提取方式"):
        value = (fragment.frontmatter or {}).get(key)
        if value:
            if isinstance(value, (list, dict)):
                lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                lines.append(f"{key}: {_yaml_safe(value)}")

    if not fragment.self_check_passed:
        lines.append(f"验证状态: pending-verification")

    lines.append("---")

    body = [f"# {fragment.title}", ""]

    if fragment.background:
        body.extend(["## 背景", "", fragment.background, ""])

    if fragment.core_content:
        body.extend(["## 核心内容", "", fragment.core_content, ""])

    if fragment.boundaries:
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

    all_related = list(fragment.related_concepts)
    for link in fragment.cross_agent_links:
        if link not in all_related:
            all_related.append(link)
    if all_related:
        body.extend(["## 相关链接", ""])
        for concept in all_related:
            body.append(f"- [[{concept}]]")
        body.append("")

    return "\n".join(lines + [""] + body)


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
                    publish_event("knowledge_distilled", "distill", {
                        "fragment_id": f"{session_id}_{frag.form}",
                        "form": frag.form,
                        "title": frag.title,
                        "session_id": session_id,
                    })
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
        for i, fragment in enumerate(result.fragments):
            page_id = f"{result.session_id[:8]}_{fragment.form}_{i + 1}"
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
                        # 将关联结果写入 frontmatter（结构化查询用）
                        refs = [
                            {"page": str(a.to_page), "reason": a.reason,
                             "similarity": round(a.similarity, 4)}
                            for a in actions
                        ]
                        fragment.frontmatter["cross_agent_refs"] = refs
                        # 更新文件 frontmatter（保留 body 中 linker 已注入的链接）
                        self._update_frontmatter_field(
                            file_path, "cross_agent_refs", refs,
                        )
                except Exception:
                    logger.debug("Cross-agent linking failed for %s", file_path, exc_info=True)

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


def distill_and_write(session_id: str, messages: List[Dict],
                      wiki_base: str = None) -> Tuple[DistillationResult, List[str]]:
    """便捷函数：蒸馏并写入 Wiki"""
    engine = DistillationEngine(wiki_base=wiki_base)
    result = engine.process(session_id, messages)
    written = engine.write_pages(result)
    return result, written
