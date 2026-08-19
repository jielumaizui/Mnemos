"""Test LinkProbeWorker production wiring."""

from unittest.mock import MagicMock, patch


class TestGetLinkProbeWorker:
    def test_disabled_by_default(self, tmp_path):
        from core.hephaestus.link_probe_worker import get_link_probe_worker

        cfg = {"features.enable_link_probe": False}
        assert get_link_probe_worker(cfg) is None

    def test_enabled_returns_worker(self, tmp_path):
        from core.hephaestus.link_probe_worker import get_link_probe_worker, LinkProbeWorker

        cfg = {
            "features.enable_link_probe": True,
            "database_dir": tmp_path,
        }
        worker = get_link_probe_worker(cfg)
        assert isinstance(worker, LinkProbeWorker)

    def test_init_failure_returns_none(self, tmp_path):
        from core.hephaestus.link_probe_worker import get_link_probe_worker

        cfg = {"features.enable_link_probe": True}
        with patch(
            "core.hephaestus.link_probe_worker.LinkProbeWorker", side_effect=RuntimeError("boom")
        ):
            assert get_link_probe_worker(cfg) is None


class TestPipelineWiring:
    def test_document_distillation_pipeline_passes_worker_when_enabled(self, tmp_path):
        from core.hephaestus.document_pipeline import DocumentDistillationPipeline
        from core.hephaestus.distillation_engine import DistillSelfCheck

        with patch("core.hephaestus.document_pipeline.get_link_probe_worker") as mock_get:
            mock_worker = MagicMock()
            mock_get.return_value = mock_worker
            pipeline = DocumentDistillationPipeline(wiki_base=str(tmp_path))
            assert isinstance(pipeline._self_check, DistillSelfCheck)
            assert pipeline._self_check._link_probe is mock_worker

    def test_distillation_engine_passes_worker_when_enabled(self, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine, DistillSelfCheck

        with patch("core.hephaestus.distillation_engine.get_link_probe_worker") as mock_get:
            mock_worker = MagicMock()
            mock_get.return_value = mock_worker
            engine = DistillationEngine(wiki_base=str(tmp_path))
            assert isinstance(engine._self_check, DistillSelfCheck)
            assert engine._self_check._link_probe is mock_worker


class TestCliLinkProbe:
    def test_cmd_link_probe_disabled(self, capsys):
        from core.cli.commands.link_probe import cmd_link_probe

        args = MagicMock()
        args.link_probe_cmd = "run"
        with patch(
            "core.cli.commands.link_probe._get_config",
            return_value={"features.enable_link_probe": False},
        ):
            rc = cmd_link_probe(args)
        assert rc == 1
        captured = capsys.readouterr()
        assert "未启用" in captured.out

    def test_cmd_link_probe_status_empty(self, capsys, tmp_path):
        from core.cli.commands.link_probe import cmd_link_probe

        args = MagicMock()
        args.link_probe_cmd = "status"
        cfg = {
            "features.enable_link_probe": True,
            "database_dir": tmp_path,
        }
        with patch("core.cli.commands.link_probe._get_config", return_value=cfg):
            rc = cmd_link_probe(args)
        assert rc == 0
        captured = capsys.readouterr()
        assert "链接探测队列" in captured.out


class TestDaemonService:
    def test_service_link_probe_disabled(self):
        from mnemos_daemon import service_link_probe

        cfg = {"features.enable_link_probe": False}
        result = service_link_probe(cfg)
        assert result["enabled"] is False
        assert result["probed"] == 0
        assert result["broken"] == 0
        assert result["updated"] == 0
        assert result["errors"] == 0
