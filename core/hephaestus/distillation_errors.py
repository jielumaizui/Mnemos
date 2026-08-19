# -*- coding: utf-8 -*-
"""API failure reporting for the distillation pipeline."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.hephaestus.distillation_pause import RESUME_AFTER_SECONDS
from core.utils import atomic_write_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from core.hephaestus.distill_response import DistillBackendResponse


class DistillationAPIError(Exception):
    """所有 LLM API（主备）均不可用，蒸馏必须暂停。"""

    def __init__(
        self,
        message: str = "所有 LLM API 不可用",
        chain_desc: str = "",
        *,
        response_evidence: "DistillBackendResponse | None" = None,
    ):
        self.chain_desc = chain_desc
        self.response_evidence = response_evidence
        super().__init__(message)


def generate_distillation_error_report(error: DistillationAPIError, wiki_dir: Path) -> Path:
    """生成蒸馏 API 故障报告，写入 Wiki 99-Reports 并触发 Obsidian 弹窗。

    基于错误内容指纹去重：相同 API 故障在短时间内不会生成多个重复报告。
    """
    reports_dir = wiki_dir / "99-Reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    resume_after = RESUME_AFTER_SECONDS
    # 稳定指纹：排除生成时间，避免相同错误产生多个文件
    fingerprint = f"{error.chain_desc or ''}\n{str(error)}\n{resume_after}"
    content_hash = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]

    # 若已存在相同指纹的报告，直接返回，避免重复文件
    for existing in reports_dir.glob("蒸馏API故障报告-*.md"):
        try:
            if content_hash in existing.name:
                logger.info("[Distillation] 相同 API 故障报告已存在: %s", existing.name)
                return existing
        # DEBT(S8): 容错跳过，避免单条记录中断批量处理
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError
        ):
            continue

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = reports_dir / f"蒸馏API故障报告-{timestamp}-{content_hash}.md"

    report_content = f"""---
标题: 蒸馏 API 故障报告
生成时间: {datetime.now().isoformat()}
状态: 紧急
类型: system-alert
---

# 🔴 蒸馏系统 API 故障

## 故障描述

所有配置的 LLM API 均不可用，蒸馏已自动暂停。

## API 主备链

{error.chain_desc or '未配置'}

## 错误信息

```
{str(error)}
```

## 建议排查步骤

1. 检查 API Key 是否有效（Hermes `~/.hermes/auth.json` 或环境变量）
2. 检查 API 余额/配额
3. 检查网络连接
4. 如使用 DMX API，确认模型状态（kimi-k2.5-free、MiniMax-M3-free）
5. 如使用 SiliconFlow，确认模型状态（deepseek-ai/DeepSeek-V4-Flash）

## 自动恢复

系统将在 **{resume_after // 60} 分钟**后自动尝试恢复，优先使用 PRIMARY API。
"""
    atomic_write_text(report_path, report_content, encoding="utf-8")

    # 触发 Obsidian 弹窗
    try:
        from core.app.obsidian_opener import open_obsidian

        rel_path = report_path.relative_to(wiki_dir)
        open_obsidian(page_path=str(rel_path))
    except ImportError as e:
        logger.warning("[Distillation] Obsidian 弹窗失败: %s", e, exc_info=True)

    return report_path
