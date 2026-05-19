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
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [mcp] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

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
            "session_save": self._tool_session_save,
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
        }

    # ---- Tool 实现 ----

    def _tool_wiki_search(self, query: str, limit: int = 5) -> Dict:
        """搜索知识库"""
        from core.config import get_config
        from integrations.oracle import WikiReader
        wiki_dir = get_config().wiki_dir
        reader = WikiReader(wiki_dir)
        results = reader.search(query, limit=limit)
        return {
            "success": True,
            "results": results,
            "query": query,
        }

    def _tool_wiki_read(self, page_path: str) -> Dict:
        """读取指定 wiki 页面"""
        from core.config import get_config
        from integrations.oracle import WikiReader
        wiki_dir = get_config().wiki_dir
        reader = WikiReader(wiki_dir)
        content = reader.read_page(page_path)
        return {
            "success": True,
            "content": content,
            "path": page_path,
        }

    def _tool_session_search(self, query: str, session_id: str = "",
                             limit: int = 10) -> Dict:
        """
        搜索历史会话记录，自动合并分片内容

        使用场景：
        - 用户说"我们之前聊过什么"
        - 需要查找某次 session 的完整对话
        - 查询按 hash/range/segment 分片存储的聊天记录

        特性：
        - 自动检测分段记录（segment=xxx, type=chunk）
        - 按 hash/session 标识合并所有分段为完整对话
        - 返回合并后的记忆列表
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
            logger.error(f"会话搜索失败: {e}")
            return {
                "success": False,
                "message": f"搜索失败: {e}",
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
                "message": "Memos 未配置，无法摄入知识。请在 ~/.mnemos/config.json 中配置 memos.token 和 memos.api_url",
            }

        try:
            client = MemosClient(
                host=config.memos_api_url,
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
            logger.error(f"知识摄入失败: {e}")
            return {
                "success": False,
                "message": f"摄入失败: {e}",
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
                logger.warning(f"Charon 解析触发失败: {e}")
                parse_result = {"error": str(e)}

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
                          task_type: str = "", subtype: str = "") -> Dict:
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

        if not knowledge or not knowledge.checklist:
            return {
                "success": True,
                "alert": False,
                "message": "无守护清单，跳过检查",
            }

        # 2. 初始化 Guard 并检查
        guard = InProcessGuard(knowledge)
        alert = guard.check(user_message, ai_response)

        if not alert:
            return {
                "success": True,
                "alert": False,
                "message": "无风险触发",
            }

        return {
            "success": True,
            "alert": True,
            "level": alert.level.value,
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
        profile = analyzer.analyze_full(days=30)
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
        """获取画像驱动的 AI 行为提示词"""
        from core.persona.pythia import PreferenceAnalyzer
        analyzer = PreferenceAnalyzer()
        profile = analyzer.analyze_full(days=30)
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
        return {
            "success": True,
            "behavior_prompts": prompts,
            "raw_profile": {
                "energy": profile.energy.__dict__ if profile.energy else {},
                "cognitive": profile.cognitive.__dict__ if profile.cognitive else {},
                "value": profile.value.__dict__ if profile.value else {},
            },
        }

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
        retro_dir = get_config().wiki_dir / "retrospectives"
        if not retro_dir.exists():
            return {"success": True, "retrospectives": []}

        items = []
        for md_file in sorted(retro_dir.rglob("*.md"), reverse=True):
            try:
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
        for md_file in wiki_dir.rglob("*.md"):
            try:
                content = md_file.read_text(encoding="utf-8", errors="ignore")
                src = "human_written"
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        try:
                            fm = yaml.safe_load(parts[1]) or {}
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
                            # 也可根据 frontmatter 中的 source 字段
                            explicit = fm.get("source", "")
                            if explicit == "memos":
                                src = "memos_sync"
                            elif explicit == "distill":
                                src = "distilled"
                            elif explicit == "retrospective":
                                src = "retrospective"
                            elif explicit == "git":
                                src = "git_knowledge"
                        except Exception:
                            pass
                sources[src] = sources.get(src, 0) + 1
            except Exception:
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
                           tags: List[str] = None) -> Dict:
        """
        保存完整聊天记录到 Memos（L1 原始池）

        使用场景：
        - Agent 会话结束时，将本轮对话完整保存
        - 支持按 hash/range/segment 分片存储
        - 自动添加 _meta 完整性校验
        """
        from core.config import get_config
        from integrations.styx import MemosClient

        config = get_config()
        if not config.memos_enabled or not config.memos_token:
            return {
                "success": False,
                "message": "Memos 未配置，无法保存会话",
            }

        try:
            client = MemosClient(
                token=config.memos_token,
                base_url=config.memos_api_url,
            )

            save_tags = list(tags or [])
            if "source=agent" not in save_tags:
                save_tags.append("source=agent")
            if "level=L1" not in save_tags:
                save_tags.append("level=L1")

            memories = client.save_session_full(
                session_id=session_id,
                messages=messages,
                tags=save_tags,
                visibility="PUBLIC",
            )

            return {
                "success": True,
                "message": f"已保存 {len(memories)} 条分片记录",
                "session_id": session_id,
                "chunks": len(memories),
                "memo_ids": [m.id for m in memories],
            }
        except Exception as e:
            logger.error(f"会话保存失败: {e}")
            return {
                "success": False,
                "message": f"保存失败: {e}",
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

            result = {
                "success": True,
                "title": doc.title,
                "doc_type": doc.doc_type.value if hasattr(doc.doc_type, "value") else str(doc.doc_type),
                "pages": doc.pages,
                "word_count": len(doc.content.split()) if doc.content else 0,
                "has_toc": doc.toc is not None and len(doc.toc) > 0,
                "toc": doc.toc[:20] if doc.toc else [],
                "content_preview": doc.content[:2000] if doc.content else "",
            }

            if save_to_memos:
                memo_id = processor.save_to_memos(doc)
                result["memo_id"] = memo_id
                result["pipeline"] = "文档 → Memos → Wiki 00-Inbox → Charon 解析"

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
            profile = analyzer.analyze_full(days=30)

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
                # 简单 ping：获取用户自身信息
                resp = client.session.get(
                    f"{client.base_url}/api/v1/auth/status",
                    headers=client.headers,
                    timeout=5,
                )
                checks["memos_reachable"] = resp.status_code == 200
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
            checks["wiki_pages"] = 0

        return {
            "success": True,
            "healthy": healthy,
            "checks": checks,
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
                "version": "0.1.0",
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
                "name": "session_save",
                "description": "保存完整聊天记录到 Memos（L1 原始池）。按 hash/range/segment 分片存储，带 _meta 完整性校验。当 Agent 会话结束且对话有价值时使用此工具保存。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "会话唯一标识"},
                        "messages": {"type": "array", "items": {"type": "object"}, "description": "消息列表 [{role, content, timestamp}]"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "额外标签（可选）", "default": []},
                    },
                    "required": ["session_id", "messages"],
                },
            },
            {
                "name": "knowledge_ingest",
                "description": "知识摄入 — 将用户主动提供的人工知识写入Memos，自动进入Wiki处理链路（Memos→00-Inbox→语义索引/标签/热度评分→知识图谱）。当用户说'记住这个'、'帮我记下'、'这很重要'时使用此工具。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "用户提供的知识内容"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表（可选，如 ['coding', 'important']）", "default": []},
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
                        "file_path": {"type": "string", "description": "文件绝对路径（如 ~/notes/architecture.md 或 /Users/zhuwei/project/main.py）"},
                        "title": {"type": "string", "description": "文档标题（可选，默认使用文件名）", "default": ""},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签列表（可选）", "default": []},
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
                        "task_type": {"type": "string", "description": "按任务类型过滤"},
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
