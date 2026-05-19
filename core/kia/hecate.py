"""
Shadow Page - 知识影子页面系统

为每个 Wiki 页面创建"影子"，通过联网搜索获取外部相关信息：
- 官方文档/博客的最新版本
- 社区讨论（StackOverflow、GitHub Issues、Reddit）
- 中文社区内容（知乎、掘金、微信公众号）
- 最新动态和新闻

双轨联网：
- tavily-search: 通用网络搜索，覆盖官方文档和技术博客
- agent-reach: 社交平台搜索，覆盖小红书、抖音、微信公众号等

设计原则：
- 影子页面独立存储，不污染主页面
- 搜索可手动触发、自动触发或定期刷新
- 查询基于页面标题+关键词自动生成，无需人工编写
- 结果自动分类整理，支持快速浏览
"""
# Hecate — 幽冥女神 — 影子页面，知识的对偶与镜像
# 原模块: shadow_page.py



import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from core.config import get_config
import logging

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str = ""
    source: str = ""           # tavily / agent_reach
    category: str = "general"  # official / community / news / blog / social
    published_date: str = ""
    relevance_score: float = 0.0


@dataclass
class ShadowPage:
    """影子页面"""
    shadow_for: str            # 原页面路径
    search_date: str
    queries_used: List[str] = field(default_factory=list)
    sources: List[SearchResult] = field(default_factory=list)
    content: str = ""          # 生成的 Markdown 内容


