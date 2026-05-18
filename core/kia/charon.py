# Charon — 冥河渡夫 — 连接 Worker，摆渡数据于各系统之间
# 原模块: connect_worker.py

#!/usr/bin/env python3
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
from datetime import datetime
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Set, Tuple, Optional
from core.config import get_config


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


def _safe_filename(name: str) -> str:
    """生成安全的文件名（保留可读性）"""
    # 替换不安全的字符，但保留中文
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip()
    # 限制长度
    if len(safe) > 80:
        safe = safe[:80]
    return safe or "untitled"


def _ensure_dirs():
    for d in ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ========== 实体提取引擎 ==========

class EntityExtractor:
    """多维度实体提取器"""

    def __init__(self):
        self.tech_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(t) for t in TECH_KEYWORDS) + r')\b',
            re.IGNORECASE
        )
        self.concept_pattern = re.compile(
            r'\b(' + '|'.join(re.escape(c) for c in CONCEPT_KEYWORDS) + r')\b',
            re.IGNORECASE
        )

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

        # 7. 人名提取（英文：First Last 格式）
        name_pattern = re.compile(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b')
        for match in name_pattern.finditer(text):
            name = match.group(0)
            # 过滤掉常见非人名
            if name.lower() not in ('i am', 'it is', 'we are', 'you are', 'the', 'this', 'that'):
                result["people"].add(name)

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

        return result


# ========== 关系引擎 ==========

class RelationEngine:
    """关系分析和建立"""

    def __init__(self):
        self.co_occurrence = defaultdict(lambda: defaultdict(int))
        self.entity_docs = defaultdict(list)  # entity -> [doc_paths]

    def analyze_session(self, doc_path: str, entities: Dict[str, Set[str]]):
        """分析单个 session 中的所有实体共现"""
        all_entities = set()
        for category, items in entities.items():
            all_entities.update(items)

        self.entity_docs[doc_path].extend(all_entities)

        # 共现统计
        entities_list = list(all_entities)
        for i, e1 in enumerate(entities_list):
            for e2 in entities_list[i + 1:]:
                if e1 != e2:
                    self.co_occurrence[e1][e2] += 1
                    self.co_occurrence[e2][e1] += 1

    def get_relations(self, entity: str, min_count: int = 1) -> List[Tuple[str, int]]:
        """获取实体的关系列表"""
        relations = self.co_occurrence.get(entity, {})
        return sorted(
            [(e, c) for e, c in relations.items() if c >= min_count],
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
            continue


# ========== 主流程 ==========

def run_connect_cycle(dry_run: bool = False) -> Dict:
    """执行一轮关联构建 — 生成 Obsidian 知识图谱"""
    _ensure_dirs()

    if not INBOX_DIR.exists():
        print("[Connect] 无 Inbox 目录，跳过")
        return {"people": 0, "projects": 0, "tech": 0, "concepts": 0, "mocs": 0}

    print("[Connect] 构建 Obsidian 知识图谱...")

    extractor = EntityExtractor()
    relation_engine = RelationEngine()

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
            relation_engine.analyze_session(doc_name, entities)

            for category, items in entities.items():
                for item in items:
                    all_entities[category][item].add(doc_name)

            # 记录项目技术栈
            for proj in entities["projects"]:
                project_tech[proj].update(entities["tech"])

        except Exception:
            continue

    stats = {"people": 0, "projects": 0, "tech": 0, "concepts": 0, "mocs": 0}

    if dry_run:
        total = sum(len(v) for v in all_entities.values())
        print(f"[Connect] [DRY RUN] 将生成 {total} 个实体节点 + MOC 枢纽")
        return stats

    # 生成人物页面
    for name, sessions in all_entities["people"].items():
        if len(sessions) < 2:
            continue
        relations = relation_engine.get_relations(name)
        related_sessions = relation_engine.get_related_sessions(name)
        md_content, safe_name = generate_person_page(name, relations, related_sessions)
        (PEOPLE_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
        stats["people"] += 1

    # 生成项目页面
    for name, sessions in all_entities["projects"].items():
        if len(sessions) < 1:  # 项目允许只出现1次
            continue
        relations = relation_engine.get_relations(name)
        related_sessions = relation_engine.get_related_sessions(name)
        tech_stack = project_tech.get(name, set())
        md_content, safe_name = generate_project_page(name, relations, related_sessions, tech_stack)
        (PROJECTS_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
        stats["projects"] += 1

    # 生成技术页面
    for name, sessions in all_entities["tech"].items():
        if len(sessions) < 2:
            continue
        relations = relation_engine.get_relations(name)
        related_sessions = relation_engine.get_related_sessions(name)
        md_content, safe_name = generate_tech_page(name, relations, related_sessions)
        (TECH_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
        stats["tech"] += 1

    # 生成概念页面
    for name, sessions in all_entities["concepts"].items():
        if len(sessions) < 2:
            continue
        relations = relation_engine.get_relations(name)
        related_sessions = relation_engine.get_related_sessions(name)
        md_content, safe_name = generate_concept_page(name, relations, related_sessions)
        (CONCEPTS_DIR / f"{safe_name}.md").write_text(md_content, encoding="utf-8")
        stats["concepts"] += 1

    # 生成 MOC 枢纽页面（关键！）
    stats["mocs"] = generate_moc_pages(all_entities)

    # 增强 Source 页面
    enrich_source_pages(extractor, relation_engine)

    print(f"[Connect] 完成: {stats['people']} 人, {stats['projects']} 项目, "
          f"{stats['tech']} 技术, {stats['concepts']} 概念, {stats['mocs']} MOC")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Connect Worker - L2 to L3 Knowledge Graph")
    parser.add_argument("--watch", action="store_true", help="守护模式，每10分钟执行")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    args = parser.parse_args()

    if args.watch:
        print("[Connect] 守护模式启动")
        while True:
            print(f"\n=== {datetime.now().isoformat()} ===")
            run_connect_cycle(dry_run=args.dry_run)
            time.sleep(600)
    else:
        stats = run_connect_cycle(dry_run=args.dry_run)
        print(f"\n=== 知识图谱构建完成 ===")
        for k, v in stats.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
