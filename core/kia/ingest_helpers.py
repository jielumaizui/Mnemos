"""
Ingest 引擎纯函数辅助模块

从 ingest_engine.py 抽离的无状态辅助函数（不依赖 IngestEngine 实例）。
全部为纯函数：相同输入恒得相同输出，无副作用，便于独立单测。
"""

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Dict

# 预编译正则，避免每次调用重新编译
_FINGERPRINT_RE = re.compile(r'[^\w\u4e00-\u9fa5]')
_WIKI_REF_RE = re.compile(r'\[\[([^\]]+)\]\]')

# RuleScorer 旁路记录器（阶段0：只记录，不改变决策）
_bypass_logger = logging.getLogger("rule_scorer_bypass")
_bypass_logger.setLevel(logging.DEBUG)
if not _bypass_logger.handlers:
    _bypass_handler = logging.FileHandler(
        Path.home() / ".mnemos" / "logs" / "rule_scorer_bypass.log",
        encoding="utf-8"
    )
    _bypass_handler.setFormatter(logging.Formatter(
        '%(asctime)s | %(message)s'
    ))
    _bypass_logger.addHandler(_bypass_handler)


def _bypass_record(content: str, source: str, rule_score: float, original_result):
    """旁路记录：记录 RuleScorer 评分，不改变原有决策"""
    try:
        _bypass_logger.info(json.dumps({
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "content_preview": content[:200] if content else "",
            "rule_score": round(rule_score, 3),
            "original_result": str(original_result),
        }, ensure_ascii=False))
    except Exception:
        pass  # 记录失败不影响主流程


# ==================== 内容指纹与去重 ====================

def compute_content_fingerprint(content: str) -> str:
    """计算内容指纹（用于去重检测）"""
    cleaned = _FINGERPRINT_RE.sub('', content.lower())
    sample = cleaned[:200]
    # 加盐：内容长度信息防止前缀相同的长内容碰撞
    salt = f":{len(cleaned)}"
    return hashlib.md5((sample + salt).encode('utf-8')).hexdigest()[:16]


def is_duplicate_content(existing_body: str,
                         new_description: str,
                         threshold: float = 0.8) -> bool:
    """检测内容是否重复

    Args:
        existing_body: 现有页面内容
        new_description: 新描述
        threshold: 相似度阈值（默认 0.8，当前实现以指纹前缀匹配 + 包含检测为主）

    Returns:
        是否重复
    """
    existing_descriptions = re.findall(
        r'### 新来源 - \d{4}-\d{2}-\d{2}\n\n(.+?)(?=\n###|\Z)',
        existing_body,
        re.DOTALL,
    )

    if not existing_descriptions:
        return False

    new_fp = compute_content_fingerprint(new_description)

    for existing_desc in existing_descriptions:
        existing_fp = compute_content_fingerprint(existing_desc)
        # 完整指纹匹配（前12字符碰撞概率对短内容过高）
        if new_fp == existing_fp:
            return True
        # 内容包含检测（增加长度相近限制，避免短描述被长描述误判）
        len_new = len(new_description)
        len_existing = len(existing_desc)
        if len_new > 0 and len_existing > 0:
            # 只有当长度差异不超过 30% 且一方包含另一方时才判定重复
            len_ratio = min(len_new, len_existing) / max(len_new, len_existing)
            if len_ratio >= 0.7:
                if new_description in existing_desc or existing_desc in new_description:
                    return True

    return False


# ==================== 噪声消息过滤 (P11) ====================

# 预编译噪声检测正则
_NOISE_RE_PATTERNS = [
    # 纯标点/空白
    re.compile(r'^[\s\uff0c\u3002\uff01\uff1f,.!?；：:""''()（）[]{}【】]+$'),
    # 纯 emoji（常见范围）
    re.compile(r'^[\u2764\U0001f300-\U0001f9ff\u2600-\u26ff\u2700-\u27bf\s]+$'),
]

# 常见低价值短语（大小写不敏感）
_NOISE_PHRASES = {
    # 中文
    '好的', '好', 'ok', 'okay', '继续', '嗯', '啊', '哦', '行', '可以',
    '明白', '知道了', '了解', '收到', '对的', '没错', '是的', '嗯嗯',
    '谢谢', '多谢', '辛苦了', '拜托', '麻烦了',
    # 英文
    'yes', 'yep', 'yeah', 'no', 'nope', 'nah',
    'go on', 'go ahead', 'next', 'proceed',
    'thanks', 'thank you', 'thx', 'ty',
    'got it', 'gotcha', 'understood', ' Roger ',
    # 极简命令
    '开始吧', '来吧', '动手吧', '搞起',
}


def is_noise_message(content: str,
                     min_length: int = 4,
                     enable_phrase_match: bool = True,
                     enable_regex_match: bool = True) -> bool:
    """判断消息是否为低价值噪声

    【P11 Noise Filtering】
    过滤不应进入知识库的低价值对话内容：
    - 过短消息（< min_length 字符）
    - 纯标点 / 纯 emoji
    - 常见敷衍短语（"好的", "ok", "继续" 等）

    Args:
        content: 消息内容
        min_length: 最小有效长度（默认 4，中文语境下 "继续"=6 会被过滤）
        enable_phrase_match: 启用短语匹配
        enable_regex_match: 启用正则匹配

    Returns:
        True = 是噪声，应跳过
    """
    if not content or not isinstance(content, str):
        return True

    stripped = content.strip()

    # 1. 空内容
    if not stripped:
        return True

    # 2. 长度检查
    if len(stripped) < min_length:
        return True

    # 3. 正则匹配（纯标点 / 纯 emoji）
    if enable_regex_match:
        for pattern in _NOISE_RE_PATTERNS:
            if pattern.match(stripped):
                return True

    # 4. 短语匹配（去除标点后精确匹配）
    if enable_phrase_match:
        # 提取核心文本（去除标点、空白、大小写）
        core = re.sub(r'[^\w\u4e00-\u9fa5]', '', stripped).lower()
        if core in _NOISE_PHRASES:
            return True

    # 5. 重复字符检测（如 "哈哈哈哈哈哈" / "oooooooo"）
    if len(set(stripped)) <= 3 and len(stripped) >= 6:
        return True

    # === RuleScorer 旁路记录（阶段0：只记录，不改变决策）===
    try:
        from core.kia.rule_scorer import RuleScorer
        scorer = RuleScorer()
        rule_score = scorer.score(content)
        _bypass_record(content, "is_noise_message", rule_score, False)
    except Exception:
        pass
    
    return False


