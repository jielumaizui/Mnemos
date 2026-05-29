# Styx — 冥河 — Memos SDK，连接记忆与遗忘的边界
# 原模块: memos_sdk.py

#!/usr/bin/env python3
"""
Memos Python SDK - HTTP API 版本
支持连接池和并发优化
"""

import os
import re
import json
import time
import math
import hashlib
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import get_config

# 全局连接池（进程内共享）
import logging
logger = logging.getLogger(__name__)
_session_lock = threading.Lock()
_sessions: Dict[str, requests.Session] = {}


def get_session(base_url: str) -> requests.Session:
    """获取或创建带连接池的 Session（线程安全）"""
    # 规范化 URL 作为 key，避免尾部斜杠导致重复 session
    key = base_url.rstrip('/')
    with _session_lock:
        if key not in _sessions:
            session = requests.Session()
            # 连接池配置
            adapter = HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=Retry(
                    total=3,
                    backoff_factor=0.1,
                    status_forcelist=[500, 502, 503, 504]
                )
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            _sessions[key] = session
        return _sessions[key]


@dataclass
class Memory:
    """记忆条目数据结构"""
    id: int
    uid: str
    content: str
    tags: List[str]
    visibility: str
    created_at: str
    updated_at: str
    agent: str = ""
    raw_source_path: Optional[str] = None


# ==================== 自定义异常 ====================

class MemosRateLimitError(Exception):
    """429 速率限制错误，建议指数退避重试"""
    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class MemosAuthError(Exception):
    """401/403 认证/授权错误，不建议重试"""
    pass


class MemosPayloadTooLargeError(Exception):
    """413 请求体过大错误"""
    pass


