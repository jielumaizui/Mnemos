"""
WeChat Collector - 微信聊天记录采集模块

职责：
- 定位并读取微信SQLite数据库
- 解密（如需要）并提取用户自己的发言
- 分析情感、话题、时间模式
- 输出标准化WechatSignal到SignalStore

隐私原则：
- 只分析自己的发言（Des=0）
- 不存储原始聊天内容，只存content_hash和提取的信号
- 敏感信息（手机号、地址等）过滤

技术说明：
- Mac版微信使用SQLCipher加密SQLite数据库
- 解密需要微信的密钥（从内存中提取或通过其他方式获取）
- 本模块提供框架，解密逻辑可插拔
"""

import os
import re
import sys
import json
import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Iterator, Tuple
from datetime import datetime, timedelta
from collections import Counter


from .signal_store import SignalStore, get_signal_store, WechatSignal
import logging

logger = logging.getLogger(__name__)


# ========== 配置 ==========

# 微信数据路径（平台特定，当前仅支持 macOS）
if sys.platform == "darwin":
    WECHAT_BASE_DIR = Path.home() / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data" / "Library" / "Application Support" / "com.tencent.xinWeChat"
elif sys.platform == "win32":
    # Windows 微信数据路径（尚未实现解密逻辑）
    WECHAT_BASE_DIR = Path.home() / "Documents" / "WeChat Files"
else:
    # Linux 桌面版微信路径（尚未实现）
    WECHAT_BASE_DIR = Path.home() / ".wechat"

PLATFORM_SUPPORTED = sys.platform == "darwin"

# 敏感信息过滤规则
SENSITIVE_PATTERNS = [
    (r'\b1[3-9]\d{9}\b', '[PHONE]'),           # 手机号
    (r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]'),  # 邮箱
    (r'\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日]?', '[DATE]'),  # 日期
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '[IP]'),  # IP地址
]

# 情感关键词（简化版）
POSITIVE_WORDS = ['好', '棒', '赞', '开心', '谢谢', '完美', '不错', '喜欢', '哈哈', '嘻嘻', '太好了', '给力']
NEGATIVE_WORDS = ['烦', '累', '糟', '差', '讨厌', '郁闷', '无语', '失望', '生气', '难受', '痛苦', 'md']


# ========== WeChatCollector 类 ==========

