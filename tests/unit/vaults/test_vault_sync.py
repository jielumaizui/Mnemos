# -*- coding: utf-8 -*-
"""Unit tests for core/vaults/vault_sync.py"""

from unittest.mock import MagicMock, patch

import pytest

from core.vaults import vault_sync as vault_sync_module
from core.vaults.vault_sync import (
    _vault_git_commit,
    sync_all_projections,
    sync_kg_projection,
    sync_observation_projection,
    sync_reflection_projection,
)


class TestVaultGitCommit:
    """_vault_git_commit 测试"""

    def test_initializes_repo_and_commits(self, tmp_path):
        """当 vault 未初始化 git 时，应自动 init 并提交。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        (vault_dir / "hello.md").write_text("# Hello", encoding="utf-8")

        # 确保环境中存在 git
        result = _vault_git_commit(vault_dir, "test commit")
        assert result["committed"] is True
        assert (vault_dir / ".git").exists()
        assert "test commit" in result["output"] or result["output"] != ""

    def test_returns_false_when_vault_missing(self, tmp_path):
        """vault 目录不存在时应直接返回 committed=False。"""
        missing = tmp_path / "missing"
        result = _vault_git_commit(missing, "test")
        assert result == {"committed": False, "output": ""}

    def test_captures_exception_output(self, tmp_path):
        """subprocess 抛出异常时，异常信息应写入 output。"""
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()

        with patch.object(
            vault_sync_module.subprocess,
            "run",
            side_effect=RuntimeError("git not found"),
        ):
            result = _vault_git_commit(vault_dir, "test")

        assert result["committed"] is False
        assert "git not found" in result["output"]


class TestSyncAllProjections:
    """sync_all_projections 测试"""

    @pytest.fixture
    def mock_importers_exporters(self):
        """mock 所有 projection 依赖的导入/导出类。"""
        # 这些类在 vault_sync 函数内部通过 from ... import 引入，
        # 需要 patch 它们的真实定义模块。
        with (
            patch("core.kia.kg_exporter.KGExporter") as mock_kg,
            patch("core.kia.knowledge_graph.KnowledgeGraph") as mock_graph,
            patch(
                "core.cognitive.observation_projection.rebuild_observation_projection"
            ) as mock_obs,
            patch("core.reflection.reflection_exporter.ReflectionExporter") as mock_refl_exp,
            patch("core.reflection.reflection_store.ReflectionStore") as mock_refl_store,
            patch("core.persona.delphi.PersonaStore") as mock_persona,
            patch("core.vaults.vault_sync._vault_git_commit") as mock_commit,
        ):

            kg_instance = MagicMock()
            kg_instance.export_to_vault.return_value = {"entities": 3, "relations": 5}
            mock_kg.return_value = kg_instance

            mock_obs.return_value = MagicMock(
                observation_count=2,
                dimension_count=2,
            )

            refl_instance = MagicMock()
            refl_instance.export_all.return_value = {"records": 4, "shifts": 2}
            mock_refl_exp.return_value = refl_instance
            mock_refl_store.return_value = MagicMock()

            mock_persona.load_canonical_persona_versions_read_only.return_value = [
                (MagicMock(version="v1"), None)
            ]
            persona_instance = mock_persona.for_projection_replay.return_value
            persona_instance.project_all_personas.return_value = {
                "current": 1,
                "history": 0,
            }

            mock_commit.return_value = {"committed": True, "output": "ok"}

            yield {
                "kg": mock_kg,
                "graph": mock_graph,
                "obs": mock_obs,
                "refl_exp": mock_refl_exp,
                "refl_store": mock_refl_store,
                "persona": mock_persona,
                "commit": mock_commit,
            }

    def test_sync_kg_projection_composes_and_closes_graph(self, tmp_path, fake_config):
        vault_dir = tmp_path / "vault"
        fake_config.database_dir.mkdir(parents=True, exist_ok=True)
        (fake_config.database_dir / "knowledge_graph.db").touch()
        with (
            patch("core.kia.kg_exporter.KGExporter") as mock_exporter,
            patch("core.kia.knowledge_graph.KnowledgeGraph") as mock_graph,
        ):
            mock_exporter.return_value.export_to_vault.return_value = {
                "entities": 2,
                "relations": 3,
            }

            result = sync_kg_projection(vault_dir, config=fake_config)

        assert result == {
            "status": "ok",
            "entities": 2,
            "relations": 3,
            "error": "",
        }
        mock_graph.assert_called_once_with(
            db_path=str(fake_config.database_dir / "knowledge_graph.db"),
            wiki_base=str(vault_dir),
            initialize=False,
            read_only=True,
            config=fake_config,
        )
        mock_exporter.assert_called_once_with(
            str(vault_dir),
            kg=mock_graph.return_value,
            lifecycle=None,
            emit_runtime_consumption=False,
        )
        mock_graph.return_value.close.assert_called_once_with()

    def test_sync_all_projections_returns_summary(
        self,
        tmp_path,
        patched_get_config,
        mock_importers_exporters,
    ):
        """sync_all_projections 应返回包含各层结果的 summary。"""
        vault_dir = tmp_path / "vault"
        raw_dir = tmp_path / "raw"

        patched_get_config.database_dir.mkdir(parents=True, exist_ok=True)
        (patched_get_config.database_dir / "knowledge_graph.db").touch()
        summary = sync_all_projections(
            vault_dir=vault_dir,
            raw_dir=raw_dir,
            config=patched_get_config,
        )

        assert summary["vault_dir"] == str(vault_dir)
        assert summary["raw_dir"] == str(raw_dir)

        assert summary["kg"]["status"] == "ok"
        assert summary["kg"]["entities"] == 3
        assert summary["kg"]["relations"] == 5

        assert summary["observation"]["status"] == "ok"
        assert summary["observation"]["observations"] == 2
        assert summary["observation"]["dimensions"] == 2

        assert summary["reflection"]["status"] == "ok"
        assert summary["reflection"]["records"] == 4
        assert summary["reflection"]["shifts"] == 2

        assert summary["persona"]["status"] == "ok"
        assert summary["persona"]["version"] == "v1"
        assert summary["canonical_delta"] == {}
        assert summary["status"] == "ok"

        mock_importers_exporters["obs"].assert_called_once()
        mock_importers_exporters["persona"].return_value.save_persona.assert_not_called()
        mock_importers_exporters[
            "persona"
        ].for_projection_replay.return_value.project_all_personas.assert_called_once()

        assert summary["git"]["committed"] is True
        mock_importers_exporters["commit"].assert_called_once()

    def test_sync_all_projections_skips_git_when_commit_false(
        self,
        tmp_path,
        patched_get_config,
        mock_importers_exporters,
    ):
        """commit=False 时不应调用 _vault_git_commit。"""
        summary = sync_all_projections(
            vault_dir=tmp_path / "vault",
            raw_dir=tmp_path / "raw",
            commit=False,
            config=patched_get_config,
        )
        assert summary["git"] == {"committed": False, "output": "skipped"}
        mock_importers_exporters["commit"].assert_not_called()


class TestIndividualProjectionErrors:
    """单个 projection 在 exporter 抛出异常时的错误处理测试"""

    def test_sync_kg_projection_returns_error(self, tmp_path, fake_config):
        """KGExporter 抛出异常时，应返回 error 状态。"""
        fake_config.database_dir.mkdir(parents=True, exist_ok=True)
        (fake_config.database_dir / "knowledge_graph.db").touch()
        with (
            patch("core.kia.kg_exporter.KGExporter") as mock_kg,
            patch("core.kia.knowledge_graph.KnowledgeGraph"),
        ):
            mock_kg.side_effect = RuntimeError("kg export failed")
            result = sync_kg_projection(tmp_path / "vault", config=fake_config)

        assert result["status"] == "error"
        assert "kg export failed" in result["error"]

    def test_sync_observation_projection_returns_error(self, tmp_path, fake_config):
        """Read-only Observation replay errors remain visible."""
        with patch(
            "core.cognitive.observation_projection.rebuild_observation_projection"
        ) as mock_obs:
            mock_obs.side_effect = RuntimeError("observation engine failed")
            result = sync_observation_projection(
                tmp_path / "vault",
                config=fake_config,
            )

        assert result["status"] == "error"
        assert "observation engine failed" in result["error"]

    def test_sync_reflection_projection_returns_error(self, tmp_path, fake_config):
        """Reflection projection exceptions remain visible."""
        with (
            patch("core.reflection.reflection_exporter.ReflectionExporter") as mock_exp,
            patch("core.reflection.reflection_store.ReflectionStore"),
        ):
            mock_exp.side_effect = RuntimeError("reflection export failed")
            result = sync_reflection_projection(
                tmp_path / "vault",
                config=fake_config,
            )

        assert result["status"] == "error"
        assert "reflection export failed" in result["error"]


def test_sync_all_projections_fails_closed_on_canonical_delta(
    tmp_path,
    fake_config,
    monkeypatch,
):
    fake_config.database_dir.mkdir(parents=True, exist_ok=True)
    target = fake_config.database_dir / "knowledge_graph.db"
    target.write_bytes(b"before")

    def mutate_source(_vault_dir, **_kwargs):
        target.write_bytes(b"after")
        return {"status": "ok"}

    monkeypatch.setattr(vault_sync_module, "sync_kg_projection", mutate_source)
    for name in (
        "sync_observation_projection",
        "sync_reflection_projection",
        "sync_persona_projection",
    ):
        monkeypatch.setattr(
            vault_sync_module,
            name,
            lambda _vault_dir, **_kwargs: {"status": "skipped"},
        )

    with pytest.raises(RuntimeError, match="knowledge_graph.db"):
        sync_all_projections(
            vault_dir=tmp_path / "vault",
            raw_dir=tmp_path / "raw",
            commit=False,
            config=fake_config,
        )


def test_sync_all_projections_tracks_cognitive_graph_canonical_delta(
    tmp_path,
    fake_config,
    monkeypatch,
):
    fake_config.database_dir.mkdir(parents=True, exist_ok=True)
    target = fake_config.database_dir / "cognitive_graph.db"
    target.write_bytes(b"before")

    def mutate_source(_vault_dir, **_kwargs):
        target.write_bytes(b"after")
        return {"status": "ok"}

    monkeypatch.setattr(vault_sync_module, "sync_kg_projection", mutate_source)
    for name in (
        "sync_observation_projection",
        "sync_reflection_projection",
        "sync_persona_projection",
    ):
        monkeypatch.setattr(
            vault_sync_module,
            name,
            lambda _vault_dir, **_kwargs: {"status": "skipped"},
        )

    with pytest.raises(RuntimeError, match="cognitive_graph.db"):
        sync_all_projections(
            vault_dir=tmp_path / "vault",
            raw_dir=tmp_path / "raw",
            commit=False,
            config=fake_config,
        )


def test_canonical_hash_fallback_includes_uncheckpointed_wal(
    tmp_path,
    monkeypatch,
):
    database_dir = tmp_path / "database"
    database_dir.mkdir()
    main = database_dir / "knowledge_graph.db"
    wal = database_dir / "knowledge_graph.db-wal"
    main.write_bytes(b"main")
    wal.write_bytes(b"wal-before")

    def fail_snapshot(*_args, **_kwargs):
        raise vault_sync_module.sqlite3.DatabaseError("read-only WAL unavailable")

    monkeypatch.setattr(vault_sync_module.sqlite3, "connect", fail_snapshot)
    before = vault_sync_module._canonical_source_hashes(database_dir)
    wal.write_bytes(b"wal-after")
    after = vault_sync_module._canonical_source_hashes(database_dir)

    assert before["knowledge_graph.db"] != after["knowledge_graph.db"]


def test_sync_all_projections_does_not_commit_partial_failure(
    tmp_path,
    fake_config,
    monkeypatch,
):
    fake_config.database_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "knowledge_graph.db",
        "observations.db",
        "reflections.db",
        "user_signals.db",
        "producer_consumer_ledger.db",
        "cognitive_graph.db",
    ):
        (fake_config.database_dir / name).write_bytes(name.encode("utf-8"))

    monkeypatch.setattr(
        vault_sync_module,
        "sync_kg_projection",
        lambda _vault_dir, **_kwargs: {"status": "error", "error": "kg failed"},
    )
    for name in (
        "sync_observation_projection",
        "sync_reflection_projection",
        "sync_persona_projection",
    ):
        monkeypatch.setattr(
            vault_sync_module,
            name,
            lambda _vault_dir, **_kwargs: {"status": "skipped"},
        )
    commit = MagicMock(return_value={"committed": True, "output": "unexpected"})
    monkeypatch.setattr(vault_sync_module, "_vault_git_commit", commit)

    summary = sync_all_projections(
        vault_dir=tmp_path / "vault",
        raw_dir=tmp_path / "raw",
        config=fake_config,
    )

    assert summary["status"] == "error"
    assert summary["git"] == {"committed": False, "output": "skipped: projection error"}
    commit.assert_not_called()
