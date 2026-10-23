"""U6 regression tests. Real PowerShell gets only in-process fake CLI functions."""
import base64
import json
import os
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from termsync import cli, picker, restore, rows
from termsync.capture import build_capture, live_session_keys
from termsync.emit import Emit
from termsync.model import PendingEntry, Session
from termsync.restore_codex import CodexRestoreInfo, lookup_codex, validate_resume_args
from termsync.store import Store

A = "11111111-1111-4111-8111-111111111111"


def forbidden(*args):
    raise AssertionError("Codex reached Claude lookup")


def make_row(**changes):
    session = Session(A, r"D:\work", "Codex restore", agent="codex",
                      resume_args=("-m", "model", "-s", "read-only", "-a", "on-request"))
    row = rows._desk_row(session, {}, forbidden, lambda _: True, frozenset(),
                         lambda _: CodexRestoreInfo(Path("rollout.jsonl")))
    return replace(row, **changes)


def empty_emit():
    return Emit(sessions=(), claude_process_count=0, boot_time=1)


def write_metadata(home, *, archived=False, source="cli", header_id=A, compressed=False):
    path = home / "rollout.jsonl"
    data = json.dumps({"type": "session_meta", "payload": {
        "id": header_id, "source": source, "thread_source": "user", "cwd": str(home),
    }}).encode() + b"\n"
    if compressed:
        from compression import zstd
        path = path.with_suffix(".jsonl.zst")
        data = zstd.compress(data)
    path.write_bytes(data)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id, source, cwd, rollout_path, archived)")
        connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                           (A, source, str(home), str(path), int(archived)))
    return path


@pytest.mark.parametrize("archived,compressed", [(False, False), (True, False), (True, True)])
def test_current_metadata_and_compressed_rollout(tmp_path, archived, compressed):
    path = write_metadata(tmp_path, archived=archived, compressed=compressed)
    assert lookup_codex(A, tmp_path) == CodexRestoreInfo(path, archived)


@pytest.mark.parametrize("change,reason", [
    ({"source": "exec"}, "CLI session unavailable"),
    ({"header_id": "different"}, "identity mismatch"),
])
def test_ineligible_or_wrong_rollout_refused(tmp_path, change, reason):
    write_metadata(tmp_path, **change)
    assert reason in lookup_codex(A, tmp_path).reason


def test_deleted_rollout_is_not_restorable(tmp_path):
    write_metadata(tmp_path).unlink()
    assert lookup_codex(A, tmp_path).reason == "Codex rollout missing"


def test_header_fallback_without_database(tmp_path):
    directory = tmp_path / "archived_sessions"
    directory.mkdir()
    path = directory / f"rollout-date-{A}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": A, "source": "cli", "cwd": str(tmp_path),
    }}) + "\n")
    assert lookup_codex(A, tmp_path) == CodexRestoreInfo(path, True)


def test_old_pending_defaults_and_codex_pending_survives_capture_and_clears(tmp_path):
    assert PendingEntry.from_json({"sessionId": "old", "armedAt": 1}).agent == "claude"
    row = make_row()
    store = Store(tmp_path / "store")
    restore._record_pending(store, [row], False, 100)
    restore._record_restore(store, [row], False, 100, empty_emit())
    pending = store.read_pending()
    assert pending.entries[0].agent == "codex"
    assert pending.entries[0].resume_args == row.resume_args
    captured = build_capture(empty_emit(), pending).sessions[0]
    assert captured.agent == "codex" and captured.armed
    assert captured.resume_args == row.resume_args
    assert store.newest().sessions[0].agent == "codex"
    # Another provider's same ID must not clear this entry.
    assert store.expire_pending(101, frozenset({("claude", A)})) == pending
    observed = replace(empty_emit(), codex=(replace(captured, armed=False).to_json(),))
    assert cli.snap(store, emit_runner=lambda: observed, clock=lambda: 102) == 0
    assert store.read_pending() is None
    assert not store.newest().sessions[0].armed
    assert live_session_keys(observed) == {("codex", A)}


def test_archived_is_unchecked_but_explicit_selection_is_retained():
    row = make_row(archived=True)
    row_set = rows.Rows((row,), ())
    state = picker.initial_state(row_set)
    assert not state.selected
    assert picker.initial_state(row_set, frozenset({A})).selected == {A}
    text = "\n".join(picker.render(state, 0))
    assert "ARCHIVED CODEX SESSIONS" in text
    assert "Archived session; restoring it will unarchive it." in text
    assert cli._render_row(row, 0).startswith("[ ]")


def test_codex_history_never_uses_claude_history_of_same_id():
    # A sentinel makes any accidental Claude-history access visible.
    history = {A: object()}
    row = rows._desk_row(Session(A, "cwd", "title", agent="codex"), history,
                         forbidden, lambda _: True, frozenset({("claude", A)}),
                         lambda _: CodexRestoreInfo(Path("rollout.jsonl")))
    assert row.selectable and not row.live and row.prompt_count is None


def test_archive_changed_after_picker_requires_new_selection():
    row = make_row()
    ok, skipped = restore.preflight([row], forbidden, lambda _: True,
                                    lambda _: CodexRestoreInfo(Path("new"), True))
    assert not ok and "archived since selection" in skipped[0].reason


def test_missing_codex_file_or_cwd_fails_before_spawn(tmp_path):
    row = make_row()
    calls = []
    outcome = restore.restore_selected(
        frozenset({A}), rows.Rows((row,), ()), Store(tmp_path), empty_emit(),
        run=False, session_file=forbidden, cwd_exists=lambda _: True,
        codex_lookup=lambda _: CodexRestoreInfo(reason="Codex rollout missing"),
        spawner=calls.append,
    )
    assert not calls and not outcome.spawned
    ok, skipped = restore.preflight([row], forbidden, lambda _: False,
                                    lambda _: CodexRestoreInfo(Path("rollout")))
    assert not ok and skipped[0].reason == "cwd gone"