class WeChatCollector:
    """微信聊天记录采集器"""

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()
        self.db_path: Optional[Path] = None
        self.contact_db: Optional[Path] = None
        self.my_wxid: Optional[str] = None
        self._cipher_key: Optional[bytes] = None

    # ---- 数据库发现 ----

    def discover_databases(self) -> List[Dict]:
        """
        发现本地微信数据库。

        当前仅支持 macOS。Windows/Linux 路径已预留但未实现解密逻辑。

        Returns:
            列表，每项包含账号信息和数据库路径
        """
        accounts = []

        if not PLATFORM_SUPPORTED:
            return accounts

        if not WECHAT_BASE_DIR.exists():
            return accounts

        # 查找账号目录（通常是 hash 命名的文件夹）
        for account_dir in WECHAT_BASE_DIR.iterdir():
            if not account_dir.is_dir():
                continue

            # 查找数据库文件
            chat_dbs = list(account_dir.rglob("Chat_*.db"))
            contact_db = account_dir / "Contact" / "Contact.sqlite"
            session_db = account_dir / "SessionInfo" / "SessionInfo.sqlite"

            if chat_dbs:
                accounts.append({
                    "account_dir": str(account_dir),
                    "account_name": account_dir.name,
                    "chat_dbs": [str(p) for p in chat_dbs],
                    "contact_db": str(contact_db) if contact_db.exists() else None,
                    "session_db": str(session_db) if session_db.exists() else None,
                })

        return accounts

    # ---- 解密 ----

    def set_cipher_key(self, key: str):
        """
        设置解密密钥。

        密钥获取方式：
        1. 从微信进程内存中提取（需要外部工具）
        2. 使用开源工具如 wechat-dump
        3. 手动导出未加密备份

        Args:
            key: SQLCipher 密钥字符串
        """
        self._cipher_key = key.encode('utf-8') if isinstance(key, str) else key

    def _connect_db(self, db_path: Path) -> Optional[sqlite3.Connection]:
        """
        连接数据库（自动处理解密）。

        如果设置了cipher_key，尝试解密连接。
        否则尝试直接连接（未加密或已解密的数据库）。
        """
        try:
            if self._cipher_key:
                # 使用 SQLCipher 扩展解密
                # 注意：需要安装 sqlcipher 或 pysqlcipher3
                return self._connect_sqlcipher(db_path)
            else:
                # 尝试直接连接（可能已解密或不加密）
                conn = sqlite3.connect(str(db_path))
                # 验证连接：尝试读取表列表
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
                cursor.fetchone()
                return conn
        except sqlite3.DatabaseError as e:
            if "file is not a database" in str(e).lower() or "not a database" in str(e).lower():
                print(f"[WeChat] 数据库已加密，需要解密密钥: {db_path.name}")
                print(f"         原因: macOS 版微信使用 SQLCipher 加密本地数据库")
                print(f"         解决: 使用 import_wechat_text() 手动导入聊天记录")
            else:
                print(f"[WeChat] 连接数据库失败: {db_path} - {e}")
            return None
        except Exception as e:
            print(f"[WeChat] 连接数据库失败: {db_path} - {e}")
            return None

    def _connect_sqlcipher(self, db_path: Path) -> Optional[sqlite3.Connection]:
        """使用SQLCipher解密连接"""
        try:
            # 尝试使用 pysqlcipher3
            from pysqlcipher3 import dbapi2 as sqlcipher
            conn = sqlcipher.connect(str(db_path))
            _cipher_key_escaped = self._cipher_key.decode().replace('"', '""')
            conn.execute(f'PRAGMA key = "{_cipher_key_escaped}";')
            # 验证连接
            conn.execute("SELECT count(*) FROM sqlite_master;")
            return conn
        except ImportError:
            print("[WeChat] pysqlcipher3 未安装，尝试替代方案...")
        except Exception as e:
            print(f"[WeChat] SQLCipher解密失败: {e}")

        # 备选：尝试使用 sqlcipher 命令行工具
        try:
            decrypted_path = db_path.parent / f"{db_path.stem}_decrypted.db"
            subprocess.run(
                [
                    "sqlcipher", str(db_path),
                    f"PRAGMA key = '{self._cipher_key.decode().replace(chr(39), chr(39)+chr(39))}'; ATTACH DATABASE '{decrypted_path}' AS plaintext KEY ''; SELECT sqlcipher_export('plaintext'); DETACH DATABASE plaintext;"
                ],
                capture_output=True, timeout=30, check=True
            )
            if decrypted_path.exists():
                return sqlite3.connect(str(decrypted_path))
        except Exception as e:
            logger.warning(f"忽略异常: {e}")

        return None

    def decrypt_with_external_tool(self, tool_path: str = None) -> bool:
        """
        使用外部工具解密数据库。

        支持的方案：
        1. wechat-dump (https://github.com/0xHJK/wechat-dump)
        2. 手动导出微信聊天记录为文本

        Returns:
            是否成功
        """
        # 这里预留外部工具调用接口
        print("[WeChat] 外部解密工具需要手动配置")
        print("  方案1: 安装 pysqlcipher3 并设置密钥")
        print("  方案2: 使用 wechat-dump 工具导出")
        print("  方案3: 微信自带导出功能导出为文本")
        return False

    # ---- 消息提取 ----

    def extract_my_messages(self, chat_db_path: str = None, days: int = 30,
                            limit: int = 5000) -> List[Dict]:
        """
        提取自己的微信消息。

        Args:
            chat_db_path: 指定数据库路径，None则自动发现
            days: 最近N天
            limit: 最大提取条数

        Returns:
            消息列表，每项包含时间、内容hash、长度等
        """
        if chat_db_path:
            db_path = Path(chat_db_path)
        else:
            dbs = self.discover_databases()
            if not dbs:
                return []
            # 使用第一个账号的第一个数据库
            db_path = Path(dbs[0]["chat_dbs"][0])

        self.db_path = db_path

        # 获取自己的wxid
        self.my_wxid = self._get_my_wxid()

        conn = self._connect_db(db_path)
        if not conn:
            return []

        messages = []
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_timestamp = int(cutoff.timestamp())

        try:
            cursor = conn.cursor()

            # 获取所有消息表（微信按聊天对象分表）
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Chat_%'")
            tables = [row[0] for row in cursor.fetchall()]

            for table in tables:
                try:
                    # 查询自己的消息 (Des=0 表示自己发的)
                    # 注意：不同版本的微信字段名可能不同
                    cursor.execute(f"""
                        SELECT CreateTime, Message, Des, Type
                        FROM {table}
                        WHERE CreateTime > ?
                          AND Des = 0
                          AND Type = 1
                        ORDER BY CreateTime DESC
                        LIMIT ?
                    """, (cutoff_timestamp, limit))

                    for row in cursor.fetchall():
                        create_time, content, des, msg_type = row

                        if not content or not isinstance(content, str):
                            continue

                        # 过滤系统消息和过短的
                        if len(content.strip()) < 2:
                            continue

                        # 过滤敏感信息
                        has_sensitive, filtered_content = self._filter_sensitive(content)

                        messages.append({
                            "timestamp": datetime.fromtimestamp(create_time).isoformat(),
                            "content": filtered_content,
                            "content_hash": hashlib.md5(content.encode()).hexdigest()[:16],
                            "msg_length": len(content),
                            "has_sensitive_content": has_sensitive,
                            "chat_table": table,
                        })

                except Exception:
                    continue

            # 按时间排序
            messages.sort(key=lambda x: x["timestamp"])

        finally:
            conn.close()

        return messages

    def _get_my_wxid(self) -> Optional[str]:
        """获取自己的微信ID"""
        if not self.db_path:
            return None

        # 尝试从 Contact.sqlite 获取
        contact_db = self.db_path.parent / "Contact" / "Contact.sqlite"
        if contact_db.exists():
            try:
                conn = sqlite3.connect(str(contact_db))
                cursor = conn.cursor()
                # 自己的联系人信息通常在特定表中
                cursor.execute("SELECT UserName FROM Contact WHERE UserName LIKE 'wxid_%' LIMIT 1")
                row = cursor.fetchone()
                conn.close()
                if row:
                    return row[0]
            except Exception as e:
                logger.warning(f"忽略异常: {e}")

        return None

    def _filter_sensitive(self, content: str) -> Tuple[bool, str]:
        """
        过滤敏感信息。

        Returns:
            (是否含敏感信息, 过滤后的内容)
        """
        has_sensitive = False
        filtered = content

        for pattern, replacement in SENSITIVE_PATTERNS:
            if re.search(pattern, filtered):
                has_sensitive = True
                filtered = re.sub(pattern, replacement, filtered)

        return has_sensitive, filtered

    # ---- 情感分析 ----

    def analyze_emotion(self, content: str) -> Tuple[float, float]:
        """
        简单情感分析。

        Returns:
            (valence: -1到1, arousal: 0到1)
        """
        positive_count = sum(1 for w in POSITIVE_WORDS if w in content)
        negative_count = sum(1 for w in NEGATIVE_WORDS if w in content)

        total = positive_count + negative_count
        if total == 0:
            return 0.0, 0.0

        # valence: 正负情感倾向
        valence = (positive_count - negative_count) / max(total, 1)
        valence = max(-1.0, min(1.0, valence))

        # arousal: 情感强度（感叹号、大写、表情等）
        arousal_signals = (
            content.count('！') + content.count('!') +
            content.count('？') + content.count('?') +
            len(re.findall(r'[哈哈|嘻嘻|呜呜|啊啊]', content)) +
            len(re.findall(r'[🔥|💪|❤️|😂|🤣|😊|😄|😆|😭|😡]', content))
        )
        arousal = min(1.0, arousal_signals / 5)

        return valence, arousal

    def extract_topics(self, content: str) -> List[str]:
        """
        简单话题提取（基于关键词匹配）。

        未来可替换为更精确的NLP模型。
        """
        topics = []

        topic_keywords = {
            "工作": ["项目", "客户", "需求", "上线", "bug", "会议", "加班", "deadline"],
            "技术": ["代码", "程序", "算法", "架构", "数据库", "API", "框架", "部署"],
            "产品": ["用户", "体验", "功能", "设计", "迭代", "反馈", "数据"],
            "生活": ["吃饭", "睡觉", "天气", "电影", "游戏", "旅行", "周末", "假期"],
            "人际": ["朋友", "同事", "老板", "家人", "聚会", "约", "见面"],
            "情绪": ["开心", "烦", "累", "郁闷", "兴奋", "焦虑", "期待"],
            "学习": ["书", "课程", "学习", "考试", "证书", "培训", "技能"],
            "投资": ["股票", "基金", "理财", "房价", "涨", "跌", "收益", "亏损"],
        }

        for topic, keywords in topic_keywords.items():
            if any(kw in content for kw in keywords):
                topics.append(topic)

        return topics

    # ---- 信号存储 ----

    def collect_and_store(self, chat_db_path: str = None, days: int = 30) -> int:
        """
        采集微信消息并存储为信号。

        Returns:
            存储的信号数量
        """
        messages = self.extract_my_messages(chat_db_path, days)
        if not messages:
            return 0

        count = 0
        daily_sequence = Counter()  # 记录每天的消息序号

        for msg in messages:
            # 计算当天序号
            date = msg["timestamp"][:10]
            daily_sequence[date] += 1

            # 情感分析
            valence, arousal = self.analyze_emotion(msg["content"])

            # 话题提取
            topics = self.extract_topics(msg["content"])

            # 时间特征
            dt = datetime.fromisoformat(msg["timestamp"])

            signal = WechatSignal(
                timestamp=msg["timestamp"],
                content_hash=msg["content_hash"],
                msg_length=msg["msg_length"],
                emotional_valence=valence,
                emotional_arousal=arousal,
                topic_tags=topics,
                chat_type="unknown",  # 需要进一步区分私聊/群聊
                hour_of_day=dt.hour,
                day_of_week=dt.weekday(),
                msg_sequence_in_day=daily_sequence[date],
                has_sensitive_content=msg["has_sensitive_content"],
            )

            try:
                self.store.insert_wechat_signal(signal)
                count += 1
            except Exception:
                continue

        return count

    # ---- 便捷查询 ----

    def get_collection_summary(self) -> str:
        """获取采集摘要"""
        dbs = self.discover_databases()
        lines = ["📱 微信采集摘要"]
        lines.append(f"发现账号: {len(dbs)}")
        for acc in dbs:
            lines.append(f"  {acc['account_name']}: {len(acc['chat_dbs'])} 个聊天数据库")
        return "\n".join(lines)


