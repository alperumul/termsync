"""Cross-provider storage, reader isolation, and U5's restore boundary."""
from dataclasses import replace
from pathlib import Path

import pytest

from termsync import cli, picker, restore, rows
from termsync.capture import build_capture, live_session_ids
from termsync.capture_windows import DesktopObservation
from termsync.emit import Emit, run_capture
from termsync.model import Session
from termsync.store import Store, layout_hash
from termsync.restore_codex import CodexRestoreInfo


CODEX = {"sessionId": "codex-session", "agent": "codex", "cwd": "D:\\code",
         "title": "Codex task", "resumeArgs": ["-m", "model"],
         "rolloutPath": "C:\\fake\\rollout.jsonl.zst", "archived": True}
CLAUDE = {"sessionId": "claude-session", "cwd": "D:\\code", "titleRaw": "Claude task",
          "titleAttachOk": True, "alive": True, "procStart": "123", "procStartLive": "123",
          "kind": "interactive", "name": "Claude task"}


def fail():
    raise TimeoutError("fixture reader timeout")


def good_claude():
    return Emit(sessions=(CLAUDE,), claude_process_count=1, boot_time=100)


def good_codex():
    return {"sessions": [CODEX], "errors": []}


@pytest.mark.parametrize("claude_runner,codex_runner,expected_agents,outcome", [
    (good_claude, good_codex, {"claude", "codex"}, "ok"),
    (good_claude, fail, {"claude"}, "degraded"),
    (fail, good_codex, {"codex"}, "degraded"),
    (fail, fail, set(), "error"),
])
def test_reader_failures_are_independent_through_real_store(
    tmp_path, claude_runner, codex_runner, expected_agents, outcome,
):
    observed = run_capture(claude_runner, codex_runner, lambda: DesktopObservation(windows_seen=0))
    store = Store(tmp_path / "store")
    result = cli.snap(store, emit_runner=lambda: observed, clock=lambda: 1000)
    assert result == (1 if outcome == "error" else 0)
    status = store.read_status()
    assert status.outcome == outcome
    assert bool(status.claude_error) == (claude_runner is fail)
    assert bool(status.codex_error) == (codex_runner is fail)
    if outcome == "error":
        assert store.newest() is None
    else:
        assert {s.agent for s in store.newest().sessions} == expected_agents
        assert status.codex_sessions_seen == int("codex" in expected_agents)


def test_codex_live_set_and_capture_use_the_same_ids():
    observed = run_capture(good_claude, good_codex, lambda: DesktopObservation(windows_seen=0))
    result = build_capture(observed, None)
    assert live_session_ids(observed) == {s.session_id for s in result.sessions}


def test_old_layout_defaults_to_claude_and_codex_metadata_roundtrips():
    assert Session.from_json({"sessionId": "old", "cwd": "D:\\code"}).agent == "claude"
    session = Session.from_json(CODEX)
    restored = Session.from_json(session.to_json())
    assert restored == session
    assert restored.resume_args == ("-m", "model")
    assert restored.archived
    assert restored.rollout_path.endswith(".zst")
    assert layout_hash([session]) != layout_hash([replace(session, agent="claude")])


def test_bad_codex_contract_degrades_without_hiding_claude():
    observed = run_capture(good_claude, lambda: {"sessions": [CLAUDE], "errors": []},
                           lambda: DesktopObservation(windows_seen=0))
    assert observed.codex_error
    assert observed.codex == ()
    assert observed.sessions == (CLAUDE,)


def test_codex_rows_never_use_claude_lookup_or_launcher(tmp_path):
    def forbidden(*args):
        raise AssertionError("Codex row reached Claude file lookup")
    def lookup(_):
        return CodexRestoreInfo(Path("rollout.jsonl"), archived=True)
    row = rows._desk_row(Session.from_json(CODEX), {}, forbidden, lambda _: True,
                         frozenset(), lookup)
    assert row.selectable and row.agent == "codex"
    assert "[codex]" in cli._render_row(row, 0)
    assert "Unarchive on restore" in picker._render_row(row, False, False, 0)
    ok, skipped = restore.preflight([row], forbidden, lambda _: True, lookup)
    assert ok == [row] and skipped == []
    argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=0,
                                 window_name="desk-test-1")
    assert argv[argv.index("-Agent") + 1] == "codex"
    assert "-Archived" in argv


def test_session_id_collision_fails_loudly_instead_of_merging_agents():
    observed = replace(good_claude(), codex=({**CODEX, "sessionId": "claude-session"},))
    with pytest.raises(Exception, match="Conflicting"):
        build_capture(observed, None)
