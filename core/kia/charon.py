# Charon — 冥河渡夫 — 连接 Worker，摆渡数据于各系统之间
# 原模块: connect_worker.py

#!/usr/bin/env python3
import logging
"""
Connect Worker - L2 → L3 关联层：构建 Obsidian 知识图谱

核心设计（Obsidian 图谱最佳实践）：
    三层结构：
        L1 原始层: 00-Inbox/        — 原始 session 素材
        L2 节点层: 01-People/       — 人名实体
                   02-Projects/     — 项目实体
                   03-Tech/         — 技术栈实体
                   04-Concepts/     — 概念/方法论
        L3 枢纽层: 05-MOCs/         — 主题地图（Graph View 中心节点）

    关系网络：
        - 共现关系：同 session 中同时出现的实体
        - 引用关系：页面中的 [[wikilinks]]
        - MOC 聚合：MOC 页面聚合同类实体

    Graph View 效果：
        - MOC 页面是高连接度枢纽（星型中心）
        - 实体之间通过共现建立横向连接
        - Sources 通过 wikilinks 附着到知识网络上

用法：
    python3 core/connect_worker.py              # 执行一轮关联
    python3 core/connect_worker.py --watch      # 守护模式
"""

import os
import sys
import re
import json
import argparse
import time
import hashlib
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple, Optional
from core.config import get_config
logger = logging.getLogger(__name__)


def _get_wiki_dir():
    """Lazy-load wiki directory to avoid side effects at import time."""
    return get_config().wiki_dir


# Lazy module-level constants: evaluated on first access, not at import time
# This prevents get_config() from running when the module is merely imported
class _LazyPath:
    """Descriptor-like lazy path that evaluates get_config() only when accessed."""
    __slots__ = ('_segments',)
    def __init__(self, *segments):
        self._segments = segments
    def __truediv__(self, other):
        return _LazyPath(*self._segments, other)
    def __rtruediv__(self, other):
        raise NotImplementedError
    def _resolve(self):
        result = _get_wiki_dir()
        for seg in self._segments:
            result = result / seg
        return result
    def __str__(self):
        return str(self._resolve())
    def __repr__(self):
        return f"LazyPath({'/'.join(self._segments)})"
    def __fspath__(self):
        return str(self._resolve())
    def __getattr__(self, name):
        return getattr(self._resolve(), name)
    def __hash__(self):
        return hash(self._resolve())
    def __eq__(self, other):
        return self._resolve() == other
    def __iter__(self):
        return iter(self._resolve())


WIKI_DIR = _LazyPath()
INBOX_DIR    = _LazyPath("00-Inbox")
PEOPLE_DIR   = _LazyPath("01-People")
PROJECTS_DIR = _LazyPath("02-Projects")
TECH_DIR     = _LazyPath("03-Tech")
CONCEPTS_DIR = _LazyPath("04-Concepts")
MOCS_DIR     = _LazyPath("05-MOCs")
RETROS_DIR   = _LazyPath("06-Retrospectives")

ALL_DIRS = [INBOX_DIR, PEOPLE_DIR, PROJECTS_DIR, TECH_DIR, CONCEPTS_DIR, MOCS_DIR, RETROS_DIR]

# ========== 实体分类词典 ==========

TECH_KEYWORDS = {
    # 编程语言
    "python", "javascript", "typescript", "go", "golang", "rust", "java", "c++", "c#",
    "ruby", "php", "swift", "kotlin", "scala", "elixir", "haskell", "lua", "perl",
    # 前端框架
    "react", "vue", "angular", "svelte", "solidjs", "nextjs", "nuxt", "remix", "astro",
    # 后端框架
    "django", "flask", "fastapi", "tornado", "express", "koa", "nestjs", "spring",
    "laravel", "rails", "gin", "echo", "beego",
    # 数据库
    "postgresql", "mysql", "mariadb", "mongodb", "redis", "sqlite", "elasticsearch",
    "clickhouse", "timescaledb", "influxdb", "neo4j", "dynamodb", "cassandra",
    # 基础设施
    "docker", "kubernetes", "k8s", "terraform", "ansible", "pulumi", "vagrant",
    "jenkins", "github actions", "gitlab ci", "circleci", "travis ci",
    # 云平台
    "aws", "gcp", "azure", "aliyun", "tencent cloud", "cloudflare", "vercel", "netlify",
    # 移动端
    "react native", "flutter", "ionic", "cordova", "electron", "tauri",
    # AI/ML
    "tensorflow", "pytorch", "jax", "onnx", "scikit-learn", "pandas", "numpy",
    "matplotlib", "seaborn", "plotly", "opencv", "hugging face", "langchain",
    "llamaindex", "openai", "anthropic", "claude", "gpt", "gemini", "llama",
    # 工具链
    "webpack", "vite", "rollup", "esbuild", "parcel", "babel", "swc",
    "eslint", "prettier", "typescript compiler", "tsc",
    "git", "github", "gitlab", "bitbucket", "svn", "mercurial",
    # 系统
    "linux", "macos", "windows", "ubuntu", "debian", "centos", "fedora", "arch",
    "nginx", "apache", "traefik", "caddy", "haproxy",
    # 监控
    "prometheus", "grafana", "loki", "jaeger", "zipkin", "datadog", "newrelic",
    # 消息队列
    "kafka", "rabbitmq", "nats", "mqtt", "rocketmq", "pulsar",
    # 其他工具
    "memos", "obsidian", "notion", "logseq", "vscode", "vim", "neovim", "emacs",
    "cursor", "intellij", "pycharm", "webstorm", "postman", "insomnia",
    "微信小程序", "支付宝小程序", "抖音小程序", "uniapp", "taro",
}

