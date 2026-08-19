# -*- coding: utf-8 -*-
"""Tests for core.import_guard dynamic import whitelist (S23)."""

from __future__ import annotations

import pytest

from core.import_guard import assert_allowed_module, is_allowed_module


class TestImportGuard:
    @pytest.mark.parametrize(
        "module_path",
        [
            "core.config",
            "core.kia.amphora",
            "integrations.agora",
            "integrations.sources.claude_source",
        ],
    )
    def test_allows_core_and_integrations(self, module_path: str) -> None:
        assert is_allowed_module(module_path) is True
        assert_allowed_module(module_path)  # should not raise

    @pytest.mark.parametrize(
        "module_path",
        [
            "os.system",
            "subprocess.Popen",
            "pickle",
            "requests",
            "",
            "../../etc/passwd",
        ],
    )
    def test_rejects_arbitrary_modules(self, module_path: str) -> None:
        assert is_allowed_module(module_path) is False
        with pytest.raises(ValueError):
            assert_allowed_module(module_path)

    def test_import_guard_blocks_disallowed_in_preflight_builder(self) -> None:
        from integrations.preflight_builder import _import_optional_class

        assert _import_optional_class("os.system", "system") is None

    def test_import_guard_allows_allowed_in_preflight_builder(self, monkeypatch) -> None:
        from integrations.preflight_builder import _import_optional_class

        class FakeModule:
            Sentinel = object()

        monkeypatch.setitem(__import__("sys").modules, "core.kia.fake_sentinel", FakeModule())
        assert _import_optional_class("core.kia.fake_sentinel", "Sentinel") is FakeModule.Sentinel

    def test_import_guard_blocks_disallowed_in_apollon(self) -> None:
        from integrations.apollon import _import_optional_class

        assert _import_optional_class("subprocess.Popen", "Popen") is None

    def test_import_guard_blocks_disallowed_in_distillation_engine(self) -> None:
        from core.hephaestus.distillation_engine import _try_init

        assert _try_init("os.system", "system") is None
