from __future__ import annotations

"""
Distillation Agent - 供 Claude Code 子 Agent 调用的蒸馏执行器

职责：
- 从队列获取待蒸馏的 session 数据
- 构建结构化蒸馏 prompt
- 指导 Agent 生成 KnowledgeUnit 并写入 wiki

用法（由 Claude Code Agent 工具调用）：
    {sys_executable} core/kia/distillation_agent.py --next              # 获取下一个任务 + prompt
    {sys_executable} core/kia/distillation_agent.py --done {{session_id}}  # 标记完成
    {sys_executable} core/kia/distillation_agent.py --list               # 列出待处理任务

Agent 蒸馏流程：
    1. 调用 --next 获取任务数据和蒸馏 prompt
    2. Agent 分析 session 内容，提取知识
    3. Agent 生成 Markdown + frontmatter，写入 wiki/00-Inbox/ 或对应目录
    4. 调用 --done 标记任务完成
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import get_config
from core.hephaestus.distillation_prompts import TYPE_DISTILL_PROMPTS
from core.kia import amphora

# 路径改为 config 驱动
QUEUE_DIR = get_config().claude_data_dir / "distill_queue"
WIKI_DIR = get_config().wiki_dir


def _ensure_dir():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)


# 委托给 amphora.py（SQLite 队列），消除 JSON 文件重复实现
_list_pending = amphora.list_pending
_get_next = amphora.get_next
_mark_done = amphora.mark_done
_mark_failed = amphora.mark_failed


# 简单关键词映射，用于判断 session 类型
_TYPE_KEYWORDS = {
    "coding": ["python", "javascript", "java", "sql", "代码", "函数", "类", "bug", "debug", "报错", "编译", "部署", "git", "api", "接口", "数据库", "算法", "性能", "优化", "重构"],
    "marketing": ["活动", "营销", "推广", "用户", "转化", "roi", "渠道", "投放", "裂变", "拉新", "留存", "促活", "文案", "海报", "campaign", "品牌", "曝光"],
    "analysis": ["数据", "分析", "指标", "报表", "统计", "趋势", "同比", "环比", "漏斗", "归因", "sql", "查询", "可视化", "dashboard", "ab测试", "假设"],
    "strategy": ["战略", "策略", "规划", "目标", "优先级", "资源", "竞争", "差异化", "壁垒", "商业模式", "变现", "增长", "组织架构", "协同", "决策"],
    "writing": ["文章", "文案", "写作", "文档", "标题", "结构", "段落", "措辞", "风格", "语气", "编辑", "校对", "发布", "内容", "创作", "排版"],
    "review": ["审查", "评审", "代码审查", "codereview", "检查", "质量", "标准", "规范", "红线", "遗漏", "错误", "改进", "重构建议", "设计评审"],
}


def _detect_session_type(messages: list) -> str:
    """根据消息内容判断 session 类型"""
    all_text = " ".join(m.get("content", "") for m in messages).lower()
    scores = {}
    for stype, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw.lower() in all_text)
        scores[stype] = score
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else "general"


# ========== Prompt 模板 ==========

DISTILL_PROMPT_TEMPLATE = """# 知识蒸馏任务

你是一个知识蒸馏专家。请将以下 AI 对话会话转换为结构化的 Wiki 知识单元。

## Session 信息

- Session ID: {session_id}
- 来源: {source}
- 工作目录: {working_dir}
- 消息数: {message_count}
- 创建时间: {created_at}

## 原始对话

{conversation_text}

## 蒸馏要求

请分析以上对话，提取以下类型的知识：

1. **decision** - 关键决策（选择了什么方案、为什么）
2. **pattern** - 模式/最佳实践（可复用的方法）
3. **pitfall** - 陷阱/教训（踩过的坑、错误及修正）
4. **snippet** - 代码片段（有价值的代码、配置）
5. **reference** - 参考（链接、文档、工具推荐）
6. **todo** - 待办（需要后续跟进的事项，带建议时间）

## 输出格式

为每个提取的知识单元生成一个独立的 Markdown 文件，文件名格式：
`{{YYYY-MM-DD}}_{{type}}_{{keyword}}.md`

文件内容必须包含 YAML frontmatter：

```yaml
---
level: L2
type: decision | pattern | pitfall | snippet | reference | todo
source_session: {session_id}
tags: [tag1, tag2]
entities: ["技术名", "项目名", "人名"]
confidence: 0.0-1.0
created_at: "YYYY-MM-DD"
review_trigger: "YYYY-MM-DD"  # 仅 todo 类型需要
---
```

