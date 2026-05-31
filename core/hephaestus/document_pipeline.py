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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.hephaestus.distillation_engine import (
    DistillSelfCheck, CrossAgentLinker, generate_wiki_page,
    KnowledgeFragment, HostAgentCaller, FORM_TO_ENTITY_TYPE,
)

logger = logging.getLogger(__name__)


# ========== 文档专用 Prompts ==========

DOCUMENT_JUDGE_PROMPT = '''你是一个文档价值判断器。请分析以下文档，判断其类型和知识价值。

**文档信息：**
- 标题：{title}
- 类型：{doc_type}
- 页数/章节数：{page_count}
- 目录/大纲：
{outline}

**文档前 2000 字内容预览：**
{content_preview}

**判断任务：**

1. `judgment`：该文档是否值得索引到个人知识库？
   - `index`：值得详细索引（书籍、经典方法论、高质量报告）
   - `reference`：值得保留但不需要深度蒸馏（手册、参考资料、普通文档）
   - `skip`：无需索引（空白、重复、纯广告）

2. `doc_category`：文档类别
   - `book`：书籍/专著（有完整章节结构，系统阐述某个领域）
   - `strategy`：策略/方案/计划（有目标、策略、执行步骤）
   - `data`：数据/报表/看板（以数据表格、统计为主）
   - `report`：报告/总结（述职、复盘、调研报告）
   - `manual`：手册/指南（操作步骤、规范、SOP）
   - `reference`：参考资料（字典、百科、论文）

3. `entity_type`：映射到知识库实体类型
   - `book` → `concept`（通用知识/方法论）
   - `strategy` → `project`（项目/策略）
   - `data` → `dataset`（数据集/洞察）
   - `report` → `retrospective`（复盘/总结）
   - `manual` → `technology`（技术/工具）
   - `reference` → `technology`（参考资料）

4. `key_topics`：文档涉及的 3-7 个核心主题词

5. `audience`：目标读者（如"管理者""运营人员""技术人员"）

6. `why`：判断理由（1-2句话）

输出严格 JSON 格式，不要 markdown 代码块标记：
{{
  "judgment": "index|reference|skip",
  "doc_category": "book|strategy|data|report|manual|reference",
  "entity_type": "concept|project|dataset|retrospective|technology",
  "key_topics": ["主题1", "主题2"],
  "audience": "...",
  "why": "..."
}}
'''


BOOK_METHODOLOGY_PROMPT = '''你是一位严格的知识蒸馏专家。你的任务是从书籍章节中**客观提取作者表达的内容**，不允许添加你自己的意见、评价或补充。

## 🔴 铁律：客观性要求

1. **禁止添加你的观点**：你只能复述和结构化作者明确表达的内容，不能加入"我认为""值得注意的是""更重要的是"等主观评价。
2. **禁止补充案例**：如果作者没有提供某个场景的例子，你不能编造。你只能提取作者已经给出的例子，并将其抽象为通用表述。
3. **禁止价值判断**：不能说"这个方法很好""这个理论有局限性"，只能陈述作者的观点本身。
4. **禁止延伸推理**：不能从作者的观点推出作者没有明确说的结论。

## 输出结构

输出严格 JSON，包含两个独立区域：

```json
{{
  "objective_extraction": {{
    "methodologies": [
      {{
        "name": "作者命名的方法论（保持原名，不改造）",
        "principle": "作者阐述的核心原理（只引述作者观点）",
        "how_it_works": "作者描述的运作方式",
        "key_elements": ["作者明确列出的要素"],
        "boundaries": "作者提到的适用边界（如果没有明确说，写'作者未明确说明'）",
        "anti_patterns": ["作者提到的常见误用"]
      }}
    ],
    "mental_models": [
      {{
        "name": "作者使用的心智模型名称",
        "description": "作者对该模型的描述",
        "application": "作者提到的应用场景"
      }}
    ],
    "action_principles": [
      "作者明确给出的行动原则或判断标准"
    ],
    "key_examples": [
      "作者提供的核心案例（如有）——保留案例但抽象掉具体公司/人名"
    ]
  }},
  "ai_expansion": {{
    "related_concepts": ["与本书主题相关的其他知识领域（AI建议）"],
    "potential_blindspots": ["读者可能忽略的视角或反方观点（AI提醒）"],
    "practice_suggestions": ["将本书方法应用于不同场景的补充建议（AI建议，非作者原意）"],
    "critical_questions": ["值得进一步思考的问题（AI提出）"]
  }}
}}
```

## 字段说明

- `objective_extraction`：必须严格基于本章内容，**零添加**
- `ai_expansion`：AI 基于个人知识库和通识的**关联性补充**，必须与 `objective_extraction` 物理分离
- 如果某个字段作者没有提及，写空数组 `[]` 或空字符串 `""`，不要编造

**输入：书籍章节内容**
{book_content}
'''