# ==================== 消息质量评分 (P13) ====================

# 常用中文停用词
_STOPWORDS_ZH = {
    '的', '了', '在', '是', '我', '有', '和', '就', '不', '人',
    '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去',
    '你', '会', '着', '没有', '看', '好', '自己', '这', '那', '啊',
    '嗯', '哦', '呢', '吧', '吗', '哈', '这个', '那个', '然后', '就是',
    '还是', '但是', '因为', '所以', '如果', '虽然', '不过', '其实',
    '可能', '应该', '觉得', '认为', '知道', '想要', '需要',
}

# 有效内容信号词
_VALUE_SIGNALS = {
    # 技术/操作信号
    '代码', '函数', '方法', '类', '模块', '接口', 'API', '配置',
    '部署', '调试', '测试', '优化', '重构', '版本', '提交', '分支',
    '数据库', '查询', '缓存', '队列', '服务', '容器', '集群',
    'python', 'javascript', 'typescript', 'java', 'go', 'rust',
    'docker', 'kubernetes', 'linux', 'nginx', 'redis',
    # 问题/解决信号
    '问题', '错误', 'bug', '异常', '崩溃', '失败', '超时',
    '解决', '修复', '方案', '思路', '分析', '排查', '定位',
    # 决策/设计信号
    '设计', '架构', '方案', '策略', '选择', '对比', '优缺点',
    '建议', '推荐', '决定', '结论', '原因', '理由',
    # 知识信号
    '原理', '机制', '流程', '步骤', '指南', '文档', '规范',
    '标准', '模式', '算法', '数据结构', '协议', '格式',
}


def score_message_quality(content: str) -> Dict[str, float]:
    """轻量消息质量评分（纯规则，零 API 成本）

    【P13 Content Quality Score Before Ingest】
    针对聊天消息优化的快速评分：
    - 内容长度（适中为佳，过短/过长都扣分）
    - 信息密度（有效词 / 停用词比例）
    - 语义丰富度（词汇多样性 + 价值信号命中）

    返回分数 0-100，以及各维度明细。
    当前策略：只评分不拦截，记录后观察再设门槛。

    Returns:
        {
            "total_score": float,      # 总分 0-100
            "length_score": float,     # 长度维度 0-30
            "density_score": float,    # 密度维度 0-35
            "richness_score": float,   # 丰富度维度 0-35
            "details": {
                "char_count": int,
                "valid_word_count": int,
                "stopword_count": int,
                "unique_ratio": float,
                "value_signals": int,
            }
        }
    """
    # === RuleScorer 旁路记录（阶段0：只记录，不改变决策）===
    try:
        from core.kia.rule_scorer import RuleScorer
        scorer = RuleScorer()
        rule_score = scorer.score(content)
        _bypass_record(content, "score_message_quality", rule_score, None)
    except Exception:
        pass
    
    if not content or not isinstance(content, str):
        return _empty_quality_result()

    stripped = content.strip()
    char_count = len(stripped)

    # === 1. 长度评分 (0-30) ===
    if char_count < 20:
        length_score = char_count  # 0-20
    elif char_count < 100:
        length_score = 20 + (char_count - 20) * 0.125  # 20-30
    elif char_count < 500:
        length_score = 30
    elif char_count < 1000:
        length_score = 30 - (char_count - 500) * 0.01
    else:
        length_score = max(15, 25 - (char_count - 1000) * 0.005)
    length_score = max(0, min(30, length_score))

    # === 2. 信息密度 (0-35) ===
    # 提取词
    words = re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{2,}', stripped)
    if not words:
        return _empty_quality_result(char_count=char_count)

    total_words = len(words)
    stopwords = [w for w in words if w.lower() in _STOPWORDS_ZH]
    stopword_count = len(stopwords)
    valid_words = total_words - stopword_count

    # 有效词比例
    valid_ratio = valid_words / total_words if total_words > 0 else 0

    # 中文/英文混合加分
    has_zh = any(re.search(r'[\u4e00-\u9fa5]', w) for w in words)
    has_en = any(re.search(r'[a-zA-Z]', w) for w in words)
    mixed_bonus = 0.05 if has_zh and has_en else 0

    density_score = (valid_ratio + mixed_bonus) * 35
    density_score = max(0, min(35, density_score))

    # === 3. 语义丰富度 (0-35) ===
    # 词汇多样性
    unique_words = set(w.lower() for w in words)
    unique_ratio = len(unique_words) / total_words if total_words > 0 else 0

    # 价值信号命中
    content_lower = stripped.lower()
    value_signals = sum(1 for sig in _VALUE_SIGNALS if sig.lower() in content_lower)
    value_signal_score = min(value_signals * 3, 15)

    # 结构信号（列表、代码、链接）
    struct_signals = 0
    if re.search(r'^\s*[-*\d]\s+', content, re.M):
        struct_signals += 3
    if '`' in content or '```' in content:
        struct_signals += 5
    if re.search(r'https?://', content):
        struct_signals += 3
    struct_signals = min(struct_signals, 10)

    richness_score = (unique_ratio * 10 + value_signal_score + struct_signals)
    richness_score = max(0, min(35, richness_score))

    # === 总分 ===
    total_score = length_score + density_score + richness_score

    return {
        "total_score": round(total_score, 1),
        "length_score": round(length_score, 1),
        "density_score": round(density_score, 1),
        "richness_score": round(richness_score, 1),
        "details": {
            "char_count": char_count,
            "valid_word_count": valid_words,
            "stopword_count": stopword_count,
            "unique_ratio": round(unique_ratio, 3),
            "value_signals": value_signals,
        }
    }


