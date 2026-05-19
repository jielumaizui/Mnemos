from __future__ import annotations

#!/usr/bin/env python3
"""
Background Review — 报告生成器
将审查 Agent 的 JSON 输出转换为 ERRORS.md 格式并写入
"""

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass
from core.config import get_config

ERRORS_MD_PATH = get_config().data_dir / "learnings/ERRORS.md"
REVIEW_LOG_PATH = get_config().data_dir / "logs/review_history.jsonl"


@dataclass
class ReviewFinding:
    """审查发现"""
    id: str
    severity: str
    dimension: str
    file: str
    line_start: int
    line_end: int
    title: str
    description: str
    suggestion: str
    effort: str
    risk_if_ignored: str
    review_batch: str = ""  # 审查批次 ID
    status: str = "open"  # open / investigating / fixed / wontfix

    def dedupe_key(self) -> str:
        """生成去重键"""
        content = f"{self.file}:{self.line_start}:{self.title}"
        return hashlib.sha1(content.encode()).hexdigest()[:12]

    def to_errors_md(self) -> str:
        """转换为 ERRORS.md 条目格式"""
        severity_emoji = {
            "critical": "🔴",
            "warning": "🟡",
            "info": "🟢"
        }.get(self.severity, "⚪")

        lines = [
            f"## {self.id} | {self.title}",
            f"",
            f"- **严重程度**: {severity_emoji} {self.severity}",
            f"- **维度**: {self.dimension}",
            f"- **文件**: `{self.file}:{self.line_start}-{self.line_end}`",
            f"- **审查批次**: {self.review_batch}",
            f"- **状态**: {self.status}",
            f"",
            f"**描述**:",
            f"{self.description}",
            f"",
            f"**建议修复**:",
            f"{self.suggestion}",
            f"",
            f"**修复成本**: {self.effort}",
            f"**忽略风险**: {self.risk_if_ignored}",
            f""
        ]
        return "\n".join(lines)


