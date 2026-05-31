"""
CrossAgentLinker — 跨 Agent 知识关联器

【E14 全库修复】蒸馏生成新页面后，检测其他 Agent 是否有相似主题页面，
在原页面添加双向链接，不生成合成页。

设计来源：08-功能梳理/34-跨Agent知识关联.md
ADR-019: 优先 hnswlib 双索引融合检索，DNAEngine/SimHash 作为兼容降级。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

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


@dataclass
class WikiPage:
    """用于跨 Agent 分歧检测的轻量页面表示"""
    path: Path
    frontmatter: Dict
    content: str


@dataclass
class AgentConclusion:
    """单个 Agent 在某个主题上的结论"""
    agent: str
    session_id: str
    date_str: str
    conclusion: str
    confidence: float
    source_page: str


@dataclass
class DivergenceReport:
    """跨 Agent 认知分歧报告"""
    topic: str
    divergences: List[AgentConclusion]
    severity: str
    detected_at: datetime

    def to_push_message(self) -> str:
        lines = [f"关于 **{self.topic}**，不同 Agent 给出了不同建议："]
        for item in self.divergences:
            date = f"（{item.date_str}）" if item.date_str else ""
            lines.append(f"- {item.agent}{date}：{item.conclusion}")
        lines.append("\n需要我对比一下两种方案的利弊吗？")
        return "\n".join(lines)


class CrossAgentLinker:
    """
    跨 Agent 知识关联器

    在蒸馏生成新页面后，检测其他 Agent 是否有相似主题页面，
    在原页面添加双向链接，不生成合成页。
    """

    VECTOR_SIMILARITY_THRESHOLD = 0.75
    DNA_SIMILARITY_THRESHOLD = 0.30
    MAX_LINKS_PER_PAGE = 3
    WORKSPACE_NAMES = ["claude", "hermes", "kimi", "codex", "gpt", "shared"]

    def __init__(self, wiki_root: Path = None, vector_index=None):
        self.wiki_root = wiki_root or get_config().wiki_dir
        self.vector_index = vector_index
        self.dna = None
        self._keyword_index: Optional[Dict[str, List[Tuple[Path, str]]]] = None

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

    def handle_event(self, event_type: str, data: Dict) -> List[LinkAction]:
        """事件总线入口：蒸馏完成后自动建立跨 Agent 关联"""
        if event_type not in {"distill_complete", "knowledge_distilled", "page.created"}:
            return []
        page_path = data.get("page_path") or data.get("path") or data.get("wiki_path")
        if not page_path:
            return []
        return self.link_after_distill(Path(page_path))

    # ── 内部方法 ──

    def _build_keyword_index(self) -> Dict[str, List[Tuple[Path, str]]]:
        """构建关键词倒排索引：keyword -> [(page_path, agent), ...]

        扫描主题分类目录 + 00-Inbox，按 frontmatter 中的「关键词」字段建立索引。
        索引缓存在实例变量中，避免重复全量扫描。
        """
        if self._keyword_index is not None:
            return self._keyword_index

        index: Dict[str, List[Tuple[Path, str]]] = {}
        search_dirs = [
            "01-People", "02-Projects", "03-Tech", "04-Concepts",
            "05-MOCs", "06-Retrospectives", "07-Shadow", "00-Inbox",
        ] + self.WORKSPACE_NAMES
        for d in search_dirs:
            dir_path = self.wiki_root / d
            if not dir_path.exists():
                continue
            for md_file in dir_path.rglob("*.md"):
                try:
                    fm = self._read_frontmatter(md_file)
                    keywords = fm.get("关键词", [])
                    if isinstance(keywords, str):
                        try:
                            import json
                            keywords = json.loads(keywords)
                        except Exception:
                            keywords = [keywords]
                    if not isinstance(keywords, list):
                        continue
                    agent = self._extract_agent_from_path(md_file) or "unknown"
                    for kw in keywords:
                        kw_clean = str(kw).strip().lower()
                        if kw_clean:
                            index.setdefault(kw_clean, []).append((md_file, agent))
                except Exception:
                    continue

        self._keyword_index = index
        logger.info(f"[CrossAgentLinker] 关键词索引构建完成: {len(index)} 个关键词, "
                    f"{sum(len(v) for v in index.values())} 条映射")
        return index

    def _find_similar_by_keywords(self, page_path: Path,
                                  exclude_agent: str) -> List[Tuple[Path, float]]:
        """基于关键词倒排索引 + Jaccard 系数查找跨 Agent 相似页面"""
        fm = self._read_frontmatter(page_path)
        page_keywords = fm.get("关键词", [])
        if isinstance(page_keywords, str):
            try:
                import json
                page_keywords = json.loads(page_keywords)
            except Exception:
                page_keywords = [page_keywords]
        if not isinstance(page_keywords, list):
            return []

        page_keywords = [str(k).strip().lower() for k in page_keywords if str(k).strip()]
        if not page_keywords:
            return []

        keyword_index = self._build_keyword_index()

        # 查倒排索引收集候选页面
        candidate_counts: Dict[Path, Tuple[str, int]] = {}  # path -> (agent, overlap_count)
        for kw in page_keywords:
            for cand_path, cand_agent in keyword_index.get(kw, []):
                if cand_path == page_path or cand_agent == exclude_agent:
                    continue
                _, count = candidate_counts.get(cand_path, (cand_agent, 0))
                candidate_counts[cand_path] = (cand_agent, count + 1)

        # 计算 Jaccard 系数
        results = []
        for cand_path, (cand_agent, overlap) in candidate_counts.items():
            try:
                cand_fm = self._read_frontmatter(cand_path)
                cand_keywords = cand_fm.get("关键词", [])
                if isinstance(cand_keywords, str):
                    try:
                        import json
                        cand_keywords = json.loads(cand_keywords)
                    except Exception:
                        cand_keywords = [cand_keywords]
                if not isinstance(cand_keywords, list):
                    continue
                cand_keywords = [str(k).strip().lower() for k in cand_keywords if str(k).strip()]
                union = len(set(page_keywords) | set(cand_keywords))
                if union == 0:
                    continue
                score = overlap / union
                if score >= 0.05:  # Jaccard 阈值 — 关键词更稀疏，允许更低的重叠
                    results.append((cand_path, score))
            except Exception:
                continue

        return sorted(results, key=lambda x: x[1], reverse=True)

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
                    filters={
                        "workspace": [
                            w for w in self.WORKSPACE_NAMES
                            if w != exclude_agent and w != "shared"
                        ]
                    },
                    top_k=self.MAX_LINKS_PER_PAGE * 3,
                )
                for r in search_results:
                    md_file = Path(r["path"])
                    score = float(r.get("score", 0))
                    if score < self.VECTOR_SIMILARITY_THRESHOLD:
                        continue
                    if md_file.exists() and md_file != page_path:
                        other_agent = self._extract_agent_from_path(md_file)
                        if other_agent and other_agent != exclude_agent:
                            results.append((md_file, score))
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at cross_agent_linker.py", exc_info=True)
                pass

        # 方案 B：关键词倒排索引（替代 DNAEngine/SimHash O(n²) 降级）
        if not results:
            results = self._find_similar_by_keywords(page_path, exclude_agent)

        # 方案 C：文本相似度最终兜底（限制在同名文件附近，避免全库扫描）
        if not results:
            stem = page_path.stem.lower()
            for search_path in [self.wiki_root / ws for ws in self.WORKSPACE_NAMES]:
                if not search_path.exists():
                    continue
                for md_file in search_path.rglob("*.md"):
                    if md_file == page_path:
                        continue
                    other_agent = self._extract_agent_from_path(md_file)
                    if not other_agent or other_agent == exclude_agent:
                        continue
                    # 只在同名或高度相关文件中搜索
                    if stem not in md_file.stem.lower() and md_file.stem.lower() not in stem:
                        continue
                    try:
                        score = self._text_similarity(page_path, md_file)
                        if score >= self.DNA_SIMILARITY_THRESHOLD:
                            results.append((md_file, score))
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

    def _get_dna(self) -> Optional[DNAEngine]:
        """懒加载 DNAEngine，避免旧库结构问题阻断向量路径"""
        if self.dna is not None:
            return self.dna
        try:
            self.dna = DNAEngine()
            return self.dna
        except Exception as exc:
            logger.warning("DNAEngine 初始化失败，跳过跨 Agent 降级检索: %s", exc)
            return None

    def _text_similarity(self, left: Path, right: Path) -> float:
        """DNA 不可用时的零依赖文本相似度降级"""
        import re

        try:
            left_text = left.read_text(encoding="utf-8")[:2000].lower()
            right_text = right.read_text(encoding="utf-8")[:2000].lower()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at cross_agent_linker.py", exc_info=True)
            return 0.0
        left_terms = set(re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fa5]{2,4}", left_text))
        right_terms = set(re.findall(r"[a-z0-9_]{3,}|[\u4e00-\u9fa5]{2,4}", right_text))
        if not left_terms or not right_terms:
            return 0.0
        score = len(left_terms & right_terms) / len(left_terms | right_terms)
        if left.stem.lower() == right.stem.lower():
            score += 0.2
        return min(1.0, score)

    def _extract_agent_from_path(self, page_path: Path) -> Optional[str]:
        """从页面路径提取 agent 来源 — 支持 workspace 和 Inbox 路径"""
        try:
            rel = page_path.relative_to(self.wiki_root)
            first_part = rel.parts[0].lower()
            if first_part in self.WORKSPACE_NAMES:
                return first_part
        except ValueError:
            pass

        # frontmatter 推断（优先）
        try:
            fm = self._read_frontmatter(page_path)
            # 先查蓝图标准字段 "来源"
            agent = fm.get("来源") or fm.get("source")
            if agent and agent != "unknown":
                return agent.lower()
            # 兼容旧字段
            agent = fm.get("source_agent")
            if agent:
                return agent.lower()
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at cross_agent_linker.py", exc_info=True)
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
        """读取页面 frontmatter（支持 YAML 列表/对象）"""
        content = page_path.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return {}
        try:
            _, yaml_block, _ = content.split("---", 2)
            return yaml.safe_load(yaml_block.strip()) or {}
        except Exception:
            # 降级：简单 key: value 解析
            fm = {}
            try:
                _, yaml_block, _ = content.split("---", 2)
                for line in yaml_block.strip().split("\n"):
                    if ":" in line and not line.strip().startswith("-"):
                        k, v = line.split(":", 1)
                        fm[k.strip()] = v.strip()
            except Exception:
                pass
            return fm


class CrossAgentDivergenceDetector:
    """
    跨 Agent 认知分歧检测器。

    v1 采用保守规则：只在不同 Agent 的结论出现明确方向对立时报告分歧，
    避免把互补建议误判为冲突。
    """

    CONTRADICTION_PAIRS = [
        ("sentinel", "cluster"),
        ("单体", "微服务"),
        ("同步", "异步"),
        ("乐观锁", "悲观锁"),
        ("行锁", "表锁"),
        ("拉", "推"),
        ("客户端", "服务端"),
        ("集中式", "分布式"),
    ]

    NEGATION_SIGNALS = ["不要", "不推荐", "避免", "有问题", "缺点", "不适合"]

    def __init__(self, wiki_root: Path = None):
        self.wiki_root = wiki_root or get_config().wiki_dir
        self.linker = CrossAgentLinker(wiki_root=self.wiki_root)

    def detect(self, topic: str) -> Optional[DivergenceReport]:
        pages = self._find_pages_by_topic(topic)
        conclusions = []
        for page in pages:
            agent = page.frontmatter.get("source_agent") or self.linker._extract_agent_from_path(page.path)
            if not agent:
                continue
            conclusion = self._extract_conclusion(page)
            if not conclusion:
                continue
            conclusions.append(AgentConclusion(
                agent=str(agent).lower(),
                session_id=self._first_session_id(page.frontmatter.get("source_sessions")),
                date_str=self._format_date(page.frontmatter.get("distilled_at", "")),
                conclusion=conclusion,
                confidence=float(page.frontmatter.get("confidence", 0.5) or 0.5),
                source_page=str(page.path),
            ))

        conclusions = self._dedup_by_agent(conclusions)
        if len(conclusions) < 2 or not self._are_contradictory(conclusions):
            return None

        return DivergenceReport(
            topic=topic,
            divergences=conclusions,
            severity=self._classify_severity(topic, conclusions),
            detected_at=datetime.now(),
        )

    def _find_pages_by_topic(self, topic: str) -> List[WikiPage]:
        topic_lower = topic.lower()
        pages = []
        for md_file in self.wiki_root.rglob("*.md"):
            if any(part.startswith(".") for part in md_file.relative_to(self.wiki_root).parts):
                continue
            try:
                content = md_file.read_text(encoding="utf-8")
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at cross_agent_linker.py", exc_info=True)
                continue
            fm = self.linker._read_frontmatter(md_file)
            haystack = " ".join([
                md_file.stem,
                str(fm.get("topic", "")),
                str(fm.get("title", "")),
                str(fm.get("tags", "")),
                content[:2000],
            ]).lower()
            if topic_lower in haystack:
                pages.append(WikiPage(md_file, fm, content))
        return pages

    def _extract_conclusion(self, page: WikiPage) -> Optional[str]:
        for key in ["decision", "conclusion", "recommendation"]:
            value = page.frontmatter.get(key)
            if value:
                return str(value).strip()[:200]

        for heading in ["## 决策", "## 结论", "## 推荐", "## Decision", "## Conclusion"]:
            if heading not in page.content:
                continue
            start = page.content.find(heading) + len(heading)
            end = page.content.find("\n## ", start)
            if end == -1:
                end = len(page.content)
            paragraph = page.content[start:end].strip()
            for line in paragraph.splitlines():
                clean = line.strip(" -\t")
                if clean:
                    return clean[:200]
        return None

    def _dedup_by_agent(self, conclusions: List[AgentConclusion]) -> List[AgentConclusion]:
        latest = {}
        for item in conclusions:
            current = latest.get(item.agent)
            if current is None or item.date_str >= current.date_str:
                latest[item.agent] = item
        return list(latest.values())

    def _are_contradictory(self, conclusions: List[AgentConclusion]) -> bool:
        texts = [item.conclusion.lower() for item in conclusions]
        for left, right in self.CONTRADICTION_PAIRS:
            if any(left.lower() in text for text in texts) and any(right.lower() in text for text in texts):
                return True

        for i, left in enumerate(texts):
            for j, right in enumerate(texts):
                if i == j:
                    continue
                if any(signal in right for signal in self.NEGATION_SIGNALS):
                    terms = self._technical_terms(left)
                    if any(term in right for term in terms):
                        return True
        return False

    def _classify_severity(self, topic: str, conclusions: List[AgentConclusion]) -> str:
        high_keywords = ["集群", "架构", "选型", "方案", "部署", "基础设施"]
        text = topic + " " + " ".join(item.conclusion for item in conclusions)
        if any(keyword in text for keyword in high_keywords):
            return "high"
        return "medium"

    def _technical_terms(self, text: str) -> List[str]:
        import re

        terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text)
        terms.extend(re.findall(r"[\u4e00-\u9fa5]{2,8}", text))
        stop = {"建议", "使用", "方案", "模式", "可以", "需要", "推荐"}
        return [term.lower() for term in terms if term.lower() not in stop]

    def _first_session_id(self, source_sessions) -> str:
        if isinstance(source_sessions, list) and source_sessions:
            first = source_sessions[0]
            if isinstance(first, dict):
                return str(first.get("session_id", ""))
            return str(first)
        return ""

    def _format_date(self, value: str) -> str:
        if not value:
            return ""
        raw = str(value).strip().strip('"')
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m-%d"):
            try:
                dt = datetime.strptime(raw[:len(fmt)], fmt)
                return f"{dt.month}/{dt.day}"
            except ValueError:
                continue
        return raw[:10]


class DivergencePushManager:
    """分歧推送冷却管理器"""

    PUSH_COOLDOWN_HOURS = 24

    def __init__(self):
        self._recent_pushes: Dict[str, datetime] = {}

    def should_push(self, report: DivergenceReport) -> bool:
        last = self._recent_pushes.get(report.topic)
        if not last:
            return True
        return datetime.now() - last >= timedelta(hours=self.PUSH_COOLDOWN_HOURS)

    def mark_pushed(self, topic: str):
        self._recent_pushes[topic] = datetime.now()