def _empty_quality_result(char_count: int = 0) -> Dict[str, float]:
    """空内容质量结果"""
    return {
        "total_score": 0.0,
        "length_score": 0.0,
        "density_score": 0.0,
        "richness_score": 0.0,
        "details": {
            "char_count": char_count,
            "valid_word_count": 0,
            "stopword_count": 0,
            "unique_ratio": 0.0,
            "value_signals": 0,
        }
    }


# ==================== 实体/概念回退提取 ====================

def _clean_wiki_refs(content: str) -> str:
    """移除 Wiki 引用标记 [[...]]，用于后续文本处理。"""
    return _WIKI_REF_RE.sub(r'\1', content)


# ── 技术实体词典（可扩展）─────────────────────────────
_ENTITY_TECH_TERMS = {
    # 编程语言
    'Python', 'JavaScript', 'TypeScript', 'Java', 'Go', 'Golang', 'Rust',
    'C++', 'C#', 'Ruby', 'PHP', 'Swift', 'Kotlin', 'Scala', 'Haskell',
    'Lua', 'Perl', 'R', 'MATLAB', 'SQL', 'Bash', 'Shell', 'PowerShell',
    # 前端框架/库
    'React', 'Vue', 'Angular', 'Svelte', 'Next.js', 'Nuxt', 'Nuxt.js',
    'jQuery', 'Bootstrap', 'Tailwind', 'Tailwind CSS', 'Webpack', 'Vite',
    'Rollup', 'Parcel', 'Babel', 'ESLint', 'Prettier',
    # 后端框架
    'Django', 'Flask', 'FastAPI', 'Tornado', 'Spring', 'Spring Boot',
    'Express', 'Koa', 'NestJS', 'Rails', 'Laravel', 'Symfony',
    # 数据/AI/ML
    'TensorFlow', 'PyTorch', 'Keras', 'Scikit-learn', 'NumPy', 'Pandas',
    'Matplotlib', 'Seaborn', 'OpenCV', 'NLTK', 'spaCy', 'Hugging Face',
    'XGBoost', 'LightGBM', 'CatBoost', 'JAX', 'ONNX',
    # 数据库
    'MySQL', 'PostgreSQL', 'SQLite', 'MongoDB', 'Redis', 'Elasticsearch',
    'Cassandra', 'DynamoDB', 'Firebase', 'Neo4j', 'ClickHouse',
    # 工具/平台
    'Docker', 'Kubernetes', 'K8s', 'Git', 'GitHub', 'GitLab', 'Bitbucket',
    'Jenkins', 'GitHub Actions', 'GitLab CI', 'Travis CI', 'CircleCI',
    'AWS', 'Azure', 'GCP', 'Google Cloud', '阿里云', '腾讯云',
    'Linux', 'Ubuntu', 'CentOS', 'Debian', 'macOS', 'Windows',
    'Nginx', 'Apache', 'Traefik', 'HAProxy',
    'Prometheus', 'Grafana', 'ELK', 'Sentry', 'Datadog',
    # 协议/格式
    'HTTP', 'HTTPS', 'TCP', 'UDP', 'IP', 'IPv4', 'IPv6',
    'REST', 'RESTful', 'GraphQL', 'gRPC', 'WebSocket', 'WebRTC',
    'JSON', 'XML', 'YAML', 'TOML', 'INI', 'CSV', 'Markdown', 'MDX',
    'HTML', 'CSS', 'Sass', 'SCSS', 'Less', 'SVG',
    'JWT', 'OAuth', 'OAuth2', 'OpenID', 'SAML', 'LDAP', 'SSO',
    # 虚拟化/容器
    'VMware', 'VirtualBox', 'KVM', 'QEMU', 'LXC', 'systemd',
    # 其他常用
    'Node.js', 'Nodejs', 'npm', 'yarn', 'pnpm', 'pip', 'conda', 'maven',
    'Gradle', 'CMake', 'Make', 'Bazel', 'Buck',
    'VS Code', 'Visual Studio Code', 'IntelliJ', 'PyCharm', 'Vim', 'Neovim',
    'Jira', 'Confluence', 'Notion', 'Slack', 'Discord', 'Trello',
}

# 中文技术实体后缀模式（实体 = 有具体实现的东西）
_ENTITY_ZH_SUFFIXES = [
    '语言', '编译器', '解释器', '虚拟机', '运行时', '引擎', '框架',
    '库', '包', '模块', '组件', '插件', '扩展', '工具', '平台',
    '系统', '操作系统', '数据库', '服务器', '客户端', '中间件',
    '服务', '应用', '程序', '软件', '硬件', '芯片', '设备',
    '协议', '接口', 'API', 'SDK', 'CLI', 'GUI', 'IDE',
    '模型', '算法', '网络', '集群', '节点', '容器', '镜像',
]

