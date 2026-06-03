# -*- coding: utf-8 -*-
"""
ClaudeSource — Claude Code Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
从 claude_live_sync.py 迁移的完整 JSONL 消息解析逻辑：
  - thinking/tool_use/tool_result 内容块提取
  - 增量同步（offset 追踪）
  - 工具调用/推理内容提取
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class ClaudeSource(AgentSource):
    """Claude Code 数据源插件"""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def model_tag(self) -> str:
        return "claude-code"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        return config.claude_data_dir if hasattr(config, "claude_data_dir") else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Claude 会话"""
        base = self.data_dir
        if not base:
            return []

        projects_dir = base / "projects"
        if not projects_dir.exists():
            return []

        sessions = []
        for jsonl_file in projects_dir.rglob("*.jsonl"):
            sessions.append(SessionInfo(
                session_id=jsonl_file.stem,
                source_path=jsonl_file,
                working_dir=str(jsonl_file.parent),
                mtime=jsonl_file.stat().st_mtime,
            ))
        return sessions

    def completeness_capabilities(self) -> Dict[str, Any]:
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "reasoning": "available",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "full",
        }

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 JSONL 会话文件为 Turn 列表 — P0-6 完整录入版"""
        turns = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.warning(f"[ClaudeSource] 读取失败 {session_path}: {e}")
            return turns

        current_user = ""
        current_assistant = ""
        current_meta: Dict[str, Any] = {}
        turn_number = 0
        completeness_loss: List[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                completeness_loss.append("json_decode_error")
                continue

            standardized = self._standardize_message(msg)
            if not standardized:
                continue

            role = standardized.get("role", "")
            content = standardized.get("content", "")

            if role == "user":
                if current_assistant:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=current_user,
                        assistant_content=current_assistant,
                        timestamp=current_meta.get("timestamp"),
                        metadata=current_meta,
                        tool_calls=current_meta.get("tool_calls", []),
                        tool_results=current_meta.get("tool_results", []),
                        reasoning=current_meta.get("reasoning", ""),
                        raw_event_refs=current_meta.get("raw_event_refs", []),
                        source_files=[str(session_path)],
                        completeness={
                            "visible_text": "full",
                            "tool_results": "full" if current_meta.get("tool_results") else "unavailable",
                            "reasoning": "full" if current_meta.get("reasoning") else "unavailable",
                            "attachments": "unavailable",
                            "truncated": False,
                            "loss_reasons": completeness_loss,
                        },
                    ))
                    turn_number += 1
                    current_user = content
                    current_assistant = ""
                    current_meta = {}
                    completeness_loss = []
                else:
                    current_user = content
            elif role == "assistant":
                current_assistant = content
                current_meta = {
                    "timestamp": standardized.get("timestamp"),
                    "tool_calls": standardized.get("tool_calls", []),
                    "tool_results": standardized.get("tool_results", []),
                    "reasoning": standardized.get("reasoning", ""),
                    "raw_event_refs": standardized.get("raw_event_refs", []),
                }

        # 保存最后一轮
        if current_user or current_assistant:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=current_user,
                assistant_content=current_assistant,
                timestamp=current_meta.get("timestamp"),
                metadata=current_meta,
                tool_calls=current_meta.get("tool_calls", []),
                tool_results=current_meta.get("tool_results", []),
                reasoning=current_meta.get("reasoning", ""),
                raw_event_refs=current_meta.get("raw_event_refs", []),
                source_files=[str(session_path)],
                completeness={
                    "visible_text": "full",
                    "tool_results": "full" if current_meta.get("tool_results") else "unavailable",
                    "reasoning": "full" if current_meta.get("reasoning") else "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": completeness_loss,
                },
            ))

        return turns

    def _standardize_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        标准化 Claude Code JSONL 消息格式 — P0-6 完整录入版。

        修复：不再在解析阶段截断 thinking / tool_result。
        大内容由 CaptureService 层写入 artifact。
        """
        if not isinstance(msg, dict):
            return None

        message_data = msg.get("message", msg)
        role = message_data.get("role", "")
        if not role:
            role = msg.get("type", "")

        raw_content = message_data.get("content", "")
        tool_calls_raw = message_data.get("tool_calls") or msg.get("tool_calls") or []
        tool_calls = tool_calls_raw if isinstance(tool_calls_raw, list) else []
        tool_results_raw = msg.get("toolUseResult") or msg.get("tool_results") or []
        tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []
        reasoning = ""
        raw_event_refs = []

        # 处理 content（可能是字符串或内容块数组）
        if isinstance(raw_content, list):
            content_parts = []
            for part in raw_content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                if part_type == "text":
                    content_parts.append(part.get("text", ""))
                elif part_type in ("thinking", "reasoning"):
                    # P0-6: 不再截断 reasoning
                    reasoning = part.get("thinking", part.get("text", ""))
                elif part_type == "tool_use":
                    tool_calls.append({
                        "name": part.get("name", "unknown"),
                        "input": part.get("input", {}),
                        "id": part.get("id", ""),
                    })
                elif part_type == "tool_result":
                    # P0-6: 完整保留 tool_result，不再截断 stdout/stderr
                    tool_results.append({
                        "stdout": str(part.get("content", "")),
                        "tool_use_id": part.get("tool_use_id", ""),
                    })
                elif "content" in part:
                    content_parts.append(str(part["content"]))
                else:
                    # 未知块记录到 raw_event_refs
                    raw_event_refs.append({"type": part_type, "raw": part})
            content = "\n".join(content_parts)
        else:
            content = str(raw_content)

        if not role or not content:
            return None

        result = {
            "role": role,
            "content": content,
            "timestamp": msg.get("timestamp", ""),
        }

        if tool_calls:
            result["tool_calls"] = [
                {
                    "name": t.get("name", t.get("function", {}).get("name", "unknown")),
                    "input": t.get("input", t.get("arguments", t.get("function", {}).get("arguments", {}))),
                    "id": t.get("id", ""),
                }
                for t in tool_calls[:10]
            ]

        if tool_results:
            if isinstance(tool_results, dict):
                result["tool_results"] = [{
                    "stdout": str(tool_results.get("stdout", "")),
                    "stderr": str(tool_results.get("stderr", "")),
                    "tool_use_id": tool_results.get("tool_use_id", ""),
                }]
            elif isinstance(tool_results, list):
                result["tool_results"] = tool_results

        if reasoning:
            result["reasoning"] = reasoning

        if raw_event_refs:
            result["raw_event_refs"] = raw_event_refs

        return result

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Claude 自定义标签"""
        tags = []
        meta = turn.metadata
        if meta.get("tool_calls"):
            tags.append("has-tools=true")
        if meta.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
