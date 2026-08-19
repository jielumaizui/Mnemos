"""Shared data/helpers for config example generation and verification."""

from typing import Any, Dict, List, Optional, Set, Tuple

from core.config import DEFAULT_CONFIG

_SECRET_LIKE_KEYS = {"api_key", "token"}


def redact_secrets(
    value: Any,
    key: str = "",
    _visited: Optional[Set[int]] = None,
) -> Any:
    """Recursively blank out secret string values in example configs."""
    visited = _visited if _visited is not None else set()
    value_id = id(value)
    if value_id in visited:
        return "<cyclic-reference>"
    if isinstance(value, dict):
        visited.add(value_id)
        try:
            return {k: redact_secrets(v, k, visited) for k, v in value.items()}
        finally:
            visited.discard(value_id)
    if isinstance(value, list):
        visited.add(value_id)
        try:
            return [redact_secrets(v, "", visited) for v in value]
        finally:
            visited.discard(value_id)
    if key in _SECRET_LIKE_KEYS and isinstance(value, str):
        return ""
    return value


EXAMPLE_CONFIG: Dict[str, Any] = redact_secrets(DEFAULT_CONFIG)

# Public environment variables that should appear in .env.example.
# Sections are used to generate a readable, grouped .env.example file.
ENV_VAR_GROUPS: List[Tuple[str, List[Tuple[str, str]]]] = [
    (
        "Base Paths",
        [
            ("MNEMOS_DIR", "Mnemos 数据根目录（默认 ~/.mnemos）"),
            ("MNEMOS_WIKI_DIR", "Obsidian / Mnemos vault 路径"),
            ("MNEMOS_DATABASE_DIR", "运行时数据库/日志目录（覆盖 system.database_dir）"),
        ],
    ),
    (
        "Performance",
        [
            ("MNEMOS_PERFORMANCE_TIER", "性能档位：eco / default / performance / dev"),
        ],
    ),
    (
        "Preflight / Oracle / Push / Cache",
        [
            ("MNEMOS_PREFLIGHT_TIMEOUT_SEC", "preflight 超时秒数"),
            ("MNEMOS_WIKI_INDEX_CACHE_TTL", "wiki 索引缓存 TTL（秒）"),
            ("MNEMOS_PUSH_INDEX_CACHE_TTL", "push 索引缓存 TTL（秒）"),
            ("MNEMOS_OBSIDIAN_CACHE_TTL", "Obsidian vault 扫描缓存 TTL（秒）"),
            ("MNEMOS_OBSIDIAN_CACHE_MAX_ENTRIES", "Obsidian 扫描缓存最大条目数"),
        ],
    ),
    (
        "Retention Days",
        [
            ("MNEMOS_RETENTION_DAYS_OBSERVATIONS", "observations 保留天数"),
            ("MNEMOS_RETENTION_DAYS_REFLECTIONS", "reflections 保留天数"),
            ("MNEMOS_RETENTION_DAYS_USER_SIGNALS", "user_signals 保留天数"),
            ("MNEMOS_RETENTION_DAYS_APPLICATION_SIGNALS", "application_signals 保留天数"),
            ("MNEMOS_RETENTION_DAYS_KNOWLEDGE_GRAPH", "knowledge_graph 保留天数"),
            ("MNEMOS_RETENTION_DAYS_WIKI_METRICS_QUERY_LOG", "wiki_metrics_query_log 保留天数"),
            ("MNEMOS_RETENTION_DAYS_MNEMOS_SEARCH_SESSIONS", "mnemos_search_sessions 保留天数"),
            ("MNEMOS_RETENTION_DAYS_LINK_PROBE_QUEUE", "link_probe_queue 保留天数"),
            ("MNEMOS_RETENTION_DAYS_MODEL_CALL_LEDGER", "model_call_ledger 保留天数"),
            ("MNEMOS_RETENTION_DAYS_DISTILLATION_CHUNKS", "distillation_chunks 保留天数"),
        ],
    ),
    (
        "Persona",
        [
            ("MNEMOS_PERSONA_AB_TEST", "画像 A/B 测试开关 true/false"),
        ],
    ),
    (
        "Legacy / Migration",
        [
            ("WIKI_DIR", "MNEMOS_WIKI_DIR 的兼容别名"),
            ("L1_STORAGE_API_URL", "遗留 L1 外部存储 API URL"),
            ("L1_STORAGE_TOKEN", "遗留 L1 外部存储 Token"),
            ("CLAUDE_SETTINGS_JSON", "Claude Code settings.json 路径"),
        ],
    ),
    (
        "Required Model Endpoints",
        [
            ("MNEMOS_LLM_API_KEY", "LLM（对话/蒸馏模型）API key"),
            ("MNEMOS_LLM_BASE_URL", "LLM 模型 API 地址"),
            ("MNEMOS_LLM_MODEL", "LLM 模型 ID"),
            ("MNEMOS_EMBEDDING_API_KEY", "Embedding（向量/语义召回模型）API key"),
            ("MNEMOS_EMBEDDING_BASE_URL", "Embedding 模型 API 地址"),
            ("MNEMOS_EMBEDDING_MODEL", "Embedding 模型 ID"),
            ("MNEMOS_RERANKER_API_KEY", "Reranker（搜索重排模型）API key"),
            ("MNEMOS_RERANKER_BASE_URL", "Reranker 模型 API 地址"),
            ("MNEMOS_RERANKER_MODEL", "Reranker 模型 ID"),
        ],
    ),
    (
        "Optional Multimodal Model",
        [
            ("MNEMOS_MULTIMODAL_API_KEY", "可选多模态/视觉模型 API key"),
            ("MNEMOS_MULTIMODAL_BASE_URL", "可选多模态/视觉模型 API 地址"),
            ("MNEMOS_MULTIMODAL_MODEL", "可选多模态/视觉模型 ID"),
        ],
    ),
    (
        "Agent Source Homes",
        [
            ("KIMI_HOME", "Kimi 会话数据目录"),
            ("HERMES_HOME", "Hermes 数据目录"),
            ("CURSOR_HOME", "Cursor 数据目录"),
            ("GEMINI_HOME", "Gemini 数据目录"),
            ("WINDSURF_HOME", "Windsurf 数据目录"),
            ("AIDER_PROJECT_ROOTS", "Aider 项目根目录（逗号分隔）"),
        ],
    ),
    (
        "Runtime / Integration",
        [
            ("MNEMOS_HOST_AGENT", "宿主 Agent 标识"),
            ("MNEMOS_HOOK_EVENT", "Hook 事件名"),
            (
                "MNEMOS_MCP_LAUNCH_CAPABILITY_REF",
                "MCP 启动能力引用（运行时注入，示例留空）",
            ),
            ("MNEMOS_STORAGE_BACKEND_PLUGINS", "存储后端插件模块列表（逗号分隔）"),
        ],
    ),
    (
        "Generic Overrides",
        [
            (
                "MNEMOS_<SECTION>__<KEY>",
                "通用配置覆盖，如 MNEMOS_DISTILL__MAX_TASKS_PER_CYCLE=10",
            ),
        ],
    ),
]


def public_env_vars() -> List[str]:
    """Return the flat list of concrete env var names to document."""
    result: List[str] = []
    for _section, vars in ENV_VAR_GROUPS:
        for name, _desc in vars:
            if "<" in name:
                # Generic placeholder, not a real env var.
                continue
            if name.startswith("MNEMOS_") or name in {
                "WIKI_DIR",
                "L1_STORAGE_API_URL",
                "L1_STORAGE_TOKEN",
                "CLAUDE_SETTINGS_JSON",
                "KIMI_HOME",
                "HERMES_HOME",
                "CURSOR_HOME",
                "GEMINI_HOME",
                "WINDSURF_HOME",
                "AIDER_PROJECT_ROOTS",
            }:
                result.append(name)
    return result
