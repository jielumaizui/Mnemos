"""
ContextAssembler — 上下文组装器

【E14 全库修复】设计草案占位模块。
负责组装 LLM prompt 所需的上下文。
"""
from typing import List, Dict


class ContextAssembler:
    """上下文组装（设计草案，待完善）"""

    def assemble(self, sources: List[Dict]) -> str:
        """组装上下文文本"""
        return "\n\n".join(str(s) for s in sources)
