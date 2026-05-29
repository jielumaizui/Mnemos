# Oracle — 神谕所 — Wiki 读取，查询知识库的接口
# 原模块: wiki_reader.py

#!/usr/bin/env python3
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

与Memos的区别：
- Memos: 有权限控制，用于上下文恢复
- Wiki: 无限制，热力值控制深度，用于知识查询
"""

import logging


import os
import sys
import re
import yaml
from pathlib import Path
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)
try:
    from core.wiki_metrics import get_default_metrics
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False


@dataclass
class WikiPage:
    """Wiki页面"""
    page_id: str
    title: str
    content: str
    frontmatter: Dict
    page_type: str  # entity/concept/source/synthesis
    heat_level: str  # cold/warm/hot
    heat_score: float
    entities: List[str]
    concepts: List[str]
    last_modified: datetime


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

    # 读取深度配置（简化为3级）
    READ_DEPTH = {
        "cold": {"type": "summary", "chars": 100, "related": False, "deep": False, "desc": "冷知识，摘要100字"},
        "warm": {"type": "paragraph", "chars": 500, "related": False, "deep": False, "desc": "温知识，段落500字"},
        "hot": {"type": "full", "chars": -1, "related": True, "deep": False, "desc": "热知识，全文+关联"},
    }

    def __init__(self, wiki_path: str = None):
        if wiki_path is None:
            from core.config import get_config
            self.wiki_path = get_config().wiki_dir
        else:
            self.wiki_path = Path(wiki_path).expanduser()
        self.metrics = get_default_metrics() if _METRICS_AVAILABLE else None
        self.index = {}  # 页面索引
        self._build_index()

    def _build_index(self):
        """构建Wiki索引（包含热力值）"""
        for subdir in ['00-Inbox', '01-People', '02-Projects', '03-Tech', '04-Concepts', '05-MOCs', '06-Retrospectives']:
            dir_path = self.wiki_path / subdir
            if not dir_path.exists():
                continue

            for file_path in dir_path.glob("*.md"):
                rel_path = file_path.relative_to(self.wiki_path)
                page_id = str(rel_path.with_suffix(''))

                # 快速索引（只读frontmatter）
                try:
                    fm = self._quick_read_frontmatter(file_path)
                    # 获取热力值
                    heat_info = self._get_heat_info(page_id)

                    self.index[page_id] = {
                        "type": subdir,
                        "title": fm.get("title", file_path.stem),
                        "entities": fm.get("entities", []),
                        "concepts": fm.get("concepts", []),
                        "path": file_path,
                        "heat_level": heat_info.get("level", "L1"),
                        "heat_score": heat_info.get("score", 0),
                    }
                except Exception as e:
                    logger.warning(f"解析 frontmatter 失败: {e}")

    def _get_heat_info(self, page_id: str) -> Dict:
        """从 wiki_metrics 获取页面热力信息"""
        if self.metrics:
            page = self.metrics.get_page(page_id)
            if page:
                return {"level": page.heat_level, "score": page.heat_score}
        return {"level": "cold", "score": 0.0}

    def _quick_read_frontmatter(self, file_path: Path) -> Dict:
        """快速读取frontmatter"""
        try:
            content = file_path.read_text(encoding='utf-8', errors='ignore')
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    return yaml.safe_load(parts[1]) or {}
            return {}
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at oracle.py", exc_info=True)
            return {}

    def _parse_content(self, content: str) -> Tuple[Dict, str]:
        """解析frontmatter和正文"""
        if content.startswith('---'):
            parts = content.split('---', 2)
            if len(parts) >= 3:
                try:
                    fm = yaml.safe_load(parts[1]) or {}
                    body = parts[2].strip()
                    return fm, body
                except Exception as e:
                    logger.warning(f"快速解析 frontmatter 失败: {e}")
        return {}, content

    def _read_by_depth(self, page_id: str, heat_level: str) -> Optional[Dict]:
        """
        根据热力等级读取对应深度

        核心逻辑：热力值决定读取深度
        """
        if page_id not in self.index:
            return None

        info = self.index[page_id]
        file_path = info["path"]

        try:
            content = file_path.read_text(encoding='utf-8')
            frontmatter, body = self._parse_content(content)
        except Exception as e:
            logger.warning(f"读取页面内容失败: {e}")
            return None

        config = self.READ_DEPTH.get(heat_level, self.READ_DEPTH["L1"])
        read_type = config["type"]

        if read_type == "metadata":
            # L0：只返回元数据
            return {
                "title": frontmatter.get("title", page_id),
                "tags": frontmatter.get("tags", []),
                "entities": frontmatter.get("entities", []),
                "concepts": frontmatter.get("concepts", []),
                "heat_level": heat_level,
                "heat_score": info["heat_score"],
                "note": "沉睡知识，低活跃度，可唤醒",
                "depth": "metadata_only"
            }

        elif read_type == "summary":
            # L1-L3：返回前N字摘要
            max_chars = config["chars"]
            summary = body[:max_chars] if len(body) > max_chars else body
            if len(body) > max_chars:
                summary += "..."
            return {
                "summary": summary,
                "title": frontmatter.get("title", page_id),
                "heat_level": heat_level,
                "heat_score": info["heat_score"],
                "depth": f"summary_{max_chars}"
            }

        elif read_type == "paragraph":
            # L4-L6：返回关键段落
            max_chars = config["chars"]
            content = body[:max_chars] if len(body) > max_chars else body

            result = {
                "content": content,
                "title": frontmatter.get("title", page_id),
                "entities": frontmatter.get("entities", []),
                "concepts": frontmatter.get("concepts", []),
                "heat_level": heat_level,
                "heat_score": info["heat_score"],
                "depth": f"paragraph_{max_chars}"
            }

            # L6 + 关联
            if config["related"]:
                result["related"] = self._get_related_pages(page_id, limit=3)

            return result

        elif read_type == "full":
            # L7-L8：全文 + 关联
            result = {
                "content": body,
                "title": frontmatter.get("title", page_id),
                "entities": frontmatter.get("entities", []),
                "concepts": frontmatter.get("concepts", []),
                "heat_level": heat_level,
                "heat_score": info["heat_score"],
                "depth": "full",
                "related": self._get_related_pages(page_id, limit=5) if config["related"] else []
            }
            return result

        elif read_type == "full_plus":
            # L9：全文 + 深度追踪
            result = {
                "content": body,
                "title": frontmatter.get("title", page_id),
                "entities": frontmatter.get("entities", []),
                "concepts": frontmatter.get("concepts", []),
                "heat_level": heat_level,
                "heat_score": info["heat_score"],
                "depth": "full_plus",
                "related": self._get_related_pages(page_id, limit=5),
                "deep_traced": True,
                "note": "核心知识，深度追踪"
            }
            return result

        return None

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
                    related.append({
                        "page_id": p,
                        "relation": f"entity:{entity}",
                        "type": "entity_link"
                    })

        # 通过concepts查找
        for concept in info["concepts"][:3]:
            pages = self._find_pages_by_concept(concept)
            for p in pages[:2]:
                if p != page_id and p not in [r["page_id"] for r in related]:
                    related.append({
                        "page_id": p,
                        "relation": f"concept:{concept}",
                        "type": "concept_link"
                    })

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
                results.append({
                    "page_id": page_id,
                    "title": info["title"],
                    "type": info["type"],
                    "heat_level": info["heat_level"],
                    "heat_score": info["heat_score"],
                    "relevance_score": score,
                    "reasons": reasons
                })

        # 按热力值和关联度双重排序
        # 优先热力值高，其次关联度
        results.sort(key=lambda x: (x["heat_score"], x["relevance_score"]), reverse=True)
        return results

    def read_page_by_heat(self, page_id: str) -> Optional[Dict]:
        """
        根据页面热力值自动读取对应深度

        这是核心方法：热力值决定读取深度
        """
        if page_id not in self.index:
            return None

        heat_level = self.index[page_id]["heat_level"]
        return self._read_by_depth(page_id, heat_level)

    def get_knowledge(self, query: str, include_related: bool = True) -> Dict:
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

        # 2. 按热力值分组
        by_level = {
            "L9": [],
            "L7-L8": [],
            "L4-L6": [],
            "L1-L3": [],
            "L0": []
        }

        for item in all_relevant:
            level = item["heat_level"]
            if level == "L9":
                by_level["L9"].append(item)
            elif level in ["L7", "L8"]:
                by_level["L7-L8"].append(item)
            elif level in ["L4", "L5", "L6"]:
                by_level["L4-L6"].append(item)
            elif level in ["L1", "L2", "L3"]:
                by_level["L1-L3"].append(item)
            else:
                by_level["L0"].append(item)

        # 3. 根据热力值读取对应深度
        results = {
            "found": True,
            "query": query,
            "total_pages": len(all_relevant),
            "by_heat_level": {},
            "context": []
        }

        # 按优先级读取（高热力优先）
        for level_group in ["L9", "L7-L8", "L4-L6", "L1-L3", "L0"]:
            pages = by_level[level_group]
            if not pages:
                continue

            group_result = {
                "count": len(pages),
                "pages": [],
                "depth": self.READ_DEPTH.get(level_group.split("-")[0], {}).get("desc", "未知")
            }

            for item in pages:
                content = self.read_page_by_heat(item["page_id"])
                if content:
                    group_result["pages"].append({
                        "page_id": item["page_id"],
                        "title": item["title"],
                        "heat_level": item["heat_level"],
                        "heat_score": item["heat_score"],
                        "content": content
                    })

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
            ""
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
        stats = {
            "total_pages": len(self.index),
            "by_type": {},
            "by_heat_level": {}
        }

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
        print(f"Wiki统计:")
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
