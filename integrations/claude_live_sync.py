"""
ClaudeLiveSync — Claude 实时同步

【E14 全库修复】设计草案占位模块。
负责实时监控 Claude session 文件变化并同步到 Memos。
实际同步逻辑由 core/sync_framework/sync_engine.py 实现。
"""
from typing import Optional, Dict


class ClaudeLiveSync:
    """Claude 实时同步入口（设计草案，待完善）"""

    def __init__(self):
        pass

    def start_watching(self):
        """启动文件监控"""
        pass

    def sync_now(self) -> Dict:
        """立即执行同步"""
        return {"synced": 0, "skipped": 0}
