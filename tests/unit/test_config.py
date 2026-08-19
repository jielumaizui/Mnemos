"""
Config 模块单元测试 — 覆盖配置加载、环境变量、路径解析、单例行为。

测试策略：
- 每个测试使用独立的 tmp_path，避免交叉污染
- monkeypatch 控制环境变量和 Path.home()
- 每次测试后重置 get_config() / reload_config() 的全局单例
"""

import json
import sys
from pathlib import Path

import pytest

# ---- 单例重置 fixture ----


@pytest.fixture(autouse=True)  # noqa
def reset_config_singleton(monkeypatch):
    """每个测试前后重置 Config 全局单例，并清理可能泄漏的环境变量。"""
    import core.config as _config_mod

    _config_mod._config = None
    # 清理可能从外部注入的环境变量，确保测试隔离
    monkeypatch.delenv("MNEMOS_WIKI_DIR", raising=False)
    monkeypatch.delenv("MNEMOS_DAEMON__SERVICES__L1_SYNC", raising=False)
    monkeypatch.delenv("MNEMOS_SCORING__RETRAIN_BUFFER", raising=False)
    monkeypatch.delenv("MNEMOS_CAPTURE__MAX_WORKERS", raising=False)
    monkeypatch.delenv("MNEMOS_CROSS_AGENT_SHARE", raising=False)
    monkeypatch.delenv("MNEMOS_PERFORMANCE_TIER", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__BOOL_TRUE", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__BOOL_YES", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__BOOL_FALSE", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__INT", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__FLOAT", raising=False)
    monkeypatch.delenv("MNEMOS_TEST__STRING", raising=False)
    monkeypatch.delenv("L1_STORAGE_API_URL", raising=False)
    monkeypatch.delenv("L1_STORAGE_TOKEN", raising=False)
    yield
    _config_mod._config = None


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    """将 Path.home() 指向临时目录，模拟干净的用户主目录。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


# ==== 测试：Config.__init__ 默认值 ====


def test_config_defaults_with_no_files(mock_home, monkeypatch):
    """无任何配置文件时，Config 应使用代码默认值并正确解析路径。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    # mnemos_dir 默认指向 ~/.mnemos
    assert cfg.mnemos_dir == mock_home / ".mnemos"
    assert cfg.data_dir == mock_home / ".mnemos"

    # config_path 默认指向 ~/.mnemos/configs/main.json
    assert cfg.config_path == mock_home / ".mnemos" / "configs" / "main.json"

    # wiki_dir 平台相关自动检测（v2 默认主认知 Vault）
    if sys.platform in ("darwin", "win32"):
        expected_wiki = mock_home / "Documents" / "mnemos"
    else:
        expected_wiki = mock_home / "mnemos"
    assert cfg.wiki_dir == expected_wiki

    # raw vault 默认路径
    if sys.platform in ("darwin", "win32"):
        expected_raw = mock_home / "Documents" / "raw"
    else:
        expected_raw = mock_home / "raw"
    assert cfg.obsidian_vault_path == expected_raw

    # 认知图数据库默认路径
    assert cfg.cognitive_graph_db_path == mock_home / ".mnemos" / "cognitive_graph.db"

    # vault 访问器
    assert cfg.vault_enabled("mnemos") is True
    assert cfg.vault_enabled("raw") is True
    assert "mnemos" in cfg.list_vaults()
    assert "raw" in cfg.list_vaults()
    assert cfg.vault_dir("mnemos") == expected_wiki

    # 核心默认值断言
    assert cfg.get("performance_tier") == "default"
    assert cfg.get("cross_agent_share") is False
    assert cfg.get("scoring.ewma_alpha") == 0.1
    assert cfg.get("capture.max_workers") == 4
    assert cfg.get("daemon.services.capture_worker") is True
    assert cfg.get("daemon.services.raw_projection") is True
    assert cfg.get("raw_projection.enabled") is True
    assert cfg.get("raw_projection.chunk_turns") == 5
    assert cfg.get("raw_projection.max_files") == 0
    assert cfg.get("raw_projection.max_turn_chars") == 0
    assert cfg.get("distill.structured_output_contract.enforce") is True
    assert cfg.get("reflection_export.max_records_per_run") == 200
    assert cfg.get("reflection_export.max_records_per_day") == 20
    assert cfg.get("shadow_projection.max_pages_per_batch") == 50
    assert cfg.get("dispute_scan.max_daily_disputes") == 10
    assert cfg.get("dispute_scan.max_pages_per_scan") == 500
    assert cfg.get("dispute_scan.min_conflict_strength") == 0.5
    assert cfg.get("dispute_scan.auto_resolve_min_gap") == 0.30
    assert cfg.get("dispute_scan.merge_min_gap") == 0.15
    assert cfg.get("dispute_scan.freshness_half_life_days") == 30
    assert cfg.get("dispute_scan.citation_max_reference") == 20
    assert cfg.get("dispute_scan.adaptive_learning.enabled") is False
    assert cfg.get("dispute_scan.weights.confidence") == 0.25

    # 属性访问器
    assert cfg.persona_enabled is True
    assert cfg.mcp_enabled is True
    assert cfg.claude_code_enabled is True
    assert cfg.cross_agent_share is False


def test_config_secures_runtime_log_directories(mock_home, monkeypatch):
    """Config 初始化应创建并收敛敏感日志目录权限。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    logs_dir = cfg.data_dir / "logs"
    assert logs_dir.exists()
    assert logs_dir.is_dir()
    assert logs_dir.stat().st_mode & 0o777 == 0o700


def test_default_wiki_path_legacy_alias_matches_mnemos_vault(mock_home, monkeypatch):
    """_default_wiki_path 是旧兼容别名，应始终指向主认知 vault 默认路径。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    assert cfg._default_wiki_path() == cfg._default_mnemos_vault_path()
    assert cfg._default_wiki_path() == cfg.wiki_dir


def test_config_loads_json_file(mock_home, monkeypatch):
    """Config 应正确加载 JSON 配置文件并覆盖默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_data = {
        "performance_tier": "performance",
        "cross_agent_share": False,
    }
    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps(config_data), encoding="utf-8")

    cfg = Config()

    assert cfg.get("performance_tier") == "performance"
    assert cfg.cross_agent_share is False

    # 未被覆盖的默认值仍保留
    assert cfg.get("scoring.ewma_alpha") == 0.1
    assert cfg.persona_enabled is True


# ==== 测试：性能档位预设 ====


def test_performance_tier_low_power(mock_home, monkeypatch):
    """低功耗档位应正确覆盖 embedding、capture、distill 等默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({"performance_tier": "low_power"}), encoding="utf-8")

    cfg = Config()

    assert cfg.get("performance_tier") == "low_power"
    assert cfg.get("embedding.enabled") is False
    assert cfg.get("capture.max_workers") == 1
    assert cfg.get("distill.max_tasks_per_cycle") == 1
    assert cfg.get("scheduler.worker_threads") == 1
    assert cfg.get("daemon.services.distill_and_merge") is False
    assert cfg.get("daemon.services.capture_worker") is True  # 核心链路保留


