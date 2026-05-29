# -*- coding: utf-8 -*-
"""
aporia — 可证伪性标记器（骨架）

原 aporia 模块已按蓝图合并到 ShadowPage / 争议解决流程。
此文件保留向后兼容的公开接口，供 orchestrator.run_falsify() 调用。

TODO: 完整实现可证伪性标记生命周期：
  1. 为 Wiki 页面初始化 falsifiability mark（可测试假设列表）
  2. 定期扫描 marks，触发争议检测或实验验证
  3. 将验证结果回写到页面 frontmatter
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class FalsifiabilityMark:
    """可证伪性标记记录"""

    def __init__(self, page_path: str, hypotheses: List[str] = None):
        self.page_path = page_path
        self.hypotheses = hypotheses or []
        self.created_at = ""
        self.status = "pending"  # pending / tested / confirmed / refuted


class FalsifiabilityMarker:
    """可证伪性标记器（骨架实现）"""

    def __init__(self, wiki_base: str = ""):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else Path.home()
        self._marks: Dict[str, FalsifiabilityMark] = {}

    def get_mark(self, page_path: str) -> Optional[FalsifiabilityMark]:
        """获取页面的可证伪性标记"""
        return self._marks.get(page_path)

    def init_mark_for_page(self, page: Path) -> Optional[FalsifiabilityMark]:
        """为页面初始化可证伪性标记"""
        page_path = str(page)
        if page_path in self._marks:
            return self._marks[page_path]

        # 骨架：从页面 frontmatter 提取假设（简单启发式）
        hypotheses = []
        try:
            content = page.read_text(encoding="utf-8", errors="ignore")
            # 简单启发：包含 "假设"、"断言"、"如果...那么" 的行
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("假设：") or line.startswith("断言："):
                    hypotheses.append(line)
                elif "如果" in line and "那么" in line and len(line) < 200:
                    hypotheses.append(line)
        except Exception:
            pass

        if not hypotheses:
            return None

        mark = FalsifiabilityMark(page_path, hypotheses)
        self._marks[page_path] = mark
        logger.debug(f"[Falsifiability] 为 {page_path} 创建 {len(hypotheses)} 条假设")
        return mark

    def scan_all_marks(self) -> List[FalsifiabilityMark]:
        """扫描所有待测试的标记"""
        return [m for m in self._marks.values() if m.status == "pending"]
