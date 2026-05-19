# -*- coding: utf-8 -*-
"""
Task ID Parser - Task ID 自动解析模块

解析用户消息中的任务触发词，自动生成 task-id 标签。
纯文本处理，无 SQLite 依赖。

迁移自: memos-client/task_id_parser.py
改造点:
- 添加 from __future__ import annotations
- 确保正则表达式中的中文在 UTF-8 下正确
- 无外部依赖，纯文本处理
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, List


class TaskIdParser:
    """Task ID 解析器"""

    # 触发词列表
    TASK_TRIGGERS: List[str] = [
        "任务：", "任务:", "任务： ", "任务: ",
        "执行关于",
        "task:", "task：", "task-",
        "#任务",
        "【任务】",
    ]

    # 关键词提取正则
    # 注意：r 前缀确保 Python 以原始字符串处理，中文字符在 UTF-8 源码中自动正确
    KEYWORD_PATTERNS: List[str] = [
        # "任务：xxx" / "任务: xxx"
        r'任务[：:]\s*(.+?)(?:[，。；,;]|$)',
        r'task[：:-]?\s*(.+?)(?:[，。；,;]|$)',
        # "执行关于xxx的任务"
        r'执行关于(.+?的?)(?:任务|操作|处理)',
        r'#任务\s*(.+?)(?:[，。；,;]|$)',
        r'【任务】\s*(.+?)(?:[，。；,;]|$)',
    ]

    @classmethod
    def parse(cls, user_message: str) -> Optional[str]:
        """
        解析用户消息，提取任务 ID

        Args:
            user_message: 用户输入的消息

        Returns:
            task-id 字符串或 None
        """
        # 检查是否包含触发词
        has_trigger = any(trigger in user_message for trigger in cls.TASK_TRIGGERS)

        if not has_trigger:
            # 没有触发词，使用默认任务
            return cls._generate_default_task_id()

        # 尝试提取关键词
        keyword = cls._extract_keyword(user_message)

        if keyword:
            return cls._generate_task_id(keyword)

        # 有触发词但无法提取关键词，使用默认
        return cls._generate_default_task_id()

    @classmethod
    def _extract_keyword(cls, user_message: str) -> Optional[str]:
        """提取任务关键词"""
        # 按优先级尝试匹配
        for pattern in cls.KEYWORD_PATTERNS:
            match = re.search(pattern, user_message, re.IGNORECASE | re.UNICODE)
            if match:
                keyword = match.group(1).strip()
                # 清理关键词
                keyword = cls._clean_keyword(keyword)
                if keyword:
                    return keyword
        return None

    @classmethod
    def _clean_keyword(cls, keyword: str) -> str:
        """清理关键词，生成合法 slug"""
        # 移除常见无意义词
        remove_words = ['的', '任务', '操作', '处理', '关于', '一下', '一个']
        for word in remove_words:
            keyword = keyword.replace(word, '')

        # 转小写，替换空格和特殊字符
        keyword = keyword.lower().strip()
        keyword = re.sub(r'[\s]+', '-', keyword)  # 空格换 -
        # 只保留中文、英文、数字、-（\u4e00-\u9fa5 匹配 CJK 统一汉字）
        keyword = re.sub(r'[^a-z0-9\u4e00-\u9fa5-]', '', keyword)
        keyword = re.sub(r'-+', '-', keyword)  # 多个 - 合并
        keyword = keyword.strip('-')

        return keyword[:30]  # 限制长度

    @classmethod
    def _generate_task_id(cls, keyword: str) -> str:
        """生成 task-id 标签（UTC 日期）"""
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        return f"task:{today}-{keyword}"

    @classmethod
    def _generate_default_task_id(cls) -> str:
        """生成默认 task-id（UTC 日期 + 时间）"""
        now = datetime.now(timezone.utc)
        today = now.strftime('%Y%m%d')
        unique = now.strftime('%H%M')
        return f"task:daily-{today}-{unique}"

    @classmethod
    def is_private_request(cls, user_message: str) -> bool:
        """
        检测用户是否要求私有标记

        关键词：私有、保密、private、personal、不要共享
        """
        private_keywords = [
            "私有", "保密", "私密", "隐私",
            "private", "personal", "confidential",
            "不要共享", "不要分享", "仅自己可见", "仅自己",
        ]

        message_lower = user_message.lower()
        return any(kw in message_lower or kw in user_message for kw in private_keywords)


class TagBuilder:
    """五维标签构建器"""

    @staticmethod
    def build_tags(
        source: str,      # 来源: claude/hermes/openclaw/human-local
        model: str,       # 模型名
        task_id: str,     # 任务 ID
        scope: str = "public",  # 范围: public/private
        date: str = None  # 日期 (YYYYMMDD)
    ) -> List[str]:
        """
        构建完整的五维标签

        五维标签：
        1. source:{claude|hermes|openclaw|human-local}
        2. time:{YYYYMMDD}
        3. model:{model_name}
        4. scope:{public|private}
        5. task:{YYYYMMDD-slug}
        """
        if date is None:
            date = datetime.now(timezone.utc).strftime('%Y%m%d')

        tags = [
            f"source={source}",
            f"time={date}",
            f"model={model}",
            f"scope={scope}",
        ]

        if task_id:
            tags.append(task_id)

        return tags

    @staticmethod
    def parse_tags(tag_string: str) -> dict:
        """从标签字符串解析五维标签（兼容 = 和 : 分隔符）"""
        tags = {}
        for tag in tag_string.split(','):
            tag = tag.strip()
            # 支持 key=value 和 key:value 两种格式
            for sep in ('=', ':'):
                if sep in tag:
                    key, value = tag.split(sep, 1)
                    tags[key] = value
                    break
            else:
                # 无分隔符的裸标签（如 task:xxx 格式整体）
                if tag.startswith('task:'):
                    tags['task_id'] = tag
        return tags
