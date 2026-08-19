"""Entity lexicons and extraction engine for Charon."""

import re
from pathlib import Path
from typing import Dict, Set

from core.kia.relation_endpoint_quality import (
    extract_labeled_chinese_tech_terms,
    is_valid_relation_endpoint,
)
from core.utils import LazyPath

WIKI_DIR = LazyPath("wiki_dir")

TECH_KEYWORDS = {
    # 编程语言
    "python",
    "javascript",
    "typescript",
    "go",
    "golang",
    "rust",
    "java",
    "c++",
    "c#",
    "ruby",
    "php",
    "swift",
    "kotlin",
    "scala",
    "elixir",
    "haskell",
    "lua",
    "perl",
    # 前端框架
    "react",
    "vue",
    "angular",
    "svelte",
    "solidjs",
    "nextjs",
    "nuxt",
    "remix",
    "astro",
    # 后端框架
    "django",
    "flask",
    "fastapi",
    "tornado",
    "express",
    "koa",
    "nestjs",
    "spring",
    "laravel",
    "rails",
    "gin",
    "echo",
    "beego",
    # 数据库
    "postgresql",
    "mysql",
    "mariadb",
    "mongodb",
    "redis",
    "sqlite",
    "elasticsearch",
    "clickhouse",
    "timescaledb",
    "influxdb",
    "neo4j",
    "dynamodb",
    "cassandra",
    # 基础设施
    "docker",
    "kubernetes",
    "k8s",
    "terraform",
    "ansible",
    "pulumi",
    "vagrant",
    "jenkins",
    "github actions",
    "gitlab ci",
    "circleci",
    "travis ci",
    # 云平台
    "aws",
    "gcp",
    "azure",
    "aliyun",
    "tencent cloud",
    "cloudflare",
    "vercel",
    "netlify",
    # 移动端
    "react native",
    "flutter",
    "ionic",
    "cordova",
    "electron",
    "tauri",
    # AI/ML
    "tensorflow",
    "pytorch",
    "jax",
    "onnx",
    "scikit-learn",
    "pandas",
    "numpy",
    "matplotlib",
    "seaborn",
    "plotly",
    "opencv",
    "hugging face",
    "langchain",
    "llamaindex",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "llama",
    # 工具链
    "webpack",
    "vite",
    "rollup",
    "esbuild",
    "parcel",
    "babel",
    "swc",
    "eslint",
    "prettier",
    "typescript compiler",
    "tsc",
    "git",
    "github",
    "gitlab",
    "bitbucket",
    "svn",
    "mercurial",
    # 系统
    "linux",
    "macos",
    "windows",
    "ubuntu",
    "debian",
    "centos",
    "fedora",
    "arch",
    "nginx",
    "apache",
    "traefik",
    "caddy",
    "haproxy",
    # 监控
    "prometheus",
    "grafana",
    "loki",
    "jaeger",
    "zipkin",
    "datadog",
    "newrelic",
    # 消息队列
    "kafka",
    "rabbitmq",
    "nats",
    "mqtt",
    "rocketmq",
    "pulsar",
    # 其他工具
    "obsidian",
    "notion",
    "logseq",
    "vscode",
    "vim",
    "neovim",
    "emacs",
    "cursor",
    "intellij",
    "pycharm",
    "webstorm",
    "postman",
    "insomnia",
    "微信小程序",
    "支付宝小程序",
    "抖音小程序",
    "uniapp",
    "taro",
}