# ── 技术概念词典（可扩展）─────────────────────────────
_CONCEPT_TECH_TERMS = {
    # 编程基础概念
    'OOP', '面向对象', 'FP', '函数式编程', '面向过程', '声明式', '命令式',
    '类', '对象', '实例', '属性', '方法', '函数', '过程', '子程序',
    '接口', '抽象类', '基类', '父类', '子类', '超类', '元类',
    '继承', '多继承', '多重继承', '多态', '封装', '抽象', '组合', '聚合',
    '泛型', '模板', '类型系统', '静态类型', '动态类型', '强类型', '弱类型',
    '变量', '常量', '字面量', '表达式', '语句', '块', '作用域',
    '闭包', '回调', '回调函数', '高阶函数', '匿名函数', 'Lambda', '箭头函数',
    '递归', '尾递归', '迭代', '循环', '遍历', '枚举',
    '异常', '错误', 'Bug', '调试', '断点', '堆栈', '调用栈', '栈溢出',
    '内存', '堆', '栈', '垃圾回收', 'GC', '引用计数', '内存泄漏',
    '线程', '进程', '协程', '纤程', '并发', '并行', '同步', '异步',
    '阻塞', '非阻塞', '事件循环', '消息队列', '信号量', '互斥锁', '死锁',
    '装饰器', '注解', '属性描述符', '上下文管理器', '生成器', '迭代器',
    '可迭代对象', '序列', '映射', '集合', '字典', '哈希表', '数组', '链表',
    # 设计模式
    '单例模式', '工厂模式', '抽象工厂', '建造者模式', '原型模式',
    '适配器模式', '桥接模式', '组合模式', '装饰器模式', '外观模式',
    '享元模式', '代理模式',
    '责任链模式', '命令模式', '解释器模式', '迭代器模式', '中介者模式',
    '备忘录模式', '观察者模式', '发布订阅', '状态模式', '策略模式',
    '模板方法', '访问者模式', 'MVC', 'MVVM', 'MVP', '依赖注入', 'DI', 'IoC',
    # 数据结构
    '数组', '链表', '双向链表', '循环链表', '栈', '队列', '双端队列', '优先队列',
    '堆', '二叉堆', '树', '二叉树', '二叉搜索树', '平衡树', 'AVL树', '红黑树',
    'B树', 'B+树', 'Trie树', '前缀树', '后缀树', '线段树', '树状数组',
    '有向图', '无向图', '加权图', '拓扑排序', '并查集', '哈希表', '散列表',
    '布隆过滤器', '跳表', 'LRU缓存',
    # 算法
    '排序算法', '快速排序', '归并排序', '堆排序', '冒泡排序', '插入排序',
    '选择排序', '计数排序', '桶排序', '基数排序', '希尔排序', 'TimSort',
    '搜索算法', '二分搜索', '线性搜索', '广度优先搜索', 'BFS', '深度优先搜索', 'DFS',
    'Dijkstra', 'A*', 'A星', 'Bellman-Ford', 'Floyd-Warshall', '最小生成树',
    'Prim', 'Kruskal', '动态规划', 'DP', '贪心算法', '分治算法', '回溯算法',
    '滑动窗口', '双指针', '前缀和', '差分数组', '单调栈', '单调队列',
    # 架构/运维
    '微服务', '单体架构', '分布式系统', '集群', '负载均衡', '高可用', 'HA',
    '容错', '熔断', '降级', '限流', '缓存', 'CDN', '边缘计算',
    '消息队列', '事件驱动', 'CQRS', '事件溯源', ' Saga模式',
    'CI', 'CD', 'CI/CD', 'DevOps', 'GitOps', 'IaC', 'SRE',
    '测试驱动开发', 'TDD', '行为驱动开发', 'BDD', '领域驱动设计', 'DDD',
    '敏捷开发', 'Scrum', 'Kanban', '看板', '瀑布模型', '螺旋模型',
    # 安全
    '加密', '解密', '哈希', '摘要', '签名', '证书', 'SSL', 'TLS',
    'XSS', 'CSRF', 'SQL注入', '中间人攻击', '重放攻击',
    # 网络
    'DNS', 'CDN', '负载均衡', '反向代理', '正向代理', 'NAT', 'VPN', '防火墙',
    'CORS', '同源策略', '跨域', 'Cookie', 'Session', 'LocalStorage',
    # 前端专项
    '虚拟DOM', 'VDOM', 'Diff算法', '响应式', '双向绑定', '单向数据流',
    '状态管理', '组件化', '模块化', '懒加载', '代码分割', 'Tree Shaking',
    'SSR', 'CSR', 'SSG', 'ISR', 'Hydration', '预渲染', '服务端渲染',
    'PWA', 'Service Worker', 'Web Worker', 'WebAssembly', 'WASM',
    # AI/ML 概念
    '神经网络', '深度学习', '机器学习', '强化学习', '监督学习', '无监督学习',
    '迁移学习', '联邦学习', 'Transformer', '注意力机制', '自注意力',
    '嵌入', 'Embedding', '向量', 'Token', 'Prompt', '上下文', '幻觉',
    'RAG', '检索增强生成', 'Agent', '智能体', '多模态', '大语言模型', 'LLM',
    '微调', 'Fine-tuning', '量化', '蒸馏', '推理', '训练', 'epoch', 'batch',
    '损失函数', '优化器', '反向传播', '梯度下降', '学习率', '过拟合', '欠拟合',
}

# 中文概念后缀模式（概念 = 抽象的知识/方法/思想）
_CONCEPT_ZH_SUFFIXES = [
    '编程', '开发', '设计', '架构', '模式', '范式', '原理', '机制',
    '流程', '过程', '方法', '技术', '策略', '方案', '规范', '标准',
    '理论', '思想', '哲学', '模型', '算法', '结构', '特性', '属性',
    '性能', '效率', '优化', '重构', '调试', '测试', '部署', '运维',
    '安全', '隐私', '权限', '认证', '授权', '审计', '监控', '日志',
    '协议', '接口', '格式', '规范', '约定', '契约', '规则', '约束',
]

# 预编译正则
_ENTITY_CAMEL_RE = re.compile(r'\b([A-Z][a-z]+[A-Z]\w+)\b')
# 大写缩写：要求前后是词边界或标点，避免从已有词中截取
_ENTITY_ACRONYM_RE = re.compile(r'(?:^|[\s\(\)[]"\',;：，。！？])\b([A-Z]{2,10})\b(?=[\s\(\)[]"\',;：，。！？]|$)')
# 中文实体：严格匹配 —— 必须有技术动词/介词前缀 + 2-6个中文字 + 后缀
# 避免把普通中文短语（如"在函数定义前应用"）误报为实体
_ENTITY_ZH_RE = re.compile(
    r'(?:使用|基于|通过|配合|搭建|部署到|采用|调用|引入|集成|安装|配置|管理|监控|查询|优化|开发|运行|支持|包含|需要|用到)'
    r'[\u4e00-\u9fa5]{1,6}(?:' + '|'.join(_ENTITY_ZH_SUFFIXES) + r')'
)
# 中文概念：2-6个中文字 + 后缀，避免单字误报
_CONCEPT_ZH_RE = re.compile(
    r'[\u4e00-\u9fa5]{2,6}(?:' + '|'.join(_CONCEPT_ZH_SUFFIXES) + r')'
)


