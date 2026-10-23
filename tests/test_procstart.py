"""§5's `procStart` pid-reuse guard (S3, §13.4) — positive AND negative equality.

`capture.build_capture` is the only place this comparison happens (§5 step
6), so these tests exercise it end to end via a synthetic single-entry
`Emit`, rather than importing a standalone comparator. These constructed
entries use the registry field shapes from the sanitized golden fixture;
negative cases introduce explicit mismatches without altering that fixture.
"""

from __future__ import annotations

from typing import Any

from termsync.capture import build_capture
from termsync.emit import Emit

# A synthetic FILETIME string at realistic precision, and a neighbour that
# differs by exactly one tick in the final digit.
REAL_PROC_START = "130000000000000003"
ONE_TICK_OFF = "130000000000000004"

# Verified (see the S3 work): at this magnitude a double's ULP is ~16, so
# EVERY neighbour within a few ticks of REAL_PROC_START -- including
# ONE_TICK_OFF above -- collides with it under `float()`. That is not a
# coincidence to route around; it is the whole reason rule 2 ("no numeric
# conversion of any kind") exists: an 18-digit FILETIME string exceeds
# IEEE-754 double precision, so `float(a) == float(b)` is true even for the
# most common, most realistic mismatch (a pid reused moments apart), not
# just for a contrived pair. FLOAT_COLLIDES_BUT_STRING_DIFFERS pins one
# concrete instance of that collision so a `float()` creeping into the
# comparator is caught by an assertion, not just by argument.
FLOAT_COLLIDES_BUT_STRING_DIFFERS = "130000000000000002"
assert float(REAL_PROC_START) == float(FLOAT_COLLIDES_BUT_STRING_DIFFERS)  # the collision itself
assert REAL_PROC_START != FLOAT_COLLIDES_BUT_STRING_DIFFERS  # but the strings differ


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sessionId": "s1",
        "cwd": r"C:\fixture\tool",
        "alive": True,
        "procStart": REAL_PROC_START,
        "procStartLive": REAL_PROC_START,
        "kind": "interactive",
        "titleRaw": "termsync work",
        "titleAttachOk": True,
        "name": "termsync-1",
        "version": "2.1.238",
    }
    base.update(overrides)
    return base


def _emit_of(*entries: dict[str, Any]) -> Emit:
    return Emit(sessions=tuple(entries), claude_process_count=len(entries), boot_time=1)


def _survives(entry: dict[str, Any]) -> bool:
    result = build_capture(_emit_of(entry), pending=None)
    return len(result.sessions) == 1


def test_matching_proc_start_is_accepted() -> None:
    """The positive case an earlier revision omitted: equal strings survive."""
    entry = _entry(procStart=REAL_PROC_START, procStartLive=REAL_PROC_START)
    assert _survives(entry) is True


def test_proc_start_differing_by_a_single_tick_is_rejected() -> None:
    entry = _entry(procStart=REAL_PROC_START, procStartLive=ONE_TICK_OFF)
    assert _survives(entry) is False


def test_float_colliding_proc_start_pair_is_still_rejected_as_strings() -> None:
    """Guards against a `float()` creeping into the comparison.

    REAL_PROC_START and FLOAT_COLLIDES_BUT_STRING_DIFFERS are distinct
    18-digit decimal strings that compare EQUAL once passed through
    `float()` (asserted above) -- so a comparator that silently converts to
    float would wrongly accept this pair. The correct string-ordinal
    comparator must still reject it.
    """
    entry = _entry(procStart=REAL_PROC_START, procStartLive=FLOAT_COLLIDES_BUT_STRING_DIFFERS)
    assert _survives(entry) is False


def test_missing_proc_start_live_is_rejected() -> None:
    entry = _entry(procStart=REAL_PROC_START)
    del entry["procStartLive"]
    assert _survives(entry) is False


def test_none_proc_start_live_is_rejected() -> None:
    """H1's guard fallback: `.StartTime` unreadable -> procStartLive stays null."""
    entry = _entry(procStart=REAL_PROC_START, procStartLive=None)
    assert _survives(entry) is False
