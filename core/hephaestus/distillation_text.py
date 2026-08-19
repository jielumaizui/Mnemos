# -*- coding: utf-8 -*-
"""Message cleaning and session text construction for distillation."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config
from core.hephaestus.tokenizer import get_tokenizer

EFFECTIVE_MAX_TOKENS = 24000
PER_MESSAGE_TOKEN_LIMIT = 6000
PRIVATE_THINKING_PATTERN = re.compile(
    r"\[thinking\].*?(?:\[/thinking\]|$)", re.DOTALL
)


def _clean_message_content_with_exclusions(
    content: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Return visible content plus non-content metadata for explicit exclusions."""
    if not content:
        return "", []
    exclusions = [
        {
            "kind": "private_thinking",
            "start": match.start(),
            "end": match.end(),
            "char_count": match.end() - match.start(),
        }
        for match in PRIVATE_THINKING_PATTERN.finditer(content)
    ]
    return PRIVATE_THINKING_PATTERN.sub("", content), exclusions


def clean_message_content(content: str) -> str:
    """Remove explicit private-thinking blocks without dropping visible evidence."""
    return _clean_message_content_with_exclusions(content)[0]


def _resolve_token_limits(
    max_tokens: int | None,
    per_message_token_limit: Optional[int],
) -> Tuple[int, Optional[int]]:
    """解析总会话与单条消息的实际 token 上限。"""
    cfg = get_config()

    effective_max_tokens = max_tokens
    if effective_max_tokens is None:
        effective_max_tokens = int(
            cfg.get("distill.effective_max_tokens", EFFECTIVE_MAX_TOKENS) or EFFECTIVE_MAX_TOKENS
        )

    return effective_max_tokens, per_message_token_limit


def _build_message_lines(
    messages: List[Dict],
    effective_msg_limit: Optional[int],
    tokenizer: Any,
) -> Tuple[List[str], List[Dict]]:
    """清洗消息并构建带 [role] 前缀的行列表，同时记录单条截断元数据。"""
    lines: List[str] = []
    message_truncations: List[Dict] = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        input_content = msg.get("content", "")
        if not input_content.strip():
            continue
        content, exclusions = _clean_message_content_with_exclusions(input_content)
        exclusion_meta = {
            "explicit_exclusion_count": len(exclusions),
            "explicit_excluded_chars": sum(item["char_count"] for item in exclusions),
            "explicit_exclusions": exclusions,
        }
        if not content:
            message_truncations.append(
                {
                    "turn": i + 1,
                    "role": role,
                    "input_length": len(input_content),
                    "original_length": 0,
                    "original_tokens": 0,
                    "kept_tokens": 0,
                    "truncated": False,
                    "fully_excluded": True,
                    **exclusion_meta,
                }
            )
            continue

        original_len = len(content)
        original_tokens = tokenizer.estimate(content)

        if effective_msg_limit and original_tokens > effective_msg_limit:
            content = tokenizer.truncate_to_tokens(content, effective_msg_limit)
            truncated = True
            kept_tokens = effective_msg_limit
        else:
            truncated = False
            kept_tokens = original_tokens

        message_truncations.append(
            {
                "turn": i + 1,
                "role": role,
                "input_length": len(input_content),
                "original_length": original_len,
                "original_tokens": original_tokens,
                "kept_tokens": kept_tokens,
                "truncated": truncated,
                "fully_excluded": False,
                **exclusion_meta,
            }
        )

        line = f"[{role}] {content}"
        lines.append(line)

    return lines, message_truncations


def _assemble_full_text(
    lines: List[str],
    total_tokens: int,
    out_meta: Optional[Dict],
    message_truncations: List[Dict],
) -> str:
    """总 token 未超限时组装完整文本并写回 out_meta。"""
    if out_meta is not None:
        out_meta.update(
            {
                "total_turns": len(lines),
                "head_turns": len(lines),
                "tail_turns": 0,
                "omitted_turns": 0,
                "truncated": False,
                "message_truncations": message_truncations,
                "distill_input_mode": "full",
                "total_tokens": total_tokens,
            }
        )
    return "\n\n".join(lines)


def _select_head_lines(
    lines: List[str], head_budget: int, tokenizer: Any
) -> Tuple[List[str], int]:
    """按 token 预算从头部选择行。"""
    head_lines: List[str] = []
    head_tokens = 0
    for line in lines:
        t = tokenizer.estimate(line)
        if head_tokens + t > head_budget:
            break
        head_lines.append(line)
        head_tokens += t
    return head_lines, head_tokens


