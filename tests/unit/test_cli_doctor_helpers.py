from pathlib import Path


class DummyConfig:
    def __init__(self):
        self.config_path = Path("/tmp/mnemos/configs/main.json")
        self._store = {
            "performance_tier": "default",
            "embedding": {"enabled": True, "use_rerank": True},
            "capture": {"max_workers": 4, "max_payload_bytes": 200000},
        }

    def get(self, key, default=None):
        value = self._store
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


def test_describe_performance_settings_includes_sources(monkeypatch):
    from core.cli.doctor_helpers import describe_performance_settings

    monkeypatch.setenv("MNEMOS_CAPTURE__MAX_WORKERS", "9")
    rows = describe_performance_settings(DummyConfig())
    by_key = {row.key: row for row in rows}

    assert by_key["embedding.enabled"].source == "performance_tier:default"
    assert by_key["embedding.use_rerank"].source == "performance_tier:default"
    assert by_key["capture.max_workers"].source == "env:MNEMOS_CAPTURE__MAX_WORKERS"
    assert by_key["capture.max_payload_bytes"].source == "performance_tier:default"


def test_format_performance_settings_verbose_marks_source():
    from core.cli.doctor_helpers import describe_performance_settings, format_performance_settings

    text = "\n".join(
        format_performance_settings(describe_performance_settings(DummyConfig()), verbose=True)
    )

    assert "embedding: 开启 [source=performance_tier:default]" in text
    assert "max_workers: 4 [source=performance_tier:default]" in text


def test_optional_dependency_status_reports_missing_modules():
    from core.cli.doctor_helpers import optional_dependency_statuses

    statuses = optional_dependency_statuses(
        [("missing_feature", "integrations.definitely_missing_module_for_test", "MissingClass")]
    )

    assert statuses[0].name == "missing_feature"
    assert statuses[0].status == "missing"
    assert statuses[0].detail == "模块未安装/已移除"


def test_optional_dependency_status_reports_import_errors(monkeypatch):
    from types import SimpleNamespace

    import core.cli.doctor_helpers as helpers

    def boom(_module_path):
        raise RuntimeError("broken import")

    monkeypatch.setattr(helpers, "importlib", SimpleNamespace(import_module=boom))

    statuses = helpers.optional_dependency_statuses([("broken", "core.broken", "Broken")])

    assert statuses[0].status == "error"
    assert statuses[0].detail == "导入失败: RuntimeError"


def test_format_optional_dependency_statuses_is_user_visible():
    from core.cli.doctor_helpers import (
        OptionalDependencyStatus,
        format_optional_dependency_statuses,
    )

    text = "\n".join(
        format_optional_dependency_statuses(
            [
                OptionalDependencyStatus(
                    name="DNA",
                    module="core.knowledge_dna",
                    class_name="DNAEngine",
                    status="missing",
                    detail="模块未安装/已移除",
                )
            ]
        )
    )

    assert "DNA: skip (core.knowledge_dna.DNAEngine) - 模块未安装/已移除" in text


def test_kia_optional_dependencies_are_available():
    """验证 doctor 中引用的 KIA 可选依赖模块路径正确且可导入。"""
    from core.cli.doctor_helpers import optional_dependency_statuses

    deps = [
        ("时间胶囊", "core.kia.aion", "TimeCapsule"),
        ("快照", "core.kia.ananke", "VersionTimeTravel"),
        ("影子页面", "core.kia.hecate", "ShadowPageManager"),
    ]
    statuses = optional_dependency_statuses(deps)
    for s in statuses:
        assert s.status == "available", f"{s.name} 应为 available，实际是 {s.status}: {s.detail}"
