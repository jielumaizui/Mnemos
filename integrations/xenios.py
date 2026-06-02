# Xenios — 客主之谊 — AI 上下文读取，跨框架的礼貌访问
# 原模块: ai_context_reader.py

#!/usr/bin/env python3
"""
AI Context Reader - AI上下文读取模块

权限规则：
1. 同框架（同agent）：默认可随意读取
2. 跨框架：需用户主动授权
3. 私有标签（scope=private）：
   - 其他AI框架：不可读
   - 同框架：仅当前实例可读（需验证session/instance标识）

标签体系沿用现有，不做修改。
"""

import logging


import os
import sys
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass

from integrations.styx import MemosClient, Memory


logger = logging.getLogger(__name__)
@dataclass
class ReadPolicy:
    """读取策略"""
    allow_same_agent: bool = True      # 同agent默认可读
    allow_cross_agent: bool = False  # 跨agent需授权
    allow_private_same_agent: bool = True   # 同agent私有可读（当前实例）
    allow_private_cross_agent: bool = False  # 跨agent私有不可读


class AIContextReader:
    """
    AI上下文读取器

    读取规则：
    1. scope=public + 同agent: ✅ 直接读取
    2. scope=public + 跨agent: ❌ 需用户授权（默认拒绝）
    3. scope=private + 同agent: ✅ 仅当前session/instance可读
    4. scope=private + 跨agent: ❌ 绝对禁止
    """

    def __init__(self, agent: str = "claude", instance_id: str = None,
                 use_local_search: bool = True):
        """
        Args:
            agent: 当前AI框架标识（claude/hermes/openclaw）
            instance_id: 当前实例标识（如session_id或进程ID）
            use_local_search: 是否使用本地 FTS5 搜索（替代 Memos API 直接搜索）
        """
        self.agent = agent
        self.instance_id = instance_id or self._generate_instance_id()
        token = os.getenv("MEMOS_TOKEN")
        if not token:
            raise ValueError("MEMOS_TOKEN 环境变量未设置")
        self.client = MemosClient(token=token, agent=agent)

        # 权限状态
        self._cross_agent_authorized = False
        self._authorized_agents = set()

    def _generate_instance_id(self) -> str:
        """生成实例标识"""
        import hashlib
        import uuid
        return hashlib.md5(f"{self.agent}:{uuid.uuid4()}".encode()).hexdigest()[:12]

    def authorize_cross_agent(self, agents: List[str] = None, duration_minutes: int = 30):
        """
        授权跨agent读取

        Args:
            agents: 特定agent列表，None表示授权所有
            duration_minutes: 授权有效期（分钟）
        """
        self._cross_agent_authorized = True
        if agents:
            self._authorized_agents.update(agents)

        # 记录授权时间，用于过期检查
        self._auth_expire_at = datetime.now() + timedelta(minutes=duration_minutes)
        print(f"[ContextReader] 已授权跨agent读取，有效期{duration_minutes}分钟")

    def revoke_cross_agent_auth(self):
        """撤销跨agent授权"""
        self._cross_agent_authorized = False
        self._authorized_agents.clear()
        print("[ContextReader] 已撤销跨agent授权")

    def _is_cross_agent_authorized(self, target_agent: str) -> bool:
        """检查是否授权读取目标agent"""
        # 检查是否过期
        if hasattr(self, '_auth_expire_at') and datetime.now() > self._auth_expire_at:
            self.revoke_cross_agent_auth()
            return False

        if not self._cross_agent_authorized:
            return False

        # 如果指定了特定agent列表，检查是否在列表中
        if self._authorized_agents and target_agent not in self._authorized_agents:
            return False

        return True

    def can_read(self, memory: Memory) -> Tuple[bool, str]:
        """
        判断是否可以读取某条记忆

        Returns:
            (是否可读, 原因)
        """
        # 提取标签信息
        tags = memory.tags if hasattr(memory, 'tags') else []
        tag_dict = self._parse_tags(tags)

        source_agent = tag_dict.get('source', '').lower()
        scope = tag_dict.get('scope', 'public').lower()

        # 确定记忆属于哪个agent
        memory_agent = source_agent or self._extract_agent_from_tags(tags)

        # 判断是否同agent
        is_same_agent = memory_agent == self.agent

        # 规则判断
        if scope == 'private':
            # 私有记录
            if not is_same_agent:
                # 跨agent + 私有 = 绝对禁止
                return False, f"私有记录，{memory_agent}的内容不可被{self.agent}读取"

            # 同agent + 私有：检查是否当前实例
            # 从标签中提取instance/session标识
            memory_instance = self._extract_instance_id(tags)
            if memory_instance and memory_instance != self.instance_id:
                # 同agent但不同实例
                return False, f"私有记录仅限实例{memory_instance}读取"

            # 同agent + 当前实例（或未标记实例）
            return True, "同agent私有记录，当前实例可读"

        else:
            # 公开记录
            if is_same_agent:
                return True, "同agent公开记录"
            else:
                # 跨agent，检查授权
                if self._is_cross_agent_authorized(memory_agent):
                    return True, f"跨agent已授权({memory_agent})"
                else:
                    return False, f"跨agent({memory_agent})需授权，使用authorize_cross_agent()授权"

    def _parse_tags(self, tags: List[str]) -> Dict[str, str]:
        """解析标签为字典"""
        result = {}
        for tag in tags:
            if '=' in tag:
                key, value = tag.split('=', 1)
                result[key] = value
            elif ':' in tag:
                key, value = tag.split(':', 1)
                result[key] = value
        return result

    def _extract_agent_from_tags(self, tags: List[str]) -> str:
        """从标签中提取agent标识"""
        for tag in tags:
            # 匹配 agent:xxx 或 source=xxx
            if tag.startswith('agent:'):
                return tag.split(':', 1)[1].lower()
            if tag.startswith('source='):
                return tag.split('=', 1)[1].lower()
            # 匹配 xxx-private 格式
            if '-private' in tag:
                return tag.replace('-private', '').lower()
        return 'unknown'

    def _extract_instance_id(self, tags: List[str]) -> Optional[str]:
        """从标签中提取实例标识"""
        for tag in tags:
            # 匹配 instance:xxx 或 session:xxx
            if tag.startswith('instance:'):
                return tag.split(':', 1)[1]
            if tag.startswith('session:'):
                return tag.split(':', 1)[1]
        return None

    def read_my_context(self, limit: int = 50, days: int = 7) -> List[Memory]:
        """
        读取同agent的上下文

        自动过滤：
        - 只读取scope=public或当前实例的scope=private
        - 按时间倒序
        """
        # 搜索当前agent的记录
        memories = self.client.list_by_tags([f"source={self.agent}"], limit=limit * 2)

        # 过滤和排序
        results = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        for mem in memories:
            # 检查时间
            try:
                mem_time = datetime.fromisoformat(mem.created_at.replace('Z', '+00:00'))
                if mem_time < cutoff:
                    continue
            except Exception as e:
                logger.warning(f"日期解析失败: {e}")

            # 检查权限
            can_read, reason = self.can_read(mem)
            if can_read:
                results.append(mem)

        return results[:limit]

    def read_cross_agent(self, target_agents: List[str], limit: int = 20) -> Dict[str, List[Memory]]:
        """
        读取跨agent内容（需授权）

        Args:
            target_agents: 目标agent列表
            limit: 每个agent读取数量

        Returns:
            {agent: memories}
        """
        results = {}

        for target in target_agents:
            if not self._is_cross_agent_authorized(target):
                print(f"[ContextReader] ⚠️ 未授权读取 {target}，跳过")
                continue

            # 读取目标agent的公开记录
            memories = self.client.list_by_tags([f"source={target}", "scope=public"], limit=limit)

            # 再过滤一次权限（确保没有私有记录）
            filtered = []
            for mem in memories:
                can_read, _ = self.can_read(mem)
                if can_read:
                    filtered.append(mem)

            results[target] = filtered

        return results

    def search_context(self, query: str, limit: int = 20,
                       include_cross_agent: bool = False,
                       max_tokens: int = 1500) -> List[Dict]:
        """
        搜索上下文（直接调用 Memos API）

        Args:
            query: 搜索关键词
            limit: 结果数量
            include_cross_agent: 是否包含跨agent结果（需已授权）
            max_tokens: Memos 层最大 token 预算（默认 1500）
        """
        results = []

        # 直接调用 Memos API（含分片重组）
        try:
            merged = self.client.search_and_merge_segments(query, limit=limit)
            for mem in merged:
                can_read, reason = self.can_read(mem)
                if can_read:
                    results.append({
                        "memory": mem,
                        "source": "self",
                        "reason": reason
                    })
        except Exception as e:
            print(f"[ContextReader] API 搜索失败: {e}")

        # 2. 如授权，搜索其他agent
        if include_cross_agent and self._cross_agent_authorized:
            # 跨 agent 搜索保持 API 调用（其他 agent 的记录不在本地索引）
            for target in self._authorized_agents:
                try:
                    merged = self.client.search_and_merge_segments(
                        f"source={target} {query}", limit=limit
                    )
                    for mem in merged:
                        can_read, reason = self.can_read(mem)
                        if can_read:
                            results.append({
                                "memory": mem,
                                "source": target,
                                "reason": reason
                            })
                except Exception as e:
                    logger.warning(f"can_read 检查失败: {e}")

        return results[:limit]

    def _truncate_by_time(self, items: List[Dict],
                          max_tokens: int) -> List[Dict]:
        """
        按时间分层截断搜索结果

        分层策略：
        - 24h 内: 保留完整 content_preview（~200字）
        - 7d 内: 截断到 150 字
        - 30d 内: 截断到 80 字
        - 更早: 截断到 40 字 + 标签信息

        同时受 max_tokens 总预算限制。
        """
        now = datetime.now()
        token_used = 0
        results = []

        for item in items:
            # 解析创建时间
            created_at = item.get("created_at", "")
            try:
                if created_at:
                    if created_at.endswith("Z"):
                        created_at = created_at[:-1] + "+00:00"
                    created = datetime.fromisoformat(created_at)
                    if created.tzinfo:
                        now = now.astimezone(created.tzinfo)
                    days_old = (now - created).days
                else:
                    days_old = 999
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at xenios.py", exc_info=True)
                days_old = 999

            # 按时间分层截断
            content = item.get("content_preview", "")
            if days_old <= 1:
                # 24h 内：完整保留
                truncated = content
            elif days_old <= 7:
                truncated = content[:150] + "..." if len(content) > 150 else content
            elif days_old <= 30:
                truncated = content[:80] + "..." if len(content) > 80 else content
            else:
                # 更早：只保留摘要 + 标签
                truncated = content[:40] + "..." if len(content) > 40 else content

            # Token 估算（粗略：中文字 ≈ 1 token，英文 ≈ 0.25 token）
            token_cost = len(truncated) // 2
            if token_used + token_cost > max_tokens:
                break
            token_used += token_cost

            results.append({
                "memos_id": item.get("memos_id", ""),
                "title": item.get("title", ""),
                "content_preview": truncated,
                "created_at": item.get("created_at", ""),
                "source": item.get("source", "")
            })

        return results

    def format_context(self, memories: List[Dict]) -> str:
        """
        格式化上下文为字符串（支持本地搜索结果和 Memory 对象）

        【自引用防护 L3】输出自动包裹 wiki-ref 标记，防止循环污染

        Args:
            memories: Dict 列表（本地搜索结果）或 Memory 对象列表
        """
        lines = ["<!-- wiki-ref: do-not-ingest -->"]

        for mem in memories:
            if isinstance(mem, dict):
                # 本地搜索结果
                title = mem.get("title", mem.get("memos_id", "unknown"))[:40]
                content = mem.get("content_preview", "")
                lines.append(f"---")
                lines.append(f"[{title}]")
                lines.append(f"{content}")
                lines.append("")
            else:
                # Memory 对象（API 返回）
                tags_str = ', '.join(getattr(mem, 'tags', [])[:5])
                content = getattr(mem, 'content', '')[:500]
                lines.append(f"---")
                lines.append(f"[{tags_str}]")
                lines.append(f"{content}...")
                lines.append("")

        lines.append("<!-- /wiki-ref: do-not-ingest -->")
        return '\n'.join(lines)


