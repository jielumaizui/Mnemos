"""P3 audit unit tests for core.cognitive_graph.updater."""

import pytest

from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater
from core.cognitive_graph.updater import _wiki_urn
from core.mnemos_bus import Event

USER_VAULT = "/" + "Users/user/Documents/mnemos"


def _vault_path(relative: str) -> str:
    return f"{USER_VAULT}/{relative}"


@pytest.fixture
def store(tmp_path):
    return CognitiveGraphStore(str(tmp_path / "cognitive_graph.db"))


@pytest.fixture
def updater(store):
    return CognitiveGraphUpdater(store=store)


# ---------------------------------------------------------------------------
# core/cognitive_graph/updater.py::_wiki_urn
# ---------------------------------------------------------------------------


class TestWikiUrn:
    def test_empty_path(self):
        assert _wiki_urn("") == ""

    def test_path_with_mnemos(self):
        assert (
            _wiki_urn(_vault_path("00-Inbox/python.md"))
            == "wiki://00-Inbox/python.md"
        )

    def test_path_without_mnemos(self):
        assert _wiki_urn("/tmp/random/page.md") == "wiki://page.md"


# ---------------------------------------------------------------------------
# core/cognitive_graph/updater.py::CognitiveGraphUpdater.on_wiki_page_updated
# ---------------------------------------------------------------------------


class TestOnWikiPageUpdated:
    def test_wiki_page_updated_does_not_add_self_relation(self, updater, store):
        """wiki_page_updated 不应再生成自引用边，避免图谱污染和遍历死循环。"""
        event = Event(
            event_type="wiki_page_updated",
            source="test",
            payload={"page_path": _vault_path("00-Inbox/python.md")},
        )
        updater.on_wiki_page_updated(event)

        rels = store.get_relations(relation_type="related_to")
        assert len(rels) == 0

    def test_empty_page_path_is_noop(self, updater, store):
        event = Event(
            event_type="wiki_page_updated",
            source="test",
            payload={"page_path": ""},
        )
        updater.on_wiki_page_updated(event)
        assert store.get_stats()["relations"] == 0
