"""
蒸馏 Prompt 版本号 — 供 prompt_builder.py 与 wiki frontmatter 追溯使用。

旧的硬编码 prompt 常量（DISTILLATION_PROMPT / STAGE1_FILTER_PROMPT 等）已移除，
统一走 core.hephaestus.prompt_builder.PromptBuilder 从 prompts/distill/{task_type}/ 加载模板。
"""

PROMPT_VERSION = "v2.1-2026-06-04"
