"""P3 audit unit tests for core.persona.calibration_cli helpers."""

import json
import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from core.persona import calibration_cli
from core.persona.pythia import PreferenceProfile, EnergyProfile, CognitiveProfile, ValueProfile
from core.wiki_derived_projection import DerivedProjectionLifecycle
from core.wiki_projection_lifecycle import WikiProjectionLedger
from tests.persona_decision_fixtures import save_persona_version_authorized


class _RecordingBus:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)
        return event.trace_id


@pytest.fixture
def profile():
    return PreferenceProfile(
        version=1,
        energy=EnergyProfile(
            focus_depth=0.6,
            startup_difficulty=0.4,
            endurance_mode=0.5,
            switching_flexibility=0.7,
            recovery_cycle=0.5,
        ),
        cognitive=CognitiveProfile(),
        value=ValueProfile(),
    )


# ---------------------------------------------------------------------------
# core/persona/calibration_cli.py::_calibrate_layer
# ---------------------------------------------------------------------------


class TestCalibrateLayer:
    def test_calibrate_layer_skips_and_rates(self, monkeypatch, profile, caplog):
        calibration = {"ratings": {}, "comments": {}}

        # energy layer has 5 dimensions; rating 4 triggers a comment prompt
        inputs = iter(["", "4", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))

        with caplog.at_level("INFO", logger="core.persona.calibration_cli"):
            calibration_cli._calibrate_layer("能量模式", "energy", profile, calibration)

        assert calibration["ratings"]["focus_depth"] is None
        assert calibration["ratings"]["startup_difficulty"] == 4
        assert calibration["ratings"]["endurance_mode"] is None


# ---------------------------------------------------------------------------
# core/persona/calibration_cli.py::_save_calibration
# ---------------------------------------------------------------------------


class TestSaveCalibration:
    def test_save_calibration_propagates_canonical_failure(
        self,
        tmp_path,
        monkeypatch,
        profile,
    ):
        class FailingSignalStore:
            db_path = tmp_path / "signals.db"

            def prepare_persona_calibration_material_action(self, **_kwargs):
                return object()

            def record_persona_calibration(self, **_kwargs):
                raise RuntimeError("calibration commit failed")

        store = SimpleNamespace(
            persona_page=tmp_path / "missing-persona.md",
            signal_store=FailingSignalStore(),
        )
        monkeypatch.setattr(
            calibration_cli,
            "get_config",
            lambda: SimpleNamespace(data_dir=tmp_path / "data"),
        )

        with pytest.raises(RuntimeError, match="calibration commit failed"):
            calibration_cli._save_calibration(
                store,
                profile,
                {
                    "version": profile.version,
                    "calibrated_at": "2026-01-01T00:00:00+00:00",
                    "ratings": {"focus_depth": 5},
                    "comments": {},
                },
            )

    def test_save_calibration_updates_files(
        self,
        tmp_path,
        monkeypatch,
        profile,
        caplog,
    ):
        calibration = {
            "version": profile.version,
            "calibrated_at": "2026-01-01T00:00:00+00:00",
            "ratings": {
                "focus_depth": 5,
                "startup_difficulty": 3,
            },
            "comments": {},
        }

        persona_page = tmp_path / "L5-Feedback" / "user-persona.md"
        persona_page.parent.mkdir(parents=True, exist_ok=True)
        persona_page.write_text("---\nversion: 1\n---\n# Persona\n", encoding="utf-8")

        db_path = tmp_path / "signals.db"
        from core.persona.delphi import PersonaStore
        from core.persona.psyche import SignalStore

        signal_store = SignalStore(initialize_schema=True, db_path=db_path)
        save_persona_version_authorized(
            signal_store,
            version=profile.version,
            period_start="2026-01-01",
            period_end="2026-01-01",
            energy=asdict(profile.energy),
            cognitive=asdict(profile.cognitive),
            value=asdict(profile.value),
            blindspot={},
            signal_count=profile.signal_count,
            generated_at="2026-01-01T00:00:00+00:00",
        )

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        values = {
            "trusted_push.mode": "off",
            "trusted_push.db_path": str(tmp_path / "trusted_push.db"),
        }
        fake_config = SimpleNamespace(
            data_dir=data_dir,
            database_dir=tmp_path,
            get=lambda key, default=None: values.get(key, default),
        )
        monkeypatch.setattr(calibration_cli, "get_config", lambda: fake_config)
        monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
        ledger = WikiProjectionLedger(tmp_path / "wiki_projection.db")
        lifecycle = DerivedProjectionLifecycle(
            tmp_path,
            ledger=ledger,
            event_bus=_RecordingBus(),
        )
        store = PersonaStore(
            wiki_dir=tmp_path,
            signal_store=signal_store,
            projection_lifecycle=lifecycle,
        )

        with caplog.at_level("INFO", logger="core.persona.calibration_cli"):
            calibration_cli._save_calibration(store, profile, calibration)

        updated = persona_page.read_text(encoding="utf-8")
        assert "calibration_score" in updated
        binding = lifecycle.binding_for_path(persona_page)
        assert binding is not None
        assert binding["status"] == "published"
        assert binding["event_trace_id"]

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                """
                SELECT user_confirmed, calibration_score
                FROM persona_revisions
                WHERE version = ?
                """,
                (profile.version + 1,),
            ).fetchone()
            assert row[0] == 1
            assert row[1] == 4.0
            assert conn.execute("SELECT COUNT(*) FROM persona_versions").fetchone()[0] == 0

        calib_files = list((data_dir / "calibrations").glob("*.json"))
        assert len(calib_files) == 1
        saved = json.loads(calib_files[0].read_text(encoding="utf-8"))
        assert saved["ratings"]["focus_depth"] == 5
        signal_store.close()


