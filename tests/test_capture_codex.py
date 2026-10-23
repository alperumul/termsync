"""Codex identity policy against isolated metadata and injected Windows reads."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from termsync import capture_codex as codex

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


def process(pid=10, *args, start="123"):
    return {"pid": pid, "procStart": start, "argv": ["codex.exe", *args], "console": True}


def thread(thread_id=A, **overrides):
    return {"id": thread_id, "source": "cli", "thread_source": "user",
            "cwd": "\\\\?\\D:\\work", "name": "My thread", **overrides}


def lock(thread_id=A, pid=10, start="123"):
    return {"path": thread_id + ".lock", "holders": [{"pid": pid, "procStart": start}]}


@pytest.mark.parametrize("args,kind", [
    ([], "interactive"), (["resume", A], "interactive"), (["fork", A], "interactive"),
    (["-C", "D:\\a folder", "--model=some-model", "a prompt"], "interactive"),
    (["-m", "model", "exec", "prompt"], "noninteractive"),
    (["--config", 'x="exec"', "resume", A], "interactive"),
    (["exec", "resume", A], "noninteractive"), (["app-server"], "noninteractive"),
    (["agents"], "noninteractive"), (["--remote", "localhost"], "remote"),
    (["resume", A, "--remote=localhost"], "remote"), (["--unknown-option"], "unknown"),
    (["-m"], "unknown"), (["--help"], "noninteractive"),
    (["--", "exec"], "interactive"),
])
def test_command_classification(args, kind):
    assert codex.classify_argv(["codex.exe", *args])[0] == kind


def test_resume_flags_preserve_values_and_last_override_only():
    assert codex.classify_argv([
        "codex.exe", "-m", "old", "resume", A, "--model=new", "-s", "read-only",
        "-a", "on-request", "-c", "secret=value",
    ]) == ("interactive", ("-m", "new", "-s", "read-only", "-a", "on-request"))


def test_two_sessions_in_same_folder_keep_distinct_ids_and_index_names():
    sessions, errors = codex.join_sessions(
        [process(10), process(20)], {A: thread(), B: thread(B)},
        [lock(), lock(B, 20)], {A: "First", B: "Second"},
    )
    assert errors == []
    assert [(s["sessionId"], s["cwd"], s["title"]) for s in sessions] == [
        (A, "D:\\work", "First"), (B, "D:\\work", "Second"),
    ]


@pytest.mark.parametrize("source,thread_source", [
    ("exec", "user"), ("vscode", "user"), ({"subagent": {}}, "user"),
    ("cli", "guardian_review"), ("cli", "subagent"),
])
def test_noninteractive_metadata_is_never_restorable(source, thread_source):
    sessions, errors = codex.join_sessions(
        [process()], {A: thread(source=source, thread_source=thread_source)}, [lock()], {},
    )
    assert sessions == []
    assert errors  # the actual interactive process could not be mapped


@pytest.mark.parametrize("owners", [[], [{"pid": 10, "procStart": "old"}],
                                     [{"pid": 99, "procStart": "123"}]])
def test_lock_presence_or_reused_pid_never_proves_liveness(owners):
    sessions, errors = codex.join_sessions(
        [process()], {A: thread()}, [{"path": A + ".lock", "holders": owners}], {},
    )
    assert sessions == []
    assert errors


def test_exec_holder_of_a_cli_origin_thread_is_excluded():
    sessions, errors = codex.join_sessions([process(10, "exec")], {A: thread()}, [lock()], {})
    assert sessions == []
    assert errors == []


def test_two_cli_locks_on_one_process_are_ambiguous_even_with_newest_timestamp():
    sessions, errors = codex.join_sessions(
        [process()], {A: thread(updated_at_ms=1), B: thread(B, updated_at_ms=999)},
        [lock(), lock(B)], {},
    )
    assert sessions == []
    assert "found 2" in errors[0]


def test_guardian_lock_does_not_compete_with_main_cli_thread():
    sessions, errors = codex.join_sessions(
        [process()], {A: thread(), B: thread(B, source='{"subagent":{}}')},
        [lock(), lock(B)], {},
    )
    assert len(sessions) == 1 and sessions[0]["sessionId"] == A
    assert errors == []


def test_archived_state_labels_and_known_options_are_preserved():
    sessions, errors = codex.join_sessions(
        [process(10, "-m", "model")],
        {A: thread(name=None, title="long " * 100, archived=1, thread_source=None)}, [lock()], {},
    )
    assert errors == []
    assert sessions[0]["archived"] is True
    assert sessions[0]["resumeArgs"] == ["-m", "model"]
    assert len(sessions[0]["title"]) == 90


def test_console_attachment_is_required():
    sessions, errors = codex.join_sessions(
        [{**process(), "console": False}], {A: thread()}, [lock()], {},
    )
    assert sessions == [] and "console" in errors[0]


def test_unc_prefix_is_preserved_as_unc():
    assert codex.normalize_cwd("\\\\?\\UNC\\server\\share") == "\\\\server\\share"


def make_database(home: Path, number=5):
    connection = sqlite3.connect(home / f"state_{number}.sqlite")
    connection.execute("CREATE TABLE threads (id TEXT, source TEXT, cwd TEXT, name TEXT)")
    connection.execute("INSERT INTO threads VALUES (?, 'cli', 'D:\\work', 'Database name')", (A,))
    connection.commit()
    connection.close()


def test_database_is_readonly_and_highest_numeric_schema_is_used(tmp_path):
    make_database(tmp_path, 9)
    make_database(tmp_path, 10)
    assert codex._database(tmp_path, "state").name == "state_10.sqlite"
    connection = codex._connect(tmp_path / "state_10.sqlite", time.monotonic() + 2)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("DELETE FROM threads")
    connection.close()
    rows = codex.read_threads(tmp_path, [A], time.monotonic() + 2)
    assert rows[A]["name"] == "Database name"
    assert codex.is_cli_thread(rows[A])  # old schema omits thread_source


def test_capture_prefilters_locks_and_reads_latest_index_name(tmp_path):
    make_database(tmp_path)
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    (locks / (A + ".lock")).touch()
    (locks / ".coordination.lock").touch()
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": A, "thread_name": "Old"}) + "\n"
        + json.dumps({"id": A, "thread_name": "Latest; name"}) + "\n",
    )
    calls = []

    def observe(mode, paths, timeout):
        assert 0 < timeout <= 15
        calls.append((mode, paths))
        return [process()] if mode == "Processes" else [{**lock(), "path": paths[0]}]

    report = codex.capture(tmp_path, observe)
    assert report["errors"] == []
    assert report["sessions"][0]["title"] == "Latest\uff1b name"
    assert calls[1] == ("Locks", [str(locks / (A + ".lock"))])


def test_known_exec_only_desk_does_not_read_database_or_locks(tmp_path):
    def observe(mode, paths, timeout):
        assert mode == "Processes"
        return [process(10, "exec")]
    assert codex.capture(tmp_path, observe) == {"sessions": [], "errors": []}


def test_missing_locks_is_degraded_not_healthy_empty_capture(tmp_path):
    report = codex.capture(tmp_path, lambda *args: [process()])
    assert report["sessions"] == [] and report["errors"]


def test_rollout_header_plain_and_compressed_are_bounded(tmp_path):
    payload = {"type": "session_meta", "payload": thread()}
    raw = json.dumps(payload).encode() + b"\n" + b"unread conversation body"
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(raw)
    assert codex.read_header(path) == thread()
    zstd = pytest.importorskip("compression.zstd")
    compressed = tmp_path / "rollout.jsonl.zst"
    compressed.write_bytes(zstd.compress(raw))
    assert codex.read_header(compressed) == thread()
    path.write_bytes(b"x" * (codex.MAX_HEADER_BYTES + 1))
    with pytest.raises(ValueError, match="budget"):
        codex.read_header(path)


def test_name_index_budget_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(codex, "MAX_INDEX_BYTES", 10)
    (tmp_path / "session_index.jsonl").write_text('x' * 11)
    with pytest.raises(ValueError, match="budget"):
        codex.read_names(tmp_path, {A})


def test_worker_timeout_propagates_without_launching_real_processes(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        assert kwargs["timeout"] == 20
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        raise subprocess.TimeoutExpired(argv, 20)
    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        codex.run_worker(tmp_path)


def test_recent_logs_can_flag_disagreement_but_old_pid_logs_cannot(tmp_path):
    make_database(tmp_path)
    connection = sqlite3.connect(tmp_path / "state_5.sqlite")
    connection.execute("INSERT INTO threads VALUES (?, 'cli', 'D:\\work', 'Second')", (B,))
    connection.commit()
    connection.close()
    connection = sqlite3.connect(tmp_path / "logs_2.sqlite")
    connection.execute("CREATE TABLE logs (thread_id TEXT, ts INTEGER, process_uuid TEXT)")
    connection.execute("INSERT INTO logs VALUES (?, 100, 'pid:10:old')", (B,))
    connection.commit()
    start = str((11_644_473_600 + 200) * 10_000_000)
    session = {"sessionId": A, "pid": 10, "procStart": start}
    assert codex.corroborate_logs(tmp_path, [session], time.monotonic() + 2) == []
    connection.execute("INSERT INTO logs VALUES (?, 201, 'pid:10:new')", (B,))
    connection.commit()
    connection.close()
    assert "disagree" in codex.corroborate_logs(tmp_path, [session], time.monotonic() + 2)[0]


def test_windows_boundary_rejects_ps51_nested_array_shape(monkeypatch):
    paths = [A + ".lock", B + ".lock"]

    def fake_run(argv, **kwargs):
        assert json.loads(kwargs["input"]) == paths
        return subprocess.CompletedProcess(
            argv, 0, json.dumps([{"path": " ".join(paths), "holders": []}]).encode(), b"",
        )
    monkeypatch.setattr(codex.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="combined"):
        codex.windows_observations("Locks", paths, 5)