def test_performance_tier_dev(mock_home, monkeypatch):
    """dev 档位应启用更高资源配额。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({"performance_tier": "dev"}), encoding="utf-8")

    cfg = Config()

    assert cfg.get("performance_tier") == "dev"
    assert cfg.get("embedding.enabled") is True
    assert cfg.get("capture.max_workers") == 8
    assert cfg.get("distill.max_tasks_per_cycle") == 20
    assert cfg.get("distill.token_budget_total") == 64000


# ==== 测试：环境变量覆盖 ====


def test_mnemos_dir_env_override(mock_home, monkeypatch):
    """MNEMOS_DIR 环境变量应覆盖默认的 ~/.mnemos 路径。"""
    custom_dir = mock_home / "custom_mnemos"
    custom_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MNEMOS_DIR", str(custom_dir))
    from core.config import Config

    cfg = Config()

    assert cfg.mnemos_dir == custom_dir
    assert cfg.data_dir == custom_dir
    assert cfg.config_path == custom_dir / "configs" / "main.json"


def test_cross_agent_share_true_from_json(mock_home, monkeypatch):
    """cross_agent_share=True 应能从 JSON 配置文件正确加载。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({"cross_agent_share": True}), encoding="utf-8")

    cfg = Config()
    assert cfg.cross_agent_share is True