# ---------------------------------------------------------------------------
# core/persona/calibration_cli.py::_print_calibration_report
# ---------------------------------------------------------------------------


class TestPrintCalibrationReport:
    def test_report(self, caplog):
        calibration = {
            "ratings": {
                "focus_depth": 2,
                "startup_difficulty": 3,
            },
            "comments": {"focus_depth": "感觉不太准"},
        }

        with caplog.at_level("INFO", logger="core.persona.calibration_cli"):
            calibration_cli._print_calibration_report(calibration)

        assert "校准报告" in caplog.text
        assert "focus_depth: 2/5" in caplog.text
        assert "感觉不太准" in caplog.text

    def test_report_no_ratings(self, caplog):
        calibration = {"ratings": {}}

        with caplog.at_level("INFO", logger="core.persona.calibration_cli"):
            calibration_cli._print_calibration_report(calibration)

        assert "未收集到有效评分" in caplog.text


# ---------------------------------------------------------------------------
# core/persona/calibration_cli.py::_get_label_for_score
# ---------------------------------------------------------------------------


class TestGetLabelForScore:
    def test_three_labels_low(self):
        labels = ["低", "中", "高"]
        assert calibration_cli._get_label_for_score(0.0, labels) == "低"
        assert calibration_cli._get_label_for_score(0.39, labels) == "低"

    def test_three_labels_mid(self):
        labels = ["低", "中", "高"]
        assert calibration_cli._get_label_for_score(0.4, labels) == "中"
        assert calibration_cli._get_label_for_score(0.6, labels) == "中"

    def test_three_labels_high(self):
        labels = ["低", "中", "高"]
        assert calibration_cli._get_label_for_score(0.61, labels) == "高"
        assert calibration_cli._get_label_for_score(1.0, labels) == "高"

    def test_four_labels(self):
        labels = ["A", "B", "C", "D"]
        assert calibration_cli._get_label_for_score(0.0, labels) == "A"
        assert calibration_cli._get_label_for_score(0.29, labels) == "A"
        assert calibration_cli._get_label_for_score(0.3, labels) == "B"
        assert calibration_cli._get_label_for_score(0.49, labels) == "B"
        assert calibration_cli._get_label_for_score(0.5, labels) == "C"
        assert calibration_cli._get_label_for_score(0.69, labels) == "C"
        assert calibration_cli._get_label_for_score(0.7, labels) == "D"
        assert calibration_cli._get_label_for_score(0.9, labels) == "D"

    def test_fallback_middle(self):
        labels = ["一", "二", "三", "四", "五"]
        assert calibration_cli._get_label_for_score(0.5, labels) == "三"