CONCEPT_KEYWORDS = {
    # 架构
    "api", "rest", "graphql", "grpc", "websocket", "webhook", "soap",
    "microservice", "monolith", "serverless", "faas", "lambda", "edge computing",
    "cqrs", "event sourcing", "saga", "circuit breaker", "bulkhead",
    "crud", "mvc", "mvvm", "mvp", "clean architecture", "hexagonal architecture",
    "ddd", "domain driven design", "onion architecture",
    # 工程实践
    "ci/cd", "devops", "sre", "platform engineering", "gitops",
    "agile", "scrum", "kanban", "xp", "lean", "waterfall",
    "tdd", "bdd", "atdd", "ddd", "unit test", "integration test", "e2e test",
    "mutation testing", "property based testing",
    # 安全
    "oauth", "jwt", "sso", "ldap", "rbac", "abac", "zero trust",
    "authentication", "authorization", "encryption", "hash", "salting",
    "csrf", "xss", "sql injection", "mitm",
    # 性能
    "cache", "cdn", "load balancer", "reverse proxy", "rate limiting",
    "sharding", "partitioning", "replication", "indexing",
    "async", "sync", "concurrency", "parallelism", "threading", "coroutine",
    "event-driven", "message queue", "pub/sub", "stream processing",
    # 数据
    "etl", "elt", "data pipeline", "data warehouse", "data lake", "data mesh",
    "olap", "oltp", "cdc", "data lineage", "data governance",
    # 知识管理
    "知识库", "知识图谱", "wiki", "zettelkasten", "moc", "map of content",
    "复盘", "checklist", "sop", "模板", "最佳实践",
    # 产品
    "mvp", "pmf", "growth", "留存", "活跃", "转化", "漏斗", "ab测试",
    "用户画像", "用户旅程", "客户分层", "精细化运营",
    # 管理
    "okr", "kpi", "北极星指标", "okr", "敏捷", "迭代", "冲刺",
}

PROJECT_INDICATORS = {
    "项目", "project", "产品", "product", "应用", "app", "系统", "system",
    "平台", "platform", "服务", "service", "组件", "component", "模块", "module",
}

CHINESE_SURNAMES = {
    "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "徐", "孙",
    "胡", "朱", "高", "林", "何", "郭", "马", "罗", "梁", "宋", "郑", "谢",
    "韩", "唐", "冯", "于", "董", "萧", "程", "曹", "袁", "邓", "许", "傅",
    "沈", "曾", "彭", "吕", "苏", "卢", "蒋", "蔡", "贾", "丁", "魏", "薛",
    "叶", "阎", "余", "潘", "杜", "戴", "夏", "钟", "汪", "田", "任", "姜",
}


def _safe_filename(name: str) -> str:
    """生成安全的文件名（保留可读性）"""
    # 替换不安全的字符，但保留中文
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()
    # 限制长度
    if len(safe) > 60:
        hash_suffix = hashlib.md5(safe.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe[:60]}_{hash_suffix}"
    return safe or "untitled"


def _ensure_dirs():
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ========== 实体提取引擎 ==========

