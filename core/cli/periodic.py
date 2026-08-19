"""Bounded periodic loop helpers for CLI watch modes."""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from threading import Event
from typing import Callable


@dataclass(frozen=True)
class PeriodicLoopResult:
    cycles: int
    failures: int
    stopped_reason: str


def add_periodic_loop_args(
    parser: argparse.ArgumentParser,
    *,
    default_interval: float,
) -> None:
    parser.add_argument("--once", action="store_true", help="watch 模式只执行一轮后退出")
    parser.add_argument("--max-cycles", type=int, default=None, help="watch 模式最多执行 N 轮")
    parser.add_argument("--run-seconds", type=float, default=None, help="watch 模式最多运行 N 秒")
    parser.add_argument(
        "--interval",
        type=float,
        default=default_interval,
        help=f"watch 模式每轮间隔秒数（默认 {default_interval:g}）",
    )


def resolve_max_cycles(*, once: bool, max_cycles: int | None) -> int | None:
    if once:
        return 1
    return max_cycles


def run_periodic_loop(
    callback: Callable[[], object],
    *,
    interval: float,
    stop_event: Event | None = None,
    max_cycles: int | None = None,
    run_seconds: float | None = None,
    max_consecutive_failures: int = 1,
    on_error: Callable[[BaseException], object] | None = None,
    sleep_fn: Callable[[float], object] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> PeriodicLoopResult:
    if interval < 0:
        raise ValueError("interval must be non-negative")
    if max_cycles is not None and max_cycles < 0:
        raise ValueError("max_cycles must be non-negative")
    if run_seconds is not None and run_seconds < 0:
        raise ValueError("run_seconds must be non-negative")
    if max_consecutive_failures < 1:
        raise ValueError("max_consecutive_failures must be at least 1")

    cycles = 0
    consecutive_failures = 0
    deadline = None if run_seconds is None else monotonic_fn() + run_seconds

    while True:
        if stop_event is not None and stop_event.is_set():
            return PeriodicLoopResult(cycles, consecutive_failures, "stop_event")
        if max_cycles is not None and cycles >= max_cycles:
            return PeriodicLoopResult(cycles, consecutive_failures, "max_cycles")
        if deadline is not None and monotonic_fn() >= deadline:
            return PeriodicLoopResult(cycles, consecutive_failures, "run_seconds")

        try:
            callback()
            consecutive_failures = 0
        except BaseException as exc:
            consecutive_failures += 1
            if on_error is not None:
                on_error(exc)
            if consecutive_failures >= max_consecutive_failures:
                raise
        cycles += 1

        if stop_event is not None and stop_event.is_set():
            return PeriodicLoopResult(cycles, consecutive_failures, "stop_event")
        if max_cycles is not None and cycles >= max_cycles:
            return PeriodicLoopResult(cycles, consecutive_failures, "max_cycles")

        sleep_for = interval
        if deadline is not None:
            remaining = deadline - monotonic_fn()
            if remaining <= 0:
                return PeriodicLoopResult(cycles, consecutive_failures, "run_seconds")
            sleep_for = min(sleep_for, remaining)

        if stop_event is not None:
            if stop_event.wait(sleep_for):
                return PeriodicLoopResult(cycles, consecutive_failures, "stop_event")
        else:
            sleep_fn(sleep_for)
