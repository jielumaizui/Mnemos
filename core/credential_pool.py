# -*- coding: utf-8 -*-
"""
Credential Pool - API Key 池管理

特性：
- 多 Provider 支持（Anthropic / OpenAI / SiliconFlow / Gemini）
- 自动轮转 + 冷却机制
- 健康检查 + 失效自动切换
- 指数退避重试
- 宿主 Agent 环境变量自动加载（API Key 复用）

用法:
    from core.credential_pool import CredentialPool, Provider

    pool = CredentialPool()

    # 获取可用 key
    key = pool.get_key(Provider.ANTHROPIC)

    # 标记成功/失败
    pool.mark_success(key.id)
    pool.mark_failure(key.id, error="rate_limit")

    # 健康检查
    pool.health_check()
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)

import base64
import hashlib
import os
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any
from pathlib import Path

from core.config import get_config


# ==================== 1. 枚举与数据模型 ====================

class Provider(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    SILICONFLOW = "siliconflow"
    GEMINI = "gemini"


class KeyStatus(Enum):
    ACTIVE = "active"           # 正常使用
    COOLING = "cooling"         # 冷却中（暂时不可用）
    EXPIRED = "expired"         # 已过期/失效
    DISABLED = "disabled"       # 手动禁用


@dataclass
class Credential:
    """单个凭证"""
    id: str
    provider: Provider
    api_key: str
    api_base: Optional[str] = None
    model: Optional[str] = None
    status: KeyStatus = KeyStatus.ACTIVE
    # 使用统计
    total_calls: int = 0
    success_calls: int = 0
    failure_calls: int = 0
    last_used: Optional[datetime] = None
    # 冷却
    cooldown_until: Optional[datetime] = None
    cooldown_reason: Optional[str] = None
    # 元数据
    label: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["provider"] = self.provider.value
        d["status"] = self.status.value
        d["created_at"] = self.created_at.isoformat()
        d["last_used"] = self.last_used.isoformat() if self.last_used else None
        d["cooldown_until"] = self.cooldown_until.isoformat() if self.cooldown_until else None
        return d

    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.success_calls / self.total_calls

    @property
    def is_available(self) -> bool:
        if self.status == KeyStatus.DISABLED:
            return False
        if self.status == KeyStatus.EXPIRED:
            return False
        if self.cooldown_until and datetime.now(timezone.utc) < self.cooldown_until:
            return False
        return True


# ==================== 2. _LazyPath ====================

class _LazyPath:
    """Lazy path that resolves get_config().data_dir only on access."""
    __slots__ = ('_segments',)

    def __init__(self, *segments):
        self._segments = segments

    def __truediv__(self, other):
        return _LazyPath(*self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        result = get_config().data_dir
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


DB_PATH = _LazyPath("credential_pool.db")


# ==================== 3. Encryption Helpers ====================

def _derive_key() -> bytes:
    """派生轻量级加密密钥（基于用户环境，非强安全）"""
    from pathlib import Path
    seed = os.environ.get("HOME", "") + os.environ.get("USER", "") + str(Path.home())
    return hashlib.sha256(seed.encode()).digest()


def _encrypt_key(api_key: str) -> str:
    """轻量 XOR 加密 + base64（防止数据库文件被直接读取时暴露明文）"""
    if not api_key:
        return ""
    key = _derive_key()
    data = api_key.encode("utf-8")
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "enc:" + base64.b64encode(encrypted).decode()


def _decrypt_key(encrypted: str) -> str:
    """解密 api_key，向后兼容明文存储"""
    if not encrypted:
        return ""
    if not encrypted.startswith("enc:"):
        # 向后兼容：明文存储的旧数据
        return encrypted
    key = _derive_key()
    data = base64.b64decode(encrypted[4:].encode())
    decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return decrypted.decode("utf-8")


# ==================== 3. CredentialPool ====================

class CredentialPool:
    """
    API Key 池管理器

    设计：
    - 每个 Provider 一个 key 池
    - 轮询选择（加权：成功率高的优先）
    - 失败冷却：429/5xx 错误触发冷却
    - 指数退避：连续失败冷却时间递增
    """

    # 冷却配置
    COOLDOWN_MINUTES = {
        "rate_limit": 1,        # 429 → 冷却 1 分钟
        "server_error": 5,      # 5xx → 冷却 5 分钟
        "auth_error": 60,       # 401/403 → 冷却 60 分钟（可能是 key 失效）
        "timeout": 2,           # 超时 → 冷却 2 分钟
        "unknown": 5,           # 未知错误 → 冷却 5 分钟
    }

    # 最大连续失败次数（超过则标记 EXPIRED）
    MAX_CONSECUTIVE_FAILURES = 5

    def __init__(self, db_path: Optional[str] = None):
        if db_path is not None:
            self._db_path = Path(db_path)
        else:
            self._db_path = None  # 使用 _LazyPath
        self._local = threading.local()
        self._host_agent_loaded = False
        self._init_db()

        # 加载环境变量中的 key
        self._load_from_env()

    @property
    def db_path(self) -> Path:
        if self._db_path is not None:
            return self._db_path
        return Path(str(DB_PATH))

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            db = self.db_path
            db.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(str(db), timeout=10, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS credentials (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                api_key TEXT NOT NULL,
                api_base TEXT,
                model TEXT,
                status TEXT DEFAULT 'active',
                total_calls INTEGER DEFAULT 0,
                success_calls INTEGER DEFAULT 0,
                failure_calls INTEGER DEFAULT 0,
                consecutive_failures INTEGER DEFAULT 0,
                last_used TIMESTAMP,
                cooldown_until TIMESTAMP,
                cooldown_reason TEXT,
                label TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_cred_provider ON credentials(provider);
            CREATE INDEX IF NOT EXISTS idx_cred_status ON credentials(status);
        """)
        conn.commit()

    def _load_from_env(self):
        """从环境变量加载 key"""
        env_mappings = [
            (Provider.ANTHROPIC, "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"),
            (Provider.OPENAI, "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"),
            (Provider.SILICONFLOW, "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL"),
            (Provider.GEMINI, "GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL"),
        ]

        for provider, key_env, base_env, model_env in env_mappings:
            api_key = os.getenv(key_env)
            if api_key:
                self.add_key(
                    provider=provider,
                    api_key=api_key,
                    api_base=os.getenv(base_env),
                    model=os.getenv(model_env),
                    label=f"env:{key_env}",
                )

    def load_from_host_agent(self):
        """
        从宿主 Agent 环境变量自动读取 API Key

        支持：
        - ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN → Provider.ANTHROPIC
        - OPENAI_API_KEY → Provider.OPENAI
        - SILICONFLOW_API_KEY → Provider.SILICONFLOW
        - GEMINI_API_KEY → Provider.GEMINI
        """
        if self._host_agent_loaded:
            return

        host_mappings = [
            (Provider.ANTHROPIC, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]),
            (Provider.OPENAI, ["OPENAI_API_KEY"]),
            (Provider.SILICONFLOW, ["SILICONFLOW_API_KEY"]),
            (Provider.GEMINI, ["GEMINI_API_KEY"]),
        ]

        for provider, env_names in host_mappings:
            for env_name in env_names:
                api_key = os.getenv(env_name)
                if api_key:
                    self.add_key(
                        provider=provider,
                        api_key=api_key,
                        label=f"host_agent:{env_name}",
                    )

        self._host_agent_loaded = True

    # ---- Key 管理 ----

    def add_key(self, provider: Provider, api_key: str,
                api_base: Optional[str] = None,
                model: Optional[str] = None,
                label: Optional[str] = None) -> Credential:
        """添加 key 到池中"""
        conn = self._get_conn()

        key_hash = self._hash_key(api_key)
        encrypted_key = _encrypt_key(api_key)

        # 检查是否已存在相同 key（数据库中存储的是加密后的 key）
        existing = conn.execute(
            "SELECT id FROM credentials WHERE api_key = ?",
            (encrypted_key,),
        ).fetchone()
        cred = Credential(
            id=f"cred_{provider.value}_{key_hash[:8]}",
            provider=provider,
            api_key=api_key,
            api_base=api_base,
            model=model,
            label=label,
        )

        if existing:
            # 更新已有记录
            conn.execute(
                """UPDATE credentials SET
                    api_base = ?, model = ?, label = ?, status = 'active'
                   WHERE api_key = ?""",
                (api_base, model, label, encrypted_key),
            )
        else:
            conn.execute(
                """INSERT INTO credentials
                    (id, provider, api_key, api_base, model, status, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (cred.id, cred.provider.value, encrypted_key, cred.api_base,
                 cred.model, cred.status.value, json.dumps({})),
            )

        conn.commit()
        return cred

    def remove_key(self, cred_id: str) -> bool:
        """从池中移除 key"""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM credentials WHERE id = ?", (cred_id,))
        conn.commit()
        return cursor.rowcount > 0

    def get_key(self, provider: Provider,
                strategy: str = "weighted") -> Optional[Credential]:
        """
        获取可用 key

        Args:
            provider: Provider 类型
            strategy: 选择策略（weighted=加权轮询, round_robin=轮询, random=随机）
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        # 先清理已过期的冷却
        conn.execute(
            """UPDATE credentials SET status = 'active', cooldown_until = NULL
               WHERE provider = ? AND status = 'cooling' AND cooldown_until < ?""",
            (provider.value, now),
        )

        # 获取可用 key
        rows = conn.execute(
            """SELECT * FROM credentials
               WHERE provider = ? AND status = 'active'
                 AND (cooldown_until IS NULL OR cooldown_until < ?)
               ORDER BY success_calls DESC, total_calls ASC""",
            (provider.value, now),
        ).fetchall()

        if not rows:
            return None

        creds = [self._row_to_cred(r) for r in rows]

        if strategy == "weighted":
            return self._weighted_select(creds)
        elif strategy == "round_robin":
            return self._round_robin_select(provider, creds)
        else:
            import random
            return random.choice(creds)

    def get_all_keys(self, provider: Optional[Provider] = None) -> List[Credential]:
        """获取所有 key"""
        conn = self._get_conn()
        if provider:
            rows = conn.execute(
                "SELECT * FROM credentials WHERE provider = ?",
                (provider.value,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM credentials").fetchall()
        return [self._row_to_cred(r) for r in rows]

    # ---- 使用记录 ----

    def mark_success(self, cred_id: str):
        """标记调用成功"""
        conn = self._get_conn()
        conn.execute(
            """UPDATE credentials SET
                total_calls = total_calls + 1,
                success_calls = success_calls + 1,
                consecutive_failures = 0,
                last_used = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), cred_id),
        )
        conn.commit()

    def mark_failure(self, cred_id: str, error: str = "unknown"):
        """
        标记调用失败

        根据错误类型触发冷却：
        - rate_limit → 短冷却
        - auth_error → 长冷却（可能 key 已失效）
        - server_error → 中等冷却
        """
        conn = self._get_conn()

        # 分类错误
        error_type = self._classify_error(error)
        cooldown_min = self.COOLDOWN_MINUTES.get(error_type, 5)

        # 连续失败次数 +1
        row = conn.execute(
            "SELECT consecutive_failures FROM credentials WHERE id = ?",
            (cred_id,),
        ).fetchone()
        consecutive = (row[0] if row else 0) + 1

        # 指数退避：冷却时间 = 基础 * 2^(连续失败-1)
        actual_cooldown = cooldown_min * (2 ** max(0, consecutive - 1))
        cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=actual_cooldown)

        # 超过最大连续失败则标记过期
        new_status = "active"
        if consecutive >= self.MAX_CONSECUTIVE_FAILURES:
            new_status = "expired"
        elif error_type in ("rate_limit", "server_error", "timeout", "auth_error"):
            new_status = "cooling"

        conn.execute(
            """UPDATE credentials SET
                total_calls = total_calls + 1,
                failure_calls = failure_calls + 1,
                consecutive_failures = ?,
                last_used = ?,
                cooldown_until = ?,
                cooldown_reason = ?,
                status = ?
               WHERE id = ?""",
            (
                consecutive,
                datetime.now(timezone.utc).isoformat(),
                cooldown_until.isoformat() if new_status == "cooling" else None,
                error_type,
                new_status,
                cred_id,
            ),
        )
        conn.commit()

    def _classify_error(self, error: str) -> str:
        """分类错误类型"""
        error_lower = error.lower()
        if any(k in error_lower for k in ["429", "rate limit", "ratelimit", "too many"]):
            return "rate_limit"
        if any(k in error_lower for k in ["401", "403", "unauthorized", "auth", "invalid key", "api key"]):
            return "auth_error"
        if any(k in error_lower for k in ["500", "502", "503", "504", "server", "internal"]):
            return "server_error"
        if any(k in error_lower for k in ["timeout", "timed out", "connection", "network"]):
            return "timeout"
        return "unknown"

    # ---- 选择策略 ----

    def _weighted_select(self, creds: List[Credential]) -> Optional[Credential]:
        """加权选择：成功率高的优先"""
        if not creds:
            return None

        # 按成功率排序，成功率相同按总调用数少的优先
        creds.sort(key=lambda c: (c.success_rate, -c.total_calls), reverse=True)

        # 加权随机：给前几位更高权重
        import random
        weights = [1.0 / (i + 1) for i in range(len(creds))]
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return creds[i]
        return creds[0]

    def _round_robin_select(self, provider: Provider,
                            creds: List[Credential]) -> Optional[Credential]:
        """轮询选择"""
        if not creds:
            return None

        # 简单的基于总调用数最少的选择
        creds.sort(key=lambda c: c.total_calls)
        return creds[0]

    # ---- 健康检查 ----

    def health_check(self, provider: Optional[Provider] = None) -> Dict[str, Any]:
        """
        健康检查：统计各 Provider 的 key 状态

        Returns:
            {
                "anthropic": {"total": 3, "active": 2, "cooling": 1, ...},
                "openai": {...},
            }
        """
        conn = self._get_conn()
        providers = [provider] if provider else list(Provider)

        result = {}
        for p in providers:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM credentials WHERE provider = ? GROUP BY status",
                (p.value,),
            ).fetchall()
            status_counts = {r[0]: r[1] for r in rows}

            # 获取总体统计
            total = sum(status_counts.values())
            total_calls = conn.execute(
                "SELECT SUM(total_calls) FROM credentials WHERE provider = ?",
                (p.value,),
            ).fetchone()[0] or 0

            result[p.value] = {
                "total_keys": total,
                "status_breakdown": status_counts,
                "total_calls": total_calls,
            }

        return result

    def reset_cooldown(self, cred_id: str) -> bool:
        """手动重置冷却状态"""
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE credentials SET
                status = 'active', cooldown_until = NULL, cooldown_reason = NULL,
                consecutive_failures = 0
               WHERE id = ?""",
            (cred_id,),
        )
        conn.commit()
        return cursor.rowcount > 0

    # ---- 工具方法 ----

    def _row_to_cred(self, row: sqlite3.Row) -> Credential:
        return Credential(
            id=row["id"],
            provider=Provider(row["provider"]),
            api_key=_decrypt_key(row["api_key"]),
            api_base=row["api_base"],
            model=row["model"],
            status=KeyStatus(row["status"]),
            total_calls=row["total_calls"],
            success_calls=row["success_calls"],
            failure_calls=row["failure_calls"],
            last_used=datetime.fromisoformat(row["last_used"]) if row["last_used"] else None,
            cooldown_until=datetime.fromisoformat(row["cooldown_until"]) if row["cooldown_until"] else None,
            cooldown_reason=row["cooldown_reason"],
            label=row["label"],
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """计算 key 的哈希（用于 ID 生成，不存储）"""
        import hashlib
        return hashlib.sha256(api_key.encode()).hexdigest()


# ==================== 与 LLM 调用集成 ====================

def get_anthropic_client_with_fallback():
    """
    获取 Anthropic client，自动使用 CredentialPool 选择可用 key

    如果主 key 失败，自动切换到备用 key。
    """
    pool = get_default_pool()
    cred = pool.get_key(Provider.ANTHROPIC)

    if not cred:
        # 回退到环境变量
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("No available Anthropic API key")
        import anthropic
        return anthropic.Anthropic(api_key=api_key), None

    import anthropic
    kwargs = {"api_key": cred.api_key}
    if cred.api_base:
        kwargs["base_url"] = cred.api_base

    client = anthropic.Anthropic(**kwargs)
    return client, cred


# ==================== 便捷函数 ====================

_default_pool: Optional[CredentialPool] = None
_pool_lock = threading.Lock()


def get_default_pool() -> CredentialPool:
    """获取全局默认 CredentialPool 实例（自动加载宿主 Agent 环境变量）"""
    global _default_pool
    if _default_pool is None:
        with _pool_lock:
            if _default_pool is None:
                _default_pool = CredentialPool()
                _default_pool.load_from_host_agent()
    return _default_pool


# ==================== CLI ====================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Credential Pool CLI")
    parser.add_argument("--add", nargs=2, metavar=("PROVIDER", "API_KEY"),
                        help="添加 key")
    parser.add_argument("--base", help="API base URL")
    parser.add_argument("--model", help="默认模型")
    parser.add_argument("--list", action="store_true", help="列出所有 key")
    parser.add_argument("--provider", help="筛选 Provider")
    parser.add_argument("--health", action="store_true", help="健康检查")
    parser.add_argument("--reset", help="重置冷却状态的 key ID")
    parser.add_argument("--remove", help="移除 key ID")
    args = parser.parse_args()

    pool = get_default_pool()

    if args.add:
        provider = Provider(args.add[0])
        cred = pool.add_key(
            provider=provider,
            api_key=args.add[1],
            api_base=args.base,
            model=args.model,
        )
        logger.info(f"添加: {cred.id} ({cred.provider.value})")
        return

    if args.list:
        provider = Provider(args.provider) if args.provider else None
        creds = pool.get_all_keys(provider)
        for c in creds:
            status_icon = "OK" if c.is_available else "XX"
            print(f"{status_icon} {c.id} | {c.provider.value} | {c.status.value} | "
                  f"calls={c.total_calls} success={c.success_rate:.1%} | {c.label or ''}")
        return

    if args.health:
        health = pool.health_check()
        logger.info(json.dumps(health, indent=2, ensure_ascii=False))
        return

    if args.reset:
        if pool.reset_cooldown(args.reset):
            logger.info(f"已重置: {args.reset}")
        else:
            logger.info("未找到")
        return

    if args.remove:
        if pool.remove_key(args.remove):
            logger.info(f"已移除: {args.remove}")
        else:
            logger.info("未找到")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
