# -*- coding: utf-8 -*-
"""
Content Formatter - 知识内容多模态自动表达

根据知识内容特征，自动选择最佳表达形式：
- 多方案对比 → 对比矩阵表格
- 步骤/流程 → Mermaid 流程图
- 大量参数 → YAML/JSON 配置块
- 正反两面 → ✅/❌ 对照表
- 层级结构 → 嵌套列表或树形图

设计原则：
- 后处理增强，不改 LLM 原始输出
- 启发式检测，轻量快速
- 保留原始内容作为 fallback
- 用户可手动覆盖自动选择
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


class ExpressionForm(Enum):
    """表达形式枚举"""
    MARKDOWN_LIST = "markdown_list"      # 默认：Markdown 列表
    COMPARISON_TABLE = "comparison_table"  # 对比矩阵表格
    MERMAID_FLOW = "mermaid_flow"        # Mermaid 流程图
    CONFIG_BLOCK = "config_block"        # YAML/JSON 配置块
    PRO_CON_TABLE = "pro_con_table"      # ✅/❌ 对照表
    CHECKLIST = "checklist"              # 检查清单
    TREE_DIAGRAM = "tree_diagram"        # 树形决策图


@dataclass
class FormatSuggestion:
    """格式建议"""
    form: ExpressionForm
    confidence: float
    reason: str
    formatted_content: str = ""


class ContentFormatter:
    """内容格式化器"""

    # 检测规则（正则 + 关键词）
    DETECTION_RULES = [
        # 对比类
        {
            "form": ExpressionForm.COMPARISON_TABLE,
            "patterns": [
                r"(?:对比|比较|vs|versus|区别|差异|选.*还是|A.*B).*\n.*(?:优点|缺点|优势|劣势|适用|不适用)",
                r"(?:方案[一二三四五ABCDE]|选项[一二三四五]).*\n.*(?:特点|优劣|对比)",
            ],
            "keywords": ["对比", "比较", "vs", "versus", "区别", "差异", "优缺点", "优劣"],
            "weight": 1.0,
        },
        # 流程类
        {
            "form": ExpressionForm.MERMAID_FLOW,
            "patterns": [
                r"(?:步骤|流程|顺序|先.*再.*最后|第一步|阶段[一二三四五]).*\n.*(?:执行|操作|进入|判断)",
                r"(?:如果.*则|when.*then|if.*else).*\n.*(?:执行|返回|结束)",
            ],
            "keywords": ["步骤", "流程", "顺序", "先", "再", "最后", "第一步", "阶段", "如果", "则"],
            "weight": 0.9,
        },
        # 配置类
        {
            "form": ExpressionForm.CONFIG_BLOCK,
            "patterns": [
                r"(?:配置|参数|设置|选项|字段|属性).*\n.*[=:].*",
                r"(?:yaml|json|toml|env|config).*\n.*",
            ],
            "keywords": ["配置", "参数", "设置", "选项", "字段", "属性", "默认值"],
            "weight": 0.85,
        },
        # 正反类
        {
            "form": ExpressionForm.PRO_CON_TABLE,
            "patterns": [
                r"(?:不要|避免|切忌|禁止|不推荐).*\n.*(?:应该|推荐|建议|优先|尽量)",
                r"(?:错误|反模式|bad|wrong).*\n.*(?:正确|最佳|best|good)",
            ],
            "keywords": ["不要", "避免", "切忌", "应该", "推荐", "优先", "错误", "正确", "反模式"],
            "weight": 0.8,
        },
        # 检查清单类
        {
            "form": ExpressionForm.CHECKLIST,
            "patterns": [
                r"(?:检查清单|checklist|核对|确认|验证).*\n.*[\[\(].*[\]\)]",
                r"^(?:-|\*)\s*\[[\sx]\].*",
            ],
            "keywords": ["检查", "核对", "确认", "验证", "清单", "checklist"],
            "weight": 0.75,
        },
        # 决策树类
        {
            "form": ExpressionForm.TREE_DIAGRAM,
            "patterns": [
                r"(?:如果.*则.*否则|取决于|根据.*选择|场景[一二三四五].*用).*\n.*",
            ],
            "keywords": ["如果", "取决于", "根据", "场景", "选择", "判断", "条件"],
            "weight": 0.7,
        },
    ]

    def detect_form(self, content: str) -> FormatSuggestion:
        """
        检测内容最佳表达形式

        Returns:
            FormatSuggestion，包含建议形式和置信度
        """
        scores: Dict[ExpressionForm, float] = {}
        reasons: Dict[ExpressionForm, List[str]] = {}

        for rule in self.DETECTION_RULES:
            form = rule["form"]
            score = 0.0
            matched_reasons = []

            # 正则匹配
            for pattern in rule.get("patterns", []):
                if re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
                    score += 0.4
                    matched_reasons.append(f"匹配模式: {pattern[:40]}...")

            # 关键词匹配
            content_lower = content.lower()
            for kw in rule.get("keywords", []):
                if kw.lower() in content_lower:
                    score += 0.15
                    matched_reasons.append(f"关键词: {kw}")

            # 应用权重
            score *= rule.get("weight", 1.0)

            # 去重原因
            if matched_reasons:
                scores[form] = score
                reasons[form] = matched_reasons[:3]  # 最多保留 3 个原因

        if not scores:
            return FormatSuggestion(
                form=ExpressionForm.MARKDOWN_LIST,
                confidence=1.0,
                reason="未检测到特定模式，使用默认列表格式",
            )

        # 选择得分最高的
        best_form = max(scores, key=scores.get)
        best_score = scores[best_form]

        # 如果最高分和第二名的差距 < 0.2，降低置信度
        sorted_scores = sorted(scores.values(), reverse=True)
        confidence = min(best_score, 1.0)
        if len(sorted_scores) > 1 and (sorted_scores[0] - sorted_scores[1]) < 0.2:
            confidence *= 0.8

        return FormatSuggestion(
            form=best_form,
            confidence=round(confidence, 2),
            reason="; ".join(reasons.get(best_form, ["模式匹配"])),
        )

    def format_content(self, content: str,
                       forced_form: ExpressionForm = None) -> str:
        """
        将内容格式化为最佳表达形式

        Args:
            content: 原始 Markdown 内容
            forced_form: 强制使用指定形式（None 则自动检测）

        Returns:
            格式化后的内容
        """
        if forced_form:
            suggestion = FormatSuggestion(form=forced_form, confidence=1.0, reason="用户指定")
        else:
            suggestion = self.detect_form(content)

        # 如果置信度太低，返回原始内容
        if suggestion.confidence < 0.3:
            return content

        # 根据形式格式化
        formatters = {
            ExpressionForm.COMPARISON_TABLE: self._to_comparison_table,
            ExpressionForm.MERMAID_FLOW: self._to_mermaid_flow,
            ExpressionForm.CONFIG_BLOCK: self._to_config_block,
            ExpressionForm.PRO_CON_TABLE: self._to_pro_con_table,
            ExpressionForm.CHECKLIST: self._to_checklist,
            ExpressionForm.TREE_DIAGRAM: self._to_tree_diagram,
        }

        formatter = formatters.get(suggestion.form)
        if formatter:
            suggestion.formatted_content = formatter(content)
            return suggestion.formatted_content

        return content

    # ========== 具体格式化器 ==========

    def _to_comparison_table(self, content: str) -> str:
        """转换为对比矩阵表格"""
        lines = content.strip().split("\n")

        # 尝试提取方案/选项
        options = []
        for line in lines:
            match = re.match(r"^(?:###?\s*)?(?:方案|选项|方法|工具|框架)?\s*([一二三四五ABCDE].*?)[：:\s]", line)
            if match:
                options.append(match.group(1).strip())

        if len(options) < 2:
            # 回退：简单表格
            return self._simple_table(content)

        # 提取对比维度
        dimensions = ["适用场景", "优点", "缺点", "复杂度", "推荐度"]
        found_dimensions = []
        for dim in dimensions:
            if dim in content:
                found_dimensions.append(dim)

        if not found_dimensions:
            found_dimensions = ["特点", "适用情况"]

        # 生成表格
        table_lines = ["| 维度 | " + " | ".join(options) + " |", "|" + "---|" * (len(options) + 1)]

        for dim in found_dimensions:
            row = [f"**{dim}**"]
            for _ in options:
                # 简化：每个选项的该维度留空让用户填充
                row.append("待补充")
            table_lines.append("| " + " | ".join(row) + " |")

        return "\n".join(table_lines) + "\n\n> 请根据原文内容填充上表各单元格"

    def _to_mermaid_flow(self, content: str) -> str:
        """转换为 Mermaid 流程图"""
        lines = ["```mermaid", "flowchart TD"]

        # 提取步骤
        steps = []
        for i, line in enumerate(content.split("\n"), 1):
            # 匹配 "步骤 X:" 或 "第 X 步" 或 "1. "
            match = re.match(r"^(?:步骤\s*([一二三四五\d]+)|第\s*([一二三四五\d]+)\s*步|\d+[.\)]\s*)(.+)$", line.strip())
            if match:
                step_text = (match.group(3) or match.group(1) or match.group(2)).strip()
                steps.append((i, step_text[:30]))  # 截断避免过长

        if len(steps) < 2:
            return content  # 回退

        # 生成节点
        node_ids = {}
        for i, (idx, text) in enumerate(steps):
            node_id = f"S{i}"
            node_ids[i] = node_id
            lines.append(f'    {node_id}["{text}"]')

        # 生成边
        for i in range(len(steps) - 1):
            lines.append(f"    {node_ids[i]} --> {node_ids[i + 1]}")

        lines.append("```")
        return "\n".join(lines)

    def _to_config_block(self, content: str) -> str:
        """转换为 YAML 配置块"""
        lines = content.strip().split("\n")

        # 尝试提取 key-value 对
        config = {}
        for line in lines:
            match = re.match(r"^(?:[-*]\s*)?(\w+)[\s:=]+(.+)$", line.strip())
            if match:
                key = match.group(1).strip()
                value = match.group(2).strip()
                config[key] = value

        if len(config) < 2:
            return content  # 回退

        yaml_lines = ["```yaml", "# 配置参数"]
        for key, value in config.items():
            yaml_lines.append(f"{key}: {value}")
        yaml_lines.append("```")

        return "\n".join(yaml_lines)

    def _to_pro_con_table(self, content: str) -> str:
        """转换为对照表"""
        lines = content.strip().split("\n")

        pros = []
        cons = []

        for line in lines:
            line_stripped = line.strip()
            # 正面
            if re.match(r"^(?:\u2705|\u2713|\u221a|\u3010\u6b63\u3011|推荐|应该|优先|尽量|建议)", line_stripped):
                pros.append(re.sub(r"^(?:\u2705|\u2713|\u221a|\u3010\u6b63\u3011)\s*", "", line_stripped))
            elif any(kw in line_stripped for kw in ["应该", "推荐", "优先", "尽量", "建议", "正确"]):
                if not any(kw in line_stripped for kw in ["不要", "避免", "切忌"]):
                    pros.append(line_stripped)

            # 负面
            if re.match(r"^(?:\u274c|\u2717|\u00d7|\u3010\u53cd\u3011|不要|避免|切忌|禁止)", line_stripped):
                cons.append(re.sub(r"^(?:\u274c|\u2717|\u00d7|\u3010\u53cd\u3011)\s*", "", line_stripped))
            elif any(kw in line_stripped for kw in ["不要", "避免", "切忌", "禁止", "错误"]):
                cons.append(line_stripped)

        if not pros and not cons:
            return content  # 回退

        table_lines = ["| Do | Don't |", "|---|---|"]
        max_len = max(len(pros), len(cons))
        for i in range(max_len):
            p = pros[i] if i < len(pros) else ""
            c = cons[i] if i < len(cons) else ""
            table_lines.append(f"| {p} | {c} |")

        return "\n".join(table_lines)

    def _to_checklist(self, content: str) -> str:
        """转换为检查清单"""
        lines = content.strip().split("\n")
        result = []

        for line in lines:
            line_stripped = line.strip()
            # 如果已经是 checklist 格式，保留
            if re.match(r"^[\-*]\s*\[[\sxX]\]", line_stripped):
                result.append(line_stripped)
            # 否则转换为 checklist
            elif re.match(r"^[\-*]\s+", line_stripped):
                text = re.sub(r"^[\-*]\s+", "", line_stripped)
                result.append(f"- [ ] {text}")
            elif line_stripped and not line_stripped.startswith("#"):
                result.append(f"- [ ] {line_stripped}")
            else:
                result.append(line_stripped)

        return "\n".join(result)

    def _to_tree_diagram(self, content: str) -> str:
        """转换为树形决策图"""
        lines = ["```mermaid", "flowchart TD"]

        # 简单提取条件分支
        branches = []
        for line in content.split("\n"):
            match = re.match(r"^(?:如果|当|若)\s*(.+?)[，,]?\s*(?:则|就|那么)\s*(.+)$", line.strip())
            if match:
                condition = match.group(1).strip()[:25]
                action = match.group(2).strip()[:25]
                branches.append((condition, action))

        if len(branches) < 2:
            return content  # 回退

        lines.append('    Start(["开始判断"])')
        for i, (cond, action) in enumerate(branches):
            node_c = f"C{i}"
            node_a = f"A{i}"
            lines.append(f'    {node_c}["{cond}?"]')
            lines.append(f'    {node_a}["{action}"]')
            if i == 0:
                lines.append(f"    Start --> {node_c}")
            else:
                lines.append(f"    A{i-1} --> {node_c}")
            lines.append(f"    {node_c} -- 是 --> {node_a}")

        lines.append("```")
        return "\n".join(lines)

    def _simple_table(self, content: str) -> str:
        """简单表格回退"""
        lines = content.strip().split("\n")
        rows = []
        for line in lines:
            line = line.strip()
            if line.startswith(("-", "*", "1.", "2.", "3.")):
                rows.append(line.lstrip("- *123456789.").strip())

        if len(rows) < 2:
            return content

        table = ["| 项目 | 说明 |", "|---|---|"]
        for row in rows:
            if ":" in row:
                parts = row.split(":", 1)
                table.append(f"| {parts[0].strip()} | {parts[1].strip()} |")
            else:
                table.append(f"| {row} | |")

        return "\n".join(table)


# ========== 便捷函数 ==========

def auto_format(content: str, forced_form: str = None) -> str:
    """便捷函数：自动格式化内容"""
    formatter = ContentFormatter()
    if forced_form:
        form_map = {
            "table": ExpressionForm.COMPARISON_TABLE,
            "flow": ExpressionForm.MERMAID_FLOW,
            "config": ExpressionForm.CONFIG_BLOCK,
            "procon": ExpressionForm.PRO_CON_TABLE,
            "checklist": ExpressionForm.CHECKLIST,
            "tree": ExpressionForm.TREE_DIAGRAM,
        }
        return formatter.format_content(content, form_map.get(forced_form))
    return formatter.format_content(content)


def detect_best_form(content: str) -> str:
    """便捷函数：检测最佳表达形式"""
    formatter = ContentFormatter()
    suggestion = formatter.detect_form(content)
    return suggestion.form.value