DATA_INSIGHT_PROMPT = '''你是一位数据分析师。请从以下数据报表中提取关键洞察。

**数据内容：**
{data_content}

**提取要求：**

输出严格 JSON：
```json
{{
  "data_profile": {{
    "scope": "数据覆盖范围",
    "time_range": "时间范围",
    "key_metrics": ["指标1", "指标2"]
  }},
  "insights": [
    {{
      "observation": "观察到的现象/趋势",
      "evidence": "支撑数据（具体数字）",
      "implication": "业务/决策含义",
      "confidence": "高|中|低"
    }}
  ],
  "anomalies": [
    {{
      "description": "异常描述",
      "data_point": "具体数据",
      "possible_cause": "可能原因"
    }}
  ],
  "recommendations": [
    "基于数据的可行动建议"
  ]
}}
```

**规则：**
- 每个洞察必须有具体数字支撑
- 区分"相关性"和"因果性"
- 标注置信度，不确定的用"低"
'''


STRATEGY_EXTRACT_PROMPT = '''你是一位策略分析专家。请客观提取以下方案/计划文档中的内容，不添加你的主观评价。

**文档内容：**
{strategy_content}

**提取要求：**

输出严格 JSON，包含两个区域：

```json
{{
  "objective_extraction": {{
    "strategy_overview": {{
      "goal": "核心目标",
      "timeframe": "时间框架",
      "target_audience": "目标对象"
    }},
    "key_decisions": [
      {{
        "decision": "决策内容",
        "rationale": "决策理由",
        "alternatives_considered": "考虑过的替代方案",
        "risks": ["风险1", "风险2"]
      }}
    ],
    "action_items": [
      {{
        "action": "行动项",
        "owner": "负责人（如有）",
        "deadline": "时间节点（如有）",
        "success_criteria": "成功标准"
      }}
    ],
    "methodologies": [
      {{
        "name": "使用的通用方法论/框架",
        "how_applied": "如何在本方案中应用"
      }}
    ],
    "lessons_learned": [
      "可复用的经验教训"
    ]
  }},
  "ai_expansion": {{
    "related_concepts": ["相关的通用方法论或理论模型（AI建议）"],
    "potential_blindspots": ["该策略可能忽略的视角或风险（AI提醒）"],
    "practice_suggestions": ["将该方法论应用于其他场景的建议（AI建议）"],
    "critical_questions": ["值得进一步思考的问题（AI提出）"]
  }}
}}
```

**规则：**
- objective_extraction 必须严格基于文档内容，零添加
- 将具体业务动作抽象为通用方法论
- 保留决策逻辑，去掉具体人名/公司名
- ai_expansion 是 AI 关联补充，必须与客观提取分离
'''


REPORT_SUMMARY_PROMPT = '''你是一位复盘分析专家。请从以下报告/总结中提取关键结论和可复用经验。

**报告内容：**
{report_content}

**提取要求：**

输出严格 JSON：
```json
{{
  "report_meta": {{
    "period": "时间周期",
    "scope": "覆盖范围"
  }},
  "key_achievements": [
    {{
      "achievement": "关键成果",
      "metrics": "支撑数据",
      "factors": "成功因素"
    }}
  ],
  "key_challenges": [
    {{
      "challenge": "关键挑战",
      "root_cause": "根因分析",
      "lesson": "经验教训"
    }}
  ],
  "decisions_made": [
    {{
      "decision": "做出的决策",
      "outcome": "结果",
      "retrospective": "复盘：如果重来会怎么做"
    }}
  ],
  "patterns_identified": [
    "发现的模式/规律"
  ],
  "reusable_methods": [
    {{
      "method": "可复用的方法",
      "context": "适用场景",
      "effectiveness": "有效程度"
    }}
  ]
}}
```
'''


