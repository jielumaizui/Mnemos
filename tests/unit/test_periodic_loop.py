import threading

import pytest

from core.cli.periodic import run_periodic_loop


def test_periodic_loop_stops_after_max_cycles():
    calls = []
    sleeps = []

    result = run_periodic_loop(
        lambda: calls.append("tick"),
        interval=5,
        max_cycles=3,
        sleep_fn=sleeps.append,
    )

    assert calls == ["tick", "tick", "tick"]
    assert sleeps == [5, 5]
    assert result.cycles == 3
    assert result.stopped_reason == "max_cycles"


def test_periodic_loop_respects_stop_event_before_sleep():
    stop_event = threading.Event()
    calls = []

    def callback():
        calls.append("tick")
        stop_event.set()

    result = run_periodic_loop(
        callback,
        interval=5,
        stop_event=stop_event,
        sleep_fn=lambda _: pytest.fail("loop should not sleep after stop_event"),
    )

    assert calls == ["tick"]
    assert result.stopped_reason == "stop_event"


def test_periodic_loop_raises_after_consecutive_failures():
    errors = []

    with pytest.raises(RuntimeError, match="boom"):
        run_periodic_loop(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            interval=0,
            max_consecutive_failures=2,
            on_error=errors.append,
            sleep_fn=lambda _: None,
        )

    assert len(errors) == 2
