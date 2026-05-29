def test_profile_manager_recover_credit_persists():
    from core.persona.hamartia import BlindSpotProfileManager

    class FakeStore:
        def __init__(self):
            self.saved = None

        def get_latest_persona_version(self):
            return {
                "blindspot_profile": {
                    "challenge_credit": 4.0,
                    "credit_max": 5.0,
                    "credit_recovery_rate": 0.75,
                }
            }

        def update_blindspot_profile(self, data):
            self.saved = data

    store = FakeStore()
    manager = BlindSpotProfileManager(store=store)

    credit = manager.recover_credit()

    assert credit == 4.75
    assert store.saved["challenge_credit"] == 4.75


def test_blindspot_discovery_handles_credit_recovery_event(monkeypatch, tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery
    import core.persona.hamartia as hamartia

    class FakeManager:
        def recover_credit(self):
            return 8.5

    monkeypatch.setattr(hamartia, "BlindSpotProfileManager", FakeManager)

    discovery = BlindspotDiscovery(wiki_base=str(tmp_path), db_path=str(tmp_path / "blindspots.db"))
    result = discovery.handle_event("daily_credit_recovery")

    assert result == {
        "status": "ok",
        "event_type": "daily_credit_recovery",
        "challenge_credit": 8.5,
    }


def test_blindspot_discovery_ignores_unrelated_event(tmp_path):
    from core.app.blindspot_discovery import BlindspotDiscovery

    discovery = BlindspotDiscovery(wiki_base=str(tmp_path), db_path=str(tmp_path / "blindspots.db"))

    assert discovery.handle_event("something_else") == {
        "status": "ignored",
        "event_type": "something_else",
    }
