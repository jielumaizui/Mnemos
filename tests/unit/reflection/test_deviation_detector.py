from dataclasses import asdict
from datetime import datetime, timedelta

from core.reflection.deviation_detector import DeviationDetector, ListeningSession
from core.reflection.models import MirrorSnapshot
from core.reflection.mirror_engine import MirrorResult


def _make_mirror():
    return MirrorResult(
        snapshots=[
            MirrorSnapshot(
                observation_id="obs-1",
                dimension="time",
                value_summary="历史完成周期: 5周",
                evidence_summary="",
                confidence=0.8,
                recency_weight=0.5,
            ),
        ],
        dimensions_involved=["time"],
    )


def _make_omission_mirror():
    return MirrorResult(
        snapshots=[
            MirrorSnapshot(
                observation_id="obs-omission",
                dimension="time",
                value_summary="timeline budget scope",
                evidence_summary="",
                confidence=0.8,
                recency_weight=0.5,
            ),
        ],
        dimensions_involved=["time"],
    )


def test_start_listening_creates_session():
    detector = DeviationDetector()
    mirror = _make_mirror()
    session = detector.start_listening("session-1", "new_project", mirror)

    assert isinstance(session, ListeningSession)
    assert session.session_id == "session-1"
    assert session.trigger_scene == "new_project"
    assert session.mirror is mirror
    assert detector.get_session("session-1") is session


def test_add_user_message_appends_when_no_deviation():
    detector = DeviationDetector()
    mirror = _make_mirror()
    detector.start_listening("session-1", "new_project", mirror)

    signal = detector.add_user_message("session-1", "我正在规划这个项目")
    assert signal is None

    session = detector.get_session("session-1")
    assert session.user_messages == ["我正在规划这个项目"]


def test_close_session_removes_session():
    detector = DeviationDetector()
    detector.start_listening("session-1", "new_project", _make_mirror())
    detector.close_session("session-1")

    assert detector.get_session("session-1") is None


def test_add_user_message_expires_old_session():
    detector = DeviationDetector()
    mirror = _make_mirror()
    session = detector.start_listening("session-1", "new_project", mirror)
    # Manually age the session beyond the default 600s timeout
    session.last_activity_at = datetime.now() - timedelta(seconds=700)

    signal = detector.add_user_message("session-1", "新的消息")
    assert signal is None
    assert detector.get_session("session-1") is None


def test_numeric_deviation_detected_in_session():
    detector = DeviationDetector()
    detector.start_listening("session-1", "new_project", _make_mirror())

    signal = detector.add_user_message("session-1", "预计2周完成")
    assert signal is not None
    assert signal.deviation_type == "numeric"
    assert signal.dimension == "time"
    assert signal.user_claim == "2.0周"
    assert asdict(signal)["user_claim"] == "2.0周"
    assert signal.observed_fact == "历史数据: 5.0周"


def test_omission_threshold_controls_ignored_keyword_ratio():
    detector = DeviationDetector()
    detector.start_listening("session-1", "new_project", _make_omission_mirror())

    assert detector.add_user_message("session-1", "我先整理 timeline") is None
    assert detector.add_user_message("session-1", "再安排人员") is None
    signal = detector.add_user_message("session-1", "最后确定发布")

    assert signal is not None
    assert signal.deviation_type == "omission"
    assert signal.dimension == "time"
