# -*- coding: utf-8 -*-
"""
Tokenizer — 统一 Token 估算器

优先使用 tiktoken（cl100k_base，适用于 GPT-4/Claude/DeepSeek 等现代模型），
不可用时回退到基于字符的启发式估算。

使用方式：
    from core.hephaestus.tokenizer import get_tokenizer
    tokenizer = get_tokenizer()
    n_tokens = tokenizer.estimate(text)
    truncated = tokenizer.truncate_to_tokens(text, max_tokens=8000)
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class Tokenizer:
    """统一 Token 估算器 — tiktoken 优先，启发式回退。"""

    # 默认编码器：cl100k_base 覆盖 GPT-4、GPT-4o、Claude、DeepSeek-V3 等主流模型
    DEFAULT_ENCODING = "cl100k_base"

    # 启发式估算系数
    CHINESE_RATIO = 1.5
    ENGLISH_RATIO = 1.3

    def __init__(self, encoding_name: str | None = None):
        self._encoding_name = encoding_name or self.DEFAULT_ENCODING
        self._encoder = None
        self._encoder_failed = False

    def _get_encoder(self):
        """懒加载 tiktoken 编码器。"""
        if self._encoder is not None:
            return self._encoder
        if self._encoder_failed:
            return None
        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding(self._encoding_name)
            logger.debug("[Tokenizer] 使用 tiktoken 编码器: %s", self._encoding_name)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            self._encoder_failed = True
            self._encoder = None
            logger.info(
                "[Tokenizer] tiktoken 不可用，回退到启发式估算 " "(pip install tiktoken 可提升精度)"
            )
        return self._encoder

    def estimate(self, text: str) -> int:
        """估算文本的 token 数量。"""
        if not text:
            return 0
        encoder = self._get_encoder()
        if encoder is not None:
            try:
                return len(encoder.encode(text))
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                # tiktoken 异常时回退到启发式
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        return self._heuristic_estimate(text)

    def _heuristic_estimate(self, text: str) -> int:
        """基于字符的启发式 token 估算。"""
        chinese = len(re.findall(r"[一-龥]", text))
        english_words = re.findall(r"[a-zA-Z]+", text)
        # 短单词（≤3 字符）按单词数估算，长序列按字符数估算
        # 避免 "x" * 500 被当作 1 个单词严重低估
        word_tokens = sum(
            self.ENGLISH_RATIO if len(w) <= 3 else len(w) * 0.3 for w in english_words
        )
        word_chars = sum(len(w) for w in english_words)
        others = len(text) - chinese - word_chars
        return int(chinese * self.CHINESE_RATIO + word_tokens + max(0, others) * 0.5)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """将文本截断到不超过 max_tokens 个 token。

        优先使用 tiktoken 做精确截断；回退时按字符比例估算。
        """
        if not text:
            return text
        encoder = self._get_encoder()
        if encoder is not None:
            try:
                tokens = encoder.encode(text)
                if len(tokens) <= max_tokens:
                    return text
                return encoder.decode(tokens[:max_tokens])  # type: ignore[no-any-return]
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)
        # 回退：按估算比例截断
        est = self._heuristic_estimate(text)
        if est <= max_tokens:
            return text
        ratio = max_tokens / est
        # 在比例截断处向后搜索最近的换行边界，避免切断单词
        cutoff = int(len(text) * ratio)
        # 尝试在句子/段落边界截断
        for boundary in ("\n\n", "\n", "。", ". ", "；", "; "):
            pos = text.rfind(boundary, max(0, cutoff - 200), cutoff + 50)
            if pos > 0:
                return text[: pos + len(boundary)].rstrip()
        return text[:cutoff]

    def _split_once(self, text: str, max_tokens: int) -> Tuple[str, str]:
        """把 text 切成 (head, tail)，其中 head 是不超过 max_tokens 的最大前缀。

        优先在段落、句子、单词边界处切分；无法找到合适边界时按 token 硬切。
        """
        if not text:
            return text, ""
        if self.estimate(text) <= max_tokens:
            return text, ""

        encoder = self._get_encoder()
        if encoder is not None:
            try:
                tokens = encoder.encode(text)
                if len(tokens) <= max_tokens:
                    return text, ""
                # 精确前缀
                candidate = encoder.decode(tokens[:max_tokens])
                # 在自然边界处回退（保留分隔符，避免拼接时丢失空格/换行）
                for boundary in ("\n\n", "\n", "。", ". ", "；", "; "):
                    pos = candidate.rfind(boundary)
                    if pos > 0:
                        head = candidate[: pos + len(boundary)]
                        if self.estimate(head) <= max_tokens:
                            return head, text[len(head) :]
                # 单词边界（保留末尾空格，保证拼接后仍是单空格分隔）
                pos = candidate.rfind(" ")
                if pos > 0:
                    head = candidate[: pos + 1]
                    if self.estimate(head) <= max_tokens:
                        return head, text[len(head) :]
                # 硬切 token 边界
                return candidate, text[len(candidate) :]
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                logging.getLogger(__name__).warning("Unexpected error", exc_info=True)

        # 启发式回退：优先在 cutoff 之前找自然边界，避免切断单词
        est = self._heuristic_estimate(text)
        if est <= max_tokens:
            return text, ""
        ratio = max_tokens / est
        cutoff = int(len(text) * ratio)

        # 1. 段落/句子/分号边界
        for boundary in ("\n\n", "\n", "。", ". ", "；", "; "):
            pos = text.rfind(boundary, 0, cutoff + len(boundary))
            if pos > 0:
                head = text[: pos + len(boundary)]
                if self.estimate(head) <= max_tokens:
                    return head, text[len(head) :]

        # 2. cutoff 之前的空格
        pos = text.rfind(" ", 0, cutoff + 1)
        if pos > 0:
            head = text[: pos + 1]
            if self.estimate(head) <= max_tokens:
                return head, text[len(head) :]

        # 3. 若 cutoff 卡在长单词中间，则向后找下一个空格
        pos = text.find(" ", cutoff)
        if pos >= 0:
            head = text[: pos + 1]
            if self.estimate(head) <= max_tokens:
                return head, text[len(head) :]

        # 4. 兜底：硬切
        head = text[:cutoff]
        return head, text[cutoff:]

    def split_to_tokens(self, text: str, max_tokens: int) -> List[str]:
        """把文本按自然边界切分成多段，每段不超过 max_tokens 个 token。

        与 truncate_to_tokens 不同，split_to_tokens 会返回所有分段，**不丢失内容**。
        """
        if not text or max_tokens <= 0:
            return [text] if text else []
        if self.estimate(text) <= max_tokens:
            return [text]

        chunks: List[str] = []
        remaining = text
        while self.estimate(remaining) > max_tokens:
            head, tail = self._split_once(remaining, max_tokens)
            if not head or len(head) >= len(remaining):
                # 安全兜底：避免空 head 或无进展导致死循环
                head = self.truncate_to_tokens(remaining, max_tokens)
                tail = remaining[len(head) :]
            elif self.estimate(head) > max_tokens:
                # 边界回退导致 token 数仍超限，硬切到精确上限
                head = self.truncate_to_tokens(head, max_tokens)
                tail = remaining[len(head) :]
            chunks.append(head)
            remaining = tail
        if remaining:
            chunks.append(remaining)
        return chunks


# 全局单例（懒初始化）
_tokenizer_instance: Optional[Tokenizer] = None


def get_tokenizer(encoding_name: str | None = None) -> Tokenizer:
    """获取全局 Tokenizer 实例。"""
    global _tokenizer_instance
    if _tokenizer_instance is None:
        _tokenizer_instance = Tokenizer(encoding_name=encoding_name)
    return _tokenizer_instance


def estimate_tokens(text: str) -> int:
    """便捷函数：估算文本 token 数。"""
    return get_tokenizer().estimate(text)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """便捷函数：将文本截断到指定 token 数。"""
    return get_tokenizer().truncate_to_tokens(text, max_tokens)


def split_to_tokens(text: str, max_tokens: int) -> List[str]:
    """便捷函数：把文本按 token 数切分成多段，不丢失内容。"""
    return get_tokenizer().split_to_tokens(text, max_tokens)