正文要求：
- 用结构化 Markdown（标题、列表、代码块）
- 包含「背景」「内容」「为什么重要」三个部分
- 代码块标注语言
- 如果是 pitfall，必须包含「原因」和「正确做法」
- 如果是 todo，必须包含「截止时间」和「验收标准」

## 写入位置

根据内容分类选择目录：
- 技术相关 → wiki/03-Tech/
- 项目相关 → wiki/02-Projects/
- 人物相关 → wiki/01-People/
- 概念/方法论 → wiki/04-Concepts/
- 不确定 → wiki/00-Inbox/

## 质量要求

- 只提取**真正有价值**的知识（排除闲聊、过渡语句）
- 每个知识单元必须**可独立理解**（不依赖原始对话上下文）
- 置信度 < 0.6 的内容不要写入
- 如果 session 中没有值得提取的知识，可以什么都不写，直接标记完成

完成蒸馏后，请使用以下命令标记任务完成：
```bash
{sys_executable} {module_path} --done {session_id}
```
"""


def build_prompt(task: Dict) -> str:
    """为 Agent 构建蒸馏 prompt（根据 session 类型选择专用模板）"""
    meta = task.get("meta", {})
    messages = task.get("messages", [])

    # 1. 判断 session 类型
    session_type = _detect_session_type(messages)

    # 2. 格式化对话文本
    conversation_lines = []
    for i, msg in enumerate(messages, 1):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if len(content) > 2000:
            content = content[:2000] + "\n\n[...内容过长，已截断...]"
        conversation_lines.append(f"### Message {i} ({role})\n\n{content}\n")
    conversation_text = "\n---\n\n".join(conversation_lines)

    # 3. 根据类型选择 prompt 模板
    if session_type in TYPE_DISTILL_PROMPTS and session_type != "general":
        # 使用类型专用模板
        type_prompt = TYPE_DISTILL_PROMPTS[session_type]
        prompt = type_prompt.format(session_content=conversation_text)

        # 在开头追加 session 元信息
        header = f"""# 知识蒸馏任务

> Session ID: {task['session_id']}
> 检测类型: **{session_type}**
> 来源: {meta.get('source', 'unknown')}
> 工作目录: {meta.get('working_dir', '')}
> 消息数: {len(messages)}
> 创建时间: {task.get('created_at', '')[:19]}

---

"""
        prompt = header + prompt
    else:
        # 使用通用模板
        prompt = DISTILL_PROMPT_TEMPLATE.format(
            session_id=task["session_id"],
            source=meta.get("source", "unknown"),
            working_dir=meta.get("working_dir", ""),
            message_count=len(messages),
            created_at=task.get("created_at", ""),
            conversation_text=conversation_text,
            sys_executable=sys.executable,
            module_path=str(Path(__file__).resolve()),
        )

    return prompt


def main():
    parser = argparse.ArgumentParser(description="Distillation Agent")
    parser.add_argument("--next", action="store_true",
                        help="获取下一个待蒸馏任务并输出 prompt")
    parser.add_argument("--list", action="store_true",
                        help="列出待处理任务摘要")
    parser.add_argument("--done", metavar="SESSION_ID",
                        help="标记任务完成")
    parser.add_argument("--fail", metavar="SESSION_ID",
                        help="标记任务失败")
    parser.add_argument("--output", default=None,
                        help="完成时写入的 wiki 文件路径")
    args = parser.parse_args()

    if args.list:
        pending = _list_pending()
        if pending:
            print(f"待蒸馏任务: {len(pending)}")
            for task in pending:
                meta = task.get("meta", {})
                print(f"  - {task['session_id'][:20]}... | "
                      f"消息: {len(task.get('messages', [])):3d} | "
                      f"来源: {meta.get('source', 'unknown'):8s}")
        else:
            print("无待蒸馏任务")
        return

    if args.next:
        task = _get_next()
        if not task:
            print("队列空，无待蒸馏任务")
            return

        prompt = build_prompt(task)

        # 同时保存 prompt 到临时文件，方便 Agent 读取
        prompt_path = QUEUE_DIR / f"{task['session_id']}_prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        print(f"任务: {task['session_id']}")
        print(f"Prompt 已保存: {prompt_path}")
        print(f"消息数: {len(task.get('messages', []))}")
        print()
        print("=" * 60)
        print(prompt)
        print("=" * 60)
        return

    if args.done:
        success = _mark_done(args.done, args.output)
        print(f"{'已标记完成' if success else '任务不存在'}: {args.done}")
        return

    if args.fail:
        success = _mark_failed(args.fail, "Agent execution failed")
        print(f"{'已标记失败' if success else '任务不存在'}: {args.fail}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