class ShadowPageManager:
    """影子页面管理器"""

    SHADOW_DIR_NAME = "07-Shadow"

    # 来源域名分类
    CATEGORY_MAP = {
        "official": [
            "docs.", "documentation.", "developer.", "api.",
            "github.com", "gitlab.com",
            "apache.org", "python.org", "mozilla.org",
            "microsoft.com", "google.com", "cloud.google",
            "aws.amazon.com", "azure.microsoft.com",
            "kubernetes.io", "docker.com",
        ],
        "community": [
            "stackoverflow.com", "stackexchange.com",
            "reddit.com", "news.ycombinator.com",
            "discuss.", "forum.", "community.",
            "github.com/discussions",
        ],
        "blog": [
            "medium.com", "dev.to", "hashnode.com",
            "substack.com", "blog.",
            "juejin.cn", "segmentfault.com", "csdn.net",
        ],
        "social": [
            "zhihu.com", "xiaohongshu.com", "douyin.com",
            "weixin.qq.com", "mp.weixin.qq.com",
            "twitter.com", "x.com", "linkedin.com",
        ],
        "news": [
            "techcrunch.com", "theverge.com", "arstechnica.com",
            "36kr.com", "pingwest.com", "solidot.org",
        ],
    }

    def __init__(self, wiki_base: str = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else (
            get_config().wiki_dir
        )
        self.shadow_dir = self.wiki_base / self.SHADOW_DIR_NAME
        self.shadow_dir.mkdir(parents=True, exist_ok=True)

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
        except Exception:
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
            queries.append(f"{' '.join(concepts[:2])} 最新进展 2026")

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

    def search_tavily(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """
        使用 tavily-search 进行搜索

        需要 tvly CLI 已安装并登录
        """
        results = []
        if not shutil.which("tvly"):
            logger.warning("tvly CLI 未安装，跳过 tavily 搜索")
            return results

        try:
            cmd = [
                "tvly", "search", query,
                "--max-results", str(max_results),
                "--json",
            ]
            output = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if output.returncode != 0:
                return results

            data = json.loads(output.stdout)
            for item in data.get("results", []):
                url = item.get("url", "")
                results.append(SearchResult(
                    title=item.get("title", "无标题"),
                    url=url,
                    snippet=item.get("content", "")[:300],
                    source="tavily",
                    category=self._classify_url(url),
                    relevance_score=item.get("score", 0.5),
                ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass

        return results

    def search_agent_reach(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """
        使用 agent-reach 进行社交平台搜索

        覆盖：小红书、抖音、微信公众号等
        """
        results = []
        try:
            # agent-reach 的调用方式取决于 skill 的具体实现
            # 这里使用通用的 skill 调用方式
            cmd = ["claude", "skill", "agent-reach", query]
            output = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if output.returncode != 0:
                return results

            # 解析 agent-reach 的输出（假设为 JSON 格式）
            data = json.loads(output.stdout)
            for item in data.get("results", []):
                results.append(SearchResult(
                    title=item.get("title", "无标题"),
                    url=item.get("url", ""),
                    snippet=item.get("summary", "")[:300],
                    source="agent_reach",
                    category="social",
                    relevance_score=item.get("relevance", 0.5),
                ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass

        return results

    def search_all(self, queries: List[str],
                   use_tavily: bool = True,
                   use_agent_reach: bool = True) -> List[SearchResult]:
        """
        执行所有搜索，合并去重结果

        Args:
            queries: 查询列表
            use_tavily: 是否使用 tavily
            use_agent_reach: 是否使用 agent_reach

        Returns:
            去重后的搜索结果列表
        """
        all_results = []

        for query in queries:
            if use_tavily:
                all_results.extend(self.search_tavily(query))
            if use_agent_reach:
                all_results.extend(self.search_agent_reach(query))

        # 按 URL 去重，保留相关性更高的
        url_map: Dict[str, SearchResult] = {}
        for result in all_results:
            if result.url in url_map:
                if result.relevance_score > url_map[result.url].relevance_score:
                    url_map[result.url] = result
            else:
                url_map[result.url] = result

        # 按相关性排序
        sorted_results = sorted(
            url_map.values(),
            key=lambda x: x.relevance_score,
            reverse=True
        )

        return sorted_results

    # ========== 影子页面生成 ==========

    def create_shadow(self, page_path: Path,
                      search_results: List[SearchResult] = None,
                      auto_search: bool = True) -> Optional[ShadowPage]:
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
            search_results = self.search_all(queries)

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

    def _generate_markdown(self, page_path: Path, shadow: ShadowPage,
                           categorized: Dict[str, List[SearchResult]]) -> str:
        """生成影子页面的 Markdown 内容"""
        title = self._extract_title(page_path.read_text(encoding="utf-8")) or page_path.stem

        lines = [
            "---",
            f"shadow_for: \"{shadow.shadow_for}\"",
            f"search_date: {shadow.search_date}",
            f"sources_count: {len(shadow.sources)}",
            f"queries_used: {json.dumps(shadow.queries_used, ensure_ascii=False)}",
            "---",
            "",
            f"# Shadow: {title}",
            "",
            f"> 影子页面自动生成，包含外部相关信息。",
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
            lines.extend([
                f"## {cat_title}",
                "",
            ])

            for result in results[:8]:  # 每类最多 8 条
                lines.append(f"- [{result.title}]({result.url})")
                if result.snippet:
                    snippet = result.snippet.replace("\n", " ")[:150]
                    lines.append(f"  - {snippet}...")
                lines.append("")

        # 添加相关性排序的完整列表
        lines.extend([
            "## 全部来源（按相关性排序）",
            "",
        ])
        for i, result in enumerate(shadow.sources[:20], 1):
            lines.append(f"{i}. [{result.title}]({result.url}) — `{result.source}` ({result.relevance_score:.2f})")

        lines.append("")
        return "\n".join(lines)

    def _write_shadow(self, page_path: Path, shadow: ShadowPage):
        """写入影子页面文件"""
        shadow_filename = f"{page_path.stem}.shadow.md"
        shadow_path = self.shadow_dir / shadow_filename
        shadow_path.write_text(shadow.content, encoding="utf-8")

    def sync_shadow(self, page_path: Path) -> Optional[ShadowPage]:
        """同步更新影子页面（重新搜索）"""
        return self.create_shadow(page_path, auto_search=True)

    def get_shadow(self, page_path: Path) -> Optional[ShadowPage]:
        """获取已存在的影子页面"""
        shadow_path = self.shadow_dir / f"{page_path.stem}.shadow.md"
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
        except Exception:
            return None

    def list_shadows(self) -> List[Path]:
        """列出所有影子页面"""
        if not self.shadow_dir.exists():
            return []
        return list(self.shadow_dir.glob("*.shadow.md"))

    def remove_shadow(self, page_path: Path) -> bool:
        """删除影子页面"""
        shadow_path = self.shadow_dir / f"{page_path.stem}.shadow.md"
        if shadow_path.exists():
            shadow_path.unlink()
            return True
        return False

    def batch_sync(self, page_pattern: str = "*.md") -> Dict[str, int]:
        """
        批量同步影子页面

        Returns:
            {created: N, updated: N, failed: N}
        """
        stats = {"created": 0, "updated": 0, "failed": 0}
        inbox = self.wiki_base / "00-Inbox"

        if not inbox.exists():
            return stats

        for page in inbox.glob(page_pattern):
            try:
                existing = self.get_shadow(page)
                shadow = self.sync_shadow(page)
                if shadow:
                    if existing:
                        stats["updated"] += 1
                    else:
                        stats["created"] += 1
                else:
                    stats["failed"] += 1
            except Exception:
                stats["failed"] += 1

        return stats

    # ========== 辅助方法 ==========

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        """提取 frontmatter"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    return yaml.safe_load(parts[1]) or {}
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")
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
            return keywords.get(layer, [])
        return []

    @classmethod
    def _classify_url(cls, url: str) -> str:
        """根据 URL 分类来源"""
        url_lower = url.lower()
        for category, patterns in cls.CATEGORY_MAP.items():
            for pattern in patterns:
                if pattern in url_lower:
                    return category
        return "general"

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


# ========== 便捷函数 ==========

def create_shadow_for_page(page_path: str, wiki_base: str = None) -> Optional[ShadowPage]:
    """便捷函数：为单个页面创建影子"""
    manager = ShadowPageManager(wiki_base=wiki_base)
    return manager.create_shadow(Path(page_path))


def sync_all_shadows(wiki_base: str = None) -> Dict[str, int]:
    """便捷函数：同步所有影子页面"""
    manager = ShadowPageManager(wiki_base=wiki_base)
    return manager.batch_sync()
