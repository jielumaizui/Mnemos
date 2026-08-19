"""
Shadow Page - 知识影子页面系统

为每个 Wiki 页面创建"影子"，通过联网搜索获取外部相关信息：
- 官方文档/博客的最新版本
- 社区讨论（StackOverflow、GitHub Issues、Reddit）
- 中文社区内容（知乎、掘金、微信公众号）
- 最新动态和新闻

搜索引擎：
- tavily-search: 主引擎，通用网络搜索
- fallback_search: 可选回退，由宿主 agent 注入（如 SearchWeb 等自有搜索工具）

设计原则：
- 影子页面独立存储，不污染主页面
- 搜索可手动触发、自动触发或定期刷新
- 查询基于页面标题+关键词自动生成，无需人工编写
- 结果自动分类整理，支持快速浏览
"""

# Hecate — 幽冥女神 — 影子页面，知识的对偶与镜像
# 原模块: shadow_page.py


import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from core.config import get_config
from core.vaults.naming import shadow_projection_stem
import logging

# Constants extracted from magic numbers
OUTPUT_SECONDS = 30

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class SearchResult:
    """搜索结果"""

    title: str
    url: str
    snippet: str = ""
    source: str = ""  # tavily / fallback
    category: str = "general"  # official / community / news / blog / social
    published_date: str = ""
    relevance_score: float = 0.0


@dataclass
class ShadowPage:
    """影子页面"""

    shadow_for: str  # 原页面路径
    search_date: str
    queries_used: List[str] = field(default_factory=list)
    sources: List[SearchResult] = field(default_factory=list)
    content: str = ""  # 生成的 Markdown 内容


