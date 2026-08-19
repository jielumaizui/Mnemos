"""
全局测试配置与共享 Fixture

[P0-FIX] 重构说明：
1. 将模块导入时的全局 monkeypatch 改为 fixture 管理的临时 patch，
   避免污染整个 Python 进程的 tempfile 行为。
2. 新增共享 fixture：fake_config、tmp_db、reset_singletons，
   消除各测试文件中重复定义 FakeConfig / 手动重置单例的 boilerplate。
3. EventBus 隔离保持 autouse，确保所有测试默认不写入生产 events.db。
"""

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_RUN_ENVIRONMENT = None


def _formal_state_targets(environment):
    manifest = environment.get("MNEMOS_RUN_ENVIRONMENT_MANIFEST")
    if manifest:
        try:
            payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
            formal_targets = tuple(
                Path(value).expanduser().resolve(strict=False)
                for value in payload.get("formal_state_targets", [])
            )
            pause_target = next(
                path for path in formal_targets if str(path).endswith("distillation_state.db")
            )
            database_dir = pause_target.parent.resolve(strict=False)
            return tuple(
                dict.fromkeys((*formal_targets, *_projection_targets_for_database(database_dir)))
            )
        except (OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            pass
    home = Path(environment.get("HOME") or Path.home()).expanduser().resolve(strict=False)
    mnemos_dir = (
        Path(environment.get("MNEMOS_DIR") or home / ".mnemos").expanduser().resolve(strict=False)
    )
    database_value = environment.get("MNEMOS_DATABASE_DIR")
    if not database_value:
        config_path = mnemos_dir / "configs" / "main.json"
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            database_value = payload.get("system", {}).get("database_dir")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            database_value = None
    database_dir = Path(database_value).expanduser() if database_value else mnemos_dir
    database_dir = database_dir.resolve(strict=False)
    return tuple(
        dict.fromkeys(
            (
                mnemos_dir / "configs" / "main.json",
                database_dir / "distillation_state.db",
                mnemos_dir / "benchmarks" / "golden" / "latest",
                *_projection_targets_for_database(database_dir),
            )
        )
    )


def _projection_targets_for_database(database_dir):
    return tuple(
        database_dir / name
        for name in (
            "knowledge_graph.db",
            "knowledge_graph.db-wal",
            "wiki_metrics.db",
            "wiki_metrics.db-wal",
            "wiki_projection.db",
            "wiki_projection.db-wal",
            "embedding_index/relation_index.bin",
            "embedding_index/wiki_index.bin",
            "embedding_index/wiki_meta.json",
        )
    )


_FORMAL_STATE_TARGETS = _formal_state_targets(dict(os.environ))


def _bind_process_to_hermetic_run(root):
    """Freeze the pre-collection run identity and refresh tempfile's env cache."""

    from core.ops.hermetic_run import bind_process_run_environment

    bind_process_run_environment(dict(os.environ))
    tempfile.tempdir = None
    active_temp = Path(tempfile.gettempdir()).resolve(strict=False)
    if active_temp != root and root not in active_temp.parents:
        raise pytest.UsageError(f"hermetic tempfile directory escapes run root: {active_temp}")


def _activate_hermetic_test_environment():
    """Install the run boundary before pytest imports any test modules."""

    if os.environ.get("MNEMOS_RUN_ENVIRONMENT_HASH"):
        root = Path(os.environ["MNEMOS_RUN_ROOT"]).resolve(strict=False)
        for key in (
            "HOME",
            "MNEMOS_DIR",
            "MNEMOS_DATABASE_DIR",
            "MNEMOS_WIKI_DIR",
            "TMPDIR",
            "PYTHONPYCACHEPREFIX",
        ):
            path = Path(os.environ[key]).resolve(strict=False)
            if path != root and root not in path.parents:
                raise pytest.UsageError(f"inherited hermetic path escapes run root: {key}")
        _bind_process_to_hermetic_run(root)
        return None

    from core.ops.hermetic_run import HermeticRunEnvironment

    base_environment = dict(os.environ)
    root = Path(tempfile.mkdtemp(prefix="mnemos-pytest-"))
    run = HermeticRunEnvironment.create(
        root,
        profile="isolated",
        base_environment=base_environment,
    )
    os.environ.clear()
    os.environ.update(run.environment)
    os.environ["MNEMOS_TEST_RUN"] = "1"
    _bind_process_to_hermetic_run(run.root)
    return run


_TEST_RUN_ENVIRONMENT = _activate_hermetic_test_environment()


def pytest_configure(config):
    """Keep every pytest basetemp inside the process-bound HRE."""

    root_value = os.environ.get("MNEMOS_RUN_ROOT")
    if not root_value:
        raise pytest.UsageError("pytest requires a process-bound hermetic run root")
    raw_root = Path(root_value).expanduser()
    if raw_root.is_symlink():
        raise pytest.UsageError("pytest hermetic run root cannot be a symlink")
    root = Path(root_value).resolve(strict=False)
    temporary = root / "tmp"
    if not root.is_dir() or temporary.is_symlink() or not temporary.is_dir():
        raise pytest.UsageError("pytest hermetic temporary parent is invalid or symlinked")
    dedicated = temporary / "pytest"
    if dedicated.is_symlink():
        raise pytest.UsageError("pytest dedicated basetemp cannot be a symlink")
    if dedicated.exists() and not dedicated.is_dir():
        raise pytest.UsageError("pytest dedicated basetemp must be a directory")
    if dedicated.resolve(strict=False) != dedicated:
        raise pytest.UsageError("pytest dedicated basetemp resolves outside its lexical path")
    configured = config.option.basetemp
    if configured is None:
        config.option.basetemp = str(dedicated)
        return
    raw_basetemp = Path(configured).expanduser()
    if raw_basetemp.is_symlink():
        raise pytest.UsageError("configured pytest basetemp cannot be a symlink")
    basetemp = raw_basetemp.resolve(strict=False)
    if basetemp != dedicated:
        raise pytest.UsageError(
            f"pytest basetemp must equal the dedicated hermetic path {dedicated}: {basetemp}"
        )


def pytest_sessionfinish(session, exitstatus):
    """Fail the whole test run if a protected formal target changed."""

    del exitstatus
    if _TEST_RUN_ENVIRONMENT is None:
        return
    changed = _TEST_RUN_ENVIRONMENT.finalize()
    if changed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "Hermetic test boundary detected formal state changes: " + ", ".join(changed),
                red=True,
            )


