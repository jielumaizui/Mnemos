#!/usr/bin/env python3
"""
Memos MCP Server - 项目级三层共享架构
项目隔离 -> 框架共享 -> 全局共享

使用 JSON-RPC 2.0 over stdio 协议(与 agora.py 一致)
"""

from __future__ import annotations

import json
import os
import sys
import time
import random
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

# 相对项目根目录的路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.styx import MemosClient
from core.config import get_config

# 配置日志到 stderr，避免污染 stdout(MCP 协议通道)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [mcp-sdk] %(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# JSON-RPC 2.0 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_TOOL_EXECUTION_ERROR = -32000

_clients: dict[str, MemosClient] = {}
_PID = os.getpid()

# 框架标识
_FRAMEWORK = os.getenv("MEMOS_AGENT", "unknown")
# 项目标识(从工作目录或环境变量)
_PROJECT = os.getenv("MEMOS_PROJECT") or os.path.basename(os.getcwd()) or "default"
# 会话标识(可选，用于更细粒度)
_SESSION = os.getenv("MEMOS_SESSION", f"{_FRAMEWORK}-{_PID}")


def _retry_with_backoff(func, max_retries=3, base_delay=0.1):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.1)
            time.sleep(delay)


def get_client() -> MemosClient:
    """获取客户端(进程隔离)"""
    key = f"{_PID}:{_FRAMEWORK}:{_PROJECT}"
    if key not in _clients:
        config = get_config()
        token = config.memos_token or os.getenv("MEMOS_TOKEN")
        if not token:
            raise ValueError("MEMOS_TOKEN 环境变量未设置")
        base_url = config.memos_api_url or os.getenv("MEMOS_API_URL", "http://localhost:5230")
        _clients[key] = MemosClient(token=token, base_url=base_url, agent=_FRAMEWORK)
    return _clients[key]


def _auto_tags(scope: str, user_tags: list) -> list:
    """自动生成标签

    scope:
        - project: 项目级隔离 (project:xxx)
        - framework: 框架级共享 ({framework}-shared)
        - global: 全局共享 (shared)
    """
    auto_tags = []

    if scope == "project":
        # 项目级：当前项目可见
        auto_tags.append(f"project:{_PROJECT}")
        auto_tags.append(f"{_FRAMEWORK}-project")  # 便于框架内查询所有项目
    elif scope == "framework":
        # 框架级：同一 AI 的所有项目可见
        auto_tags.append(f"{_FRAMEWORK}-shared")
    elif scope == "global":
        # 全局：所有 AI 可见
        auto_tags.append("shared")

    # 用户自定义标签
    if user_tags:
        auto_tags.extend(user_tags)

    return list(set(auto_tags))


