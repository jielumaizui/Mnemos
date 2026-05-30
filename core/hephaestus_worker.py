# Hephaestus Worker — 赫菲斯托斯之工坊
# 蒸馏 Worker — 自动处理 distill_queue，委托 Agent 执行蒸馏

"""
职责：
- 轮询 distill_queue/ 中的待蒸馏任务（默认跟随 claude_data_dir）
- 调用 AgentDelegate 将任务委托给可用 Agent
- 监控结果路径，处理完成的蒸馏输出
- 将结果移入 00-Inbox/，原文件归档

设计原则：同源复用 — 谁启动 Mnemos，蒸馏就交给谁。
"""

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable

from core.config import get_config
from core.prometheus_fire import AgentDelegate, DistillTask
from core.helios import AgentDetector

logger = logging.getLogger(__name__)


class HephaestusWorker:
    """蒸馏 Worker — 火神工坊

    自动处理 distill_queue，将原始对话蒸馏为结构化知识。
    """

    _last_delegate_at: float = 0.0

    def __init__(
        self,
        queue_dir: Path = None,
        output_dir: Path = None,
        inbox_dir: Path = None,
        archive_dir: Path = None,
    ):
        self.delegate = AgentDelegate()
        self.config = get_config()
        self._queue_dir = queue_dir
        self._output_dir = output_dir
        self._inbox_dir = inbox_dir
        self._archive_dir = archive_dir
        self._completed_notified = set()

    @property
    def queue_dir(self) -> Path:
        """蒸馏队列目录"""
        if self._queue_dir:
            return self._queue_dir
        return get_config().claude_data_dir / "distill_queue"

    @property
    def output_dir(self) -> Path:
        """蒸馏输出目录（Agent 将结果写入这里）"""
        if self._output_dir:
            return self._output_dir
        return Path.home() / ".mnemos" / "distill_output"

    @property
    def inbox_dir(self) -> Path:
        """Wiki Inbox 目录"""
        if self._inbox_dir:
            return self._inbox_dir
        return self.config.wiki_dir / "00-Inbox"

    @property
    def archive_dir(self) -> Path:
        """已处理队列文件归档目录"""
        if self._archive_dir:
            return self._archive_dir
        return Path.home() / ".mnemos" / "distill_archive"

    def process_all(self, max_tasks: int = None) -> int:
        """处理队列中所有待蒸馏任务

        Returns:
            处理的任务数量
        """
        if not self.queue_dir.exists():
            logger.debug(f"蒸馏队列不存在: {self.queue_dir}")
            return 0

        # 先检查超时任务，恢复为待处理
        self._recover_expired_delegations()

        if max_tasks is None:
            max_tasks = int(self.config.get("distill.max_tasks_per_cycle", 5) or 5)
        if max_tasks <= 0:
            logger.info("[Hephaestus] 本轮 max_tasks<=0，跳过队列处理")
            return 0

        all_task_files = sorted(self.queue_dir.glob("*.json"))
        task_files = all_task_files[:max_tasks]
        if len(all_task_files) > max_tasks:
            logger.info(
                "[Hephaestus] 队列积压 %d 个任务，本轮限量处理 %d 个",
                len(all_task_files), max_tasks,
            )
        processed = 0

        for task_file in task_files:
            try:
                if self.process_one_file(task_file):
                    processed += 1
            except Exception as e:
                logger.warning(f"处理蒸馏任务失败 {task_file.name}: {e}")
                continue

        return processed

    def _recover_expired_delegations(self, max_age_hours: int = 24):
        """检查已委托但超时的任务，恢复为待处理状态重新委托"""
        import time
        recovered = 0
        for delegated_file in self.queue_dir.glob("*.delegated"):
            try:
                mtime = delegated_file.stat().st_mtime
                age_hours = (time.time() - mtime) / 3600
                if age_hours > max_age_hours:
                    task_file = delegated_file.with_suffix(".json")
                    shutil.move(str(delegated_file), str(task_file))
                    logger.warning(
                        f"任务超时恢复: {delegated_file.name} "
                        f"(已委托 {age_hours:.1f} 小时，超过 {max_age_hours} 小时上限)"
                    )
                    recovered += 1
            except Exception as e:
                logger.debug(f"检查委托超时失败 {delegated_file.name}: {e}")
        if recovered > 0:
            logger.info(f"超时任务恢复: {recovered} 个任务已恢复为待处理")

    def process_one(self, session_id: str) -> bool:
        """处理指定 session_id 的蒸馏任务"""
        task_file = self.queue_dir / f"{session_id}.json"
        if not task_file.exists():
            logger.warning(f"任务不存在: {task_file}")
            return False
        return self.process_one_file(task_file)

    MAX_RETRIES = 3  # 最大重试次数

    def process_one_file(self, task_file: Path) -> bool:
        """处理单个蒸馏任务文件"""
        # 读取任务
        try:
            data = json.loads(task_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取任务文件失败 {task_file}: {e}")
            return False

        session_id = data.get("session_id")
        if not session_id:
            logger.warning(f"任务缺少 session_id: {task_file}")
            return False
        self._emit_progress(session_id, "started", "正在提炼知识...")

        # 检查重试次数
        retry_count = data.get("retry_count", 0)
        if retry_count >= self.MAX_RETRIES:
            logger.error(
                f"任务重试次数耗尽 ({retry_count}/{self.MAX_RETRIES})，标记为失败: {session_id}"
            )
            self._archive_failed_task(task_file, data, "重试次数耗尽，Agent持续不可用")
            return False

        # 检查是否已有输出
        output_path = self.output_dir / f"{session_id}.md"
        if output_path.exists() and output_path.stat().st_size > 100:
            logger.info(f"蒸馏结果已存在，直接处理: {session_id}")
            self._emit_progress(session_id, "extracted", "检测到已完成的蒸馏输出，准备写入 wiki")
            self._move_to_inbox(output_path, session_id, data)
            self._archive_task(task_file)
            return True

        # 构建任务
        task = DistillTask(
            session_id=session_id,
            messages=data.get("messages", []),
            meta=data.get("meta", {}),
        )

        # 委托给 Agent
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._rate_limit_delegate()
        ok = self.delegate.delegate(task, output_path)
        if not ok:
            logger.warning(f"无可用的 Agent 执行蒸馏，任务保留: {session_id}")
            return False

        # 不阻塞等待 Agent 完成（异步模式）
        # 结果将在下次 process_all() 或 daemon 轮询时处理
        logger.info(f"蒸馏任务已委托，等待 Agent 完成: {session_id}")
        self._emit_progress(session_id, "judged", "蒸馏任务已委托给宿主 Agent")

        # 将任务文件标记为"已委托"（重命名），记录重试次数
        delegated_file = task_file.with_suffix(".delegated")
        data["retry_count"] = retry_count + 1
        delegated_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        task_file.unlink()

        return True

    def collect_completed(self, max_files: int = None) -> int:
        """收集已完成的蒸馏结果并移入 Inbox

        Returns:
            收集的任务数量
        """
        if not self.output_dir.exists():
            return 0

        if max_files is None:
            max_files = int(self.config.get("distill.max_collect_per_cycle", 20) or 20)
        if max_files <= 0:
            logger.info("[Hephaestus] 本轮 max_files<=0，跳过结果收集")
            return 0

        collected = 0
        output_files = sorted(self.output_dir.glob("*.md"))
        if len(output_files) > max_files:
            logger.info(
                "[Hephaestus] 完成结果积压 %d 个，本轮限量收集 %d 个",
                len(output_files), max_files,
            )

        for output_file in output_files[:max_files]:
            session_id = output_file.stem

            # 检查对应的任务文件是否存在（.json 或 .delegated）
            task_file = self.queue_dir / f"{session_id}.json"
            delegated_file = self.queue_dir / f"{session_id}.delegated"
            task_data = None

            if task_file.exists():
                try:
                    task_data = json.loads(task_file.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning(f"Unexpected error in hephaestus_worker.py", exc_info=True)
                    pass
            elif delegated_file.exists():
                try:
                    task_data = json.loads(delegated_file.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning(f"Unexpected error in hephaestus_worker.py", exc_info=True)
                    pass

            # 检查输出是否已完成（不是占位符）
            content = output_file.read_text(encoding="utf-8")
            if "MNEMOS_DISTILL_TASK" in content and len(content) < 200:
                continue  # Agent 尚未覆盖占位符

            # 验证输出格式：无效格式不入 Inbox，移入 failed/
            validation = self._validate_distill_output(content)
            if not validation["valid"]:
                logger.warning(
                    f"蒸馏输出格式验证失败 [{session_id[:8]}]: {validation['reason']}"
                )
                self._move_to_failed(output_file, session_id, task_data or {}, validation["reason"])
                if task_file.exists():
                    self._archive_task(task_file)
                if delegated_file.exists():
                    self._archive_task(delegated_file)
                continue
            parsed = self._parse_distill_output(content)
            if parsed and parsed.get("judgment") == "skip":
                self._emit_progress(session_id, "skipped", "本次对话无新知识可提炼")
            elif parsed:
                fragments = parsed.get("fragments") or []
                self._emit_progress(session_id, "extracted", f"已提炼 {len(fragments)} 条知识")

            # 移入 Inbox
            self._move_to_inbox(output_file, session_id, task_data or {})

            # 归档任务文件
            if task_file.exists():
                self._archive_task(task_file)
            if delegated_file.exists():
                self._archive_task(delegated_file)

            collected += 1

        return collected

    def _rate_limit_delegate(self):
        """宿主 Agent 委托限速，避免积压恢复时连续唤起高负载任务。"""
        interval = float(self.config.get("distill.min_task_interval_seconds", 1.0) or 0)
        if interval <= 0:
            return
        now = time.monotonic()
        wait = interval - (now - HephaestusWorker._last_delegate_at)
        if wait > 0:
            time.sleep(wait)
        HephaestusWorker._last_delegate_at = time.monotonic()

    def _emit_progress(self, session_id: str, stage: str, message: str):
        """发送蒸馏进度事件，不影响主流程"""
        if stage == "completed" and session_id in self._completed_notified:
            return
        if stage == "completed":
            self._completed_notified.add(session_id)

        progress_map = {
            "started": 0,
            "judged": 25,
            "extracted": 50,
            "completed": 100,
            "skipped": 0,
        }
        payload = {
            "session_id": session_id,
            "stage": stage,
            "status": stage,
            "progress_pct": progress_map.get(stage, 0),
            "message": message,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            from core.mnemos_bus import publish_event
            publish_event("distillation_progress", "hephaestus_worker", payload)
        except Exception as exc:
            logger.debug("发送蒸馏进度事件失败: %s", exc)

        try:
            from core.kia import amphora
            amphora_step = {
                "started": "extracting",
                "judged": "structuring",
                "extracted": "verifying",
                "completed": "done",
                "skipped": "done",
            }.get(stage)
            if amphora_step:
                amphora.update_progress(session_id, amphora_step, message)
        except Exception:
            logger.warning(f"Unexpected error in hephaestus_worker.py", exc_info=True)
            pass

    def _parse_distill_output(self, content: str) -> Optional[dict]:
        try:
            from core.hephaestus.distillation_engine import extract_json
            parsed = extract_json(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            logger.warning(f"Unexpected error in hephaestus_worker.py", exc_info=True)
            return None

    def _validate_distill_output(self, content: str) -> dict:
        """验证蒸馏输出是否为有效格式

        Returns:
            {"valid": bool, "reason": str}
        """
        # 1. 空内容检查
        if not content or not content.strip():
            return {"valid": False, "reason": "输出为空"}

        # 2. 尝试提取 JSON
        try:
            from core.hephaestus.distillation_engine import extract_json
        except ImportError:
            # 无法导入解析器，跳过严格验证（降级容忍）
            return {"valid": True, "reason": "解析器不可用，跳过严格验证"}

        parsed = extract_json(content)
        if parsed is None:
            return {"valid": False, "reason": "无法从输出中提取有效 JSON"}

        # 3. 检查 judgment 字段
        judgment = parsed.get("judgment")
        if judgment not in ("knowledge", "skill", "skip"):
            return {
                "valid": False,
                "reason": f"judgment 字段无效: '{judgment}' (期望: knowledge/skill/skip)"
            }

        # 4. skip 判定时，允许 fragments 为空
        if judgment == "skip":
            return {"valid": True, "reason": "判定为 skip，无需 fragments"}

        # 5. knowledge 判定时，检查 fragments
        fragments = parsed.get("fragments")
        if not fragments or not isinstance(fragments, list):
            return {
                "valid": False,
                "reason": f"judgment=knowledge 但 fragments 缺失或不是数组"
            }

        # 6. 检查每个 fragment 的必要字段
        for i, frag in enumerate(fragments):
            if not isinstance(frag, dict):
                return {"valid": False, "reason": f"fragment[{i}] 不是对象"}
            if not frag.get("title"):
                return {"valid": False, "reason": f"fragment[{i}] 缺少 title"}
            if not frag.get("form"):
                return {"valid": False, "reason": f"fragment[{i}] 缺少 form"}

        return {"valid": True, "reason": f"验证通过: {judgment}, {len(fragments)} fragments"}

    def _move_to_failed(self, output_path: Path, session_id: str,
                        task_data: dict, reason: str):
        """将格式验证失败的蒸馏输出移入 failed 目录"""
        failed_dir = Path.home() / ".mnemos" / "distill_failed"
        failed_dir.mkdir(parents=True, exist_ok=True)

        ts = task_data.get("meta", {}).get("timestamp", "")
        if not ts:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")

        failed_name = f"{ts}-{session_id[:8]}.md"
        failed_path = failed_dir / failed_name

        raw_content = output_path.read_text(encoding="utf-8")
        meta = task_data.get("meta", {})
        header = f"""---
session_id: {session_id}
source: {meta.get('source', 'unknown')}
failed_at: {ts}
fail_reason: {reason}
tags: [distill-failed]
---

# 蒸馏失败: {reason}

**Session**: {session_id}
**来源**: {meta.get('source', 'unknown')}
**失败原因**: {reason}

---

## 原始输出

```
{raw_content}
```

"""
        failed_path.write_text(header, encoding="utf-8")
        logger.warning(f"无效蒸馏输出已移入 failed: {failed_path}")

        # 删除原始输出文件
        output_path.unlink()

    def _move_to_inbox(self, output_path: Path, session_id: str, task_data: dict):
        """将蒸馏结果移入 Wiki Inbox

        优先尝试解析 Agent 返回的结构化 JSON，生成标准 frontmatter。
        解析失败则回退到原始内容 + 基础 frontmatter。
        """
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        # 读取内容
        raw_content = output_path.read_text(encoding="utf-8")

        # 跳过判定为 skip 的结果，不污染 Inbox
        try:
            from core.hephaestus.distillation_engine import extract_json
            parsed = extract_json(raw_content)
            if parsed and parsed.get("judgment") == "skip":
                logger.info(f"蒸馏判定为 skip，跳过入库: {session_id}")
                output_path.unlink()
                return
        except Exception:
            logger.warning(f"Unexpected error in hephaestus_worker.py", exc_info=True)
            pass

        # 构建 Inbox 文件名
        source = task_data.get("meta", {}).get("source", "unknown")
        ts = task_data.get("meta", {}).get("timestamp", "")
        if not ts:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")

        inbox_name = f"{ts}-{source}-{session_id[:8]}.md"
        inbox_path = self.inbox_dir / inbox_name

        # 尝试解析结构化 JSON 输出
        structured_pages = self._try_parse_structured_output(
            raw_content, session_id, source
        )

        if structured_pages:
            # 一个 session 可能产生多个知识片段，写入多个文件
            for i, page_content in enumerate(structured_pages):
                if i == 0:
                    inbox_path.write_text(page_content, encoding="utf-8")
                    logger.info(f"结构化蒸馏结果已移入 Inbox: {inbox_path}")
                else:
                    extra_name = f"{ts}-{source}-{session_id[:8]}-{i}.md"
                    extra_path = self.inbox_dir / extra_name
                    extra_path.write_text(page_content, encoding="utf-8")
                    logger.info(f"结构化蒸馏结果已移入 Inbox: {extra_path}")
            self._emit_progress(session_id, "completed", f"已写入 wiki：{len(structured_pages)} 个页面")
        else:
            # 回退：原始内容 + 基础 frontmatter
            meta = task_data.get("meta", {})
            fm = f"""---
session_id: {session_id}
source: {meta.get('source', 'unknown')}
working_dir: {meta.get('working_dir', '')}
distilled_at: {ts}
tags: [distilled, {meta.get('source', 'unknown')}]
---

"""
            inbox_path.write_text(fm + raw_content, encoding="utf-8")
            logger.info(f"蒸馏结果已移入 Inbox (原始格式): {inbox_path}")
            self._emit_progress(session_id, "completed", f"已写入 wiki：{inbox_path.name}")

        # 删除输出文件
        output_path.unlink()

    def _try_parse_structured_output(self, raw_content: str, session_id: str,
                                     source: str) -> List[str]:
        """尝试解析 Agent 返回的结构化 JSON，生成标准 frontmatter 页面

        Returns:
            成功时返回 wiki 页面列表，失败返回空列表
        """
        try:
            from core.hephaestus.distillation_engine import (
                extract_json, generate_wiki_page, KnowledgeFragment
            )

            parsed = extract_json(raw_content)
            if not parsed:
                return []

            # 检查是否为 skip
            if parsed.get("judgment") == "skip":
                logger.info(f"Agent 判断为 skip，跳过: {session_id}")
                return []

            fragments = parsed.get("fragments", [])
            if not fragments:
                return []

            pages = []
            for frag in fragments:
                # 构建 KnowledgeFragment
                fragment = KnowledgeFragment(
                    form=frag.get("form", "经验法则"),
                    title=frag.get("title", "未命名"),
                    frontmatter=frag.get("frontmatter", {}),
                    background=frag.get("background", ""),
                    core_content=frag.get("core_content", ""),
                    boundaries=frag.get("boundaries", {}),
                    anti_patterns=frag.get("anti_patterns", []),
                    related_concepts=frag.get("related_concepts", []),
                )
                page = generate_wiki_page(fragment, session_id, source)
                pages.append(page)

            return pages
        except Exception as e:
            logger.debug(f"结构化解析失败: {e}")
            return []

    def _archive_task(self, task_file: Path):
        """归档已处理的任务文件"""
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.archive_dir / task_file.name
        shutil.move(str(task_file), str(archive_path))

    def _archive_failed_task(self, task_file: Path, task_data: dict, reason: str):
        """归档失败的任务文件（重试耗尽等）"""
        failed_dir = self.archive_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        # 更新任务数据记录失败原因
        task_data["failed_at"] = datetime.now().isoformat()
        task_data["fail_reason"] = reason
        failed_path = failed_dir / task_file.name
        failed_path.write_text(
            json.dumps(task_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        task_file.unlink()
        logger.warning(f"任务已归档到 failed: {failed_path}")

    def get_pending_count(self) -> int:
        """获取待处理任务数量"""
        if not self.queue_dir.exists():
            return 0
        return len(list(self.queue_dir.glob("*.json")))

    def get_delegated_count(self) -> int:
        """获取已委托但未完成的任务数量"""
        if not self.queue_dir.exists():
            return 0
        return len(list(self.queue_dir.glob("*.delegated")))

    def get_stats(self) -> dict:
        """获取 Worker 统计信息"""
        return {
            "pending": self.get_pending_count(),
            "delegated": self.get_delegated_count(),
            "queue_dir": str(self.queue_dir),
            "output_dir": str(self.output_dir),
            "inbox_dir": str(self.inbox_dir),
            "archive_dir": str(self.archive_dir),
        }

    def watch_queue(self, interval: float = 60.0, callback: Optional[Callable] = None) -> None:
        """监控 distill_queue 目录，新 .json 文件出现时触发处理

        Args:
            interval: 轮询间隔（秒），默认 60 秒
            callback: 可选回调函数，每次处理完调用
        """
        logger.info(f"[Hephaestus] 开始监控 distill_queue: {self.queue_dir} (间隔 {interval}s)")

        # 尝试使用 watchdog（如果可用）
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class DistillQueueHandler(FileSystemEventHandler):
                def __init__(self, worker: HephaestusWorker, cb: Optional[Callable] = None):
                    self.worker = worker
                    self.callback = cb
                    self._last_process = 0
                    self._debounce_seconds = 5

                def on_created(self, event):
                    if not event.is_directory and event.src_path.endswith(".json"):
                        now = time.time()
                        if now - self._last_process < self._debounce_seconds:
                            return
                        self._last_process = now
                        logger.info(f"[Hephaestus] 检测到新任务: {event.src_path}")
                        self._process()

                def on_modified(self, event):
                    if not event.is_directory and event.src_path.endswith(".json"):
                        now = time.time()
                        if now - self._last_process < self._debounce_seconds:
                            return
                        self._last_process = now
                        self._process()

                def _process(self):
                    try:
                        count = self.worker.process_all()
                        if count > 0 and self.callback:
                            self.callback(count)
                    except Exception as e:
                        logger.error(f"[Hephaestus] watch 处理失败: {e}")

            handler = DistillQueueHandler(self, callback)
            observer = Observer()
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            observer.schedule(handler, str(self.queue_dir), recursive=False)
            observer.start()
            logger.info("[Hephaestus] watchdog 监控已启动")

            try:
                while True:
                    time.sleep(interval)
                    try:
                        # 定期轮询作为后备
                        count = self.process_all()
                        if count > 0:
                            logger.info(f"[Hephaestus] 轮询处理 {count} 个任务")
                            if callback:
                                callback(count)
                    except Exception as e:
                        logger.error(f"[Hephaestus] 轮询处理失败: {e}")
            except KeyboardInterrupt:
                pass
            finally:
                observer.stop()
                observer.join()

        except ImportError:
            # watchdog 不可用，回退到纯轮询
            logger.info("[Hephaestus] watchdog 不可用，回退到轮询模式")
            while True:
                try:
                    count = self.process_all()
                    if count > 0:
                        logger.info(f"[Hephaestus] 轮询处理 {count} 个任务")
                        if callback:
                            callback(count)
                except Exception as e:
                    logger.error(f"[Hephaestus] 轮询处理失败: {e}")
                time.sleep(interval)