CONCEPT_KEYWORDS = {
    # 架构
    "api",
    "rest",
    "graphql",
    "grpc",
    "websocket",
    "webhook",
    "soap",
    "microservice",
    "monolith",
    "serverless",
    "faas",
    "lambda",
    "edge computing",
    "cqrs",
    "event sourcing",
    "saga",
    "circuit breaker",
    "bulkhead",
    "crud",
    "mvc",
    "mvvm",
    "mvp",
    "clean architecture",
    "hexagonal architecture",
    "ddd",
    "domain driven design",
    "onion architecture",
    # 工程实践
    "ci/cd",
    "devops",
    "sre",
    "platform engineering",
    "gitops",
    "agile",
    "scrum",
    "kanban",
    "xp",
    "lean",
    "waterfall",
    "tdd",
    "bdd",
    "atdd",
    "ddd",
    "unit test",
    "integration test",
    "e2e test",
    "mutation testing",
    "property based testing",
    # 安全
    "oauth",
    "jwt",
    "sso",
    "ldap",
    "rbac",
    "abac",
    "zero trust",
    "authentication",
    "authorization",
    "encryption",
    "hash",
    "salting",
    "csrf",
    "xss",
    "sql injection",
    "mitm",
    # 性能
    "cache",
    "cdn",
    "load balancer",
    "reverse proxy",
    "rate limiting",
    "sharding",
    "partitioning",
    "replication",
    "indexing",
    "async",
    "sync",
    "concurrency",
    "parallelism",
    "threading",
    "coroutine",
    "event-driven",
    "message queue",
    "pub/sub",
    "stream processing",
    # 数据
    "etl",
    "elt",
    "data pipeline",
    "data warehouse",
    "data lake",
    "data mesh",
    "olap",
    "oltp",
    "cdc",
    "data lineage",
    "data governance",
    # 知识管理
    "知识库",
    "知识图谱",
    "wiki",
    "zettelkasten",
    "moc",
    "map of content",
    "复盘",
    "checklist",
    "sop",
    "模板",
    "最佳实践",
    # 产品
    "mvp",
    "pmf",
    "growth",
    "留存",
    "活跃",
    "转化",
    "漏斗",
    "ab测试",
    "用户画像",
    "用户旅程",
    "客户分层",
    "精细化运营",
    # 管理
    "okr",
    "kpi",
    "北极星指标",
    "okr",
    "敏捷",
    "迭代",
    "冲刺",
}

PROJECT_INDICATORS = {
    "项目",
    "project",
    "产品",
    "product",
    "应用",
    "app",
    "系统",
    "system",
    "平台",
    "platform",
    "服务",
    "service",
    "组件",
    "component",
    "模块",
    "module",
}

PROJECT_INDICATOR_PATTERN = "|".join(
    re.escape(indicator)
    for indicator in sorted(PROJECT_INDICATORS, key=len, reverse=True)
)

CHINESE_SURNAMES = {
    "王",
    "李",
    "张",
    "刘",
    "陈",
    "杨",
    "赵",
    "黄",
    "周",
    "吴",
    "徐",
    "孙",
    "胡",
    "朱",
    "高",
    "林",
    "何",
    "郭",
    "马",
    "罗",
    "梁",
    "宋",
    "郑",
    "谢",
    "韩",
    "唐",
    "冯",
    "于",
    "董",
    "萧",
    "程",
    "曹",
    "袁",
    "邓",
    "许",
    "傅",
    "沈",
    "曾",
    "彭",
    "吕",
    "苏",
    "卢",
    "蒋",
    "蔡",
    "贾",
    "丁",
    "魏",
    "薛",
    "叶",
    "阎",
    "余",
    "潘",
    "杜",
    "戴",
    "夏",
    "钟",
    "汪",
    "田",
    "任",
    "姜",
}


