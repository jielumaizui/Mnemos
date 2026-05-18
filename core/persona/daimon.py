"""
Signal Collector - 统一信号采集接口

职责：
- 聚合所有数据源的用户行为信号
- 提供统一的采集、预处理和存储接口
- 支持增量采集（只采新数据）

数据源：
1. AI聊天记录（distill_queue / wiki_state.db / session历史）
2. 知识库交互（wiki文件访问/修改）
3. Git历史（commit message / 频率 / 风格）
4. 文件系统行为（创建/修改/组织模式）
5. Obsidian图谱（链接密度 / 枢纽节点）
6. 微信聊天记录（wechat_collector.py 提供）
"""
# Daimon — 守护灵 — 信号采集器，伴随用户收集行为
# 原模块: signal_collector.py



import os
import re
import sys
import json
import sqlite3
import subprocess
import logging
from pathlib import Path
from typing import Dict, List, Optional, Iterator, Any
from datetime import datetime, timedelta
from collections import Counter

from core.config import get_config
from .psyche import (
    SignalStore, get_signal_store,
    SessionSignal, GitSignal, MemosSignal, log_session_signal,
)

logger = logging.getLogger(__name__)


class SignalCollector:
    """统一信号采集器"""

    def __init__(self, store: SignalStore = None):
        self.store = store or get_signal_store()

    def _wiki_dir(self) -> Path:
        from core.config import get_config
        return get_config().wiki_dir

    def _wiki_state_db(self) -> Path:
        from core.config import get_config
        return get_config().claude_data_dir / "wiki_state.db"

    def _distill_queue_dir(self) -> Path:
        from core.config import get_config
        return get_config().claude_data_dir / "distill_queue"

    # ============================================================
    # 1. AI Session 信号采集
    # ============================================================

    def collect_from_distill_queue(self) -> int:
        """
        从 distill_queue 采集 session 信号。
        读取已完成的 session JSON 文件，提取行为信号。
        """
        count = 0
        if not self._distill_queue_dir().exists():
            return count

        for json_file in self._distill_queue_dir().glob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                signal = self._parse_session_to_signal(data)
                if signal:
                    # 去重：按 session_id 检查
                    if self.store.session_exists(signal.session_id):
                        continue
                    self.store.insert_session_signal(signal)
                    count += 1
            except Exception:
                continue

        return count

    def collect_from_wiki_state(self) -> int:
        """
        从 wiki_state.db 采集已处理的 session 元数据。
        """
        count = 0
        if not self._wiki_state_db().exists():
            return count

        try:
            with sqlite3.connect(str(self._wiki_state_db())) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("""
                    SELECT * FROM processed_sessions
                    WHERE processed_at >= date('now', '-30 days')
                    ORDER BY processed_at DESC
                """)
                for row in cursor.fetchall():
                    data = dict(row)
                    # 尝试重构 session 结构
                    signal = self._parse_wiki_state_to_signal(data)
                    if signal:
                        # 去重：按 session_id 检查
                        if self.store.session_exists(signal.session_id):
                            continue
                        self.store.insert_session_signal(signal)
                        count += 1
        except Exception as e:
            logger.warning(f"忽略异常: {e}")

        return count

    def _parse_session_to_signal(self, data: Dict) -> Optional[SessionSignal]:
        """解析 session 数据为 SessionSignal"""
        messages = data.get("messages", [])
        if not messages:
            return None

        user_messages = [m for m in messages if m.get("role") == "user"]
        assistant_messages = [m for m in messages if m.get("role") == "assistant"]

        if not user_messages:
            return None

        # 基本统计
        user_contents = [m.get("content", "") for m in user_messages]
        avg_length = sum(len(c) for c in user_contents) / max(len(user_contents), 1)

        # 纠正检测：用户消息中包含否定/修正信号
        correction_keywords = ["不对", "错了", "不是", "应该", "换个", "不对，"]
        correction_count = sum(
            1 for c in user_contents
            if any(kw in c for kw in correction_keywords)
        )

        # 追问深度：连续用户消息数（assistant回复后用户继续问）
        follow_up_depth = self._calculate_follow_up_depth(messages)

        # 终止类型推断
        termination_type = self._infer_termination_type(messages)

        # 产出类型
        output_type = self._infer_output_type(messages, data)

        # 时间
        created_at = data.get("created_at", datetime.now().isoformat())

        return SessionSignal(
            session_id=data.get("session_id", ""),
            timestamp=created_at,
            task_type=data.get("task_type", ""),
            task_subtype=data.get("task_subtype", ""),
            user_msg_count=len(user_messages),
            avg_user_msg_length=avg_length,
            correction_count=correction_count,
            follow_up_depth=follow_up_depth,
            termination_type=termination_type,
            output_type=output_type,
            working_dir=data.get("working_dir", ""),
            agent=data.get("agent", "claude"),
        )

    def _parse_wiki_state_to_signal(self, data: Dict) -> Optional[SessionSignal]:
        """从 wiki_state 记录解析信号"""
        # wiki_state.db 的字段可能不同，做适配
        return SessionSignal(
            session_id=data.get("session_id", data.get("id", "")),
            timestamp=data.get("processed_at", datetime.now().isoformat()),
            task_type=data.get("task_type", ""),
            working_dir=data.get("working_dir", ""),
        )

    def _calculate_follow_up_depth(self, messages: List[Dict]) -> int:
        """计算追问深度"""
        depth = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user" and i > 0:
                # 检查是否是回复assistant后的追问
                prev_msgs = messages[:i]
                assistant_msgs = [m for m in prev_msgs if m.get("role") == "assistant"]
                if assistant_msgs:
                    depth += 1
        return depth

    def _infer_termination_type(self, messages: List[Dict]) -> str:
        """推断终止类型"""
        if not messages:
            return ""

        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user_msg = m.get("content", "")
                break

        content = last_user_msg.lower()

        # 满意终止
        if any(kw in content for kw in ["好的", "完美", "可以", "ok", "谢谢", "先这样", "搞定了"]):
            return "satisfied"

        # 推进终止
        if any(kw in content for kw in ["开始吧", "执行", "推进", "下一步", "继续"]):
            return "progress"

        # 委托终止
        if any(kw in content for kw in ["你决定", "你来", "按你的"]):
            return "delegated"

        # 放弃终止
        if any(kw in content for kw in ["算了", "放弃", "不做了", "先这样吧"]):
            return "abandoned"

        return "unknown"

    def _infer_output_type(self, messages: List[Dict], data: Dict) -> str:
        """推断产出类型"""
        all_content = " ".join(m.get("content", "") for m in messages)

        if "```" in all_content or "def " in all_content or "class " in all_content:
            return "code"
        if "# " in all_content and len(all_content) > 500:
            return "document"
        if any(kw in all_content for kw in ["选", "方案", "决定", "用哪个"]):
            return "decision"

        return "discussion"

    # ============================================================
    # 2. Git 信号采集
    # ============================================================

    def collect_from_git(self, repo_paths: List[str] = None) -> int:
        """
        从 Git 仓库采集行为信号。
        默认扫描 ~/projects 和当前工作目录下的 git 仓库。
        """
        count = 0

        if repo_paths is None:
            repo_paths = self._discover_git_repos()

        for repo_path in repo_paths:
            try:
                count += self._collect_from_single_repo(Path(repo_path))
            except Exception:
                continue

        return count

    def _discover_git_repos(self) -> List[str]:
        """发现本地 Git 仓库（跨平台）"""
        repos = []
        search_paths = [
            Path.home() / "projects",
            Path.home() / "workspace",
            Path.home() / "dev",
            Path.cwd(),
        ]

        # Windows 常用路径
        if sys.platform == "win32":
            search_paths.extend([
                Path.home() / "Documents" / "GitHub",
                Path.home() / "source" / "repos",
                Path.home() / "Documents" / "workspace",
            ])

        for base in search_paths:
            if not base.exists():
                continue
            for git_dir in base.rglob(".git"):
                repos.append(str(git_dir.parent))

        return repos

    def _collect_from_single_repo(self, repo_path: Path) -> int:
        """从单个仓库采集"""
        count = 0

        # 获取最近30天的 commit
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        try:
            result = subprocess.run(
                [
                    "git", "-C", str(repo_path),
                    "log", f"--since={since}",
                    "--format=%H|%ci|%s",
                    "--shortstat",
                ],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return 0

            commits = self._parse_git_log(result.stdout)
            for commit in commits:
                # 去重：按 commit_hash 检查
                if self.store.git_commit_exists(commit["hash"]):
                    continue
                signal = GitSignal(
                    repo_path=str(repo_path),
                    commit_hash=commit["hash"],
                    timestamp=commit["timestamp"],
                    message_length=len(commit["message"]),
                    has_issue_reference="#" in commit["message"],
                    has_pr_reference="PR" in commit["message"].upper() or "pull" in commit["message"].lower(),
                    files_changed=commit.get("files_changed", 0),
                    lines_added=commit.get("lines_added", 0),
                    lines_deleted=commit.get("lines_deleted", 0),
                    test_files_changed=commit.get("test_files_changed", 0),
                    commit_type=self._infer_commit_type(commit["message"]),
                    is_weekend=datetime.fromisoformat(commit["timestamp"].replace("Z", "+00:00")).weekday() >= 5,
                    hour_of_day=datetime.fromisoformat(commit["timestamp"].replace("Z", "+00:00")).hour,
                )
                self.store.insert_git_signal(signal)
                count += 1

        except Exception as e:
            logger.warning(f"忽略异常: {e}")

        return count

    def _parse_git_log(self, log_output: str) -> List[Dict]:
        """解析 git log 输出"""
        commits = []
        lines = log_output.strip().split("\n")

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if "|" in line:
                parts = line.split("|", 2)
                if len(parts) >= 3:
                    commit = {
                        "hash": parts[0],
                        "timestamp": parts[1],
                        "message": parts[2],
                    }
                    # 下一行可能是 shortstat
                    if i + 1 < len(lines) and "changed" in lines[i + 1]:
                        stat = self._parse_git_stat(lines[i + 1])
                        commit.update(stat)
                        i += 1
                    commits.append(commit)
            i += 1

        return commits

    def _parse_git_stat(self, stat_line: str) -> Dict:
        """解析 git shortstat 行"""
        result = {"files_changed": 0, "lines_added": 0, "lines_deleted": 0, "test_files_changed": 0}

        # e.g. "3 files changed, 50 insertions(+), 10 deletions(-)"
        files_match = re.search(r'(\d+) file', stat_line)
        if files_match:
            result["files_changed"] = int(files_match.group(1))

        insertions_match = re.search(r'(\d+) insertion', stat_line)
        if insertions_match:
            result["lines_added"] = int(insertions_match.group(1))

        deletions_match = re.search(r'(\d+) deletion', stat_line)
        if deletions_match:
            result["lines_deleted"] = int(deletions_match.group(1))

        # 简单检测是否含 test 文件（从上下文推断，不准确）
        if "test" in stat_line.lower():
            result["test_files_changed"] = 1

        return result

    def _infer_commit_type(self, message: str) -> str:
        """推断 commit 类型"""
        msg_lower = message.lower()

        patterns = [
            (r'^feat|^feature|^add', 'feat'),
            (r'^fix|^bugfix|^hotfix', 'fix'),
            (r'^docs|^doc', 'docs'),
            (r'^refactor|^restructure', 'refactor'),
            (r'^test|^spec', 'test'),
            (r'^chore|^ci|^build|^deps', 'chore'),
            (r'^style|^format|^lint', 'style'),
        ]

        for pattern, ctype in patterns:
            if re.search(pattern, msg_lower):
                return ctype

        return "other"

    # ============================================================
    # 3. 知识库交互信号采集
    # ============================================================

    def collect_from_wiki(self) -> int:
        """
        从 Wiki 目录采集知识库交互信号。
        目前通过文件mtime推断访问/修改模式。
        """
        count = 0
        if not self._wiki_dir().exists():
            return count

        cutoff = datetime.now() - timedelta(days=30)

        for md_file in self._wiki_dir().rglob("*.md"):
            try:
                stat = md_file.stat()
                mtime = datetime.fromtimestamp(stat.st_mtime)

                if mtime < cutoff:
                    continue

                # 读取 frontmatter 获取标签等信息
                tags_added = []
                tags_removed = []
                try:
                    content = md_file.read_text(encoding="utf-8", errors="ignore")
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            try:
                                import yaml
                                fm = yaml.safe_load(parts[1]) or {}
                                tags_added = fm.get("tags", [])
                            except Exception as e:
                                logger.warning(f"忽略异常: {e}")
                except Exception as e:
                    logger.warning(f"忽略异常: {e}")

                page_rel = str(md_file.relative_to(self._wiki_dir()))
                # 去重：按 page_path 检查（7天内已存在则跳过）
                if self.store.knowledge_page_exists(page_rel, since=cutoff.isoformat()):
                    continue

                # 写入知识信号（通过 store 方法，确保 signal_metadata 同步创建）
                self.store.insert_knowledge_signal(
                    page_path=page_rel,
                    action_type="modify" if mtime > cutoff else "access",
                    timestamp=mtime.isoformat(),
                    tags_added=json.dumps(tags_added, ensure_ascii=False),
                    tags_removed=json.dumps(tags_removed, ensure_ascii=False),
                )
                count += 1

            except Exception:
                continue

        return count

    # ============================================================
    # 4. 文件系统信号采集
    # ============================================================

    def collect_from_file_system(self, watch_paths: List[str] = None) -> int:
        """
        从文件系统采集行为信号。
        扫描指定目录，分析文件创建/修改模式。
        """
        count = 0

        if watch_paths is None:
            watch_paths = [
                str(Path.home() / "projects"),
                str(Path.home() / "workspace"),
                str(Path.cwd()),
            ]

        cutoff = datetime.now() - timedelta(days=7)

        for path_str in watch_paths:
            path = Path(path_str)
            if not path.exists():
                continue

            for file_path in path.rglob("*"):
                if not file_path.is_file():
                    continue

                try:
                    stat = file_path.stat()
                    mtime = datetime.fromtimestamp(stat.st_mtime)
                    ctime = datetime.fromtimestamp(stat.st_ctime)

                    # 只采集最近有活动的
                    if mtime < cutoff and ctime < cutoff:
                        continue

                    # 排除系统文件
                    if file_path.name.startswith("."):
                        continue

                    action = "create" if ctime > cutoff and abs((ctime - mtime).total_seconds()) < 60 else "modify"

                    # 计算目录深度
                    try:
                        rel_path = file_path.relative_to(path)
                        depth = len(rel_path.parts) - 1
                    except ValueError:
                        depth = 0

                    # 检测是否在 inbox/下载目录
                    is_inbox = any(kw in str(file_path).lower() for kw in ["download", "desktop", "inbox", "tmp"])

                    # 检测是否在 git 中
                    is_versioned = (file_path.parent / ".git").exists() or any(
                        (file_path.parents[i] / ".git").exists()
                        for i in range(min(5, len(file_path.parents)))
                    )

                    # 去重：按 file_path 检查（7天内已存在则跳过）
                    if self.store.file_system_exists(str(file_path), since=cutoff.isoformat()):
                        continue

                    # 写入文件系统信号（通过 store 方法，确保 signal_metadata 同步创建）
                    self.store.insert_file_system_signal(
                        file_path=str(file_path),
                        action_type=action,
                        timestamp=mtime.isoformat(),
                        file_extension=file_path.suffix,
                        directory_depth=depth,
                        project_name=path.name,
                        is_in_inbox=int(is_inbox),
                        is_versioned=int(is_versioned),
                    )
                    count += 1

                except Exception:
                    continue

        return count

    # ============================================================
    # 5. Memos 笔记信号采集
    # ============================================================

    def collect_from_memos(self, max_records: int = None) -> int:
        """
        从 Memos 采集笔记信号。
        过滤 AI 生成的内容，只保留用户原创笔记。
        """
        try:
            from memos_sdk import MemosClient
        except ImportError:
            return 0

        client = MemosClient()
        all_memos = client.list_all_memos(max_records=max_records)

        # AI 生成标记（用于过滤）
        ai_indicators = {
            "agent=claude", "agent=kimi", "agent=gpt", "agent=hermes",
            "type=session-delta", "type=clean-refined",
            "source=claude", "source=hermes", "source=kimi",
            "type=chunk",
        }

        count = 0
        for memo in all_memos:
            tags = set(memo.get("tags", []))
            content = memo.get("content", "")

            # 跳过 AI 生成的内容
            if tags & ai_indicators:
                continue
            # 跳过空内容
            if not content or not content.strip():
                continue
            # 跳过纯 JSON 元数据（hermes chunk 等）
            if content.strip().startswith('{"_meta"') or content.strip().startswith('{"session_id"'):
                continue

            # 提取内容特征
            content_len = len(content)
            has_title = bool(re.search(r'^#+\s', content, re.MULTILINE))
            has_list = bool(re.search(r'(^|\n)[\s]*[-*+\d]+[.)\s]', content))
            has_code = bool(re.search(r'```', content))
            has_link = bool(re.search(r'\[.*?\]\(.*?\)', content))
            img_count = len(re.findall(r'!\[.*?\]\(.*?\)', content))

            # 解析时间 (Memos API 使用 createTime)
            created = memo.get("createTime", "")
            if not created:
                continue

            memo_uid = memo.get("name", "")

            # 去重：按 memo_uid 检查
            if memo_uid and self.store.memos_exists(memo_uid):
                continue

            # 构建信号
            signal = MemosSignal(
                timestamp=created,
                content_length=content_len,
                has_title=has_title,
                has_list=has_list,
                has_code_block=has_code,
                has_link=has_link,
                image_count=img_count,
                tag_count=len(tags),
                tags_json=json.dumps(list(tags), ensure_ascii=False),
                is_ai_generated=False,
                ai_agent="",
                memo_uid=memo_uid,
            )

            self.store.insert_memos_signal(signal)
            count += 1

        return count

    # ============================================================
    # 6. 统一采集入口
    # ============================================================

    def collect_all(self, sources: List[str] = None) -> Dict[str, int]:
        """
        统一采集入口。

        Args:
            sources: 指定采集哪些源，默认按配置自动选择

        Returns:
            各源采集数量统计
        """
        all_sources = {
            "session": self.collect_from_distill_queue,
            "wiki_state": self.collect_from_wiki_state,
            "git": self.collect_from_git,
            "wiki": self.collect_from_wiki,
            "file_system": self.collect_from_file_system,
            "memos": self.collect_from_memos,
            # "wechat": wechat_collector.collect,  # 由 Task #81 提供
        }

        if sources is None:
            # 按配置自动选择数据源
            sources = self._resolve_sources_from_config()

        active_sources = {k: v for k, v in all_sources.items() if k in sources}

        results = {}
        for name, collector in active_sources.items():
            try:
                count = collector()
                results[name] = count
            except Exception:
                results[name] = -1  # 错误标记

        return results

    def _resolve_sources_from_config(self) -> List[str]:
        """根据配置解析启用的数据源"""
        config = get_config()
        if not config.persona_enabled:
            return ["session"]  # 画像系统关闭时至少保留 session

        enabled = []
        ds = config.persona_data_sources
        if ds.get("session", {}).get("enabled", True):
            enabled.extend(["session", "wiki_state"])
        if ds.get("git", {}).get("enabled", False):
            enabled.append("git")
        if ds.get("memos", {}).get("enabled", False):
            enabled.append("memos")
        if ds.get("wiki", {}).get("enabled", False):
            enabled.append("wiki")
        if ds.get("file_system", {}).get("enabled", False):
            enabled.append("file_system")
        # wechat 暂不支持自动采集
        return enabled or ["session"]

    def get_collection_summary(self) -> str:
        """获取采集摘要"""
        stats = self.store.get_signal_stats(days=30)
        lines = ["📡 信号采集摘要（最近30天）"]
        for source, count in stats.items():
            lines.append(f"  {source}: {count} 条信号")
        total = sum(v for v in stats.values() if v > 0)
        lines.append(f"  总计: {total} 条")
        return "\n".join(lines)


# ========== 便捷函数 ==========

def collect_all_signals(sources: List[str] = None) -> Dict[str, int]:
    """便捷函数：采集所有信号"""
    collector = SignalCollector()
    return collector.collect_all(sources)


def collect_and_log():
    """采集并输出日志"""
    collector = SignalCollector()
    results = collector.collect_all()
    print(collector.get_collection_summary())
    return results


if __name__ == "__main__":
    collect_and_log()
