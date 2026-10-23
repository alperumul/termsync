"""Replay sanitized, derived G3 fixtures without live provider or desktop reads.

Expected positions preserve the recorded comparisons and final owner oracle.
The same-cwd case retains the corrected provider-isolated comparison/candidates.
See fixtures/README.md for provenance and the sanitization boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from termsync.capture import build_capture
from termsync.capture_windows import DesktopObservation, parse_desktop
from termsync.cli import snap
from termsync.emit import Emit
from termsync.model import Layout, Pending, PendingEntry, Session, Status
from termsync.store import Store, layout_hash

FIXTURES = Path(__file__).parent / "fixtures" / "windows_replay"
OBSERVATIONS = (
    "01-distinct",
    "02-duplicate",
    "03-manual-rename-minimized",
    "04-moved-duplicate",
    "05-reordered",
    "06-same-cwd-distinct",
    "07-tab-closed",
    "08-window-closed",
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def replay(index):
    case = read_json(FIXTURES / f"{OBSERVATIONS[index]}.json")
    envelope = case["emit"]
    envelope["sessions"] = tuple(envelope["sessions"])
    envelope["codex"] = tuple(envelope["codex"])
    return Emit(**envelope, desktop=parse_desktop(case["desktop"]))


def fixture_ids():
    oracle = read_json(FIXTURES / "oracle.json")
    return {
        row["identityEvidence"]["launchTab"]: row["sessionId"]
        for row in oracle["rows"] + oracle["closedRows"]
        if row["sessionId"] is not None
    }


def test_u9_saved_g3_capture_reaches_picker_in_recorded_tab_order():
    from termsync.picker import initial_state, render, visible_rows
    from termsync.rows import build_rows

    ids = fixture_ids()
    result = build_capture(replay(5), None)
    sessions = tuple(s for s in reversed(result.sessions) if s.session_id in ids.values())
    layout = Layout(1, "capture", 100, 1, "fixture", sessions)
    rows = build_rows(layout, {}, frozenset(), frozenset(),
                      lambda _: Path("fake.jsonl"), lambda _: True)
    state = initial_state(rows)
    assert [r.session_id for r in visible_rows(state)] == [
        ids[label] for label in ("s1", "s2", "s3", "s4", "s6", "s7", "s5")
    ]
    headers = [line for line in render(state, now=1000) if line.startswith("WINDOW")]
    assert headers == [
        "WINDOW 3  (3 sessions)", "WINDOW 4  (1 session)",
        "WINDOW 5  (2 sessions)", "WINDOW UNKNOWN  (1 session)",
    ]
    assert state.selected == frozenset(ids.values())
    assert layout.sessions == sessions  # rendering never rewrites source placement


# Recorded ordinals include the two pre-existing attributable work windows.
# None is unknown; absence means the session had closed or was not launched.
EXPECTED = (
    {"s1": (3, 0), "s2": (3, 1), "s3": (3, 2), "s4": (4, 0),
     "s5": (4, 2), "s6": (5, 0)},
    {"s1": None, "s2": (3, 1), "s3": (3, 2), "s4": (4, 0),
     "s5": (4, 2), "s6": (5, 0), "s7": None},
    {"s1": None, "s2": (3, 1), "s3": (3, 2), "s4": (4, 0),
     "s5": None, "s6": (5, 0), "s7": None},
    {"s1": None, "s2": (3, 1), "s3": (3, 2), "s4": (4, 0),
     "s5": None, "s6": (5, 0), "s7": None},
    {"s1": None, "s2": (3, 1), "s3": (3, 2), "s4": (4, 2),
     "s5": None, "s6": (5, 0), "s7": None},
    {"s1": (3, 0), "s2": (3, 1), "s3": (3, 2), "s4": (4, 2),
     "s5": None, "s6": (5, 0), "s7": (5, 1)},
    {"s1": (3, 0), "s2": (3, 1), "s3": (3, 2),
     "s5": None, "s6": (4, 0), "s7": (4, 1)},
    {"s1": (3, 0), "s2": (3, 1), "s3": (3, 2), "s5": None},
)


@pytest.mark.parametrize("index", range(8), ids=OBSERVATIONS)
def test_saved_observation_replays_fixture_placement_and_counts(index):
    emit = replay(index)
    assert emit.desktop.error is None
    result = build_capture(emit, None)
    sessions = {session.session_id: session for session in result.sessions}
    for label, session_id in fixture_ids().items():
        if label not in EXPECTED[index]:
            assert session_id not in sessions
            continue
        session = sessions[session_id]
        expected = EXPECTED[index][label] or (None, None)
        assert (session.window_ordinal, session.tab_index) == expected
    assert result.window_count == (5, 5, 5, 5, 5, 5, 4, 3)[index]
    assert emit.desktop.windows_seen == (6 if index < 7 else 5)
    assert all(s.window_ordinal is None and s.tab_index is None
               for s in result.sessions if s.agent == "codex")
    assert result.window_unknown_sessions == sum(
        s.window_ordinal is None for s in result.sessions
    )


def test_startup_duplicate_has_two_live_sessions_but_only_one_matching_tab():
    emit = replay(1)
    ids = fixture_ids()
    duplicate_entries = [entry for entry in emit.sessions
                         if entry["sessionId"] in (ids["s1"], ids["s7"])]
    assert len(duplicate_entries) == 2
    assert {entry["titleRaw"] for entry in duplicate_entries} == {"\u2733 Fixture-one"}
    scans = read_json(FIXTURES / f"{OBSERVATIONS[1]}.json")["desktop"]["windows"]
    assert sum(tab["name"] == "\u2733 Fixture-one"
               for scan in scans for tab in scan["tabs"]) == 1
    result = build_capture(emit, None)
    assert all(s.window_ordinal is None and s.tab_index is None
               for s in result.sessions if s.session_id in (ids["s1"], ids["s7"]))


def test_same_cwd_reuses_corrected_provider_isolated_evidence():
    case = read_json(FIXTURES / f"{OBSERVATIONS[5]}.json")
    emit = replay(5)
    comparison = case["comparison"]
    assert emit.codex_error == comparison["codexError"]
    assert emit.codex_error and emit.claude_error is None
    result = build_capture(emit, None)
    actual = {s.session_id: s for s in result.sessions}
    candidates = case["claudeCandidatesIsolated"]
    ordinals = {104: 1, 105: 2, 103: 3, 106: 4, 102: 5}
    for candidate in candidates:
        session = actual[candidate["sessionId"]]
        assert (session.window_ordinal, session.tab_index) == (
            ordinals.get(candidate["hwnd"]), candidate["tabIndex"],
        )
    first, second = (actual[row["sessionId"]] for row in comparison["sessions"])
    assert first.cwd == second.cwd
    assert first.window_ordinal != second.window_ordinal


def entry(session_id="a", title="Alpha", started_at=10, **overrides):
    return {
        "sessionId": session_id, "titleRaw": title, "titleAttachOk": True,
        "name": title, "cwd": "D:\\fixture", "alive": True,
        "procStart": "123456789012345678", "procStartLive": "123456789012345678",
        "kind": "interactive", "startedAt": started_at, **overrides,
    }


def window(hwnd, *titles):
    return {
        "hwnd": hwnd, "existsBefore": True, "existsAfter": True,
        "state": "ok", "error": None,
        "tabs": [{"index": index, "name": title} for index, title in enumerate(titles)],
    }


def desktop(*windows):
    handles = [w["hwnd"] for w in windows]
    return parse_desktop({"before": handles, "after": handles, "windows": list(windows)})


def emit_of(*entries, observation=None, **kwargs):
    return Emit(sessions=tuple(entries), claude_process_count=len(entries), boot_time=1,
                desktop=observation, **kwargs)


def test_ordinals_are_independent_of_hwnd_enumeration_and_session_input_order():
    entries = (entry("z", "Known", 5), entry("b", "Tie B", 9),
               entry("a", "Tie A", 9), entry("d", "Missing D", None),
               entry("c", "Missing C", None))
    windows = (window(99, "Known"), window(88, "Tie B"), window(77, "Tie A"),
               window(66, "Missing D"), window(55, "Missing C"), window(1, "Plain"))
    expected = {"z": 1, "a": 2, "b": 3, "c": 4, "d": 5}
    for raw_entries, raw_windows in ((entries, windows), (entries[::-1], windows[::-1])):
        result = build_capture(emit_of(*raw_entries, observation=desktop(*raw_windows)), None)
        assert {s.session_id: s.window_ordinal for s in result.sessions} == expected
        assert result.window_count == 5


@pytest.mark.parametrize("invalid_start", [None, 0, -1, True, "1", 1.5])
def test_invalid_start_times_follow_known_times_without_using_hwnd(invalid_start):
    emit = emit_of(entry("a", "Missing", invalid_start), entry("z", "Known", 20),
                   observation=desktop(window(1, "Missing"), window(900, "Known")))
    result = build_capture(emit, None)
    assert {s.session_id: s.window_ordinal for s in result.sessions} == {"a": 2, "z": 1}


def test_window_rank_uses_earliest_member_even_when_it_is_not_first_tab():
    emit = emit_of(entry("a", "Later", 50), entry("b", "Earlier", 5),
                   entry("c", "Middle", 20),
                   observation=desktop(window(100, "Later", "Plain", "Earlier"),
                                       window(10, "Middle")))
    result = build_capture(emit, None)
    assert {s.session_id: (s.window_ordinal, s.tab_index) for s in result.sessions} == {
        "a": (1, 0), "b": (1, 2), "c": (2, 0),
    }


def test_armed_and_codex_sessions_stay_unknown_even_with_matching_titles():
    pending = Pending(1, (PendingEntry("p", "Pending", "D:\\fixture", 1),))
    codex = Session("c", "D:\\fixture", "Codex", agent="codex",
                    window_ordinal=77, tab_index=88).to_json()
    emit = emit_of(entry(), codex=(codex,),
                   observation=desktop(window(1, "Alpha", "Pending", "Codex")))
    result = build_capture(emit, pending)
    assert {s.session_id: (s.window_ordinal, s.tab_index) for s in result.sessions} == {
        "a": (1, 0), "c": (None, None), "p": (None, None),
    }
    assert result.window_unknown_sessions == 2


@pytest.mark.parametrize("failure", ["claude", "desktop"])
def test_incomplete_attribution_read_preserves_sessions_but_clears_all_placement(failure):
    emit = replay(5)
    if failure == "claude":
        emit = replace(emit, claude_error="registry incomplete")
    else:
        emit = replace(emit, desktop=replace(emit.desktop, error="UIA timed out"))
    result = build_capture(emit, None)
    assert result.sessions
    assert all(s.window_ordinal is None and s.tab_index is None for s in result.sessions)
    assert result.window_count is None
    assert result.window_unknown_sessions == len(result.sessions)


def test_snap_persists_physical_attributable_and_unknown_counts_with_provider_error(tmp_path):
    emit = replay(5)
    store = Store(tmp_path / "isolated-store")
    expected = build_capture(emit, None)
    assert snap(store, emit_runner=lambda: emit, clock=lambda: 1000) == 0
    layout, status = store.newest(), store.read_status()
    assert layout.sessions == expected.sessions
    assert layout.window_count == 5
    assert status.windows_seen == 6  # Includes minimized plain-only F4.
    assert status.window_unknown_sessions == expected.window_unknown_sessions
    assert status.window_error is None
    assert status.codex_error == emit.codex_error
    assert status.claude_error is None
    assert status.outcome == "degraded"


def test_snap_desktop_failure_preserves_capture_and_records_error(tmp_path):
    emit = emit_of(entry(), observation=DesktopObservation(error="UIA timeout"))
    store = Store(tmp_path / "isolated-store")
    assert snap(store, emit_runner=lambda: emit, clock=lambda: 1000) == 0
    layout, status = store.newest(), store.read_status()
    assert len(layout.sessions) == 1
    assert layout.window_count is None
    assert layout.sessions[0].window_ordinal is None
    assert status.outcome == "degraded"
    assert status.window_error == "UIA timeout"
    assert status.windows_seen is None
    assert status.window_unknown_sessions == 1


def test_snap_partial_claude_failure_keeps_independent_sessions_without_placement(tmp_path):
    emit = emit_of(
        entry(), observation=desktop(window(1, "Alpha")),
        claude_error="one registry file unreadable",
        codex=(Session("codex", "D:\\fixture", "Codex", agent="codex").to_json(),),
    )
    store = Store(tmp_path / "isolated-store")
    assert snap(store, emit_runner=lambda: emit, clock=lambda: 1000) == 0
    layout, status = store.newest(), store.read_status()
    assert {s.session_id for s in layout.sessions} == {"a", "codex"}
    assert all(s.window_ordinal is None and s.tab_index is None for s in layout.sessions)
    assert layout.window_count is None
    assert status.outcome == "degraded"
    assert status.claude_error == "one registry file unreadable"
    assert status.codex_error is None
    assert status.windows_seen == 1
    assert status.window_unknown_sessions == 2


def test_store_versions_reorder_and_move_but_refreshes_title_only(tmp_path):
    store = Store(tmp_path / "isolated-store")
    first = Session("a", "D:\\fixture", "Original", window_ordinal=1, tab_index=0)
    second = Session("b", "D:\\fixture", "Second", window_ordinal=1, tab_index=2)
    assert store.write_capture((first, second), 1, 1, window_count=1).wrote_version == 1
    renamed = replace(first, title="Renamed", last_prompt_at=99)
    refreshed = store.write_capture((second, renamed), 2, 1, window_count=1)
    assert refreshed.wrote_version is None
    assert next(s for s in refreshed.layout.sessions if s.session_id == "a").title == "Renamed"
    reordered = replace(renamed, tab_index=3)
    assert store.write_capture((reordered, second), 3, 1, window_count=1).wrote_version == 2
    moved = replace(reordered, window_ordinal=2, tab_index=0)
    moved_write = store.write_capture((moved, second), 4, 1, window_count=2)
    assert moved_write.wrote_version == 3
    assert Store(store.root).newest().window_count == 2
    assert layout_hash((first, second)) != layout_hash((reordered, second))
    assert layout_hash((reordered, second)) != layout_hash((moved, second))


def test_legacy_layout_and_status_decode_with_unknown_placement():
    layout = Layout.from_json({
        "version": 1, "source": "capture", "capturedAt": 100, "bootId": 1,
        "hash": "sha256:legacy", "sessions": [{"sessionId": "a", "cwd": "D:\\fixture"}],
    })
    status = Status.from_json({"ranAt": 100, "outcome": "ok", "sessionsSeen": 1})
    assert layout.window_count is None
    assert layout.sessions[0].window_ordinal is None
    assert layout.sessions[0].tab_index is None
    assert status.windows_seen is None and status.window_error is None
    assert status.window_unknown_sessions == 0


def test_legacy_hash_upgrade_allocates_once_then_deduplicates(tmp_path):
    store = Store(tmp_path / "isolated-store")
    store.layouts_dir.mkdir(parents=True)
    session = Session("a", "D:\\fixture", "Original")
    old_identity = f"{session.agent}|{session.session_id}|{session.cwd}|0"
    old_hash = "sha256:" + hashlib.sha256(old_identity.encode("utf-8")).hexdigest()
    old_layout = Layout(1, "capture", 100, 1, old_hash, (session,))
    (store.layouts_dir / "000001.json").write_text(
        json.dumps(old_layout.to_json()), encoding="utf-8",
    )
    upgraded = store.write_capture((session,), 200, 1)
    assert upgraded.wrote_version == 2
    assert upgraded.layout.hash != old_hash
    assert store.write_capture((session,), 300, 1).wrote_version is None


def test_empty_successful_desktop_is_zero_but_legacy_missing_desktop_is_unknown():
    observed = build_capture(emit_of(observation=desktop()), None)
    legacy = build_capture(emit_of(), None)
    assert observed.window_count == 0
    assert legacy.window_count is None
    assert observed.window_unknown_sessions == legacy.window_unknown_sessions == 0