class MemosServerError(Exception):
    """5xx 服务器错误，建议重试"""
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class MemosClient:
    """Memos HTTP API 客户端"""

    # 脱敏规则（内置默认值，可被配置文件覆盖）
    _DEFAULT_SANITIZE_PATTERNS = [
        (r'sk-[a-zA-Z0-9]{20,}', '[API-KEY]'),
        (r'gh[pousr]_[A-Za-z0-9_]{36,}', '[GITHUB-TOKEN]'),
        (r'AKID[0-9a-zA-Z]{10,}', '[CLOUD-KEY]'),
        (r'password[:=]\s*\S+', 'password=[HIDDEN]'),
        (r'secret[:=]\s*\S+', 'secret=[HIDDEN]'),
        (r'token[:=]\s*\S+', 'token=[HIDDEN]'),
    ]

    # 自动分类关键词
    SHARED_KEYWORDS = ['我的', '我习惯', '我讨厌', '偏好', '约定', '规则']

    def __init__(self, token: str = None, base_url: str = "", agent: str = None,
                 metrics_callback: Optional[Callable[[str, float, int], None]] = None):
        self.base_url = base_url.rstrip('/')
        self.token = token or os.getenv("MEMOS_TOKEN")
        # 优先从环境变量读取 agent，其次是传入的参数
        self.agent = os.getenv("MEMOS_AGENT") or agent or "unknown"
        self.headers = {"Authorization": f"Bearer {self.token}"}
        # 使用连接池
        self.session = get_session(self.base_url)

        # Config-driven 参数
        cfg = get_config()
        self.max_content_bytes = cfg.get("memos.max_content_bytes", 7792)
        self.ingest_batch_size = cfg.get("memos.ingest_batch_size", 10)
        self.ingest_batch_interval = cfg.get("memos.ingest_batch_interval", 10)

        # 查询缓存
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._cache_ttl = cfg.get("memos.query_cache_ttl", 30)

        # metrics 回调
        self._metrics_callback = metrics_callback

        # 加载脱敏规则（优先从配置文件，否则用内置默认值）
        self.SENSITIVE_PATTERNS = self._load_sanitize_patterns()

    # ==================== 配置加载 ====================

    @staticmethod
    def _load_sanitize_patterns() -> List[Tuple[str, str]]:
        """从 ~/.mnemos/configs/sanitize_patterns.json 加载脱敏规则，不存在则用内置默认值"""
        cfg_dir = Path.home() / ".mnemos" / "configs"
        patterns_file = cfg_dir / "sanitize_patterns.json"
        if patterns_file.exists():
            try:
                with open(patterns_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 期望格式: [["regex", "replacement"], ...]
                patterns = []
                for item in data:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        patterns.append((item[0], item[1]))
                if patterns:
                    return patterns
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error", exc_info=True)
                pass
        return list(MemosClient._DEFAULT_SANITIZE_PATTERNS)

    # ==================== 缓存 ====================

    def _cache_get(self, key: str) -> Optional[Any]:
        """从缓存获取数据，过期返回 None"""
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return val
            del self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any):
        """写入缓存"""
        self._cache[key] = (time.time(), value)

    def _cache_key(self, method: str, *args) -> str:
        """生成缓存 key"""
        return f"{method}:{':'.join(str(a) for a in args)}"

    # ==================== 请求封装 ====================

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        统一请求入口，包含错误分类和 metrics 回调

        Args:
            method: HTTP 方法 (GET/POST/PATCH/DELETE)
            url: 完整 URL
            **kwargs: 传递给 requests 的额外参数

        Returns:
            requests.Response

        Raises:
            MemosRateLimitError: 429 速率限制
            MemosAuthError: 401/403 认证失败
            MemosPayloadTooLargeError: 413 请求体过大
            MemosServerError: 5xx 服务器错误
        """
        start_time = time.time()
        resp = getattr(self.session, method.lower())(url, headers=self.headers, **kwargs)
        elapsed = time.time() - start_time

        # metrics 回调
        if self._metrics_callback:
            try:
                self._metrics_callback(method, elapsed, resp.status_code)
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at styx.py", exc_info=True)
                pass  # metrics 不应影响主流程

        # 错误分类
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            retry_secs = int(retry_after) if retry_after and retry_after.isdigit() else None
            raise MemosRateLimitError(
                f"速率限制 (429)，建议指数退避重试{f'，Retry-After: {retry_secs}s' if retry_secs else ''}",
                retry_after=retry_secs
            )
        elif resp.status_code in (401, 403):
            raise MemosAuthError(
                f"认证/授权失败 ({resp.status_code})，请检查 token 是否有效，不建议重试"
            )
        elif resp.status_code == 413:
            raise MemosPayloadTooLargeError(
                f"请求体过大 (413)，请减少内容长度"
            )
        elif resp.status_code >= 500:
            raise MemosServerError(
                f"服务器错误 ({resp.status_code})，建议重试",
                status_code=resp.status_code
            )

        return resp

    def _sanitize(self, content: str) -> str:
        """脱敏处理"""
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)
        return content

    def _extract_tags(self, content: str) -> Tuple[str, List[str]]:
        """从内容中提取 #标签（支持 hash:xxx、range=1-9 等格式）"""
        tags = re.findall(r'#([^#\s]+)', content)
        # 移除内容中的标签标记
        clean_content = re.sub(r'#([^#\s]+)', '', content).strip()
        return clean_content, tags

    def _auto_classify(self, content: str) -> List[str]:
        """自动分类标签"""
        tags = []
        if any(kw in content for kw in self.SHARED_KEYWORDS):
            tags.append("shared")
        tags.append(f"agent={self.agent}")
        return tags

    # ==================== 核心 API ====================

    def _truncate_content(self, content: str, max_bytes: int) -> Tuple[str, bool, int]:
        """
        按字节数截断内容（Memos API 按字节限制）
        确保不在多字节 UTF-8 字符中间截断

        返回: (截断后内容, 是否被截断, 原始字节数)
        """
        encoded = content.encode('utf-8')
        original_bytes = len(encoded)

        if original_bytes <= max_bytes:
            return content, False, original_bytes

        # 按字节截断，但确保不截断在多字节字符中间
        truncated_bytes = encoded[:max_bytes]

        # 尝试解码，如果失败则回退
        try:
            truncated_content = truncated_bytes.decode('utf-8')
        except UnicodeDecodeError:
            # 如果截断在多字节字符中间，逐步回退
            while truncated_bytes:
                try:
                    truncated_content = truncated_bytes.decode('utf-8')
                    break
                except UnicodeDecodeError:
                    truncated_bytes = truncated_bytes[:-1]
            else:
                truncated_content = ""

        return truncated_content, True, original_bytes

    def save_long_content(self, content: str, tags: List[str] = None,
                         visibility: str = "PRIVATE",
                         title: str = None,
                         chunk_tag_factory: Callable[[int, str, int], List[str]] = None) -> List[Memory]:
        """
        保存长内容（自动分片）

        当内容超过限制时，自动分成多条记录保存，并建立关联

        Args:
            content: 内容（任意长度）
            tags: 标签列表
            visibility: 可见性
            title: 内容标题（用于分片关联）
            chunk_tag_factory: 可选回调函数，接收 (idx, chunk_content, total_chunks) 返回额外标签列表

        Returns:
            List[Memory] 分片记录列表
        """
        from datetime import datetime

        if not title:
            title = f"content-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # 脱敏
        content = self._sanitize(content)
        content_bytes = len(content.encode('utf-8'))

        # 计算每片可用空间（留出空间给分片标记）
        auto_tags = self._auto_classify(content)
        if tags:
            auto_tags.extend(tags)
        auto_tags = list(set(auto_tags))

        # 计算分片标记的开销（字节）
        # 分片提示: "[N/M] «title»\n\n" ≈ 20 + len(title) 字符，按 UTF-8 最多 3 字节/字符
        # 标签: part=N/M, group=title, type=chunk-x ≈ 50 + len(title) 字符
        # save() 方法内部还会添加截断提示和额外标签
        header_overhead = (20 + len(title)) * 3  # 最坏情况：全部中文
        tag_overhead = (50 + len(title)) * 3
        total_overhead = header_overhead + tag_overhead + 600  # 增加缓冲

        # 每片内容可用空间（字节）
        available_per_chunk = self.max_content_bytes - total_overhead

        if available_per_chunk < 3000:
            available_per_chunk = 3000

        # 如果内容不需要分片，直接保存
        if content_bytes <= available_per_chunk:
            result = self.save(content, tags, visibility)
            return [result]

        # 需要分片：智能分片（按字节计算，但保持段落边界）
        chunks = self._split_content_smart_bytes(content, available_per_chunk)
        memories = []

        for idx, chunk in enumerate(chunks):
            # 添加分片标记
            chunk_tags = auto_tags.copy()
            chunk_tags.append(f"segment:{idx+1}/{len(chunks)}")
            chunk_tags.append(f"group={title}")
            chunk_tags.append("type=segmented")

            if idx == 0:
                chunk_tags.append("type=chunk-head")
            else:
                chunk_tags.append("type=chunk-body")

            # 调用工厂函数获取额外标签（range、summary、hash 等）
            if chunk_tag_factory:
                extra_tags = chunk_tag_factory(idx, chunk, len(chunks))
                chunk_tags.extend(extra_tags)

            # 组装内容（添加分片提示）
            chunk_content = f"[{idx+1}/{len(chunks)}] «{title}»\n\n{chunk}"

            result = self.save(chunk_content, chunk_tags, visibility)
            memories.append(result)

        return memories

    def _split_content_smart_bytes(self, content: str, max_chunk_bytes: int) -> List[str]:
        """智能分片（按字节计算，但尽量在段落或句子边界）"""
        content_bytes = content.encode('utf-8')
        if len(content_bytes) <= max_chunk_bytes:
            return [content]

        chunks = []
        remaining = content

        while remaining:
            remaining_bytes = remaining.encode('utf-8')
            if len(remaining_bytes) <= max_chunk_bytes:
                chunks.append(remaining)
                break

            # 估算字符位置（假设平均 2 字节/字符用于中英文混合）
            estimated_chars = max_chunk_bytes // 2
            chunk = remaining[:estimated_chars]

            # 调整以确保不超过字节限制
            while len(chunk.encode('utf-8')) > max_chunk_bytes and len(chunk) > 100:
                chunk = chunk[:-50]  # 每次减少 50 字符

            # 尝试在段落边界分片
            last_para = chunk.rfind('\n\n')
            if last_para > len(chunk) * 0.7:
                split_pos = last_para
            else:
                # 尝试在句子边界分片
                last_sentence = max(
                    chunk.rfind('. '),
                    chunk.rfind('。'),
                    chunk.rfind('？'),
                    chunk.rfind('！')
                )
                if last_sentence > len(chunk) * 0.7:
                    split_pos = last_sentence + 1
                else:
                    # 硬性截断（在空格处）
                    last_space = chunk.rfind(' ')
                    if last_space > len(chunk) * 0.8:
                        split_pos = last_space
                    else:
                        split_pos = len(chunk)

            chunks.append(remaining[:split_pos].strip())
            remaining = remaining[split_pos:].strip()

        return chunks

    def _split_content_smart(self, content: str, max_chunk_size: int) -> List[str]:
        """智能分片：尽量在段落或句子边界分片"""
        if len(content) <= max_chunk_size:
            return [content]

        chunks = []
        remaining = content

        while remaining:
            if len(remaining) <= max_chunk_size:
                chunks.append(remaining)
                break

            # 尝试在段落边界分片（双换行）
            chunk = remaining[:max_chunk_size]
            last_para = chunk.rfind('\n\n')

            if last_para > max_chunk_size * 0.7:  # 至少保留 70% 的内容
                split_pos = last_para
            else:
                # 尝试在句子边界分片（句号+空格或换行）
                last_sentence = max(
                    chunk.rfind('. '),
                    chunk.rfind('。'),
                    chunk.rfind('？'),
                    chunk.rfind('！')
                )
                if last_sentence > max_chunk_size * 0.7:
                    split_pos = last_sentence + 1
                else:
                    # 硬性截断（在空格处）
                    last_space = chunk.rfind(' ')
                    if last_space > max_chunk_size * 0.8:
                        split_pos = last_space
                    else:
                        split_pos = max_chunk_size

            chunks.append(remaining[:split_pos].strip())
            remaining = remaining[split_pos:].strip()

        return chunks

    def save(self, content: str, tags: List[str] = None, visibility: str = "PRIVATE") -> Memory:
        """保存记忆"""
        # 1. 脱敏
        content = self._sanitize(content)

        # 2. 自动标签
        auto_tags = self._auto_classify(content)
        if tags:
            auto_tags.extend(tags)
        auto_tags = list(set(auto_tags))

        # 3. 准备标签字符串（#key=value 格式，用于 Memos 识别）
        tag_str = ' '.join([f"#{t}" for t in auto_tags])
        tag_bytes = len(tag_str.encode('utf-8'))

        # 4. 计算可用空间（留出空间给标签和提示）
        # Memos API 限制 8192 字节
        available_bytes = self.max_content_bytes - tag_bytes - 200
        if available_bytes < 3000:
            available_bytes = 3000

        # 5. 按字节数截断内容
        truncated_content, was_truncated, original_bytes = self._truncate_content(
            content, available_bytes
        )

        # 6. 组装最终内容
        if was_truncated:
            truncation_notice = f"\n\n[⚠️ 内容过长已截断：{original_bytes} 字节 → {len(truncated_content.encode('utf-8'))} 字节]"
            final_content = f"{truncated_content}{truncation_notice}\n\n{tag_str}".strip()
        else:
            final_content = f"{truncated_content}\n\n{tag_str}".strip()

        # 7. 调用 API（使用统一请求入口）
        resp = self._make_request(
            "POST",
            f"{self.base_url}/api/v1/memos",
            json={
                "content": final_content,
                "visibility": visibility
            }
        )
        resp.raise_for_status()
        data = resp.json()

        # Parse name like "memos/UID" to get uid
        name = data.get("name", "")
        uid = name.replace("memos/", "") if name.startswith("memos/") else name

        # 使用 API 返回的 tags（从内容中提取的）
        result_tags = data.get("tags", auto_tags)

        # 如果被截断，在返回的 Memory 中标记
        result_content = content
        if was_truncated:
            result_content = f"{content[:100]}... [⚠️ 已截断：{original_bytes} 字符]"

        return Memory(
            id=uid,
            uid=uid,
            content=result_content,
            tags=result_tags,
            visibility=data.get("visibility", visibility),
            created_at=data.get("createTime"),
            updated_at=data.get("updateTime"),
            agent=self.agent
        )

    def idempotent_save(self, content: str, tags: List[str], visibility: str = "PUBLIC") -> Memory:
        """
        幂等保存：仅在内容不存在时保存（按 hash 去重）

        Args:
            content: 内容文本
            tags: 标签列表
            visibility: 可见性

        Returns:
            Memory 对象（如果已存在则返回标记了 id=0 的占位 Memory）
        """
        content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()
        existing = self.get_full_content_by_hash(content_hash)
        if existing:
            # Return a dummy Memory indicating skip
            return Memory(
                id=0, uid="", content=existing, tags=tags,
                visibility=visibility, created_at="", updated_at="",
                agent=self.agent, raw_source_path=None
            )
        return self.save(content=content, tags=tags, visibility=visibility)

    def update_tags(self, memo_uid: str, add_tags: List[str] = None, remove_tags: List[str] = None) -> bool:
        """
        增量更新标签：对已有 memo 添加或移除标签

        Args:
            memo_uid: 记忆 UID
            add_tags: 要添加的标签列表
            remove_tags: 要移除的标签列表

        Returns:
            是否更新成功
        """
        if not add_tags and not remove_tags:
            return True

        # 1. 获取当前 memo
        current = self.get_by_uid(memo_uid)
        if not current:
            return False

        # 2. 合并标签
        current_tags = set(current.tags)
        if add_tags:
            current_tags.update(add_tags)
        if remove_tags:
            current_tags -= set(remove_tags)

        # 3. 重新构建内容（Memos 从内容中提取标签）
        # 保留原始内容（不含标签行），追加新标签行
        clean_content = current.content.rstrip()
        # 移除旧的标签行（以 # 开头的行）
        lines = clean_content.split('\n')
        content_lines = [l for l in lines if not re.match(r'^\s*#[^\s]', l)]
        clean_content = '\n'.join(content_lines).rstrip()

        tag_line = " ".join([f"#{t}" for t in current_tags])
        new_content = f"{clean_content}\n\n{tag_line}"

        # 4. PATCH 更新
        try:
            resp = self._make_request(
                "PATCH",
                f"{self.base_url}/api/v1/memos/{memo_uid}",
                json={"content": new_content}
            )
            return resp.ok
        except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
            return False

    def list_all_memos(self, max_records: int = None, filter_fn=None) -> List[Dict]:
        """
        获取所有记忆记录（自动处理分页）

        Args:
            max_records: 最大返回记录数（None表示获取全部）
            filter_fn: 过滤函数，接收memo字典返回bool

        Returns:
            符合条件的原始memo字典列表
        """
        all_memos = []
        page_token = None
        page_size = 1000
        safety_limit = 50  # 最多50页，防止无限循环

        for page_num in range(safety_limit):
            params = {"pageSize": page_size}
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._make_request(
                    "GET",
                    f"{self.base_url}/api/v1/memos",
                    params=params
                )
            except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
                break

            if not resp.ok:
                break

            data = resp.json()
            memos = data.get("memos", [])

            if filter_fn:
                filtered = [m for m in memos if filter_fn(m)]
                all_memos.extend(filtered)
            else:
                all_memos.extend(memos)

            # 检查是否达到限制
            if max_records and len(all_memos) >= max_records:
                all_memos = all_memos[:max_records]
                break

            # 检查是否有下一页
            page_token = data.get("nextPageToken")
            if not page_token or len(memos) == 0:
                break

        return all_memos

    def list_by_tags(self, tags: List[str], limit: int = None) -> List[Memory]:
        """
        按标签查询记忆（支持分页获取全部）

        Args:
            tags: 标签列表（匹配任一标签）
            limit: 最大返回数量（None表示获取全部）

        Returns:
            匹配的记忆列表
        """
        # 查询缓存
        cache_key = self._cache_key("list_by_tags", ",".join(sorted(tags)), limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        memories = []
        seen_uids = set()

        def filter_by_tags(memo):
            memo_tags = memo.get("tags", [])
            return any(t in memo_tags for t in tags)

        # 使用分页获取所有匹配的记录
        all_matching = self.list_all_memos(
            max_records=limit,
            filter_fn=filter_by_tags
        )

        for m in all_matching:
            content = m.get("content", "")
            clean_content, _ = self._extract_tags(content)

            name = m.get("name", "")
            uid = name.replace("memos/", "") if name.startswith("memos/") else name

            if uid not in seen_uids:
                seen_uids.add(uid)
                memories.append(Memory(
                    id=uid,
                    uid=uid,
                    content=clean_content,
                    tags=m.get("tags", []),
                    visibility=m.get("visibility", "PRIVATE"),
                    created_at=m.get("createTime") or m.get("createTime"),
                    updated_at=m.get("updateTime") or m.get("updateTime"),
                    agent=self.agent
                ))

        # 写入缓存
        self._cache_set(cache_key, memories)
        return memories

    def search(self, query: str, limit: int = None) -> List[Memory]:
        """
        搜索记忆（支持分页获取全部）

        Args:
            query: 搜索关键词
            limit: 最大返回数量（None表示获取全部）

        Returns:
            匹配的记忆列表
        """
        # 查询缓存
        cache_key = self._cache_key("search", query, limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        memories = []
        seen_uids = set()
        page_token = None
        page_size = 1000
        safety_limit = 50

        for page_num in range(safety_limit):
            params = {
                "pageSize": page_size,
                "filter": f"content.contains('{query}')"
            }
            if page_token:
                params["pageToken"] = page_token

            try:
                resp = self._make_request(
                    "GET",
                    f"{self.base_url}/api/v1/memos",
                    params=params
                )
            except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
                break

            if not resp.ok:
                break

            data = resp.json()
            memos = data.get("memos", [])

            for m in memos:
                content = m.get("content", "")
                clean_content, _ = self._extract_tags(content)

                name = m.get("name", "")
                uid = name.replace("memos/", "") if name.startswith("memos/") else m.get("uid", "")

                if uid in seen_uids:
                    continue
                seen_uids.add(uid)

                api_tags = m.get("tags", [])
                memories.append(Memory(
                    id=uid,
                    uid=uid,
                    content=clean_content,
                    tags=api_tags,
                    visibility=m.get("visibility", "PRIVATE"),
                    created_at=m.get("createAt") or m.get("createTime"),
                    updated_at=m.get("updateAt") or m.get("updateTime"),
                    agent=self.agent
                ))

            # 检查是否达到限制
            if limit and len(memories) >= limit:
                memories = memories[:limit]
                break

            # 检查是否有下一页
            page_token = data.get("nextPageToken")
            if not page_token or len(memos) == 0:
                break

        # 写入缓存
        self._cache_set(cache_key, memories)
        return memories

    def list_all(self, limit: int = None) -> List[Memory]:
        """
        获取所有记忆记录（支持分页）

        Args:
            limit: 最大返回数量（None表示获取全部）

        Returns:
            记忆列表
        """
        memories = []
        seen_uids = set()

        def to_memory(m):
            content = m.get("content", "")
            clean_content, _ = self._extract_tags(content)
            name = m.get("name", "")
            uid = name.replace("memos/", "") if name.startswith("memos/") else name

            return Memory(
                id=uid,
                uid=uid,
                content=clean_content,
                tags=m.get("tags", []),
                visibility=m.get("visibility", "PRIVATE"),
                created_at=m.get("createTime") or m.get("createAt"),
                updated_at=m.get("updateTime") or m.get("updateAt"),
                agent=self.agent
            ), uid

        all_raw = self.list_all_memos(max_records=limit)
        for m in all_raw:
            mem, uid = to_memory(m)
            if uid not in seen_uids:
                seen_uids.add(uid)
                memories.append(mem)

        return memories

    def delete(self, memo_uid: str) -> bool:
        """删除记忆（使用 uid）"""
        try:
            resp = self._make_request(
                "PATCH",
                f"{self.base_url}/api/v1/memos/{memo_uid}",
                json={"rowStatus": "ARCHIVED"}
            )
            return resp.ok
        except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
            return False

    def update_memo(self, memo_uid: str, content: str = None, tags: List[str] = None) -> Optional[Memory]:
        """
        更新记忆条目

        Args:
            memo_uid: 记忆UID
            content: 新内容（可选，不提供则保留原内容）
            tags: 新标签列表（可选，提供则完全替换原标签）

        Returns:
            更新后的 Memory 对象，失败返回 None
        """
        # 1. 获取当前内容
        current = self.get_by_uid(memo_uid)
        if not current:
            return None

        # 2. 准备更新数据
        update_data = {}
        new_content = content or current.content

        if content is not None:
            # 脱敏处理
            new_content = self._sanitize(content)

        # 如果提供了标签，在内容末尾添加标签行（Memos 从内容中提取标签）
        if tags is not None:
            # 去重并添加 agent 标签
            auto_tags = list(set(tags))
            if f"{self.agent}-private" not in auto_tags:
                auto_tags.append(f"{self.agent}-private")

            # 构建标签行
            tag_line = "\n\n" + " ".join([f"#{tag}" for tag in auto_tags])
            new_content = new_content + tag_line

        update_data["content"] = new_content

        # 3. 调用 API 更新
        try:
            resp = self._make_request(
                "PATCH",
                f"{self.base_url}/api/v1/memos/{memo_uid}",
                json=update_data
            )
        except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
            return None

        if not resp.ok:
            return None

        # 4. 返回更新后的 Memory
        data = resp.json()
        name = data.get("name", "")
        uid = name.replace("memos/", "") if name.startswith("memos/") else name

        return Memory(
            id=uid,
            uid=uid,
            content=data.get("content", new_content),
            tags=data.get("tags", tags or current.tags),
            visibility=data.get("visibility", current.visibility),
            created_at=current.created_at,
            updated_at=data.get("updateTime"),
            agent=self.agent
        )

    def get_by_uid(self, memo_uid: str) -> Optional[Memory]:
        """根据 UID 获取记忆"""
        try:
            resp = self._make_request(
                "GET",
                f"{self.base_url}/api/v1/memos/{memo_uid}"
            )
        except (MemosRateLimitError, MemosAuthError, MemosPayloadTooLargeError, MemosServerError):
            return None

        if not resp.ok:
            return None

        m = resp.json()
        content = m.get("content", "")
        clean_content, tags = self._extract_tags(content)

        # 从 name 解析 uid
        name = m.get("name", "")
        uid = name.replace("memos/", "") if name.startswith("memos/") else m.get("uid", "")

        return Memory(
            id=uid,
            uid=uid,
            content=clean_content,
            tags=tags,
            visibility=m.get("visibility", "PRIVATE"),
            created_at=m.get("createAt") or m.get("createTime"),
            updated_at=m.get("updateAt") or m.get("updateTime"),
            agent=self.agent
        )

    # ==================== 批量 Ingest 接口（Clean/Expand模式）====================

    def batch_save(
        self,
        contents: List[Dict[str, Any]],
        mode: str = "clean",  # "clean" 或 "expand"
        visibility: str = "PUBLIC"
    ) -> Dict[str, Any]:
        """
        批量保存内容（简化版，不再写入level标签，热力系统仅在Wiki层维护）

        Args:
            contents: 内容列表，每项为 dict 包含:
                - content: 内容文本
                - tags: 标签列表
                - source: 来源
            mode: "clean" 或 "expand"（仅用于日志标识，不再写入标签）
            visibility: 可见性

        Returns:
            批量保存结果
        """
        from time import sleep

        results = {
            "mode": mode,
            "total": len(contents),
            "successful": [],
            "failed": [],
            "batches": []
        }

        for i in range(0, len(contents), self.ingest_batch_size):
            batch = contents[i:i + self.ingest_batch_size]
            batch_num = i // self.ingest_batch_size + 1
            total_batches = (len(contents) + self.ingest_batch_size - 1) // self.ingest_batch_size

            print(f"[Ingest] 批次 {batch_num}/{total_batches}: {len(batch)} 条")

            batch_results = []
            for item in batch:
                try:
                    # 仅保留原始标签，不再写入处理状态标签
                    # 蒸馏体系通过 fingerprint 表追踪，不在 Memos 上打 processed/ingest 标签
                    tags = item.get("tags", [])

                    result = self.save(
                        content=item["content"],
                        tags=tags,
                        visibility=visibility
                    )
                    batch_results.append({"status": "success", "uid": result.uid})
                    results["successful"].append(result.uid)

                except Exception as e:
                    print(f"  [Ingest] 失败: {e}")
                    batch_results.append({"status": "failed", "error": str(e)})
                    results["failed"].append({"content": item.get("content", "")[:100], "error": str(e)})

            results["batches"].append({
                "batch": batch_num,
                "count": len(batch),
                "results": batch_results
            })

            # 批次间隔（非最后一批）
            if i + self.ingest_batch_size < len(contents):
                print(f"[Ingest] 等待 {self.ingest_batch_interval}s...")
                sleep(self.ingest_batch_interval)

        print(f"[Ingest] 完成: {len(results['successful'])}/{len(contents)} 成功")
        return results

    # 保留旧方法名作为别名（兼容性）
    batch_save_with_heat = batch_save

    def ingest_clean_batch(self, records: List[Dict]) -> Dict:
        """
        Clean层批量Ingest（Raw → Source页 + 实体提取）

        Args:
            records: L1原始记录列表，每项包含 content, source, l1_uid
        """
        contents = []
        for record in records:
            content = record.get("content", "")
            source = record.get("source", "unknown")
            l1_uid = record.get("l1_uid", "")

            # 添加来源标记
            tagged_content = f"""# Clean: {source}

**来源**: [[L1-{l1_uid[:8]}]]
**时间**: {datetime.now().isoformat()}

---

{content}
"""
            contents.append({
                "content": tagged_content,
                "tags": [
                    f"from:{l1_uid[:8]}",
                    "type=clean-refined",
                    "scope:public"
                ]
            })

        return self.batch_save(contents, mode="clean")

    def ingest_expand_batch(self, synthesis: Dict) -> Dict:
        """
        Expand层批量Ingest（多源合成 → Wiki）

        Args:
            synthesis: 合成结果，包含 content, sources, entities
        """
        content = synthesis.get("content", "")
        sources = synthesis.get("sources", [])
        entities = synthesis.get("entities", [])

        # 添加来源引用
        source_refs = "\n".join([f"- [[{s}]]" for s in sources[:10]])
        entity_links = ", ".join([f"[[{e}]]" for e in entities[:5]])

        tagged_content = f"""# Expand Synthesis

**实体**: {entity_links}
**时间**: {datetime.now().isoformat()}

## 合成内容

{content}

## 引用来源
{source_refs}

---
*Expand层: 仅当积累3+素材或达到L3热度时触发*
"""
        return self.batch_save(
            [{"content": tagged_content, "tags": ["type=expand-synthesis", "scope:public"]}],
            mode="expand"
        )

    def mark_l1_processed(self, l1_uid: str, l2_uid: str = None) -> bool:
        """
        【已废弃】旧 Clean 体系的 processed 标签机制不再使用。
        蒸馏体系通过 fingerprint 表追踪处理状态，不在 Memos 上打标签。

        保留此方法作为兼容性空操作，避免调用方报错。
        """
        print("[Deprecated] mark_l1_processed 已废弃，蒸馏体系使用指纹表追踪状态")
        return True

    def save_session(self, working_dir: str, summary: str = ""):
        """保存会话状态"""
        session_content = f"[SESSION] {self.agent}\n工作目录: {working_dir}\n摘要: {summary}"
        return self.save(session_content, tags=["type=session"])

    def list_sessions(self, limit: int = 10) -> List[Memory]:
        """列出会话"""
        return self.list_by_tags(["type=session", f"agent={self.agent}"], limit=limit)

    def save_session_full(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        tags: List[str] = None,
        visibility: str = "PUBLIC",
    ) -> List[Memory]:
        """
        保存完整会话（L1原始池），所有AI框架通用。
        按消息分片（每组最多5条），每片携带精确的 _meta 元数据：
        hash（完整性校验）、range（消息范围）、segment（分片位置）、summary（摘要）。

        Args:
            session_id: 会话ID
            messages: 完整消息列表 [{role, content, timestamp, ...}]
            tags: 五维标签（由调用方生成传入）
            visibility: PUBLIC（默认）或 PRIVATE

        Returns:
            List[Memory] 分片记录列表（短 session 也返回列表）
        """
        import json

        # 计算完整内容的 hash（用于完整性校验）
        full_payload = {
            "session_id": session_id,
            "message_count": len(messages),
            "messages": messages,
        }
        full_json = json.dumps(full_payload, ensure_ascii=False, indent=2)
        content_hash = hashlib.md5(full_json.encode("utf-8")).hexdigest()[:8]

        # 按消息数分片（每组最多 5 条，保证单片不超限）
        MAX_MSGS_PER_CHUNK = 5
        total_chunks = (len(messages) + MAX_MSGS_PER_CHUNK - 1) // MAX_MSGS_PER_CHUNK
        memories = []

        for chunk_idx in range(0, len(messages), MAX_MSGS_PER_CHUNK):
            chunk_messages = messages[chunk_idx:chunk_idx + MAX_MSGS_PER_CHUNK]
            start_msg = chunk_idx + 1
            end_msg = min(chunk_idx + MAX_MSGS_PER_CHUNK, len(messages))
            seg_num = chunk_idx // MAX_MSGS_PER_CHUNK + 1

            # 生成摘要
            chunk_preview = json.dumps(chunk_messages, ensure_ascii=False)[:300].replace("\n", " ")
            summary = chunk_preview[:120]

            # 构建带 _meta 的 payload
            payload = {
                "_meta": {
                    "hash": content_hash,
                    "range": f"{start_msg}-{end_msg}",
                    "segment": f"{seg_num}/{total_chunks}",
                    "summary": summary,
                    "total_messages": len(messages),
                },
                "session_id": session_id,
                "message_count": len(chunk_messages),
                "messages": chunk_messages,
            }
            content = json.dumps(payload, ensure_ascii=False, indent=2)

            # 组装标签（精简统一体系）
            # P8: 五维标签 = source=, time=, model=, scope=, session=
            # SDK 自动标签：agent=（_auto_classify 添加）, hash=
            # 分片标签（仅多片时）：range=, segment=, type=chunk
            chunk_tags = (tags or []).copy()

            # P8: 自动添加/覆盖 session 标签
            chunk_tags = [t for t in chunk_tags if not t.startswith("session=")]
            chunk_tags.append(f"session={session_id}")

            # P8: 确保 level=L1 标签存在（蒸馏层通过此标签识别原始数据）
            # 移除旧的状态标记 processed=false
            chunk_tags = [t for t in chunk_tags if t != "processed=false"]
            if not any(t.startswith("level=") for t in chunk_tags):
                chunk_tags.append("level=L1")

            chunk_tags.append(f"hash={content_hash}")
            if total_chunks > 1:
                chunk_tags.append(f"range={start_msg}-{end_msg}")
                chunk_tags.append(f"segment={seg_num}/{total_chunks}")
                chunk_tags.append("type=chunk")

            # 如果单组仍超限，压缩处理
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > self.max_content_bytes - 500:
                # 去掉缩进
                content = json.dumps(payload, ensure_ascii=False)
                content_bytes = len(content.encode("utf-8"))
                if content_bytes > self.max_content_bytes - 500:
                    # 截断超长消息
                    for msg in payload["messages"]:
                        c = msg.get("content", "")
                        if len(c.encode("utf-8")) > 2000:
                            msg["content"] = c[:600] + "...[truncated]"
                    content = json.dumps(payload, ensure_ascii=False)

            result = self.save(content, chunk_tags, visibility)
            memories.append(result)

        return memories

    # ==================== 分段内容搜索与合并 ====================

    SEGMENT_PATTERN = re.compile(r'segment:(\d+)/(\d+)')
    HASH_PATTERN = re.compile(r'hash:([a-f0-9]{8})')
    SESSION_PATTERN = re.compile(r'session:([a-z0-9]+)')

    def search_and_merge_segments(self, query: str, limit: int = 20) -> List[Memory]:
        """
        搜索记忆，自动检测并合并分段内容

        工作流程:
        1. 执行普通搜索
        2. 识别分段记录
        3. 提取 hash/session 标识
        4. 搜索并合并所有相关分段
        5. 返回合并后的 Memory 列表

        Args:
            query: 搜索关键词
            limit: 返回结果数量限制

        Returns:
            List[Memory] 合并后的记忆列表（分段内容已合并为单条）
        """
        # 1. 执行初始搜索
        initial_results = self.search(query, limit=limit * 2)

        # 2. 分离分段记录和普通记录
        segmented_groups = {}  # hash -> [Memory, ...]
        normal_memories = []

        for mem in initial_results:
            # 检查是否为分段记录
            is_segmented = any('type=chunk' in t or 'segment=' in t for t in mem.tags)

            if is_segmented:
                # 提取标识符
                group_key = self._extract_segment_group_key(mem)
                if group_key:
                    if group_key not in segmented_groups:
                        segmented_groups[group_key] = []
                    segmented_groups[group_key].append(mem)
                else:
                    normal_memories.append(mem)
            else:
                normal_memories.append(mem)

        # 3. 合并分段内容
        merged_memories = []

        for group_key, segments in segmented_groups.items():
            # 获取所有分段（可能需要额外搜索）
            all_segments = self._fetch_all_segments(group_key, segments)

            # 合并为单条 Memory
            merged = self._merge_segments_to_memory(all_segments)
            if merged:
                merged_memories.append(merged)

        # 4. 合并结果并返回
        all_results = normal_memories + merged_memories

        # 按时间排序
        all_results.sort(key=lambda x: x.created_at or '', reverse=True)

        return all_results[:limit]

    def _extract_segment_group_key(self, memory: Memory) -> Optional[Tuple[str, str]]:
        """
        从分段记录中提取分组标识符

        优先级:
        1. hash=xxx (完整内容 hash，最可靠)
        2. session=xxx (session 标识)
        3. 从内容 JSON 中提取 session_id 或 hash
        """
        # 从标签中提取（新格式：hash=xxx, session=xxx）
        for tag in memory.tags:
            if tag.startswith('hash='):
                return ('hash', tag.split('=', 1)[1])
            if tag.startswith('session='):
                return ('session', tag.split('=', 1)[1])

        # 从内容 JSON 中提取 session_id 或 hash
        content = memory.content or ''
        try:
            json_start = content.find('{')
            json_end = content.rfind('}')
            if json_start >= 0 and json_end > json_start:
                data = json.loads(content[json_start:json_end + 1])
                meta = data.get('_meta', {})
                session_id = data.get('session_id', '')
                content_hash = meta.get('hash', '')
                if content_hash:
                    return ('hash', content_hash[:8])
                if session_id:
                    return ('session', session_id[:8])
        except (json.JSONDecodeError, ValueError):
            pass

        return None

    def _fetch_all_segments(self, group_key: Tuple[str, str],
                            known_segments: List[Memory]) -> List[Memory]:
        """
        获取分组的所有分段

        Args:
            group_key: (type, value) 如 ('hash', 'a1b2c3d4')
            known_segments: 已知的分段列表

        Returns:
            完整的分段列表（包含可能遗漏的分段）
        """
        key_type, key_value = group_key

        # 构建搜索标签（统一等号格式）
        search_tag = f"{key_type}={key_value}"

        # 搜索所有相关记录
        try:
            all_related = self.list_by_tags([search_tag], limit=50)

            # 过滤出分段记录
            all_segments = [
                m for m in all_related
                if any('segment=' in t or 'type=chunk' in t for t in m.tags)
            ]

            # 合并已知分段和新找到的分段（去重）
            seen_uids = {m.uid for m in known_segments}
            for seg in all_segments:
                if seg.uid not in seen_uids:
                    known_segments.append(seg)

        except Exception as e:
            print(f"[MemosClient] 获取分段失败: {e}")

        # 按分段位置排序
        return self._sort_segments_by_part_num(known_segments)

    def _sort_segments_by_part_num(self, segments: List[Memory]) -> List[Memory]:
        """按分段位置排序"""
        def get_part_num(mem):
            for tag in mem.tags:
                match = self.SEGMENT_PATTERN.search(tag)
                if match:
                    return int(match.group(1))
            return 999  # 无标记的排最后

        return sorted(segments, key=get_part_num)

    def _merge_segments_to_memory(self, segments: List[Memory]) -> Optional[Memory]:
        """
        将多个分段合并为单条 Memory

        合并策略:
        1. 保留第一条的元数据
        2. 合并所有内容（去除重复标记）
        3. 合并标签（去重）
        """
        if not segments:
            return None

        if len(segments) == 1:
            return segments[0]

        # 使用第一条作为主记录
        primary = segments[0]

        # 合并内容
        merged_content = self._merge_segment_contents(segments)

        # 合并标签（去重）
        all_tags = set()
        for seg in segments:
            all_tags.update(seg.tags)

        # 移除分段特有标签，添加合并标记
        merged_tags = [t for t in all_tags
                       if not t.startswith('segment=')
                       and not t.startswith('range=')
                       and not t.startswith('type=chunk')]
        merged_tags.append('type=merged')
        merged_tags.append(f'chunks={len(segments)}')
        # 保留 hash（所有片相同，去重）
        hash_tags = list(set(t for t in all_tags if t.startswith('hash=')))
        merged_tags.extend(hash_tags)

        # 创建新的 Memory 对象
        return Memory(
            id=primary.uid,
            uid=primary.uid,
            content=merged_content,
            tags=list(set(merged_tags)),
            visibility=primary.visibility,
            created_at=primary.created_at,
            updated_at=primary.updated_at,
            agent=primary.agent
        )

    def _merge_segment_contents(self, segments: List[Memory]) -> str:
        """
        合并分段内容

        清理策略:
        1. 移除每个分段的 header 标记
        2. 移除 "分段 X/Y 结束" 标记
        3. 保留第一个分段的元数据头部
        4. 按顺序拼接内容
        """
        parts = []

        for i, seg in enumerate(segments):
            content = seg.content or ''

            # 第一段：保留元数据头部，移除尾部标记
            if i == 0:
                # 移除尾部 "分段 X/Y 结束" 标记
                content = re.sub(r'\n?---\n?\n?\*\*\[分段 \d+/\d+ 结束\]\*\*.*?下一段.*?\n?', '', content, flags=re.DOTALL)
                parts.append(content)

            # 中间段：移除 header 和 footer
            elif i < len(segments) - 1:
                # 移除头部到第一个 --- 之后
                content = re.sub(r'^.*?---\n\n\*\*⚠️ 此为分段内容.*?(---\n\n)?', '', content, flags=re.DOTALL)
                # 移除尾部
                content = re.sub(r'\n?---\n?\n?\*\*\[分段 \d+/\d+ 结束\]\*\*.*?下一段.*?\n?', '', content, flags=re.DOTALL)
                parts.append(content.strip())

            # 最后一段：只移除 header
            else:
                content = re.sub(r'^.*?---\n\n\*\*⚠️ 此为分段内容.*?(---\n\n)?', '', content, flags=re.DOTALL)
                parts.append(content.strip())

        # 合并所有部分
        merged = '\n\n'.join(parts)

        # 添加合并标记
        merged = f"""[已合并 {len(segments)} 个分段]

{merged}

---
*此内容由 {len(segments)} 个分段合并而成，原始 Session: {segments[0].uid[:8]}...*
"""

        return merged

    def get_full_content_by_hash(self, content_hash: str) -> Optional[str]:
        """
        通过 content hash 获取完整内容（合并所有分段）

        Args:
            content_hash: 完整内容 hash（前8位或完整16位）

        Returns:
            合并后的完整内容，如果未找到则返回 None
        """
        # 搜索所有带此 hash 的记录
        results = self.list_by_tags([f'hash={content_hash[:8]}'], limit=50)

        # 过滤分段记录并排序
        segments = [
            m for m in results
            if any('segment=' in t or 'type=chunk' in t for t in m.tags)
        ]

        if not segments:
            return None

        segments = self._sort_segments_by_part_num(segments)
        merged = self._merge_segments_to_memory(segments)

        return merged.content if merged else None

    # ==================== MCP 集成存根 ====================

    @classmethod
    def as_mcp_server(cls) -> Dict[str, Any]:
        """
        返回 MCP Server 能力描述（存根）

        Returns:
            描述 MCP server 能力的字典，包含名称、版本、工具列表等
        """
        return {
            "name": "styx-memos",
            "version": "0.1.0",
            "description": "Memos knowledge store MCP server (stub)",
            "capabilities": {
                "tools": True,
                "resources": False,
                "prompts": False,
            },
            "tools": [
                {
                    "name": "memos_save",
                    "description": "Save a new memory to Memos",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Memory content"},
                            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                            "visibility": {"type": "string", "enum": ["PUBLIC", "PRIVATE"], "default": "PUBLIC"},
                        },
                        "required": ["content"],
                    },
                },
                {
                    "name": "memos_search",
                    "description": "Search memories by query",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "limit": {"type": "integer", "description": "Max results"},
                        },
                        "required": ["query"],
                    },
                },
                {
                    "name": "memos_list_by_tags",
                    "description": "List memories by tags",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to filter"},
                            "limit": {"type": "integer", "description": "Max results"},
                        },
                        "required": ["tags"],
                    },
                },
                {
                    "name": "memos_idempotent_save",
                    "description": "Save only if content doesn't already exist (by hash)",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Memory content"},
                            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                            "visibility": {"type": "string", "enum": ["PUBLIC", "PRIVATE"], "default": "PUBLIC"},
                        },
                        "required": ["content", "tags"],
                    },
                },
                {
                    "name": "memos_update_tags",
                    "description": "Add or remove tags from an existing memo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "memo_uid": {"type": "string", "description": "Memo UID"},
                            "add_tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to add"},
                            "remove_tags": {"type": "array", "items": {"type": "string"}, "description": "Tags to remove"},
                        },
                        "required": ["memo_uid"],
                    },
                },
            ],
        }


# ==================== 测试 ====================

if __name__ == '__main__':
    token = os.getenv("MEMOS_TOKEN", "your-token-here")
    client = MemosClient(token=token, agent='claude')

    # 测试保存
    mem = client.save("我偏好使用 pathlib 而不是 os.path", tags=["shared:preferences"])
    print(f"保存成功: ID={mem.id}, UID={mem.uid}")

    # 测试查询
    memories = client.list_by_tags(["shared"], limit=5)
    print(f"\n找到 {len(memories)} 条共享记忆:")
    for m in memories:
        print(f"  - [{m.id}] {m.content[:50]}...")