# ========== 数据模型 ==========

@dataclass
class DocumentJudgeResult:
    """文档价值判断结果"""
    judgment: str = "skip"           # index / reference / skip
    doc_category: str = "reference"  # book / strategy / data / report / manual / reference
    entity_type: str = "technology"  # concept / project / dataset / retrospective / technology
    key_topics: List[str] = field(default_factory=list)
    audience: str = ""
    why: str = ""
    confidence: float = 0.0


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


# ========== LLM Judge ==========

class DocumentLLMJudge:
    """文档价值判断器 — 判定文档是否值得索引，以及文档类别"""

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()

    def judge(self, title: str, doc_type: str, content: str,
              metadata: Dict, session_id: str = "") -> DocumentJudgeResult:
        """判断文档价值和类别"""
        # 提取目录/大纲（前 3000 字）
        preview = content[:3000]
        outline = self._extract_outline(content)
        page_count = metadata.get("pages", metadata.get("slides", metadata.get("chapters", 0)))

        prompt = DOCUMENT_JUDGE_PROMPT.replace("{title}", title)
        prompt = prompt.replace("{doc_type}", doc_type)
        prompt = prompt.replace("{page_count}", str(page_count))
        prompt = prompt.replace("{outline}", outline or "无目录")
        prompt = prompt.replace("{content_preview}", preview)

        result = self._caller.call(prompt, expect_json=True)
        if result is None:
            logger.warning(f"[DocJudge] LLM 调用失败，使用规则回退")
            return self._fallback_judge(title, doc_type, content, metadata)

        try:
            data = result if isinstance(result, dict) else json.loads(result.get("raw", "{}"))
        except Exception:
            data = {}

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
        """从内容中提取目录/章节结构"""
        # 匹配 Markdown 标题层级
        headings = re.findall(r'^#{1,3}\s+(.+)$', content, re.MULTILINE)
        if headings:
            return "\n".join(f"- {h}" for h in headings[:20])
        return ""

    def _fallback_judge(self, title: str, doc_type: str,
                        content: str, metadata: Dict) -> DocumentJudgeResult:
        """规则回退判断"""
        # 简单规则
        if doc_type in ("pdf", "epub") and metadata.get("pages", 0) > 50:
            return DocumentJudgeResult(
                judgment="index", doc_category="book",
                entity_type="concept", key_topics=[],
                confidence=0.6
            )
        if doc_type in ("xlsx", "xls", "csv"):
            return DocumentJudgeResult(
                judgment="index", doc_category="data",
                entity_type="dataset", key_topics=[],
                confidence=0.6
            )
        if doc_type in ("ppt", "pptx"):
            return DocumentJudgeResult(
                judgment="index", doc_category="report",
                entity_type="retrospective", key_topics=[],
                confidence=0.6
            )
        return DocumentJudgeResult(
            judgment="reference", doc_category="reference",
            entity_type="technology", key_topics=[],
            confidence=0.5
        )


# ========== 知识提取器 ==========