def _word_match(text: str, term: str) -> bool:
    """检查 term 在 text 中是否出现。

    - 纯 ASCII 英文词：要求整词匹配（避免 SQL 从 PostgreSQL 误报）
    - 含中文的词：允许子串匹配（中文复合词是常态，如"函数"在"高阶函数"中合理）
    """
    if not term:
        return False

    # 判断 term 类型
    has_zh = bool(re.search(r'[\u4e00-\u9fa5]', term))
    text_lower = text.lower()
    term_lower = term.lower()

    if not has_zh:
        # 纯英文/数字：整词匹配
        idx = 0
        while True:
            pos = text_lower.find(term_lower, idx)
            if pos == -1:
                return False
            prev_ok = pos == 0 or not re.match(r'[a-z0-9_]', text_lower[pos - 1])
            end = pos + len(term)
            next_ok = end >= len(text) or not re.match(r'[a-z0-9_]', text_lower[end])
            if prev_ok and next_ok:
                return True
            idx = pos + 1
    else:
        # 含中文：子串匹配即可
        return term_lower in text_lower


def extract_entities_fallback(content: str) -> List[str]:
    """实体提取回退方案（增强版）

    零 API 成本，通过技术词典 + 模式匹配识别：
    - 编程语言、框架、库、工具、平台（英文精确匹配）
    - 中文技术实体（xxx语言/框架/引擎/系统等）
    - CamelCase 标识符
    - 大写缩写（2-10 字母）
    """
    entities = set()
    clean = _clean_wiki_refs(content)

    # 1. 英文技术实体词典匹配（整词匹配，避免子串误报）
    for term in _ENTITY_TECH_TERMS:
        if _word_match(clean, term):
            entities.add(term)

    # 2. CamelCase 标识符
    for m in _ENTITY_CAMEL_RE.finditer(clean):
        name = m.group(1)
        if len(name) > 3:
            entities.add(name)

    # 3. 大写缩写（补充词典未覆盖的，如 HTTP 已在词典中）
    for m in _ENTITY_ACRONYM_RE.finditer(clean):
        # group(1) 是捕获组中的缩写，group(0) 包含前导分隔符
        acronym = m.group(1) if m.lastindex else m.group(0)
        # 清理前导空格/标点
        acronym = acronym.strip()
        if 2 <= len(acronym) <= 10:
            entities.add(acronym)

    # 4. 中文技术实体（xxx语言/框架/引擎/系统等）
    for m in _ENTITY_ZH_RE.finditer(clean):
        name = m.group(0)
        if len(name) >= 4:
            entities.add(name)

    # 5. 版本号关联：Python 3、React 18 → 提取基础名
    for m in re.finditer(r'([A-Za-z][A-Za-z0-9+#]{1,15})\s*[\d\.]+', clean):
        base = m.group(1)
        if base in _ENTITY_TECH_TERMS or len(base) > 3:
            entities.add(base)

    # 去重并限制数量（按字母序，优先保留词典命中的）
    result = sorted(entities, key=lambda x: (x not in _ENTITY_TECH_TERMS, x.lower()))
    return result[:12]


def extract_concepts_fallback(content: str) -> List[str]:
    """概念提取回退方案（增强版）

    零 API 成本，通过概念词典 + 模式匹配识别：
    - 编程概念、设计模式、算法、数据结构、架构思想（英文精确匹配）
    - 中文技术概念（xxx编程/设计/架构/原理/方法等）
    """
    concepts = set()
    clean = _clean_wiki_refs(content)
    lower = clean.lower()

    # 1. 概念词典匹配
    for term in _CONCEPT_TECH_TERMS:
        if _word_match(clean, term):
            concepts.add(term)

    # 2. "xxx 是 yyy" 定义句式中提取概念词（前缀必须是明确的技术词）
    # 只匹配 2-4 字技术前缀 + 编程/设计/架构/方法/模式/原理/机制
    _concept_prefixes = {
        '面向对象', '函数式', '命令式', '声明式', '响应式', '并发', '异步', '同步',
        '事件驱动', '数据驱动', '领域驱动', '测试驱动', '行为驱动',
        '对象', '函数', '类', '模块', '组件', '服务', '接口', '协议',
        '装饰器', '生成器', '迭代器', '闭包', '回调', '代理', '适配器',
        '单例', '工厂', '观察者', '策略', '模板', '访问者',
        '快速', '归并', '堆', '冒泡', '插入', '选择', '计数', '桶', '基数',
        '广度优先', '深度优先', '二分', '线性', '动态规划', '贪心', '分治', '回溯',
    }
    for prefix in _concept_prefixes:
        for suffix in ('编程', '设计', '架构', '开发', '方法', '模式', '原理', '机制'):
            term = prefix + suffix
            if term in clean:
                concepts.add(term)

    # 去重排序，优先保留词典命中项
    result = sorted(concepts, key=lambda x: (x not in _CONCEPT_TECH_TERMS, x.lower()))
    return result[:12]


# ==================== 句子级抽取 ====================

def extract_entity_description(entity: str, content: str) -> str:
    """从内容中提取实体的描述（包含该实体的句子）"""
    sentences = re.split(r'[。！？\n]', content)
    for sent in sentences:
        if entity in sent and len(sent.strip()) > 10:
            return sent.strip()
    return f"涉及{entity}的相关记录"


