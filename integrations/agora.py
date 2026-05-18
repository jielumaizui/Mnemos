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
            "knowledge_ingest": self._tool_knowledge_ingest,
            "preflight_inject": self._tool_preflight_inject,
            "guard_check": self._tool_guard_check,
            "persona_summary": self._tool_persona_summary,
            "persona_behavior_prompt": self._tool_persona_behavior_prompt,
            "signal_collect": self._tool_signal_collect,
            "retrospective_list": self._tool_retrospective_list,
            "knowledge_source_list": self._tool_knowledge_source_list,
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
