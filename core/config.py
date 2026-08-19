"""
配置系统 v2 — 统一管理所有路径、开关和常量

优先级（高到低）：
1. 环境变量 (MNEMOS_* 前缀)
2. 用户配置文件 (~/.mnemos/configs/main.json)
3. 代码默认值 (DEFAULT_CONFIG)
"""

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set, cast
import logging

from core.config_provider import ConfigProvider as _ConfigProvider
from core.config_registry import (
    CONFIG_REGISTRY,
    ConfigValidationIssue,
    ConfigValidationError,
)
from core.config_persistence import (
    default_claude_settings_path,
    load_historical_config,
    write_config_file,
)
from core.config_value_normalization import discard_non_restorable_capture_values
from core.ops.config_scope import current_config
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.runtime_environment import auto_type_environment_value, environment_get
from core.runtime_environment import environment_items, run_owned_override_is_stale
from core.ops.durable_io import read_native_bytes
from core.utils import secure_directory

# Constants extracted from magic numbers
PARTS = 7
logger = logging.getLogger(__name__)
ConfigProvider = _ConfigProvider

# === 性能档位预设 ===
PERFORMANCE_TIERS: Dict[str, Dict[str, Any]] = {
    "low_power": {
        "embedding": {"enabled": False, "use_rerank": False},
        "capture": {"max_payload_bytes": 100000, "max_workers": 1},
        "distill": {"max_tasks_per_cycle": 1, "token_budget_total": 4000},
        "scheduler": {"worker_threads": 1},
        "daemon": {
            "services": {
                "distill_and_merge": False,
                "persona_analyzer": False,
                "signal_collector": False,
                "eventbus": False,
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
                "distill_and_merge": False,
                "persona_analyzer": False,
                "signal_collector": False,
                "eventbus": False,
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
    "claude_data_dir": None,
    "scheduler": {"worker_threads": 4},
    "wiki": {
        "vault_path": None,  # 自动检测
    },
    "agent_kit": {"runtime_receipt_max_age_seconds": 86400},
    "storage": {
        "backend": "obsidian",
        "obsidian": {
            "vault_path": None,  # 默认从 vaults.raw 解析（兼容别名）
            "daily_size_threshold": 819200,  # 800KB
            "scan_cache_ttl_seconds": 60,
            "scan_cache_max_entries": 1024,
        },
        "retention_days": {
            "observations": 180,
            "reflections": 180,
            "user_signals": 90,
            "application_signals": 90,
            "knowledge_graph": 365,
            "wiki_metrics_query_log": 90,
            "mnemos_search_sessions": 90,
            "link_probe_queue": 90,
            "model_call_ledger": 90,
            "distillation_chunks": 30,
        },
        "maintenance": {
            "interval_hours": 24,
            "vacuum_day_of_week": 0,  # 0=周日
            "vacuum_size_threshold_mb": 100,
        },
        "disk_budget": {
            "sqlite_wal_file_max_mb": 512,
            "sqlite_wal_total_max_mb": 1024,
            "temp_total_max_mb": 2048,
            "temp_stale_minutes": 60,
            "snapshot_total_max_mb": 20480,
            "snapshot_growth_max_mb_per_day": 8192,
            # Legacy full-copy Raw projection backups are manual-retention
            # evidence. New projections write only a small change manifest.
            "raw_projection_backup_total_max_mb": 1024,
            "raw_events_max_mb": 4096,
            "raw_events_growth_max_mb_per_day": 2048,
            "growth_sample_min_seconds": 300,
        },
    },
    # === 遗留外部 L1/Memos 存储兼容（已弃用，新部署不使用）===
    "l1_storage": {
        "enabled": False,
        "api_url": "",
        "token": "",
    },
    # === Vault 布局（v2）：raw 独立 + mnemos 主认知 Vault ===
    "vaults": {
        "raw": {"path": None, "enabled": True, "auto_register": True},
        "mnemos": {"path": None, "enabled": True, "auto_register": True},
    },
    # === 跨层认知图数据库（Agent/KIA 跨层推理）===
    # NOTE: reconcile_interval_seconds / entity_link_threshold 当前由内部常量驱动。
    "cognitive_graph": {
        "enabled": True,
        "db_path": None,  # 默认 ~/.mnemos/cognitive_graph.db
    },
    # === 证据回链修复（P0.5 EvidenceBackfill）===
    "evidence_backfill": {
        "default_limit": 0,  # 0=不限制 changed pages；CLI --limit 可临时覆盖
        "max_refs_per_page": 20,
        "frontmatter_ref_limit": 10,
        "unresolved_sample_limit": 50,
        "change_sample_limit": 100,
        "include_relation_evidence": True,
        "relation_evidence_types": ["anti_pattern_quote", "distill_extraction"],
        "write_frontmatter": True,
        "write_report": True,
        "report_dir": "99-Reports/认知数据就绪度",
    },
    # === 可信推送写入闭环（P0）===
    "trusted_push": {
        "mode": "off",  # off | shadow | enforce
        "db_path": None,  # 默认 database_dir/trusted_push.db
        "evidence_ttl_days": 14,
        "rejected_evidence_ttl_hours": 24,
        "high_entropy_min_length": 80,
        "high_entropy_threshold": 4.2,
    },
    "mnemos_dir": None,  # 默认 ~/.mnemos，可通过 MNEMOS_DIR 覆盖
    "persona": {
        "enabled": True,
        "strategy_injection_enabled": True,
        "strategy_token_limit": 300,
        "skill_report_only": True,
        "ab_test_enabled": False,
        "data_sources": {
            "session": {"enabled": True},
            "git": {"enabled": False},
            "wiki": {"enabled": False},
            "file_system": {"enabled": False},
        },
    },
    "cross_agent_share": False,
    "security": {
        # True 表示部署方明确接受 env: secret references 作为 keyring 不可用时的降级。
        "accept_env_secret_fallback": False,
    },
    "integrations": {
        "claude_code": {
            "enabled": True,
            "settings_json_path": None,
        },
        "mcp": {"enabled": True},
        "openclaw": {
            "state_dir": None,  # 默认 ~/.openclaw
        },
        "crush": {"home": None},
        "codex": {"codex_home": None, "xdg_config_home": None},
    },
    # === 评分层常量 ===
    # NOTE: model_version_keep / feedback_fatigue_* 当前未接入，保留核心评分常量。
    "scoring": {
        "ewma_alpha": 0.1,
        "min_samples_per_dimension": 12,
        "domain_scorers_enabled": True,
        "clustering": {"enabled": False},
        "training_scheduler": {"enabled": False},
    },
    # === 蒸馏层常量 ===
    "distill": {
        "tick_interval_seconds": 300,
        "auto": True,
        "cognitive_action_worker_limit": 100,
        "cognitive_action_worker_interval_seconds": 60,
        "operational_incident_worker_interval_seconds": 60,
        # Health budget for durable cognitive actions awaiting their worker.
        # Keep this separate from the per-cycle worker throughput limit.
        "cognitive_actions": {"queued_budget": 0},
        # 默认按当前产品设计走 LLM API 蒸馏。宿主 Agent 负责调用工具和使用知识，
        # 不再默认承担“自己蒸馏自己”的后台脑力任务。
        "trigger_threshold": 0.4,
        "incremental_batch_turns": 5,
        "llm_cost_budget_per_session": 10,
        "cold_knowledge_archive_days": 90,
        # Token 预算：total = system + context + content + output_reserve
        "token_budget_total": 16000,
        "token_budget_system_pct": 0.10,
        "token_budget_context_pct": 0.25,
        "token_budget_content_pct": 0.55,
        "token_budget_output_reserve": 2000,
        "response_tokens": 6000,
        "dynamic_response_tokens_enabled": True,
        "response_tokens_default": 6000,
        "response_tokens_medium": 8000,
        "response_tokens_long": 12000,
        "response_tokens_retry_max": 16000,
        "response_tokens_short_input_threshold": 6000,
        "response_tokens_medium_input_threshold": 16000,
        "response_tokens_merge_fragment_threshold": 2,
        "effective_max_tokens": 24000,
        "per_message_token_limit": 6000,
        # 分块阈值因子（基于 token_budget_total）
        "chunk_std_factor": 3,
        "chunk_total_factor": 25,
        "chunk_size_factor": 1.5,
        # 片段质量阈值
        "fragment_boundary_chars": 8000,
        "min_value_context_chars": 30,
        "value_prejudgment_rule_assessment_length": 3000,
        "content_formatter_max_tokens": 8000,
        # 真实环境保护：避免积压时一次性跑满 CPU/磁盘/宿主 Agent。
        "max_tasks_per_cycle": 5,
        "min_task_interval_seconds": 1.0,
        "poll_interval_seconds": 60,
        # 单个蒸馏任务总超时（秒），防止 LLM 调用卡住导致 processing 永久占用。
        "task_timeout_seconds": 300,
        "task_timeout_medium_seconds": 900,
        "task_timeout_long_seconds": 1800,
        "task_timeout_chunked_seconds": 3600,
        "chunk_checkpoint_enabled": True,
        "chunk_checkpoint_db_path": None,
        # 片段合并与质量校验
        "fragment_merge_threshold": 0.4,
        "enable_llm_fragment_merge": True,
        "extract_correction_retries": 1,
        "min_session_fragment_pass_ratio": 0.5,
        "auto_expression_formatting": True,
        # 正式写入 mnemos vault 前必须满足 distill_output_v4 契约。
        "structured_output_contract": {"enforce": True},
        # P1: distill_output_v4 action router。默认开启，但只在结构化输出存在时接管。
        "action_router": {
            "enabled": True,
            "db_path": None,
            "backup_dir": "distill_action_backups",
            "shadow_dir": "07-Shadow/distill-actions",
            "min_merge_confidence": 0.72,
            "max_direct_conflict_strength": 0.35,
        },
    },
    # === 知识图谱常量 ===
    # NOTE: freshness_decay_half_life_days 在 hephaestus 新鲜度计算中使用；
    # 其余阈值/向量参数当前由对应模块内部常量驱动，未接入 Config。
    "knowledge_graph": {
        "freshness_decay_half_life_days": 30,
        "projection_enabled": True,
        "projection_max_relations": 200,
        "projection_max_relations_per_entity": 5,
        "implicit_relation_discovery_enabled": True,
        "implicit_relation_max_entities_per_event": 5,
    },
    # === 争议扫描常量 ===
    # DisputeResolver / DisputeScorer 共同读取这些值，用户可通过
    # ~/.mnemos/configs/main.json 或 MNEMOS_DISPUTE_SCAN__* 环境变量覆盖。
    "dispute_scan": {
        "enabled": True,
        "interval_seconds": 3600,
        "max_daily_disputes": 10,
        "max_pages_per_scan": 500,
        "min_conflict_strength": 0.5,
        "auto_resolve_min_gap": 0.30,
        "merge_min_gap": 0.15,
        "freshness_half_life_days": 30,
        "citation_max_reference": 20,
        "weights": {
            "confidence": 0.25,
            "freshness": 0.25,
            "citation": 0.20,
            "quality": 0.15,
            "source": 0.10,
            "core": 0.05,
        },
        "adaptive_learning": {
            "enabled": False,
            "learning_rate": 0.05,
            "min_samples_before_update": 5,
            "max_weight": 0.60,
            "min_weight": 0.05,
        },
    },
    # === 应用层信号检测：默认只供诊断/显式工具调用，不主动弹窗 ===
    "application_signals": {
        "enabled": True,
        "auto_notify": False,
        "avoidance": {"enabled": True, "cooldown_days": 14},
        "cross_agent_divergence": {"enabled": True, "cooldown_days": 7},
        "freshness": {"enabled": True, "cooldown_days": 30},
    },
    "quality_gate": {
        "enabled": True,
        "base_threshold": 0.55,
        "review_margin": 0.15,
        "cognitive_value": {
            "enabled": True,
            "base_threshold": 0.55,
            "review_margin": 0.15,
        },
    },
    # === Wiki 路由质量预算（health_check + doctor 引用）===
    "health": {
        "wiki_route_budgets": {
            "inbox_ready_to_classify": 100,
            "needs_review_pages": 500,
            "formal_source_prefixed_pages": 250,
            "title_basename_collision_groups": 350,
        },
    },
    # Durable provider-boundary accounting.  This cap is independent of the
    # per-distillation-run cap and is intentionally not an opt-out switch.
    "model_call_ledger": {
        "daily_cost_cap": 50.0,
        # Free providers must declare their zero price explicitly; a missing
        # price can never silently remove the pre-dispatch budget guard.
        "allow_explicit_zero_price": False,
    },
    # === Capture 队列层常量 ===
    "capture": {
        # Only short-lived queue payloads use a retention window.  Canonical
        # Raw revision idempotency remains permanent in capture receipts.
        "payload_retention_days": 30,
        "artifact_ttl_days": 30,
        "artifact_max_total_bytes": 1073741824,
        "max_queue_depth": 10000,
        "per_source_max_queue_depth": 1000,
        "max_workers": 4,
        "per_source_concurrency": 1,
        "max_batch_per_tick": 50,
        "tick_interval_seconds": 5,
        "max_payload_bytes": 200000,
        # reasoning/thinking 采集策略：off|summary|artifact_summary|full
        "reasoning_mode": "artifact_summary",
    },
    # === 文件/路径监听开关：默认关闭，daemon 可按需接入 ===
    "watchers": {
        "enabled": False,
        "agent_paths": {"enabled": False, "poll_interval_seconds": 300},
        "debounce_seconds": 5,
    },
    # === daemon 服务开关 ===
    "daemon": {
        "heartbeat_stale_seconds": 180,
        "max_workers": 4,
        "services": {
            "capture_worker": True,
            "raw_projection": True,
            # 唯一连续 owner：按 manifest 轮询本地 raw/agent sources。
            # watcher/trigger 只是加速器，不能作为采集的唯一闸门。
            "raw_sync": True,
            "retry_failed": True,
            "distill_and_merge": True,
            "distill_cognitive_actions": True,
            "operational_incidents": True,
            "wiki_route": True,
            "heartbeat": True,
            "inbox_scanner": True,
            "signal_collector": True,
            "persona_analyzer": True,
            "persona_challenge": True,
            "eventbus": True,
            "observation_engine": True,
            "reflection_engine": True,
            "feedback_prompt": True,
            "recap_consumption": True,
            "dispute_scan": True,
            "reminder_scan": True,
            "freshness_refresh": True,
            "entropy_scan": True,
            "link_probe": False,  # 受 features.enable_link_probe 共同控制；默认关闭避免空转
            "db_maintenance": True,
            # 与 INTERVALS 一一对应的服务开关
            "startup_compensation": True,
            "drift_report": True,
            "preflight_checks": True,
            "scheduler_tick": True,
            "adaptive_config": True,
            "search_ignore_detection": True,
            "user_correction_detection": True,
            "cognitive_graph_reconcile": True,
            "prediction_maturity": True,
            "training_governance": True,
            "cognitive_consolidation": True,
            "trigger_dispatcher": True,
            "file_ingestor": True,
            "agent_path_watch": False,
            "wiki_auto_commit": True,
        },
    },
    # === Obsidian Wiki routing ===
    "wiki_route": {
        "interval_seconds": 3600,
    },
    # === COG-048 governed training ===
    "training_governance": {
        "interval_seconds": 3600,
        "reconcile_batch_limit": 100,
    },
    # === canonical raw_events.db -> Obsidian raw vault 投影 ===
    "raw_projection": {
        "enabled": True,
        "interval_seconds": 300,
        "max_files": 0,
        "chunk_turns": 5,
        "max_turn_chars": 0,
        "include_eligible_delete": False,
        # 单个投影 .md 文件的字节上限；超过时拆成 <base>.part-NNN.md 多个分片，
        # <base>.md 变为索引页。0 表示不限制。防止超大文件卡死 Obsidian 索引。
        "max_file_bytes": 2097152,
    },
    # === raw_events.db 生命周期 ===
    "raw_event_store": {
        "db_path": None,
        "enabled": True,
        "recalc_days": 7,
        "retention_days": 30,
        "survival_prune_threshold": 35.0,
        "freshness_half_life_days": 30,
        "physical_delete_enabled": True,
        "physical_delete_batch_limit": 10000,
        "startup_delay_seconds": 600,
    },
    # === 认知压缩/遗忘计划 ===
    "cognitive_consolidation": {
        "db_path": None,
        "raw_vault_dir": None,
        "method_pages_dir": "04-Concepts/方法论",
        "candidate_limit": 50,
        "raw_purge_limit": 10,
        "min_key_details": 1,
        "max_key_details": 2,
        "interval_seconds": 86400,
    },
    # === 认知就绪度的效果证据时效窗口 ===
    "cognitive_readiness": {
        "freshness_window_seconds": 2592000,
    },
    # === 认知信任评分与投递闸门 ===
    "trust": {
        "db_path": None,
        "base_trust_score": 0.6,
        "evidence_ref_bonus": 0.08,
        "min_merge_score": 0.72,
        "min_delivery_score": 0.55,
        "min_delivery_task_fit": 0.45,
        "min_guard_score": 0.75,
        "min_guard_task_fit": 0.7,
        "ignore_penalty": 0.12,
        "dismiss_penalty": 0.22,
        "no_click_penalty": 0.08,
        "contradicted_penalty": 0.35,
        "harmful_penalty": 1.0,
        "harmful_cooldown_days": 30,
    },
    # === 执行中守护：分析循环/重复读取阈值 ===
    "guard": {
        "analysis_loop": {
            "enabled": True,
            "max_analysis_turns_without_action": 2,
            "max_repeated_reads_per_target": 2,
        }
    },
    # === 策略补丁：把高价值经验转成受控 preflight/guard 注入项 ===
    "policy_patch": {
        "enabled": True,
        "db_path": None,
        "ttl_days": 30,
        "min_confidence": 0.7,
        "max_active": 5,
    },
    # === 行动前预测账本 ===
    "prediction": {
        "predictive_delivery_window_hours": 168,
        "maturity_interval_seconds": 3600,
        "maturity_batch_limit": 100,
    },
    # === 知识投递策略与预算 ===
    "delivery": {
        "db_path": None,
        "preference": "balanced",
        "profiles": {
            "quiet": {
                "daily_total": 4,
                "per_task_total": 1,
                "per_task_hint": 1,
                "per_task_warn": 0,
                "force_open_daily": 0,
                "same_topic_cooldown_hours": 48,
                "dismiss_cooldown_days": 30,
                "overflow_defer_hours": 1,
            },
            "balanced": {
                "daily_total": 12,
                "per_task_total": 3,
                "per_task_hint": 2,
                "per_task_warn": 1,
                "force_open_daily": 0,
                "same_topic_cooldown_hours": 24,
                "dismiss_cooldown_days": 14,
                "overflow_defer_hours": 1,
            },
            "active": {
                "daily_total": 24,
                "per_task_total": 5,
                "per_task_hint": 3,
                "per_task_warn": 2,
                "force_open_daily": 1,
                "same_topic_cooldown_hours": 12,
                "dismiss_cooldown_days": 7,
                "overflow_defer_hours": 1,
            },
        },
    },
    # === 受控求证队列 ===
    "verification_queue": {
        "enabled": True,
        "db_path": None,
        "report_path": None,
        "blindspots_db_path": None,
        "max_candidates": 50,
        "max_disputes": 10,
        "max_blindspots": 10,
        "max_freshness_alerts": 10,
        "cron": "20 16 * * *",
        "respect_resource_budget": True,
    },
    # === 新鲜度自动刷新 ===
    "freshness_refresh": {
        "redistill_enabled": False,
        "auto_refresh_limit": 3,
        "archive_limit": 10,
        "interval_seconds": 86400,
        "auto_refresh_on_stale": True,  # knowledge_stale 事件是否自动触发刷新
    },
    # === 熵减扫描 ===
    "entropy": {
        "scan_sample_size": 200,
        "scan_interval_seconds": 86400,
    },
    # === Obsidian 可见投影限流 ===
    "reflection_export": {
        "max_records_per_run": 200,
        "max_records_per_day": 20,
    },
    "shadow_projection": {
        "max_pages_per_batch": 50,
    },
    # === 链接探测 ===
    "link_probe": {
        "interval_seconds": 3600,
    },
    # === 文件摄入 ===
    "file_ingestor": {
        "watch_dir": "",  # 空字符串表示使用默认 DATA_DIR/file_ingest
    },
    # === 同步层常量 ===
    # Batches bound latency only.  Durable canonical Raw cursors and source
    # reconciliation ensure that limits never define completion.
    "sync": {
        "raw_sync_sessions_per_source": 10,
        "raw_sync_turns_per_session": 100,
        # Backfill 历史回填配置（补齐中间缺洞）
        "backfill_max_turns_per_session": 0,  # 0=无限制
        "retry_failed_limit": 50,
    },
    # === 统一提醒引擎（ReminderEngine）常量 ===
    "reminder": {
        "enabled": True,
        "contextual_cooldown_seconds": 600,
        "freshness_cooldown_seconds": 86400,
        "scan_interval_seconds": 86400,
        "max_contextual_per_turn": 3,
    },
    # === 应用层常量 ===
    # NOTE: AdaptiveConfig 的内置覆盖矩阵见 core/kia/adaptive_policy_matrix.py；
    # app.push_max_items 仍保留为旧投递预算兜底键。
    "app": {
        "push_max_items": 3,
    },
    "feedback_signal": {"db_path": None},
    "raw": {"index": {"retention_days": 180}},
    # === 自适应配置规则 ===
    "adaptive_config": {
        "rules": [],
    },
    # === Committed CognitionEpisode dispatch ===
    "cognition_episode": {
        "dispatch_startup_limit": 100,
    },
    # === 事件总线常量 ===
    "event_bus": {
        "max_workers": 1,
        "max_latency_ms": 10,
        "queue_depth_alert": 1000,
        "max_queue_depth": 10000,
        "max_recover_events": 1000,
        "max_chain_depth": 10,
        "dead_letter_alert": 10,
        "dead_letter_max": 1000,
        "dead_letter_replay_max_age_hours": 168,
        "dead_letter_replay_per_type_limit": 100,
        "startup_replay_limit": 500,
        "max_retries": 5,
        "retry_base_seconds": 1.0,
        "retry_max_seconds": 60.0,
        "dispatch_workers": 1,
        "handler_timeout_seconds": 0,
        "lease_seconds": 300.0,
    },
    # === 运维常量 ===
    "ops": {
        "daemon_log_max_bytes": 10 * 1024 * 1024,
    },
    # === 自动愈合编排 ===
    "auto_heal": {
        "enabled": True,
        "user_intervention_budget": 10,
        "default_verification_command": "python3 mnemos_cli.py health --json",
        "record_action_ledger": True,
    },
    # === 系统运维 ===
    "system": {
        "database_dir": None,  # 运行时数据目录（.db + 日志 + 事件 + 蒸馏产物），null 表示使用 data_dir
    },
    # === Reflection / Observation / Feedback 运行时接入 ===
    "observation": {
        "enabled": True,
        "interval_seconds": 3600,
        "inject_on_session_start": True,
        # L3-Observations 单页字节上限；超过时拆成 <dim>.part-NNN.md 分片，
        # <dim>.md 变为索引页。0 表示不限制。防止超大文件卡死 Obsidian 索引。
        "projection_max_file_bytes": 2097152,
    },
    "reflection": {
        "enabled": True,
        "interval_seconds": 86400,
        "auto_trigger_on_session_end": True,
        "manual_query": "分析最近认知与决策模式",
        "register_default_consumers": True,
        # L3 Observation 驱动 L4 Reflection：高置信度/突变观察自动触发反思
        "observation_trigger_enabled": True,
        "observation_trigger_confidence": 0.7,
    },
    "feedback": {
        "enabled": True,
        "prompt_interval_seconds": 86400,
        "recap_consumption_interval_seconds": 60,
        "pending_hours": 24,
        "pending_limit": 10,
    },
    # === 认知决策飞轮（legacy config key: skill） ===
    "skill": {
        "cognitive_decision_flywheel": {
            "min_occurrences": 3,
            "time_window_days": 30,
            "wiki_jaccard_threshold": 0.7,
            "min_usage_count": 5,
            "min_age_days": 7,
            "min_confidence": 0.6,
            "failure_rate_threshold": 0.3,
            "new_scenario_threshold": 3,
            "exception_threshold": 2,
            "cleanup_days": 60,
            "grace_period_days": 7,
        }
    },
    # === Embedding / Reranker 语义搜索 ===
    # NOTE: ttl_days / similarity_threshold / index_interval_hours 当前由对应
    # 模块内部常量驱动；真实 API key 默认只记录 env 名，避免写入公开配置。
    "embedding": {
        "enabled": True,  # 默认开启；无 API key 时各模块会优雅降级
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "api_key_env": "MNEMOS_EMBEDDING_API_KEY",
        "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
        "model": "BAAI/bge-m3",
        "embedding_model": "BAAI/bge-m3",
        "use_rerank": True,  # 是否启用重排（个人版默认开启）
    },
    "reranker": {
        "enabled": True,
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "api_key_env": "MNEMOS_RERANKER_API_KEY",
        "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
        "model": "BAAI/bge-reranker-v2-m3",
    },
    # === Optional multimodal / vision model ===
    "multimodal": {
        "enabled": False,
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "api_key_env": "MNEMOS_MULTIMODAL_API_KEY",
        "api_key_source": "",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "timeout": 120,
        # Hard reservation before a vision request.  A provider-specific
        # deployment may raise this, but may not omit it.
        "max_input_tokens": 131072,
        "chain": [],
        "providers": {},
        "rate_limits": {},
    },
    "relation_embedding": {
        # True = 批量 flush，减少写放大（默认）
        # False = 每次 add/remove 立即落盘（兼容旧行为）
        "batch_flush": True,
        "flush_interval_seconds": 60,
        "flush_batch_size": 10,
    },
    # === LLM API / 蒸馏模型 ===
    # chain: 主备 API 链，按优先级尝试。全部失败时暂停蒸馏。
    # api_key_source 支持: "hermes:<path>" / "env:<VAR>" / 空（直接读 api_key）
    "llm": {
        # 默认优先使用 SiliconFlow DeepSeek V4 Flash；免费 dmxapi 模型作为失败兜底。
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": "",
        "api_key_env": "MNEMOS_LLM_API_KEY",
        "api_key_source": "env:MNEMOS_LLM_API_KEY",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "timeout": 120,
        "api_key_sources": [],
        "key_strategy": "weighted",
        "race_timeout": 30,
        "providers": {
            "dmxapi": {
                "base_url": "https://www.dmxapi.cn/v1",
                "api_key": "",
                "api_key_env": "DMXAPI_API_KEY",
                "model": "kimi-k2.5-free",
            },
            "siliconflow": {
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "",
                "api_key_env": "SILICONFLOW_API_KEY",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
            },
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o-mini",
            },
        },
        # provider -> model -> {"input": 每 1K tokens 价格, "output": ...}
        # 用于 distill.llm_cost_budget_per_session 会话成本估算。
        "provider_prices": {},
        # provider -> {"rpm": int, "tpm": int,
        #              "models": {"model_name": {"rpm": int, "tpm": int}}}
        "rate_limits": {
            "dmxapi": {
                "rpm": 5,
                "models": {
                    "kimi-k2.5-free": {"rpm": 5},
                    "MiniMax-M2.7-free": {"rpm": 5},
                },
            },
            "siliconflow": {"rpm": 500, "tpm": 2000000},
        },
        # 调用策略：
        # - sequential：按 chain 顺序逐个尝试。
        # - priority_race：优先免费模型；免费模型限流时并行等待+兜底付费模型。
        "routing_strategy": "sequential",
        "chain": [
            {
                "provider": "siliconflow",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V4-Flash",
                "api_key": "",
                "api_key_source": "env:SILICONFLOW_API_KEY",
                "cost_level": "paid",
                "timeout": 120,
            },
            {
                "provider": "dmxapi",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "kimi-k2.5-free",
                "api_key": "",
                "api_key_source": "env:DMXAPI_API_KEY",
                "cost_level": "free",
                "timeout": 120,
            },
            {
                "provider": "dmxapi",
                "base_url": "https://www.dmxapi.cn/v1",
                "model": "MiniMax-M2.7-free",
                "api_key": "",
                "api_key_source": "env:DMXAPI_API_KEY",
                "cost_level": "free",
                "timeout": 120,
            },
        ],
    },
    # === 文档处理 ===
    "document_process": {
        "max_file_size_mb": 100,
    },
    # === 预加载模式 ===
    "preflight": {
        "mode": "full",  # light | full
        "timeout_sec": 5,
    },
    "oracle": {
        "index_cache_ttl_seconds": 60,
    },
    "push": {
        "index_cache_ttl_seconds": 60,
    },
    # === 意图路由 ===
    "intent_router": {
        "llm_fallback_enabled": False,
        "llm_fallback_threshold": 0.65,
        "llm_fallback_timeout_seconds": 2.0,
    },
    # === 上下文感知搜索 ===
    "search": {
        "weights": {},  # 覆盖 DEFAULT_WEIGHTS 中的项
    },
    # === Charon 关联 Worker ===
    "charon": {
        # None 表示使用模块内置默认词典；设为空列表则真正清空词典
        "tech_keywords": None,
        "concept_keywords": None,
    },
    # === 增量批处理 ===
    # NOTE: incremental.batch_interval 暂无消费点，待接入 capture worker 调度后恢复。
    "incremental": {},
    # === 功能开关 ===
    "features": {
        "enable_link_probe": False,
    },
}

CONFIG_REGISTRY.bind_defaults(DEFAULT_CONFIG, PERFORMANCE_TIERS)


class Config:
    """配置管理器 v2 — JSON + 环境变量"""

    def __init__(
        self, config_path: Optional[Path] = None, *, strict: bool = True, provision: bool = True
    ):
        self._mnemos_dir = self._resolve_mnemos_dir()
        self._use_default_config_path = config_path is None
        self._strict = strict
        self._provision = provision
        self.config_path = config_path or self._mnemos_dir / "configs" / "main.json"
        self._migrated_from_legacy = False
        self._ignored_obsolete_keys: tuple[str, ...] = ()
        self._runtime_environment: Dict[str, str] = {}
        self._persisted_data: Dict[str, Any] = {}
        self._persisted_source_data: Dict[str, Any] = {}
        self._effective_sources: Dict[str, str] = {key: "default" for key in CONFIG_REGISTRY.keys()}
        self._data = self._load()
        self._database_dir = self._resolve_database_dir()
        if self._provision:
            # 确保核心目录存在（首次运行或用户自定义路径时）
            self._mnemos_dir.mkdir(parents=True, exist_ok=True)
            self._database_dir.mkdir(parents=True, exist_ok=True)
            log_dirs = {self._mnemos_dir / "logs", self._database_dir / "logs"}
            for log_dir in log_dirs:
                log_dir.mkdir(parents=True, exist_ok=True)
            # 加固敏感目录权限
            secure_directory(self._mnemos_dir)
            secure_directory(self._database_dir)
            secure_directory(self._mnemos_dir / "configs")
            for log_dir in log_dirs:
                secure_directory(log_dir)
        if self._migrated_from_legacy:
            # A non-strict, non-provisioning Config is used by migration
            # diagnostics.  It must be able to inspect an old source without
            # silently materialising ``configs/main.json`` or creating a
            # runtime directory.  Strict readonly runtime callers still fail
            # closed and ask for an explicit migration.
            if not self._provision and self._strict:
                raise ConfigValidationError(
                    [
                        ConfigValidationIssue(
                            "migration_required",
                            str(self.config_path),
                            "legacy_config",
                        )
                    ]
                )
            if self._provision:
                self._write_config_file(self._persisted_data)
                logger.info("已自动迁移旧配置到 %s", self.config_path)

    def _resolve_mnemos_dir(self) -> Path:
        """确定 Mnemos 数据目录"""
        env = environment_get("MNEMOS_DIR")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".mnemos"

    def _resolve_database_dir(self) -> Path:
        """确定运行时数据目录（数据库 + 日志 + 事件 + 蒸馏产物等）

        优先级：MNEMOS_DATABASE_DIR 环境变量 > 配置文件 system.database_dir > data_dir
        """
        env = environment_get("MNEMOS_DATABASE_DIR")
        if env and not run_owned_override_is_stale(
            "MNEMOS_DATABASE_DIR",
            "MNEMOS_RUN_DEFAULT_DATABASE_DIR",
        ):
            return Path(env).expanduser()
        cfg = self._data.get("system", {}).get("database_dir")
        if cfg:
            return Path(cfg).expanduser()
        return self._mnemos_dir

    def _load(self) -> Dict:
        """加载配置：代码默认值 < 档位预设 < JSON 文件 < 环境变量

        加载顺序修正：
        1. 代码默认值
        2. 从 JSON 文件读取 performance_tier
        3. 应用该 tier 的档位预设
        4. 合并 JSON 文件的其他配置（用户显式覆盖项）
        5. 环境变量覆盖
        """
        data = copy.deepcopy(DEFAULT_CONFIG)

        # 先读取 JSON 文件（不合并），只为拿到用户设置的 performance_tier
        file_data = {}
        try:
            config_kind = inspect_path_kind(self.config_path)
            if config_kind == "file":
                file_data = json.loads(
                    read_native_bytes(self.config_path).decode("utf-8")
                )
                if not isinstance(file_data, dict):
                    raise ConfigValidationError(
                        [
                            ConfigValidationIssue(
                                "invalid_root_type",
                                str(self.config_path),
                                f"file:{self.config_path}",
                                expected_type="object",
                                actual_type=type(file_data).__name__,
                            )
                        ]
                    )
            elif config_kind != "missing":
                raise DurableIOError("config_path_not_regular")
        except ConfigValidationError:
            raise
        except (OSError, IOError, UnicodeError, json.JSONDecodeError) as e:
            logger.warning("配置文件加载失败: %s", e)
            if self._strict:
                raise ConfigValidationError(
                    [
                        ConfigValidationIssue(
                            "invalid_source",
                            str(self.config_path),
                            f"file:{self.config_path}",
                            expected_type="valid_json_object",
                            actual_type=type(e).__name__,
                        )
                    ]
                ) from e
            config_kind = "unavailable"
        if config_kind == "missing" and self._use_default_config_path:
            legacy_data = load_historical_config(
                mnemos_dir=self._mnemos_dir,
                config_path=self.config_path,
                log=logger,
            )
            if legacy_data:
                file_data = legacy_data
                self._migrated_from_legacy = True

        # Keep the exact source document for migration planning.  Runtime
        # compatibility sanitizers below may deliberately omit obsolete keys
        # from the effective/persisted runtime view, but they must not hide a
        # source migration obligation.
        self._persisted_source_data = copy.deepcopy(file_data)
        if self._migrated_from_legacy:
            file_data, _, _ = CONFIG_REGISTRY.migrate_aliases(file_data)
        # Preserve startup compatibility for obsolete local keys while explicitly
        # discarding values that cannot re-enable removed behavior.  The source
        # document is retained above for explicit migration and is never rewritten
        # implicitly.
        file_data, self._ignored_obsolete_keys = discard_non_restorable_capture_values(file_data)
        self._persisted_data = copy.deepcopy(file_data)

        # 应用性能档位预设（优先使用 JSON 中的 tier，其次默认值）
        tier = file_data.get("performance_tier", data.get("performance_tier", "default"))
        if tier not in PERFORMANCE_TIERS:
            raise ConfigValidationError(
                [
                    ConfigValidationIssue(
                        "invalid_value",
                        "performance_tier",
                        f"file:{self.config_path}",
                        expected_type="one_of:" + ",".join(sorted(PERFORMANCE_TIERS)),
                        actual_type=str(tier),
                    )
                ]
            )
        self._deep_merge(data, PERFORMANCE_TIERS[tier])
        self._mark_effective_sources(PERFORMANCE_TIERS[tier], f"tier:{tier}")
        data["performance_tier"] = tier  # 确保 tier 本身被保留
        self._effective_sources["performance_tier"] = (
            f"file:{self.config_path}" if "performance_tier" in file_data else "default"
        )

        # 合并 JSON 文件的其他配置（用户显式覆盖项优先于档位预设）
        if file_data:
            if self._strict:
                CONFIG_REGISTRY.assert_valid_override_tree(
                    file_data,
                    source=f"file:{self.config_path}",
                )
            self._deep_merge(data, file_data)
            self._mark_effective_sources(file_data, f"file:{self.config_path}")

        # 环境变量覆盖 (MNEMOS_* 前缀)
        self._apply_env_overrides(data)
        # 解析自动检测值
        self._resolve_auto_values(data)

        return data

    def _mark_effective_sources(self, values: Dict[str, Any], source: str) -> None:
        for key in CONFIG_REGISTRY.flatten_override(values):
            self._effective_sources[key] = source

    def _deep_merge(
        self,
        base: Dict,
        override: Dict,
        _visited: Optional[Set[int]] = None,
    ) -> None:
        visited = _visited if _visited is not None else set()
        override_id = id(override)
        if override_id in visited:
            logger.warning("[Config] 检测到循环引用配置，跳过合并")
            return
        visited.add(override_id)
        for key, val in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(val, dict):
                self._deep_merge(base[key], val, visited)
            else:
                base[key] = val
        visited.discard(override_id)

    def _apply_env_overrides(self, data: Dict):
        """Apply every environment mapping declared by the config registry."""
        env_map = CONFIG_REGISTRY.env_targets
        for env_var, path in env_map.items():
            val = environment_get(env_var)
            if val is None:
                continue
            marker_by_var = {
                "MNEMOS_WIKI_DIR": "MNEMOS_RUN_DEFAULT_WIKI_DIR",
            }
            marker = marker_by_var.get(env_var)
            if marker and run_owned_override_is_stale(env_var, marker):
                continue
            if env_var == "MNEMOS_MCP_LAUNCH_CAPABILITY_REF":
                self._runtime_environment[env_var] = val
                continue
            if path is None:
                continue
            typed_value = self.auto_type(val)
            errors = CONFIG_REGISTRY.validate_flat_values(
                {path: typed_value},
                source=f"env:{env_var}",
            )
            if errors:
                raise ConfigValidationError(errors)
            d = data
            parts = path.split(".")
            for k in parts[:-1]:
                d = d.setdefault(k, {})
            d[parts[-1]] = typed_value
            self._effective_sources[path] = f"env:{env_var}"

        # 通用 MNEMOS_ 前缀覆盖：MNEMOS_SCORING__RETRAIN_BUFFER → scoring.retrain_buffer
        for key, val in environment_items():
            if key.startswith("MNEMOS_") and key not in env_map and key != "MNEMOS_DIR":
                # MNEMOS_SCORING__RETRAIN_BUFFER → scoring.retrain_buffer
                parts = key[PARTS:].lower().split("__")
                if len(parts) >= 2:
                    dotted_key = ".".join(parts)
                    typed_value = self.auto_type(val)
                    errors = CONFIG_REGISTRY.validate_flat_values(
                        {dotted_key: typed_value},
                        source=f"env:{key}",
                    )
                    if errors and self._strict:
                        raise ConfigValidationError(errors)
                    if errors:
                        continue
                    d = data
                    for p in parts[:-1]:
                        d = d.setdefault(p, {})
                    d[parts[-1]] = typed_value
                    self._effective_sources[dotted_key] = f"env:{key}"

    @staticmethod
    def auto_type(val: str) -> Any:
        """尝试将字符串转为合适的类型"""
        return auto_type_environment_value(val)

    def _resolve_auto_values(self, data: Dict):
        # === Vault 路径解析（v2）===
        # raw / mnemos 为 canonical；wiki.vault_path 与 storage.obsidian.vault_path 为兼容别名。
        vaults = data.setdefault("vaults", {})
        raw_cfg = vaults.setdefault("raw", {"path": None, "enabled": True, "auto_register": True})
        mnemos_cfg = vaults.setdefault(
            "mnemos", {"path": None, "enabled": True, "auto_register": True}
        )

        if raw_cfg.get("path") is None:
            legacy_raw = data.get("storage", {}).get("obsidian", {}).get("vault_path")
            raw_cfg["path"] = legacy_raw if legacy_raw else str(self._default_raw_vault_path())
            self._effective_sources["vaults.raw.path"] = (
                "derived:storage.obsidian.vault_path" if legacy_raw else "auto:platform"
            )
        if mnemos_cfg.get("path") is None:
            legacy_wiki = data.get("wiki", {}).get("vault_path")
            mnemos_cfg["path"] = (
                legacy_wiki if legacy_wiki else str(self._default_mnemos_vault_path())
            )
            self._effective_sources["vaults.mnemos.path"] = (
                "derived:wiki.vault_path" if legacy_wiki else "auto:platform"
            )

        # 保持旧属性与 canonical vault 路径同步，避免旧代码读到不一致路径。
        data["wiki"]["vault_path"] = mnemos_cfg["path"]
        data.setdefault("storage", {}).setdefault("obsidian", {})["vault_path"] = raw_cfg["path"]
        self._effective_sources["wiki.vault_path"] = "derived:vaults.mnemos.path"
        self._effective_sources["storage.obsidian.vault_path"] = "derived:vaults.raw.path"

        # === 跨层认知图数据库路径 ===
        cg = data.setdefault("cognitive_graph", {})
        if cg.get("db_path") is None:
            cg["db_path"] = str(self._mnemos_dir / "cognitive_graph.db")
            self._effective_sources["cognitive_graph.db_path"] = "auto:mnemos_dir"

        cc_path = data["integrations"]["claude_code"]["settings_json_path"]
        if cc_path is None:
            data["integrations"]["claude_code"]["settings_json_path"] = str(
                default_claude_settings_path()
            )
            self._effective_sources["integrations.claude_code.settings_json_path"] = "auto:platform"

        # 自动检测：如果 embedding api_key 已配置（配置文件或环境变量），自动启用 embedding。
        # 但尊重 low_power / eco 档位的显式关闭，避免环境变量意外覆盖节能意图。
        emb = data.get("embedding", {})
        tier = data.get("performance_tier", "default")
        api_key = emb.get("api_key", "")
        env_api_key = environment_get("SILICONFLOW_API_KEY") or environment_get("OPENAI_API_KEY")
        if (
            tier not in ("low_power", "eco")
            and not emb.get("enabled", False)
            and (api_key or env_api_key)
        ):
            emb["enabled"] = True
            self._effective_sources["embedding.enabled"] = "auto:external_credential"
            logger.info("[Config] 检测到 embedding api_key 已配置，自动启用语义搜索")

    def _default_mnemos_vault_path(self) -> Path:
        """主认知 Vault 默认路径"""
        if sys.platform in ("darwin", "win32"):
            return Path.home() / "Documents" / "mnemos"
        return Path.home() / "mnemos"

    def _default_raw_vault_path(self) -> Path:
        """L1 raw Vault 默认路径"""
        if sys.platform in ("darwin", "win32"):
            return Path.home() / "Documents" / "raw"
        return Path.home() / "raw"

    _default_wiki_path = _default_mnemos_vault_path

    def _write_config_file(self, data: Dict[str, Any]) -> None:
        return write_config_file(self.config_path, data, self._provision)

    def save(self):
        """Persist explicit source values without materializing env/default layers."""
        CONFIG_REGISTRY.assert_valid_override_tree(
            self._persisted_data,
            source="runtime:save",
        )
        self._write_config_file(self._persisted_data)

    def persisted_data(self) -> Dict[str, Any]:
        """Return only values present in the persisted source document."""
        import copy

        return copy.deepcopy(self._persisted_data)

    def persisted_source_data(self) -> Dict[str, Any]:
        """Return the exact pre-sanitization source document for migrations."""
        import copy

        return copy.deepcopy(self._persisted_source_data)

    def replace_persisted_data(self, data: Dict[str, Any]) -> None:
        """Validate and atomically replace a migrated source document."""
        CONFIG_REGISTRY.assert_valid_override_tree(data, source="migration")
        self._write_config_file(data)
        import copy

        self._persisted_data = copy.deepcopy(data)
        self._data = self._load()
        self._database_dir = self._resolve_database_dir()

    # ---- 核心访问方法 ----

    @property
    def mnemos_dir(self) -> Path:
        return self._mnemos_dir

    @property
    def data_dir(self) -> Path:
        return self._mnemos_dir

    @property
    def database_dir(self) -> Path:
        return self._database_dir

    @property
    def wiki_dir(self) -> Path:
        """兼容别名，指向主认知 Vault（mnemos）。"""
        return self.vault_dir("mnemos")

    def vault_dir(self, name: str) -> Path:
        """返回指定 vault 的目录路径。"""
        vaults = self._data.get("vaults", {})
        if name not in vaults:
            raise KeyError(f"未知 vault: {name}")
        return Path(vaults[name]["path"]).expanduser()

    def vault_enabled(self, name: str) -> bool:
        """指定 vault 是否启用。"""
        return self._data.get("vaults", {}).get(name, {}).get("enabled", False)  # type: ignore[no-any-return]  # noqa: E501

    def list_vaults(self) -> list[str]:
        """返回所有已配置 vault 名称。"""
        return list(self._data.get("vaults", {}).keys())

    @property
    def persona_enabled(self) -> bool:
        return self._data["persona"]["enabled"]  # type: ignore[no-any-return]

    @property
    def persona_data_sources(self) -> Dict:
        return self._data["persona"]["data_sources"]  # type: ignore[no-any-return]

    def is_source_enabled(
        self, source: str
    ) -> bool:  # noqa: Vulture - public Config API for persona source gates.
        # type: ignore[no-any-return]
        return self._data["persona"]["data_sources"].get(source, {}).get("enabled", False)  # type: ignore[no-any-return]  # noqa: E501

    @property
    def claude_code_enabled(self) -> bool:
        return self._data["integrations"]["claude_code"]["enabled"]  # type: ignore[no-any-return]

    @property
    def claude_settings_path(self) -> Path:
        return Path(self._data["integrations"]["claude_code"]["settings_json_path"]).expanduser()

    @property
    def mcp_enabled(self) -> bool:
        return self._data["integrations"]["mcp"]["enabled"]  # type: ignore[no-any-return]

    @property
    def cross_agent_share(self) -> bool:
        return self._data.get("cross_agent_share", False)  # type: ignore[no-any-return]

    @property
    def storage_backend(self) -> str:
        """当前配置的存储后端类型（仅 obsidian）"""
        return self._data.get("storage", {}).get("backend", "obsidian").lower()  # type: ignore[no-any-return]  # noqa: E501

    @property
    def obsidian_vault_path(self) -> Path:
        """Return the configured Obsidian Raw projection path."""
        return self.vault_dir("raw")

    @property
    def cognitive_graph_enabled(self) -> bool:
        # type: ignore[no-any-return]
        return self._data.get("cognitive_graph", {}).get("enabled", True)  # type: ignore[no-any-return]  # noqa: E501

    @property
    def l1_storage_enabled(self) -> bool:
        """遗留外部 L1/Memos 存储是否启用（已弃用）"""
        return self._data.get("l1_storage", {}).get("enabled", False)  # type: ignore[no-any-return]

    @property
    def l1_storage_token(self) -> str:
        """遗留外部 L1/Memos 存储 Token（优先环境变量，回退配置文件）"""
        return environment_get("L1_STORAGE_TOKEN") or self._data.get("l1_storage", {}).get(
            "token", ""
        )

    @property
    def l1_storage_api_url(self) -> str:
        """遗留外部 L1/Memos 存储 API URL"""
        return environment_get("L1_STORAGE_API_URL") or self._data.get("l1_storage", {}).get(
            "api_url", ""
        )

    @property
    def cognitive_graph_db_path(self) -> Path:
        return Path(self._data["cognitive_graph"]["db_path"]).expanduser()

    @property
    def claude_data_dir(self) -> Path:
        # 优先使用用户显式配置
        configured = self._data.get("claude_data_dir")
        if configured:
            p = Path(configured).expanduser()
            if p.exists():
                return p

        # 新版 Claude Code 默认 ~/.claude，优先检测
        modern = Path.home() / ".claude"
        if modern.exists():
            return modern
        if sys.platform == "win32":
            p = Path(environment_get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude"
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Claude"
        else:
            p = Path.home() / ".config" / "claude"
        return p if p.exists() else modern

    def get(self, key: str, default=None) -> Any:
        """按点号路径获取配置值：config.get('scoring.retrain_buffer')"""
        spec = CONFIG_REGISTRY.require(key)
        keys = key.split(".")
        val = self._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                raise ConfigValidationError(
                    [
                        ConfigValidationIssue(
                            "missing_effective_key",
                            key,
                            "runtime",
                            expected_type=spec.value_type_name,
                            actual_type="missing",
                        )
                    ]
                )
        return val

    def explain(self, key: str) -> Dict[str, Any]:
        """Return value-free provenance for one canonical effective key."""
        spec = CONFIG_REGISTRY.require(key)
        return {
            "key": key,
            "effective_source": self._effective_sources.get(key, "default"),
            "value_type": type(self.get(key)).__name__,
            "secret": spec.secret,
        }

    @property
    def config_fingerprint(self) -> str:
        """Stable hash of the effective canonical configuration; never exposes values."""
        return CONFIG_REGISTRY.fingerprint(self._data)

    def get_runtime_environment(self, name: str, default: str = "") -> str:
        """Return an env-only process value that is never persisted by save()."""
        return self._runtime_environment.get(name, default)

    def set(self, key: str, value: Any):
        """按点号路径设置配置值"""
        value = CONFIG_REGISTRY.coerce_runtime_value(
            key,
            value,
            source="runtime:set",
        )
        keys = key.split(".")
        for root in (self._data, self._persisted_data):
            data = root
            for k in keys[:-1]:
                if k not in data or not isinstance(data[k], dict):
                    data[k] = {}
                data = data[k]
            data[keys[-1]] = value
        self._effective_sources[key] = "runtime:set"

    def to_dict(self) -> Dict:
        import copy

        return copy.deepcopy(self._data)

    def load_agent_config(self, agent_name: str) -> Dict:
        """加载指定 Agent 的配置"""
        agents_path = self._mnemos_dir / "configs" / "agents.json"
        try:
            agents_kind = inspect_path_kind(agents_path)
            if agents_kind == "file":
                agents = json.loads(read_native_bytes(agents_path).decode("utf-8"))
                return agents.get(agent_name, {})  # type: ignore[no-any-return]
            if agents_kind != "missing":
                raise DurableIOError("agent_config_path_not_regular")
        except (OSError, IOError, UnicodeError, json.JSONDecodeError):
            logger.warning("Agent 配置加载失败", exc_info=True)
        return {}


# 全局配置实例
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config
    scoped = current_config()
    if scoped is not None:
        return cast(Config, scoped)
    if _config is None:
        _config = Config()
    return _config


def reload_config():
    """重新加载配置"""
    global _config
    _config = Config()


def reset_config() -> None:
    """重置全局配置单例，用于 daemon 重启或测试隔离。"""
    global _config
    _config = None