def main():
    """CLI入口"""
    import argparse

    parser = argparse.ArgumentParser(description="AI Context Reader")
    parser.add_argument("--agent", default="claude", help="当前agent标识")
    parser.add_argument("--read-my", action="store_true", help="读取自己的上下文")
    parser.add_argument("--read-cross", nargs="+", help="读取跨agent内容（需授权）")
    parser.add_argument("--authorize", nargs="+", help="授权跨agent读取")
    parser.add_argument("--search", help="搜索关键词")

    args = parser.parse_args()

    reader = AIContextReader(agent=args.agent)

    if args.authorize:
        reader.authorize_cross_agent(args.authorize)

    if args.read_my:
        memories = reader.read_my_context()
        print(f"找到 {len(memories)} 条记录：")
        print(reader.format_context(memories))

    if args.read_cross:
        if not reader._cross_agent_authorized:
            print("错误：未授权跨agent读取，先使用 --authorize")
            return
        results = reader.read_cross_agent(args.read_cross)
        for agent, memories in results.items():
            print(f"\n=== {agent} ({len(memories)}条) ===")
            print(reader.format_context(memories))

    if args.search:
        results = reader.search_context(args.search, include_cross_agent=True)
        print(f"找到 {len(results)} 条结果：")
        for r in results:
            print(f"\n[{r['source']}] {r['reason']}")
            print(r['memory'].content[:300])


if __name__ == "__main__":
    main()