def extract_concept_definition(concept: str, content: str) -> str:
    """从内容中提取概念的定义

    匹配 "xxx是..." / "xxx指的是..." 等定义模式。
    """
    patterns = [
        rf'{re.escape(concept)}是(.+?)[。；]',
        rf'{re.escape(concept)}指的是(.+?)[。；]',
        rf'{re.escape(concept)}(.+?)模式',
        rf'{re.escape(concept)}(.+?)方法',
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return match.group(0)
    return f"关于{concept}的相关记录"


# ==================== 循环污染检测 ====================

def check_wiki_self_reference(content: str,
                               wiki_dir: str = "") -> Tuple[bool, str, List[str]]:
    """检测内容是否引用了已存在的 Wiki 页面（循环污染 L2 检测）

    【循环污染防护 - 第 2 层】
    检测信号：
    1. 内容中包含 [[xxx]] Wiki 引用
    2. xxx 对应的 Wiki 页面已在 wiki_dir 中存在

    宁可漏掉，不可污染。如果 Wiki 页面存在但不确定是否自引用，
    保守策略：标记为自引用。

    Args:
        content: 待检测内容
        wiki_dir: Wiki 页面目录

    Returns:
        (是否自引用, 原因, 检测到的引用列表)
    """
    # 先移除代码块和行内代码，避免代码示例中的 [[...]] 被误检
    cleaned = re.sub(r'```[\s\S]*?```', '', content)  # 代码块
    cleaned = re.sub(r'`[^`]+`', '', cleaned)          # 行内代码
    wiki_refs = re.findall(r'\[\[([^\]]+)\]\]', cleaned)
    if not wiki_refs:
        return False, "No wiki references", []

    wiki_path = Path(wiki_dir).expanduser()
    if not wiki_path.exists():
        # Wiki 目录不存在，无法检测，保守放行
        return False, "Wiki directory not found, cannot verify", wiki_refs

    existing_refs = []
    for ref in wiki_refs:
        # 支持 [[page]] 和 [[page|display]] 两种格式
        page_name = ref.split('|')[0].strip()
        # 规范化文件名
        safe_name = re.sub(r'[^\w\-]', '_', page_name)
        # 检查多种可能的文件路径
        possible_paths = [
            wiki_path / f"{safe_name}.md",
            wiki_path / f"{page_name}.md",
            wiki_path / "concepts" / f"{safe_name}.md",
            wiki_path / "entities" / f"{safe_name}.md",
            wiki_path / "outputs" / f"{safe_name}.md",
        ]
        if any(p.exists() for p in possible_paths):
            existing_refs.append(page_name)

    if existing_refs:
        return (
            True,
            f"References existing wiki pages: {', '.join(existing_refs[:3])}",
            existing_refs,
        )

    return False, "Wiki references point to non-existing pages", wiki_refs


def detect_wiki_reference_pollution(content: str,
                                    tags: List[str]) -> Tuple[bool, float, str]:
    """检测内容是否被 Wiki 引用污染（循环污染检测）

    【循环污染防护 - 第 3 层】
    检测信号：
    1. 内容中包含大量 [[Wiki引用]] 格式
    2. 标签表明内容来自 AI 对话（source=claude/hermes 等）
    3. 引用密度过高（>30% 的内容是引用）

    Returns:
        (是否污染, 污染指数 0-1, 原因)
    """
    wiki_refs = re.findall(r'\[\[([^\]]+)\]\]', content)
    ref_count = len(wiki_refs)

    if ref_count == 0:
        return False, 0.0, "No wiki references"

    # 引用密度
    ref_chars = sum(len(ref) for ref in wiki_refs)
    total_chars = len(content)
    density = ref_chars / total_chars if total_chars > 0 else 0

    # 来源标签
    source_tags = [t for t in tags if t.startswith('source=')]
    ai_sources = ['source=claude', 'source=hermes', 'source=openclaw', 'source=ai']
    is_ai_source = any(s in st for st in source_tags for s in ai_sources)

    # 显式 Wiki 引用标记
    has_wiki_ref_tag = any('wiki-ref' in t or t == 'contains:wiki-refs' for t in tags)

    # 判定
    if has_wiki_ref_tag and is_ai_source:
        return True, density, "AI-generated content explicitly marked as containing wiki references"

    if is_ai_source and density > 0.3:
        return True, density, f"AI-generated content with high wiki reference density ({density:.1%})"

    if ref_count > 10:
        return True, density, f"Excessive wiki references ({ref_count})"

    return False, density, "Within acceptable range"


# ==================== Prompt Injection 检测 (H8 系统化威胁扫描) ====================

# 20+ 种威胁模式，按类别分组
_PI_PATTERNS = [
    # === Category 1: 指令覆盖 (prompt_injection) ===
    (re.compile(r'ignore\s+(all\s+)?(previous|prior|above)\s+instructions?', re.I), "prompt_injection", 0.95),
    (re.compile(r'forget\s+(all\s+)?(previous|prior|above)\s+instructions?', re.I), "prompt_injection", 0.95),
    (re.compile(r'disregard\s+(all\s+)?(previous|prior|above)\s+instructions?', re.I), "prompt_injection", 0.95),
    (re.compile(r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', re.I), "prompt_injection", 0.95),
    # 中文指令覆盖
    (re.compile(r'忽略\s*(之前|以上|前面)\s*的?指令', re.I), "prompt_injection", 0.95),
    (re.compile(r'忘记\s*(之前|以上|前面)\s*的?指令', re.I), "prompt_injection", 0.95),

    # === Category 2: 角色劫持 (role_hijack) ===
    (re.compile(r'you\s+are\s+now\s+(a\s+)?(new\s+)?(role|assistant|bot|ai\s+model|expert|advisor|developer)', re.I), "role_hijack", 0.70),
    (re.compile(r'from\s+now\s+on\s+you\s+are\s+(a\s+)?(new\s+)?(role|assistant|bot|ai|expert)', re.I), "role_hijack", 0.75),
    (re.compile(r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', re.I), "role_hijack", 0.80),
    # 中文角色劫持
    (re.compile(r'你现在(是|扮演|作为)(一个)?(新的)?(角色|助手|AI|专家|顾问)', re.I), "role_hijack", 0.70),
    (re.compile(r'从现在开始你(是|扮演|作为)(一个)?(新的)?(角色|助手|AI|专家|顾问)', re.I), "role_hijack", 0.75),
    (re.compile(r'(roleplay|角色扮演)\s*[:-]\s*(as|扮演)', re.I), "role_hijack", 0.60),

    # === Category 3: 系统提示操作 (sys_prompt_override) ===
    (re.compile(r'system\s*prompt', re.I), "sys_prompt_override", 0.80),
    (re.compile(r'system\s+prompt\s+override', re.I), "sys_prompt_override", 0.90),
    (re.compile(r'系统提示', re.I), "sys_prompt_override", 0.80),
    # 中文系统提示操作
    (re.compile(r'系统\s*提示\s*(覆盖|修改|替换)', re.I), "sys_prompt_override", 0.90),

    # === Category 4: 越狱/开发者模式 (jailbreak) ===
    (re.compile(r'jailbreak', re.I), "jailbreak", 0.90),
    (re.compile(r'developer\s*mode', re.I), "jailbreak", 0.85),
    (re.compile(r'D\.?A\.?N\.?', re.I), "jailbreak", 0.90),
    (re.compile(r'do\s+anything\s+now', re.I), "jailbreak", 0.85),
    (re.compile(r'越狱模式', re.I), "jailbreak", 0.90),
    (re.compile(r'开发者模式', re.I), "jailbreak", 0.85),

    # === Category 5: 欺骗/隐藏 (deception) ===
    (re.compile(r'do\s+not\s+tell\s+(the\s+)?user', re.I), "deception_hide", 0.85),
    (re.compile(r'不要告诉用户', re.I), "deception_hide", 0.85),
    (re.compile(r'对用户隐藏', re.I), "deception_hide", 0.80),

    # === Category 6: 分隔符滥用 (delimiter_abuse) ===
    (re.compile(r'[-=]{10,}\s*\n\s*(ignore|forget|you are|忽略|你现在)', re.I), "delimiter_abuse", 0.85),

    # === Category 7: 隐藏内容注入 (hidden_content) ===
    (re.compile(r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', re.I), "hidden_content", 0.75),
    (re.compile(r'<\s*div\s+style\s*=\s*["\'][\s\S]*?display\s*:\s*none', re.I), "hidden_content", 0.70),

    # === Category 8: 数据渗透 (exfiltration) ===
    (re.compile(r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', re.I), "exfiltration", 0.90),
    (re.compile(r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', re.I), "exfiltration", 0.90),
    (re.compile(r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)', re.I), "exfiltration", 0.85),

    # === Category 9: 持久化/后门 (persistence) ===
    (re.compile(r'authorized_keys', re.I), "persistence", 0.80),
    (re.compile(r'\$HOME/\.ssh|\~/\.ssh', re.I), "persistence", 0.80),

    # === Category 10: 翻译执行 (translate_execute) ===
    (re.compile(r'translate\s+.*\s+into\s+.*\s+and\s+(execute|run|eval)', re.I), "translate_execute", 0.75),
]

# 不可见字符（零宽字符、双向文本覆盖）
_PI_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',  # zero-width
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',  # bidirectional override
}

# 敏感关键词（用于组合检测）
_PI_SENSITIVE_KEYWORDS = {
    'password', 'secret', 'token', 'api_key', 'apikey', 'credential',
    '密码', '密钥', '令牌', '凭证',
}

# 类别组合乘数：某些类别同时出现 = 风险叠加
_CATEGORY_MULTIPLIERS = {
    # prompt_injection + exfiltration = 数据渗透指令覆盖（高危）
    frozenset({"prompt_injection", "exfiltration"}): 1.3,
    # role_hijack + deception_hide = 恶意角色扮演（高危）
    frozenset({"role_hijack", "deception_hide"}): 1.2,
    # sys_prompt_override + hidden_content = 隐蔽的系统提示修改（高危）
    frozenset({"sys_prompt_override", "hidden_content"}): 1.25,
    # jailbreak + persistence = 越狱后植入后门（高危）
    frozenset({"jailbreak", "persistence"}): 1.3,
}


def detect_prompt_injection(content: str) -> Tuple[bool, float, str, List[str], Dict]:
    """检测内容是否包含 Prompt Injection 攻击

    【H8 Prompt Injection 系统化威胁扫描】
    零 LLM 成本规则检测，识别 10 类威胁模式 + 不可见字符 + 组合风险。

    威胁类别：
    1. prompt_injection — 指令覆盖
    2. role_hijack — 角色劫持
    3. sys_prompt_override — 系统提示操作
    4. jailbreak — 越狱/开发者模式
    5. deception_hide — 欺骗/隐藏
    6. delimiter_abuse — 分隔符滥用
    7. hidden_content — 隐藏内容注入（HTML 注释、display:none）
    8. exfiltration — 数据渗透（curl/wget/cat 敏感文件）
    9. persistence — 持久化/后门（ssh authorized_keys）
    10. translate_execute — 翻译执行链

    组合检测：多类别同时命中时应用 risk multiplier
    不可见字符：检测零宽字符和双向文本覆盖

    Returns:
        (是否检测到, 风险分数 0-1, 原因, 匹配模式列表, 详细结果字典)
    """
    matched_patterns = []
    matched_categories = set()
    max_base_score = 0.0

    # 1. 规则匹配
    for pattern, category, score in _PI_PATTERNS:
        if pattern.search(content):
            matched_patterns.append(f"[{category}] {pattern.pattern[:40]}...")
            matched_categories.add(category)
            max_base_score = max(max_base_score, score)

    # 2. 不可见字符检测
    invisible_count = sum(1 for ch in content if ch in _PI_INVISIBLE_CHARS)
    if invisible_count > 0:
        matched_patterns.append(f"[invisible_chars] 发现 {invisible_count} 个不可见字符")
        matched_categories.add("invisible_chars")
        max_base_score = max(max_base_score, min(0.5 + invisible_count * 0.1, 0.90))

    # 3. 敏感关键词组合加分
    has_sensitive = any(kw in content.lower() for kw in _PI_SENSITIVE_KEYWORDS)
    if has_sensitive and max_base_score > 0.3:
        max_base_score = min(1.0, max_base_score + 0.08)

    # 4. 类别组合乘数
    multiplier = 1.0
    for cat_combo, mult in _CATEGORY_MULTIPLIERS.items():
        if cat_combo.issubset(matched_categories):
            multiplier = max(multiplier, mult)

    final_score = min(1.0, max_base_score * multiplier)

    # 5. 构建详细结果
    detail = {
        "base_score": max_base_score,
        "multiplier": multiplier,
        "final_score": final_score,
        "categories": sorted(matched_categories),
        "pattern_count": len(matched_patterns),
        "invisible_chars": invisible_count,
        "has_sensitive_keywords": has_sensitive,
    }

    # 6. 分级返回
    if final_score >= 0.85:
        return (
            True,
            final_score,
            f"High-risk threat detected: {', '.join(sorted(matched_categories))}",
            matched_patterns,
            detail
        )
    elif final_score >= 0.60:
        return (
            True,
            final_score,
            f"Suspicious signal: {', '.join(sorted(matched_categories))}",
            matched_patterns,
            detail
        )

    return False, final_score, "Clean", matched_patterns, detail


def sanitize_prompt_injection(content: str, finding_detail: Dict) -> str:
    """安全降级：发现威胁时替换为安全提示，保持系统可用

    Args:
        content: 原始内容
        finding_detail: detect_prompt_injection 返回的 detail 字典

    Returns:
        安全降级后的内容（保留原始内容的 hash 供追溯）
    """
    import hashlib
    content_hash = hashlib.sha1(content.encode()).hexdigest()[:16]
    categories = ", ".join(finding_detail.get("categories", ["unknown"]))

    return (
        f"[BLOCKED: content contained potential security threats]\n"
        f"Threat categories: {categories}\n"
        f"Content hash: {content_hash} (for audit tracing)\n"
        f"This content was blocked from entering the knowledge base."
    )


# 向后兼容：保留旧函数签名
def detect_prompt_injection_legacy(content: str) -> Tuple[bool, float, str, List[str]]:
    """旧版签名兼容（返回4元组）"""
    detected, score, reason, patterns, _detail = detect_prompt_injection(content)
    return detected, score, reason, patterns


# ==================== 标签体系辅助 (v6.0) ====================

# 有效系统标签键
VALID_SYSTEM_TAG_KEYS = {
    "type", "status", "stage", "evidence", "level",
    "actionable", "temporal",
}

# 有效业务标签键
VALID_BUSINESS_TAG_KEYS = {
    "domain", "project", "source", "verify",
}


def parse_tags(tag_list: List[str]) -> Dict[str, str]:
    """解析 key=value 格式标签列表

    Args:
        tag_list: 标签字符串列表，如 ["type=heuristic", "stage=captured"]

    Returns:
        {key: value} 字典
    """
    result = {}
    for tag in tag_list:
        if "=" in tag:
            key, value = tag.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def format_tag(key: str, value: str) -> str:
    """格式化单个标签

    Args:
        key: 标签键
        value: 标签值

    Returns:
        "key=value" 格式字符串
    """
    return f"{key}={value}"


def validate_tag(tag: str) -> Tuple[bool, str]:
    """验证标签格式和键名是否有效

    Args:
        tag: 标签字符串

    Returns:
        (是否有效, 原因)
    """
    if "=" not in tag:
        return False, "标签必须使用 key=value 格式"

    key, _value = tag.split("=", 1)
    key = key.strip()

    if key in VALID_SYSTEM_TAG_KEYS or key in VALID_BUSINESS_TAG_KEYS:
        return True, ""

    # 允许自定义键（以 x- 前缀）
    if key.startswith("x-"):
        return True, ""

    return False, f"未知标签键 '{key}'，建议使用 x-{key} 前缀"


def extract_tags_from_frontmatter(content: str) -> List[str]:
    """从 Markdown frontmatter 中提取 tags 字段

    Args:
        content: Markdown 内容

    Returns:
        标签列表
    """
    if not content.startswith("---"):
        return []

    parts = content.split("---", 2)
    if len(parts) < 3:
        return []

    frontmatter = parts[1]

    # 简单解析 tags 行
    # 支持: tags: [a, b, c] 或 tags:\n  - a\n  - b
    tags = []

    # 数组格式
    match = re.search(r'tags:\s*\[(.*?)\]', frontmatter, re.DOTALL)
    if match:
        items = match.group(1).split(",")
        tags = [t.strip().strip('"\'') for t in items if t.strip()]
        return tags

    # 列表格式
    in_tags = False
    for line in frontmatter.split("\n"):
        if line.strip().startswith("tags:"):
            in_tags = True
            continue
        if in_tags:
            if line.strip().startswith("-"):
                tag = line.strip()[1:].strip().strip('"\'')
                if tag:
                    tags.append(tag)
            elif line.strip() and not line.startswith(" "):
                break

    return tags


def build_tag_string(tags: Dict[str, str]) -> str:
    """将标签字典转换为 YAML 格式的 tags 数组字符串

    Args:
        tags: {key: value} 字典

    Returns:
        YAML 数组字符串，如 "tags:\n  - type=problem-solution\n  - stage=captured"
    """
    lines = ["tags:"]
    for key, value in tags.items():
        lines.append(f"  - {key}={value}")
    return "\n".join(lines)