class EntityExtractor:
    """多维度实体提取器"""

    # 停用词：不应被提取为独立实体的系统术语、frontmatter 字段名、通用片段
    STOP_WORDS: set[str] = {
        # frontmatter 字段名（LLM 输出中常见，会被误提取）
        "名称",
        "领域",
        "摘要",
        "触发器",
        "别名",
        "跨agent关联",
        "标签推荐系统",
        "类型",
        "状态",
        "知识阶段",
        "来源数量",
        "证据级别",
        "置信度",
        "时效性",
        "创建日期",
        "关键词",
        "版本标记",
        "决策摘要",
        "合并来源",
        "提取方式",
        # 系统术语（单独出现时不应作为实体）
        "系统",
        "模块",
        "接口",
        "引擎",
        "服务",
        "组件",
        "数据库",
        "服务器",
        "客户端",
        "中间件",
        "微服务",
        "程序",
        "框架",
        "平台",
        "模型",
        "协议",
        "算法",
        "代码",
        "函数",
        "方法",
        "类",
        "对象",
        "变量",
        # 通用中文停用词/片段
        "的",
        "了",
        "在",
        "是",
        "我",
        "有",
        "和",
        "就",
        "不",
        "人",
        "都",
        "一",
        "一个",
        "上",
        "也",
        "很",
        "到",
        "说",
        "要",
        "去",
        "你",
        "会",
        "着",
        "没有",
        "看",
        "好",
        "自己",
        "这",
        "那",
        "之",
        "与",
        "或",
        "及",
        "等",
        "中",
        "内",
        "外",
        "下",
        "前",
        "后",
        "时",
        "间",
        "地",
        "方",
        "法",
        "情",
        "理",
        "事",
        "实",
        "现",
        "当",
        "从",
        "把",
        "被",
        "给",
        "让",
        "向",
        "往",
        "于",
        "而",
        "却",
        "但是",
        "因为",
        "所以",
        "如果",
        "那么",
        "虽然",
        "而且",
        "或者",
        "还是",
        "只要",
        "只有",
        "除非",
        "假如",
        "例如",
        "比如",
        "像",
        "似乎",
        "也许",
        "大概",
        "大约",
        "差不多",
        "几乎",
        "根本",
        "简直",
        "完全",
        "绝对",
        "比较",
        "最",
        "更",
        "太",
        "非常",
        "特别",
        "十分",
        "极其",
        "相当",
        "颇",
        "挺",
        "怪",
        "老",
        "真",
        "够",
        "多么",
        "怎么",
        "怎样",
        "如何",
        "为什么",
        "为何",
        "难道",
        "别",
        "不要",
        "不能",
        "不会",
        "不可",
        "不得",
        "不该",
        "不必",
        "不用",
        "何必",
        "未必",
        "首先",
        "其次",
        "再次",
        "最后",
        "总之",
        "综上所述",
        "由此看来",
        "也就是说",
        "换句话说",
        "换言之",
        "简言之",
        "归根结底",
        "归根到底",
        "说到底",
    }

    def __init__(self, wiki_base: str | Path | None = None, bootstrap_from_existing: bool = True):
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else Path(str(WIKI_DIR))
        # 允许配置覆盖关键词集合（保留硬编码作为默认值）
        from core.config import get_config

        cfg = get_config()
        tech_keywords_cfg = cfg.get("charon.tech_keywords")
        self.tech_keywords = set(
            tech_keywords_cfg if tech_keywords_cfg is not None else list(TECH_KEYWORDS)
        )
        concept_keywords_cfg = cfg.get("charon.concept_keywords")
        self.concept_keywords = set(
            concept_keywords_cfg if concept_keywords_cfg is not None else list(CONCEPT_KEYWORDS)
        )
        if bootstrap_from_existing:
            self._bootstrap_from_existing_pages()
        self.tech_pattern = re.compile(
            r"\b("
            + "|".join(re.escape(t) for t in sorted(self.tech_keywords, key=len, reverse=True))
            + r")\b",
            re.IGNORECASE,
        )
        self.concept_pattern = re.compile(
            r"\b("
            + "|".join(re.escape(c) for c in sorted(self.concept_keywords, key=len, reverse=True))
            + r")\b",
            re.IGNORECASE,
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
        result = {  # type: ignore[var-annotated]
            "people": set(),
            "projects": set(),
            "tech": set(),
            "concepts": set(),
        }

        result["tech"].update(self._extract_tech_entities(text_lower))
        result["concepts"].update(self._extract_concepts(text_lower))
        result["tech"].update(self._extract_code_languages(text_lower))
        result["projects"].update(
            self._extract_projects_from_context(cwd, git_branch)
        )
        result["projects"].update(self._extract_projects_from_text(text))
        result["people"].update(self._extract_people(text))
        result["tech"].update(self._extract_zh_tech_terms(text))

        url_projects, url_people = self._extract_url_entities(text)
        result["projects"].update(url_projects)
        result["people"].update(url_people)

        result["tech"].update(self._extract_library_names(text))
        self._filter_stop_words(result)

        return result

    def _extract_tech_entities(self, text_lower: str) -> Set[str]:
        """基于预定义模式提取技术栈实体。"""
        return {match.group(1).lower() for match in self.tech_pattern.finditer(text_lower)}

    def _extract_concepts(self, text_lower: str) -> Set[str]:
        """基于预定义模式提取概念实体。"""
        return {match.group(1).lower() for match in self.concept_pattern.finditer(text_lower)}

    @staticmethod
    def _extract_code_languages(text_lower: str) -> Set[str]:
        """从代码块标记中提取编程语言。"""
        ignored = {"text", "markdown", "md", "txt"}
        return {
            lang.lower()
            for lang in re.findall(r"```(\w+)", text_lower)
            if lang not in ignored
        }

    @staticmethod
    def _extract_projects_from_context(cwd: str, git_branch: str) -> Set[str]:
        """从工作目录和 git 分支名提取项目名。"""
        projects = set()
        if cwd:
            proj_name = Path(cwd).name
            if proj_name and proj_name not in (".", "~", "home", "users"):
                projects.add(proj_name)
        if git_branch and git_branch not in ("main", "master", "dev", "develop"):
            projects.add(git_branch)
        return projects

    def _extract_projects_from_text(self, text: str) -> Set[str]:
        """从文本声明中提取项目名。"""
        projects = set()
        proj_matches = re.findall(
            rf"(?:{PROJECT_INDICATOR_PATTERN})[\s:：]+([\w\-一-鿿]+)",
            text,
            re.IGNORECASE,
        )
        for m in proj_matches:
            if len(m) >= 2:
                projects.add(m.strip())
        projects.update(self.extract_chinese_projects(text))
        return projects

    def _extract_people(self, text: str) -> Set[str]:
        """提取英文和中文人名。"""
        people = set()
        name_pattern = re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b")
        non_names = {"i am", "it is", "we are", "you are", "the", "this", "that"}
        for match in name_pattern.finditer(text):
            name = match.group(0)
            if name.lower() not in non_names:
                people.add(name)
        people.update(self.extract_chinese_names(text))
        return people

    @staticmethod
    def _extract_zh_tech_terms(text: str) -> Set[str]:
        return extract_labeled_chinese_tech_terms(text)

    @staticmethod
    def _extract_url_entities(text: str) -> tuple:
        """从 GitHub/GitLab URL 中提取项目和组织/用户名。"""
        url_pattern = re.compile(
            r"https?://(?:github\.com|gitlab\.com)/([^/\s]+)/([^/\s]+)"
        )
        projects = set()
        people = set()
        for match in url_pattern.finditer(text):
            org, repo = match.groups()
            projects.add(repo)
            people.add(org)
        return projects, people

    @staticmethod
    def _extract_library_names(text: str) -> Set[str]:
        """从类似 package.json / requirements 的文本中提取库名。"""
        lib_pattern = re.compile(r'["\']([\w\-@/]+)["\']\s*[:：]')
        libs = set()
        for match in lib_pattern.finditer(text):
            lib = match.group(1)
            if "/" in lib:
                lib = lib.split("/")[-1]
            if len(lib) >= 2 and not lib.startswith("http"):
                libs.add(lib.lower())
        return libs

    def _is_valid_extracted_entity(self, item: str, category: str) -> bool:
        """Validate extractor output before it can become a KG endpoint."""
        text = str(item or "").strip()
        if not text or text.lower() in self.STOP_WORDS:
            return False
        if not is_valid_relation_endpoint(text):
            return False
        lowered = text.lower()
        if category == "tech" and lowered in self.tech_keywords:
            return True
        if category == "concepts" and lowered in self.concept_keywords:
            return True
        try:
            from core.kia.entity_manager import EntityManager

            return EntityManager._is_valid_entity_name(text)
        except (ImportError, AttributeError):
            return True

    def _filter_stop_words(self, result: Dict[str, Set[str]]):
        """移除各类别中的停用词和切片伪实体。"""
        for category in result:
            result[category] = {
                item
                for item in result[category]
                if self._is_valid_extracted_entity(item, category)
            }

    def extract_chinese_names(self, text: str) -> Set[str]:
        """中文人名：百家姓 + 1-2 字名，并用局部上下文降噪。"""
        names = set()
        indicators = ["说", "认为", "提到", "建议", "负责", "和", "与", "找", "问", "告诉"]
        for match in re.finditer(r"([一-鿿]{2,3})", text):
            name = match.group(1)
            # 截断明确为动词后缀的 3 字词组（"建国"等常见名字后缀不移除）
            if len(name) == 3 and name[-1] in {
                "说",
                "认",
                "提",
                "负",
                "和",
                "与",
                "找",
                "问",
                "告",
            }:
                name = name[:2]
            if name[0] not in CHINESE_SURNAMES:
                continue
            context = text[max(0, match.start() - 4) : min(len(text), match.end() + 4)]
            if any(ind in context for ind in indicators):
                names.add(name)
        return names

    def extract_chinese_projects(self, text: str) -> Set[str]:
        """中文项目名：识别项目指示词 + 名称。"""
        projects = set()
        patterns = [
            rf"(?:{PROJECT_INDICATOR_PATTERN})[\s:：「『\"']+([\w\-一-鿿]{{2,20}})(?:[」』\"'\s，。,.]|$)",
            r"(?:代号|codename)[\s:：]+([\w\-一-鿿]{2,10})(?:[\s，。,.]|$)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                projects.add(match.group(1).strip())
        return projects
