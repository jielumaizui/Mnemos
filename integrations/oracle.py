#!/usr/bin/env python3
# Oracle — 神谕所 — Wiki 读取，查询知识库的接口
# 原模块: wiki_reader.py

"""
Wiki Reader - Wiki知识读取模块（热力值控制深度版）

特点：
1. 全Wiki可读，无权限限制
2. AI自主决定是否读取
3. 根据热力值控制读取深度（L0-L9不同深度）
4. 列出所有相关条目，按热力值排序

读取深度规则：
- L0: 元数据（标题/标签）
- L1-L3: 摘要(100字)
- L4-L6: 段落(500字)
- L7-L8: 全文 + 关联
- L9: 全文 + 深度追踪

与L1 storage的区别：
- L1 storage: 有权限控制，用于上下文恢复
- Wiki: 无限制，热力值控制深度，用于知识查询
"""

from __future__ import annotations

import logging


import re
import time
import yaml
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple, cast
from datetime import datetime

from core.config import get_config

logger = logging.getLogger(__name__)
try:
    from core.wiki_metrics import WikiMetrics, get_default_metrics

    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

try:
    from core.frontmatter import fm_get
except ImportError:
    fm_get = None  # type: ignore[assignment]


class WikiReader:
    """
    Wiki读取器 - 热力值控制读取深度

    读取深度配置：
    - L0: 元数据（标题/标签）- 沉睡知识
    - L1-L3: 摘要(100字) - 低热
    - L4-L6: 段落(500字) + 关联 - 中热
    - L7-L8: 全文 + 关联 - 高热
    - L9: 全文 + 深度追踪 - 核心知识
    """

    # 页面索引缓存（按 wiki 目录），默认 TTL 60 秒
    _index_cache: Dict[str, Tuple[float, Dict]] = {}

    # 读取深度配置（简化为3级）
    READ_DEPTH = {
        "metadata": {
            "type": "metadata",
            "chars": 0,
            "related": False,
            "deep": False,
            "desc": "元数据",
        },
        "cold": {
            "type": "summary",
            "chars": 100,
            "related": False,
            "deep": False,
            "desc": "冷知识，摘要100字",
        },
        "warm": {
            "type": "paragraph",
            "chars": 500,
            "related": False,
            "deep": False,
            "desc": "温知识，段落500字",
        },
        "hot": {
            "type": "full",
            "chars": -1,
            "related": True,
            "deep": False,
            "desc": "热知识，全文+关联",
        },
    }

    DEPTH_TO_LEVEL = {
        "metadata": "metadata",
        "summary": "cold",
        "full": "hot",
    }

    def __init__(
        self,
        wiki_path: str | None = None,
        *,
        enable_metrics: bool = True,
    ):
        if wiki_path is None:
            self.wiki_path = get_config().wiki_dir
        else:
            self.wiki_path = Path(wiki_path).expanduser()
        self._index_cache_ttl = float(get_config().get("oracle.index_cache_ttl_seconds", 60))
        self.metrics = (
            self._init_metrics() if _METRICS_AVAILABLE and enable_metrics else None
        )
        self.index = self._build_index()  # 页面索引（带缓存）

    def _init_metrics(self):
        """默认知识库复用全局账本；显式非默认知识库使用局部账本，避免热力串库。"""
        try:
            from core.config import get_config

            cfg_wiki = Path(get_config().wiki_dir).expanduser().resolve()
            current_wiki = self.wiki_path.expanduser().resolve()
            if current_wiki == cfg_wiki:
                return get_default_metrics()
            return WikiMetrics(
                db_path=str(current_wiki / ".kg" / "wiki_metrics.db"),
                wiki_dir=str(current_wiki),
            )
        except ImportError:
            logger.debug("初始化 Wiki 热力账本失败", exc_info=True)
            return None

    def _build_index(self) -> Dict:
        """构建Wiki索引（包含热力值），结果按 wiki 目录缓存。"""
        cache_key = str(self.wiki_path)
        entry = self._index_cache.get(cache_key)
        if entry and (time.time() - entry[0]) < self._index_cache_ttl:
            return entry[1]

        index: Dict = {}
        for subdir in [
            "00-Inbox",
            "01-People",
            "02-Projects",
            "03-Tech",
            "04-Concepts",
            "05-MOCs",
            "06-Retrospectives",
        ]:
            dir_path = self.wiki_path / subdir
            if not dir_path.exists():
                continue

            for file_path in dir_path.rglob("*.md"):
                rel_path = file_path.relative_to(self.wiki_path)
                page_id = str(rel_path.with_suffix("")).replace("\\", "/")

                # 快速索引（只读frontmatter）
                try:
                    fm = self._quick_read_frontmatter(file_path)
                    # 获取热力值
                    heat_info = self._get_heat_info(page_id)

                    verification = fm_get(fm, "status") or ""
                    confidence = float(fm_get(fm, "confidence") or 0.5)
                    index[page_id] = {
                        "type": subdir,
                        "title": fm_get(fm, "name") or file_path.stem,
                        "entities": fm_get(fm, "keywords") or [],
                        "concepts": fm_get(fm, "aliases") or [],
                        "path": file_path,
                        "heat_level": heat_info.get("level", "cold"),
                        "heat_score": heat_info.get("score", 0),
                        "verification": verification,
                        "confidence": confidence,
                    }
                except (OSError, UnicodeError, ValueError, TypeError, KeyError) as e:
                    logger.warning("解析 frontmatter 失败: %s", e, exc_info=True)

        self._index_cache[cache_key] = (time.time(), index)
        return index

    def _get_heat_info(self, page_id: str) -> Dict:
        """从 wiki_metrics 获取页面热力信息"""
        if self.metrics:
            page = self.metrics.get_page(page_id) or self.metrics.get_page(f"{page_id}.md")
            if page:
                return {"level": page.heat_level, "score": page.heat_score}
        return {"level": "cold", "score": 0.0}

    def _record_heat(self, page_id: str, access_type: str) -> Dict[str, Any]:
        """记录页面使用热力，并刷新当前索引里的热力字段。"""
        if not self.metrics:
            return cast(Dict[str, Any], self.index.get(page_id, {}))
        try:
            self.metrics.update_heat(page_id, access_type=access_type)
            heat_info = self._get_heat_info(page_id)
            if page_id in self.index:
                self.index[page_id]["heat_level"] = heat_info.get("level", "cold")
                self.index[page_id]["heat_score"] = heat_info.get("score", 0.0)
            return heat_info
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logger.debug("heat update failed for %s", page_id, exc_info=True)
            return cast(Dict[str, Any], self.index.get(page_id, {}))

    def _quick_read_frontmatter(self, file_path: Path) -> Dict:
        """快速读取frontmatter"""
        try:
            with file_path.open("r", encoding="utf-8", errors="ignore") as handle:
                if handle.readline().strip() != "---":
                    return {}
                lines: List[str] = []
                total = 0
                for line in handle:
                    if line.strip() == "---":
                        return yaml.safe_load("".join(lines)) or {}
                    total += len(line.encode("utf-8"))
                    if total > 65536:
                        return {}
                    lines.append(line)
            return {}
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            logging.getLogger(__name__).warning(
                "Caught unexpected error at oracle.py", exc_info=True
            )
            return {}

    def _parse_content(self, content: str) -> Tuple[Dict, str]:
        """解析frontmatter和正文"""
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                    return fm, body
                except (yaml.YAMLError, ValueError) as e:
                    logger.warning("快速解析 frontmatter 失败: %s", e)
        return {}, content

    def _page_title(self, frontmatter: Dict, page_id: str) -> str:
        """返回用户可读标题，兼容中文 frontmatter。"""
        return fm_get(frontmatter, "name") or fm_get(frontmatter, "title") or page_id

    def _read_by_depth(
        self,
        page_id: str,
        heat_level: str,
        *,
        record_access: bool = True,
    ) -> Optional[Dict]:
        """
        根据热力等级读取对应深度

        核心逻辑：热力值决定读取深度
        """
        if page_id not in self.index:
            return None

        info = self.index[page_id]
        file_path = info["path"]
        try:
            last_modified = datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
            last_modified = ""

        try:
            content = file_path.read_text(encoding="utf-8")
            frontmatter, body = self._parse_content(content)
        except (OSError, IOError) as e:
            logger.warning("读取页面内容失败: %s", e)
            return None

        if record_access:
            self._update_heat_info(page_id, heat_level)
        config = self.READ_DEPTH.get(heat_level, self.READ_DEPTH["cold"])
        read_type = cast(str, config["type"])
        return self._build_read_result(read_type, frontmatter, body, page_id, info, last_modified)

    def _update_heat_info(self, page_id: str, requested_level: str) -> str:
        """记录访问热力并同步 index 中的热力字段。"""
        info = self.index[page_id]
        heat_info = self._record_heat(page_id, "read")
        if not isinstance(heat_info, dict):
            return requested_level
        reported_level = heat_info.get("level", requested_level)
        info["heat_level"] = reported_level
        info["heat_score"] = heat_info.get("score", info["heat_score"])
        return cast(str, reported_level)

    def _build_read_result(
        self,
        read_type: str,
        frontmatter: Dict,
        body: str,
        page_id: str,
        info: Dict,
        last_modified: str,
    ) -> Optional[Dict]:
        """按读取类型分派到具体 builder。"""
        if read_type == "metadata":
            return self._build_metadata_result(frontmatter, page_id, info, last_modified)
        if read_type == "summary":
            return self._build_summary_result(frontmatter, body, page_id, info, last_modified)
        if read_type == "paragraph":
            return self._build_paragraph_result(frontmatter, body, page_id, info, last_modified)
        if read_type == "full":
            return self._build_full_result(frontmatter, body, page_id, info, last_modified)
        if read_type == "full_plus":
            return self._build_full_plus_result(frontmatter, body, page_id, info, last_modified)
        return None

    def _build_metadata_result(
        self, frontmatter: Dict, page_id: str, info: Dict, last_modified: str
    ) -> Dict:
        """L0：只返回元数据。"""
        return {
            "title": self._page_title(frontmatter, page_id),
            "tags": frontmatter.get("tags", []),
            "entities": frontmatter.get("entities", []),
            "concepts": frontmatter.get("concepts", []),
            "heat_level": info["heat_level"],
            "heat_score": info["heat_score"],
            "verification": info.get("verification", ""),
            "confidence": info.get("confidence", 0.5),
            "source": fm_get(frontmatter, "source") or "",
            "last_modified": last_modified,
            "note": "沉睡知识，低活跃度，可唤醒",
            "depth": "metadata_only",
        }

    def _build_summary_result(
        self, frontmatter: Dict, body: str, page_id: str, info: Dict, last_modified: str
    ) -> Dict:
        """L1-L3：返回前N字摘要。"""
        max_chars: int = self.READ_DEPTH["cold"]["chars"]  # type: ignore[assignment]
        summary = body[:max_chars] if len(body) > max_chars else body
        if len(body) > max_chars:
            summary += "..."
        return {
            "summary": summary,
            "title": self._page_title(frontmatter, page_id),
            "heat_level": info["heat_level"],
            "heat_score": info["heat_score"],
            "verification": info.get("verification", ""),
            "confidence": info.get("confidence", 0.5),
            "source": fm_get(frontmatter, "source") or "",
            "last_modified": last_modified,
            "depth": f"summary_{max_chars}",
        }

    def _build_paragraph_result(
        self,
        frontmatter: Dict,
        body: str,
        page_id: str,
        info: Dict,
        last_modified: str,
    ) -> Dict:
        """L4-L6：返回关键段落。"""
        max_chars = cast(int, self.READ_DEPTH["warm"]["chars"])
        content = body[:max_chars] if len(body) > max_chars else body
        result: Dict = {
            "content": content,
            "title": self._page_title(frontmatter, page_id),
            "entities": frontmatter.get("entities", []),
            "concepts": frontmatter.get("concepts", []),
            "heat_level": info["heat_level"],
            "heat_score": info["heat_score"],
            "verification": info.get("verification", ""),
            "confidence": info.get("confidence", 0.5),
            "source": fm_get(frontmatter, "source") or "",
            "last_modified": last_modified,
            "depth": f"paragraph_{max_chars}",
        }
        if self.READ_DEPTH["warm"]["related"]:
            result["related"] = self._get_related_pages(page_id, limit=3)
        return result

    def _build_full_result(
        self,
        frontmatter: Dict,
        body: str,
        page_id: str,
        info: Dict,
        last_modified: str,
    ) -> Dict:
        """L7-L8：全文 + 关联。"""
        config = self.READ_DEPTH["hot"]
        return {
            "content": body,
            "title": self._page_title(frontmatter, page_id),
            "entities": frontmatter.get("entities", []),
            "concepts": frontmatter.get("concepts", []),
            "heat_level": info["heat_level"],
            "heat_score": info["heat_score"],
            "verification": info.get("verification", ""),
            "confidence": info.get("confidence", 0.5),
            "source": fm_get(frontmatter, "source") or "",
            "last_modified": last_modified,
            "depth": "full",
            "related": self._get_related_pages(page_id, limit=5) if config["related"] else [],
        }

    def _build_full_plus_result(
        self,
        frontmatter: Dict,
        body: str,
        page_id: str,
        info: Dict,
        last_modified: str,
    ) -> Dict:
        """L9：全文 + 深度追踪。"""
        return {
            "content": body,
            "title": self._page_title(frontmatter, page_id),
            "entities": frontmatter.get("entities", []),
            "concepts": frontmatter.get("concepts", []),
            "heat_level": info["heat_level"],
            "heat_score": info["heat_score"],
            "verification": info.get("verification", ""),
            "confidence": info.get("confidence", 0.5),
            "source": fm_get(frontmatter, "source") or "",
            "last_modified": last_modified,
            "depth": "full_plus",
            "related": self._get_related_pages(page_id, limit=5),
            "deep_traced": True,
            "note": "核心知识，深度追踪",
        }

    def _get_related_pages(self, page_id: str, limit: int = 5) -> List[Dict]:
        """获取关联页面"""
        if page_id not in self.index:
            return []

        info = self.index[page_id]
        related = []

        # 通过entities查找
        for entity in info["entities"][:3]:
            pages = self._find_pages_by_entity(entity)
            for p in pages[:2]:
                if p != page_id:
                    related.append(
                        {"page_id": p, "relation": f"entity:{entity}", "type": "entity_link"}
                    )

        # 通过concepts查找
        for concept in info["concepts"][:3]:
            pages = self._find_pages_by_concept(concept)
            for p in pages[:2]:
                if p != page_id and p not in [r["page_id"] for r in related]:
                    related.append(
                        {"page_id": p, "relation": f"concept:{concept}", "type": "concept_link"}
                    )

        return related[:limit]

    def _find_pages_by_entity(self, entity: str) -> List[str]:
        """查找包含特定实体的页面"""
        results = []
        for page_id, info in self.index.items():
            if entity in info["entities"]:
                results.append(page_id)
        return results

    def _find_pages_by_concept(self, concept: str) -> List[str]:
        """查找包含特定概念的页面"""
        results = []
        for page_id, info in self.index.items():
            if concept in info["concepts"]:
                results.append(page_id)
        return results

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """搜索知识库（MCP / Agora 兼容接口）

        等同于 search_all_relevant，但返回数量受 limit 限制。
        """
        results = self.search_all_relevant(query)
        return results[:limit]

    def read_page(self, page_path: str, depth: str | None = None) -> Optional[Dict]:
        """读取指定 wiki 页面（MCP / Agora 兼容接口）

        page_path 可以是相对路径或 page_id（支持 .md 后缀）。
        """
        # Normalize: 去掉 .md 后缀，统一路径分隔符
        page_id = page_path.replace("\\", "/").removesuffix(".md")
        if depth:
            level = self.DEPTH_TO_LEVEL.get(depth)
            if level:
                return self._read_by_depth(page_id, level)
        return self.read_page_by_heat(page_id)

    def read_access_metadata(self, page_path: str) -> Optional[Dict]:
        """Read only the ACL-bearing frontmatter without access side effects."""
        page_id = page_path.replace("\\", "/").removesuffix(".md")
        info = self.index.get(page_id)
        if info is None:
            return None
        frontmatter = self._quick_read_frontmatter(info["path"])
        return {
            "page_id": page_id,
            "frontmatter": frontmatter,
            "acl_metadata_complete": frontmatter.get("acl_metadata_complete") is True,
            "acl_reconciliation_status": frontmatter.get(
                "acl_reconciliation_status",
                "",
            ),
        }

    @staticmethod
    def _query_terms(query: str) -> List[str]:
        terms = re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z0-9_\-]{2,}", query.lower())
        return terms or [query.lower()]

    def _fallback_body_search(self, query: str, limit: int = 20) -> List[Dict]:
        """回退：遍历所有 markdown 文件搜索正文内容（与 ContextAwareSearch._recall_from_files 逻辑一致）"""
        keywords = self._query_terms(query)
        if not keywords:
            return []
        results = []
        for subdir in [
            "00-Inbox",
            "01-People",
            "02-Projects",
            "03-Tech",
            "04-Concepts",
            "05-MOCs",
            "06-Retrospectives",
        ]:
            dir_path = self.wiki_path / subdir
            if not dir_path.exists():
                continue
            for file_path in dir_path.rglob("*.md"):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    frontmatter, body = self._parse_content(content)
                    body_lower = body.lower()
                    if not any(kw in body_lower for kw in keywords):
                        continue
                    match_count = sum(1 for kw in keywords if kw in body_lower)
                    score: float = 5 + match_count * 2
                    rel_path = file_path.relative_to(self.wiki_path)
                    page_id = str(rel_path.with_suffix("")).replace("\\", "/")
                    title = fm_get(frontmatter, "name") or file_path.stem
                    verification = fm_get(frontmatter, "status") or ""
                    confidence = float(fm_get(frontmatter, "confidence") or 0.5)
                    if verification == "pending-verification":
                        score *= 0.1
                    elif confidence < 0.5:
                        score *= 0.3
                    heat_info = self._get_heat_info(page_id)
                    results.append(
                        {
                            "page_id": page_id,
                            "title": title,
                            "type": subdir,
                            "heat_level": heat_info.get("level", "cold"),
                            "heat_score": heat_info.get("score", 0),
                            "relevance_score": round(score, 2),
                            "reasons": ["body_match"],
                            "verification": verification,
                            "confidence": confidence,
                            "scope": frontmatter.get("scope", ""),
                            "source_agent": frontmatter.get("source_agent", ""),
                            "session_id": frontmatter.get("session_id", ""),
                            "project": frontmatter.get("project", ""),
                            "tags": list(frontmatter.get("tags") or []),
                            "acl_schema_version": int(
                                frontmatter.get("acl_schema_version") or 0
                            ),
                            "acl_metadata_complete": (
                                frontmatter.get("acl_metadata_complete") is True
                            ),
                            "acl_reconciliation_status": frontmatter.get(
                                "acl_reconciliation_status",
                                "",
                            ),
                        }
                    )
                    if len(results) >= limit:
                        break
                # DEBT(S8): 容错跳过，避免单条记录中断批量处理
                except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
                    continue
            if len(results) >= limit:
                break
        # 按关联度优先排序，热力值次要
        results.sort(key=lambda x: (x["relevance_score"], x["heat_score"]), reverse=True)
        return results

    def search_all_relevant(self, query: str) -> List[Dict]:
        """
        搜索所有相关页面（不限制数量，返回全部）

        返回按热力值排序的结果列表
        """
        query_lower = query.lower()
        results = []

        for page_id, info in self.index.items():
            score = 0
            reasons = []

            # 标题匹配（高权重）
            title = info["title"].lower()
            if query_lower in title:
                score += 20
                reasons.append("title_exact")
            elif any(word in title for word in query_lower.split()):
                score += 10
                reasons.append("title_partial")

            # 实体匹配（中权重）
            for entity in info["entities"]:
                if query_lower in entity.lower():
                    score += 15
                    reasons.append(f"entity:{entity}")

            # 概念匹配（中权重）
            for concept in info["concepts"]:
                if query_lower in concept.lower():
                    score += 12
                    reasons.append(f"concept:{concept}")

            # 路径匹配（低权重）
            if query_lower in page_id.lower():
                score += 8
                reasons.append("path_match")

            if score > 0:
                # 质量过滤：pending-verification 或低置信度页面大幅降权
                confidence = info.get("confidence", 0.5)
                verification = info.get("verification", "")
                score_float: float = score
                if verification == "pending-verification":
                    score_float *= 0.1  # 降权 90%
                elif confidence < 0.5:
                    score_float *= 0.3  # 降权 70%

                frontmatter = self._quick_read_frontmatter(info["path"])
                results.append(
                    {
                        "page_id": page_id,
                        "title": info["title"],
                        "type": info["type"],
                        "heat_level": info["heat_level"],
                        "heat_score": info["heat_score"],
                        "relevance_score": round(score_float, 2),
                        "reasons": reasons,
                        "verification": verification,
                        "confidence": confidence,
                        "scope": frontmatter.get("scope", ""),
                        "source_agent": frontmatter.get("source_agent", ""),
                        "session_id": frontmatter.get("session_id", ""),
                        "project": frontmatter.get("project", ""),
                        "tags": list(frontmatter.get("tags") or []),
                        "acl_schema_version": int(
                            frontmatter.get("acl_schema_version") or 0
                        ),
                        "acl_metadata_complete": (
                            frontmatter.get("acl_metadata_complete") is True
                        ),
                        "acl_reconciliation_status": frontmatter.get(
                            "acl_reconciliation_status",
                            "",
                        ),
                    }
                )

        # 按关联度优先排序，热力值作为次要因素
        # 标题匹配 > 实体/概念匹配 > 路径匹配 > 正文匹配
        # 00-Inbox 中的原始碎片降权，优先展示结构化知识页面
        def _sort_key(x):
            relevance = x["relevance_score"]
            heat = x["heat_score"]
            if x.get("type") == "00-Inbox":
                # Inbox 原始文件降权：relevance 取 10%
                return (relevance * 0.1, heat)
            return (relevance, heat)

        results.sort(key=_sort_key, reverse=True)
        if not results:
            results = self._fallback_body_search(query)
        return results

    def record_authorized_search(self, results: List[Dict]) -> None:
        """Apply heat updates only to access-authorized search results."""
        for item in results:
            heat_info = self._record_heat(item["page_id"], "search_hit")
            if isinstance(heat_info, dict):
                item["heat_level"] = heat_info.get("level", item["heat_level"])
                item["heat_score"] = heat_info.get("score", item["heat_score"])

    def read_page_by_heat(self, page_id: str) -> Optional[Dict]:
        """
        根据页面热力值自动读取对应深度

        这是核心方法：热力值决定读取深度
        """
        if page_id not in self.index:
            return None

        heat_level = self.index[page_id]["heat_level"]
        return self._read_by_depth(page_id, heat_level)

    def preview_page_by_heat(self, page_id: str) -> Optional[Dict]:
        """Read at the current heat depth without recording an access side effect."""

        if page_id not in self.index:
            return None
        heat_level = self.index[page_id]["heat_level"]
        return self._read_by_depth(page_id, heat_level, record_access=False)

    def get_knowledge(self, query: str, _include_related: bool = True) -> Dict:
        """
        获取Wiki知识（主入口）

        流程：
        1. 搜索所有相关页面
        2. 按热力值分组
        3. 根据热力值读取对应深度
        4. 组装上下文
        """
        # 1. 搜索所有相关页面
        all_relevant = self.search_all_relevant(query)

        if not all_relevant:
            return {"found": False, "message": "未找到相关知识"}

        # 2. 按热力值分组（统一为 cold/warm/hot）
        by_level: Dict[str, List[Dict[str, Any]]] = {
            "hot": [],
            "warm": [],
            "cold": [],
            "unknown": [],
        }

        for item in all_relevant:
            level = item["heat_level"]
            if level == "hot":
                by_level["hot"].append(item)
            elif level == "warm":
                by_level["warm"].append(item)
            elif level == "cold":
                by_level["cold"].append(item)
            else:
                by_level["unknown"].append(item)

        # 3. 根据热力值读取对应深度
        results: Dict[str, Any] = {
            "found": True,
            "query": query,
            "total_pages": len(all_relevant),
            "by_heat_level": {},
            "context": [],
        }

        # 按优先级读取（高热力优先）
        for level_group in ["hot", "warm", "cold", "unknown"]:
            pages = by_level[level_group]
            if not pages:
                continue

            group_result: Dict[str, Any] = {
                "count": len(pages),
                "pages": [],
                "depth": self.READ_DEPTH.get(level_group, {}).get("desc", "未知"),
            }

            for item in pages:
                content = self.read_page_by_heat(item["page_id"])
                if content:
                    group_result["pages"].append(
                        {
                            "page_id": item["page_id"],
                            "title": item["title"],
                            "heat_level": item["heat_level"],
                            "heat_score": item["heat_score"],
                            "content": content,
                        }
                    )

            results["by_heat_level"][level_group] = group_result

            # 组装上下文（按热力值优先级）
            for page in group_result["pages"][:5]:  # 每个等级最多5个
                results["context"].append(self._format_page_for_context(page))

        return results

    def _format_page_for_context(self, page: Dict) -> str:
        """格式化页面为上下文字符串"""
        content = page["content"]
        title = content.get("title", page["title"])
        heat = page["heat_level"]

        lines = [
            f"### {title} [{heat}]",
            f"来源: [[{page['page_id']}]] | 热力值: {page['heat_score']}",
            "",
        ]

        if "content" in content:
            lines.append(content["content"][:2000])  # 限制长度
        elif "summary" in content:
            lines.append(content["summary"])
        elif "note" in content:
            lines.append(content["note"])

        lines.append("\n---\n")
        return "\n".join(lines)

    def should_read_wiki(self, user_message: str, threshold: int = 10) -> Tuple[bool, List[Dict]]:
        """
        判断是否应该读取Wiki

        Args:
            user_message: 用户输入
            threshold: 相关度阈值

        Returns:
            (是否需要读取, 所有相关页面列表)
        """
        all_relevant = self.search_all_relevant(user_message)

        if not all_relevant:
            return False, []

        # 检查最高相关度是否超过阈值
        max_score = all_relevant[0]["relevance_score"]
        should_read = max_score >= threshold

        return should_read, all_relevant

    def get_stats(self) -> Dict:
        """获取Wiki统计"""
        stats: Dict[str, Any] = {"total_pages": len(self.index), "by_type": {}, "by_heat_level": {}}

        for page_id, info in self.index.items():
            page_type = info["type"]
            heat_level = info["heat_level"]

            stats["by_type"][page_type] = stats["by_type"].get(page_type, 0) + 1
            stats["by_heat_level"][heat_level] = stats["by_heat_level"].get(heat_level, 0) + 1

        return stats