@pytest.fixture(scope="session")
def _production_state_targets():
    """Capture formal state paths before tests can replace configuration."""
    return _FORMAL_STATE_TARGETS


@pytest.fixture(autouse=True)
def _forbid_production_projection_mutation(_production_state_targets, monkeypatch):
    """Deterministically block this process from writing formal state.

    Per-test before/after hashes cannot attribute daemon writes to the test
    process. Direct connection and file-operation guards can.
    """

    protected_databases = {
        str(path.resolve(strict=False))
        for path in _production_state_targets
        if path.name
        in {
            "distillation_state.db",
            "knowledge_graph.db",
            "wiki_metrics.db",
            "wiki_projection.db",
        }
    }
    original_connect = sqlite3.connect
    original_path_open = Path.open
    original_os_open = os.open
    original_unlink = Path.unlink
    original_rename = Path.rename
    original_replace = Path.replace

    protected_files = {str(path.resolve(strict=False)) for path in _production_state_targets}

    def _is_protected(path) -> bool:
        return str(Path(path).expanduser().resolve(strict=False)) in protected_files

    def guarded_connect(database, *args, **kwargs):
        raw = str(database)
        plain_path = raw.removeprefix("file:").split("?", 1)[0]
        if (
            str(Path(plain_path).expanduser().resolve(strict=False)) in protected_databases
            and "mode=ro" not in raw
        ):
            raise AssertionError("Test attempted writable production SQLite connection: " + raw)
        return original_connect(database, *args, **kwargs)

    def guarded_path_open(path, mode="r", *args, **kwargs):
        if _is_protected(path) and any(flag in mode for flag in "wax+"):
            raise AssertionError(f"Test attempted writable production file open: {path}")
        return original_path_open(path, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if _is_protected(path) and flags & write_flags:
            raise AssertionError(f"Test attempted writable production os.open: {path}")
        return original_os_open(path, flags, *args, **kwargs)

    def guarded_unlink(path, *args, **kwargs):
        if _is_protected(path):
            raise AssertionError(f"Test attempted production file deletion: {path}")
        return original_unlink(path, *args, **kwargs)

    def guarded_rename(path, target, *args, **kwargs):
        if _is_protected(path) or _is_protected(target):
            raise AssertionError(f"Test attempted production file rename: {path}")
        return original_rename(path, target, *args, **kwargs)

    def guarded_replace(path, target, *args, **kwargs):
        if _is_protected(path) or _is_protected(target):
            raise AssertionError(f"Test attempted production file replace: {path}")
        return original_replace(path, target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    monkeypatch.setattr(Path, "rename", guarded_rename)
    monkeypatch.setattr(Path, "replace", guarded_replace)
    yield


def _root_magicmock_artifacts():
    return sorted(_REPO_ROOT.glob("*MagicMock*"))


@pytest.fixture(autouse=True)  # noqa
def _forbid_root_magicmock_artifacts():
    """Fail fast if tests leak MagicMock-derived paths into the repo root."""
    existing = _root_magicmock_artifacts()
    if existing:
        paths = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in existing)
        pytest.fail(f"MagicMock test artifacts present in repository root: {paths}")

    yield

    leaked = _root_magicmock_artifacts()
    if leaked:
        paths = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in leaked)
        pytest.fail(f"Test leaked MagicMock artifacts into repository root: {paths}")


