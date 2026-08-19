"""
InProcessGuard embedding client health_check 缓存测试
"""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp


from core.kia.aegis import InProcessGuard


class _FakeConfig:
    """测试用配置替身，同时满足 dict 访问和 attribute 访问。"""

    def __init__(self):
        self._tmpdir = mkdtemp()
        self.database_dir = Path(self._tmpdir)
        self._values = {"embedding.enabled": True}

    def get(self, key, default=None):
        return self._values.get(key, default)

    def __getitem__(self, key):
        return self._values[key]


class CountingClient:
    """记录 health_check 调用次数的 fake client"""

    def __init__(self, available=True):
        self.available = available
        self.health_check_calls = 0

    def health_check(self):
        self.health_check_calls += 1
        return {"available": self.available}


def test_health_check_cached_within_ttl(monkeypatch):
    """TTL 内多次创建 Guard，health_check 只应调用一次"""
    client = CountingClient(available=True)

    # 强制配置启用 embedding
    fake_cfg = _FakeConfig()
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: fake_cfg,
    )
    monkeypatch.setattr(
        "core.embeddings.siliconflow_client.get_embedding_client",
        lambda: client,
    )
    # 清空缓存
    import core.kia.aegis as aegis_mod

    aegis_mod._EMBEDDING_CLIENT_CHECK = (None, datetime.min)

    g1 = InProcessGuard()
    g2 = InProcessGuard()
    g3 = InProcessGuard()

    assert g1.embedding_client is client
    assert g2.embedding_client is client
    assert g3.embedding_client is client
    assert client.health_check_calls == 1


def test_health_check_refreshed_after_ttl(monkeypatch):
    """缓存过期后再次创建 Guard，应重新调用 health_check"""
    client = CountingClient(available=True)

    fake_cfg = _FakeConfig()
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: fake_cfg,
    )
    monkeypatch.setattr(
        "core.embeddings.siliconflow_client.get_embedding_client",
        lambda: client,
    )

    import core.kia.aegis as aegis_mod

    aegis_mod._EMBEDDING_CLIENT_CHECK = (None, datetime.min)

    InProcessGuard()
    assert client.health_check_calls == 1

    # 模拟缓存过期
    aegis_mod._EMBEDDING_CLIENT_CHECK = (
        client,
        datetime.now() - timedelta(seconds=aegis_mod._EMBEDDING_CLIENT_CHECK_TTL_SECONDS + 1),
    )

    g2 = InProcessGuard()
    assert g2.embedding_client is client
    assert client.health_check_calls == 2


def test_unavailable_client_not_cached(monkeypatch):
    """health_check 不可用时不应缓存可用结果"""
    client = CountingClient(available=False)

    fake_cfg = _FakeConfig()
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: fake_cfg,
    )
    monkeypatch.setattr(
        "core.embeddings.siliconflow_client.get_embedding_client",
        lambda: client,
    )

    import core.kia.aegis as aegis_mod

    aegis_mod._EMBEDDING_CLIENT_CHECK = (None, datetime.min)

    g = InProcessGuard()
    assert g.embedding_client is None
    assert client.health_check_calls == 1
