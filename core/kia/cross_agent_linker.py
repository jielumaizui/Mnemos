"""
CrossAgentLinker — 跨 Agent 知识关联器

【E14 全库修复】蒸馏生成新页面后，检测其他 Agent 是否有相似主题页面，
在原页面添加双向链接，不生成合成页。

设计来源：08-功能梳理/34-跨Agent知识关联.md
ADR-019: 优先 hnswlib 双索引融合检索，DNAEngine/SimHash 作为兼容降级。
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config import get_config
from core.kia.genos import DNAEngine

logger = logging.getLogger(__name__)


@dataclass
class LinkAction:
    """链接操作"""
    from_page: Path
    to_page: Path
    reason: str
    similarity: float


class CrossAgentLinker:
    """
    跨 Agent 知识关联器

    在蒸馏生成新页面后，检测其他 Agent 是否有相似主题页面，
    在原页面添加双向链接，不生成合成页。
    """

    SIMILARITY_THRESHOLD = 0.30
    MAX_LINKS_PER_PAGE = 3
    WORKSPACE_NAMES = ["claude", "hermes", "shared"]

    def __init__(self, wiki_root: Path = None, vector_index=None):
        self.wiki_root = wiki_root or get_config().wiki_dir
        self.vector_index = vector_index
        self.dna = DNAEngine()

    # ── 主入口 ──

    def link_after_distill(self, new_page_path: Path) -> List[LinkAction]:
        """
        蒸馏完成后调用：为新页面找跨 Agent 关联

        Args:
            new_page_path: 新蒸馏生成的页面路径

        Returns:
            执行的链接操作列表
        """
        new_agent = self._extract_agent_from_path(new_page_path)
        if not new_agent:
            logger.debug(f"无法识别页面 agent 来源: {new_page_path}")
            return []

        similar = self._find_cross_workspace_similar(new_page_path, new_agent)

        links = []
        for sim_page, score in similar[:self.MAX_LINKS_PER_PAGE]:
            other_agent = self._extract_agent_from_path(sim_page)
            if other_agent and other_agent != new_agent:
                if not self._link_exists(new_page_path, sim_page):
                    links.append(LinkAction(
                        from_page=new_page_path,
                        to_page=sim_page,
                        reason=f"跨Agent关联（{new_agent} ↔ {other_agent}）",
                        similarity=score,
                    ))
                if not self._link_exists(sim_page, new_page_path):
                    links.append(LinkAction(
                        from_page=sim_page,
                        to_page=new_page_path,
                        reason=f"跨Agent关联（{other_agent} ↔ {new_agent}）",
                        similarity=score,
                    ))

        for action in links:
            self._append_link(action)

        if links:
            logger.info(f"跨Agent关联: {new_page_path.name} ↔ {len(links)//2} 个页面")

        return links

    # ── 内部方法 ──

    def _find_cross_workspace_similar(self, page_path: Path,
                                      exclude_agent: str) -> List[Tuple[Path, float]]:
        """跨 workspace 查找相似页面"""
        results = []

        # 方案 A：优先使用向量索引（ADR-019）
        if self.vector_index and hasattr(self.vector_index, 'hybrid_search'):
            try:
                query = page_path.read_text(encoding="utf-8")[:2000]
                search_results = self.vector_index.hybrid_search(
                    query=query,
                    top_k=self.MAX_LINKS_PER_PAGE * 3,
                )
                for r in search_results:
                    md_file = Path(r["path"])
                    if md_file.exists() and md_file.name != page_path.name:
                        other_agent = self._extract_agent_from_path(md_file)
                        if other_agent and other_agent != exclude_agent:
                            results.append((md_file, r["score"]))
            except Exception:
                pass

        # 方案 B：DNAEngine/SimHash 兼容降级
        if not results:
            source_dna = self.dna.compute_dna(page_path)
            if not source_dna:
                return []
            for ws in self.WORKSPACE_NAMES:
                if ws == exclude_agent:
                    continue
                ws_path = self.wiki_root / ws
                if not ws_path.exists():
                    continue
                for md_file in ws_path.rglob("*.md"):
                    if md_file == page_path:
                        continue
                    try:
                        target_dna = self.dna.compute_dna(md_file)
                        if not target_dna:
                            continue
                        sim = self.dna.compare(source_dna, target_dna)
                        if sim.overall_score >= self.SIMILARITY_THRESHOLD:
                            results.append((md_file, sim.overall_score))
                    except Exception:
                        continue

        seen = set()
        unique_results = []
        for md_file, score in sorted(results, key=lambda x: x[1], reverse=True):
            key = str(md_file)
            if key not in seen:
                seen.add(key)
                unique_results.append((md_file, score))

        return unique_results[:self.MAX_LINKS_PER_PAGE * 3]

    def _extract_agent_from_path(self, page_path: Path) -> Optional[str]:
        """从页面路径提取 agent 来源"""
        try:
            rel = page_path.relative_to(self.wiki_root)
            first_part = rel.parts[0].lower()
            if first_part in self.WORKSPACE_NAMES:
                return first_part
        except ValueError:
            pass

        # frontmatter 推断
        try:
            fm = self._read_frontmatter(page_path)
            agent = fm.get("source_agent")
            if agent:
                return agent
        except Exception:
            pass

        # 文件名推断
        stem = page_path.stem.lower()
        for known in ["claude", "hermes", "kimi", "codex", "gpt"]:
            if known in stem:
                return known

        return None

    def _link_exists(self, from_page: Path, to_page: Path) -> bool:
        """检查链接是否已存在"""
        if not from_page.exists():
            return False
        content = from_page.read_text(encoding="utf-8")
        rel_link = self._make_relative_link(from_page, to_page)
        abs_link = str(to_page)
        return rel_link in content or abs_link in content

    def _append_link(self, action: LinkAction):
        """在页面末尾添加链接"""
        content = action.from_page.read_text(encoding="utf-8")
        rel_link = self._make_relative_link(action.from_page, action.to_page)
        link_line = f"- {rel_link} <!-- 跨Agent关联: {action.reason} -->\n"

        if "## 相关链接" in content:
            content = content.replace("## 相关链接", f"## 相关链接\n{link_line}")
        else:
            content = content.rstrip() + f"\n\n## 相关链接\n{link_line}"

        action.from_page.write_text(content, encoding="utf-8")

    def _make_relative_link(self, from_page: Path, to_page: Path) -> str:
        """生成 Obsidian 兼容的相对链接"""
        # 尝试从共同根目录（wiki_root）生成相对路径
        try:
            rel = to_page.relative_to(self.wiki_root)
            return f"[[{rel.with_suffix('').as_posix()}]]"
        except ValueError:
            pass
        # 回退：从源文件所在目录生成
        try:
            rel = to_page.relative_to(from_page.parent)
            return f"[[{rel.with_suffix('').as_posix()}]]"
        except ValueError:
            return f"[[{to_page.stem}]]"

    def _read_frontmatter(self, page_path: Path) -> Dict:
        """读取页面 frontmatter"""
        content = page_path.read_text(encoding="utf-8")
        fm = {}
        if content.startswith("---"):
            _, yaml_block, _ = content.split("---", 2)
            for line in yaml_block.strip().split("\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
        return fm
