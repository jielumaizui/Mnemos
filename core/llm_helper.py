from __future__ import annotations

"""
LLM辅助提炼模块

职责：
1. 感悟类成熟度判断
2. 代码操作序列理解
3. 轻量级LLM调用（用于分类和提炼辅助）

使用：
    from core.llm_helper import LLMHelper

    helper = LLMHelper()
    maturity = helper.assess_insight_maturity(content)
    operations = helper.understand_code_operations(content)
"""

import os
import re
import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MaturityAssessment:
    """感悟类成熟度评估结果"""
    stage: str           # preliminary/developing/mature
    confidence: float    # 0-1
    reasoning: str       # 判断依据
    key_indicators: List[str]  # 关键指标


@dataclass
class CodeOperation:
    """代码操作理解结果"""
    step: int
    action: str          # 操作描述
    command: str         # 原始命令
    context: str         # 上下文
    result: str          # 执行结果（成功/失败/待确认）
    key_learning: str    # 关键学习点
    dependencies: List[int]  # 依赖的步骤


class LLMHelper:
    """LLM辅助提炼（轻量级调用）"""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        # 同源复用：不再直接持有 API key，委托给宿主 Agent
        self.api_key = None
        self.model = model or "agent-delegate"
        self._client = None


    def assess_insight_maturity(self, content: str) -> MaturityAssessment:
        """
        评估感悟类内容的成熟度

        判断维度：
        1. 想法的完整性（初步想法 vs 深入思考 vs 成熟观点）
        2. 支持的证据（有无具体例子、经验支撑）
        3. 自我反思程度（是否经过深思熟虑）
        4. 行动转化（是否有明确的行动方向）

        Returns:
            MaturityAssessment: 成熟度评估结果
        """
        # 简化实现：基于文本特征判断
        # 实际应调用LLM，这里提供基于规则的后备方案

        indicators = []
        stage = "preliminary"
        confidence = 0.5

        # 完整性指标
        word_count = len(content)
        if word_count < 100:
            indicators.append("简短片段，可能是初步想法")
            stage = "preliminary"
            confidence = 0.4
        elif word_count < 300:
            indicators.append("中等长度，有基本阐述")
            stage = "developing"
            confidence = 0.6
        else:
            indicators.append("较长文本，有深入论述")
            stage = "mature"
            confidence = 0.7

        # 证据支撑
        evidence_markers = ['例如', '比如', '经验', '经历', '观察到', '发现']
        evidence_count = sum(1 for m in evidence_markers if m in content)
        if evidence_count >= 2:
            indicators.append(f"有{evidence_count}个具体例子/经验支撑")
            if stage == "preliminary":
                stage = "developing"
            confidence = min(confidence + 0.1, 0.9)

        # 反思深度
        reflection_markers = ['反思', '回顾', '思考', '意识到', '认识到', '总结']
        reflection_count = sum(1 for m in reflection_markers if m in content)
        if reflection_count >= 2:
            indicators.append(f"包含{reflection_count}次反思性表述")
            if stage == "developing":
                stage = "mature"
            confidence = min(confidence + 0.1, 0.95)

        # 行动意向
        action_markers = ['应该', '需要', '计划', '下一步', '行动', '尝试']
        action_count = sum(1 for m in action_markers if m in content)
        if action_count >= 1:
            indicators.append("包含行动意向")
            confidence = min(confidence + 0.05, 1.0)

        reasoning = f"基于{len(indicators)}个指标判断：{'；'.join(indicators[:3])}"

        return MaturityAssessment(
            stage=stage,
            confidence=confidence,
            reasoning=reasoning,
            key_indicators=indicators
        )

    def understand_code_operations(self, content: str) -> List[CodeOperation]:
        """
        理解代码操作序列

        提取：
        1. 命令执行的上下文
        2. 成功/失败的判断
        3. 关键学习点
        4. 步骤间的依赖关系

        Returns:
            List[CodeOperation]: 操作序列
        """
        import re

        operations = []
        lines = content.split('\n')

        # 提取命令行
        command_pattern = r'^(?:\$|#)\s*(.+)$'

        step = 0
        current_context = []

        for i, line in enumerate(lines):
            line = line.strip()

            # 收集上下文（前3行）
            if not re.match(command_pattern, line):
                current_context.append(line)
                if len(current_context) > 5:
                    current_context.pop(0)
                continue

            # 发现命令
            match = re.match(command_pattern, line)
            if match:
                step += 1
                command = match.group(1)

                # 分析执行结果（看后5行）
                result_lines = lines[i+1:i+6]
                result_text = '\n'.join(result_lines)

                # 判断结果
                if re.search(r'(?:Successfully|success|成功|OK)', result_text, re.I):
                    result = "成功"
                elif re.search(r'(?:Error|error|失败|ERR|Failed)', result_text, re.I):
                    result = "失败"
                elif re.search(r'(?:Warning|warning|警告|WARN)', result_text, re.I):
                    result = "警告"
                else:
                    result = "待确认"

                # 提取关键学习点（简化版）
                key_learning = ""
                learning_patterns = [
                    r'(?:注意|关键|重要|必须|需要)[：:]([^。]+)',
                    r'(?:通过|通过此|这次)[^，]+(?:了解|学会|掌握|明白)[^。]+',
                ]
                for pattern in learning_patterns:
                    learning_match = re.search(pattern, result_text)
                    if learning_match:
                        key_learning = learning_match.group(1).strip()
                        break

                # 查找依赖（是否有"然后"、"接下来"等）
                dependencies = []
                if step > 1:
                    # 检查是否有明确的依赖词
                    if any(word in ' '.join(current_context[-3:]) for word in ['然后', '接着', '接下来', '之后']):
                        dependencies.append(step - 1)

                operations.append(CodeOperation(
                    step=step,
                    action=self._describe_action(command),
                    command=command,
                    context='\n'.join(current_context[-3:]) if current_context else "",
                    result=result,
                    key_learning=key_learning or "无明确学习点",
                    dependencies=dependencies
                ))

                current_context = []

        return operations

    def _describe_action(self, command: str) -> str:
        """生成命令的描述性动作"""
        import re

        # 常见命令映射
        patterns = {
            r'^pip\s+(?:install|uninstall)': 'Python包管理',
            r'^npm\s+(?:install|i|add)': 'Node包安装',
            r'^git\s+(?:clone|pull|push|commit)': 'Git版本控制',
            r'^docker\s+(?:run|build|compose)': 'Docker容器操作',
            r'^kubectl\s+': 'Kubernetes集群管理',
            r'^python3?\s+': 'Python脚本执行',
            r'^curl\s+': 'HTTP请求',
            r'^ls\s+': '文件列表',
            r'^cat\s+': '文件查看',
            r'^cd\s+': '目录切换',
            r'^mkdir\s+': '创建目录',
            r'^rm\s+': '删除文件',
            r'^cp\s+': '复制文件',
            r'^mv\s+': '移动文件',
        }

        for pattern, action in patterns.items():
            if re.match(pattern, command, re.I):
                return action

        return "命令执行"

    def classify_content_semantic(self, content: str) -> Dict[str, Any]:
        """
        语义分类（Layer 3备用实现）

        当规则分类置信度不足时，使用LLM进行语义分类
        """
        # 简化实现：关键词密度分类
        # 实际应调用LLM

        keywords = {
            'code': ['def', 'class', 'import', 'function', '代码', '编程'],
            'business': ['客户', '销售', '价格', '合同', '金额', '利润'],
            'knowledge': ['研究', '理论', '方法', '分析', '结论', '论文'],
            'insight': ['觉得', '认为', '感受', '想法', '反思', '体会']
        }

        scores = {}
        for type_name, words in keywords.items():
            score = sum(1 for w in words if w in content) / len(words)
            scores[type_name] = score

        max_type = max(scores, key=scores.get)
        max_score = scores[max_type]

        return {
            'type': max_type,
            'confidence': max_score,
            'scores': scores,
            'method': 'keyword_density'  # 实际应为'llm'
        }


