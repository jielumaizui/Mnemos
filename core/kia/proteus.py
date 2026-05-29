# -*- coding: utf-8 -*-
"""
proteus — 知识演化兼容层

原 proteus 模块已按蓝图迁移，此文件保留向后兼容的公开接口。
KnowledgeFreshnessChecker 实际实现位于 core.app.knowledge_freshness_checker。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class FreshnessAlert:
    """新鲜度告警结果"""
    type: str
    severity: str
    message: str = ""


class KnowledgeFreshnessChecker:
    """知识新鲜度检查器（兼容层）

    委托给 core.app.knowledge_freshness_checker.KnowledgeFreshnessChecker
    并扩展蓝图要求的 check() 接口。
    """

    def __init__(self, half_life_days: int = 30):
        self.half_life_days = half_life_days

    def check(self, page: Dict) -> Optional[FreshnessAlert]:
        """检查页面新鲜度，返回告警或 None"""
        fm = page.get("frontmatter", {})

        # 1.  timeless 页面跳过
        temporal_scope = fm.get("temporal_scope", "")
        if temporal_scope == "timeless" or fm.get("时效性") == " timeless":
            return None

        # 2. 版本过期检查
        version_info = fm.get("version_info") or fm.get("版本")
        latest_version = fm.get("latest_version")
        if version_info and latest_version and version_info != latest_version:
            return FreshnessAlert(
                type="newer_version",
                severity="high",
                message=f"当前版本 {version_info}，最新版本 {latest_version}",
            )

        # 3. 内容 stale 检查
        modified_raw = fm.get("修改日期") or fm.get("last_modified")
        if modified_raw:
            try:
                if isinstance(modified_raw, str):
                    modified = datetime.strptime(modified_raw, "%Y-%m-%d")
                elif hasattr(modified_raw, "year"):  # datetime/date 对象
                    modified = datetime(modified_raw.year, modified_raw.month, modified_raw.day)
                else:
                    modified = None
                if modified:
                    age_days = (datetime.now() - modified).days
                    if age_days > self.half_life_days * 2:  # 2 个半衰期视为 stale
                        return FreshnessAlert(
                            type="potentially_stale",
                            severity="medium",
                            message=f"内容已 {age_days} 天未更新，可能过时",
                        )
            except (ValueError, TypeError):
                pass

        return None


class IterationTracker:
    """迭代追踪器（骨架）

    TODO: 蓝图中要求的迭代质量门控逻辑待完整实现。
    当前仅暴露测试所需的常量接口和最小 get_stats() 实现。
    """

    MIN_CHECKLIST_DELTA_RATIO = 0.1
    MAX_VERSIONS_PER_DAY = 5

    def __init__(self):
        self._iterations = []

    def record_iteration(self, page_path: str, delta: dict = None) -> None:
        """记录一次迭代"""
        self._iterations.append({
            "page_path": page_path,
            "delta": delta or {},
            "timestamp": datetime.now().isoformat(),
        })

    def get_stats(self) -> dict:
        """返回迭代统计"""
        return {
            "total": len(self._iterations),
            "pages": len({i["page_path"] for i in self._iterations}),
        }
