"""
MCP Server - Model Context Protocol 服务器

职责：
- 通过 stdin/stdout 与 AI Agent 通信
- 暴露 mnemos 的能力作为 MCP tools
- 支持：知识库查询、用户画像读取、信号采集触发、KIA 闭环（预加载、守护）

协议：MCP (Model Context Protocol) over JSON-RPC 2.0
传输：stdio
"""
# Agora — 古希腊广场 — MCP 协议中心，公共交流场所
# 原模块: mcp_server.py



import json
import os
import re
import sys
import logging
from typing import Dict, List, Optional, Any
from pathlib import Path

# 配置日志到 stderr，避免污染 stdout（MCP 协议通道）
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [mcp] %(levelname)s: %(message)s"
)

# JSON-RPC 2.0 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_TOOL_EXECUTION_ERROR = -32000


class MCPServer:
    """MCP 服务器 - stdio 模式，JSON-RPC 2.0"""

    def __init__(self):
        self.tools = self._register_tools()

    def _register_tools(self) -> Dict[str, Any]:
        """注册可用 tools"""
        return {
            "wiki_search": self._tool_wiki_search,
            "wiki_read": self._tool_wiki_read,
            "wiki_write": self._tool_wiki_write,
            "session_search": self._tool_session_search,
            "capture_turn": self._tool_capture_turn,
            "capture_session": self._tool_capture_session,
            "end_session": self._tool_end_session,
            "capture_status": self._tool_capture_status,
            "knowledge_ingest": self._tool_knowledge_ingest,
            "knowledge_import": self._tool_knowledge_import,
            "knowledge_distill": self._tool_knowledge_distill,
            "document_process": self._tool_document_process,
            "wiki_build": self._tool_wiki_build,
            "preflight_inject": self._tool_preflight_inject,
            "guard_check": self._tool_guard_check,
            "persona_summary": self._tool_persona_summary,
            "persona_behavior_prompt": self._tool_persona_behavior_prompt,
            "persona_update": self._tool_persona_update,
            "signal_collect": self._tool_signal_collect,
            "retrospective_list": self._tool_retrospective_list,
            "knowledge_source_list": self._tool_knowledge_source_list,
            "health_check": self._tool_health_check,
            "self_diagnose": self._tool_self_diagnose,
            "configure_memos": self._tool_configure_memos,
            "configure_wiki": self._tool_configure_wiki,
            "detect_sources": self._tool_detect_sources,
            "context_aware_search": self._tool_context_aware_search,
            "intent_route": self._tool_intent_route,
            "blindspot_check": self._tool_blindspot_check,
            "predictive_push": self._tool_predictive_push,
            "freshness_check": self._tool_freshness_check,
        }

    # ---- Tool 实现 ----

    def _tool_wiki_search(self, query: str, limit: int = 5) -> Dict:
        """搜索知识库

        统一搜索入口：优先 ContextAwareSearch（KG 召回 + 正文搜索 + 画像加权），
        无结果时回退到 WikiReader（标题/实体/概念/路径索引）。
        """
        from core.config import get_config
        from core.app.context_search import ContextAwareSearch
        from integrations.oracle import WikiReader

        wiki_dir = get_config().wiki_dir
        results = []

        # 1. 优先使用 ContextAwareSearch（更完善的搜索：正文 + frontmatter + KG + 画像加权）
        try:
            searcher = ContextAwareSearch(wiki_base=str(wiki_dir))
            ca_results = searcher.search(query, limit=limit)
            for r in ca_results:
                results.append({
                    "page_id": r.page_path.replace(".md", ""),
                    "title": r.title,
                    "type": self._infer_type_from_path(r.page_path),
                    "heat_level": "warm",
                    "heat_score": round(r.score * 100, 1),
                    "relevance_score": round(r.relevance, 2),
                    "reasons": [r.match_reason or "context_search"],
                    "verification": getattr(r, "verification", ""),
                    "confidence": getattr(r, "confidence", 0.5),
                })
        except Exception:
            logger.debug("ContextAwareSearch 失败，回退到 WikiReader", exc_info=True)

        # 2. 回退到 WikiReader（如果 ContextAwareSearch 无结果）
        if not results:
            reader = WikiReader(wiki_dir)
            results = reader.search(query, limit=limit)

        # 记录训练样本
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            AdaptiveScorerV2.enqueue_training_sample(
                session_id=f"search-{hash(query) & 0xFFFFFFFF}",
                dimension="kg",
                features={"query_length": len(query), "result_count": len(results), "tool": "wiki_search"},
                expected_score=0.7 if results else 0.4,
                source="wiki_search",
            )
        except Exception:
            pass

        return {
            "success": True,
            "results": results,
            "query": query,
        }

    def _infer_type_from_path(self, page_path: str) -> str:
        """从路径推断知识类型"""
        if "/" in page_path:
            return page_path.split("/")[0]
        return "00-Inbox"

    def _tool_wiki_read(self, page_path: str) -> Dict:
        """读取指定 wiki 页面"""
        from core.config import get_config
        from integrations.oracle import WikiReader
        wiki_dir = get_config().wiki_dir
        reader = WikiReader(wiki_dir)
        content = reader.read_page(page_path)
        # 记录训练样本：用户阅读页面 → 正样本（distill 维度）
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            AdaptiveScorerV2.enqueue_training_sample(
                session_id=f"read-{hash(page_path) & 0xFFFFFFFF}",
                dimension="distill",
                features={"page_path": page_path, "content_length": len(content) if content else 0, "tool": "wiki_read"},
                expected_score=0.6,
                source="wiki_read",
            )
        except Exception:
            pass
        return {
            "success": True,
            "content": content,
            "path": page_path,
        }

    def _tool_session_search(self, query: str = "", session_id: str = "",
                             memos_uid: str = "", limit: int = 10) -> Dict:
        """
        搜索历史会话记录，自动合并分片内容

        使用场景：
        - 用户说"我们之前聊过什么"
        - 需要查找某次 session 的完整对话
        - 通过 memos_uid 反查原始对话

        特性：
        - 自动检测分段记录（segment=xxx, type=chunk）
        - 按 hash/session 标识合并所有分段为完整对话
        - 支持 memos_uid 反查
        """
        from core.config import get_config
        from integrations.styx import MemosClient

        config = get_config()
        if not config.memos_enabled or not config.memos_token:
            return {
                "success": False,
                "message": "Memos 未配置，无法搜索会话记录",
            }

        try:
            client = MemosClient(
                base_url=config.memos_api_url,
                token=config.memos_token,
            )

            # 如果提供了 memos_uid，反查 session_id
            if memos_uid and not session_id:
                session_id = client.get_session_by_uid(memos_uid)

            # 如果提供了 session_id，构造精确查询
            if session_id:
                search_query = f"session:{session_id}"
            else:
                search_query = query

            results = client.search_and_merge_segments(
                query=search_query,
                limit=limit,
            )

            # 序列化 Memory 对象
            serialized = []
            for mem in results:
                serialized.append({
                    "id": mem.id,
                    "uid": mem.uid,
                    "content": mem.content,
                    "tags": mem.tags,
                    "visibility": mem.visibility,
                    "created_at": mem.created_at,
                    "updated_at": mem.updated_at,
                    "agent": mem.agent,
                })

            return {
                "success": True,
                "query": search_query,
                "results": serialized,
                "count": len(serialized),
                "merged": True,
            }
        except Exception as e:
            logger.error(f"会话搜索失败: {type(e).__name__}", exc_info=True)
            return {
                "success": False,
                "message": "搜索失败",
            }

    def _tool_knowledge_ingest(self, content: str, tags: List[str] = None,
                               source: str = "human") -> Dict:
        """
        知识摄入 — 将用户主动提供的人工知识写入 Memos，进入知识库处理链路。

        完整流程：
        1. Agent 调用此工具，把用户口头/输入的知识存入 Memos
        2. Memos 同步机制将内容同步到 Wiki 的 00-Inbox/
        3. Charon（Connect Worker）对内容进行语义索引、实体提取、标签构建、热度评分
        4. 知识正式纳入图谱，可被 wiki_search / wiki_read 检索

        这是除 Memos 自动同步、Agent 对话蒸馏、Git 历史提取之外，
        另一个重要的知识入口：用户主动投喂。
        """
        from core.config import get_config
        from integrations.styx import MemosClient

        config = get_config()
        if not config.memos_enabled or not config.memos_token:
            return {
                "success": False,
                "message": "Memos 未配置，无法摄入知识。请在 ~/.mnemos/configs/main.json 中配置 memos.token 和 memos.api_url",
            }

        try:
            client = MemosClient(
                base_url=config.memos_api_url,
                token=config.memos_token,
            )
            # 自动添加 source 标签，便于后续溯源
            ingest_tags = list(tags or [])
            if source not in ingest_tags:
                ingest_tags.append(source)
            if "mnemos-ingest" not in ingest_tags:
                ingest_tags.append("mnemos-ingest")

            memory = client.save(content, tags=ingest_tags)
            return {
                "success": True,
                "message": "知识已成功摄入 Memos，将自动同步到 Wiki 并经过解析器处理",
                "memo_id": memory.id,
                "tags": memory.tags,
                "ingested_length": len(content),
                "pipeline": "Memos → Wiki 00-Inbox → Charon(语义索引/标签/热度) → 知识图谱",
            }
        except Exception as e:
            logger.error(f"知识摄入失败: {type(e).__name__}", exc_info=True)
            return {
                "success": False,
                "message": "摄入失败",
            }

    def _tool_knowledge_import(self, file_path: str, title: str = "",
                                tags: List[str] = None,
                                trigger_parse: bool = True) -> Dict:
        """
        知识导入 — 将用户指定的本地文件解析并存入知识库

        使用场景：
        - 用户说"把这个文件加入知识库：~/notes/architecture.md"
        - 用户说"解析这个代码文件，提取设计模式"
        - 用户说"把这份文档存进去，以后好查"

        处理流程：
        1. 读取指定路径的文件内容
        2. 根据文件类型处理（.md 保留原格式，代码文件加代码块包装）
        3. 添加 frontmatter（来源标记 file_import，原始路径，导入时间）
        4. 写入 Wiki 00-Inbox/
        5. 立即触发 Charon 解析（语义索引、实体提取、标签、热度评分）
        """
        from core.config import get_config
        from core.kia.charon import run_connect_cycle
        from pathlib import Path
        from datetime import datetime
        import mimetypes

        src_path = Path(file_path).expanduser().resolve()
        if not src_path.exists():
            return {
                "success": False,
                "message": f"文件不存在: {file_path}",
            }

        if not src_path.is_file():
            return {
                "success": False,
                "message": f"路径不是文件: {file_path}",
            }

        # 读取内容
        try:
            raw_bytes = src_path.read_bytes()
            # 尝试 UTF-8，失败则用 latin-1（保证不丢数据）
            try:
                content = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = raw_bytes.decode("latin-1")
        except Exception as e:
            return {
                "success": False,
                "message": f"读取文件失败: {e}",
            }

        # 文件类型处理
        suffix = src_path.suffix.lower()
        code_exts = {
            ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".cpp",
            ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
            ".bash", ".zsh", ".fish", ".ps1", ".pl", ".lua", ".r", ".m", ".mm",
            ".sql", ".dockerfile", ".makefile", ".cmake", ".gradle", ".svelte",
            ".vue", ".html", ".css", ".scss", ".sass", ".less", ".xml", ".json",
        }
        text_exts = {".txt", ".log", ".csv", ".tsv", ".ini", ".cfg", ".conf", ".toml"}

        # 生成标题
        doc_title = title or src_path.stem
        safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', doc_title).strip()[:60]

        # 构建 markdown 内容
        if suffix == ".md":
            # Markdown 文件：保留内容，在开头添加/合并 frontmatter
            if content.startswith("---"):
                # 已有 frontmatter，追加我们的标记
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    existing_fm = parts[1]
                    body = parts[2].lstrip("\n")
                    new_fm = f"""---
{existing_fm.rstrip()}
imported_from: {src_path}
imported_at: {datetime.now().isoformat()}
source: file_import
tags: [{', '.join(tags or ['file_import'])}]
---

"""
                    md_content = new_fm + body
                else:
                    md_content = content
            else:
                md_content = f"""---
title: {safe_title}
imported_from: {src_path}
imported_at: {datetime.now().isoformat()}
source: file_import
tags: [{', '.join(tags or ['file_import'])}]
---

{content}
"""
        elif suffix in code_exts:
            # 代码文件：包装为 markdown 代码块
            lang = suffix.lstrip(".")
            if lang == "js":
                lang = "javascript"
            elif lang == "ts":
                lang = "typescript"
            elif lang == "py":
                lang = "python"
            elif lang == "sh" or lang == "bash" or lang == "zsh":
                lang = "bash"
            elif lang == "dockerfile":
                lang = "dockerfile"
            elif lang == "makefile":
                lang = "makefile"
            elif lang == "html":
                lang = "html"
            elif lang == "css" or lang == "scss" or lang == "sass" or lang == "less":
                lang = "css"
            elif lang == "json":
                lang = "json"
            elif lang == "xml":
                lang = "xml"
            elif lang == "sql":
                lang = "sql"
            elif lang == "yaml" or lang == "yml":
                lang = "yaml"

            md_content = f"""---
title: {safe_title}
imported_from: {src_path}
imported_at: {datetime.now().isoformat()}
source: file_import
file_type: code
language: {lang}
tags: [{', '.join(tags or ['file_import', 'code'])}]
---

# {safe_title}

原始路径：`{src_path}`

```{lang}
{content}
```
"""
        elif suffix in text_exts or suffix in {".yaml", ".yml"}:
            # 纯文本文件
            md_content = f"""---
title: {safe_title}
imported_from: {src_path}
imported_at: {datetime.now().isoformat()}
source: file_import
file_type: text
tags: [{', '.join(tags or ['file_import', 'text'])}]
---

# {safe_title}

原始路径：`{src_path}`

```text
{content}
```
"""
        else:
            # 其他类型：尝试文本展示
            md_content = f"""---
title: {safe_title}
imported_from: {src_path}
imported_at: {datetime.now().isoformat()}
source: file_import
file_type: {suffix.lstrip('.') or 'unknown'}
tags: [{', '.join(tags or ['file_import'])}]
---

# {safe_title}

原始路径：`{src_path}`
文件类型：{suffix or 'unknown'}

```text
{content}
```
"""

        # 写入 Inbox
        try:
            config = get_config()
            inbox_dir = config.wiki_dir / "00-Inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            inbox_name = f"{ts}-import-{safe_title[:30]}.md"
            inbox_path = inbox_dir / inbox_name
            inbox_path.write_text(md_content, encoding="utf-8")
        except Exception as e:
            return {
                "success": False,
                "message": f"写入 Inbox 失败: {e}",
            }

        # 触发 Charon 解析
        parse_result = None
        if trigger_parse:
            try:
                parse_result = run_connect_cycle(dry_run=False)
            except Exception as e:
                logger.warning(f"Charon 解析触发失败: {type(e).__name__}", exc_info=True)
                parse_result = {"error": "parse_failed"}

        return {
            "success": True,
            "message": f"文件已导入知识库: {inbox_path.name}",
            "original_path": str(src_path),
            "inbox_path": str(inbox_path),
            "title": safe_title,
            "file_size": len(raw_bytes),
            "content_length": len(content),
            "pipeline": "文件 → 00-Inbox → Charon(语义索引/实体提取/标签/热度) → 知识图谱",
            "parse_result": parse_result,
        }

    def _tool_preflight_inject(self, task_type: str, subtype: str = "",
                               context_text: str = "") -> Dict:
        """
        KIA 闭环 - 任务前知识装载

        根据任务类型从 retrospective 经验库装载历史教训和检查清单。
        这是 KIA（Knowledge-in-Action）闭环的第一步。
        """
        from core.kia.prophasis import PreFlightInjector
        from core.kia.kairos import TimeWindow, TimeWindowType

        injector = PreFlightInjector()
        # MCP 调用通常是即时任务
        time_window = TimeWindow(
            window=TimeWindowType.IMMEDIATE,
            days_until=0,
        )
        knowledge = injector.inject(task_type, subtype, time_window, context_text)

        if not knowledge:
            return {
                "success": True,
                "loaded": False,
                "message": f"未找到 {task_type}/{subtype} 的历史经验",
            }

        return {
            "success": True,
            "loaded": True,
            "task_type": knowledge.task_type,
            "subtype": knowledge.subtype,
            "version": knowledge.version,
            "checklist_count": len(knowledge.checklist),
            "checklist": [
                {
                    "item": c.item,
                    "severity": c.severity,
                    "freshness_score": round(c.freshness_score, 2),
                    "hit_count": c.hit_count,
                }
                for c in knowledge.checklist
            ],
            "lessons_summary": knowledge.lessons_summary,
        }

    def _tool_guard_check(self, user_message: str, ai_response: str = "",
                          task_type: str = "", subtype: str = "",
                          context: Dict = None) -> Dict:
        """
        KIA 闭环 - 执行中守护检查

        检测当前对话是否触及历史经验中的风险点。
        需要先调用 preflight_inject 装载知识（会自动复用）。
        这是 KIA 闭环的第二步。
        """
        from core.kia.prophasis import PreFlightInjector
        from core.kia.aegis import InProcessGuard
        from core.kia.kairos import TimeWindow, TimeWindowType

        # 1. 装载知识（如果未装载）
        injector = PreFlightInjector()
        time_window = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
        knowledge = injector.inject(task_type, subtype, time_window, "")

        # 无清单时回退到高风险默认规则
        if not knowledge or not knowledge.checklist:
            from core.kia.prophasis import LoadedKnowledge, ChecklistItem
            knowledge = LoadedKnowledge(
                task_type=task_type or "general",
                subtype=subtype or "",
                version=1,
                checklist=[
                    ChecklistItem(
                        item="涉及删除/覆盖/生产环境/密钥/不可逆迁移的操作，请二次确认",
                        source="default_guard",
                        severity="critical",
                        trigger_keywords=["删除", "清空", "覆盖", "drop", "truncate", "rm", "生产", "prod", "密钥", "key", "迁移", "migrate"],
                        risk_patterns=[r"删除.*?(文件|数据|表|目录)", r"覆盖.*?配置", r"生产.*?部署", r"(api[_-]?key|token|密码|secret)"],
                    ),
                    ChecklistItem(
                        item="未测试的代码提交可能导致回滚风险",
                        source="default_guard",
                        severity="high",
                        trigger_keywords=["提交", "commit", "push", "合并", "merge"],
                        risk_patterns=[r"提交.*?(未测试|没测试|无测试)"],
                    ),
                ],
                lessons_summary="",
            )

        # 2. 初始化 Guard 并检查
        guard = InProcessGuard(knowledge)
        alert = guard.check(user_message, ai_response, context=context)

        if not alert:
            return {
                "success": True,
                "alert": False,
                "message": "无风险触发",
            }

        # 记录训练样本：guard 触发警报 → 负样本（ops 维度，规则可能过度敏感）
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            AdaptiveScorerV2.enqueue_training_sample(
                session_id=f"guard-{hash(user_message) & 0xFFFFFFFF}",
                dimension="ops",
                features={"triggered_by": alert.triggered_by, "level": alert.level.value, "tool": "guard_check"},
                expected_score=0.2,
                source="guard_check",
            )
        except Exception:
            pass

        level_val = alert.level.value
        risk_level = "alert" if level_val == "interrupt" else "warn" if level_val == "hint" else "info"
        return {
            "success": True,
            "alert": True,
            "risk_level": risk_level,
            "level": level_val,
            "triggered_by": alert.triggered_by,
            "trigger_text": alert.trigger_text,
            "suggestion": alert.suggestion,
            "checklist_item": alert.checklist_item.item if alert.checklist_item else "",
            "severity": alert.checklist_item.severity if alert.checklist_item else "medium",
        }

    def _tool_persona_summary(self) -> Dict:
        """获取用户画像摘要"""
        from core.persona.pythia import PreferenceAnalyzer
        analyzer = PreferenceAnalyzer()
        profile = analyzer.analyze(days=30)
        return {
            "success": True,
            "profile": {
                "energy": profile.energy.__dict__ if profile.energy else {},
                "cognitive": profile.cognitive.__dict__ if profile.cognitive else {},
                "value": profile.value.__dict__ if profile.value else {},
                "insufficient_dimensions": list(profile.insufficient_dimensions) if hasattr(profile, 'insufficient_dimensions') else [],
            },
        }

    def _tool_persona_behavior_prompt(self) -> Dict:
        """获取画像驱动的 AI 行为提示词（含 Mnemos 连接指南）"""
        from core.persona.pythia import PreferenceAnalyzer

        # 1. 画像分析
        analyzer = PreferenceAnalyzer()
        profile = analyzer.analyze(days=30)
        prompts = []
        if profile.energy:
            e = profile.energy
            if e.focus_depth and e.focus_depth > 0.7:
                prompts.append("用户偏好深度专注模式，避免频繁打断。")
            if e.endurance_mode and e.endurance_mode > 0.6:
                prompts.append("用户耐力较好，可接受长周期任务。")
        if profile.cognitive:
            c = profile.cognitive
            if c.abstraction and c.abstraction > 0.7:
                prompts.append("用户抽象能力强，可直接给高层设计。")
            if c.skepticism and c.skepticism > 0.6:
                prompts.append("用户有质疑习惯，主动暴露假设和局限。")
        if profile.value:
            v = profile.value
            if v.correctness_vs_efficiency and v.correctness_vs_efficiency > 0.6:
                prompts.append("用户重正确性轻效率，宁可慢也要对。")
            if v.perfection_vs_completion and v.perfection_vs_completion > 0.6:
                prompts.append("用户追求完美，注意细节和边界条件。")

        # 2. Mnemos 宿主 Agent 连接指南（提示词注入）
        onboarding = self._load_onboarding_prompt()

        return {
            "success": True,
            "behavior_prompts": prompts,
            "onboarding_prompt": onboarding,
            "raw_profile": {
                "energy": profile.energy.__dict__ if profile.energy else {},
                "cognitive": profile.cognitive.__dict__ if profile.cognitive else {},
                "value": profile.value.__dict__ if profile.value else {},
            },
        }

    def _load_onboarding_prompt(self) -> str:
        """加载宿主 Agent 连接指南（根据当前系统状态动态裁剪）"""
        from core.diagnostics import ConnectionDiagnostics

        onboarding_path = Path(__file__).parent.parent / "prompts" / "agent_onboarding.md"
        if onboarding_path.exists():
            base = onboarding_path.read_text(encoding="utf-8")
        else:
            base = (
                "\n[Mnemos Onboarding]\n"
                "你是 Mnemos 的宿主 Agent。请帮用户完成以下连接任务：\n"
                "1. 调用 self_diagnose() 查看系统状态\n"
                "2. 如有 Memos，调用 configure_memos(api_url=..., token=...)\n"
                "3. 确认 Wiki 路径，调用 configure_wiki(vault_path=...)\n"
                "4. 调用 detect_sources() 检查 Agent 数据源\n"
            )

        # 获取当前状态，动态标注任务完成度
        try:
            report = ConnectionDiagnostics.full_report()
            tasks = report.get("tasks", [])

            # 构建状态摘要
            lines = ["\n[Mnemos 连接状态快照]"]
            pending_high = [t for t in tasks if t.get("priority") == "high" and not t.get("completed")]
            pending_medium = [t for t in tasks if t.get("priority") == "medium" and not t.get("completed")]

            if not pending_high and not pending_medium:
                lines.append("✓ 所有核心连接已就绪，Mnemos 完全在线。")
            else:
                if pending_high:
                    lines.append("🔴 高优先级待办:")
                    for t in pending_high:
                        lines.append(f"  • {t['task']}: {t['action']}")
                if pending_medium:
                    lines.append("🟡 中优先级待办:")
                    for t in pending_medium:
                        lines.append(f"  • {t['task']}: {t['action']}")

            # 合并到 onboarding 末尾
            return base + "\n" + "\n".join(lines) + "\n"
        except Exception:
            # 诊断失败时返回原始模板
            return base

    def _tool_signal_collect(self, sources: List[str] = None) -> Dict:
        """触发信号采集"""
        from core.persona.daimon import SignalCollector
        collector = SignalCollector()
        results = collector.collect_all(sources=sources)
        return {
            "success": True,
            "results": results,
        }

    def _tool_retrospective_list(self, task_type: str = None, limit: int = 10) -> Dict:
        """列出可用的 retrospective 经验"""
        from core.config import get_config
        wiki_dir = get_config().wiki_dir
        # 优先 06-Retrospectives，兼容 retrospectives
        retro_dir = None
        for d in [wiki_dir / "06-Retrospectives", wiki_dir / "retrospectives"]:
            if d.exists():
                retro_dir = d
                break
        if not retro_dir:
            return {"success": True, "retrospectives": []}

        items = []
        _MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB
        for md_file in sorted(retro_dir.rglob("*.md"), reverse=True):
            try:
                if md_file.stat().st_size > _MAX_FILE_SIZE:
                    logger.warning(f"Retrospective 文件过大跳过: {md_file.name}")
                    continue
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                title = md_file.stem
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            import yaml
                            fm = yaml.safe_load(parts[1]) or {}
                            title = fm.get("title", title)
                            task_types = fm.get("applies_when", {}).get("task_type", [])
                            if task_type and task_type not in task_types:
                                continue
                        except Exception as e:
                            logger.warning(f"YAML 解析失败: {e}")
                items.append({
                    "path": str(md_file.relative_to(retro_dir)),
                    "title": title,
                })
                if len(items) >= limit:
                    break
            except Exception as e:
                logger.warning(f"遍历文件失败: {e}")
                continue

        return {"success": True, "retrospectives": items}

    def _tool_knowledge_source_list(self) -> Dict:
        """列出知识库的来源分布统计"""
        from core.config import get_config
        wiki_dir = get_config().wiki_dir

        sources = {
            "human_written": 0,      # 人工直接写入（无 source 标记）
            "memos_sync": 0,         # Memos 同步
            "distilled": 0,          # Agent 对话蒸馏
            "retrospective": 0,      # 自动复盘
            "git_knowledge": 0,      # Git 历史提取
            "other": 0,              # 其他/未知
        }

        if not wiki_dir.exists():
            return {"success": True, "sources": sources, "total": 0}

        import yaml
        _MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB
        for md_file in wiki_dir.rglob("*.md"):
            try:
                if md_file.stat().st_size > _MAX_FILE_SIZE:
                    logger.warning(f"Wiki 文件过大跳过: {md_file.name}")
                    continue
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                src = "human_written"
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            fm = yaml.safe_load(parts[1]) or {}
                            from core.frontmatter import fm_get
                            tags = fm.get("tags", [])
                            # 根据标签判断来源
                            if "memos" in tags or "memos-sync" in tags:
                                src = "memos_sync"
                            elif "distilled" in tags:
                                src = "distilled"
                            elif "retrospective" in tags:
                                src = "retrospective"
                            elif "git" in tags:
                                src = "git_knowledge"
                            # 根据 frontmatter 中的 source / 来源 字段
                            explicit = fm_get(fm, "source", "")
                            if explicit in ("memos", "memos-sync"):
                                src = "memos_sync"
                            elif explicit in ("distill", "distilled"):
                                src = "distilled"
                            elif explicit == "retrospective":
                                src = "retrospective"
                            elif explicit == "git":
                                src = "git_knowledge"
                            elif explicit in ("claude", "kimi", "codex", "openclaw", "hermes"):
                                src = "distilled"
                        except Exception:
                            logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                            pass
                sources[src] = sources.get(src, 0) + 1
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at agora.py", exc_info=True)
                continue

        total = sum(sources.values())
        return {
            "success": True,
            "sources": sources,
            "total": total,
            "wiki_dir": str(wiki_dir),
        }

    def _tool_wiki_write(self, page_path: str, content: str,
                         frontmatter: Dict = None) -> Dict:
        """
        写入 Wiki 页面

        使用场景：
        - Agent 执行蒸馏后，将结果写入 Wiki
        - Agent 生成新的知识页面
        - 更新已有页面的内容
        """
        from core.config import get_config
        from datetime import datetime

        config = get_config()
        wiki_dir = config.wiki_dir

        # 安全路径处理
        safe_path = page_path.lstrip("/")
        target = (wiki_dir / safe_path).resolve()

        # 确保在 wiki 目录内
        try:
            target.relative_to(wiki_dir.resolve())
        except ValueError:
            return {
                "success": False,
                "message": f"路径超出 Wiki 目录范围: {page_path}",
            }

        target.parent.mkdir(parents=True, exist_ok=True)

        # 构建完整内容（frontmatter + body）
        fm = dict(frontmatter or {})
        fm.setdefault("updated_at", datetime.now().isoformat())

        fm_lines = ["---"]
        for k, v in fm.items():
            if isinstance(v, list):
                fm_lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
            else:
                fm_lines.append(f"{k}: {v}")
        fm_lines.append("---")

        full_content = "\n".join(fm_lines) + "\n\n" + content

        try:
            target.write_text(full_content, encoding="utf-8")
            return {
                "success": True,
                "message": f"Wiki 页面已写入: {safe_path}",
                "path": safe_path,
                "size": len(full_content),
            }
        except Exception as e:
            logger.error(f"Wiki 写入失败: {e}")
            return {
                "success": False,
                "message": f"写入失败: {e}",
            }

    def _tool_session_save(self, session_id: str, messages: List[Dict],
                           tags: List[str] = None,
                           source_agent: str = "unknown") -> Dict:
        """
        保存完整聊天记录到 Memos（L1 原始池）

        ⚠️ Deprecated: 请使用 capture_turn / capture_session。
        此工具现已统一走 CaptureService，不再直接调用 MemosClient。
        """
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService()
            # 将 messages 转为 turns 格式
            turns = []
            user_content = ""
            assistant_content = ""
            turn_number = 0
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    if assistant_content:
                        turns.append({
                            "turn_number": turn_number,
                            "user_content": user_content,
                            "assistant_content": assistant_content,
                        })
                        turn_number += 1
                    user_content = content
                    assistant_content = ""
                elif role == "assistant":
                    assistant_content = content

            if user_content or assistant_content:
                turns.append({
                    "turn_number": turn_number,
                    "user_content": user_content,
                    "assistant_content": assistant_content,
                })

            result = service.capture_session(
                source_agent=source_agent,
                session_id=session_id,
                turns=turns,
            )
            return {
                "success": result.get("status") in ("queued", "duplicate", "done"),
                "message": f"Session 已入队: {result.get('queued_count', 0)} 轮次 queued, {result.get('duplicate_count', 0)} 重复",
                "session_id": session_id,
                "capture_result": result,
            }
        except Exception as e:
            logger.error(f"会话保存失败: {e}")
            return {
                "success": False,
                "message": f"保存失败: {e}",
            }

    def _tool_capture_turn(
        self,
        source_agent: str,
        session_id: str,
        turn_id: str = "",
        turn_number: int = 0,
        user_content: str = "",
        assistant_content: str = "",
        timestamp: str = "",
        model: str = "",
        cwd: str = "",
        metadata: Dict = None,
    ) -> Dict:
        """
        MCP 主动上报单轮对话。

        只做校验和入队，不直接写 Memos。
        返回 < 200ms。
        """
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService()
            result = service.capture_turn(
                source_agent=source_agent,
                session_id=session_id,
                turn_id=turn_id or None,
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                timestamp=timestamp or None,
                model=model or None,
                cwd=cwd or None,
                metadata=metadata or {},
            )
            return {
                "success": result["status"] in ("queued", "duplicate"),
                "status": result["status"],
                "duplicate": result.get("duplicate", False),
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": turn_number,
            }
        except Exception as e:
            logger.error(f"capture_turn 失败: {e}")
            return {
                "success": False,
                "status": "error",
                "message": str(e),
            }

    def _tool_capture_session(
        self,
        source_agent: str,
        session_id: str,
        turns: List[Dict],
    ) -> Dict:
        """
        MCP 批量上报整个 session。
        """
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService()
            result = service.capture_session(
                source_agent=source_agent,
                session_id=session_id,
                turns=turns,
            )
            return {
                "success": result.get("status") in ("queued", "duplicate"),
                "status": result["status"],
                "queued_count": result.get("queued_count", 0),
                "duplicate_count": result.get("duplicate_count", 0),
                "backpressure_count": result.get("backpressure_count", 0),
                "session_id": session_id,
            }
        except Exception as e:
            logger.error(f"capture_session 失败: {e}")
            return {
                "success": False,
                "status": "error",
                "message": str(e),
            }

    def _tool_end_session(
        self,
        source_agent: str,
        session_id: str,
    ) -> Dict:
        """
        标记 session 结束。
        """
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService()
            result = service.end_session(
                source_agent=source_agent,
                session_id=session_id,
            )
            return {
                "success": True,
                "status": result["status"],
                "session_id": session_id,
            }
        except Exception as e:
            logger.error(f"end_session 失败: {e}")
            return {
                "success": False,
                "status": "error",
                "message": str(e),
            }

    def _tool_capture_status(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int = -1,
    ) -> Dict:
        """
        查询指定 session/turn 的队列状态。
        """
        from core.sync_framework.capture_service import CaptureService

        try:
            service = CaptureService()
            result = service.get_status(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number if turn_number >= 0 else None,
            )
            return {
                "success": True,
                "status": result.get("status"),
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": result.get("turn_number"),
                "retry_count": result.get("retry_count", 0),
                "error": result.get("error"),
            }
        except Exception as e:
            logger.error(f"capture_status 失败: {e}")
            return {
                "success": False,
                "status": "error",
                "message": str(e),
            }

    def _tool_knowledge_distill(self, session_id: str,
                                messages: List[Dict],
                                write_to_wiki: bool = True) -> Dict:
        """
        触发知识蒸馏（同源复用 — 入队而非直接调 LLM）

        将原始聊天记录入蒸馏队列，由宿主 Agent 异步处理。
        遵循同源复用原则：Mnemos 不直接调用 LLM API。

        使用场景：
        - Agent 完成一次有价值的对话后，主动触发蒸馏
        - 将技术讨论、调试过程、设计决策转为 Wiki 知识
        """
        try:
            from core.kia.amphora import enqueue
            enqueue(
                session_id=session_id,
                messages=messages,
                meta={"source": "mcp", "write_to_wiki": write_to_wiki}
            )
            return {
                "success": True,
                "message": "蒸馏任务已入队，由宿主 Agent 异步处理",
                "session_id": session_id,
                "note": "任务进入 HephaestusWorker 队列，daemon 会委托给可用 Agent 执行",
            }
        except Exception as e:
            logger.error(f"蒸馏入队失败: {e}")
            return {
                "success": False,
                "message": f"蒸馏入队失败: {e}",
            }

    def _tool_document_process(self, file_path: str,
                               title: str = "",
                               save_to_memos: bool = True) -> Dict:
        """
        处理文档文件（PDF/PPT/Excel/Word/HTML/EBOOK）

        使用场景：
        - 用户说"解析这个 PDF"
        - 用户说"把这份 PPT 的内容存到知识库"
        - 提取文档结构、大纲、关键内容
        """
        from core.hephaestus.document_processor import DocumentProcessor
        from pathlib import Path

        src_path = Path(file_path).expanduser().resolve()
        if not src_path.exists():
            return {
                "success": False,
                "message": f"文件不存在: {file_path}",
            }

        try:
            processor = DocumentProcessor()
            doc = processor.process_document(src_path)

            if not doc:
                return {
                    "success": False,
                    "message": "文档解析失败，无法提取内容",
                }

            # 从 metadata 读取页数/章节等结构信息
            meta = doc.metadata or {}
            page_count = meta.get("pages", meta.get("slides", meta.get("chapters", 0)))

            # 从 Markdown 内容提取 TOC
            toc = []
            if doc.content:
                for line in doc.content.split("\n"):
                    if line.strip().startswith("#"):
                        toc.append(line.strip().lstrip("# "))

            result = {
                "success": True,
                "title": doc.title,
                "doc_type": doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type),
                "pages": page_count,
                "word_count": len(doc.content.split()) if doc.content else 0,
                "has_toc": len(toc) > 0,
                "toc": toc[:20],
                "content_preview": doc.content[:2000] if doc.content else "",
                "metadata": meta,
                "summary": doc.summary,
                "validation_status": doc.validation_status,
            }

            if save_to_memos:
                # 直接走文档蒸馏管道 → Wiki，不走 Memos 中转
                from core.hephaestus.document_pipeline import DocumentDistillationPipeline
                from core.hephaestus.distillation_engine import HostAgentCaller
                import hashlib

                session_id = f"doc-mcp-{hashlib.md5(str(src_path).encode()).hexdigest()[:8]}"
                caller = HostAgentCaller(force_provider="api")
                pipeline = DocumentDistillationPipeline(caller=caller)

                messages = [{"role": "system", "content": doc.content}]
                meta_pipe = {
                    "source": "mcp",
                    "filename": doc.filename,
                    "file_path": str(src_path),
                    "doc_type": doc.doc_type.value,
                    "pages": page_count,
                }
                distill_result = pipeline.process(session_id, messages, meta_pipe)
                wiki_paths = pipeline.write_to_wiki(distill_result, source="mcp")

                result["wiki_paths"] = [str(p) for p in wiki_paths]
                result["pipeline"] = "文档 → Wiki 蒸馏 → 00-Inbox"
                result["session_id"] = session_id

            return result
        except Exception as e:
            logger.error(f"文档处理失败: {e}")
            return {
                "success": False,
                "message": f"处理失败: {e}",
            }

    def _tool_wiki_build(self, dry_run: bool = False) -> Dict:
        """
        触发 Wiki 构建（L1 → L2）

        扫描 Memos 中的 L1 原始记录，对已完成 session 执行：
        1. 质量评分
        2. 内容去重
        3. 知识蒸馏
        4. Wiki 页面生成
        5. 索引更新
        6. Git 自动提交

        使用场景：
        - 定期构建任务（daemon 定时触发）
        - Agent 主动请求"把最近的对话整理成 Wiki"
        """
        from core.config import get_config
        from integrations.styx import MemosClient
        from core.hephaestus.wiki_builder import run_build_cycle

        config = get_config()
        if not config.memos_enabled or not config.memos_token:
            return {
                "success": False,
                "message": "Memos 未配置，无法构建 Wiki",
            }

        try:
            client = MemosClient(
                token=config.memos_token,
                base_url=config.memos_api_url,
            )
            result = run_build_cycle(client, dry_run=dry_run)
            return {
                "success": True,
                "message": "Wiki 构建完成",
                "dry_run": dry_run,
                "result": result,
            }
        except Exception as e:
            logger.error(f"Wiki 构建失败: {e}")
            return {
                "success": False,
                "message": f"构建失败: {e}",
            }

    def _tool_persona_update(self) -> Dict:
        """
        触发用户画像更新

        采集最新信号并重新计算三层画像（能量/认知/价值）。
        使用场景：
        - 用户说"更新我的画像"
        - 定期画像刷新（daemon 每小时触发）
        """
        from core.persona.daimon import SignalCollector
        from core.persona.pythia import PreferenceAnalyzer

        try:
            # 1. 采集信号
            collector = SignalCollector()
            collect_result = collector.collect_all()

            # 2. 分析画像
            analyzer = PreferenceAnalyzer()
            profile = analyzer.analyze(days=30)

            return {
                "success": True,
                "message": "画像更新完成",
                "signals_collected": collect_result,
                "profile": {
                    "energy": profile.energy.__dict__ if profile.energy else {},
                    "cognitive": profile.cognitive.__dict__ if profile.cognitive else {},
                    "value": profile.value.__dict__ if profile.value else {},
                },
            }
        except Exception as e:
            logger.error(f"画像更新失败: {e}")
            return {
                "success": False,
                "message": f"更新失败: {e}",
            }

    def _tool_context_aware_search(self, query: str, limit: int = 10,
                                     working_dir: str = "") -> Dict:
        """
        上下文感知搜索 — 知识图谱召回 + 画像加权评分

        相比 wiki_search，增加了用户画像加权（领域偏好、形态偏好、技术栈、时间模式），
        返回更精准的排序结果。
        """
        from core.app.context_search import ContextAwareSearch

        context = {}
        if working_dir:
            context["working_dir"] = working_dir

        search = ContextAwareSearch()
        results = search.search(query, context=context, limit=limit)
        # 记录训练样本：用户执行上下文搜索 → 正样本（kg + profile 维度）
        try:
            from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
            for dim in ("kg", "profile"):
                AdaptiveScorerV2.enqueue_training_sample(
                    session_id=f"ctx-search-{hash(query) & 0xFFFFFFFF}",
                    dimension=dim,
                    features={"query_length": len(query), "result_count": len(results), "has_working_dir": bool(working_dir), "tool": "context_aware_search"},
                    expected_score=0.7 if results else 0.4,
                    source="context_aware_search",
                )
        except Exception:
            pass

        return {
            "success": True,
            "query": query,
            "results": [
                {
                    "page_path": r.page_path,
                    "title": r.title,
                    "snippet": r.snippet,
                    "score": round(r.score, 3),
                }
                for r in results
            ],
            "count": len(results),
        }

    def _tool_intent_route(self, user_input: str) -> Dict:
        """
        意图路由 — 规则匹配（不调 LLM），4 种意图分类

        返回意图类型和数据源建议：
        - recall: 回忆上下文 → 查 Memos
        - knowledge: 知识查询 → 查 Wiki
        - task: 任务执行 → 直接执行
        - chat: 闲聊/其他 → 直接回复
        """
        from core.app.intent_router import IntentRouter

        router = IntentRouter()
        decision = router.route(user_input)

        return {
            "success": True,
            "intent": decision.intent,
            "confidence": round(decision.confidence, 2),
            "data_source": decision.data_source,
            "matched_keywords": decision.matched_keywords,
            "needs_correction": decision.needs_correction,
        }

    def _tool_blindspot_check(self, query: str) -> Dict:
        """
        盲点检查 — 搜索时检测知识空白

        当用户搜索某个主题但知识库中缺少相关记录时，返回盲点提醒。
        24 小时冷却期，每天最多 1 条即时提醒。
        """
        from core.app.blindspot_discovery import BlindspotDiscovery

        bd = BlindspotDiscovery()
        result = bd.check_blind_spot(query)

        base = {
            "success": True,
            "degraded": result.degraded,
        }
        if result.degraded:
            base["degraded_reasons"] = result.degraded_reasons

        if not result.reminder:
            base.update({
                "blindspot_found": False,
                "message": "未发现盲点",
            })
            return base

        base.update({
            "blindspot_found": True,
            "topic": result.reminder.topic,
            "description": result.reminder.description,
            "confidence": round(result.reminder.confidence, 2),
            "status": result.reminder.status,
        })
        return base

    def _tool_predictive_push(self, user_input: str,
                               working_dir: str = "") -> Dict:
        """
        预测性知识推送 — 两层信号检测

        Layer 1: 正则关键词检测（<1ms）
        Layer 2: LLM 确认（仅中低置信度，~500ms）
        冷启动：COLD 不推送，WARM 每天1条标注beta
        """
        from core.app.predictive_push import PredictivePush

        push = PredictivePush()
        context = {"working_dir": working_dir} if working_dir else {}
        decisions = push.detect_and_decide(user_input, context=context)

        if not decisions:
            return {
                "success": True,
                "push_available": False,
                "message": "无推送信号",
            }

        return {
            "success": True,
            "push_available": True,
            "pushes": [
                {
                    "title": d.title,
                    "page_path": d.page_path,
                    "reason": d.reason,
                    "confidence": round(d.confidence, 2),
                }
                for d in decisions
            ],
            "count": len(decisions),
        }

    def _tool_freshness_check(self, entity_name: str) -> Dict:
        """
        知识新鲜度检查 — 版本绑定 + 上下文过期

        检查特定实体的知识是否过时：
        - 版本绑定知识：与最新版本对比
        - 上下文知识：90 天未更新则标记过期
        - 罕见访问：60 天未被查询

        搜索附加型：只在搜索时展示，不主动弹出。
        """
        from core.app.freshness_alert import FreshnessAlertChecker

        checker = FreshnessAlertChecker()
        result = checker.check_knowledge_freshness(entity_name)

        if not result:
            return {
                "success": True,
                "status": "fresh",
                "fresh": True,
                "message": f"「{entity_name}」知识新鲜",
            }

        # not_found / error 时 fresh=False，明确告知用户原因
        if result.status in ("not_found", "error"):
            return {
                "success": True,
                "status": result.status,
                "fresh": False,
                "message": result.message,
            }

        if result.status == "fresh":
            return {
                "success": True,
                "status": "fresh",
                "fresh": True,
                "message": result.message,
            }

        # stale
        return {
            "success": True,
            "status": "stale",
            "fresh": False,
            "entity_name": result.entity_name,
            "alert_type": result.alert_type,
            "message": result.message,
            "confidence": round(result.confidence, 2),
            "current_version": result.current_version,
            "latest_version": result.latest_version,
        }

    def _tool_health_check(self) -> Dict:
        """
        系统健康检查

        检查 Mnemos 各组件状态：
        - 配置完整性
        - Memos API 连通性
        - Wiki 目录可写性
        - 各模块可导入性
        - 最近构建/蒸馏状态
        """
        from core.config import get_config
        from pathlib import Path

        config = get_config()
        checks = {}
        healthy = True

        # 1. 配置检查
        checks["config_loaded"] = True
        checks["memos_configured"] = bool(config.memos_enabled and config.memos_token)
        checks["wiki_dir"] = str(config.wiki_dir)
        checks["wiki_dir_exists"] = config.wiki_dir.exists()
        checks["wiki_dir_writable"] = (
            config.wiki_dir.exists() and os.access(config.wiki_dir, os.W_OK)
        )

        # 2. Memos 连通性
        if checks["memos_configured"]:
            try:
                from integrations.styx import MemosClient
                client = MemosClient(
                    token=config.memos_token,
                    base_url=config.memos_api_url,
                )
                # 使用 list_all_memos 做健康探测（兼容 REST 和 Connect API）
                client.list_all_memos(max_records=1)
                checks["memos_reachable"] = True
            except Exception as e:
                checks["memos_reachable"] = False
                checks["memos_error"] = str(e)
                healthy = False
        else:
            checks["memos_reachable"] = False

        # 3. 模块可导入性
        modules = [
            "core.config",
            "core.kia.charon",
            "core.hephaestus.wiki_builder",
            "core.hephaestus.distillation_engine",
            "core.persona.pythia",
            "integrations.styx",
            "integrations.oracle",
        ]
        for mod in modules:
            try:
                __import__(mod)
                checks[f"module_{mod.replace('.', '_')}"] = True
            except Exception as e:
                checks[f"module_{mod.replace('.', '_')}"] = False
                checks[f"module_{mod.replace('.', '_')}_error"] = str(e)
                healthy = False

        # 4. 最近文件统计
        try:
            wiki_md_count = len(list(config.wiki_dir.rglob("*.md")))
            checks["wiki_pages"] = wiki_md_count
        except Exception:
            logging.getLogger(__name__).warning(f"Caught unexpected error at agora.py", exc_info=True)
            checks["wiki_pages"] = 0

        return {
            "success": True,
            "healthy": healthy,
            "checks": checks,
        }

    def _tool_self_diagnose(self) -> Dict:
        """Mnemos 自诊断 — 返回完整系统状态报告"""
        from core.diagnostics import ConnectionDiagnostics

        report = ConnectionDiagnostics.full_report()
        report["success"] = True
        return report

    def _tool_configure_memos(self, api_url: str, token: str) -> Dict:
        """配置 Memos 连接"""
        from core.config import get_config

        config = get_config()
        try:
            config._data.setdefault("memos", {})
            config._data["memos"]["enabled"] = True
            config._data["memos"]["api_url"] = api_url.rstrip("/")
            config._data["memos"]["token"] = token
            config.save()

            # 验证连通性
            from integrations.styx import MemosClient
            client = MemosClient(token=token, base_url=api_url)
            try:
                client.list_all_memos(max_records=1)
                reachable = True
            except Exception:
                reachable = False

            return {
                "success": True,
                "memos_connected": reachable,
                "api_url": api_url,
                "message": "Memos 配置已保存" + (" 且连通性验证通过" if reachable else " 但连通性验证失败"),
            }
        except Exception as e:
            logger.error(f"配置 Memos 失败: {e}")
            return {"success": False, "message": f"配置失败: {e}"}

    def _tool_configure_wiki(self, vault_path: str) -> Dict:
        """配置 Wiki/Obsidian 路径"""
        from core.config import get_config
        from pathlib import Path

        config = get_config()
        try:
            path = Path(vault_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)

            config._data.setdefault("wiki", {})
            config._data["wiki"]["vault_path"] = str(path)
            config.save()

            return {
                "success": True,
                "vault_path": str(path),
                "exists": path.exists(),
                "writable": os.access(path, os.W_OK),
                "message": f"Wiki 路径已配置: {path}",
            }
        except Exception as e:
            logger.error(f"配置 Wiki 失败: {e}")
            return {"success": False, "message": f"配置失败: {e}"}

    def _tool_detect_sources(self) -> Dict:
        """检测所有数据源状态"""
        from core.diagnostics import ConnectionDiagnostics

        agents = ConnectionDiagnostics.check_agents()
        memos = ConnectionDiagnostics.check_memos()
        wiki = ConnectionDiagnostics.check_wiki()

        sources = {
            "memos": {
                "enabled": memos.enabled,
                "configured": memos.configured,
                "reachable": memos.reachable,
            },
            "wiki": {
                "path": wiki.path,
                "exists": wiki.exists,
                "writable": wiki.writable,
            },
        }

        # Agent 数据源（带 hooks 状态）
        for agent in agents:
            sources[agent.name] = {
                "detected": agent.available,
                "path": agent.data_dir,
                "hooks_installed": agent.hooks_installed,
            }

        # 补充 PathDiscover 发现的额外 Agent（无 adapter 的）
        from core.sync_framework.registry import PathDiscover
        for name in ["aider", "gemini", "cursor", "windsurf"]:
            if name not in sources:
                data_dir = PathDiscover.find(name)
                sources[name] = {
                    "detected": data_dir is not None,
                    "path": str(data_dir) if data_dir else None,
                    "hooks_installed": False,
                }

        return {
            "success": True,
            "sources": sources,
        }

    # ---- JSON-RPC 2.0 / MCP 协议处理 ----

    def _make_jsonrpc_response(self, request_id: Any, result: Dict) -> Dict:
        """构建标准 JSON-RPC 2.0 成功响应"""
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def _make_jsonrpc_error(self, request_id: Any, code: int, message: str,
                            data: Any = None) -> Dict:
        """构建标准 JSON-RPC 2.0 错误响应"""
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        }

    def handle_request(self, request: Dict) -> Dict:
        """处理单个 JSON-RPC 请求"""
        # 验证 JSON-RPC 版本
        if request.get("jsonrpc") != "2.0":
            return self._make_jsonrpc_error(
                request.get("id"), JSONRPC_INVALID_REQUEST,
                "Invalid JSON-RPC version, expected 2.0"
            )

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "initialize":
            return self._make_jsonrpc_response(req_id, self._handle_initialize(params))

        if method == "tools/list":
            return self._make_jsonrpc_response(req_id, self._list_tools())

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_params = params.get("arguments", {})
            return self._call_tool(req_id, tool_name, tool_params)

        return self._make_jsonrpc_error(
            req_id, JSONRPC_METHOD_NOT_FOUND,
            f"Unknown method: {method}"
        )

    def _handle_initialize(self, params: Dict) -> Dict:
        """处理 initialize 握手（MCP 协议第一步）"""
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "mnemos-mcp-server",
                "version": "2.0.0",
            },
        }

    def _list_tools(self) -> Dict:
        """列出所有可用 tools（带完整 inputSchema）"""
        tools = [
            {
                "name": "wiki_search",
                "description": "搜索知识库。知识来源包括：1) 用户主动投喂（通过knowledge_ingest存入）2) Memos同步 3) Agent对话蒸馏 4) Retrospective复盘 5) Git历史。所有知识均经过语义索引、标签构建、热度评分(L0-L9)处理。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "limit": {"type": "integer", "description": "返回数量上限", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "wiki_read",
                "description": "读取指定 wiki 页面。页面内容经过完整解析器处理：语义索引提取实体/概念/技术栈、自动标签分类、热度评分L0-L9、知识图谱关联。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page_path": {"type": "string", "description": "wiki 页面相对路径"},
                    },
                    "required": ["page_path"],
                },
            },
            {
                "name": "wiki_write",
                "description": "写入 Wiki 页面。Agent 执行蒸馏或生成新知识后，将结果写入 Wiki 知识库。支持 frontmatter 元数据写入。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page_path": {"type": "string", "description": "wiki 页面相对路径（如 'concepts/my-idea.md'）"},
                        "content": {"type": "string", "description": "页面 Markdown 内容（不含 frontmatter）"},
                        "frontmatter": {"type": "object", "description": "Frontmatter 元数据（可选）", "default": {}},
                    },
                    "required": ["page_path", "content"],
                },
            },
            {
                "name": "session_search",
                "description": "搜索历史会话记录，自动合并分片内容。支持按关键词或 session_id 查找按 hash/range/segment 分片存储的完整聊天记录。当用户问'我们之前聊过什么'、'上次那个session'、'找回之前的对话'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词（支持内容关键词、session_id 片段、hash 前缀等）", "default": ""},
                        "session_id": {"type": "string", "description": "精确 session ID（可选，提供时优先按 session_id 查找）", "default": ""},
                        "limit": {"type": "integer", "description": "返回结果数量上限", "default": 10},
                    },
                    "required": [],
                },
            },
            {
                "name": "capture_turn",
                "description": "MCP 主动上报单轮对话。只做校验和入队，不直接写 Memos，返回 < 200ms。当 Agent 正在与用户对话时，每轮对话结束后调用此工具上报。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_agent": {"type": "string", "description": "Agent 来源标识（如 claude, kimi, codex）"},
                        "session_id": {"type": "string", "description": "会话唯一标识"},
                        "turn_id": {"type": "string", "description": "轮次 ID（可选）", "default": ""},
                        "turn_number": {"type": "integer", "description": "轮次序号（可选）", "default": 0},
                        "user_content": {"type": "string", "description": "用户消息内容（可选）", "default": ""},
                        "assistant_content": {"type": "string", "description": "AI 回复内容（可选）", "default": ""},
                        "timestamp": {"type": "string", "description": "时间戳 ISO 格式（可选）", "default": ""},
                        "model": {"type": "string", "description": "使用的模型名称（可选）", "default": ""},
                        "cwd": {"type": "string", "description": "当前工作目录（可选）", "default": ""},
                        "metadata": {"type": "object", "description": "额外元数据（可选）", "default": {}},
                    },
                    "required": ["source_agent", "session_id"],
                },
            },
            {
                "name": "capture_session",
                "description": "MCP 批量上报整个 session 的多轮对话。适用于一次性上报完整对话记录的场景。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_agent": {"type": "string", "description": "Agent 来源标识"},
                        "session_id": {"type": "string", "description": "会话唯一标识"},
                        "turns": {"type": "array", "items": {"type": "object"}, "description": "轮次列表 [{turn_number, user_content, assistant_content}]"},
                    },
                    "required": ["source_agent", "session_id", "turns"],
                },
            },
            {
                "name": "end_session",
                "description": "标记 session 结束。通知 Mnemos 该会话已完成，触发后续处理（如队列排空、会话完整性校验）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_agent": {"type": "string", "description": "Agent 来源标识"},
                        "session_id": {"type": "string", "description": "会话唯一标识"},
                    },
                    "required": ["source_agent", "session_id"],
                },
            },
            {
                "name": "capture_status",
                "description": "查询指定 session/turn 在捕获队列中的状态。用于检查对话是否已成功入队或处理完成。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_agent": {"type": "string", "description": "Agent 来源标识"},
                        "session_id": {"type": "string", "description": "会话唯一标识"},
                        "turn_number": {"type": "integer", "description": "轮次序号（可选，-1 表示查询整个 session）", "default": -1},
                    },
                    "required": ["source_agent", "session_id"],
                },
            },
            {
                "name": "knowledge_ingest",
                "description": "知识摄入 — 将用户主动提供的人工知识写入Memos，自动进入Wiki处理链路（Memos→00-Inbox→语义索引/标签/热度评分→知识图谱）。当用户说'记住这个'、'帮我记下'、'这很重要'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "用户提供的知识内容"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表（可选，如 ['coding', 'important']）"},
                        "source": {"type": "string", "description": "来源标记", "default": "human"},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "knowledge_import",
                "description": "知识导入 — 将用户指定的本地文件解析并存入知识库。支持 .md（保留格式）、代码文件（自动识别语言并加代码块）、文本文件等。文件写入 Wiki 00-Inbox/ 后立即触发 Charon 解析器（语义索引、实体提取、标签、热度评分 L0-L9）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件绝对路径（如 ~/notes/architecture.md 或 /home/user/project/main.py）"},
                        "title": {"type": "string", "description": "文档标题（可选，默认使用文件名）", "default": ""},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表（可选）"},
                        "trigger_parse": {"type": "boolean", "description": "是否立即触发解析", "default": True},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "knowledge_distill",
                "description": "触发知识蒸馏 — 将原始聊天记录转为结构化 Wiki 知识（问题-解决/决策记录/经验法则/反模式/方法论/洞察关联 6种形态）。Agent 完成一次有价值的对话后，应主动调用此工具将对话转为 Wiki 知识。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "会话标识（用于追溯）"},
                        "messages": {"type": "array", "items": {"type": "object"}, "description": "消息列表 [{role, content}]"},
                        "write_to_wiki": {"type": "boolean", "description": "是否直接写入 Wiki", "default": True},
                    },
                    "required": ["session_id", "messages"],
                },
            },
            {
                "name": "document_process",
                "description": "处理文档文件（PDF/PPT/Excel/Word/HTML/EBOOK）。提取结构、大纲、关键内容，可选择存入 Memos。当用户说'解析这个PDF'、'把这份PPT存到知识库'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件绝对路径"},
                        "title": {"type": "string", "description": "文档标题（可选）", "default": ""},
                        "save_to_memos": {"type": "boolean", "description": "是否保存到 Memos", "default": True},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "wiki_build",
                "description": "触发 Wiki 构建（L1→L2）。扫描 Memos 中的 L1 原始记录，对高质量、已完成 session 执行：质量评分→去重→蒸馏→Wiki页面生成→索引更新→Git提交。当用户说'整理最近的对话'、'构建Wiki'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "dry_run": {"type": "boolean", "description": "仅预览不实际写入", "default": False},
                    },
                },
            },
            {
                "name": "knowledge_source_list",
                "description": "列出知识库的来源分布统计（各来源的知识条目数）",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "preflight_inject",
                "description": "KIA闭环-任务前知识装载：根据任务类型从retrospective经验库装载历史教训和检查清单",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "任务类型，如 coding、debugging、design"},
                        "subtype": {"type": "string", "description": "子类型", "default": ""},
                        "context_text": {"type": "string", "description": "当前会话上下文，用于场景适配", "default": ""},
                    },
                    "required": ["task_type"],
                },
            },
            {
                "name": "guard_check",
                "description": "KIA闭环-执行中守护检查：检测当前对话是否触及历史经验中的风险点",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_message": {"type": "string", "description": "用户发送的消息内容"},
                        "ai_response": {"type": "string", "description": "AI 的回复内容（可选）", "default": ""},
                        "task_type": {"type": "string", "description": "任务类型，用于装载对应守护清单", "default": ""},
                        "subtype": {"type": "string", "description": "子类型", "default": ""},
                    },
                    "required": ["user_message"],
                },
            },
            {
                "name": "persona_summary",
                "description": "获取用户画像摘要（能量/认知/价值三层雷达）",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "persona_behavior_prompt",
                "description": "获取画像驱动的 AI 行为提示词",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "persona_update",
                "description": "触发用户画像更新。采集最新信号并重新计算三层画像（能量/认知/价值）。当用户说'更新我的画像'、'重新分析我的偏好'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "signal_collect",
                "description": "触发信号采集（从各数据源收集用户行为信号）",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "指定采集哪些源（如 session, git, memos），默认按配置",
                        },
                    },
                },
            },
            {
                "name": "retrospective_list",
                "description": "列出可用的 retrospective 经验",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_type": {"type": "string", "description": "按任务类型过滤", "default": None},
                        "limit": {"type": "integer", "description": "返回数量上限", "default": 10},
                    },
                },
            },
            {
                "name": "health_check",
                "description": "系统健康检查。检查 Mnemos 各组件状态：配置完整性、Memos API 连通性、Wiki 目录可写性、模块可导入性、最近文件统计。当用户说'检查系统状态'、'doctor'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "self_diagnose",
                "description": "Mnemos 自诊断 — 返回完整的系统状态报告，包括：已连接的 Agent、数据源状态、Memos/Wiki 连接状态、缺失的配置项。宿主 Agent 应在每次会话开始时调用此工具，了解当前连接状态并决定下一步操作。",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "configure_memos",
                "description": "配置 Memos 连接 — 设置 Memos API URL 和 Token。当用户说'我有 Memos'、'连上 Memos'时使用此工具。配置后会持久化到 ~/.mnemos/configs/main.json，并立即生效。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "api_url": {"type": "string", "description": "Memos API 地址，如 https://memos.example.com"},
                        "token": {"type": "string", "description": "Memos API Token"},
                    },
                    "required": ["api_url", "token"],
                },
            },
            {
                "name": "configure_wiki",
                "description": "配置 Wiki/Obsidian 路径 — 设置知识库根目录。当用户的 Obsidian Vault 路径与当前配置不一致时使用此工具。配置后会持久化到 ~/.mnemos/configs/main.json。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "vault_path": {"type": "string", "description": "Wiki 知识库根目录绝对路径，如 ~/Documents/Obsidian Vault/wiki"},
                    },
                    "required": ["vault_path"],
                },
            },
            {
                "name": "detect_sources",
                "description": "数据源状态检测 — 返回所有 Agent 数据源和外部系统的连接状态。包括：各 Agent 数据目录是否存在、hooks 是否生效、Memos 是否连通、Wiki 目录是否可写。宿主 Agent 应在启动时调用此工具自检。",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "context_aware_search",
                "description": "上下文感知搜索 — 知识图谱召回 + 画像加权评分。相比 wiki_search，增加了用户画像加权（领域偏好、形态偏好、技术栈、时间模式），返回更精准的排序结果。当需要更精准的知识检索时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索查询"},
                        "limit": {"type": "integer", "description": "最大结果数", "default": 10},
                        "working_dir": {"type": "string", "description": "当前工作目录（用于上下文感知）", "default": ""},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "intent_route",
                "description": "意图路由 — 规则匹配（不调 LLM），4 种意图分类：recall(回忆上下文→Memos)、knowledge(知识查询→Wiki)、task(任务执行→直接执行)、chat(闲聊→直接回复)。优先级：时间词>疑问词>动作词>默认。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_input": {"type": "string", "description": "用户输入文本"},
                    },
                    "required": ["user_input"],
                },
            },
            {
                "name": "blindspot_check",
                "description": "盲点检查 — 搜索时检测知识空白。当用户搜索某个主题但知识库中缺少相关记录时返回盲点提醒。24小时冷却，每天最多1条即时提醒。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索查询（检测是否为知识空白）"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "predictive_push",
                "description": "预测性知识推送 — 两层信号检测（正则关键词<1ms + LLM确认~500ms）。当检测到用户可能需要某知识时主动推送。冷启动：COLD不推送，WARM每天1条标注beta。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "user_input": {"type": "string", "description": "用户输入文本（用于信号检测）"},
                        "working_dir": {"type": "string", "description": "当前工作目录", "default": ""},
                    },
                    "required": ["user_input"],
                },
            },
            {
                "name": "freshness_check",
                "description": "知识新鲜度检查 — 检查特定实体的知识是否过时。版本绑定知识与最新版本对比，上下文知识90天未更新标记过期。搜索附加型，不主动弹出。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "entity_name": {"type": "string", "description": "实体名称"},
                    },
                    "required": ["entity_name"],
                },
            },
        ]
        return {"tools": tools}

    def _call_tool(self, req_id: Any, name: str, params: Dict) -> Dict:
        """调用指定 tool，返回 JSON-RPC 包装响应"""
        if name not in self.tools:
            return self._make_jsonrpc_error(
                req_id, JSONRPC_METHOD_NOT_FOUND,
                f"Unknown tool: {name}"
            )

        try:
            result = self.tools[name](**params)
            return self._make_jsonrpc_response(req_id, result)
        except TypeError as e:
            logger.warning(f"Tool parameter error: {e}")
            return self._make_jsonrpc_error(
                req_id, JSONRPC_INVALID_PARAMS,
                f"Invalid parameters for tool '{name}': {e}"
            )
        except Exception as e:
            logger.error(f"Tool execution error ({name}): {e}")
            return self._make_jsonrpc_error(
                req_id, MCP_TOOL_EXECUTION_ERROR,
                f"Tool '{name}' execution failed: {e}",
                data={"tool": name, "params": params}
            )

    def run(self):
        """主循环 - 从 stdin 读取 JSON-RPC，写入 stdout"""
        logger.info("MCP server started (stdio mode, JSON-RPC 2.0)")

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                request = json.loads(line)
                response = self.handle_request(request)

                print(json.dumps(response, ensure_ascii=False), flush=True)

            except json.JSONDecodeError as e:
                resp = self._make_jsonrpc_error(
                    None, JSONRPC_PARSE_ERROR, f"Parse error: {e}"
                )
                print(json.dumps(resp, ensure_ascii=False), flush=True)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                resp = self._make_jsonrpc_error(
                    None, JSONRPC_INTERNAL_ERROR, f"Internal error: {e}"
                )
                print(json.dumps(resp, ensure_ascii=False), flush=True)

        logger.info("MCP server stopped")


def run_mcp_server():
    """外部调用入口"""
    server = MCPServer()
    server.run()


if __name__ == "__main__":
    run_mcp_server()