def _select_tail_lines(
    lines: List[str], tail_budget: int, tokenizer: Any
) -> Tuple[List[str], int]:
    """按 token 预算从尾部选择行。"""
    tail_lines: List[str] = []
    tail_tokens = 0
    for line in reversed(lines):
        t = tokenizer.estimate(line)
        if tail_tokens + t > tail_budget:
            break
        tail_lines.insert(0, line)
        tail_tokens += t
    return tail_lines, tail_tokens


def _assemble_head_tail_text(
    lines: List[str],
    head_lines: List[str],
    tail_lines: List[str],
    head_tokens: int,
    tail_tokens: int,
    out_meta: Optional[Dict],
    message_truncations: List[Dict],
) -> str:
    """组装 head-tail 截断文本并写回 out_meta。"""
    total_turns = len(lines)
    head_count = len(head_lines)
    tail_count = len(tail_lines)

    # 避免头尾重叠：优先保留 head，tail 仅补充未出现过的行
    if head_count + tail_count > total_turns:
        seen = set(head_lines)
        tail_lines = [line for line in tail_lines if line not in seen]
        tail_count = len(tail_lines)

    head_text = "\n\n".join(head_lines) if head_lines else ""
    tail_text = "\n\n".join(tail_lines) if tail_lines else ""
    omitted_turns = max(0, total_turns - head_count - tail_count)

    if out_meta is not None:
        out_meta.update(
            {
                "total_turns": total_turns,
                "head_turns": head_count,
                "tail_turns": tail_count,
                "omitted_turns": omitted_turns,
                "truncated": True,
                "message_truncations": message_truncations,
                "distill_input_mode": "head_tail",
                "total_tokens": head_tokens + tail_tokens,
            }
        )

    parts = []
    if head_text:
        parts.append(head_text)
    parts.append(f"[... {omitted_turns} turns omitted; showing head + tail ...]")
    if tail_text:
        parts.append(tail_text)
    return "\n\n".join(parts)


def build_session_text(
    messages: List[Dict],
    max_tokens: int | None = None,
    per_message_token_limit: Optional[int] = None,
    out_meta: Optional[Dict] = None,
    lossless: bool = False,
) -> str:
    """从消息列表构建对话文本 — P0-5 三层蒸馏策略（token-based）。

    策略选择（由 process() 根据总会话 token 数决定）：
    1. 小会话（<=48000 tokens）: 完整输入
    2. 中会长话（48000-400000 tokens）: 分块蒸馏
    3. 超长会话（>400000 tokens）: 分块蒸馏（避免 head-tail 丢失信息）

    Args:
        max_tokens: 总会话 token 上限（默认 24000）
        per_message_token_limit: 单条消息截断上限（token）；None 表示不截断，
                                仅对极端长消息（>6000 tokens）自动截断
        out_meta: 如果传入 dict，会将截断信息写入其中
        lossless: canonical extraction 使用；即使格式化后超过预算也返回完整可见输入，
                  并通过 budget_overflow_tokens 记录预算差额，由上游 chunker 负责分片
    """
    tokenizer = get_tokenizer()
    effective_max_tokens, effective_msg_limit = _resolve_token_limits(
        max_tokens, per_message_token_limit
    )
    if lossless:
        effective_msg_limit = None

    lines, message_truncations = _build_message_lines(
        messages, effective_msg_limit, tokenizer
    )
    total_tokens = sum(tokenizer.estimate(line) for line in lines)
    if out_meta is not None:
        out_meta.update(
            {
                "explicit_exclusion_count": sum(
                    item["explicit_exclusion_count"] for item in message_truncations
                ),
                "explicit_excluded_chars": sum(
                    item["explicit_excluded_chars"] for item in message_truncations
                ),
            }
        )

    if lossless:
        text = _assemble_full_text(lines, total_tokens, out_meta, message_truncations)
        if out_meta is not None:
            out_meta.update(
                {
                    "lossless": True,
                    "budget_overflow_tokens": max(0, total_tokens - effective_max_tokens),
                    "silent_omission_count": 0,
                }
            )
        return text

    if total_tokens <= effective_max_tokens:
        return _assemble_full_text(lines, total_tokens, out_meta, message_truncations)

    omission_marker = "\n\n[... turns omitted; showing head + tail ...]\n\n"
    marker_tokens = tokenizer.estimate(omission_marker)
    usable_tokens = effective_max_tokens - marker_tokens
    head_budget = int(usable_tokens * 0.3)
    tail_budget = usable_tokens - head_budget

    head_lines, head_tokens = _select_head_lines(lines, head_budget, tokenizer)
    tail_lines, tail_tokens = _select_tail_lines(lines, tail_budget, tokenizer)

    return _assemble_head_tail_text(
        lines,
        head_lines,
        tail_lines,
        head_tokens,
        tail_tokens,
        out_meta,
        message_truncations,
    )