@pytest.mark.parametrize("args", [
    ("--last",), ("-m",), ("-C", "elsewhere"), ("-m", "--last"),
    ("-m", "one", "-m", "two"), ("-a", "never\nexec"),
])
def test_unknown_or_malformed_flags_rejected(args):
    with pytest.raises(ValueError):
        validate_resume_args(args)


def test_actual_launcher_receives_data_and_pending_records_only_spawned(tmp_path):
    row = make_row(title="title; quotes ' Gr\u00fc\u00dfe")
    store = Store(tmp_path / "store")
    calls = []
    result = restore.restore_selected(
        frozenset({A}), rows.Rows((row,), ()), store, empty_emit(), run=False,
        session_file=forbidden, cwd_exists=lambda _: True,
        codex_lookup=lambda _: CodexRestoreInfo(row.session_file), spawner=calls.append,
        sleep=lambda _: None, emit_runner=empty_emit,
    )
    assert result.spawned == (row,)
    argv = calls[0]
    assert "-Run" not in argv and "-Archived" not in argv
    assert not any(";" in arg for arg in argv)
    encoded = argv[argv.index("-ResumeArgsB64") + 1]
    assert json.loads(base64.b64decode(encoded)) == list(row.resume_args)
    assert store.read_pending().entries[0].resume_args == row.resume_args


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 boundary")
@pytest.mark.parametrize("archived,run,fail_unarchive,empty_args", [
    (False, False, False, False), (True, False, False, False),
    (True, True, True, False), (False, True, False, True),
])
def test_real_powershell_gate_and_argument_splat(
    tmp_path, archived, run, fail_unarchive, empty_args,
):
    """No real executable named codex/claude is reachable: functions shadow both."""
    cwd = tmp_path / "space ' Gr\u00fc\u00dfe"
    cwd.mkdir()
    args = ("-m", "model with space; ' literal", "-s", "read-only", "-a", "on-request")
    if empty_args:
        args = ()
    row = make_row(cwd=str(cwd), archived=archived, resume_args=args)
    argv = restore.build_wt_argv(row, restore.DEFAULT_ARM_SCRIPT, run, 0, window_name="desk-test-1")
    script_args = argv[argv.index("-SessionId"):]
    log = tmp_path / "calls.jsonl"
    fixture = tmp_path / "harness.ps1"
    fixture.write_text('''param(
    [string]$SessionId, [string]$TitleB64, [string]$Prompts, [string]$Age,
    [string]$Agent, [string]$CwdB64, [string]$ResumeArgsB64,
    [switch]$Archived, [switch]$Run
)
$ErrorActionPreference = 'Stop'
function codex {
    ConvertTo-Json -Compress -InputObject @($args) |
        Add-Content -Encoding UTF8 -LiteralPath $env:U6_LOG
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'unarchive' -and $env:U6_FAIL -eq '1') { $global:LASTEXITCODE = 9 }
}
function claude { throw 'Wrong provider invoked' }
function Read-Host {
    '"ENTER_GATE"' | Add-Content -Encoding UTF8 -LiteralPath $env:U6_LOG
}
& $env:U6_ARM @PSBoundParameters
''', encoding="utf-8-sig")
    env = {**os.environ, "U6_LOG": str(log), "U6_ARM": str(restore.DEFAULT_ARM_SCRIPT),
           "U6_FAIL": "1" if fail_unarchive else "0"}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(fixture),
                             *script_args], cwd=tmp_path, env=env,
                            capture_output=True, timeout=20)
    assert log.exists(), result.stderr.decode(errors="replace")
    events = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()]
    if not run:
        assert events.pop(0) == "ENTER_GATE", result.stderr.decode(errors="replace")
    if archived:
        assert events.pop(0) == ["unarchive", A], result.stderr.decode(errors="replace")
    if fail_unarchive:
        assert result.returncode != 0 and events == []
    else:
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert events == [["resume", A, "-C", str(cwd), *args]]


def test_preflight_refreshes_unarchived_path_without_repeating_unarchive():
    row = make_row(archived=True)
    ok, skipped = restore.preflight([row], forbidden, lambda _: True,
                                    lambda _: CodexRestoreInfo(Path("moved.jsonl"), False))
    assert not skipped
    assert ok == [replace(row, archived=False, session_file=Path("moved.jsonl"))]
    assert "-Archived" not in restore.build_wt_argv(ok[0], Path("arm.ps1"), False, 0,
        window_name="desk-test-1")


def test_verify_run_requires_the_correct_provider(tmp_path):
    row = make_row()
    claude = {"sessionId": A, "alive": True, "kind": "interactive",
              "procStart": "123", "procStartLive": "123"}
    observed = replace(empty_emit(), sessions=(claude,))
    lines, error = restore.verify(Store(tmp_path), [row], True, lambda: observed, lambda _: None, 0)
    assert lines[0] == "restored 0/1"
    assert error == "1/1 sessions not confirmed live at verification"


def test_bad_pending_provider_warns_instead_of_crashing(tmp_path):
    store = Store(tmp_path)
    store.pending_path.write_text(json.dumps({"writtenAt": 1, "entries": [{
        "sessionId": A, "armedAt": 1, "agent": "unknown",
    }]}))
    assert store.read_pending() is None
    assert store.read_warnings
