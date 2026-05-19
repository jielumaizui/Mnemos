from __future__ import annotations

#!/usr/bin/env python3
"""
Background Review — 人工确认 CLI
交互式确认审查建议
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from core.config import get_config


APPROVAL_LOG_PATH = get_config().data_dir / "logs/review_approvals.jsonl"
REJECTION_LOG_PATH = get_config().data_dir / "logs/review_rejections.jsonl"


class ApprovalCLI:
    """人工确认 CLI"""

    def __init__(self):
        self.approval_log = APPROVAL_LOG_PATH
        self.rejection_log = REJECTION_LOG_PATH
        self._ensure_dirs()

    def _ensure_dirs(self):
        self.approval_log.parent.mkdir(parents=True, exist_ok=True)
        self.rejection_log.parent.mkdir(parents=True, exist_ok=True)

    def load_pending_items(self, json_path: str) -> List[Dict]:
        """加载待确认的建议"""
        data = Path(json_path).read_text(encoding="utf-8")
        findings = json.loads(data)
        # 只保留需要人工确认的（不在自动执行白名单内的）
        return [f for f in findings if not self._is_auto_eligible(f)]

    def _is_auto_eligible(self, finding: Dict) -> bool:
        """检查是否在自动执行白名单"""
        # 简单规则：info + xs/s + maintainability 维度
        return (
            finding.get("severity") == "info" and
            finding.get("effort") in ["xs", "s"] and
            finding.get("dimension") == "maintainability"
        )

    def display_item(self, finding: Dict, index: int, total: int):
        """显示单个建议"""
        emoji = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(finding.get("severity"), "⚪")
        print(f"\n{'='*60}")
        print(f"[{index}/{total}] {emoji} {finding.get('id')}")
        print(f"{'='*60}")
        print(f"标题: {finding.get('title')}")
        print(f"文件: {finding.get('file')}")
        print(f"严重度: {finding.get('severity')}")
        print(f"修复成本: {finding.get('effort')}")
        print(f"\n描述:\n{finding.get('description', '无')}")
        print(f"\n建议修复:\n{finding.get('suggestion', '无')}")
        print(f"\n忽略风险: {finding.get('risk_if_ignored', '无')}")

    def prompt_action(self) -> str:
        """提示用户输入"""
        print(f"\n{'-'*40}")
        print("操作: [y]确认  [n]拒绝  [e]修改  [s]跳过  [q]退出")
        print("批量: [ya]确认所有剩余  [na]拒绝所有剩余")
        while True:
            choice = input("> ").strip().lower()
            if choice in ["y", "n", "e", "s", "q", "ya", "na"]:
                return choice
            print("无效输入，请重新选择")

    def approve(self, finding: Dict):
        """确认执行"""
        log_entry = {
            "finding_id": finding["id"],
            "action": "approved",
            "timestamp": datetime.now().isoformat(),
            "file": finding.get("file"),
            "severity": finding.get("severity")
        }
        self._append_log(self.approval_log, log_entry)
        print(f"  已确认: {finding['id']}")

    def reject(self, finding: Dict):
        """拒绝"""
        reason = input("拒绝原因 (可选): ").strip()
        log_entry = {
            "finding_id": finding["id"],
            "action": "rejected",
            "timestamp": datetime.now().isoformat(),
            "file": finding.get("file"),
            "reason": reason or None
        }
        self._append_log(self.rejection_log, log_entry)
        print(f"  已拒绝: {finding['id']}")

    def modify(self, finding: Dict) -> Dict:
        """修改后确认"""
        print("\n修改模式（直接回车保留原值）:")
        new_suggestion = input(f"建议修复 [{finding.get('suggestion', '')[:50]}...]: ").strip()
        if new_suggestion:
            finding["suggestion"] = new_suggestion
            finding["modified"] = True

        new_effort = input(f"修复成本 [{finding.get('effort', 'm')}]: ").strip()
        if new_effort:
            finding["effort"] = new_effort

        log_entry = {
            "finding_id": finding["id"],
            "action": "modified",
            "timestamp": datetime.now().isoformat(),
            "file": finding.get("file"),
            "new_suggestion": finding.get("suggestion"),
            "new_effort": finding.get("effort")
        }
        self._append_log(self.approval_log, log_entry)
        print(f"   已修改并确认: {finding['id']}")
        return finding

    def _append_log(self, log_path: Path, entry: Dict):
        """追加日志"""
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def run(self, json_path: str):
        """主流程"""
        items = self.load_pending_items(json_path)
        if not items:
            print("没有需要人工确认的建议（全部在自动执行白名单中）")
            return

        print(f"\n共 {len(items)} 条建议需要确认\n")

        approved = []
        rejected = []
        skipped = []

        i = 0
        while i < len(items):
            item = items[i]
            self.display_item(item, i + 1, len(items))
            action = self.prompt_action()

            if action == "q":
                print("\n退出确认流程")
                break
            elif action == "s":
                skipped.append(item)
                print(f"  SKIP  已跳过: {item['id']}")
            elif action == "y":
                self.approve(item)
                approved.append(item)
            elif action == "n":
                self.reject(item)
                rejected.append(item)
            elif action == "e":
                item = self.modify(item)
                approved.append(item)
            elif action == "ya":
                # 确认所有剩余
                for remaining in items[i:]:
                    self.approve(remaining)
                    approved.append(remaining)
                print(f"\n批量确认 {len(items) - i} 条")
                break
            elif action == "na":
                # 拒绝所有剩余
                for remaining in items[i:]:
                    self.reject(remaining)
                    rejected.append(remaining)
                print(f"\n批量拒绝 {len(items) - i} 条")
                break

            i += 1

        # 总结
        print(f"\n{'='*60}")
        print("确认总结:")
        print(f"  已确认: {len(approved)}")
        print(f"  已拒绝: {len(rejected)}")
        print(f"  已跳过: {len(skipped)}")
        print(f"  剩余未处理: {len(items) - i}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Background Review Approval CLI")
    parser.add_argument("--input", "-i", required=True, help="审查 JSON 文件路径")
    args = parser.parse_args()

    cli = ApprovalCLI()
    cli.run(args.input)


if __name__ == "__main__":
    main()