class MemosMCPServerSDK:
    """Memos MCP 服务器 - 三层架构版本"""

    def __init__(self):
        self.tools = self._register_tools()

    def _register_tools(self) -> Dict[str, Any]:
        """注册可用 tools"""
        return {
            "memos_write_project": self._tool_memos_write_project,
            "memos_write_framework": self._tool_memos_write_framework,
            "memos_write_global": self._tool_memos_write_global,
            "memos_read_project": self._tool_memos_read_project,
            "memos_read_framework": self._tool_memos_read_framework,
            "memos_read_global": self._tool_memos_read_global,
            "memos_read_all_projects": self._tool_memos_read_all_projects,
            "memos_search": self._tool_memos_search,
            "memos_info": self._tool_memos_info,
        }

    # ---- Tool 实现 ----

    def _tool_memos_write_project(self, content: str, tags: List[str] = None) -> Dict:
        """写入项目级记忆"""
        client = get_client()
        auto_tags = _auto_tags("project", tags or [])

        def do_save():
            return client.save(content=content, tags=auto_tags)

        memory = _retry_with_backoff(do_save)
        return {
            "success": True,
            "message": f"[OK] 项目记忆已保存\n项目: {_PROJECT}\nID: {memory.uid}\n标签: {', '.join(memory.tags)}"
        }

    def _tool_memos_write_framework(self, content: str, tags: List[str] = None) -> Dict:
        """写入框架级记忆"""
        client = get_client()
        auto_tags = _auto_tags("framework", tags or [])

        def do_save():
            return client.save(content=content, tags=auto_tags)

        memory = _retry_with_backoff(do_save)
        return {
            "success": True,
            "message": f"[OK] 框架记忆已保存\n框架: {_FRAMEWORK}\nID: {memory.uid}\n标签: {', '.join(memory.tags)}"
        }

    def _tool_memos_write_global(self, content: str, tags: List[str] = None) -> Dict:
        """写入全局记忆"""
        client = get_client()
        auto_tags = _auto_tags("global", tags or [])

        def do_save():
            return client.save(content=content, tags=auto_tags)

        memory = _retry_with_backoff(do_save)
        return {
            "success": True,
            "message": f"[OK] 全局记忆已保存\nID: {memory.uid}\n标签: {', '.join(memory.tags)}"
        }

    def _tool_memos_read_project(self, limit: int = 20) -> Dict:
        """读取当前项目记忆"""
        client = get_client()
        tag = f"project:{_PROJECT}"

        def do_read():
            return client.list_by_tags([tag], limit=limit)

        memories = _retry_with_backoff(do_read)

        if not memories:
            return {"success": True, "message": f"暂无项目 '{_PROJECT}' 的记忆"}

        lines = [f"## 项目记忆: {_PROJECT}", ""]
        for m in memories:
            lines.append(f"- {m.content[:100]}...")
        return {"success": True, "message": "\n".join(lines)}

    def _tool_memos_read_framework(self, limit: int = 20) -> Dict:
        """读取框架记忆"""
        client = get_client()
        tag = f"{_FRAMEWORK}-shared"

        def do_read():
            return client.list_by_tags([tag], limit=limit)

        memories = _retry_with_backoff(do_read)

        if not memories:
            return {"success": True, "message": f"暂无 {_FRAMEWORK} 框架记忆"}

        lines = [f"## {_FRAMEWORK} 框架记忆", ""]
        for m in memories:
            lines.append(f"- {m.content[:100]}...")
        return {"success": True, "message": "\n".join(lines)}

    def _tool_memos_read_global(self, limit: int = 20) -> Dict:
        """读取全局共享记忆"""
        client = get_client()

        def do_read():
            return client.list_by_tags(["shared"], limit=limit)

        memories = _retry_with_backoff(do_read)

        if not memories:
            return {"success": True, "message": "暂无全局共享记忆"}

        lines = ["## 全局共享记忆", ""]
        for m in memories:
            lines.append(f"- {m.content[:100]}...")
        return {"success": True, "message": "\n".join(lines)}

    def _tool_memos_read_all_projects(self, limit: int = 50) -> Dict:
        """读取所有项目记忆"""
        client = get_client()
        tag = f"{_FRAMEWORK}-project"

        def do_read():
            return client.list_by_tags([tag], limit=limit)

        memories = _retry_with_backoff(do_read)

        if not memories:
            return {"success": True, "message": f"暂无 {_FRAMEWORK} 的项目记忆"}

        lines = [f"## {_FRAMEWORK} 所有项目记忆", ""]
        for m in memories:
            lines.append(f"- {m.content[:80]}...")
            lines.append(f"  标签: {', '.join(m.tags)}")
        return {"success": True, "message": "\n".join(lines)}

    def _tool_memos_search(self, query: str, scope: str = "all", limit: int = 20) -> Dict:
        """全文搜索记忆"""
        client = get_client()

        def do_search():
            return client.search(query, limit=limit * 2)

        memories = _retry_with_backoff(do_search)

        # 根据 scope 过滤
        if scope == "project":
            memories = [m for m in memories if f"project:{_PROJECT}" in m.tags]
        elif scope == "framework":
            memories = [m for m in memories if f"{_FRAMEWORK}-shared" in m.tags]
        elif scope == "global":
            memories = [m for m in memories if "shared" in m.tags]

        if not memories:
            return {"success": True, "message": f"未找到 '{query}' 的相关记忆"}

        lines = [f"## 搜索结果: '{query}' (scope: {scope})", ""]
        for m in memories[:limit]:
            lines.append(f"- {m.content[:80]}...")
            lines.append(f"  标签: {', '.join(m.tags)}")
        return {"success": True, "message": "\n".join(lines)}

    def _tool_memos_info(self) -> Dict:
        """显示当前记忆系统状态"""
        lines = [
            "## Memos 记忆系统状态",
            "",
            f"**框架**: {_FRAMEWORK}",
            f"**当前项目**: {_PROJECT}",
            f"**会话 ID**: {_SESSION}",
            f"**PID**: {_PID}",
            "",
            "**三层架构**:",
            "1. 项目级 - 仅当前项目可见",
            "2. 框架级 - 同一 AI 的所有项目可见",
            "3. 全局级 - 所有 AI 可见",
            "",
            "**自动标签**:",
            f"- project:{_PROJECT}",
            f"- {_FRAMEWORK}-project",
            f"- {_FRAMEWORK}-shared",
            "- shared",
        ]
        return {"success": True, "message": "\n".join(lines)}

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
        """处理 initialize 握手"""
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "memos-mcp-server-sdk",
                "version": "1.0.0",
            },
        }

    def _list_tools(self) -> Dict:
        """列出所有可用 tools"""
        tools = [
            {
                "name": "memos_write_project",
                "description": f"写入项目级记忆(仅当前项目可见，项目: {_PROJECT})",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签"}
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "memos_write_framework",
                "description": f"写入框架级记忆(所有 {_FRAMEWORK} 项目可见)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签"}
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "memos_write_global",
                "description": "写入全局记忆(所有 AI 可见)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "记忆内容"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签，如 shared:偏好"}
                    },
                    "required": ["content"]
                }
            },
            {
                "name": "memos_read_project",
                "description": f"读取当前项目记忆(项目: {_PROJECT})",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 20}
                    }
                }
            },
            {
                "name": "memos_read_framework",
                "description": f"读取 {_FRAMEWORK} 框架记忆",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 20}
                    }
                }
            },
            {
                "name": "memos_read_global",
                "description": "读取全局共享记忆",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 20}
                    }
                }
            },
            {
                "name": "memos_read_all_projects",
                "description": f"读取 {_FRAMEWORK} 的所有项目记忆",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 50}
                    }
                }
            },
            {
                "name": "memos_search",
                "description": "全文搜索记忆",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "scope": {"type": "string", "enum": ["project", "framework", "global", "all"], "default": "all", "description": "搜索范围"},
                        "limit": {"type": "integer", "default": 20}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "memos_info",
                "description": "显示当前记忆系统状态",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            }
        ]
        return {"tools": tools}

    def _call_tool(self, req_id: Any, name: str, params: Dict) -> Dict:
        """调用指定 tool"""
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
        logger.info("Memos MCP Server SDK started (stdio mode, JSON-RPC 2.0)")

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

        logger.info("Memos MCP Server SDK stopped")


def run_mcp_server_sdk():
    """外部调用入口"""
    server = MemosMCPServerSDK()
    server.run()


if __name__ == "__main__":
    run_mcp_server_sdk()
