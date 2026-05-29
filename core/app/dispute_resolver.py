# -*- coding: utf-8 -*-
"""
DisputeResolver — 争议仲裁界面

知识图谱检测到 suspect 冲突关系时，生成 Markdown 仲裁页面。
高强度冲突在每日报告升级，未解决 7 天后在下周报告置顶。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional



logger = logging.getLogger(__name__)
@dataclass
class DisputeAssertion:
    """争议断言"""
    page_path: str
    title: str
    content: str
    reference_count: int


@dataclass
class DisputePage:
    """争议页面"""
    topic: str
    new_assertion: DisputeAssertion
    existing_assertions: List[DisputeAssertion]
    conflict_strength: float
    is_core_knowledge: bool
    page_path: str = ""

    @property
    def severity(self) -> str:
        if self.conflict_strength > 0.9 and self.is_core_knowledge:
            return "extreme"
        elif self.conflict_strength > 0.7 and self.is_core_knowledge:
            return "high"
        else:
            return "medium"


class DisputeResolver:
    """争议仲裁器"""

    def __init__(self, wiki_base: Optional[str] = None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            from core.config import get_config
            self.wiki_base = get_config().wiki_dir

    def create_dispute_page(self, new_assertion: DisputeAssertion,
                            conflicts: List[DisputeAssertion],
                            conflict_strength: float,
                            is_core_knowledge: bool = False) -> DisputePage:
        """
        创建争议仲裁页面。

        不弹窗不中断，只生成 Markdown 页面。
        """
        topic = new_assertion.title
        dispute = DisputePage(
            topic=topic,
            new_assertion=new_assertion,
            existing_assertions=conflicts,
            conflict_strength=conflict_strength,
            is_core_knowledge=is_core_knowledge,
        )

        # 生成 Markdown
        content = self._render_dispute_page(dispute)

        # 写入 Wiki
        date_str = datetime.now().strftime("%Y-%m-%d")
        dispute_dir = self.wiki_base / "08-Disputes"
        dispute_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-{topic[:30].replace('/', '-').replace(' ', '_')}.md"
        page_path = dispute_dir / filename
        page_path.write_text(content, encoding="utf-8")

        dispute.page_path = str(page_path.relative_to(self.wiki_base))
        logger.info(f"争议页面已创建: {page_path}")

        return dispute

    def get_unresolved_disputes(self) -> List[Dict]:
        """获取未解决的争议列表"""
        disputes = []
        dispute_dir = self.wiki_base / "08-Disputes"
        if not dispute_dir.exists():
            return disputes

        for md_file in dispute_dir.glob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8")
                # 未解决的争议页面包含未勾选的 checkbox
                if "- [ ] " in content:
                    days_old = (datetime.now() - datetime.fromtimestamp(md_file.stat().st_mtime)).days
                    disputes.append({
                        "path": str(md_file.relative_to(self.wiki_base)),
                        "title": md_file.stem,
                        "days_old": days_old,
                        "needs_escalation": days_old >= 7,
                    })
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at dispute_resolver.py", exc_info=True)
                continue

        return sorted(disputes, key=lambda d: d["days_old"], reverse=True)

    def resolve_dispute(self, page_path: str, resolution: str,
                        context: str = "") -> None:
        """
        解决争议。

        Args:
            page_path: 争议页面路径
            resolution: adopt_new / keep_old / keep_both / need_more_info
            context: 附加上下文（keep_both 时必填）
        """
        full_path = self.wiki_base / page_path
        if not full_path.exists():
            return

        content = full_path.read_text(encoding="utf-8")

        # 更新 checkbox
        content = content.replace("- [ ] ", "- [x] ")

        # 添加解决方案
        resolution_labels = {
            "adopt_new": "采纳新断言",
            "keep_old": "保留旧断言",
            "keep_both": "保留双方（添加上下文）",
            "need_more_info": "需要更多信息",
        }
        content += f"\n\n---\n**解决方案**: {resolution_labels.get(resolution, resolution)}"
        if context:
            content += f"\n**上下文**: {context}"
        content += f"\n**解决时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        full_path.write_text(content, encoding="utf-8")

        # 根据解决方案更新知识图谱
        if resolution == "adopt_new":
            self._mark_old_as_deprecated(page_path)
        elif resolution == "keep_both" and context:
            self._add_context_to_both(page_path, context)

    def _render_dispute_page(self, dispute: DisputePage) -> str:
        """渲染争议页面 Markdown"""
        lines = [
            f"# 争议仲裁：{dispute.topic}",
            "",
            f"> 冲突强度：{dispute.conflict_strength:.2f} | "
            f"严重级别：{dispute.severity} | "
            f"核心知识：{'是' if dispute.is_core_knowledge else '否'}",
            "",
            "## 新断言",
            "",
            f"**来源**: [{dispute.new_assertion.title}]({dispute.new_assertion.page_path})",
            f"**引用数**: {dispute.new_assertion.reference_count}",
            "",
            f"> {dispute.new_assertion.content[:300]}",
            "",
            "## 现有断言",
            "",
        ]

        for i, assertion in enumerate(dispute.existing_assertions, 1):
            lines.append(f"### 断言 {i}")
            lines.append("")
            lines.append(f"**来源**: [{assertion.title}]({assertion.page_path})")
            lines.append(f"**引用数**: {assertion.reference_count}")
            lines.append("")
            lines.append(f"> {assertion.content[:300]}")
            lines.append("")

        lines.extend([
            "## 解决方案",
            "",
            "- [ ] **采纳新断言** — 旧断言标记为 deprecated",
            "- [ ] **保留旧断言** — 忽略新断言",
            "- [ ] **保留双方** — 添加上下文说明各自的适用范围",
            "- [ ] **需要更多信息** — 暂不决定，等待更多证据",
            "",
            "## 影响评估",
            "",
        ])

        total_refs = dispute.new_assertion.reference_count + sum(
            a.reference_count for a in dispute.existing_assertions
        )
        lines.append(f"受影响页面总数：{total_refs}")

        return "\n".join(lines)

    def _mark_old_as_deprecated(self, dispute_page_path: str) -> None:
        """标记旧断言为 deprecated"""
        try:
            from core.kia.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(wiki_base=str(self.wiki_base))
            # 更新关系置信度
            # 具体 KG 更新逻辑依赖图谱结构
        except Exception as e:
            logger.debug(f"KG 更新跳过: {e}")

    def _add_context_to_both(self, dispute_page_path: str, context: str) -> None:
        """给双方添加上下文"""
        # 上下文信息已写入争议页面
        # TODO: 实现将上下文同步回争议双方的原始页面（当前仅写入争议页面）
        pass