# ==================== 便捷函数 ====================

def assess_maturity(content: str) -> MaturityAssessment:
    """评估感悟成熟度"""
    helper = LLMHelper()
    return helper.assess_insight_maturity(content)


def understand_operations(content: str) -> List[CodeOperation]:
    """理解代码操作"""
    helper = LLMHelper()
    return helper.understand_code_operations(content)


# ==================== 测试 ====================
if __name__ == "__main__":
    # 测试感悟成熟度
    insight_content = """
    今天和团队讨论了新的架构设计。我觉得我们需要更清晰的模块划分。
    例如，目前的代码耦合度太高，每次修改都影响多个文件。
    反思之前的决策，我意识到我们没有充分考虑扩展性。
    下一步应该制定接口规范，并编写详细的文档。
    """

    maturity = assess_maturity(insight_content)
    print(f"成熟度: {maturity.stage} (置信度: {maturity.confidence:.2f})")
    print(f"依据: {maturity.reasoning}")
    print(f"指标: {maturity.key_indicators}")

    # 测试代码操作理解
    code_content = """
    安装依赖
    $ pip install requests
    Successfully installed requests-2.28.0

    接下来运行脚本
    $ python script.py
    Error: Module not found

    注意：需要安装额外的依赖
    $ pip install numpy
    Successfully installed
    """

    operations = understand_operations(code_content)
    print(f"\n提取到{len(operations)}个操作:")
    for op in operations:
        print(f"  Step {op.step}: {op.action} - {op.result}")
        if op.key_learning != "无明确学习点":
            print(f"    学习点: {op.key_learning}")
