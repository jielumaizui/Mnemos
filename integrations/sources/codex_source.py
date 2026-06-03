# -*- coding: utf-8 -*-
"""
CodexSource — Codex Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Codex 的 rollout 文件按 session 结束时一次性写入（JSONL 格式），
使用 on_created 事件为主 + 启动时全量扫描。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class CodexSource(AgentSource):
    """Codex 数据源插件"""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def model_tag(self) -> str:
        return "codex"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        # 环境变量优先
        for env_key in ("CODEX_HOME", "XDG_CONFIG_HOME"):
            env = config.get(f"integrations.codex.{env_key.lower()}")
            if env:
                p = Path(env).expanduser()
                if p.exists():
                    return p

        import os
        for env_key in ("CODEX_HOME", "XDG_CONFIG_HOME"):
            val = os.getenv(env_key)
            if val:
                if env_key == "XDG_CONFIG_HOME":
                    p = Path(val) / "codex"
                else:
                    p = Path(val).expanduser()
                if p.exists():
                    return p

        # 标准路径
        for std in ("~/.codex", "~/.config/codex"):
            p = Path(std).expanduser()
            if p.exists():
                return p
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["created"],
            "debounce": 1.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Codex rollout 文件"""
        base = self.data_dir
        if not base:
            return []

        sessions_dir = base / "sessions"
        if not sessions_dir.exists():
            return []

        sessions = []
        for jsonl_file in sessions_dir.rglob("rollout-*.jsonl"):
            # 从文件名提取 session_id
            uuid_match = re.search(r'([a-f0-9-]{36})', jsonl_file.name)
            session_id = uuid_match.group(1) if uuid_match else jsonl_file.stem

            sessions.append(SessionInfo(
                session_id=session_id,
                source_path=jsonl_file,
                working_dir=str(jsonl_file.parent),
                mtime=jsonl_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Codex rollout JSONL 文件为 Turn 列表"""
        messages = self._parse_rollout(session_path)
        return self._pair_messages_to_turns(messages)

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """Codex 聚合状态：sessions 目录下所有 rollout 文件"""
        session_dir = session_info.source_path.parent
        files = list(session_dir.rglob("rollout-*.jsonl"))
        if not files:
            return None
        total_size = 0
        max_mtime = 0
        file_entries = []
        for f in sorted(files):
            try:
                stat = f.stat()
                total_size += stat.st_size
                max_mtime = max(max_mtime, stat.st_mtime)
                file_entries.append(f"{f.name}:{stat.st_size}:{stat.st_mtime}")
            except OSError:
                pass
        import hashlib
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
            "reasoning": "unknown",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "full",
        }

    def _parse_rollout(self, rollout_path: Path) -> List[Dict[str, Any]]:
        """解析 rollout 文件，提取消息列表 — P0-6 完整录入版"""
        messages = []
        raw_event_refs = []
        try:
            with open(rollout_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = obj.get("type", "")

                    if event_type == "response_item":
                        payload = obj.get("payload", {})
                        if payload.get("type") == "message":
                            role = payload.get("role", "")
                            texts = []
                            for block in payload.get("content", []):
                                if isinstance(block, dict):
                                    btype = block.get("type", "")
                                    if btype in ("input_text", "output_text"):
                                        texts.append(block.get("text", ""))
                                    elif btype == "tool_call":
                                        raw_event_refs.append({"event_type": "tool_call", "raw": block})
                                    elif btype == "tool_output":
                                        raw_event_refs.append({"event_type": "tool_output", "raw": block})
                                    else:
                                        raw_event_refs.append({"event_type": btype, "raw": block})
                            if texts and role:
                                messages.append({
                                    "role": role,
                                    "content": "\n".join(texts),
                                    "raw_event_refs": list(raw_event_refs),
                                })
                                raw_event_refs = []

                    elif event_type == "event_msg":
                        payload = obj.get("payload", {})
                        if payload.get("type") == "user_message":
                            msg = payload.get("message", "")
                            if msg:
                                # P0-6: user_message 也应附加 raw_event_refs，然后清空
                                messages.append({
                                    "role": "user",
                                    "content": str(msg),
                                    "raw_event_refs": list(raw_event_refs),
                                })
                                raw_event_refs = []
                        else:
                            # P0-6: 保留非 message 事件引用
                            raw_event_refs.append({"event_type": event_type, "raw": obj})
                    else:
                        # P0-6: 保留所有非 message 事件
                        raw_event_refs.append({"event_type": event_type, "raw": obj})

        except Exception as e:
            logger.warning(f"[CodexSource] 读取失败 {rollout_path}: {e}")

        return messages

    def _pair_messages_to_turns(self, messages: List[Dict[str, Any]]) -> List[Turn]:
        """将消息列表配对为 Turn 列表"""
        turns = []
        user_content = ""
        assistant_content = ""
        turn_number = 0
        turn_raw_events: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            msg_raw_events = msg.get("raw_event_refs", [])

            if role == "user":
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        raw_event_refs=turn_raw_events,
                        source_files=[str(self.data_dir)] if self.data_dir else [],
                        completeness={
                            "visible_text": "full",
                            "tool_results": "unavailable",
                            "reasoning": "unavailable",
                            "attachments": "unavailable",
                            "truncated": False,
                            "loss_reasons": completeness_loss,
                        },
                    ))
                    turn_number += 1
                user_content = content
                assistant_content = ""
                turn_raw_events = []
                completeness_loss = []

            elif role == "assistant":
                assistant_content += ("\n\n" if assistant_content else "") + content
                if msg_raw_events:
                    turn_raw_events.extend(msg_raw_events)

        # 保存最后一轮
        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                raw_event_refs=turn_raw_events,
                source_files=[str(self.data_dir)] if self.data_dir else [],
                completeness={
                    "visible_text": "full",
                    "tool_results": "unavailable",
                    "reasoning": "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": completeness_loss,
                },
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Codex 自定义标签"""
        return []
