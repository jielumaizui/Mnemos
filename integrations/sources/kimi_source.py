# -*- coding: utf-8 -*-
"""
KimiSource — Kimi Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
支持 Kimi 的归档机制：context.jsonl + context_1.jsonl 等。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class KimiSource(AgentSource):
    """Kimi 数据源插件"""

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def model_tag(self) -> str:
        return "kimi-k2.5"

    @property
    def data_dir(self) -> Optional[Path]:
        path = Path.home() / ".kimi"
        return path if path.exists() else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "hybrid",
            "events": ["modified", "created"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Kimi 会话"""
        base = self.data_dir
        if not base:
            return []

        sessions_dir = base / "sessions"
        if not sessions_dir.exists():
            return []

        sessions = []
        for context_file in sessions_dir.rglob("context.jsonl"):
            session_dir = context_file.parent
            workspace_dir = session_dir.parent
            sessions.append(SessionInfo(
                session_id=session_dir.name,
                source_path=context_file,
                working_dir=str(workspace_dir),
                mtime=context_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Kimi 会话文件（含归档文件）为 Turn 列表"""
        all_messages = self._read_all_context_files(session_path.parent)
        return self._pair_messages_to_turns(all_messages)

    @staticmethod
    def _context_file_sort_key(path: Path) -> tuple:
        """自然排序 key：context_1.jsonl < context_2.jsonl < context_10.jsonl < context.jsonl"""
        if path.name == "context.jsonl":
            return (1, 0)  # 当前活跃文件最后读
        m = re.match(r"context_(\d+)\.jsonl$", path.name)
        if m:
            return (0, int(m.group(1)))
        return (2, 0)

    def _read_all_context_files(self, session_dir: Path) -> List[Dict[str, Any]]:
        """读取所有 context*.jsonl 文件（包括归档），按自然顺序合并"""
        context_files = sorted(session_dir.glob("context*.jsonl"), key=self._context_file_sort_key)
        all_messages = []

        for cf in context_files:
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            all_messages.append(msg)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"[KimiSource] 读取失败 {cf}: {e}")

        return all_messages

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """Kimi 多文件聚合状态：所有 context*.jsonl + wire.jsonl"""
        session_dir = session_info.source_path.parent
        files = list(session_dir.glob("context*.jsonl"))
        wire = session_dir / "wire.jsonl"
        if wire.exists():
            files.append(wire)

        if not files:
            return None

        total_size = 0
        max_mtime = 0
        for f in files:
            try:
                stat = f.stat()
                total_size += stat.st_size
                max_mtime = max(max_mtime, stat.st_mtime)
            except OSError:
                pass

        # fingerprint 按自然排序文件名:size:mtime 拼接后 hash
        file_entries = []
        for f in sorted(files, key=self._context_file_sort_key):
            try:
                stat = f.stat()
                file_entries.append(f"{f.name}:{stat.st_size}:{stat.st_mtime}")
            except OSError:
                pass
        fingerprint = hashlib.md5("|".join(file_entries).encode()).hexdigest()[:16]

        return {
            "mtime": max_mtime,
            "size": total_size,
            "file_count": len(files),
            "fingerprint": fingerprint,
        }

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

    def _pair_messages_to_turns(self, messages: List[Dict[str, Any]]) -> List[Turn]:
        """将消息列表配对为 Turn 列表 — 完整录入版（P0-6）"""
        turns = []
        user_content = ""
        assistant_content = ""
        turn_meta: Dict[str, Any] = {}
        turn_number = 0
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_reasoning = ""
        turn_raw_events: List[Dict[str, Any]] = []
        turn_source_files: List[str] = []
        completeness_loss: List[str] = []

        for msg in messages:
            role = msg.get("role", "")

            # 跳过系统消息，但记录到 raw_event_refs
            if role in ("_system_prompt", "_checkpoint", "_usage", "system"):
                turn_raw_events.append({"role": role, "event_type": "system", "raw": msg})
                continue

            if role == "user":
                # 如果已有 assistant 内容，保存上一轮
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        metadata=turn_meta,
                        tool_calls=turn_tool_calls,
                        tool_results=turn_tool_results,
                        reasoning=turn_reasoning,
                        raw_event_refs=turn_raw_events,
                        source_files=turn_source_files,
                        completeness={
                            "visible_text": "full",
                            "tool_results": "full" if turn_tool_results else "unavailable",
                            "reasoning": "full" if turn_reasoning else "unavailable",
                            "attachments": "unavailable",
                            "truncated": False,
                            "loss_reasons": completeness_loss,
                        },
                    ))
                    turn_number += 1

                # 处理列表格式 [{"type": "text", "text": "..."}]
                raw = msg.get("content", "")
                if isinstance(raw, list):
                    texts = []
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        itype = item.get("type", "")
                        if itype == "text":
                            texts.append(item.get("text", ""))
                        else:
                            # 未知块记录到 raw_event_refs，不静默丢弃
                            turn_raw_events.append({"role": "user", "event_type": itype, "raw": item})
                            completeness_loss.append(f"user_unknown_block:{itype}")
                    user_content = "\n".join(texts)
                else:
                    user_content = str(raw)

                assistant_content = ""
                turn_meta = {}
                turn_tool_calls = []
                turn_tool_results = []
                turn_reasoning = ""
                turn_raw_events = []
                turn_source_files = []
                completeness_loss = []

            elif role == "assistant":
                parts = msg.get("content", [])
                texts = []
                reasoning = ""

                if isinstance(parts, list):
                    for p in parts:
                        if not isinstance(p, dict):
                            continue
                        ptype = p.get("type", "")
                        if ptype == "text":
                            texts.append(p.get("text", ""))
                        elif ptype == "think":
                            # 不再截断，完整保留 reasoning
                            reasoning = p.get("think", "")
                        elif ptype == "tool_use":
                            turn_tool_calls.append({
                                "name": p.get("name", "unknown"),
                                "input": p.get("input", {}),
                                "id": p.get("id", ""),
                            })
                        else:
                            # 未知块记录到 raw_event_refs
                            turn_raw_events.append({"role": "assistant", "event_type": ptype, "raw": p})
                            completeness_loss.append(f"assistant_unknown_block:{ptype}")
                elif isinstance(parts, str):
                    texts.append(parts)

                assistant_content = "\n\n".join(texts)
                if reasoning:
                    turn_reasoning = reasoning
                    turn_meta["reasoning"] = reasoning

            elif role == "tool":
                # tool 结果结构化保存
                tool_content = msg.get("content", "")
                tool_texts = []
                if isinstance(tool_content, list):
                    for item in tool_content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            tool_texts.append(item.get("text", ""))
                        else:
                            turn_raw_events.append({"role": "tool", "event_type": "unknown", "raw": item})
                else:
                    tool_texts = [str(tool_content)]

                tool_result_text = "\n".join(tool_texts)
                if tool_result_text:
                    turn_tool_results.append({
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "content": tool_result_text,
                        "name": msg.get("name", ""),
                    })
                    assistant_content += f"\n\n[TOOL_RESULT]{tool_result_text}[/TOOL_RESULT]"

        # 保存最后一轮
        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata=turn_meta,
                tool_calls=turn_tool_calls,
                tool_results=turn_tool_results,
                reasoning=turn_reasoning,
                raw_event_refs=turn_raw_events,
                source_files=turn_source_files,
                completeness={
                    "visible_text": "full",
                    "tool_results": "full" if turn_tool_results else "unavailable",
                    "reasoning": "full" if turn_reasoning else "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": completeness_loss,
                },
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Kimi 自定义标签"""
        tags = []
        if turn.metadata.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
