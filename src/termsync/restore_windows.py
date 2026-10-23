"""Fresh destinations and bounded display readiness for one restore invocation.

Titles prove display order/count only. HWNDs never escape this invocation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import Protocol
from uuid import uuid4

from termsync.capture_windows import DesktopObservation, run_desktop
from termsync.rows import Row
from termsync.titles import normalize_title

APPEND_TIMEOUT_S = 20.0
POLL_S = 0.25
Observer = Callable[[float], DesktopObservation]


class Launcher(Protocol):
    def poll(self) -> int | None: ...


@dataclass(frozen=True)
class Destination:
    ordinal: int
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class LaunchResult:
    launched: tuple[Row, ...] = ()
    ready: tuple[Row, ...] = ()
    failures: tuple[tuple[Row, str], ...] = ()
    not_attempted: tuple[Row, ...] = ()


def plan_windows(rows: list[Row]) -> tuple[Destination, ...]:
    """Dense destinations after preflight, without changing source placement."""
    known: dict[int, list[Row]] = {}
    unknown = []
    for row in rows:
        if (row.agent == "claude" and not row.armed and not row.young
                and type(row.window_ordinal) is int and row.window_ordinal > 0
                and type(row.tab_index) is int and row.tab_index >= 0):
            known.setdefault(row.window_ordinal, []).append(row)
        else:
            unknown.append(row)
    groups = [sorted(known[key], key=lambda r: r.tab_index) for key in sorted(known)]
    if unknown:
        groups.append(unknown)
    return tuple(Destination(i, tuple(group)) for i, group in enumerate(groups, 1))


def _read(observe: Observer, timeout: float) -> dict[int, list[str]]:
    observation = observe(timeout)
    if observation.error or observation.windows_seen is None:
        raise RuntimeError(observation.error or "Desktop count unavailable")
    windows = {w["hwnd"]: [t["name"] for t in w["tabs"]] for w in observation.windows}
    if len(windows) != observation.windows_seen or len(windows) != len(observation.windows):
        raise RuntimeError("Incomplete desktop observation")
    return windows


def _check_known(windows: dict[int, list[str]], created: dict[int, list[str]],
                 active: int | None = None) -> None:
    for hwnd, titles in created.items():
        if hwnd not in windows:
            raise RuntimeError("Created destination disappeared")
        if hwnd != active and windows[hwnd] != titles:
            raise RuntimeError("Created destination prefix/count changed")


def launch_windows(
    rows: list[Row],
    launch: Callable[[Row, str], Launcher],
    accepted: Callable[[Row], None],
    *,
    observe: Observer = run_desktop,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LaunchResult:
    """Stop on the first uncertain append; never retry an accepted launch.

    `accepted` persists/reports the exact launch before readiness can fail.
    Launcher polling is nonblocking and desktop helpers have deadlines. OS
    scheduling and local persistence are not hard real-time guarantees.
    """
    plan = plan_windows(rows)
    ordered = [row for group in plan for row in group.rows]
    if not ordered:
        return LaunchResult()
    launched: list[Row] = []
    ready: list[Row] = []
    created: dict[int, list[str]] = {}
    run_id = uuid4().hex
    current = ordered[0]
    attempted = 0
    try:
        baseline = set(_read(observe, 12.0))
        for group in plan:
            destination = None
            expected: list[str] = []
            for row in group.rows:
                current = row
                # Check the whole invocation before sending another append.
                if launched:
                    before = _read(observe, 12.0)
                    _check_known(before, created)
                    if set(before) - baseline - created.keys():
                        raise RuntimeError("Unexpected new window before append")
                deadline = monotonic() + APPEND_TIMEOUT_S
                attempted += 1
                process = launch(row, f"desk-{run_id}-{group.ordinal}")
                launched.append(row)
                accepted(row)
                previous = expected.copy()
                expected.append(normalize_title(row.title))
                while True:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Append readiness exceeded 20 seconds")
                    code = process.poll()
                    if code is not None and code != 0:
                        raise RuntimeError(f"wt.exe exited {code}")
                    windows = _read(observe, min(12.0, remaining))
                    if monotonic() >= deadline:
                        raise TimeoutError("Append readiness exceeded 20 seconds")
                    _check_known(windows, created, destination)
                    new = set(windows) - baseline - created.keys()
                    if destination is None:
                        if len(new) > 1:
                            raise RuntimeError("Ambiguous new destination windows")
                        candidate = next(iter(new)) if new else None
                    else:
                        if new:
                            raise RuntimeError("Unexpected new window during append")
                        candidate = destination
                    if candidate is not None:
                        if destination is None:
                            destination = candidate
                            created[destination] = previous.copy()
                        actual = windows[candidate]
                        if actual != previous and actual != expected:
                            raise RuntimeError("Destination prefix/count changed")
                        # Recheck exit after the potentially slow UIA read.
                        code = process.poll()
                        if code is not None and code != 0:
                            raise RuntimeError(f"wt.exe exited {code}")
                        if actual == expected and code == 0:
                            destination = candidate
                            created[destination] = expected.copy()
                            ready.append(row)
                            break
                    sleep(min(POLL_S, max(0.0, deadline - monotonic())))
    except Exception as exc:  # noqa: BLE001 -- preserve every accepted launch
        return LaunchResult(tuple(launched), tuple(ready),
                            ((current, f"{type(exc).__name__}: {exc}"),),
                            tuple(ordered[attempted:]))
    return LaunchResult(tuple(launched), tuple(ready))