def test_mnemos_performance_tier_env_override(mock_home, monkeypatch):
    """MNEMOS_PERFORMANCE_TIER 环境变量应覆盖默认性能档位。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    monkeypatch.setenv("MNEMOS_PERFORMANCE_TIER", "low_power")
    cfg = Config()
    assert cfg.get("performance_tier") == "low_power"


def test_mnemos_wiki_dir_env_override(mock_home, monkeypatch):
    """MNEMOS_WIKI_DIR 环境变量应覆盖 wiki.vault_path。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    custom_wiki = mock_home / "my_wiki"
    custom_wiki.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MNEMOS_WIKI_DIR", str(custom_wiki))

    cfg = Config()

    assert cfg.wiki_dir == custom_wiki


def test_legacy_l1_storage_properties_contract(mock_home, monkeypatch):
    """旧外部 L1/Memos 属性保留配置回退与环境变量优先级契约。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    assert cfg.l1_storage_enabled is False
    assert cfg.l1_storage_token == ""
    assert cfg.l1_storage_api_url == ""

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    config_path = configs_dir / "main.json"
    config_path.write_text(
        json.dumps(
            {
                "l1_storage": {
                    "enabled": True,
                    "token": "config-token",
                    "api_url": "https://config.example.test",
                }
            }
        ),
        encoding="utf-8",
    )

    cfg = Config()
    assert cfg.l1_storage_enabled is True
    assert cfg.l1_storage_token == "config-token"
    assert cfg.l1_storage_api_url == "https://config.example.test"

    monkeypatch.setenv("L1_STORAGE_TOKEN", "env-token")
    monkeypatch.setenv("L1_STORAGE_API_URL", "https://env.example.test")
    assert cfg.l1_storage_token == "env-token"
    assert cfg.l1_storage_api_url == "https://env.example.test"


def test_generic_mnemos_env_override(mock_home, monkeypatch):
    """通用 MNEMOS_* 前缀环境变量应支持嵌套配置覆盖（双下划线表示嵌套）。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setenv("MNEMOS_SCORING__MIN_SAMPLES_PER_DIMENSION", "99")
    monkeypatch.setenv("MNEMOS_CAPTURE__MAX_WORKERS", "16")
    monkeypatch.setenv("MNEMOS_APP__PUSH_MAX_ITEMS", "25")

    cfg = Config()

    assert cfg.get("scoring.min_samples_per_dimension") == 99
    assert cfg.get("capture.max_workers") == 16
    assert cfg.get("app.push_max_items") == 25


def test_env_auto_type_conversion(mock_home, monkeypatch):
    """环境变量值应自动转换为 bool、int、float 等类型。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({}), encoding="utf-8")

    # 使用双下划线构造嵌套路径，覆盖到已有配置命名空间下
    monkeypatch.setenv("MNEMOS_PERSONA__AB_TEST_ENABLED", "true")
    monkeypatch.setenv("MNEMOS_CAPTURE__MAX_WORKERS", "42")
    monkeypatch.setenv("MNEMOS_QUALITY_GATE__BASE_THRESHOLD", "3.14")
    monkeypatch.setenv("MNEMOS_STORAGE__BACKEND", "hello")

    cfg = Config()

    assert cfg.get("persona.ab_test_enabled") is True
    assert cfg.get("capture.max_workers") == 42
    assert cfg.get("quality_gate.base_threshold") == 3.14
    assert cfg.get("storage.backend") == "hello"


# ==== 测试：get() 方法 ====


def test_get_dot_path(mock_home, monkeypatch):
    """get() 应支持点号路径访问嵌套配置。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    assert cfg.get("scoring.min_samples_per_dimension") == 12
    assert cfg.get("daemon.services.capture_worker") is True
    assert cfg.get("persona.data_sources.session.enabled") is True


