"""Helpers for `mnemos doctor`.

The CLI file is intentionally kept as a thin presentation layer; diagnostic
logic that needs tests lives here.
"""

from __future__ import annotations

import json
import os
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from core.config import DEFAULT_CONFIG, PERFORMANCE_TIERS


@dataclass(frozen=True)
class ConfigSetting:
    key: str
    label: str
    value: Any
    source: str
    formatter: str = "raw"


@dataclass(frozen=True)
class OptionalDependencyStatus:
    name: str
    module: str
    class_name: str
    status: str
    detail: str


LEGACY_OPTIONAL_DEPENDENCIES = [
    ("KnowledgeTrail", "core.knowledge_trail", "KnowledgeTrail"),
    ("免疫", "core.knowledge_immune", "KnowledgeImmuneSystem"),
    ("DNA", "core.knowledge_dna", "DNAEngine"),
    ("认知决策飞轮", "core.kia.ixion", "CognitiveDecisionFlywheel"),
    ("证伪", "core.falsifiability_marker", "FalsifiabilityMarker"),
    ("知识画像", "core.knowledge_profile", "ProfileGenerator"),
    ("时间胶囊", "core.kia.aion", "TimeCapsule"),
    ("熵", "core.entropy_engine", "EntropyEngine"),
    ("快照", "core.kia.ananke", "VersionTimeTravel"),
    ("影子页面", "core.kia.hecate", "ShadowPageManager"),
]


_PERFORMANCE_SETTING_SPECS = [
    ("embedding.enabled", "embedding", "bool_enabled"),
    ("embedding.use_rerank", "rerank", "bool_enabled"),
    ("capture.max_workers", "max_workers", "raw"),
    ("capture.max_payload_bytes", "max_payload", "bytes"),
]


def _nested_get(data: Any, key: str, default: Any | None = None) -> Any:
    value = data
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _env_name_for_key(key: str) -> str:
    return f"MNEMOS_{key.upper().replace('.', '__')}"


def _config_file_has_key(config_path: Optional[Path], key: str) -> bool:
    if not config_path:
        return False
    path = Path(config_path)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _nested_get(data, key, None) is not None


def config_value_source(config: Any, key: str) -> str:
    """Best-effort source label for a resolved config value."""
    env_name = _env_name_for_key(key)
    if env_name in os.environ:
        return f"env:{env_name}"

    config_path = getattr(config, "config_path", None)
    if _config_file_has_key(config_path, key):
        return f"config:{Path(config_path)}"  # type: ignore[arg-type]

    tier = str(config.get("performance_tier", "default") or "default")
    if _nested_get(PERFORMANCE_TIERS.get(tier, {}), key, None) is not None:
        return f"performance_tier:{tier}"

    if _nested_get(DEFAULT_CONFIG, key, None) is not None:
        return "default"
    return "runtime"


def describe_performance_settings(config: Any) -> list[ConfigSetting]:
    return [
        ConfigSetting(
            key=key,
            label=label,
            value=config.get(key),
            source=config_value_source(config, key),
            formatter=formatter,
        )
        for key, label, formatter in _PERFORMANCE_SETTING_SPECS
    ]


def _format_value(setting: ConfigSetting) -> str:
    if setting.formatter == "bool_enabled":
        return "开启" if setting.value else "关闭"
    if setting.formatter == "bytes":
        return f"{setting.value} bytes"
    return str(setting.value)


def format_performance_settings(
    settings: Iterable[ConfigSetting], *, verbose: bool = False
) -> list[str]:
    lines = []
    for setting in settings:
        suffix = f" [source={setting.source}]" if verbose else ""
        lines.append(f"  {setting.label}: {_format_value(setting)}{suffix}")
    return lines


def optional_dependency_statuses(
    specs: Iterable[tuple[str, str, str]] = LEGACY_OPTIONAL_DEPENDENCIES,
) -> list[OptionalDependencyStatus]:
    from core.import_guard import assert_allowed_module

    statuses = []
    for name, module_path, class_name in specs:
        try:
            assert_allowed_module(module_path)
            module = importlib.import_module(module_path)
        except ImportError:
            statuses.append(
                OptionalDependencyStatus(
                    name=name,
                    module=module_path,
                    class_name=class_name,
                    status="missing",
                    detail="模块未安装/已移除",
                )
            )
            continue
        except (ValueError, ImportError, AttributeError, RuntimeError) as exc:
            statuses.append(
                OptionalDependencyStatus(
                    name=name,
                    module=module_path,
                    class_name=class_name,
                    status="error",
                    detail=f"导入失败: {type(exc).__name__}",
                )
            )
            continue
        if not hasattr(module, class_name):
            statuses.append(
                OptionalDependencyStatus(
                    name=name,
                    module=module_path,
                    class_name=class_name,
                    status="missing_symbol",
                    detail="模块存在但缺少目标符号",
                )
            )
            continue
        statuses.append(
            OptionalDependencyStatus(
                name=name,
                module=module_path,
                class_name=class_name,
                status="available",
                detail="可用",
            )
        )
    return statuses


def format_optional_dependency_statuses(
    statuses: Iterable[OptionalDependencyStatus],
) -> list[str]:
    lines = []
    for item in statuses:
        state = "ok" if item.status == "available" else "skip"
        lines.append(f"  {item.name}: {state} ({item.module}.{item.class_name}) - {item.detail}")
    return lines