# ========== 手动导入Fallback ==========

class WeChatManualImporter:
    """
    微信手动导入器。

    当自动采集不可用时（数据库加密无法解密），
    允许用户手动导出聊天记录并导入。

    支持格式：
    1. 微信自带导出（聊天记录迁移生成的文本文件）
    2. 第三方工具导出（如 wxbackup、wechat-exporter）
    3. 手动复制粘贴的文本（macOS 微信多种格式兼容）
    """

    # 支持多种常见复制粘贴格式
    PATTERNS = [
        # 格式1: 2026-05-17 14:30 [我] 消息内容
        re.compile(r'(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}(?::\d{2})?)\s*\[我\]\s*(.+)'),
        # 格式2: [2026/5/17 14:30] 我: 消息内容
        re.compile(r'\[(\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{2}:\d{2}(?::\d{2})?)\]\s*我[:：]\s*(.+)'),
        # 格式3: 2026/05/17 14:30 我 消息内容
        re.compile(r'(\d{4}[/-]\d{2}[/-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*我\s+(.+)'),
        # 格式4: 14:30 [我] 消息内容（当天，无日期）
        re.compile(r'(\d{2}:\d{2}(?::\d{2})?)\s*\[我\]\s*(.+)'),
    ]

    @staticmethod
    def import_from_text(text: str, store: SignalStore = None,
                         default_date: str = None) -> int:
        """
        从文本导入微信消息。

        支持多种格式（每行一条）：
        2026-05-17 14:30 [我] 消息内容
        [2026/5/17 14:30] 我: 消息内容
        14:30 [我] 消息内容

        Args:
            text: 复制的聊天记录文本
            store: SignalStore 实例
            default_date: 如果文本中只有时间没有日期，使用此默认日期 (YYYY-MM-DD)
        """
        store = store or get_signal_store()
        count = 0

        for line in text.strip().split('\n'):
            line = line.strip()
            if not line or len(line) < 5:
                continue

            for pattern in WeChatManualImporter.PATTERNS:
                match = pattern.match(line)
                if match:
                    timestamp_str, content = match.groups()
                    dt = WeChatManualImporter._parse_timestamp(
                        timestamp_str, default_date
                    )
                    if dt is None:
                        continue

                    collector = WeChatCollector()
                    valence, arousal = collector.analyze_emotion(content)
                    topics = collector.extract_topics(content)

                    signal = WechatSignal(
                        timestamp=dt.isoformat(),
                        content_hash=hashlib.md5(content.encode()).hexdigest()[:16],
                        msg_length=len(content),
                        emotional_valence=valence,
                        emotional_arousal=arousal,
                        topic_tags=topics,
                        chat_type="unknown",
                        hour_of_day=dt.hour,
                        day_of_week=dt.weekday(),
                    )

                    store.insert_wechat_signal(signal)
                    count += 1
                    break  # 匹配到一个格式即可

        return count

    @staticmethod
    def _parse_timestamp(timestamp_str: str, default_date: str = None) -> Optional[datetime]:
        """解析时间字符串，支持多种格式。"""
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%H:%M:%S",
            "%H:%M",
        ]

        for fmt in formats:
            try:
                dt = datetime.strptime(timestamp_str, fmt)
                # 如果只有时间没有日期，补充默认日期
                if fmt in ("%H:%M:%S", "%H:%M") and default_date:
                    date_part = datetime.strptime(default_date, "%Y-%m-%d")
                    dt = dt.replace(year=date_part.year, month=date_part.month,
                                    day=date_part.day)
                return dt
            except ValueError:
                continue

        return None

    @staticmethod
    def get_import_guide() -> str:
        """返回手动导入操作指南。"""
        return '''
=== 微信聊天记录手动导入指南 ===

由于 macOS 版微信使用 SQLCipher 加密本地数据库，且数据库密钥受系统保护，
自动读取需要安装额外工具或修改系统安全设置。以下是安全的手动导入方法：

【方法1：复制粘贴（推荐）】
1. 打开微信 Mac 版
2. 进入一个聊天窗口
3. 选中你想分析的消息（可多选）
4. 按 Cmd+C 复制
5. 将复制的文本粘贴到导入函数中

【方法2：逐条复制】
1. 右键点击单条消息
2. 选择"复制"
3. 粘贴到文本文件，格式：
   2026-05-17 14:30 [我] 消息内容

【支持的格式】
- 2026-05-17 14:30 [我] 消息内容
- [2026/5/17 14:30] 我: 消息内容
- 2026/05/17 14:30 我 消息内容
- 14:30 [我] 消息内容（需指定默认日期）

【使用代码】
    from core.persona.wechat_collector import import_wechat_text
    text = """粘贴你的聊天记录到这里"""
    import_wechat_text(text)

注意：只复制你自己发送的消息（含"我"标识），不复制他人的消息。
'''.strip()

    @staticmethod
    def import_from_file(file_path: str, store: SignalStore = None) -> int:
        """从文件导入微信消息。"""
        path = Path(file_path)
        if not path.exists():
            print(f"[WeChatManualImporter] 文件不存在: {file_path}")
            return 0

        text = path.read_text(encoding="utf-8")
        return WeChatManualImporter.import_from_text(text, store)


# ========== 便捷函数 ==========

def collect_wechat_signals(days: int = 30) -> int:
    """便捷函数：采集微信信号"""
    collector = WeChatCollector()
    print(collector.get_collection_summary())
    count = collector.collect_and_store(days=days)
    print(f"✅ 采集微信信号: {count} 条")
    return count


def import_wechat_text(text: str, default_date: str = None) -> int:
    """便捷函数：从文本导入微信消息"""
    count = WeChatManualImporter.import_from_text(text, default_date=default_date)
    print(f"✅ 导入微信消息: {count} 条")
    return count


def show_wechat_import_guide() -> str:
    """便捷函数：显示手动导入指南"""
    guide = WeChatManualImporter.get_import_guide()
    print(guide)
    return guide


if __name__ == "__main__":
    # 测试发现数据库
    collector = WeChatCollector()
    print(collector.get_collection_summary())
