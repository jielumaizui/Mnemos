"""
配置系统 v2 — 统一管理所有路径、开关和常量

优先级（高到低）：
1. 环境变量 (MNEMOS_* 前缀)
2. 用户配置文件 (~/.mnemos/configs/main.json)
3. 代码默认值 (DEFAULT_CONFIG)
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
import logging

logger = logging.getLogger(__name__)

# === 性能档位预设 ===
PERFORMANCE_TIERS: Dict[str, Dict[str, Any]] = {
    "low_power": {
        "embedding": {"enabled": False, "use_rerank": False},
        "capture": {"max_payload_bytes": 100000, "max_workers": 1},
        "distill": {"max_tasks_per_cycle": 1, "token_budget_total": 4000},
        "scheduler": {"worker_threads": 1},
        "daemon": {
            "services": {
                "distill_merge": False,
                "persona_analyzer": False,
                "signal_collector": False,
                "event_bus": False,
                "inbox_scanner": False,
                "capture_worker": True,  # 核心采集链路必须保留
            }
        },
    },
    "eco": {
        "embedding": {"enabled": False, "use_rerank": False},
        "capture": {"max_payload_bytes": 100000, "max_workers": 1},
        "distill": {"max_tasks_per_cycle": 1, "token_budget_total": 4000},
        "scheduler": {"worker_threads": 1},
        "daemon": {
            "services": {
                "distill_merge": False,
                "persona_analyzer": False,
                "signal_collector": False,
                "event_bus": False,
                "inbox_scanner": False,
                "capture_worker": True,  # 核心采集链路必须保留
            }
        },
    },
    "default": {
        "embedding": {"enabled": True, "use_rerank": True},
        "capture": {"max_payload_bytes": 200000, "max_workers": 4},
        "distill": {"max_tasks_per_cycle": 5, "token_budget_total": 16000},
        "scheduler": {"worker_threads": 4},
    },
    "performance": {
        "embedding": {"enabled": True, "use_rerank": True},
        "capture": {"max_payload_bytes": 500000, "max_workers": 8},
        "distill": {"max_tasks_per_cycle": 10, "token_budget_total": 32000},
        "scheduler": {"worker_threads": 8},
    },
    "dev": {
        "embedding": {"enabled": True, "use_rerank": True},
        "capture": {"max_payload_bytes": 1048576, "max_workers": 8},
        "distill": {"max_tasks_per_cycle": 20, "token_budget_total": 64000},
        "scheduler": {"worker_threads": 8},
    },
}


# === 代码默认值 ===
DEFAULT_CONFIG: Dict[str, Any] = {
    "performance_tier": "default",
    "wiki": {
        "vault_path": None,  # 自动检测
        "subdirs": [
            "00-Inbox", "01-People", "02-Projects", "03-Tech",
            "04-Concepts", "05-MOCs", "06-Retrospectives", "99-Reports",
        ],
    },
    "memos": {
        "enabled": True,
        "api_url": "",
        "token": "",
        "max_content_bytes": 7792,
        "ingest_batch_size": 10,
        "ingest_batch_interval": 10,
        "query_cache_ttl": 30,
    },
    "mnemos_dir": None,  # 默认 ~/.mnemos，可通过 MNEMOS_DIR 覆盖
    "persona": {
        "enabled": True,
        "data_sources": {
            "session": {"enabled": True},
            "git": {"enabled": False},
            "memos": {"enabled": False},
            "wiki": {"enabled": False},
            "file_system": {"enabled": False},
        },
    },
    "cross_agent_share": True,
    "integrations": {
        "claude_code": {
            "enabled": True,
            "settings_json_path": None,
        },
        "mcp": {"enabled": True},
    },
    # === 评分层常量 ===
    "scoring": {
        "retrain_buffer": 40,
        "retrain_interval_seconds": 3600,
        "ewma_alpha": 0.1,
        "min_samples_per_dimension": 12,
        "model_version_keep": 5,
        "feedback_fatigue_max_daily": 3,
        "feedback_fatigue_min_interval_minutes": 30,
        "feedback_fatigue_ignore_cooldown_hours": 2,
    },
    # === 蒸馏层常量 ===
    "distill": {
        # 默认按当前产品设计走 LLM API 蒸馏。宿主 Agent 负责调用工具和使用知识，
        # 不再默认承担“自己蒸馏自己”的后台脑力任务。
        "provider": "api",
        "allow_host_agent_delegate": False,
        "trigger_threshold": 0.4,
        "similarity_dedup_threshold": 0.85,
        "single_threshold": 0.30,
        "aggregate_threshold": 0.50,
        "deferred_max_days": 7,
        "incremental_batch_turns": 5,
        "llm_cost_budget_per_session": 10,
        "cold_knowledge_archive_days": 90,
        "token_budget_total": 16000,
        "token_budget_system_pct": 0.10,
        "token_budget_context_pct": 0.25,
        "token_budget_content_pct": 0.55,
        "token_budget_output_reserve": 2000,
        # 真实环境保护：避免积压时一次性跑满 CPU/磁盘/宿主 Agent。
        "max_tasks_per_cycle": 5,
        "max_collect_per_cycle": 20,
        "min_task_interval_seconds": 1.0,
        "poll_interval_seconds": 60,
        "tick_interval_seconds": 300,
    },
    # === 知识图谱常量 ===
    "knowledge_graph": {
        "entity_quality_threshold": 0.3,
        "relation_confidence_strong": 0.7,
        "relation_confidence_weak": 0.4,
        "freshness_decay_half_life_days": 30,
        "freshness_deprecated_threshold": 0.2,
        "vector_dim": 1024,
        "vector_index_init_capacity": 100000,
    },
    # === 画像常量 ===
    "persona_engine": {
        "interest_decay_half_life_days": 30,
        "blind_spot_min_queries": 2,
        "preference_likelihood": {
            "search": 0.6,
            "save": 0.8,
            "share": 0.9,
            "ignore": 0.3,
        },
    },
    # === Capture 队列层常量 ===
    "capture": {
        "max_queue_depth": 10000,
        "per_source_max_queue_depth": 1000,
        "max_workers": 4,
        "per_source_concurrency": 1,
        "max_batch_per_tick": 50,
        "tick_interval_seconds": 5,
        "max_payload_bytes": 200000,
        "duplicate_ttl_days": 30,
        # reasoning/thinking 采集策略：off|summary|artifact_summary|full
        "reasoning_mode": "artifact_summary",
    },
    # === daemon 服务开关 ===
    "daemon": {
        "services": {
            "capture_worker": True,
            # 默认启用受限增量扫描；性能由 sync.l1_scan_* 预算控制。
            "l1_sync": True,
            "distill_merge": True,
            "heartbeat": True,
            "inbox_scanner": True,
            "signal_collector": True,
            "persona_analyzer": True,
            "event_bus": True,
        },
    },
    # === 同步层常量 ===
    "sync": {
        "interval_seconds": 10,
        "noise_threshold": 0.7,
        "debounce_stable_reads": 3,
        "polling_interval_openclaw": 3600,
        # L1 实时增量扫描限制（只同步尾部新增）
        "l1_scan_max_turns_per_session": 50,
        "l1_scan_max_sessions_per_source": 20,
        "l1_scan_max_sources_per_cycle": 3,
        "l1_scan_poll_interval_seconds": 60,
        "l1_scan_max_file_bytes": 2097152,
        "l1_scan_recent_hours": 24.0,
        # Backfill 历史回填配置（补齐中间缺洞）
        "backfill_max_turns_per_session": 0,  # 0=无限制
        "backfill_fill_gaps": True,
        "backfill_skip_stale_hours": 168,  # 7天
        "l1_scan_poll_interval_seconds": 60,
        "l1_scan_max_sources_per_cycle": 3,
        "l1_scan_max_sessions_per_source": 20,
        "l1_scan_max_turns_per_session": 50,
        "l1_scan_max_file_bytes": 2 * 1024 * 1024,
        "l1_scan_recent_hours": 24,
    },
    # === 应用层常量 ===
    "app": {
        "search_max_results": 10,
        "push_max_items": 3,
        "push_cooldown_minutes": 10,
        "push_penalty_multipliers": [1.5, 2.0, 6.0],
        "dispute_escalation_intensity": 0.7,
        "dispute_stale_days": 7,
        "freshness_alert_days": 90,
    },
    # === 调度器常量 ===
    "scheduler": {
        "beat_seconds": 300,
        "worker_threads": 4,
    },
    # === 事件总线常量 ===
    "event_bus": {
        "max_latency_ms": 10,
        "queue_depth_alert": 1000,
        "max_queue_depth": 10000,
        "poll_interval_seconds": 10,
        "max_recover_events": 1000,
        "dead_letter_alert": 10,
        "max_retries": 5,
    },
    # === 运维常量 ===
    "ops": {
        "daemon_log_max_bytes": 10 * 1024 * 1024,
        "heartbeat_interval_seconds": 300,
        "inbox_scan_interval_seconds": 600,
        "persona_analysis_interval_seconds": 86400,
        "health_check_interval": 60,
        "default_timeout": 30,
        "save_interval_sec": 300,
        "backup_retention_days": 30,
    },
    # === 系统运维 ===
    "system": {
        "retention_days": 365,
        "archive_days": 90,
        "trigger_buffer_size": 100,
        "feedback_min_interval": 300,
        "index_rebuild_interval": 86400,
    },
    # === Skill 飞轮 ===
    "skill": {
        "time_window_days": 30,
        "wiki_jaccard_threshold": 0.6,
        "min_usage_count": 3,
        "min_age_days": 7,
        "cleanup_days": 90,
        "grace_period_days": 14,
    },
    # === Embedding / 语义搜索 ===
    "embedding": {
        "enabled": False,              # 默认关闭，用户手动开启
        "provider": "siliconflow",     # 当前仅支持 siliconflow
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",                 # 用户填写
        "embedding_model": "BAAI/bge-m3",
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "use_rerank": True,            # 是否启用重排（个人版默认开启）
        "ttl_days": 7,
        "similarity_threshold": 0.72,  # 语义相似度阈值（bge-m3 建议 0.72）
        "index_interval_hours": 24,    # 自动重建索引间隔
    },
    # === LLM API / 蒸馏模型 ===
    "llm": {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "model": "deepseek-ai/DeepSeek-V3",
        "providers": {
            "siliconflow": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "",
                "model": "deepseek-ai/DeepSeek-V3",
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o-mini",
            },
        },
    },
    # === 增量批处理 ===
    "incremental": {
        "batch_interval": 300,
    },
}


class Config:
    """配置管理器 v2 — JSON + 环境变量"""

    def __init__(self, config_path: Optional[Path] = None):
        self._mnemos_dir = self._resolve_mnemos_dir()
        self._use_default_config_path = config_path is None
        self.config_path = config_path or self._mnemos_dir / "configs" / "main.json"
        self._migrated_from_legacy = False
        self._data = self._load()
        if self._migrated_from_legacy and not self.config_path.exists():
            self.save()
            logger.info(f"已自动迁移旧配置到 {self.config_path}")

    def _resolve_mnemos_dir(self) -> Path:
        """确定 Mnemos 数据目录"""
        env = os.getenv("MNEMOS_DIR")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".mnemos"

    def _load(self) -> Dict:
        """加载配置：代码默认值 < 档位预设 < JSON 文件 < 环境变量

        加载顺序修正：
        1. 代码默认值
        2. 从 JSON 文件读取 performance_tier
        3. 应用该 tier 的档位预设
        4. 合并 JSON 文件的其他配置（用户显式覆盖项）
        5. 环境变量覆盖
        """
        import copy
        data = copy.deepcopy(DEFAULT_CONFIG)

        # 先读取 JSON 文件（不合并），只为拿到用户设置的 performance_tier
        file_data = {}
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
            except Exception as e:
                logger.warning(f"配置文件加载失败: {e}")
        elif self._use_default_config_path:
            legacy_data = self._load_legacy_config()
            if legacy_data:
                file_data = legacy_data
                self._migrated_from_legacy = True

        # 应用性能档位预设（优先使用 JSON 中的 tier，其次默认值）
        tier = file_data.get("performance_tier", data.get("performance_tier", "default"))
        if tier in PERFORMANCE_TIERS:
            self._deep_merge(data, PERFORMANCE_TIERS[tier])
            data["performance_tier"] = tier  # 确保 tier 本身被保留

        # 合并 JSON 文件的其他配置（用户显式覆盖项优先于档位预设）
        if file_data:
            self._deep_merge(data, file_data)

        # 环境变量覆盖 (MNEMOS_* 前缀)
        self._apply_env_overrides(data)

        # 解析自动检测值
        self._resolve_auto_values(data)

        return data

    def _deep_merge(self, base: Dict, override: Dict):
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val)
            else:
                base[key] = val

    def _apply_env_overrides(self, data: Dict):
        """环境变量覆盖：MNEMOS_* 前缀 + 兼容旧变量名"""
        env_map = {
            "MEMOS_TOKEN": ("memos", "token"),
            "MEMOS_API_URL": ("memos", "api_url"),
            "MNEMOS_WIKI_DIR": ("wiki", "vault_path"),
            "WIKI_DIR": ("wiki", "vault_path"),  # 兼容旧名
            "MNEMOS_DIR": None,  # 顶层，已在 _resolve_mnemos_dir 处理
            "CLAUDE_SETTINGS_JSON": ("integrations", "claude_code", "settings_json_path"),
        }
        for env_var, path in env_map.items():
            val = os.getenv(env_var)
            if val is None:
                continue
            if path is None:
                continue
            d = data
            for k in path[:-1]:
                d = d.setdefault(k, {})
            d[path[-1]] = val

        # 通用 MNEMOS_ 前缀覆盖：MNEMOS_SCORING__RETRAIN_BUFFER → scoring.retrain_buffer
        for key, val in os.environ.items():
            if key.startswith("MNEMOS_") and key not in env_map and key != "MNEMOS_DIR":
                # MNEMOS_SCORING__RETRAIN_BUFFER → scoring.retrain_buffer
                parts = key[7:].lower().split("__")
                if len(parts) >= 2:
                    d = data
                    for p in parts[:-1]:
                        d = d.setdefault(p, {})
                    # 尝试类型转换
                    d[parts[-1]] = self._auto_type(val)

    @staticmethod
    def _auto_type(val: str) -> Any:
        """尝试将字符串转为合适的类型"""
        if val.lower() in ("true", "yes"):
            return True
        if val.lower() in ("false", "no"):
            return False
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val

    def _resolve_auto_values(self, data: Dict):
        if data["wiki"]["vault_path"] is None:
            data["wiki"]["vault_path"] = str(self._default_wiki_path())
        cc_path = data["integrations"]["claude_code"]["settings_json_path"]
        if cc_path is None:
            data["integrations"]["claude_code"]["settings_json_path"] = str(
                self._default_claude_settings_path()
            )

    def _default_wiki_path(self) -> Path:
        if sys.platform == "darwin":
            return Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        elif sys.platform == "win32":
            return Path.home() / "Documents" / "Obsidian Vault" / "wiki"
        else:
            return Path.home() / "wiki"

    def _default_claude_settings_path(self) -> Path:
        # 新版 Claude Code (>= 0.40) 默认使用 ~/.claude/settings.json
        # 旧版使用平台特定路径。优先检测新版路径。
        candidates = [
            Path.home() / ".claude" / "settings.json",
        ]
        if sys.platform == "win32":
            candidates.append(Path.home() / "AppData" / "Roaming" / "Claude" / "settings.json")
        elif sys.platform == "darwin":
            candidates.append(Path.home() / "Library" / "Application Support" / "Claude" / "settings.json")
        else:
            candidates.append(Path.home() / ".config" / "claude" / "settings.json")
        for p in candidates:
            if p.exists():
                return p
        return candidates[0]  # 默认返回新版路径

    def _load_legacy_config(self) -> Dict:
        """读取旧 YAML 配置。旧文件只作为迁移来源，运行时权威文件始终是 main.json。"""
        for legacy_path in self._legacy_config_paths():
            if not legacy_path.exists():
                continue
            try:
                import yaml
                with open(legacy_path, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    logger.info(f"检测到旧配置文件 {legacy_path}，将迁移到 {self.config_path}")
                    return loaded
            except Exception as e:
                logger.warning(f"旧配置读取失败 {legacy_path}: {e}")
        return {}

    def _legacy_config_paths(self) -> list[Path]:
        paths = [self._mnemos_dir / "config.yaml"]
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home() / ".config"
        paths.append(base / "mnemos" / "config.yaml")
        # 去重但保持顺序
        unique = []
        seen = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                unique.append(path)
                seen.add(key)
        return unique

    def save(self):
        """保存配置到 JSON 文件"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        import copy
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(self._data), f, indent=2, ensure_ascii=False)

    # ---- 核心访问方法 ----

    @property
    def mnemos_dir(self) -> Path:
        return self._mnemos_dir

    @property
    def data_dir(self) -> Path:
        return self._mnemos_dir

    @property
    def wiki_dir(self) -> Path:
        return Path(self._data["wiki"]["vault_path"]).expanduser()

    @property
    def memos_enabled(self) -> bool:
        return self._data["memos"]["enabled"]

    @property
    def memos_token(self) -> str:
        return self._data["memos"]["token"]

    @property
    def memos_api_url(self) -> str:
        return self._data["memos"]["api_url"]

    @property
    def persona_enabled(self) -> bool:
        return self._data["persona"]["enabled"]

    @property
    def persona_data_sources(self) -> Dict:
        return self._data["persona"]["data_sources"]

    def is_source_enabled(self, source: str) -> bool:
        return self._data["persona"]["data_sources"].get(source, {}).get("enabled", False)

    @property
    def claude_code_enabled(self) -> bool:
        return self._data["integrations"]["claude_code"]["enabled"]

    @property
    def claude_settings_path(self) -> Path:
        return Path(self._data["integrations"]["claude_code"]["settings_json_path"]).expanduser()

    @property
    def mcp_enabled(self) -> bool:
        return self._data["integrations"]["mcp"]["enabled"]

    @property
    def cross_agent_share(self) -> bool:
        return self._data.get("cross_agent_share", True)

    @property
    def claude_data_dir(self) -> Path:
        # 新版 Claude Code 默认 ~/.claude，优先检测
        modern = Path.home() / ".claude"
        if modern.exists():
            return modern
        if sys.platform == "win32":
            p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Claude"
        else:
            p = Path.home() / ".config" / "claude"
        return p if p.exists() else modern

    def get(self, key: str, default=None) -> Any:
        """按点号路径获取配置值：config.get('scoring.retrain_buffer')"""
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    def set(self, key: str, value: Any):
        """按点号路径设置配置值"""
        keys = key.split(".")
        data = self._data
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = value

    def to_dict(self) -> Dict:
        import copy
        return copy.deepcopy(self._data)

    def load_agent_config(self, agent_name: str) -> Dict:
        """加载指定 Agent 的配置"""
        agents_path = self._mnemos_dir / "configs" / "agents.json"
        if agents_path.exists():
            try:
                with open(agents_path, "r", encoding="utf-8") as f:
                    agents = json.load(f)
                return agents.get(agent_name, {})
            except Exception:
                logger.warning(f"Unexpected error in config.py", exc_info=True)
                pass
        return {}


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reload_config():
    """重新加载配置"""
    global _config
    _config = Config()