class EntityExtractor:
    """多维度实体提取器"""

    # 停用词：不应被提取为独立实体的系统术语、frontmatter 字段名、通用片段
    STOP_WORDS: set[str] = {
        # frontmatter 字段名（LLM 输出中常见，会被误提取）
        "名称", "领域", "摘要", "触发器", "别名", "跨agent关联", "标签推荐系统",
        "类型", "状态", "知识阶段", "来源数量", "证据级别", "置信度", "时效性",
        "创建日期", "关键词", "版本标记", "决策摘要", "合并来源", "提取方式",
        # 系统术语（单独出现时不应作为实体）
        "系统", "模块", "接口", "引擎", "服务", "组件", "数据库",
        "服务器", "客户端", "中间件", "微服务", "程序", "框架", "平台", "模型",
        "协议", "算法", "代码", "函数", "方法", "类", "对象", "变量",
        # 通用中文停用词/片段
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
        "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
        "着", "没有", "看", "好", "自己", "这", "那", "之", "与", "或", "及",
        "等", "中", "内", "外", "下", "前", "后", "时", "间", "地", "方",
        "法", "情", "理", "事", "实", "现", "当", "从", "把", "被", "给",
        "让", "向", "往", "于", "而", "却", "但是", "因为", "所以", "如果",
        "那么", "虽然", "而且", "或者", "还是", "只要", "只有", "除非", "假如",
        "例如", "比如", "像", "似乎", "也许", "大概", "大约", "差不多", "几乎",
        "根本", "简直", "完全", "绝对", "比较", "最", "更", "太", "非常", "特别",
        "十分", "极其", "相当", "颇", "挺", "怪", "老", "真", "够", "多么",
        "怎么", "怎样", "如何", "为什么", "为何", "难道", "别", "不要", "不能",
        "不会", "不可", "不得", "不该", "不必", "不用", "何必", "未必", "首先",
        "其次", "再次", "最后", "总之", "综上所述", "由此看来", "也就是说",
        "换句话说", "换言之", "简言之", "归根结底", "归根到底", "说到底",
    }

    def __init__(self, wiki_base: str | Path | None = None, bootstrap_from_existing: bool = True):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else Path(str(WIKI_DIR))
        self.tech_keywords = set(TECH_KEYWORDS)
        self.concept_keywords = set(CONCEPT_KEYWORDS)
        if bootstrap_from_existing:
            self._bootstrap_from_existing_pages()
        self.tech_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(t) for t in sorted(self.tech_keywords, key=len, reverse=True)) + r')\b',
            re.IGNORECASE
        )
        self.concept_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(c) for c in sorted(self.concept_keywords, key=len, reverse=True)) + r')\b',
            re.IGNORECASE
        )

    def _bootstrap_from_existing_pages(self):
        """从已生成实体页自举扩展词典。"""
        dir_map = {
            "tech": self.wiki_base / "03-Tech",
            "concepts": self.wiki_base / "04-Concepts",
        }
        for category, dir_path in dir_map.items():
            if not dir_path.exists():
                continue
            for md_file in dir_path.glob("*.md"):
                name = md_file.stem.strip()
                if not name:
                    continue
                if category == "tech":
                    self.tech_keywords.add(name.lower())
                elif category == "concepts":
                    self.concept_keywords.add(name.lower())

    def extract(self, text: str, cwd: str = "", git_branch: str = "") -> Dict[str, Set[str]]:
        """
        从文本中提取多类实体

        Returns:
            {
                "people": set(),
                "projects": set(),
                "tech": set(),
                "concepts": set(),
            }
        """
        text_lower = text.lower()
        result = {
            "people": set(),
            "projects": set(),
            "tech": set(),
            "concepts": set(),
        }

        # 1. 技术栈提取
        for match in self.tech_pattern.finditer(text_lower):
            result["tech"].add(match.group(1).lower())

        # 2. 概念提取
        for match in self.concept_pattern.finditer(text_lower):
            result["concepts"].add(match.group(1).lower())

        # 3. 代码块语言
        code_langs = re.findall(r'```(\w+)', text_lower)
        for lang in code_langs:
            if lang not in ('text', 'markdown', 'md', 'txt'):
                result["tech"].add(lang.lower())

        # 4. 项目名提取（从工作目录）
        if cwd:
            proj_name = Path(cwd).name
            if proj_name and proj_name not in ('.', '~', 'home', 'users'):
                result["projects"].add(proj_name)

        # 5. 项目名提取（从 git branch）
        if git_branch and git_branch not in ('main', 'master', 'dev', 'develop'):
            result["projects"].add(git_branch)

        # 6. 项目名提取（从文本中的项目声明）
        proj_matches = re.findall(
            r'(?:项目|project|产品|product|系统|system)[\s:：]+([\w\-一-鿿]+)',
            text
        )
        for m in proj_matches:
            if len(m) >= 2:
                result["projects"].add(m.strip())
        result["projects"].update(self.extract_chinese_projects(text))

        # 7. 人名提取（英文：First Last 格式）
        name_pattern = re.compile(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b')
        for match in name_pattern.finditer(text):
            name = match.group(0)
            # 过滤掉常见非人名
            if name.lower() not in ('i am', 'it is', 'we are', 'you are', 'the', 'this', 'that'):
                result["people"].add(name)
        result["people"].update(self.extract_chinese_names(text))

        # 8. 中文字符专有名词（技术相关）
        zh_terms = re.findall(r'[一-鿿]{2,6}', text)
        for term in zh_terms:
            tech_indicators = ['程序', '框架', '服务', '系统', '平台', '引擎', '模型',
                             '算法', '接口', '协议', '数据库', '服务器', '客户端',
                             '组件', '模块', '中间件', '微服务']
            if any(ind in term for ind in tech_indicators):
                result["tech"].add(term)

        # 9. URL 中的项目/服务名
        url_pattern = re.compile(r'https?://(?:github\.com|gitlab\.com)/([^/\s]+)/([^/\s]+)')
        for match in url_pattern.finditer(text):
            org, repo = match.groups()
            result["projects"].add(repo)
            result["people"].add(org)  # org 也可能是个人账号

        # 10. package.json / requirements 等提到的库名
        lib_pattern = re.compile(r'["\']([\w\-@/]+)["\']\s*[:：]')
        for match in lib_pattern.finditer(text):
            lib = match.group(1)
            if '/' in lib:  # scoped package
                lib = lib.split('/')[-1]
            if len(lib) >= 2 and not lib.startswith('http'):
                result["tech"].add(lib.lower())

        # 11. 停用词过滤：防止系统术语、frontmatter 字段名、通用片段被当成实体
        for category in result:
            result[category] = {
                item for item in result[category]
                if item.lower() not in self.STOP_WORDS
            }

        return result

    def extract_chinese_names(self, text: str) -> Set[str]:
        """中文人名：百家姓 + 1-2 字名，并用局部上下文降噪。"""
        names = set()
        indicators = ["说", "认为", "提到", "建议", "负责", "和", "与", "找", "问", "告诉"]
        for match in re.finditer(r"([一-鿿]{2,3})", text):
            name = match.group(1)
            if len(name) == 3 and name[-1] in {"说", "认", "提", "建", "负", "和", "与", "找", "问", "告"}:
                name = name[:2]
            if name[0] not in CHINESE_SURNAMES:
                continue
            context = text[max(0, match.start() - 4):min(len(text), match.end() + 4)]
            if any(ind in context for ind in indicators):
                names.add(name)
        return names

    def extract_chinese_projects(self, text: str) -> Set[str]:
        """中文项目名：识别 项目/产品/系统/平台/应用 + 名称。"""
        projects = set()
        patterns = [
            r"(?:项目|产品|系统|平台|应用)[\s:：「『\"']+([\w\-一-鿿]{2,20})(?:[」』\"'\s，。,.]|$)",
            r"(?:代号|codename)[\s:：]+([\w\-一-鿿]{2,10})(?:[\s，。,.]|$)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                projects.add(match.group(1).strip())
        return projects


# ========== 关系引擎 ==========

class RelationEngine:
    """关系分析和建立"""

    def __init__(self, half_life_days: int = 30, db_path: str | Path | None = None):
        self.half_life_days = half_life_days
        self.decay_lambda = math.log(2) / max(half_life_days, 1)
        self.co_occurrence = defaultdict(lambda: defaultdict(float))
        self.entity_docs = defaultdict(list)  # entity -> [doc_paths]
        self.db_path = Path(db_path).expanduser() if db_path else None
        if self.db_path:
            self._init_db()

    def analyze_session(self, doc_path: str, entities: Dict[str, Set[str]], timestamp: datetime = None):
        """分析单个 session 中的所有实体共现"""
        timestamp = timestamp or datetime.now()
        age_days = max((datetime.now() - timestamp).total_seconds() / 86400, 0)
        weight = round(math.exp(-self.decay_lambda * age_days), 4)

        all_entities = set()
        for category, items in entities.items():
            all_entities.update(items)

        self.entity_docs[doc_path].extend(all_entities)

        # 共现统计
        entities_list = list(all_entities)
        for i, e1 in enumerate(entities_list):
            for e2 in entities_list[i + 1:]:
                if e1 != e2:
                    self.co_occurrence[e1][e2] += weight
                    self.co_occurrence[e2][e1] += weight
                    self._persist_relation(e1, e2, weight, timestamp)

    def get_relations(self, entity: str, min_count: float = 1.0) -> List[Tuple[str, float]]:
        """获取实体的关系列表"""
        relations = self.co_occurrence.get(entity, {})
        return sorted(
            [(e, round(c, 3)) for e, c in relations.items() if c >= min_count],
            key=lambda x: x[1],
            reverse=True
        )

    def get_related_sessions(self, entity: str) -> List[str]:
        """获取提及该实体的所有文档"""
        sessions = []
        for doc_path, entities in self.entity_docs.items():
            if entity in entities:
                sessions.append(doc_path)
        return sessions

    def decrement(self, e1: str, e2: str, amount: float = 1.0):
        self.co_occurrence[e1][e2] = max(0.0, self.co_occurrence[e1][e2] - amount)
        self.co_occurrence[e2][e1] = max(0.0, self.co_occurrence[e2][e1] - amount)
        if self.db_path:
            self._decrement_persisted_relation(e1, e2, amount)

    def get_weight(self, e1: str, e2: str) -> float:
        return float(self.co_occurrence.get(e1, {}).get(e2, 0.0))

    def get_total_mentions(self, entity: str) -> int:
        return sum(1 for entities in self.entity_docs.values() if entity in entities)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS co_occurrence_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_a TEXT NOT NULL,
                    entity_b TEXT NOT NULL,
                    co_occurrence_count INTEGER DEFAULT 0,
                    weight REAL DEFAULT 0,
                    session_count INTEGER DEFAULT 0,
                    first_seen TEXT,
                    last_seen TEXT,
                    UNIQUE(entity_a, entity_b)
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(co_occurrence_relations)")}
            if "weight" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN weight REAL DEFAULT 0")
            if "session_count" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN session_count INTEGER DEFAULT 0")
            if "first_seen" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN first_seen TEXT")
            if "last_seen" not in columns:
                conn.execute("ALTER TABLE co_occurrence_relations ADD COLUMN last_seen TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_coocc_entity_a ON co_occurrence_relations(entity_a)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_coocc_entity_b ON co_occurrence_relations(entity_b)")

    def _persist_relation(self, e1: str, e2: str, weight: float, timestamp: datetime):
        if not self.db_path:
            return
        entity_a, entity_b = sorted([e1, e2])
        now = timestamp.isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                INSERT INTO co_occurrence_relations
                    (entity_a, entity_b, co_occurrence_count, weight, session_count, first_seen, last_seen)
                VALUES (?, ?, 1, ?, 1, ?, ?)
                ON CONFLICT(entity_a, entity_b) DO UPDATE SET
                    co_occurrence_count = co_occurrence_count + 1,
                    weight = weight + excluded.weight,
                    session_count = session_count + 1,
                    last_seen = excluded.last_seen
            """, (entity_a, entity_b, weight, now, now))

    def _decrement_persisted_relation(self, e1: str, e2: str, amount: float):
        entity_a, entity_b = sorted([e1, e2])
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                UPDATE co_occurrence_relations
                SET weight = MAX(weight - ?, 0),
                    co_occurrence_count = MAX(co_occurrence_count - 1, 0)
                WHERE entity_a=? AND entity_b=?
            """, (amount, entity_a, entity_b))


# ========== 页面生成器 ==========

def generate_person_page(name: str, relations: List[Tuple[str, int]],
                         sessions: List[str]) -> str:
    """生成人物页面"""
    safe_name = _safe_filename(name)

    lines = [
        "---",
        "type: person",
        f"name: {name}",
        f"heat: {len(sessions)}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {name}",
        "",
        "## 相关实体",
        "",
    ]

    for entity, count in relations[:15]:
        lines.append(f"- [[{_safe_filename(entity)}|{entity}]] — 共现 {count} 次")

    if sessions:
        lines.extend(["", "## 参与的 Session", ""])
        for sid in sessions[:10]:
            lines.append(f"- [[{sid[:40]}]]")

    return "\n".join(lines), safe_name


def generate_project_page(name: str, relations: List[Tuple[str, int]],
                          sessions: List[str], tech_stack: Set[str]) -> str:
    """生成项目页面"""
    safe_name = _safe_filename(name)

    lines = [
        "---",
        "type: project",
        f"name: {name}",
        f"heat: {len(sessions)}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {name}",
        "",
    ]

    if tech_stack:
        lines.extend(["## 技术栈", ""])
        for tech in sorted(tech_stack)[:20]:
            lines.append(f"- [[{_safe_filename(tech)}|{tech}]]")
        lines.append("")

    if relations:
        lines.extend(["## 相关实体", ""])
        for entity, count in relations[:15]:
            lines.append(f"- [[{_safe_filename(entity)}|{entity}]] — 共现 {count} 次")

    if sessions:
        lines.extend(["", "## 相关 Session", ""])
        for sid in sessions[:10]:
            lines.append(f"- [[{sid[:40]}]]")

    return "\n".join(lines), safe_name


def generate_tech_page(name: str, relations: List[Tuple[str, int]],
                       sessions: List[str]) -> str:
    """生成技术栈页面"""
    safe_name = _safe_filename(name)

    lines = [
        "---",
        "type: technology",
        f"name: {name}",
        f"heat: {len(sessions)}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {name}",
        "",
    ]

    # 推断关系类型
    deps = []
    alternatives = []
    used_by = []

    for entity, count in relations[:20]:
        ent_lower = entity.lower()
        # 简单启发式分类
        if any(d in ent_lower for d in ['db', 'database', 'sql', 'redis']):
            deps.append((entity, count))
        elif any(f in ent_lower for f in ['framework', '库', 'sdk']):
            deps.append((entity, count))
        else:
            used_by.append((entity, count))

    if deps:
        lines.extend(["## 相关技术/依赖", ""])
        for entity, count in deps[:10]:
            lines.append(f"- [[{_safe_filename(entity)}|{entity}]]")
        lines.append("")

    if used_by:
        lines.extend(["## 使用场景", ""])
        for entity, count in used_by[:10]:
            lines.append(f"- [[{_safe_filename(entity)}|{entity}]] — 共现 {count} 次")

    if sessions:
        lines.extend(["", "## 相关 Session", ""])
        for sid in sessions[:10]:
            lines.append(f"- [[{sid[:40]}]]")

    return "\n".join(lines), safe_name


def generate_concept_page(name: str, relations: List[Tuple[str, int]],
                          sessions: List[str]) -> str:
    """生成概念页面"""
    safe_name = _safe_filename(name)

    lines = [
        "---",
        "type: concept",
        f"name: {name}",
        f"heat: {len(sessions)}",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        f"# {name}",
        "",
        "## 定义",
        "",
        f"（从 {len(sessions)} 个 session 中自动提取的概念）",
        "",
    ]

    if relations:
        lines.extend(["## 相关概念 & 实体", ""])
        for entity, count in relations[:15]:
            lines.append(f"- [[{_safe_filename(entity)}|{entity}]] — 共现 {count} 次")

    if sessions:
        lines.extend(["", "## 出现过的 Session", ""])
        for sid in sessions[:10]:
            lines.append(f"- [[{sid[:40]}]]")

    return "\n".join(lines), safe_name


# ========== MOC 生成器 ==========

def generate_moc_pages(all_entities: Dict[str, Dict[str, Set[str]]]) -> int:
    """生成主题地图（MOC）页面 — Graph View 的枢纽节点"""
    count = 0

    # 1. 技术 MOC
    tech_items = all_entities.get("tech", {})
    if tech_items:
        lines = [
            "---",
            "type: MOC",
            "category: technology",
            "tags: [tech, MOC]",
            "---",
            "",
            "# 技术栈总览（MOC）",
            "",
            "## 编程语言",
            "",
        ]
        langs = {"python", "javascript", "typescript", "go", "golang", "rust", "java", "c++", "c#",
                 "ruby", "php", "swift", "kotlin", "scala"}
        for item in sorted(tech_items.keys()):
            if item.lower() in langs:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        lines.extend(["", "## 前端框架", ""])
        frontends = {"react", "vue", "angular", "svelte", "nextjs", "nuxt"}
        for item in sorted(tech_items.keys()):
            if item.lower() in frontends:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        lines.extend(["", "## 后端 & 数据库", ""])
        backends = {"django", "flask", "fastapi", "express", "nestjs", "spring",
                    "postgresql", "mysql", "mongodb", "redis", "elasticsearch"}
        for item in sorted(tech_items.keys()):
            if item.lower() in backends:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        lines.extend(["", "## 基础设施 & 工具", ""])
        infra = {"docker", "kubernetes", "k8s", "jenkins", "git", "github",
                 "aws", "gcp", "azure", "nginx", "prometheus", "grafana"}
        for item in sorted(tech_items.keys()):
            if item.lower() in infra:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        lines.extend(["", "## AI & 数据", ""])
        ai = {"tensorflow", "pytorch", "pandas", "numpy", "langchain",
              "openai", "claude", "gpt", "llm"}
        for item in sorted(tech_items.keys()):
            if item.lower() in ai:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        # 未分类的其他技术
        categorized = langs | frontends | backends | infra | ai
        others = [item for item in tech_items.keys() if item.lower() not in categorized]
        if others:
            lines.extend(["", "## 其他", ""])
            for item in sorted(others)[:30]:
                lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        (MOCS_DIR / "Tech-MOC.md").write_text("\n".join(lines), encoding="utf-8")
        count += 1

    # 2. 项目 MOC
    project_items = all_entities.get("projects", {})
    if project_items:
        lines = [
            "---",
            "type: MOC",
            "category: project",
            "tags: [project, MOC]",
            "---",
            "",
            "# 项目总览（MOC）",
            "",
        ]
        for item in sorted(project_items.keys()):
            lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        (MOCS_DIR / "Project-MOC.md").write_text("\n".join(lines), encoding="utf-8")
        count += 1

    # 3. 概念 MOC
    concept_items = all_entities.get("concepts", {})
    if concept_items:
        lines = [
            "---",
            "type: MOC",
            "category: concept",
            "tags: [concept, MOC]",
            "---",
            "",
            "# 概念 & 方法论总览（MOC）",
            "",
        ]
        for item in sorted(concept_items.keys()):
            lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        (MOCS_DIR / "Concept-MOC.md").write_text("\n".join(lines), encoding="utf-8")
        count += 1

    # 4. 人物 MOC
    people_items = all_entities.get("people", {})
    if people_items:
        lines = [
            "---",
            "type: MOC",
            "category: person",
            "tags: [person, MOC]",
            "---",
            "",
            "# 人物索引（MOC）",
            "",
        ]
        for item in sorted(people_items.keys()):
            lines.append(f"- [[{_safe_filename(item)}|{item}]]")

        (MOCS_DIR / "People-MOC.md").write_text("\n".join(lines), encoding="utf-8")
        count += 1

    # 5. 总览 MOC（链接到所有其他 MOC）
    lines = [
        "---",
        "type: MOC",
        "category: index",
        "tags: [MOC, index]",
        "---",
        "",
        "# 知识图谱总览",
        "",
        "## 按类别浏览",
        "",
    ]
    if tech_items:
        lines.append("- [[Tech-MOC|技术栈总览]]")
    if project_items:
        lines.append("- [[Project-MOC|项目总览]]")
    if concept_items:
        lines.append("- [[Concept-MOC|概念方法论]]")
    if people_items:
        lines.append("- [[People-MOC|人物索引]]")

    lines.extend([
        "",
        "## 最新 Session",
        "",
        "```dataview",
        "TABLE type, updated",
        "FROM \"wiki/00-Inbox\"",
        "SORT updated DESC",
        "LIMIT 10",
        "```",
        "",
        "## 高频实体",
        "",
        "```dataview",
        "TABLE heat as 热度, type",
        "FROM \"wiki\"",
        "WHERE type != \"source\" AND type != \"MOC\"",
        "SORT heat DESC",
        "LIMIT 20",
        "```",
    ])

    (WIKI_DIR / "Graph-Index.md").write_text("\n".join(lines), encoding="utf-8")
    count += 1

    return count


# ========== Source 页面增强 ==========

def enrich_source_pages(extractor: EntityExtractor, relation_engine: RelationEngine):
    """为 Source 页面添加丰富的 wikilinks 和元数据"""
    if not INBOX_DIR.exists():
        return

    for md_file in INBOX_DIR.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")

            # 检查是否已增强
            if "## Entities" in text:
                continue

            # 从 frontmatter 提取 cwd
            cwd = ""
            m = re.search(r'^working_dir:\s*(.+)$', text, re.MULTILINE)
            if m:
                cwd = m.group(1).strip()

            # 提取实体
            entities = extractor.extract(text, cwd=cwd)

            # 构建增强内容
            enrich_lines = ["", "## Entities", ""]

            if entities["projects"]:
                enrich_lines.append("**项目**: " + ", ".join(
                    f"[[{_safe_filename(p)}|{p}]]" for p in sorted(entities["projects"])[:5]
                ))
                enrich_lines.append("")

            if entities["tech"]:
                enrich_lines.append("**技术**: " + ", ".join(
                    f"[[{_safe_filename(t)}|{t}]]" for t in sorted(entities["tech"])[:10]
                ))
                enrich_lines.append("")

            if entities["concepts"]:
                enrich_lines.append("**概念**: " + ", ".join(
                    f"[[{_safe_filename(c)}|{c}]]" for c in sorted(entities["concepts"])[:5]
                ))
                enrich_lines.append("")

            if entities["people"]:
                enrich_lines.append("**人物**: " + ", ".join(
                    f"[[{_safe_filename(p)}|{p}]]" for p in sorted(entities["people"])[:5]
                ))
                enrich_lines.append("")

            # 添加导航链接到 MOC
            enrich_lines.extend([
                "## 导航",
                "",
                "- [[Graph-Index|知识图谱总览]]",
            ])
            if entities["tech"]:
                enrich_lines.append("- [[Tech-MOC|技术栈总览]]")
            if entities["projects"]:
                enrich_lines.append("- [[Project-MOC|项目总览]]")
            if entities["concepts"]:
                enrich_lines.append("- [[Concept-MOC|概念方法论]]")

            md_file.write_text(text + "\n".join(enrich_lines), encoding="utf-8")

        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at charon.py", exc_info=True)
            continue


def _flatten_entities(entities: Dict[str, Set[str]]) -> Set[str]:
    all_entities = set()
    for items in entities.values():
        all_entities.update(items)
    return all_entities


def _extract_page_timestamp(page_path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(page_path.stat().st_mtime)
    except OSError:
        return datetime.now()


class ConnectModule:
    """连接 Worker 的轻量热插拔封装。"""

    def __init__(self, wiki_base: str | Path | None = None, db_path: str | Path | None = None):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else Path(str(WIKI_DIR))
        self.db_path = Path(db_path).expanduser() if db_path else self.wiki_base / ".kg" / "knowledge_graph.db"
        self.extractor = EntityExtractor(wiki_base=self.wiki_base)
        self.relation_engine = RelationEngine(db_path=self.db_path)

    def handle_event(self, event_type: str, data: Dict):
        if event_type in {"page.created", "page.modified", "distill_complete"}:
            page_path = data.get("page_path")
            if page_path:
                return self._incremental_process(Path(page_path))
        if event_type == "scheduler.hourly" and data.get("task_name") == "connect_consistency_check":
            return run_connect_cycle()
        return None

    def _incremental_process(self, page_path: Path) -> Dict:
        if not page_path.exists():
            return {"status": "missing", "page_path": str(page_path)}

        text = page_path.read_text(encoding="utf-8")
        cwd = ""
        match = re.search(r'working_dir:\s*`?([^`\n]+)', text)
        if match:
            cwd = match.group(1).strip()

        old_entities = self._get_stored_entities(page_path)
        new_entities_by_type = self.extractor.extract(text, cwd=cwd)
        new_entities = _flatten_entities(new_entities_by_type)
        removed = old_entities - new_entities
        added = new_entities - old_entities

        for e1 in removed:
            for e2 in old_entities:
                if e1 != e2:
                    self.relation_engine.decrement(e1, e2)

        self.relation_engine.analyze_session(
            page_path.stem[:40],
            new_entities_by_type,
            timestamp=_extract_page_timestamp(page_path),
        )
        self._store_entities(page_path, new_entities)

        return {
            "status": "ok",
            "page_path": str(page_path),
            "added": sorted(added),
            "removed": sorted(removed),
        }

    def _get_stored_entities(self, page_path: Path) -> Set[str]:
        marker = self._marker_path(page_path)
        if not marker.exists():
            return set()
        try:
            return set(json.loads(marker.read_text(encoding="utf-8")).get("entities", []))
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at charon.py", exc_info=True)
            return set()

    def _store_entities(self, page_path: Path, entities: Set[str]):
        marker = self._marker_path(page_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"page_path": str(page_path), "entities": sorted(entities)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _marker_path(self, page_path: Path) -> Path:
        return self.wiki_base / ".kg" / "connect_entities" / f"{hashlib.md5(str(page_path).encode()).hexdigest()}.json"


# ========== 主流程 ==========

def run_connect_cycle(dry_run: bool = False, db_path: str | Path | None = None) -> Dict:
    """执行一轮关联构建 — 生成 Obsidian 知识图谱"""
    _ensure_dirs()

    if not INBOX_DIR.exists():
        logger.info("[Connect] 无 Inbox 目录，跳过")
        return {"people": 0, "projects": 0, "tech": 0, "concepts": 0, "mocs": 0}

    logger.info("[Connect] 构建 Obsidian 知识图谱...")

    extractor = EntityExtractor(wiki_base=Path(str(WIKI_DIR)))
    relation_engine = RelationEngine(db_path=db_path or (Path(str(WIKI_DIR)) / ".kg" / "knowledge_graph.db"))

    # 收集所有实体
    all_entities: Dict[str, Dict[str, Set[str]]] = {
        "people": defaultdict(set),
        "projects": defaultdict(set),
        "tech": defaultdict(set),
        "concepts": defaultdict(set),
    }

    # 项目 -> 技术栈映射
    project_tech: Dict[str, Set[str]] = defaultdict(set)

    # 扫描所有 source 文件
    for md_file in INBOX_DIR.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
            doc_name = md_file.stem[:40]

            # 提取 cwd
            cwd = ""
            m = re.search(r'working_dir:\s*`?([^`\n]+)', text)
            if m:
                cwd = m.group(1).strip()

            entities = extractor.extract(text, cwd=cwd)
            relation_engine.analyze_session(doc_name, entities, timestamp=_extract_page_timestamp(md_file))

            for category, items in entities.items():
                for item in items:
                    all_entities[category][item].add(doc_name)

            # 记录项目技术栈
            for proj in entities["projects"]:
                project_tech[proj].update(entities["tech"])

        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at charon.py", exc_info=True)
            continue

    stats = {"people": 0, "projects": 0, "tech": 0, "concepts": 0, "mocs": 0}

    if dry_run:
        total = sum(len(v) for v in all_entities.values())
        logger.info(f"[Connect] [DRY RUN] 将生成 {total} 个实体节点 + MOC 枢纽")
        return stats

    # 实体页面生成已禁用 — 与蒸馏产物的 32 字段规范冲突，且内容空洞
    # 关系分析仍在后台数据库维护，供搜索和画像使用
    # 如需恢复，取消下方注释并删除此行注释
    #
    # for name, sessions in all_entities["people"].items():
    #     if len(sessions) < 2: continue
    #     relations = relation_engine.get_relations(name)
    #     related_sessions = relation_engine.get_related_sessions(name)
    #     md_content, safe_name = generate_person_page(name, relations, related_sessions)
    #     (PEOPLE_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
    #     stats["people"] += 1
    #
    # for name, sessions in all_entities["projects"].items():
    #     if len(sessions) < 1: continue
    #     relations = relation_engine.get_relations(name)
    #     related_sessions = relation_engine.get_related_sessions(name)
    #     tech_stack = project_tech.get(name, set())
    #     md_content, safe_name = generate_project_page(name, relations, related_sessions, tech_stack)
    #     (PROJECTS_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
    #     stats["projects"] += 1
    #
    # for name, sessions in all_entities["tech"].items():
    #     if len(sessions) < 2: continue
    #     relations = relation_engine.get_relations(name)
    #     related_sessions = relation_engine.get_related_sessions(name)
    #     md_content, safe_name = generate_tech_page(name, relations, related_sessions)
    #     (TECH_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
    #     stats["tech"] += 1
    #
    # for name, sessions in all_entities["concepts"].items():
    #     if len(sessions) < 2: continue
    #     relations = relation_engine.get_relations(name)
    #     related_sessions = relation_engine.get_related_sessions(name)
    #     md_content, safe_name = generate_concept_page(name, relations, related_sessions)
    #     (CONCEPTS_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
    #     stats["concepts"] += 1
    #
    # stats["mocs"] = generate_moc_pages(all_entities)
    # enrich_source_pages(extractor, relation_engine)

    logger.info("[Connect] 实体页面生成已禁用，仅更新关系数据库")

    print(f"[Connect] 完成: {stats['people']} 人, {stats['projects']} 项目, "
          f"{stats['tech']} 技术, {stats['concepts']} 概念, {stats['mocs']} MOC")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Connect Worker - L2 to L3 Knowledge Graph")
    parser.add_argument("--watch", action="store_true", help="守护模式，每10分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    if args.watch:
        logger.info("[Connect] 守护模式启动")
        while True:
            logger.info(f"\n=== {datetime.now().isoformat()} ===")
            run_connect_cycle(dry_run=args.dry_run)
            time.sleep(600)
    else:
        stats = run_connect_cycle(dry_run=args.dry_run)
        logger.info(f"\n=== 知识图谱构建完成 ===")
        for k, v in stats.items():
            logger.info(f"  {k}: {v}")


if __name__ == "__main__":
    main()
