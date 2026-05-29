"""
性能基准测试框架

运行: python -m pytest tests/benchmark/ --benchmark-only
依赖: pip install pytest-benchmark

目标 P95:
- SyncEngine.sync_session: < 500ms
- KnowledgeGraph.search: < 200ms
- ShadowPageManager.batch_sync: < 5s (全 Vault 扫描)
- chronos 单步执行: < 1s
"""
