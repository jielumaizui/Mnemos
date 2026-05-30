#!/usr/bin/env python3
"""
Memos MCP Server - HTTP API 版本

为 Hermes Agent、OpenClaw 提供统一的记忆管理接口。
使用 Memos HTTP API，数据可在 Web UI 查看。
"""

from __future__ import annotations

import json
import logging
import sys
import os
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# 相对项目根目录的路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from integrations.styx import MemosClient
from core.config import get_config


class MemosMCPServer:
    """Memos MCP 服务器"""

    def __init__(self):
        config = get_config()
        token = config.memos_token or os.getenv("MEMOS_TOKEN")
        if not token:
            raise ValueError("MEMOS_TOKEN 环境变量未设置")
        self.token = token
        self.base_url = config.memos_api_url or os.getenv("MEMOS_API_URL", "http://localhost:5230")
        self.clients: Dict[str, MemosClient] = {}

    def _get_client(self, agent: str) -> MemosClient:
        """获取或创建指定 agent 的客户端"""
        if agent not in self.clients:
            self.clients[agent] = MemosClient(
                token=self.token,
                base_url=self.base_url,
                agent=agent
            )
        return self.clients[agent]

    def run(self):
        """运行 MCP 服务器(从 stdin 读取，stdout 输出)"""
        while True:
            try:
                line = input()
                if not line:
                    continue

                request = json.loads(line)
                response = self._handle_request(request)

                # MCP 协议不需要 request_id，直接输出结果
                if "id" in request:
                    response["id"] = request["id"]

                print(json.dumps(response, ensure_ascii=False))
                sys.stdout.flush()

            except EOFError:
                break
            except json.JSONDecodeError as e:
                print(json.dumps({"error": f"Invalid JSON: {e}"}))
                sys.stdout.flush()
            except Exception as e:
                logger.error(f"MCP 内部错误: {e}", exc_info=True)
                print(json.dumps({"error": "Internal server error"}), flush=True)

    def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """处理 MCP 请求"""
        method = request.get("method")
        params = request.get("params", {})

        if method == "initialize":
            return self._initialize(params)
        elif method == "tools/list":
            return self._list_tools(params)
        elif method == "tools/call":
            return self._call_tool(params)
        else:
            return {"error": f"Unknown method: {method}"}

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """初始化连接"""
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {}
            },
            "serverInfo": {
                "name": "memos-mcp-server",
                "version": "1.0.0"
            }
        }

    def _list_tools(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """列出可用工具"""
        tools = [
            {
                "name": "memos_write",
                "description": "写入记忆到 Memos(自动分类标签，Web 可见)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "记忆内容"
                        },
                        "agent": {
                            "type": "string",
                            "description": "AI 标识 (hermes/openclaw/claude)"
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选标签，如 shared、project:x"
                        }
                    },
                    "required": ["content", "agent"]
                }
            },
            {
                "name": "memos_read_shared",
                "description": "读取共享记忆(所有 AI 可见的偏好、踩坑记录等)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "返回条数限制",
                            "default": 20
                        }
                    }
                }
            },
            {
                "name": "memos_read_private",
                "description": "读取当前 AI 的专属记忆",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent": {
                            "type": "string",
                            "description": "AI 标识 (hermes/openclaw/claude)"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数限制",
                            "default": 20
                        }
                    },
                    "required": ["agent"]
                }
            },
            {
                "name": "memos_search",
                "description": "搜索记忆(关键词全文搜索)",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词"
                        },
                        "agent": {
                            "type": "string",
                            "description": "AI 标识"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数限制",
                            "default": 20
                        }
                    },
                    "required": ["query", "agent"]
                }
            }
        ]

        return {"tools": tools}

    def _call_tool(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """调用具体工具"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "memos_write":
                return self._memos_write(arguments)
            elif tool_name == "memos_read_shared":
                return self._memos_read_shared(arguments)
            elif tool_name == "memos_read_private":
                return self._memos_read_private(arguments)
            elif tool_name == "memos_search":
                return self._memos_search(arguments)
            else:
                return {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}], "isError": True}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}

    def _memos_write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """写入记忆"""
        content = args.get("content")
        agent = args.get("agent", "unknown")
        tags = args.get("tags", [])

        if not content:
            return {"content": [{"type": "text", "text": "Error: content is required"}], "isError": True}

        client = self._get_client(agent)
        memory = client.save(content=content, tags=tags)

        text = f"[OK] 记忆已保存\nID: {memory.uid}\n内容: {memory.content[:100]}...\n标签: {', '.join(memory.tags)}"
        return {"content": [{"type": "text", "text": text}]}

    def _memos_read_shared(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """读取共享记忆"""
        limit = args.get("limit", 20)

        # 使用任一客户端读取共享记忆
        client = self._get_client("hermes")
        memories = client.list_by_tags(["shared"], limit=limit)

        if not memories:
            return {"content": [{"type": "text", "text": "暂无共享记忆"}]}

        lines = ["## 共享记忆(所有 AI 可见)", ""]
        for m in memories:
            lines.append(f"- {m.content[:80]}...")
            lines.append(f"  标签: {', '.join(m.tags)}")
            lines.append("")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    def _memos_read_private(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """读取专属记忆"""
        agent = args.get("agent", "unknown")
        limit = args.get("limit", 20)

        client = self._get_client(agent)
        memories = client.list_by_tags([f"{agent}-private"], limit=limit)

        if not memories:
            return {"content": [{"type": "text", "text": f"暂无 {agent} 的专属记忆"}]}

        lines = [f"## {agent} 专属记忆", ""]
        for m in memories:
            lines.append(f"- {m.content[:80]}...")
            lines.append(f"  标签: {', '.join(m.tags)}")
            lines.append("")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    def _memos_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """搜索记忆"""
        query = args.get("query")
        agent = args.get("agent", "unknown")
        limit = args.get("limit", 20)

        if not query:
            return {"content": [{"type": "text", "text": "Error: query is required"}], "isError": True}

        client = self._get_client(agent)
        memories = client.search(query, limit=limit)

        if not memories:
            return {"content": [{"type": "text", "text": f"未找到包含 '{query}' 的记忆"}]}

        lines = [f"## 搜索结果: '{query}'", ""]
        for m in memories:
            lines.append(f"- {m.content[:80]}...")
            lines.append(f"  标签: {', '.join(m.tags)}")
            lines.append("")

        return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def main():
    """主入口"""
    server = MemosMCPServer()
    server.run()


if __name__ == "__main__":
    main()
