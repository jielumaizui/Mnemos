"""P3 audit unit tests for core.persona.pythia private helpers."""

from unittest.mock import MagicMock


from core.persona.pythia import (
    PreferenceAnalyzer,
    EnergyProfile,
    CognitiveProfile,
    ValueProfile,
    PreferenceProfile,
)

# ---------------------------------------------------------------------------
# core/persona/pythia.py::PreferenceAnalyzer._calculate_changes.calc_change
# ---------------------------------------------------------------------------


class TestCalculateChanges:
    def test_calc_change_stable(self):
        analyzer = PreferenceAnalyzer(store=MagicMock())
        prev = PreferenceProfile(
            version=1,
            energy=EnergyProfile(focus_depth=0.5),
            cognitive=CognitiveProfile(),
            value=ValueProfile(),
        )
        energy = EnergyProfile(focus_depth=0.54)
        cognitive = CognitiveProfile()
        value = ValueProfile()
        analyzer._calculate_changes(energy, cognitive, value, prev)
        assert energy._changes["focus_depth"] == "stable"

    def test_calc_change_significant_up(self):
        analyzer = PreferenceAnalyzer(store=MagicMock())
        prev = PreferenceProfile(
            version=1,
            energy=EnergyProfile(focus_depth=0.5),
            cognitive=CognitiveProfile(),
            value=ValueProfile(),
        )
        energy = EnergyProfile(focus_depth=0.7)
        cognitive = CognitiveProfile()
        value = ValueProfile()
        analyzer._calculate_changes(energy, cognitive, value, prev)
        assert energy._changes["focus_depth"] == "up_significant"

    def test_calc_change_major_down(self):
        analyzer = PreferenceAnalyzer(store=MagicMock())
        prev = PreferenceProfile(
            version=1,
            energy=EnergyProfile(focus_depth=0.8),
            cognitive=CognitiveProfile(),
            value=ValueProfile(),
        )
        energy = EnergyProfile(focus_depth=0.5)
        cognitive = CognitiveProfile()
        value = ValueProfile()
        analyzer._calculate_changes(energy, cognitive, value, prev)
        assert energy._changes["focus_depth"] == "down_major"


# ---------------------------------------------------------------------------
# core/persona/pythia.py::PreferenceAnalyzer._get_fs_signals
# ---------------------------------------------------------------------------


class TestGetFsSignals:
    def test_get_fs_signals(self):
        row = MagicMock()
        row.keys.return_value = ["id", "timestamp", "path"]
        row.__getitem__ = lambda self, key: {"id": 1, "timestamp": "2026-01-01", "path": "/tmp"}[  # noqa: Vulture - MagicMock row protocol.
            key
        ]
        cursor = MagicMock()
        cursor.fetchall.return_value = [row]
        conn = MagicMock()
        conn.execute.return_value = cursor
        pool = MagicMock()
        pool.get_conn.return_value = conn
        store = MagicMock()
        store._pool = pool

        analyzer = PreferenceAnalyzer(store=store)
        signals = analyzer._get_fs_signals(days=30)

        assert isinstance(signals, list)
        conn.execute.assert_called_once()
        args = conn.execute.call_args[0][1]
        assert args == ("-30 days",)

    def test_get_fs_signals_error_returns_empty(self):
        import sqlite3

        store = MagicMock()
        store._pool.get_conn.side_effect = sqlite3.Error("boom")

        analyzer = PreferenceAnalyzer(store=store)
        signals = analyzer._get_fs_signals(days=30)

        assert signals == []