def test_is_source_enabled_contract(mock_home, monkeypatch):
    """is_source_enabled() 是 persona 数据源开关的稳定访问入口。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    assert cfg.is_source_enabled("session") is True
    assert cfg.is_source_enabled("git") is False
    assert cfg.is_source_enabled("unknown") is False


def test_get_missing_key_is_rejected_by_registry(mock_home, monkeypatch):
    """get() 不允许调用点为未知键发明默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    from core.config_registry import UnknownConfigKeyError

    with pytest.raises(UnknownConfigKeyError):
        cfg.get("nonexistent.key")
    with pytest.raises(UnknownConfigKeyError):
        cfg.get("nonexistent.key", "fallback")
    with pytest.raises(UnknownConfigKeyError):
        cfg.get("scoring.nonexistent", 123)


# ==== 测试：set() 与 to_dict() ====


def test_set_and_to_dict(mock_home, monkeypatch):
    """set() 应修改内部数据，to_dict() 应返回深拷贝。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    cfg.set("storage.backend", "value")
    assert cfg.get("storage.backend") == "value"

    d = cfg.to_dict()
    assert d["storage"]["backend"] == "value"

    # to_dict 返回深拷贝，修改不应影响原配置
    d["storage"]["backend"] = "modified"
    assert cfg.get("storage.backend") == "value"


# ==== 测试：单例行为 ====


def test_get_config_singleton(mock_home, monkeypatch):
    """get_config() 应返回同一实例（单例模式）。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import get_config

    cfg1 = get_config()
    cfg2 = get_config()

    assert cfg1 is cfg2


def test_reload_config_creates_new_instance(mock_home, monkeypatch):
    """reload_config() 应创建新的 Config 实例。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import get_config, reload_config

    cfg1 = get_config()
    reload_config()
    cfg2 = get_config()

    assert cfg1 is not cfg2
    assert cfg2.config_path == cfg1.config_path


# ==== 测试：无效配置处理 ====


def test_invalid_json_is_rejected_in_strict_mode(mock_home, monkeypatch, caplog):
    """损坏的 JSON 配置文件不能静默回退到默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config
    from core.config_registry import ConfigValidationError

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text("not valid json {{{", encoding="utf-8")

    with caplog.at_level("WARNING", logger="core.config"):
        with pytest.raises(ConfigValidationError):
            Config()

    assert "配置文件加载失败" in caplog.text


def test_missing_config_file_uses_defaults(mock_home, monkeypatch):
    """配置文件完全不存在时，应使用代码默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()

    assert cfg.config_path == mock_home / ".mnemos" / "configs" / "main.json"
    assert not cfg.config_path.exists()
    assert cfg.get("performance_tier") == "default"


# ==== 测试：legacy YAML 迁移 ====


def test_legacy_yaml_migration(mock_home, monkeypatch):
    """存在旧版 config.yaml 时，应自动迁移到 main.json。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    mnemos_dir.mkdir(parents=True, exist_ok=True)

    legacy_yaml = mnemos_dir / "config.yaml"
    legacy_yaml.write_text(
        "wiki:\n  vault_path: /legacy/wiki\nstorage:\n  backend: obsidian\n",
        encoding="utf-8",
    )

    cfg = Config()

    assert cfg.wiki_dir.as_posix() == "/legacy/wiki"
    assert cfg.storage_backend == "obsidian"
    assert cfg.config_path.exists()  # 迁移后保存了 main.json


# ==== 测试：save() ====