class ReviewReporter:
    """审查报告器"""

    def __init__(self):
        self.errors_path = ERRORS_MD_PATH
        self.log_path = REVIEW_LOG_PATH
        self._ensure_dirs()

    def _ensure_dirs(self):
        """确保目录存在"""
        self.errors_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def load_existing_findings(self) -> Dict[str, ReviewFinding]:
        """从 ERRORS.md 加载已有发现（用于去重）"""
        findings = {}
        if not self.errors_path.exists():
            return findings

        content = self.errors_path.read_text(encoding="utf-8")
        # 解析现有的 ERRORS.md 条目
        # 格式: ## BR-YYYYMMDD-NNN | 标题
        pattern = r"## (BR-\d{8}-\d{3}) \| (.+?)\n"
        for match in re.finditer(pattern, content):
            finding_id = match.group(1)
            title = match.group(2)
            # 提取 file:line
            file_match = re.search(r"`(.+?):(\d+)-", content[match.end():match.end()+500])
            if file_match:
                key = f"{file_match.group(1)}:{file_match.group(2)}:{title}"
                findings[hashlib.sha1(key.encode()).hexdigest()[:12]] = finding_id

        return findings

    def process_review_output(self, json_data: str, batch_id: str) -> List[ReviewFinding]:
        """处理审查 Agent 的 JSON 输出"""
        raw_findings = json.loads(json_data)
        findings = []

        for item in raw_findings:
            finding = ReviewFinding(
                id=item.get("id", self._generate_id()),
                severity=item.get("severity", "info"),
                dimension=item.get("dimension", "unknown"),
                file=item.get("file", "unknown"),
                line_start=item.get("line_start", 0),
                line_end=item.get("line_end", 0),
                title=item.get("title", "未命名问题"),
                description=item.get("description", ""),
                suggestion=item.get("suggestion", ""),
                effort=item.get("effort", "s"),
                risk_if_ignored=item.get("risk_if_ignored", ""),
                review_batch=batch_id
            )
            findings.append(finding)

        return findings

    def dedupe_and_filter(self, findings: List[ReviewFinding]) -> List[ReviewFinding]:
        """去重和过滤"""
        existing = self.load_existing_findings()
        new_findings = []

        for finding in findings:
            key = finding.dedupe_key()
            if key in existing:
                # 已有相同问题，跳过
                continue
            new_findings.append(finding)

        return new_findings

    def append_to_errors_md(self, findings: List[ReviewFinding]):
        """追加到 ERRORS.md"""
        if not findings:
            print("[ReviewReporter] 没有新发现，无需写入")
            return

        # 读取现有内容
        if self.errors_path.exists():
            content = self.errors_path.read_text(encoding="utf-8")
        else:
            content = self._create_errors_header()

        # 追加新条目
        new_entries = []
        for finding in findings:
            new_entries.append(finding.to_errors_md())

        # 插入到文件末尾（在最后一个条目之前）
        # 找到最后一个条目的位置
        all_entries = "\n".join(new_entries)

        # 写入
        with open(self.errors_path, "a", encoding="utf-8") as f:
            f.write("\n")
            f.write(all_entries)

        print(f"[ReviewReporter] 已写入 {len(findings)} 条新发现到 ERRORS.md")

    def log_review_batch(self, batch_id: str, findings: List[ReviewFinding]):
        """记录审查批次日志"""
        log_entry = {
            "batch_id": batch_id,
            "timestamp": datetime.now().isoformat(),
            "total_findings": len(findings),
            "severity_counts": self._count_by_severity(findings),
            "finding_ids": [f.id for f in findings]
        }

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    def _generate_id(self) -> str:
        """生成唯一 ID"""
        date_str = datetime.now().strftime("%Y%m%d")
        seq = int(datetime.now().timestamp() * 1000) % 1000
        return f"BR-{date_str}-{seq:03d}"

    def _count_by_severity(self, findings: List[ReviewFinding]) -> Dict[str, int]:
        """按 severity 统计"""
        counts = {}
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    def _create_errors_header(self) -> str:
        """创建 ERRORS.md 头部"""
        return """# ERRORS — 错误模式库

> 维护方式：触发式 + Background Review 自动追加。
> 格式：每个条目独立，便于 grep 搜索。

---

## 格式模板

```markdown
## BR-YYYYMMDD-NNN | 错误关键词
- **严重程度**: critical / warning / info
- **维度**: security / logic / maintainability / performance / domain
- **文件**: `path/to/file.py:123-145`
- **审查批次**: BR-BATCH-YYYYMMDD-NNN
- **状态**: open / investigating / fixed / wontfix

**描述**:
详细描述

**建议修复**:
具体修复方案

**修复成本**: xs / s / m / l
**忽略风险**: 不修复的后果
```

---

"""

    def run(self, json_data: str, batch_id: str):
        """主入口"""
        # 1. 解析审查输出
        findings = self.process_review_output(json_data, batch_id)
        print(f"[ReviewReporter] 解析到 {len(findings)} 条发现")

        # 2. 去重
        new_findings = self.dedupe_and_filter(findings)
        print(f"[ReviewReporter] 去重后 {len(new_findings)} 条新发现")

        # 3. 写入 ERRORS.md
        self.append_to_errors_md(new_findings)

        # 4. 记录日志
        self.log_review_batch(batch_id, new_findings)

        return new_findings


def main():
    """CLI 入口"""
    import argparse
    parser = argparse.ArgumentParser(description="Background Review Reporter")
    parser.add_argument("--input", "-i", required=True, help="审查 JSON 输出文件路径")
    parser.add_argument("--batch-id", "-b", required=True, help="审查批次 ID")
    args = parser.parse_args()

    json_data = Path(args.input).read_text(encoding="utf-8")
    reporter = ReviewReporter()
    reporter.run(json_data, args.batch_id)


if __name__ == "__main__":
    main()
