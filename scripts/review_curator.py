#!/usr/bin/env python3

from __future__ import annotations
"""
Background Review — Curator 合并编排器
合并多个审查 Agent 的建议，去重、排序、生成 action 列表
"""

import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Set, Tuple
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class CuratedItem:
    """合并后的审查项"""
    id: str
    severity: str
    dimension: str
    files: List[str]
    title: str
    description: str
    suggestion: str
    effort: str
    module: str
    priority_score: float = 0.0
    related_ids: List[str] = field(default_factory=list)
    conflicts_with: List[str] = field(default_factory=list)


class ReviewCurator:
    """审查 Curator"""

    # severity 权重
    SEVERITY_WEIGHT = {
        "critical": 100,
        "warning": 50,
        "info": 10
    }

    # effort 成本因子（越低越优先）
    EFFORT_COST = {
        "xs": 0.5,
        "s": 1.0,
        "m": 2.0,
        "l": 4.0
    }

    # 模块优先级（核心模块优先修复）
    MODULE_PRIORITY = {
        "ingest": 1.5,
        "search": 1.3,
        "heat": 1.2,
        "expand": 1.2,
        "quality": 1.0,
        "config": 1.4
    }

    def __init__(self):
        self.items: List[CuratedItem] = []

    def load_from_json(self, json_path: str) -> List[Dict]:
        """从 JSON 文件加载审查结果"""
        data = Path(json_path).read_text(encoding="utf-8")
        return json.loads(data)

    def dedupe(self, findings: List[Dict]) -> List[Dict]:
        """去重：同一问题多个维度发现只保留最严重的一次"""
        # 按 dedupe_key 分组
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for f in findings:
            key = self._make_dedupe_key(f)
            groups[key].append(f)

        result = []
        for key, group in groups.items():
            # 保留 severity 最高的一条
            best = max(group, key=lambda x: self.SEVERITY_WEIGHT.get(x.get("severity", "info"), 0))
            # 合并 related_ids
            best["related_findings"] = [g["id"] for g in group if g["id"] != best["id"]]
            result.append(best)

        return result

    def detect_conflicts(self, findings: List[Dict]) -> Dict[str, List[str]]:
        """检测冲突：两个建议互相矛盾"""
        conflicts = defaultdict(list)

        # 冲突规则定义
        conflict_rules = [
            # 规则1: 同一文件的同一个函数，一个建议拆分为小函数，另一个建议合并为大函数
            {
                "name": "refactor_direction_conflict",
                "check": lambda a, b: (
                    a.get("file") == b.get("file") and
                    "拆分" in a.get("suggestion", "") and
                    "合并" in b.get("suggestion", "")
                )
            },
            # 规则2: 配置值冲突（一个建议调大，另一个建议调小）
            {
                "name": "config_value_conflict",
                "check": lambda a, b: (
                    a.get("file") == b.get("file") and
                    a.get("dimension") == "logic" and
                    "调大" in a.get("suggestion", "") and
                    "调小" in b.get("suggestion", "")
                )
            },
            # 规则3: 命名冲突（同一实体两个不同的命名建议）
            {
                "name": "naming_conflict",
                "check": lambda a, b: (
                    a.get("file") == b.get("file") and
                    "重命名" in a.get("suggestion", "") and
                    "重命名" in b.get("suggestion", "")
                )
            }
        ]

        for i, a in enumerate(findings):
            for b in findings[i+1:]:
                for rule in conflict_rules:
                    if rule["check"](a, b):
                        conflicts[a["id"]].append(b["id"])
                        conflicts[b["id"]].append(a["id"])

        return dict(conflicts)

    def classify_module(self, file_path: str) -> str:
        """根据文件路径分类模块"""
        if "ingest" in file_path.lower():
            return "ingest"
        elif "search" in file_path.lower():
            return "search"
        elif "heat" in file_path.lower():
            return "heat"
        elif "expand" in file_path.lower():
            return "expand"
        elif "quality" in file_path.lower() or "distill" in file_path.lower():
            return "quality"
        elif "config" in file_path.lower():
            return "config"
        return "other"

    def compute_priority_score(self, finding: Dict) -> float:
        """计算优先级分数"""
        severity_weight = self.SEVERITY_WEIGHT.get(finding.get("severity", "info"), 0)
        effort_cost = self.EFFORT_COST.get(finding.get("effort", "m"), 2.0)
        module_boost = self.MODULE_PRIORITY.get(self.classify_module(finding.get("file", "")), 1.0)

        # 公式: (severity_weight / effort_cost) * module_boost
        # 高 severity、低成本、核心模块 = 高优先级
        score = (severity_weight / effort_cost) * module_boost
        return round(score, 2)

    def sort_findings(self, findings: List[Dict]) -> List[Dict]:
        """排序：按优先级分数降序"""
        for f in findings:
            f["priority_score"] = self.compute_priority_score(f)

        return sorted(findings, key=lambda x: x["priority_score"], reverse=True)

    def group_by_module(self, findings: List[Dict]) -> Dict[str, List[Dict]]:
        """按模块分组"""
        groups = defaultdict(list)
        for f in findings:
            module = self.classify_module(f.get("file", ""))
            groups[module].append(f)
        return dict(groups)

    def generate_report(self, findings: List[Dict], conflicts: Dict[str, List[str]]) -> str:
        """生成 Markdown 报告"""
        now = datetime.now().isoformat()
        lines = [
            f"# Background Review 审查报告 | {now[:19]}",
            "",
            "## 摘要",
            "",
            f"- **总发现数**: {len(findings)}",
            f"- **Critical**: {sum(1 for f in findings if f.get('severity') == 'critical')}",
            f"- **Warning**: {sum(1 for f in findings if f.get('severity') == 'warning')}",
            f"- **Info**: {sum(1 for f in findings if f.get('severity') == 'info')}",
            f"- **冲突检测**: {len(conflicts)} 条建议存在冲突",
            "",
            "---",
            "",
            "## 按模块分组",
            ""
        ]

        # 按模块分组
        groups = self.group_by_module(findings)
        for module in sorted(groups.keys(), key=lambda m: sum(f.get("priority_score", 0) for f in groups[m]), reverse=True):
            module_findings = groups[module]
            lines.append(f"### {module.upper()} ({len(module_findings)} 项)")
            lines.append("")

            for f in module_findings:
                emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(f.get("severity"), "⚪")
                lines.append(f"#### {emoji} {f.get('id')} | {f.get('title')}")
                lines.append(f"- **文件**: `{f.get('file')}`")
                lines.append(f"- **维度**: {f.get('dimension')}")
                lines.append(f"- **优先级分**: {f.get('priority_score', 0)}")
                lines.append(f"- **修复成本**: {f.get('effort', 'm')}")
                if f.get('related_findings'):
                    lines.append(f"- **相关发现**: {', '.join(f['related_findings'])}")
                if conflicts.get(f.get('id')):
                    lines.append(f"- **冲突**: 与 {', '.join(conflicts[f['id']])} 冲突")
                lines.append("")
                lines.append(f"**建议**: {f.get('suggestion', '无')}")
                lines.append("")

        # Action Items
        lines.extend([
            "---",
            "",
            "## Action Items（按优先级排序）",
            ""
        ])

        for i, f in enumerate(findings[:20], 1):  # 只显示 Top 20
            emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(f.get("severity"), "⚪")
            lines.append(f"{i}. [{emoji}] `{f.get('id')}` {f.get('title')} ({f.get('effort')})")

        lines.append("")
        lines.append("---")
        lines.append("Tags: `system=background-review, agent=claude, type=audit-report`")

        return "\n".join(lines)

    def _make_dedupe_key(self, finding: Dict) -> str:
        """生成去重键"""
        content = f"{finding.get('file')}:{finding.get('line_start', 0)}:{finding.get('title', '')}"
        return hashlib.sha1(content.encode()).hexdigest()[:12]

    def curate(self, json_path: str, output_path: str = None) -> str:
        """主入口"""
        # 1. 加载
        findings = self.load_from_json(json_path)
        print(f"[Curator] 加载 {len(findings)} 条原始发现")

        # 2. 去重
        findings = self.dedupe(findings)
        print(f"[Curator] 去重后 {len(findings)} 条")

        # 3. 检测冲突
        conflicts = self.detect_conflicts(findings)
        if conflicts:
            print(f"[Curator] 发现 {len(conflicts)} 条冲突建议")

        # 4. 排序
        findings = self.sort_findings(findings)

        # 5. 生成报告
        report = self.generate_report(findings, conflicts)

        # 6. 输出
        if output_path:
            Path(output_path).write_text(report, encoding="utf-8")
            print(f"[Curator] 报告已写入: {output_path}")
        else:
            print(report)

        return report


def main():
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Background Review Curator")
    parser.add_argument("--input", "-i", required=True, help="审查 JSON 文件路径")
    parser.add_argument("--output", "-o", help="输出报告路径（默认 stdout）")
    args = parser.parse_args()

    curator = ReviewCurator()
    curator.curate(args.input, args.output)


if __name__ == "__main__":
    main()
