"""Release-review regressions using isolated stores and injected readers only."""
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from termsync import cli, picker, restore
from termsync.emit import Emit
from termsync.model import Session
from termsync.rows import Row, Rows
from termsync.store import Store


def row(agent="claude", **changes):
    return replace(Row(agent, agent, "C:/fixture", 1, 1, Path("fake.jsonl"),
                       True, False, False, False, True, agent=agent), **changes)


@pytest.mark.parametrize("agent,emit", [
    ("claude", Emit((), 0, 1, claude_error="registry timeout")),
    ("codex", Emit((), 0, 1, codex_error="lock read timeout")),
    ("claude", Emit((), 1, 1)),
])
def test_current_reader_failure_blocks_only_affected_provider(tmp_path, agent, emit):
    store = Store(tmp_path / "store")
    layout = store.write_capture(
        sessions=(Session("saved", "C:/fixture", "saved"),),
        captured_at=1, boot_id=1,
    ).layout
    candidates = Rows((row("claude"), row("codex")),
                      (row(agent, session_id="young", young=True),))
    paths = (tmp_path / "history.jsonl", tmp_path / "claude")
    checked = cli._join_rows(layout, emit, store, *paths, lambda *_: candidates)
    by_agent = {r.agent: r for r in checked.desk}
    healthy = "codex" if agent == "claude" else "claude"
    assert not by_agent[agent].selectable
    assert by_agent[healthy].selectable
    assert not checked.young[0].selectable
    assert picker.initial_state(checked).selected == frozenset({healthy})

    output = StringIO()
    assert cli.dry_run(store, emit_runner=lambda: emit, clock=lambda: 2,
                       history_path=paths[0], claude_dir=paths[1],
                       row_builder=lambda *_: candidates, out=output) == 0
    text = output.getvalue()
    assert f"Current {agent} live check failed:" in text
    assert f"[ ] {agent} " in text
    assert f"[x] {healthy} " in text


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_direct_restore_cannot_bypass_failed_live_check(tmp_path, agent):
    candidate = row(agent)
    emit = replace(Emit((), 0, 1), **{f"{agent}_error": "reader unavailable"})
    forbidden = Mock(side_effect=AssertionError("Must not read or launch"))
    store = Store(tmp_path / "store")
    result = restore.restore_selected(
        frozenset({candidate.session_id}), Rows((candidate,), ()), store, emit,
        run=False, session_file=forbidden, cwd_exists=forbidden,
        codex_lookup=forbidden, spawner=forbidden, observe=forbidden,
        sleep=forbidden, emit_runner=forbidden, clock=lambda: 2, out=StringIO(),
    )
    assert not result.spawned
    assert len(result.skipped) == 1
    assert "live check failed" in result.skipped[0].reason
    forbidden.assert_not_called()
    assert not store.root.exists()


def test_semicolon_working_directory_is_skipped_before_launch():
    candidate = row(cwd="C:/fixture;project")
    exists = Mock(side_effect=AssertionError("Unsupported directory"))
    ready, skipped = restore.preflight([candidate], lambda _: Path("fake"), exists)
    assert not ready
    assert "semicolons" in skipped[0].reason
    exists.assert_not_called()


@pytest.mark.parametrize("cwd,script", [
    ("C:/fixture;project", "C:/tool/arm.ps1"),
    ("C:/fixture", "C:/tool;copy/arm.ps1"),
])
def test_argv_builder_rejects_semicolons_in_paths(cwd, script):
    with pytest.raises(ValueError, match="semicolons"):
        restore.build_wt_argv(row(cwd=cwd), Path(script), False, 2,
                              window_name="desk-fixture-1")
