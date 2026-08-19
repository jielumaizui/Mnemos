# -*- coding: utf-8 -*-
"""Lenient JSON extraction helpers for distillation LLM responses."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple, cast

logger = logging.getLogger(__name__)

JSON_PARSE_DIRECT = "direct_json"
JSON_PARSE_MARKDOWN = "markdown_json"
JSON_PARSE_BALANCED = "balanced_json"
JSON_PARSE_FIXED = "fixed_json"
JSON_PARSE_FAILED = "failed"


@dataclass(frozen=True)
class JsonParseAttempt:
    """Single JSON extraction attempt, suitable for logs and metrics."""

    path: str
    success: bool
    error_class: str = ""
    error_message: str = ""
    position: int | None = None
    fix: str = ""
    candidate_length: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JsonExtractionResult:
    """JSON extraction result with the parse path and failure metadata."""

    data: Optional[Any]
    path: str
    attempts: Tuple[JsonParseAttempt, ...]
    correction_attempts: int
    raw_length: int

    @property
    def success(self) -> bool:
        return self.data is not None

    @property
    def fallback_used(self) -> bool:
        return self.path not in {JSON_PARSE_DIRECT, JSON_PARSE_FAILED}

    @property
    def last_error(self) -> JsonParseAttempt | None:
        for attempt in reversed(self.attempts):
            if not attempt.success:
                return attempt
        return None

    def as_dict(self) -> Dict[str, Any]:
        last_error = self.last_error
        return {
            "success": self.success,
            "path": self.path,
            "fallback_used": self.fallback_used,
            "correction_attempts": self.correction_attempts,
            "raw_length": self.raw_length,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "error_class": last_error.error_class if last_error else "",
            "error_message": last_error.error_message if last_error else "",
        }


def _clean_field(value: str) -> str:
    """清理字段中嵌套的 JSON 代码块（LLM 偶尔在 title/core_content 里输出 markdown JSON）。"""
    if not value or not isinstance(value, str):
        return value or ""
    # 移除 ```json ... ``` 代码块，保留其中的纯文本（如果有）
    # 但通常嵌套 JSON 代码块没有有意义的纯文本，直接移除整个代码块
    cleaned = re.sub(r"```(?:json)?\s*.*?\s*```", "", value, flags=re.DOTALL)
    # 清理残留的 json 对象字面量（如 {"judgment": "knowledge"...}）
    cleaned = re.sub(
        r'\{[^{}]*"(?:judgment|reason|knowledge|fragments)"[^{}]*\}',
        "",
        cleaned,
    )
    # 清理首尾空白和多余换行
    cleaned = cleaned.strip()
    # 如果清理后为空，返回原值（避免过度清理）
    return cleaned if cleaned else value.strip()


def _try_parse_json_attempt(
    raw: str,
    path: str,
    attempts: List[JsonParseAttempt],
    *,
    fix: str = "",
) -> Optional[Any]:
    try:
        result = json.loads(raw)
        attempts.append(
            JsonParseAttempt(
                path=path,
                success=True,
                fix=fix,
                candidate_length=len(raw),
            )
        )
        return result
    except json.JSONDecodeError as exc:
        attempts.append(
            JsonParseAttempt(
                path=path,
                success=False,
                error_class=exc.__class__.__name__,
                error_message=exc.msg,
                position=exc.pos,
                fix=fix,
                candidate_length=len(raw),
            )
        )
    return None


def _try_parse_json(raw: str) -> Optional[Dict]:
    """直接尝试 json.loads，保留测试和内部 helper 的简洁入口。"""
    attempts: List[JsonParseAttempt] = []
    return cast(Optional[Dict], _try_parse_json_attempt(raw, JSON_PARSE_DIRECT, attempts))


def _extract_json_from_markdown(text: str) -> Optional[Dict]:
    """从 markdown JSON 代码块中提取 JSON。"""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return _try_parse_json(match.group(1))
    return None


def _find_balanced_json_candidates(text: str) -> List[str]:
    """扫描最外层平衡花括号，返回候选 JSON 字符串列表。"""
    candidates = []
    start = text.find("{")
    while start != -1:
        brace_count = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not in_string:
                in_string = True
            elif ch == '"' and in_string:
                in_string = False
            elif ch == "{" and not in_string:
                brace_count += 1
            elif ch == "}" and not in_string:
                brace_count -= 1
                if brace_count == 0:
                    candidates.append(text[start : i + 1])
                    break
        start = text.find("{", start + 1)
    return candidates


def _try_candidates(
    candidates: List[str],
    attempts: List[JsonParseAttempt] | None = None,
    *,
    path: str = JSON_PARSE_BALANCED,
) -> Optional[Dict]:
    """按长度降序尝试解析候选 JSON。"""
    attempts = attempts if attempts is not None else []
    for candidate in sorted(candidates, key=len, reverse=True):
        result = _try_parse_json_attempt(candidate, path, attempts)
        if result is not None:
            return cast(Dict, result)
    return None


def _apply_json_fixes(
    s: str,
    attempts: List[JsonParseAttempt] | None = None,
) -> Tuple[Optional[Dict], int]:
    """自动修复常见 JSON 语法错误并尝试解析。"""
    fixes = [
        ("remove_trailing_commas", lambda x: re.sub(r",(\s*[}\]])", r"\1", x)),
        ("single_quotes_to_double_quotes", lambda x: x.replace("'", '"')),
        ("strip_control_prefix", lambda x: x.strip("\ufeff\x00\x01\x02")),
        ("escape_unescaped_newlines", lambda x: re.sub(r"(?<!\\)\n", "\\n", x)),
    ]
    attempts = attempts if attempts is not None else []
    correction_attempts = 0
    for fix_name, fix in fixes:
        s = fix(s)
        correction_attempts += 1
        result = _try_parse_json_attempt(
            s,
            JSON_PARSE_FIXED,
            attempts,
            fix=fix_name,
        )
        if result is not None:
            return result, correction_attempts  # type: ignore[return-value]
    return None, correction_attempts


def _try_fix_candidates(
    candidates: List[str],
    attempts: List[JsonParseAttempt] | None = None,
) -> Tuple[Optional[Dict], int]:
    """对候选 JSON 应用修复策略。"""
    attempts = attempts if attempts is not None else []
    total_corrections = 0
    for candidate in sorted(candidates, key=len, reverse=True):
        result, correction_attempts = _apply_json_fixes(candidate, attempts)
        total_corrections += correction_attempts
        if result is not None:
            return result, total_corrections
    return None, total_corrections


def _try_fix_whole_text(
    text: str,
    attempts: List[JsonParseAttempt] | None = None,
) -> Tuple[Optional[Dict], int]:
    """对整个文本应用修复策略。"""
    return _apply_json_fixes(text, attempts)


def _result(
    data: Optional[Any],
    path: str,
    attempts: List[JsonParseAttempt],
    correction_attempts: int,
    raw_length: int,
) -> JsonExtractionResult:
    result = JsonExtractionResult(
        data=data,
        path=path,
        attempts=tuple(attempts),
        correction_attempts=correction_attempts,
        raw_length=raw_length,
    )
    if path == JSON_PARSE_FAILED:
        last_error = result.last_error
        logger.warning(
            "[distillation_engine] JSON extraction failed path=failed raw_length=%d "
            "attempts=%d correction_attempts=%d last_error=%s:%s",
            raw_length,
            len(attempts),
            correction_attempts,
            last_error.error_class if last_error else "unknown",
            last_error.error_message if last_error else "unknown",
        )
    elif path != JSON_PARSE_DIRECT:
        logger.debug(
            "[distillation_engine] JSON extraction fallback succeeded path=%s "
            "raw_length=%d attempts=%d correction_attempts=%d",
            path,
            raw_length,
            len(attempts),
            correction_attempts,
        )
    return result


def extract_json_with_metadata(text: str) -> JsonExtractionResult:
    """从文本中提取 JSON，并返回直接/回退/修复路径元数据。"""
    if not text:
        empty_attempts = [
            JsonParseAttempt(
                path=JSON_PARSE_FAILED,
                success=False,
                error_class="EmptyResponse",
                error_message="empty response",
                candidate_length=0,
            )
        ]
        return _result(None, JSON_PARSE_FAILED, empty_attempts, 0, 0)

    parse_attempts: List[JsonParseAttempt] = []
    correction_attempts = 0
    raw_length = len(text)

    result = _try_parse_json_attempt(text, JSON_PARSE_DIRECT, parse_attempts)
    if result is not None:
        return _result(result, JSON_PARSE_DIRECT, parse_attempts, correction_attempts, raw_length)

    markdown_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if markdown_match:
        result = _try_parse_json_attempt(
            markdown_match.group(1),
            JSON_PARSE_MARKDOWN,
            parse_attempts,
        )
    else:
        result = None
    if result is not None:
        return _result(result, JSON_PARSE_MARKDOWN, parse_attempts, correction_attempts, raw_length)

    candidates = _find_balanced_json_candidates(text)
    result = _try_candidates(candidates, parse_attempts, path=JSON_PARSE_BALANCED)
    if result is not None:
        return _result(result, JSON_PARSE_BALANCED, parse_attempts, correction_attempts, raw_length)

    result, count = _try_fix_candidates(candidates, parse_attempts)
    correction_attempts += count
    if result is not None:
        return _result(result, JSON_PARSE_FIXED, parse_attempts, correction_attempts, raw_length)

    result, count = _try_fix_whole_text(text, parse_attempts)
    correction_attempts += count
    if result is not None:
        return _result(result, JSON_PARSE_FIXED, parse_attempts, correction_attempts, raw_length)

    return _result(None, JSON_PARSE_FAILED, parse_attempts, correction_attempts, raw_length)


def extract_json(text: str) -> Optional[Dict]:
    """从文本中提取 JSON，带容错处理（针对 DeepSeek-V3 等模型）。"""
    return extract_json_with_metadata(text).data  # type: ignore[return-value]
