"""Golden-file contract test: PowerShell `-Emit`'s JSON <-> `termsync.emit.parse_emit` (§13.4).

`emit_golden.json` and `emit_golden_single.json` are sanitized derived
fixtures from real `-Emit` output (the latter a single-entry slice -- see
the note next to `GOLDEN_SINGLE` below).
This is the one place the PowerShell<->Python contract is checkable at all;
when `-Emit`'s output shape changes, this test is supposed to fail first
(§13.2's design note on `model.py`).

**What this test can and cannot catch (review finding M7).** Both fixtures
are frozen files, checked in and edited only by a human/agent deliberately
regenerating them. This test therefore detects drift between the *fixture*
and the parsing/shape contract below -- e.g. someone editing
`emit_golden.json` to drop a field, or `emit.py`/this test's contract
loosening to accept less than the fixture actually carries. It CANNOT
detect the live emitter (`Snapshot-Desk.ps1 -Emit`) drifting away from what
the fixture says, because nothing here ever re-runs `-Emit` against a live
registry. §13.5 lists "Claude Code changes the registry format" with
detection "`test_model_contract.py` fails on the next run" -- that claim is
only true once a human has noticed the drift and regenerated the fixture
from a fresh capture; it is not an automatic trip-wire against a live
Claude Code upgrade.

Closing that gap for real needs one of:
  - a live-run comparison added to `snapshot\\smoke-emit.ps1` (or a sibling
    script) that runs `-Emit` against whatever registry is actually present
    and asserts it satisfies the same per-field contract as this test,
    on every install/upgrade rather than only when a human regenerates the
    golden file; or
  - a periodic/CI job that re-runs `-Emit` on a real machine and diffs its
    key set against `PER_SESSION_FIELDS` below, failing loudly (not
    silently widening the golden file) when Claude Code adds, renames, or
    removes a field.
Recording this here rather than silently leaving §13.5's claim overstated,
per the S3 review's finding M7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from termsync.emit import Emit, parse_emit

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "emit_golden.json"

# Both golden fixtures use synthetic identifiers, paths and timestamps.
# The single-entry fixture is an exact slice of the third sanitized row,
# `sessionId == "40000000-0000-4000-8000-000000000003"`, with the same
# bootTime and per-session fields. Its `claudeProcessCount` is deliberately
# set to `1` so the envelope is coherent with a one-entry `sessions` array.
# Its job is exercising the `ConvertTo-Json` single-element-array unwrap
# case (§3.1's `@()` force-wrap), not representing a raw sparse capture.
GOLDEN_SINGLE = FIXTURES / "emit_golden_single.json"

REQUIRED_TOP_LEVEL_KEYS = {"sessions", "claudeProcessCount", "bootTime"}

# Every per-session field the emitter contract carries and the Python
# pipeline depends on: the registry-sourced fields `capture.py` reads
# (`sessionId`, `cwd`, `kind`, `version`, `name`) plus the emitter's own
# additions (`alive`, `procStartLive`, `titleRaw`, `titleAttachOk`, and the
# `procStart`/`pid` pair the pid-reuse guard and liveness fact depend on).
#
# This is deliberately NOT the full set of keys either fixture carries --
# `Snapshot-Desk.ps1` passes many more registry fields through verbatim via
# `Add-Member` (`bridgeSessionId`, `entrypoint`, `messagingSocketPath`,
# `nameSince`, `nameSource`, `peerFeatures`, `peerProtocol`, `startedAt`,
# `status`, `statusUpdatedAt`, `updatedAt` are all present in the golden
# fixtures today) that nothing in `termsync` reads. Pinning those here would
# make this test fail on registry churn the pipeline does not care about,
# which is not the drift §13.2 asks this test to catch.
#
# (field name -> (expected JSON type, may the value legitimately be null))
PER_SESSION_FIELDS: dict[str, tuple[type, bool]] = {
    "sessionId": (str, False),
    "cwd": (str, False),
    "kind": (str, False),
    "version": (str, False),
    "name": (str, False),
    "titleRaw": (str, False),
    "pid": (int, False),
    "alive": (bool, False),
    "titleAttachOk": (bool, False),
    # §5 step 6 / capture.py's `_proc_start_matches`: an 18-character decimal
    # FILETIME string, compared ordinally. A JSON *number* here silently
    # destroys the pid-reuse guard through float imprecision at this
    # magnitude (synthetic example: `int(float("130000000000000003")) ==
    # 130000000000000000` -- a collision that changes identity).
    "procStart": (str, False),
    # `capture.py._proc_start_matches`: "A missing or `None` `procStartLive`
    # -- the PowerShell side's H1 guard fallback when `.StartTime` itself
    # could not be read -- fails this check." Null is a legitimate,
    # deliberately-guarded value here, not an error.
    "procStartLive": (str, True),
}


def _assert_envelope_matches_contract(payload: dict[str, Any]) -> None:
    """The full per-field contract check (review finding M7).

    Asserts the top level is EXACTLY `sessions`/`claudeProcessCount`/
    `bootTime` (a genuine set-equality check, not the subset check the
    fixture-only assertion used before this rewrite), and that every
    session entry carries every field in `PER_SESSION_FIELDS` with the
    right type -- null only where the table says null is legal.
    """
    assert payload.keys() == REQUIRED_TOP_LEVEL_KEYS, (
        f"top-level keys must be exactly {REQUIRED_TOP_LEVEL_KEYS}, got {set(payload.keys())}"
    )

    sessions = payload["sessions"]
    assert isinstance(sessions, list)

    for entry in sessions:
        for field, (expected_type, nullable) in PER_SESSION_FIELDS.items():
            assert field in entry, f"session entry is missing required field {field!r}: {entry}"
            value = entry[field]
            if value is None:
                assert nullable, f"field {field!r} is null but the contract does not allow that"
            else:
                assert isinstance(value, expected_type), (
                    f"field {field!r} must be {expected_type.__name__}, "
                    f"got {type(value).__name__} ({value!r})"
                )


def test_golden_fixture_parses_cleanly() -> None:
    result = parse_emit(GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(result, Emit)
    assert len(result.sessions) == 3


def test_golden_single_fixture_parses_cleanly() -> None:
    result = parse_emit(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    assert isinstance(result, Emit)
    assert len(result.sessions) == 1


def test_golden_fixture_top_level_keys_are_exactly_the_documented_three() -> None:
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert payload.keys() == REQUIRED_TOP_LEVEL_KEYS


def test_golden_single_fixture_top_level_keys_are_exactly_the_documented_three() -> None:
    payload = json.loads(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    assert payload.keys() == REQUIRED_TOP_LEVEL_KEYS


def test_golden_fixture_satisfies_the_full_per_session_contract() -> None:
    """M7's fix: every field in `PER_SESSION_FIELDS`, present, right type,
    null only where declared nullable. See `_assert_envelope_matches_contract`."""
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    _assert_envelope_matches_contract(payload)


def test_golden_single_fixture_satisfies_the_full_per_session_contract() -> None:
    payload = json.loads(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    _assert_envelope_matches_contract(payload)


def test_golden_fixture_carries_a_glyph_prefixed_title_raw() -> None:
    """§3.5's status-glyph prefix, as actually emitted -- \\u25d1 and \\u2733 observed."""
    result = parse_emit(GOLDEN.read_text(encoding="utf-8"))
    title_raws = [s["titleRaw"] for s in result.sessions]
    assert any(t.startswith("◑") for t in title_raws)
    assert any(t.startswith("✳") for t in title_raws)