@pytest.fixture(autouse=True)
def _isolate_amphora_queue(monkeypatch, tmp_path):
    """Keep pytest distillation tasks out of the user's real Mnemos queue."""
    import core.kia.amphora as amphora

    monkeypatch.setattr(amphora, "_DB_PATH", tmp_path / "distill_queue.db")


# ---- 共享 FakeConfig ----


class FakeConfig:
    """测试中统一使用的配置替身。

    [P1-FIX] 替代散落在 5+ 测试文件中的 _FakeConfig / _FAKE_CONFIG 定义。
    支持通过 kwargs 覆盖任意字段。
    """

    def __init__(self, **overrides):
        self._tmpdir = Path(tempfile.gettempdir()) / f"mnemos_test_{time.time():.6f}"
        self._tmpdir.mkdir(parents=True, exist_ok=True)
        self.mnemos_dir = overrides.get("mnemos_dir", self._tmpdir / ".mnemos")
        self.data_dir = overrides.get("data_dir", self._tmpdir / "data")
        self.database_dir = overrides.get("database_dir", self.data_dir)
        self.wiki_dir = overrides.get("wiki_dir", self._tmpdir / "wiki")
        self.raw_dir = overrides.get("raw_dir", self._tmpdir / "raw")
        self._values = {
            "capture.max_queue_depth": 10000,
            "capture.per_source_max_queue_depth": 1000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "daemon.startup_stagger_seconds": 0.1,
            **overrides.get("_extra", {}),
        }

    def get(self, key, default=None):
        return self._values.get(key, default)

    @property
    def obsidian_vault_path(self):
        return self.raw_dir

    @property
    def cognitive_graph_db_path(self):
        return self.database_dir / "cognitive_graph.db"

    def vault_dir(self, name: str):
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.raw_dir
        raise KeyError(name)

    def cleanup(self):
        """测试结束后清理临时目录。"""
        if self._tmpdir.exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)


# ---- Fixture: 假配置 ----


@pytest.fixture
def fake_config():
    """提供一个隔离的 FakeConfig，测试结束后自动清理临时目录。"""
    cfg = FakeConfig()
    yield cfg
    cfg.cleanup()


@pytest.fixture
def patched_get_config(monkeypatch, fake_config):
    """自动将 core.config.get_config 替换为返回 fake_config 的 stub。

    用法：在测试函数中直接声明此 fixture，无需手动 patch。
    """
    import core.config as _config_mod

    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
    return fake_config


@pytest.fixture
def canonical_material_actions(request, tmp_path):
    """Opt in to real canonical action permits for an explicit test boundary.

    The resolver uses each sink's declared canonical state database.  The
    temporary path is only a fallback for a seam that has no store binding;
    this is a real DecisionTrace seal/authorize/terminal path, not a bypass.
    It is deliberately not autouse: production callers and tests must expose
    the orchestration boundary that owns authorization.
    """

    if request.node.get_closest_marker("no_canonical_material_actions"):
        yield
        return
    from tests.cognitive_decision_fixtures import canonical_material_action_scope

    with canonical_material_action_scope(tmp_path):
        yield


@pytest.fixture
def _canonical_material_actions(canonical_material_actions):
    """Vulture-visible alias for tests that need only the fixture side effect."""

    return canonical_material_actions


# ---- Fixture: 临时 SQLite 数据库 ----


@pytest.fixture
def tmp_db_path(tmp_path):
    """返回一个临时 SQLite 文件路径，测试结束后自动删除。"""
    db_file = tmp_path / "test.db"
    return db_file


@pytest.fixture  # noqa
def tmp_db_conn(tmp_db_path):
    """返回一个已连接的临时 SQLite 连接，测试结束后自动关闭。"""
    conn = sqlite3.connect(str(tmp_db_path))
    conn.row_factory = sqlite3.Row  # noqa
    yield conn
    conn.close()


# ---- Fixture: 单例重置 ----