class ShadowPageManager:
    """影子页面管理器"""

    SHADOW_DIR_NAME = "07-Shadow"

    # 来源域名分类
    CATEGORY_MAP = {
        "official": [
            "docs.",
            "documentation.",
            "developer.",
            "api.",
            "github.com",
            "gitlab.com",
            "apache.org",
            "python.org",
            "mozilla.org",
            "microsoft.com",
            "google.com",
            "cloud.google",
            "aws.amazon.com",
            "azure.microsoft.com",
            "kubernetes.io",
            "docker.com",
        ],
        "community": [
            "stackoverflow.com",
            "stackexchange.com",
            "reddit.com",
            "news.ycombinator.com",
            "discuss.",
            "forum.",
            "community.",
            "github.com/discussions",
        ],
        "blog": [
            "medium.com",
            "dev.to",
            "hashnode.com",
            "substack.com",
            "blog.",
            "juejin.cn",
            "segmentfault.com",
            "csdn.net",
        ],
        "social": [
            "zhihu.com",
            "xiaohongshu.com",
            "douyin.com",
            "weixin.qq.com",
            "mp.weixin.qq.com",
            "twitter.com",
            "x.com",
            "linkedin.com",
        ],
        "news": [
            "techcrunch.com",
            "theverge.com",
            "arstechnica.com",
            "36kr.com",
            "pingwest.com",
            "solidot.org",
        ],
    }

    def __init__(self, wiki_base: str | None = None, fallback_search=None):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            cfg_dir = get_config().wiki_dir
            self.wiki_base = Path(cfg_dir) if not isinstance(cfg_dir, Path) else cfg_dir
        self.shadow_dir = self.wiki_base / self.SHADOW_DIR_NAME
        self.excluded_dirs = {
            self.SHADOW_DIR_NAME,
            "00-Inbox",
            "99-Reports",
            ".git",
            ".obsidian",
            ".kg",
            "__pycache__",
            "L2.4-KG",
            "L3-Observations",
            "L4-Reflections",
            "L5-Feedback",
            "08-Reminders",
            "08-Disputes",
            "L3-Observations/immune",  # 知识免疫报告是系统内部报告，不需要外部搜索 shadow
            "06-Retrospectives/entropy",
        }
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self.fallback_search = fallback_search  # 可选: Callable[[str, int], List[SearchResult]]
        self._kg = None  # 延迟初始化 KnowledgeGraph
        if yaml is None:
            logger.warning(
                "PyYAML 未安装，frontmatter 提取功能将不可用。请执行: pip install pyyaml"
            )

    # ========== 查询生成 ==========

    def generate_queries(self, page_path: Path) -> List[str]:
        """
        基于 Wiki 页面内容生成搜索查询

        生成多个查询变体，覆盖不同角度
        """
        if not page_path.exists():
            return []

        try:
            content = page_path.read_text(encoding="utf-8")
        except (OSError, IOError) as e:
            logger.warning("读取页面失败 %s: %s", page_path, e)
            return []

        fm = self._extract_frontmatter(content)
        title = self._extract_title(content) or page_path.stem

        queries = []
        base_query = title.replace("#", "").strip()

        # 查询1：标题本身
        queries.append(base_query)

        # 查询2：标题 + 工具实体
        tools = self._get_keywords(fm, "工具实体")
        if tools:
            queries.append(f"{base_query} {' '.join(tools[:2])}")

        # 查询3：核心概念 + 最新
        concepts = self._get_keywords(fm, "核心概念")
        if concepts:
            queries.append(f"{' '.join(concepts[:2])} 最新进展 {datetime.now().year}")

        # 查询4：场景标签 + 最佳实践
        scenarios = self._get_keywords(fm, "场景标签")
        if scenarios:
            queries.append(f"{' '.join(scenarios[:2])} 最佳实践")

        # 查询5：针对中文社区
        queries.append(f"{base_query} 知乎 OR 掘金 OR CSDN")

        # 去重
        seen = set()
        unique = []
        for q in queries:
            q_norm = q.lower().strip()
            if q_norm not in seen:
                seen.add(q_norm)
                unique.append(q)

        return unique[:5]  # 最多 5 个查询

    # ========== 搜索执行 ==========

    def _try_fallback(self, query: str, max_results: int) -> List[SearchResult]:
        """当 Tavily 不可用时回退到宿主 agent 搜索工具。"""
        if self.fallback_search:
            logger.info("回退到宿主 agent 搜索工具")
            return self.fallback_search(query, max_results)  # type: ignore[no-any-return]
        return []

    def _get_kg(self):
        """延迟初始化 KnowledgeGraph。"""
        if self._kg is None:
            try:
                from core.kia.knowledge_graph import KnowledgeGraph

                self._kg = KnowledgeGraph()
            except (ImportError, OSError, RuntimeError, ValueError, TypeError, sqlite3.Error) as e:
                logger.warning("KnowledgeGraph 初始化失败: %s", e)
        return self._kg

    def _local_kg_search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """基于本地知识图谱的降级搜索：查找与查询词相关的实体和关系。"""
        kg = self._get_kg()
        if kg is None:
            return []
        connect = getattr(kg, "_conn", None)
        if not callable(connect):
            logger.warning("本地 KG 不满足连接接口，跳过降级搜索")
            return []

        try:
            with connect() as conn:
                # 1. 查找名称匹配的实体
                like = f"%{query}%"
                rows = conn.execute(
                    """SELECT uid, name, entity_type, source_page, quality_score, confidence
                       FROM entities
                       WHERE name LIKE ? OR source_page LIKE ?
                       ORDER BY quality_score * confidence DESC
                       LIMIT ?""",
                    (like, like, max_results * 2),
                ).fetchall()

                entity_names = {row["name"] for row in rows}
                if not entity_names:
                    return []

                # 2. 查找与这些实体相关的关系
                placeholders = ",".join("?" * len(entity_names))
                rel_rows = conn.execute(
                    f"""SELECT source, target, relation_type, strength, confidence, context
                        FROM relations
                        WHERE source IN ({placeholders}) OR target IN ({placeholders})
                        ORDER BY strength * confidence DESC
                        LIMIT ?""",  # nosec B608: internally generated ? placeholders
                    tuple(entity_names) * 2 + (max_results * 3,),
                ).fetchall()

            results = []
            seen = set()
            for row in rel_rows:
                other = row["target"] if row["source"] in entity_names else row["source"]
                key = (row["source"], row["target"], row["relation_type"])
                if key in seen:
                    continue
                seen.add(key)
                source_page = ""
                for er in rows:
                    if er["name"] == other:
                        source_page = er["source_page"] or ""
                        break
                url = (
                    source_page
                    if source_page and source_page.startswith("/")
                    else f"#local-{other}"
                )
                snippet = (
                    row["context"] or f"{row['source']} --{row['relation_type']}--> {row['target']}"
                )
                results.append(
                    SearchResult(
                        title=f"KG: {row['source']} → {row['target']}",
                        url=url,
                        snippet=snippet[:300],
                        source="local_kg",
                        category="community",
                        relevance_score=row["strength"] * row["confidence"],
                    )
                )
                if len(results) >= max_results:
                    break
            return results
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
            logger.warning("本地 KG 搜索失败 query=%s: %s", query, e)
            return []

    def _local_wiki_search(self, page_path: Path, max_results: int = 5) -> List[SearchResult]:
        """基于本地 Wiki 链接的降级搜索：找到相关页面和双向链接。"""
        try:
            content = page_path.read_text(encoding="utf-8")
            # 提取 [[Page Name]] 双向链接
            links = re.findall(r"\[\[([^\]]+)\]\]", content)
            results = []
            for link in links[:max_results]:
                link_path = self.wiki_base / f"{link}.md"
                if link_path.exists():
                    snippet = link_path.read_text(encoding="utf-8").replace("\n", " ")[:150]
                    results.append(
                        SearchResult(
                            title=f"Wiki: {link}",
                            url=str(link_path),
                            snippet=snippet,
                            source="local_wiki",
                            category="community",
                            relevance_score=0.7,
                        )
                    )
            return results
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("本地 Wiki 搜索失败 %s: %s", page_path, e)
            return []

    def search_tavily(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """
        使用 tavily-search 进行搜索

        需要 tvly CLI 已安装并登录。
        当 Tavily 不可用时，自动调用 fallback_search（如宿主 agent 注入的搜索工具）。
        """
        if not shutil.which("tvly"):
            logger.warning("tvly CLI 未安装")
            return self._try_fallback(query, max_results)

        try:
            safe_max = max(1, min(max_results, 20))  # S22: clamp to [1, 20]
            cmd = [
                "tvly",
                "search",
                "--max-results",
                str(safe_max),
                "--json",
                "--",           # S22: end-of-options, prevents query being parsed as option
                query,
            ]
            output = subprocess.run(cmd, capture_output=True, text=True, timeout=OUTPUT_SECONDS)
            if output.returncode != 0:
                logger.warning("tavily 搜索失败: %s", output.stderr.strip())
                return self._try_fallback(query, max_results)

            results = []
            data = json.loads(output.stdout)
            for item in data.get("results", []):
                url = item.get("url", "")
                results.append(
                    SearchResult(
                        title=item.get("title", "无标题"),
                        url=url,
                        snippet=item.get("content", "")[:300],
                        source="tavily",
                        category=self._classify_url(url),
                        published_date=(
                            item.get("published_date")
                            or item.get("publishedDate")
                            or item.get("published_at")
                            or item.get("date")
                            or ""
                        ),
                        relevance_score=item.get("score", 0.5),
                    )
                )
            return results
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            logger.warning("tavily 搜索异常 query=%s: %s", query, e, exc_info=True)
            return self._try_fallback(query, max_results)

    def search_all(
        self, queries: List[str], use_tavily: bool = True, page_path: Optional[Path] = None
    ) -> List[SearchResult]:
        """
        执行搜索，合并去重结果。当 Tavily 和外部 fallback 都不可用时，
        自动降级到本地知识图谱和本地 Wiki 链接搜索。

        Args:
            queries: 查询列表
            use_tavily: 是否使用 tavily（为 False 时仍尝试 fallback_search）
            page_path: 当前页面路径，用于本地 Wiki 链接搜索

        Returns:
            去重后的搜索结果列表
        """
        all_results = []
        has_external_results = False

        for query in queries:
            if use_tavily:
                results = self.search_tavily(query)
                if results:
                    has_external_results = True
                all_results.extend(results)
            elif self.fallback_search:
                # 显式禁用 tavily 时，直接调用 fallback
                results = self.fallback_search(query, 5)
                if results:
                    has_external_results = True
                all_results.extend(results)

        # 无外部结果时，启用内置本地降级搜索
        if not has_external_results:
            logger.info("无外部搜索结果，启用本地 KG/Wiki 降级搜索")
            for query in queries:
                all_results.extend(self._local_kg_search(query))
            if page_path:
                all_results.extend(self._local_wiki_search(page_path))

        # 按 URL 去重，保留相关性更高的
        url_map: Dict[str, SearchResult] = {}
        for result in all_results:
            if result.url in url_map:
                if result.relevance_score > url_map[result.url].relevance_score:
                    url_map[result.url] = result
            else:
                url_map[result.url] = result

        # 按相关性排序
        sorted_results = sorted(url_map.values(), key=lambda x: x.relevance_score, reverse=True)

        return sorted_results

    # ========== 影子页面生成 ==========

    def create_shadow(
        self,
        page_path: Path,
        search_results: List[SearchResult] | None = None,
        auto_search: bool = True,
    ) -> Optional[ShadowPage]:
        """
        为 Wiki 页面创建影子页面

        Args:
            page_path: 原 Wiki 页面路径
            search_results: 预设的搜索结果（如果不提供且 auto_search=True，则自动搜索）
            auto_search: 是否自动执行搜索

        Returns:
            ShadowPage 对象
        """
        if not page_path.exists():
            return None

        # 生成查询
        queries = self.generate_queries(page_path)

        # 获取搜索结果
        if search_results is None and auto_search:
            search_results = self.search_all(queries, page_path=page_path)

        if not search_results:
            return None

        # 分类整理
        categorized = self._categorize_results(search_results)

        # 生成 Markdown
        shadow = ShadowPage(
            shadow_for=str(page_path),
            search_date=datetime.now().strftime("%Y-%m-%d"),
            queries_used=queries,
            sources=search_results,
        )
        shadow.content = self._generate_markdown(page_path, shadow, categorized)

        # 写入文件
        self._write_shadow(page_path, shadow)

        return shadow

    def _generate_markdown(
        self, page_path: Path, shadow: ShadowPage, categorized: Dict[str, List[SearchResult]]
    ) -> str:
        """生成影子页面的 Markdown 内容"""
        title = self._extract_title(page_path.read_text(encoding="utf-8")) or page_path.stem

        lines = [
            "---",
            f'shadow_for: "{shadow.shadow_for}"',
            f"search_date: {shadow.search_date}",
            f"sources_count: {len(shadow.sources)}",
            f"source_count: {len(shadow.sources)}",
            f"sources: {json.dumps([item.url for item in shadow.sources], ensure_ascii=False)}",
            f"evidence_level: {'multiple' if len(shadow.sources) > 1 else 'single'}",
            "knowledge_stage: P2",
            "status: active",
            f"queries_used: {json.dumps(shadow.queries_used, ensure_ascii=False)}",
            "---",
            "",
            f"# Shadow: {title}",
            "",
            "> 影子页面自动生成，包含外部相关信息。",
            f"> - 最后更新: {shadow.search_date}",
            f"> - 来源数: {len(shadow.sources)}",
            f"> - 搜索查询: {', '.join(shadow.queries_used)}",
            "",
        ]

        category_titles = {
            "official": "官方资源",
            "community": "社区讨论",
            "blog": "技术博客",
            "social": "社交/中文社区",
            "news": "最新动态",
            "general": "其他资源",
        }

        for category, results in categorized.items():
            if not results:
                continue

            cat_title = category_titles.get(category, category)
            lines.extend(
                [
                    f"## {cat_title}",
                    "",
                ]
            )

            for result in results[:8]:  # 每类最多 8 条
                lines.append(f"- [{result.title}]({result.url})")
                if result.snippet:
                    snippet = result.snippet.replace("\n", " ")[:150]
                    lines.append(f"  - {snippet}...")
                if result.published_date:
                    lines.append(f"  - 发布日期: {result.published_date}")
                lines.append("")

        # 添加相关性排序的完整列表
        lines.extend(
            [
                "## 全部来源（按相关性排序）",
                "",
            ]
        )
        for i, result in enumerate(shadow.sources[:20], 1):
            published = (
                f" · {result.published_date}"
                if result.published_date
                else ""
            )
            lines.append(
                f"{i}. [{result.title}]({result.url}) — `{result.source}` ({result.relevance_score:.2f}){published}"  # noqa: E501
            )

        lines.append("")
        return "\n".join(lines)

    def _shadow_path(self, page_path: Path) -> Path:
        rel = page_path.relative_to(self.wiki_base)
        return self.shadow_dir / f"{shadow_projection_stem(rel.as_posix())}.shadow.md"

    def _previous_shadow_path(self, page_path: Path) -> Path:
        rel = page_path.relative_to(self.wiki_base)
        suffix = hashlib.md5(str(rel).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        return self.shadow_dir / f"{page_path.stem}_{suffix}.shadow.md"

    def _write_shadow(self, page_path: Path, shadow: ShadowPage):
        """写入影子页面文件（使用相对路径短投影 ID 避免同名文件覆盖）。"""
        shadow_path = self._shadow_path(page_path)
        shadow_path.write_text(shadow.content, encoding="utf-8")
        legacy_path = self._previous_shadow_path(page_path)
        if legacy_path != shadow_path and legacy_path.exists():
            try:
                legacy_path.unlink()
            except OSError:
                logger.debug("删除旧 shadow 文件失败: %s", legacy_path, exc_info=True)

    def sync_shadow(self, page_path: Path) -> Optional[ShadowPage]:
        """同步更新影子页面（重新搜索）"""
        return self.create_shadow(page_path, auto_search=True)

    def get_shadow(self, page_path: Path) -> Optional[ShadowPage]:
        """获取已存在的影子页面"""
        shadow_path = self._shadow_path(page_path)
        if not shadow_path.exists():
            legacy_path = self._previous_shadow_path(page_path)
            if legacy_path.exists():
                shadow_path = legacy_path
        if not shadow_path.exists():
            return None

        try:
            content = shadow_path.read_text(encoding="utf-8")
            fm = self._extract_frontmatter(content)
            return ShadowPage(
                shadow_for=fm.get("shadow_for", ""),
                search_date=fm.get("search_date", ""),
                queries_used=fm.get("queries_used", []),
                content=content,
            )
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
            logger.warning("读取影子页面失败 %s: %s", shadow_path, e)
            return None

    def list_shadows(self) -> List[Path]:
        """列出所有影子页面"""
        if not self.shadow_dir.exists():
            return []
        return list(self.shadow_dir.glob("*.shadow.md"))

    def _sync_one_page(self, page: Path) -> Tuple[str, Optional[ShadowPage]]:
        """同步单页影子页面，返回状态与 ShadowPage。"""
        try:
            existing = self.get_shadow(page)
            shadow = self.sync_shadow(page)
            if shadow:
                return ("updated" if existing else "created"), shadow
            return "failed", None
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as e:
            logger.warning("同步影子页面失败 %s: %s", page, e)
            return "failed", None

    def batch_sync(
        self,
        page_pattern: str = "*.md",
        max_workers: int = 4,
        per_page_timeout: Optional[int] = 60,
        max_pages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        批量同步影子页面

        Returns:
            {status: ok/error, created: N, updated: N, failed: N, total: N}
        """
        stats = {"created": 0, "updated": 0, "failed": 0, "total": 0}

        if not self.wiki_base.exists():
            return {**stats, "status": "ok"}

        pages = self._list_pages(page_pattern)
        if max_pages is None:
            try:
                max_pages = int(get_config().get("shadow_projection.max_pages_per_batch", 50))
            except (TypeError, ValueError):
                max_pages = 50
        if max_pages and max_pages > 0:
            pages = pages[:max_pages]
        stats["total"] = len(pages)
        if not pages:
            return {**stats, "status": "ok"}

        def _sync_with_timeout(page: Path) -> Tuple[str, Optional[ShadowPage]]:
            if per_page_timeout:
                # 在单页级别用 signal/thread 简单超时
                import threading

                result_container = [None]

                def _run():
                    result_container[0] = self._sync_one_page(page)

                t = threading.Thread(target=_run)
                t.start()
                t.join(timeout=per_page_timeout)
                if t.is_alive():
                    logger.warning("同步影子页面超时 %s", page)
                    return "failed", None
                return result_container[0] or ("failed", None)
            return self._sync_one_page(page)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_sync_with_timeout, page): page for page in pages}
            for future in as_completed(futures):
                status, _ = future.result()
                if status in stats:
                    stats[status] += 1
                else:
                    stats["failed"] += 1

        total_processed = stats["created"] + stats["updated"] + stats["failed"]
        failed_rate = stats["failed"] / max(total_processed, 1)
        stats["status"] = "error" if failed_rate > 0.5 else "ok"  # type: ignore[assignment]
        return stats

    def _list_pages(self, page_pattern: str = "*.md") -> List[Path]:
        pages = []
        for page in self.wiki_base.rglob(page_pattern):
            rel_parts = page.relative_to(self.wiki_base).parts
            if any(part in self.excluded_dirs or part.startswith(".") for part in rel_parts):
                continue
            # 排除指定子路径前缀（如系统内部报告目录）
            rel_str = "/".join(rel_parts)
            if any(
                rel_str.startswith(excluded.replace("\\", "/") + "/")
                for excluded in self.excluded_dirs
            ):
                continue
            if page.name.endswith(".shadow.md"):
                continue
            pages.append(page)
        return pages

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if yaml is None:
            return {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except (yaml.YAMLError, ValueError) as e:
                    logger.warning("忽略异常: %s", e, exc_info=True)
        return {}

    @staticmethod
    def _extract_title(content: str) -> str:
        """提取标题"""
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _get_keywords(frontmatter: Dict, layer: str) -> List[str]:
        """获取指定层的关键词"""
        keywords = frontmatter.get("关键词", {})
        if isinstance(keywords, dict):
            return keywords.get(layer, [])  # type: ignore[no-any-return]
        return []

    @classmethod
    def _classify_url(cls, url: str) -> str:
        """根据 URL 分类来源。

        优先匹配更具体的模式（含路径），避免 ``github.com/discussions``
        被 ``github.com`` 误分类为 official。
        """
        url_lower = url.lower()
        best_match = ("general", 0)
        for category, patterns in cls.CATEGORY_MAP.items():
            for pattern in patterns:
                if pattern in url_lower:
                    # 更长/更具体的模式优先
                    if len(pattern) > best_match[1]:
                        best_match = (category, len(pattern))
        return best_match[0]

    @staticmethod
    def _categorize_results(results: List[SearchResult]) -> Dict[str, List[SearchResult]]:
        """将搜索结果按分类整理"""
        categorized: Dict[str, List[SearchResult]] = {
            "official": [],
            "community": [],
            "blog": [],
            "social": [],
            "news": [],
            "general": [],
        }
        for result in results:
            categorized[result.category].append(result)
        return categorized


# ========== 前提条件变化检测（MVP） ==========


@dataclass
class DecisionDependency:
    """决策依赖条件"""

    dep_type: str  # library_feature / data_volume / cost_budget / team_capability
    raw_text: str = ""  # 原始匹配文本
    entity: str = ""  # 依赖实体（如库名）
    condition: str = ""  # 条件描述（如"不支持 SSL"）
    supported_at_decision: bool = False  # 决策时该条件是否成立


@dataclass
class ValidationResult:
    """验证结果"""

    status_changed: bool = False
    current_status: bool = False
    evidence: str = ""  # 验证来源 URL
    confidence: float = 0.0


@dataclass
class PremiseChange:
    """前提条件变化"""

    dependency: DecisionDependency
    old_status: bool
    new_status: bool
    evidence: str = ""
    confidence: float = 0.0


class LibraryFeatureValidator:
    """库特性支持状态验证器（MVP）

    通过搜索目标库的官方文档/changelog，验证某特性现在是否支持。
    """

    # 检测正面信号的关键词
    POSITIVE_SIGNALS = [
        "now supports",
        "added support",
        "从 v",
        "since v",
        "已支持",
        "新增支持",
        "开始支持",
        "现已支持",
    ]

    def check(self, dep: DecisionDependency, search_func=None) -> ValidationResult:
        """
        验证库特性支持状态。

        Args:
            dep: 决策依赖
            search_func: 可选的搜索函数（默认使用 ShadowPageManager.search_tavily）

        Returns:
            ValidationResult
        """
        if not dep.entity or not dep.condition:
            return ValidationResult(status_changed=False)

        query = f"{dep.entity} {dep.condition} support changelog"

        try:
            if search_func is None:
                # 使用 ShadowPageManager 的 tavily 搜索
                manager = ShadowPageManager()
                results = manager.search_tavily(query, max_results=5)
            else:
                results = search_func(query, max_results=5)

            for r in results:
                snippet = r.snippet.lower()
                if any(sig in snippet for sig in self.POSITIVE_SIGNALS):
                    return ValidationResult(
                        status_changed=True,
                        current_status=True,
                        evidence=r.url,
                        confidence=min(r.relevance_score + 0.3, 0.9),
                    )

            return ValidationResult(status_changed=False)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("库特性验证失败 %s: %s", dep.entity, e)
            return ValidationResult(status_changed=False)


class PremiseValidator:
    """前提条件验证器（MVP）

    在影子页面的周期性刷新中，验证知识图谱中记录的用户决策依赖条件是否仍然成立。
    v1 仅支持 library_feature 类型。
    """

    # 依赖类型识别正则（简化版）
    # 使用 re.ASCII 确保 \w 只匹配 ASCII，避免误匹配中文字符
    DEPENDENCY_PATTERNS = {
        "library_feature": [
            re.compile(
                r"([a-z0-9][a-z0-9\-\.]*[a-z0-9])\s*(?:库|package|library)?.*不?支持\s*([\w\-]+)",
                re.I | re.ASCII,
            ),
            re.compile(r"([\w\-]+)\s+(?:v?\d+\.\d+).*之前?.*不?支持", re.I | re.ASCII),
        ],
    }

    def __init__(self, wiki_base: Path | None = None):
        if wiki_base:
            self.wiki_base = wiki_base
        else:
            cfg_dir = get_config().wiki_dir
            self.wiki_base = Path(cfg_dir) if not isinstance(cfg_dir, Path) else cfg_dir

    def extract_dependencies(self, content: str) -> List[DecisionDependency]:
        """从页面内容中提取决策依赖条件"""
        dependencies = []

        for dep_type, patterns in self.DEPENDENCY_PATTERNS.items():
            for pattern in patterns:
                for match in pattern.finditer(content):
                    # type: ignore[operator]
                    entity = match.group(1) if match.lastindex >= 1 else ""  # type: ignore[operator]  # noqa: E501
                    # type: ignore[operator]
                    condition = match.group(2) if match.lastindex >= 2 else ""  # type: ignore[operator]  # noqa: E501
                    raw_text = match.group(0)
                    raw_lower = raw_text.lower()
                    # 保守推断：必须明确看到支持信号，且不含否定信号，才认为 True
                    positive = any(
                        p in raw_lower
                        for p in ("支持", "已支持", "supported", "supports", "enable", "enabled")
                    )
                    negative = any(
                        n in raw_lower
                        for n in (
                            "不支持",
                            "not support",
                            "does not support",
                            "not supported",
                            "未支持",
                            "不支持",
                        )
                    )
                    supported = positive and not negative
                    dependencies.append(
                        DecisionDependency(
                            dep_type=dep_type,
                            raw_text=raw_text,
                            entity=entity,
                            condition=condition,
                            supported_at_decision=supported,
                        )
                    )

        return dependencies

    def validate_premises(self, page_path: str) -> List[PremiseChange]:
        """
        验证某 Wiki 页面关联的所有决策前提条件。

        Args:
            page_path: Wiki 页面路径（相对或绝对）

        Returns:
            前提条件变化列表
        """
        p = Path(page_path)
        if not p.exists():
            p = self.wiki_base / page_path
        if not p.exists():
            logger.warning("页面不存在，跳过前提验证: %s", page_path)
            return []

        try:
            content = p.read_text(encoding="utf-8")
        except (OSError, IOError) as e:
            logger.warning("读取页面失败 %s: %s", page_path, e)
            return []

        dependencies = self.extract_dependencies(content)
        if not dependencies:
            return []

        changes = []
        validator = LibraryFeatureValidator()

        for dep in dependencies:
            result = validator.check(dep)
            if result.status_changed and dep.supported_at_decision != result.current_status:
                changes.append(
                    PremiseChange(
                        dependency=dep,
                        old_status=dep.supported_at_decision,
                        new_status=result.current_status,
                        evidence=result.evidence,
                        confidence=result.confidence,
                    )
                )

        if changes:
            logger.info("页面 %s 检测到 %s 个前提条件变化", page_path, len(changes))

        return changes

    @staticmethod
    def format_change_report(changes: List[PremiseChange]) -> str:
        """Format premise changes for user-facing diagnostics."""
        if not changes:
            return "未检测到前提条件变化。"

        def status_label(value: bool) -> str:
            return "支持" if value else "不支持"

        lines = ["# 前提条件变化报告", ""]
        for idx, change in enumerate(changes, 1):
            dep = change.dependency
            lines.append(f"## {idx}. {dep.entity or '未知依赖'}")
            if dep.condition:
                lines.append(f"- 条件: {dep.condition}")
            if dep.raw_text:
                lines.append(f"- 原始前提: {dep.raw_text}")
            lines.append(
                f"- 状态变化: {status_label(change.old_status)} -> {status_label(change.new_status)}"
            )
            lines.append(f"- 置信度: {change.confidence:.2f}")
            if change.evidence:
                lines.append(f"- 证据: {change.evidence}")
            lines.append("")

        return "\n".join(lines).rstrip()

    def validate_premises_report(self, page_path: str) -> str:
        """Validate a page and return a formatted premise-change report."""
        return self.format_change_report(self.validate_premises(page_path))

    def validate_batch(self, page_pattern: str = "*.md") -> Dict[str, List[PremiseChange]]:
        """
        批量验证 Wiki 页面前提条件。

        Returns:
            {page_path: [PremiseChange, ...]}
        """
        results = {}
        manager = ShadowPageManager(wiki_base=str(self.wiki_base))

        for page in manager._list_pages(page_pattern):
            changes = self.validate_premises(str(page))
            if changes:
                results[str(page)] = changes

        return results
