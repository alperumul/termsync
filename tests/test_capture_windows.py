"""Desktop protocol, process bounds and provider independence; no live desktop."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier

import pytest

from termsync import capture_windows as windows
from termsync.emit import Emit, parse_emit, run_capture, run_emit


def payload():
    return {"before": [10, 20], "after": [10, 20], "windows": [
        {"hwnd": 10, "state": "ok", "tabs": [{"index": 0, "name": "one"}]},
        {"hwnd": 20, "state": "ok", "tabs": [{"index": 0, "name": "shell"}]},
    ]}


def test_empty_desktop_is_successful_and_single_window_keeps_array_shape():
    assert windows.parse_desktop({"before": [], "after": [], "windows": []}) == (
        windows.DesktopObservation(windows_seen=0)
    )
    raw = payload()
    raw["before"] = raw["after"] = [10]
    raw["windows"].pop()
    observation = windows.parse_desktop(raw)
    assert observation.windows_seen == 1 and not observation.error
    assert observation.windows[0]["tabs"][0]["name"] == "one"


@pytest.mark.parametrize("state", ["visible-zero-tabs", "error", "vanished", "surprise"])
def test_unreadable_surviving_window_retains_count_but_blocks_global_join(state):
    raw = payload()
    raw["windows"][1].update(state=state, error="fixture failure", tabs=[])
    observation = windows.parse_desktop(raw)
    assert observation.windows_seen == 2
    assert "fixture failure" in observation.error
    assert windows.placements((), observation, None) == ({}, None)


@pytest.mark.parametrize("state", ["ok", "vanished", "error"])
def test_disappeared_window_discards_stale_tabs_without_error(state):
    raw = payload()
    raw["after"] = [10]
    raw["windows"][1].update(state=state, error="may have disappeared mid-call")
    observation = windows.parse_desktop(raw)
    assert not observation.error and observation.windows_seen == 1
    assert [w["hwnd"] for w in observation.windows] == [10]


def test_new_window_blocks_join_because_its_unread_tabs_can_hide_duplicates():
    raw = payload()
    raw["after"].append(30)
    observation = windows.parse_desktop(raw)
    assert observation.error and observation.windows_seen == 3
    assert windows.placements((), observation, None) == ({}, None)


@pytest.mark.parametrize("field,value", [
    ("before", None), ("before", [10, 10]), ("before", [True]),
    ("after", "10"), ("windows", {}), ("windows", [None]), ("windows", []),
])
def test_bad_envelope_is_a_reader_failure(field, value):
    raw = payload()
    raw[field] = value
    with pytest.raises(ValueError):
        windows.parse_desktop(raw)


@pytest.mark.parametrize("tabs", [
    [], None, {}, [None], [{"index": 1, "name": "gap"}],
    [{"index": False, "name": "boolean"}], [{"index": 0, "name": None}],
])
def test_invalid_or_empty_tab_arrays_are_not_silently_healthy(tabs):
    raw = payload()
    raw["windows"][0]["tabs"] = tabs
    assert windows.parse_desktop(raw).error


def test_duplicate_or_changed_scan_handles_are_rejected():
    raw = payload()
    raw["windows"][1] = deepcopy(raw["windows"][0])
    with pytest.raises(ValueError, match="match enumerated"):
        windows.parse_desktop(raw)


def test_desktop_timeout_is_one_total_process_budget(monkeypatch):
    def run(argv, **kwargs):
        assert argv[-1].endswith("Read-Windows.ps1")
        assert kwargs["timeout"] == 12
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        raise subprocess.TimeoutExpired(argv, 12)
    monkeypatch.setattr(windows.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        windows.run_desktop()


@pytest.mark.parametrize("bad", [False, True])
def test_desktop_boundary_decodes_unicode_and_reports_nonzero_exit(monkeypatch, bad):
    raw = payload()
    raw["windows"][0]["tabs"][0]["name"] = "Grüße; 日本"
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, int(bad),
                                           json.dumps(raw).encode(), b"fixture failure")
    monkeypatch.setattr(windows.subprocess, "run", run)
    if bad:
        with pytest.raises(RuntimeError, match="fixture failure"):
            windows.run_desktop()
    else:
        assert windows.run_desktop().windows[0]["tabs"][0]["name"] == "Grüße; 日本"


def test_all_three_bounded_readers_start_concurrently_and_fail_independently():
    barrier = Barrier(3, timeout=3)
    def claude():
        barrier.wait()
        return Emit((), 0, 1, claude_error="partial registry")
    def codex():
        barrier.wait()
        return {"sessions": [], "errors": []}
    def desktop():
        barrier.wait()
        raise TimeoutError("desktop timed out")
    observed = run_capture(claude, codex, desktop)
    assert observed.boot_time == 1 and observed.claude_error == "partial registry"
    assert observed.codex_error is None
    assert "desktop timed out" in observed.desktop.error
    assert observed.desktop.windows_seen is None


def test_additive_claude_error_survives_envelope_parse():
    raw = {"sessions": [], "claudeProcessCount": 1, "bootTime": 1,
           "claudeError": "Unreadable registry file"}
    assert parse_emit(json.dumps(raw)).claude_error == raw["claudeError"]


def test_duplicate_tab_titles_block_even_one_live_session():
    raw = payload()
    raw["windows"][1]["tabs"][0]["name"] = "one"
    entries = ({"sessionId": "a", "titleRaw": "one", "titleAttachOk": True},)
    assert windows.placements(entries, windows.parse_desktop(raw), None) == ({}, 0)


@pytest.mark.parametrize("missing", [
    {"titleRaw": "", "titleAttachOk": True},
    {"titleRaw": "one", "titleAttachOk": False},
])
def test_unreadable_live_title_can_hide_duplicate_and_blocks_all_placement(missing):
    entries = ({"sessionId": "a", "titleRaw": "one", "titleAttachOk": True},
               {"sessionId": "b", "name": "fallback is not join authority", **missing})
    assert windows.placements(entries, windows.parse_desktop(payload()), None) == ({}, None)


def test_shared_normalizer_applies_to_both_sides_without_changing_tab_indexes():
    raw = payload()
    raw["windows"][0]["tabs"] = [
        {"index": 0, "name": "shell"}, {"index": 1, "name": "Grüße； 日本"},
    ]
    entries = ({"sessionId": "a", "titleRaw": "✳ Grüße; 日本", "titleAttachOk": True},)
    assert windows.placements(entries, windows.parse_desktop(raw), None) == ({"a": (1, 1)}, 1)


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 registry boundary")
def test_real_emitter_preserves_readable_entries_and_reports_malformed_files():
    # conftest pins USERPROFILE/HOME to an isolated home for all subprocesses.
    registry = Path(os.environ["USERPROFILE"]) / ".claude" / "sessions"
    registry.mkdir(parents=True)
    for name, content in {"empty": "", "null": "null", "array": "[1,2]",
                          "broken": "{", "no-pid": "{}", "bad-pid": '{"pid":"oops"}'}.items():
        (registry / f"{name}.json").write_text(content, encoding="utf-8")
    good = {"pid": 0, "sessionId": "isolated-entry", "cwd": "D:\\fixture",
            "procStart": None, "kind": "interactive", "name": "Fixture"}
    (registry / "valid.json").write_text(json.dumps(good), encoding="utf-8")
    observed = run_emit()
    assert [e["sessionId"] for e in observed.sessions] == ["isolated-entry"]
    assert all(name + ".json" in observed.claude_error
               for name in ("empty", "null", "array", "broken", "no-pid", "bad-pid"))