def main():
    """CLI入口"""
    import argparse

    parser = argparse.ArgumentParser(description="Wiki Reader - 热力值控制深度")
    parser.add_argument("--query", help="查询内容")
    parser.add_argument("--page", help="读取指定页面（按热力值深度）")
    parser.add_argument("--stats", action="store_true", help="显示统计")

    args = parser.parse_args()

    reader = WikiReader()

    if args.stats:
        stats = reader.get_stats()
        print("Wiki统计:")
        print(f"  总页面: {stats['total_pages']}")
        print(f"  按类型: {stats['by_type']}")
        print(f"  按热力: {stats['by_heat_level']}")

    elif args.page:
        content = reader.read_page_by_heat(args.page)
        if content:
            print(f"# {content.get('title', args.page)} [{content.get('heat_level')}]")
            print(f"深度: {content.get('depth')}")
            print(f"热力值: {content.get('heat_score', 0)}")
            print("\n内容:")
            if "content" in content:
                print(content["content"])
            elif "summary" in content:
                print(content["summary"])
            elif "note" in content:
                print(content["note"])
        else:
            print(f"页面未找到: {args.page}")

    elif args.query:
        should_read, relevant = reader.should_read_wiki(args.query)
        print(f"是否需要读取: {should_read}")
        print(f"找到 {len(relevant)} 个相关页面:\n")

        # 按热力值分组显示
        by_level = {}
        for item in relevant[:20]:  # 只显示前20
            level = item["heat_level"]
            if level not in by_level:
                by_level[level] = []
            by_level[level].append(item)

        for level in sorted(by_level.keys(), key=lambda x: -int(x[1:]) if x.startswith("L") else 0):
            pages = by_level[level]
            print(f"\n[{level}] {len(pages)}个页面:")
            for p in pages[:5]:
                print(f"  - {p['title']} (相关度:{p['relevance_score']}, 热力:{p['heat_score']})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
