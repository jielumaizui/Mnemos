"""Persisted persona-signal value objects."""

from dataclasses import dataclass
from typing import List


@dataclass
class SessionSignal:
    """AI对话session信号"""

    session_id: str
    timestamp: str
    task_type: str = ""
    task_subtype: str = ""
    user_msg_count: int = 0
    avg_user_msg_length: float = 0
    provided_context_richness: float = 0
    correction_count: int = 0
    correction_domains: List[str] = None  # type: ignore[assignment]
    follow_up_depth: int = 0
    options_presented: int = 0
    option_selected: int = 0
    selection_rationale: str = ""
    termination_type: str = ""
    final_feedback: str = ""
    output_type: str = ""
    output_file_count: int = 0
    duration_seconds: int = 0
    working_dir: str = ""
    agent: str = "claude"
    context_tags: List[str] = None  # type: ignore[assignment]


@dataclass
class GitSignal:
    """Git行为信号"""

    repo_path: str
    commit_hash: str
    timestamp: str
    message_length: int = 0  # noqa: Vulture - persisted git_signals.message_length contract consumed by Pythia deduction scoring.
    has_issue_reference: bool = False  # noqa: Vulture - persisted git_signals collaboration reference contract.
    has_pr_reference: bool = False  # noqa: Vulture - persisted git_signals collaboration reference contract.
    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    test_files_changed: int = 0
    commit_type: str = ""
    is_weekend: bool = False  # noqa: Vulture - persisted git_signals recovery-cycle contract consumed by Pythia.
    hour_of_day: int = 0


@dataclass
class NoteSignal:
    """笔记信号"""

    timestamp: str
    content_length: int = 0  # noqa: Vulture - persisted note_signals.content_length contract.
    has_title: bool = False  # noqa: Vulture - persisted note_signals structure metadata.
    has_list: bool = False
    has_code_block: bool = False
    has_link: bool = False  # noqa: Vulture - persisted note_signals structure metadata.
    image_count: int = 0  # noqa: Vulture - persisted note_signals structure metadata.
    tag_count: int = 0  # noqa: Vulture - persisted note_signals structure metadata.
    tags_json: str = ""
    is_ai_generated: bool = False
    ai_agent: str = ""  # noqa: Vulture - persisted note_signals.ai_agent contract.
    note_uid: str = ""


@dataclass
class WechatSignal:
    """微信聊天信号"""

    timestamp: str
    content_hash: str = ""
    msg_length: int = 0  # noqa: Vulture - persisted wechat_signals message metadata.
    has_sensitive_content: bool = False  # noqa: Vulture - persisted wechat_signals message metadata.
    emotional_valence: float = 0.0
    emotional_arousal: float = 0.0
    topic_tags: List[str] = None  # type: ignore[assignment]  # noqa: Vulture - persisted wechat_signals message metadata.
    chat_type: str = "unknown"
    hour_of_day: int = 0
    day_of_week: int = 0
    msg_sequence_in_day: int = 0  # noqa: Vulture - persisted wechat_signals message metadata.