def test_golden_single_fixture_is_the_convertto_json_unwrap_case() -> None:
    """A single-element `sessions` array must still parse as a one-element
    array, not an unwrapped bare object -- the documented `ConvertTo-Json`
    gotcha that `@()` force-wrapping in Snapshot-Desk.ps1 exists to avoid."""
    payload = json.loads(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    assert isinstance(payload["sessions"], list)
    assert len(payload["sessions"]) == 1

    result = parse_emit(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    assert len(result.sessions) == 1
    assert result.sessions[0]["sessionId"] == "40000000-0000-4000-8000-000000000003"


def test_golden_fixture_boot_time_is_present_and_not_null() -> None:
    result = parse_emit(GOLDEN.read_text(encoding="utf-8"))
    assert result.boot_time == 1700000000000


def test_golden_single_fixture_claude_process_count_matches_its_one_session() -> None:
    """L1: the fixture is a derived slice, and its `claudeProcessCount` must
    stay internally coherent with its one-entry `sessions` array -- this is
    exactly the invariant the previous hand-edited version of this fixture
    broke (`claudeProcessCount: 3` next to a single session)."""
    result = parse_emit(GOLDEN_SINGLE.read_text(encoding="utf-8"))
    assert result.claude_process_count == 1
    assert len(result.sessions) == 1


def test_golden_fixture_claude_process_count() -> None:
    result = parse_emit(GOLDEN.read_text(encoding="utf-8"))
    assert result.claude_process_count == 3


def test_golden_fixture_sessions_are_all_alive_and_interactive_with_matching_proc_start() -> None:
    """Documents the fixture's own shape (the brief's constraint on it): no
    alive:false, no titleAttachOk:false, no kind:bg, no procStart mismatch --
    every negative case lives in test_capture.py / test_procstart.py as a
    synthetic dict instead."""
    result = parse_emit(GOLDEN.read_text(encoding="utf-8"))
    for entry in result.sessions:
        assert entry["alive"] is True
        assert entry["kind"] == "interactive"
        assert entry["titleAttachOk"] is True
        assert entry["procStart"] == entry["procStartLive"]