@pytest.fixture(autouse=True)  # noqa
def reset_singletons(monkeypatch, fake_config):
    """[P1-FIX] 每个测试前后重置常见单例，防止状态泄漏。

    注意：仅重置内部状态标志，不强制重新导入模块。
    在 setup 中 patch core.kia.rule_scorer.get_config，使规则权重存储与
    优化器使用隔离的临时目录，避免测试间污染真实 rule_weights.db。
    在 teardown 中临时 patch get_config，避免 module-level side effect 读到
    MagicMock 或未初始化的真实配置。
    """
    # 隔离 RuleWeightStore / RuleScorer 到临时目录
    try:
        import core.kia.rule_scorer as _rule_scorer_mod

        monkeypatch.setattr(_rule_scorer_mod, "get_config", lambda: fake_config)
    except ImportError:
        _rule_scorer_mod = None

    # 重置 EventBus 全局单例，避免事件表在测试间泄漏
    try:
        import core.mnemos_bus as _bus_mod

        _bus_mod._global_bus = None
    except Exception:
        pass

    # 重置蒸馏暂停状态，避免其他测试或运行时留下的暂停标志导致测试 flaky
    try:
        from core.hephaestus import distillation_pause

        monkeypatch.setattr(distillation_pause, "get_config", lambda: fake_config)
        distillation_pause.resume_distillation()
    except Exception:
        pass

    yield

    if _rule_scorer_mod is not None:
        _rule_scorer_mod._rule_weight_store_instance = None
        _rule_scorer_mod._shared_rule_scorer_instance = None

    with patch("core.config.get_config", lambda: fake_config):
        # CaptureService
        try:
            from core.sync_framework.capture_service import CaptureService

            CaptureService._instance = None
            CaptureService._initialized = False
        except ImportError:
            pass

    # AgentRegistry / SourceRegistry
    try:
        from core.sync_framework.registry import SourceRegistry

        SourceRegistry._registry.clear()
        SourceRegistry._instances.clear()
    except ImportError:
        pass

    # PathDiscover cache
    try:
        from core.sync_framework.registry import PathDiscover

        PathDiscover.invalidate_cache()
    except ImportError:
        pass

    # 强制触发 GC，帮助释放 SQLite 连接持有的文件描述符
    gc.collect()


# ---- Fixture: Windows SQLite 文件锁兼容性 ----


@pytest.fixture(autouse=True)  # noqa
def _windows_sqlite_cleanup(monkeypatch):
    """[P0-FIX] 将原模块级的全局 monkeypatch 改为 fixture 级临时 patch。

    Windows 上 sqlite3 连接在测试 tearDown 时可能仍未被 GC 释放，
    导致 tempfile.TemporaryDirectory.cleanup() 抛出 PermissionError。
    修复：在 Windows 上重试 cleanup，给 GC 足够时间释放文件描述符。

    非 Windows 平台此 fixture 无实际作用。
    """
    if sys.platform != "win32":
        yield
        return

    _original_cleanup = tempfile.TemporaryDirectory.cleanup

    def _patched_cleanup(self):
        gc.collect()
        time.sleep(0.05)
        for attempt in range(5):
            try:
                return _original_cleanup(self)
            except (PermissionError, NotADirectoryError):
                gc.collect()
                time.sleep(0.2 * (attempt + 1))
        # 最终 fallback：强制删除
        shutil.rmtree(self.name, ignore_errors=True)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", _patched_cleanup)
    yield
    # pytest monkeypatch 会自动恢复，无需手动处理


# ---- Fixture: EventBus 隔离 ----


@pytest.fixture(autouse=True)  # noqa
def _isolate_eventbus(monkeypatch, tmp_path_factory):
    """隔离 EventBus 与 Wiki mutation ledger，禁止测试写入生产数据库。"""

    projection_dir = tmp_path_factory.mktemp("wiki-projection")
    monkeypatch.setattr(
        "core.wiki_projection_lifecycle._default_db_path",
        lambda: projection_dir / "wiki_projection.db",
    )
    monkeypatch.setattr(
        "core.mnemos_bus.resolve_wiki_projection_db_path",
        lambda _config=None: projection_dir / "wiki_projection.db",
    )
    monkeypatch.setattr(
        "core.mnemos_bus.publish_event",
        lambda *a, **k: k.get("trace_id") or "isolated-test-trace",
    )


# ---- Fixture: Obsidian vault 列表隔离 ----


@pytest.fixture(autouse=True)  # noqa
def _isolate_obsidian_config(monkeypatch, tmp_path_factory):
    """所有测试自动隔离：将 Obsidian 的 obsidian.json 指向临时文件，
    防止测试用临时 vault 污染真实 Obsidian vault 列表。

    配置目录通过 ``tmp_path_factory`` 单独创建，避免与测试自身的 ``tmp_path``
    混在一起，导致扫描目录的测试误把 obsidian.json 当成业务文件。
    """
    run_root = os.environ.get("MNEMOS_RUN_ROOT")
    if run_root:
        config_dir = Path(run_root) / "tmp" / f"obsidian-config-{uuid.uuid4().hex}"
        config_dir.mkdir(parents=True)
    else:
        config_dir = tmp_path_factory.mktemp("obsidian-config")
    config_path = config_dir / "obsidian.json"
    config_path.write_text('{"vaults": {}}', encoding="utf-8")
    monkeypatch.setenv("MNEMOS_OBSIDIAN_CONFIG_PATH", str(config_path))
    yield