class DocumentKnowledgeExtractor:
    """文档知识提取器 — 按文档类别使用不同策略提取结构化知识"""

    def __init__(self, caller: HostAgentCaller = None):
        self._caller = caller or HostAgentCaller()

    def extract(self, content: str, judge_result: DocumentJudgeResult,
                session_id: str = "") -> Tuple[List[KnowledgeFragment], Dict]:
        """按文档类别提取知识片段和结构化数据"""
        category = judge_result.doc_category

        if category == "book":
            return self._extract_book(content, judge_result)
        elif category == "data":
            return self._extract_data(content, judge_result)
        elif category == "strategy":
            return self._extract_strategy(content, judge_result)
        elif category == "report":
            return self._extract_report(content, judge_result)
        else:
            # manual / reference / 其他
            return self._extract_generic(content, judge_result)

    def _extract_book(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取书籍中的通用方法论 — 全量章节处理（串行，避免 CLI 竞争）"""
        chunks = self._chunk_by_chapters(content)
        logger.info(f"[DocExtractor] 书籍共 {len(chunks)} 章，开始全量蒸馏...")

        all_fragments = []
        all_methodologies = []
        all_models = []
        all_ai_expansions = []

        for i, chunk in enumerate(chunks):
            logger.info(f"[DocExtractor] 蒸馏第 {i+1}/{len(chunks)} 章...")
            prompt = BOOK_METHODOLOGY_PROMPT.replace("{book_content}", chunk[:15000])
            result = self._caller.call(prompt, expect_json=True, timeout=60, max_retries=0)
            if result is None:
                logger.warning(f"[DocExtractor] 第 {i+1} 章 LLM 调用失败，跳过")
                continue

            try:
                data = result if isinstance(result, dict) else json.loads(result.get("raw", "{}"))
            except Exception as e:
                logger.warning(f"[DocExtractor] 第 {i+1} 章 JSON 解析失败: {e}")
                continue

            # 解析客观提取区域
            obj = data.get("objective_extraction", data)  # 兼容旧格式

            # 提取方法论
            for m in obj.get("methodologies", []):
                all_methodologies.append(m)
                frag = self._methodology_to_fragment(m, judge)
                if frag:
                    all_fragments.append(frag)

            # 提取心智模型
            for mm in obj.get("mental_models", []):
                all_models.append(mm)
                frag = self._mental_model_to_fragment(mm, judge)
                if frag:
                    all_fragments.append(frag)

            # 提取行动原则
            for ap in obj.get("action_principles", []):
                frag = self._action_principle_to_fragment(ap, judge)
                if frag:
                    all_fragments.append(frag)

            # 提取核心案例
            for ex in obj.get("key_examples", []):
                frag = self._example_to_fragment(ex, judge)
                if frag:
                    all_fragments.append(frag)

            # 收集 AI 扩充（独立区域）
            ai_exp = data.get("ai_expansion", {})
            if ai_exp:
                all_ai_expansions.append(ai_exp)

        # 合并所有 AI 扩充为一个字符串
        merged_ai_expansion = self._merge_ai_expansions(all_ai_expansions)

        # 将所有 fragment 的 ai_expansion 设置为合并后的内容
        for frag in all_fragments:
            frag.ai_expansion = merged_ai_expansion

        # 去重（按名称）
        seen = set()
        unique_fragments = []
        for f in all_fragments:
            if f.title not in seen:
                seen.add(f.title)
                unique_fragments.append(f)

        logger.info(f"[DocExtractor] 书籍蒸馏完成：{len(unique_fragments)} 个唯一知识片段")
        return unique_fragments, {
            "methodologies": all_methodologies,
            "mental_models": all_models,
            "ai_expansions": all_ai_expansions,
        }

    def _extract_data(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取数据洞察"""
        prompt = DATA_INSIGHT_PROMPT.replace("{data_content}", content[:10000])
        result = self._caller.call(prompt, expect_json=True)

        if result is None:
            return self._fallback_data_fragment(content, judge)

        try:
            data = result if isinstance(result, dict) else json.loads(result.get("raw", "{}"))
        except Exception:
            return self._fallback_data_fragment(content, judge)

        fragments = []
        ai_expansions = []

        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 数据画像
        profile = data.get("data_profile", {})

        # 洞察 → 知识片段
        for ins in data.get("insights", []):
            frag = KnowledgeFragment(
                form="data-insight",
                title=ins.get("observation", "数据洞察")[:60],
                frontmatter={
                    "领域": "数据分析",
                    "证据级别": ins.get("confidence", "中"),
                },
                background=f"数据来源：{profile.get('scope', '未知')}",
                core_content=f"**观察**：{ins.get('observation', '')}\n\n"
                           f"**证据**：{ins.get('evidence', '')}\n\n"
                           f"**含义**：{ins.get('implication', '')}",
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 异常 → 反模式片段
        for anom in data.get("anomalies", []):
            frag = KnowledgeFragment(
                form="反模式",
                title=f"异常：{anom.get('description', '')[:50]}",
                frontmatter={"领域": "数据分析"},
                background=anom.get("description", ""),
                core_content=f"**数据点**：{anom.get('data_point', '')}\n\n"
                           f"**可能原因**：{anom.get('possible_cause', '')}",
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 建议 → 经验法则
        for rec in data.get("recommendations", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=f"建议：{rec[:50]}",
                frontmatter={"领域": "数据分析"},
                background="基于数据分析的建议",
                core_content=rec,
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai
        return fragments, data

    def _extract_strategy(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取策略/方案中的决策和方法论"""
        prompt = STRATEGY_EXTRACT_PROMPT.replace("{strategy_content}", content[:10000])
        result = self._caller.call(prompt, expect_json=True)

        if result is None:
            return self._fallback_generic_fragment(content, judge)

        try:
            data = result if isinstance(result, dict) else json.loads(result.get("raw", "{}"))
        except Exception:
            return self._fallback_generic_fragment(content, judge)

        fragments = []
        ai_expansions = []

        # 解析 objective_extraction（兼容旧格式）
        obj = data.get("objective_extraction", data)
        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 决策 → 决策记录
        for dec in obj.get("key_decisions", []):
            frag = KnowledgeFragment(
                form="决策记录",
                title=dec.get("decision", "决策")[:60],
                frontmatter={"领域": "策略规划"},
                background=dec.get("rationale", ""),
                core_content=f"**决策**：{dec.get('decision', '')}\n\n"
                           f"**理由**：{dec.get('rationale', '')}\n\n"
                           f"**替代方案**：{dec.get('alternatives_considered', '')}\n\n"
                           f"**风险**：{', '.join(dec.get('risks', []) or [])}",
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 方法论
        for meth in obj.get("methodologies", []):
            frag = KnowledgeFragment(
                form="方法论",
                title=meth.get("name", "方法论")[:60],
                frontmatter={"领域": "策略规划"},
                background=meth.get("how_applied", ""),
                core_content=meth.get("how_applied", ""),
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 经验教训
        for lesson in obj.get("lessons_learned", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=lesson[:60],
                frontmatter={"领域": "策略规划"},
                background="",
                core_content=lesson,
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai
        return fragments, data

    def _extract_report(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """提取报告/总结中的经验教训"""
        prompt = REPORT_SUMMARY_PROMPT.replace("{report_content}", content[:10000])
        result = self._caller.call(prompt, expect_json=True)

        if result is None:
            return self._fallback_generic_fragment(content, judge)

        try:
            data = result if isinstance(result, dict) else json.loads(result.get("raw", "{}"))
        except Exception:
            return self._fallback_generic_fragment(content, judge)

        fragments = []
        ai_expansions = []

        obj = data.get("objective_extraction", data)
        ai_exp = data.get("ai_expansion", {})
        if ai_exp:
            ai_expansions.append(ai_exp)

        # 成果 → 经验法则
        for ach in obj.get("key_achievements", []):
            frag = KnowledgeFragment(
                form="经验法则",
                title=ach.get("achievement", "成果")[:60],
                frontmatter={"领域": "复盘总结"},
                background=f"成功因素：{ach.get('factors', '')}",
                core_content=f"**成果**：{ach.get('achievement', '')}\n\n"
                           f"**数据**：{ach.get('metrics', '')}\n\n"
                           f"**成功因素**：{ach.get('factors', '')}",
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 挑战 → 反模式
        for chal in obj.get("key_challenges", []):
            frag = KnowledgeFragment(
                form="反模式",
                title=chal.get("challenge", "挑战")[:60],
                frontmatter={"领域": "复盘总结"},
                background=chal.get("root_cause", ""),
                core_content=f"**挑战**：{chal.get('challenge', '')}\n\n"
                           f"**根因**：{chal.get('root_cause', '')}\n\n"
                           f"**教训**：{chal.get('lesson', '')}",
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        # 可复用方法
        for method in obj.get("reusable_methods", []):
            frag = KnowledgeFragment(
                form="方法论",
                title=method.get("method", "方法")[:60],
                frontmatter={"领域": "复盘总结"},
                background=f"适用场景：{method.get('context', '')}",
                core_content=method.get("method", ""),
                boundaries={},
                anti_patterns=[],
                related_concepts=[],
            )
            fragments.append(frag)

        merged_ai = self._merge_ai_expansions(ai_expansions)
        for frag in fragments:
            frag.ai_expansion = merged_ai
        return fragments, data

    def _extract_generic(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """通用文档提取"""
        return self._fallback_generic_fragment(content, judge)

    # ===== 辅助方法 =====

    def _chunk_by_chapters(self, content: str) -> List[str]:
        """按章节分块（匹配 Markdown ## 标题）

        对于 PDF 按页提取的内容（大量 '## 第 X 页'），会智能合并页面为合理大小的 chunk。
        """
        # 按二级标题分割
        parts = re.split(r'\n(?=##\s)', content)
        if len(parts) <= 1:
            parts = re.split(r'\n(?=###\s)', content)

        # 检测是否是 PDF 按页模式（大量 "## 第 X 页" 标题）
        page_title_count = sum(1 for p in parts if re.match(r'^##\s+第\s*\d+\s*页', p.strip()))
        is_pdf_page_mode = page_title_count > len(parts) * 0.5

        if is_pdf_page_mode and len(parts) > 20:
            # PDF 按页模式：合并页面为更大的 chunk（每 chunk 约 12000-15000 字符）
            # 目标：300 页书 → ~13 个 chunk，减少 LLM 调用次数
            merged = []
            current_chunk = ""
            target_size = 50000
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
            chunk_size = 8000
            parts = [content[i:i+chunk_size] for i in range(0, len(content), chunk_size)]

        return [p for p in parts if p.strip()]

    def _methodology_to_fragment(self, m: Dict, judge: DocumentJudgeResult) -> Optional[KnowledgeFragment]:
        """方法论 → KnowledgeFragment"""
        name = m.get("name", "").strip()
        if not name:
            return None
        return KnowledgeFragment(
            form="方法论",
            title=name[:80],
            frontmatter={
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "通用方法论",
                "适用对象": judge.audience,
            },
            background=m.get("principle", ""),
            core_content=f"**原理**：{m.get('principle', '')}\n\n"
                       f"**运作方式**：{m.get('how_it_works', '')}\n\n"
                       f"**关键要素**：{', '.join(m.get('key_elements', []))}",
            boundaries={"applies": m.get("boundaries", ""), "not_applies": ""},
            anti_patterns=m.get("anti_patterns", []),
            related_concepts=[],
        )

    def _mental_model_to_fragment(self, mm: Dict, judge: DocumentJudgeResult) -> Optional[KnowledgeFragment]:
        """心智模型 → KnowledgeFragment"""
        name = mm.get("name", "").strip()
        if not name:
            return None
        return KnowledgeFragment(
            form="洞察关联",
            title=name[:80],
            frontmatter={
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "心智模型",
            },
            background=mm.get("description", ""),
            core_content=f"**模型描述**：{mm.get('description', '')}\n\n"
                       f"**应用场景**：{mm.get('application', '')}",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )

    def _example_to_fragment(self, ex: str, judge: DocumentJudgeResult) -> Optional[KnowledgeFragment]:
        """核心案例 → KnowledgeFragment"""
        if not ex or len(ex) < 10:
            return None
        title = ex.split("：")[0] if "：" in ex else ex[:40]
        return KnowledgeFragment(
            form="洞察关联",
            title=f"案例：{title[:50]}",
            frontmatter={
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "通用案例",
            },
            background="",
            core_content=ex,
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )

    def _merge_ai_expansions(self, expansions: List[Dict]) -> str:
        """合并多章节的 AI 扩充为一个字符串"""
        if not expansions:
            return ""

        lines = []

        # 收集所有相关概念
        all_concepts = set()
        for exp in expansions:
            for c in exp.get("related_concepts", []):
                all_concepts.add(c)
        if all_concepts:
            lines.extend(["### 相关概念", ""])
            for c in sorted(all_concepts):
                lines.append(f"- {c}")
            lines.append("")

        # 收集所有盲区提醒
        all_blindspots = set()
        for exp in expansions:
            for b in exp.get("potential_blindspots", []):
                all_blindspots.add(b)
        if all_blindspots:
            lines.extend(["### 盲区提醒", ""])
            for b in sorted(all_blindspots):
                lines.append(f"- {b}")
            lines.append("")

        # 收集所有实践建议
        all_suggestions = set()
        for exp in expansions:
            for s in exp.get("practice_suggestions", []):
                all_suggestions.add(s)
        if all_suggestions:
            lines.extend(["### 实践建议", ""])
            for s in sorted(all_suggestions):
                lines.append(f"- {s}")
            lines.append("")

        # 收集所有批判性问题
        all_questions = set()
        for exp in expansions:
            for q in exp.get("critical_questions", []):
                all_questions.add(q)
        if all_questions:
            lines.extend(["### 值得思考的问题", ""])
            for q in sorted(all_questions):
                lines.append(f"- {q}")
            lines.append("")

        return "\n".join(lines)

    def _action_principle_to_fragment(self, ap: str, judge: DocumentJudgeResult) -> Optional[KnowledgeFragment]:
        """行动原则 → KnowledgeFragment"""
        if not ap or len(ap) < 10:
            return None
        # 提取标题（冒号前的部分或前 30 字）
        title = ap.split("：")[0] if "：" in ap else ap[:30]
        return KnowledgeFragment(
            form="经验法则",
            title=title[:60],
            frontmatter={
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "通用原则",
            },
            background="",
            core_content=ap,
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )

    def _fallback_data_fragment(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """数据提取失败时的回退"""
        frag = KnowledgeFragment(
            form="data-insight",
            title="数据汇总",
            frontmatter={"领域": "数据分析"},
            background="",
            core_content=content[:5000],
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        return [frag], {}

    def _fallback_generic_fragment(self, content: str, judge: DocumentJudgeResult) -> Tuple[List[KnowledgeFragment], Dict]:
        """通用回退：提取核心内容作为 reference"""
        frag = KnowledgeFragment(
            form="reference",
            title=judge.key_topics[0] if judge.key_topics else "文档内容",
            frontmatter={
                "领域": ", ".join(judge.key_topics[:3]) if judge.key_topics else "外部文档",
            },
            background="",
            core_content=content[:8000],
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        return [frag], {}


# ========== 主管道 ==========

class DocumentDistillationPipeline:
    """文档蒸馏管道 — 外部文档深度处理的主入口"""

    def __init__(self, wiki_base: str = None, caller: HostAgentCaller = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else self._get_wiki_dir()
        self.inbox_dir = self.wiki_base / "00-Inbox"
        self._caller = caller or HostAgentCaller()
        self._judge = DocumentLLMJudge(self._caller)
        self._extractor = DocumentKnowledgeExtractor(self._caller)
        self._self_check = DistillSelfCheck()
        self._cross_linker = None  # 懒加载

    def _get_wiki_dir(self) -> Path:
        from core.config import get_config
        return get_config().wiki_dir

    def process(self, sid: str, messages: list, meta: dict) -> DocumentDistillResult:
        """处理文档 session，返回蒸馏结果"""
        if not messages:
            return DocumentDistillResult(session_id=sid, judgment="skip")

        content = messages[0].get("content", "")
        if not content:
            return DocumentDistillResult(session_id=sid, judgment="skip")

        # 提取标题和文档类型
        title, doc_type = self._parse_doc_header(content)
        filename = meta.get("filename", title)

        logger.info(f"[DocPipeline] 开始处理: {title} ({doc_type})")

        # Step 1: LLM 价值判断
        judge_result = self._judge.judge(
            title=title, doc_type=doc_type, content=content,
            metadata=meta, session_id=sid
        )
        logger.info(f"[DocPipeline] 判断结果: {judge_result.judgment} / {judge_result.doc_category} / {judge_result.entity_type}")

        # 写入文档信号（画像系统）
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
            logger.info(f"[DocPipeline] 文档信号已写入画像系统")
        except Exception as e:
            logger.debug(f"[DocPipeline] 文档信号写入失败: {e}")

        if judge_result.judgment == "skip":
            return DocumentDistillResult(
                session_id=sid, judgment="skip",
                doc_category=judge_result.doc_category
            )

        # Step 2: 知识提取
        fragments, structured_data = self._extractor.extract(
            content, judge_result, session_id=sid
        )
        logger.info(f"[DocPipeline] 提取 {len(fragments)} 个知识片段")

        # Step 3: 自检 (L5)
        self_check_issues = []
        for frag in fragments:
            # 文档蒸馏没有对话 messages，传入空列表
            try:
                issues = self._self_check._check_fragment(frag, [])
            except TypeError:
                # 兼容旧版本 _check_fragment 不需要 messages
                issues = self._self_check._check_fragment(frag)
            self_check_issues.extend(issues)
            frag.self_check_passed = len(issues) == 0
            frag.self_check_issues = issues

        # Step 4: 跨 Agent 关联 (L6)
        cross_links = []
        if fragments:
            try:
                linker = self._get_cross_linker()
                for frag in fragments:
                    # 为每个 fragment 生成临时页面路径进行关联
                    links = linker.link_after_distill_for_fragment(frag)
                    frag.cross_agent_links = [str(l.to_page) for l in links]
                    cross_links.extend(frag.cross_agent_links)
            except Exception as e:
                logger.debug(f"[DocPipeline] 跨 Agent 关联失败: {e}")

        return DocumentDistillResult(
            session_id=sid,
            judgment=judge_result.judgment,
            doc_category=judge_result.doc_category,
            fragments=fragments,
            book_meta=structured_data.get("book_meta") if judge_result.doc_category == "book" else None,
            data_insights=structured_data if judge_result.doc_category == "data" else None,
            strategy_items=structured_data if judge_result.doc_category == "strategy" else None,
            report_items=structured_data if judge_result.doc_category == "report" else None,
            self_check_issues=self_check_issues,
            cross_agent_links=cross_links,
        )

    def write_to_wiki(self, result: DocumentDistillResult, source: str = "") -> List[Path]:
        """将蒸馏结果写入 wiki Inbox"""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        written = []
        sid = result.session_id

        for i, frag in enumerate(result.fragments):
            md = generate_wiki_page(frag, sid, source=source)

            # 文件名：session前8位_形态_序号.md
            safe_form = frag.form.replace("-", "_").replace(" ", "_")
            filename = f"{sid[:8]}_{safe_form}_{i+1}.md"
            path = self.inbox_dir / filename
            path.write_text(md, encoding="utf-8")
            written.append(path)
            logger.info(f"[DocPipeline] 已写入 wiki: {path.name}")

        return written

    def _parse_doc_header(self, content: str) -> Tuple[str, str]:
        """从内容第一行解析文档标题和类型"""
        match = re.search(r'^#\s+[^\s]+\s+(\w+):\s*(.+)$', content, re.MULTILINE)
        if match:
            return match.group(2).strip(), match.group(1).strip().lower()
        return "未命名文档", "unknown"

    def _get_cross_linker(self):
        """懒加载跨 Agent 关联器"""
        if self._cross_linker is None:
            from core.kia.cross_agent_linker import CrossAgentLinker
            self._cross_linker = CrossAgentLinker(wiki_root=self.wiki_base)
        return self._cross_linker


# ========== 便捷函数 ==========

def process_doc_session(sid: str, messages: list, meta: dict, inbox: Path) -> int:
    """便捷的文档 session 处理入口（替换 distillation_engine.py 中的同名函数）

    这是向后兼容的包装函数，保持原有接口不变。
    """
    pipeline = DocumentDistillationPipeline()
    result = pipeline.process(sid, messages, meta)

    if result.judgment == "skip" or not result.fragments:
        return 0

    # 写入 wiki（如果 inbox 参数提供）
    if inbox:
        pipeline.inbox_dir = inbox
        pipeline.write_to_wiki(result, source=meta.get("source", "unknown"))

    return len(result.fragments)