def test_save_persists_config(mock_home, monkeypatch):
    """save() 应将当前配置写入 JSON 文件。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    config_path = configs_dir / "main.json"
    config_path.write_text(json.dumps({"storage": {"backend": "obsidian"}}), encoding="utf-8")

    cfg = Config()
    cfg.set("capture.max_workers", 42)
    cfg.save()

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["capture"]["max_workers"] == 42
    assert saved["storage"]["backend"] == "obsidian"


def test_save_does_not_materialize_default_or_environment_layers(mock_home, monkeypatch):
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    monkeypatch.setenv("MNEMOS_DISTILL__TOKEN_BUDGET_TOTAL", "32000")
    from core.config import Config

    config_path = mock_home / ".mnemos" / "configs" / "main.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}", encoding="utf-8")
    cfg = Config()
    cfg.set("app.push_max_items", 8)
    cfg.save()

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted == {"app": {"push_max_items": 8}}
    assert cfg.get("distill.token_budget_total") == 32000


# ==== 测试：load_agent_config ====


def test_load_agent_config(mock_home, monkeypatch):
    """load_agent_config() 应正确读取 agents.json。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    agents_path = configs_dir / "agents.json"
    agents_path.write_text(
        json.dumps({"claude": {"model": "claude-sonnet"}, "hermes": {"model": "gpt-4"}}),
        encoding="utf-8",
    )

    cfg = Config()
    claude_cfg = cfg.load_agent_config("claude")
    hermes_cfg = cfg.load_agent_config("hermes")
    missing_cfg = cfg.load_agent_config("nonexistent")

    assert claude_cfg == {"model": "claude-sonnet"}
    assert hermes_cfg == {"model": "gpt-4"}
    assert missing_cfg == {}


def test_load_agent_config_missing_file(mock_home, monkeypatch):
    """agents.json 不存在时，load_agent_config() 应返回空字典。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    cfg = Config()
    result = cfg.load_agent_config("any_agent")

    assert result == {}


# ==== 测试：自定义 config_path ====


def test_custom_config_path(mock_home, monkeypatch):
    """传入自定义 config_path 时，不应使用默认路径。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    custom_path = mock_home / "my_config.json"
    custom_path.write_text(json.dumps({"storage": {"backend": "obsidian"}}), encoding="utf-8")

    cfg = Config(config_path=custom_path)

    assert cfg.config_path == custom_path
    assert cfg.storage_backend == "obsidian"
    # mnemos_dir 仍由 _resolve_mnemos_dir 决定
    assert cfg.mnemos_dir == mock_home / ".mnemos"


def test_custom_config_path_never_follows_a_leaf_symlink(
    mock_home,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config
    from core.config_registry import ConfigValidationError

    outside = mock_home / "outside.json"
    outside.write_text(
        json.dumps({"performance_tier": "performance"}),
        encoding="utf-8",
    )
    alias = mock_home / "config-alias.json"
    alias.symlink_to(outside)

    with pytest.raises(ConfigValidationError):
        Config(config_path=alias, strict=True, provision=False)


# ==== 测试：环境变量优先级 ====


def test_env_overrides_json_overrides_defaults(mock_home, monkeypatch):
    """验证优先级链：环境变量 > JSON 文件 > 性能档位 > 代码默认值。"""
    monkeypatch.delenv("MNEMOS_DIR", raising=False)
    from core.config import Config

    mnemos_dir = mock_home / ".mnemos"
    configs_dir = mnemos_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    # JSON 文件设置 tier=performance，并覆盖 capture.max_workers
    config_path = configs_dir / "main.json"
    config_path.write_text(
        json.dumps(
            {
                "performance_tier": "performance",
                "capture": {"max_workers": 6},  # 覆盖 performance 档位的 8
            }
        ),
        encoding="utf-8",
    )

    # 环境变量进一步覆盖
    monkeypatch.setenv("MNEMOS_CAPTURE__MAX_WORKERS", "99")

    cfg = Config()

    # 环境变量优先级最高
    assert cfg.get("capture.max_workers") == 99
    # JSON 中的 tier 被保留
    assert cfg.get("performance_tier") == "performance"
    # performance 档位的其他预设生效
    assert cfg.get("distill.max_tasks_per_cycle") == 10
