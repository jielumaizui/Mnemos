"""
DecisionDependencyExtractor — 决策依赖提取器

【E14 全库修复】E13 连接 Worker 完整实现。
从文本中提取决策及其依赖关系，构建决策依赖图。
"""

import re
from typing import List, Dict, Optional, Set, Tuple
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class DecisionNode:
    """决策节点"""
    id: str
    decision: str              # 决策陈述
    premises: List[str] = field(default_factory=list)  # 前提/假设
    dependencies: List[str] = field(default_factory=list)  # 依赖的其他决策ID
    confidence: float = 0.5
    source_text: str = ""


@dataclass
class DecisionGraph:
    """决策依赖图"""
    nodes: Dict[str, DecisionNode] = field(default_factory=dict)
    edges: List[Tuple[str, str, str]] = field(default_factory=list)  # (from, to, relation_type)

    def get_root_decisions(self) -> List[DecisionNode]:
        """获取无依赖的根决策"""
        depended_ids = {edge[1] for edge in self.edges}
        return [n for nid, n in self.nodes.items() if nid not in depended_ids]

    def get_leaf_decisions(self) -> List[DecisionNode]:
        """获取无被依赖的叶决策"""
        depending_ids = {edge[0] for edge in self.edges}
        return [n for nid, n in self.nodes.items() if nid not in depending_ids]

    def to_dict(self) -> Dict:
        """导出为字典"""
        return {
            "nodes": [
                {
                    "id": n.id,
                    "decision": n.decision,
                    "premises": n.premises,
                    "dependencies": n.dependencies,
                    "confidence": n.confidence,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {"from": e[0], "to": e[1], "type": e[2]}
                for e in self.edges
            ],
        }


class DecisionDependencyExtractor:
    """提取决策节点和它们之间的依赖关系"""

    # 决策触发词（扩展版）
    DECISION_PATTERNS = [
        # 中文决策模式
        re.compile(r'(?:我们?|团队|项目组)?(?:决定|决策|选择|确定|采用|使用|选用)\s*[:：]\s*(.+?)(?:[。；\n]|因为|由于|考虑到|基于|原因是)'),
        re.compile(r'(?:最终|最后|综上|综合考虑后)?(?:决定|选择|确定|采用)\s+(.+?)(?:[。；\n]|因为|由于|考虑到)'),
        re.compile(r'(?:方案|策略|架构|设计)\s*[:：]\s*(.+?)(?:[。；\n]|优势|缺点|风险)'),
        re.compile(r'(?:选用|采用|使用|引入|迁移到|切换到)\s*[`"]?(\w+[\w\s\-+.]*\w+)[`"]?(?:\s+作为|\s+用于|\s+因为)'),
        # 英文决策模式
        re.compile(r'(?:we|team)?\s+(?:decided|chose|opted|selected)\s+(?:to\s+)?(.+?)(?:\.|because|since|due\s+to)', re.I),
        re.compile(r'(?:decision|choice)\s*[:：]\s*(.+?)(?:\.|\n)', re.I),
    ]

    # 前提/原因提取模式
    PREMISE_PATTERNS = [
        re.compile(r'(?:因为|由于|考虑到|基于|原因是|理由是)\s*(.+?)(?:[。；\n]|所以|因此|于是)'),
        re.compile(r'(?:because|since|due\s+to|given|considering)\s*(.+?)(?:\.|,\s*so|\n)', re.I),
        re.compile(r'(?:优势|优点|好处| pros?)\s*[:：]\s*(.+?)(?:[。；\n]|缺点|劣势|cons)'),
        re.compile(r'(?:前提|假设|条件|prerequisite|assumption)\s*[:：]\s*(.+?)(?:[。；\n])'),
    ]

    # 依赖关系词
    DEPENDENCY_KEYWORDS = {
        "depends_on": ["依赖", "需要", "要求", "基于", " prerequisite ", "depends on", "requires"],
        "builds_on": ["构建于", "扩展", "继承", "builds on", "extends", "inherits"],
        "replaces": ["替代", "取代", "替换", "replaces", "supersedes"],
        "conflicts_with": ["冲突", "矛盾", "不兼容", "conflicts with", "incompatible with"],
    }

    def __init__(self):
        self._counter = 0

    def extract(self, text: str) -> DecisionGraph:
        """
        提取决策及依赖

        Args:
            text: 输入文本

        Returns:
            DecisionGraph
        """
        graph = DecisionGraph()

        # 1. 按句子分割
        sentences = self._split_sentences(text)

        # 2. 提取决策节点
        for sent in sentences:
            decisions = self._extract_decisions_from_sentence(sent)
            for decision_text, confidence in decisions:
                self._counter += 1
                node_id = f"d{self._counter}"
                premises = self._extract_premises(sent, text)

                node = DecisionNode(
                    id=node_id,
                    decision=decision_text,
                    premises=premises,
                    confidence=confidence,
                    source_text=sent,
                )
                graph.nodes[node_id] = node

        # 3. 建立依赖关系
        self._link_dependencies(graph, text)

        # 4. 基于共现建立隐式依赖
        self._link_implicit_dependencies(graph, sentences)

        return graph

    def extract_from_multiple(self, texts: List[str]) -> DecisionGraph:
        """从多个文本中提取合并的决策图"""
        combined = DecisionGraph()
        for text in texts:
            graph = self.extract(text)
            combined.nodes.update(graph.nodes)
            combined.edges.extend(graph.edges)
        return combined

    def _split_sentences(self, text: str) -> List[str]:
        """分割句子（支持中英文）"""
        # 中文标点 + 英文标点
        sents = re.split(r'[。！？\n;；]+|\.(?=\s+[A-Z])|\?(?=\s+)|!(?=\s+)', text)
        return [s.strip() for s in sents if len(s.strip()) > 10]

    def _extract_decisions_from_sentence(self, sentence: str) -> List[Tuple[str, float]]:
        """从单句中提取决策"""
        results = []
        for pattern in self.DECISION_PATTERNS:
            for match in pattern.finditer(sentence):
                decision = match.group(1).strip()
                if len(decision) > 5:
                    # 置信度：模式匹配越精确，置信度越高
                    confidence = min(0.9, 0.5 + len(decision) / 100)
                    results.append((decision, confidence))
        return results

    def _extract_premises(self, sentence: str, full_text: str) -> List[str]:
        """提取前提/原因"""
        premises = []

        # 从当前句子提取
        for pattern in self.PREMISE_PATTERNS:
            for match in pattern.finditer(sentence):
                premise = match.group(1).strip()
                if len(premise) > 5:
                    premises.append(premise)

        # 从上下文（前后句）提取关联前提
        premises.extend(self._extract_contextual_premises(sentence, full_text))

        # 去重
        seen = set()
        unique = []
        for p in premises:
            key = p[:50]
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique[:5]  # 最多5个前提

    def _extract_contextual_premises(self, sentence: str, full_text: str) -> List[str]:
        """从上下文中提取关联前提"""
        premises = []
        sentences = self._split_sentences(full_text)

        try:
            idx = sentences.index(sentence)
        except ValueError:
            return premises

        # 检查前后句是否包含前提标记
        context_window = sentences[max(0, idx - 2):min(len(sentences), idx + 3)]
        for ctx_sent in context_window:
            if ctx_sent == sentence:
                continue
            for pattern in self.PREMISE_PATTERNS:
                for match in pattern.finditer(ctx_sent):
                    premise = match.group(1).strip()
                    if len(premise) > 5:
                        premises.append(premise)

        return premises

    def _link_dependencies(self, graph: DecisionGraph, text: str):
        """基于显式依赖词建立关系"""
        text_lower = text.lower()

        for node_id, node in graph.nodes.items():
            decision_lower = node.decision.lower()

            for other_id, other in graph.nodes.items():
                if node_id == other_id:
                    continue

                other_lower = other.decision.lower()

                # 检查依赖关键词
                for rel_type, keywords in self.DEPENDENCY_KEYWORDS.items():
                    for kw in keywords:
                        # 模式：当前决策 + 依赖词 + 其他决策
                        pattern = re.escape(node.decision[:20]) + r'.{0,30}' + re.escape(kw) + r'.{0,30}' + re.escape(other.decision[:20])
                        if re.search(pattern, text, re.I | re.DOTALL):
                            graph.edges.append((node_id, other_id, rel_type))
                            node.dependencies.append(other_id)
                            break

    def _link_implicit_dependencies(self, graph: DecisionGraph, sentences: List[str]):
        """基于共现和顺序建立隐式依赖"""
        node_list = list(graph.nodes.values())

        for i, node_a in enumerate(node_list):
            for j, node_b in enumerate(node_list):
                if i >= j:
                    continue

                # 检查是否在同一句子中
                co_occur = False
                for sent in sentences:
                    if node_a.decision[:15] in sent and node_b.decision[:15] in sent:
                        co_occur = True
                        break

                if co_occur:
                    # 如果B提到"基于A"或类似，建立依赖
                    if any(w in node_b.source_text.lower()
                           for w in ["基于", "依赖", "需要", "builds on", "depends"]):
                        if node_b.id not in [e[0] for e in graph.edges if e[1] == node_a.id]:
                            graph.edges.append((node_b.id, node_a.id, "builds_on"))
                            graph.nodes[node_b.id].dependencies.append(node_a.id)

    def find_circular_dependencies(self, graph: DecisionGraph) -> List[List[str]]:
        """检测循环依赖"""
        # 构建邻接表
        adj = defaultdict(list)
        for frm, to, _ in graph.edges:
            adj[frm].append(to)

        cycles = []
        visited = set()
        rec_stack = set()

        def dfs(node, path):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in adj[node]:
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # 发现循环
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)

            path.pop()
            rec_stack.remove(node)

        for node in graph.nodes:
            if node not in visited:
                dfs(node, [])

        return cycles
