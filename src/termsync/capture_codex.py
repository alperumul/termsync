"""Bounded, read-only Codex metadata + exact Windows lock-owner capture.

The worker prints one JSON object and never writes a store or launches an
agent. Windows observations are delegated to Read-Codex.ps1; all eligibility
and joining decisions live here. No title/cwd/recency-based liveness guesses.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from termsync.titles import normalize_title

SCRIPT = Path(__file__).resolve().parents[2] / "snapshot" / "Read-Codex.ps1"
WORKER_LIMIT_SECONDS = 20
READ_BUDGET_SECONDS = 15
MAX_LOCKS = 128
MAX_HEADER_BYTES = 128 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
_VALUE_OPTIONS = {
    "-c", "--config", "--enable", "--disable", "-i", "--image", "-m", "--model",
    "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
    "--add-dir", "-a", "--ask-for-approval", "--remote", "--remote-auth-token-env",
}
_BOOLEAN_OPTIONS = {
    "--no-alt-screen", "--no-daemon", "--full-auto", "--search", "--oss",
    "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--all", "--last",
}
_NONINTERACTIVE = {
    "exec", "e", "review", "login", "logout", "mcp", "mcp-server", "plugin",
    "app-server", "remote-control", "app", "completion", "update", "doctor",
    "sandbox", "debug", "apply", "a", "queue", "archive", "delete", "migrate-rollouts",
    "unarchive", "cloud", "exec-server", "features", "help", "agents",
}
_RESTORE_OPTIONS = {
    "-m": "-m", "--model": "-m", "-s": "-s", "--sandbox": "-s",
    "-a": "-a", "--ask-for-approval": "-a",
}


def classify_argv(argv: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """Classify actual CommandLineToArgvW tokens, never split on whitespace."""
    if not argv or Path(argv[0]).name.lower() != "codex.exe":
        return "unknown", ()
    args: dict[str, str] = {}
    command: str | None = None
    index = 1
    while index < len(argv):
        token = argv[index]
        option, sep, inline = token.partition("=")
        if token == "--":
            break  # remaining tokens are the prompt
        if option in ("--remote", "--remote-auth-token-env"):
            return "remote", ()
        if option in ("-h", "--help", "-V", "--version"):
            return "noninteractive", ()
        if option in _VALUE_OPTIONS:
            if not sep:
                index += 1
                if index == len(argv):
                    return "unknown", ()
                inline = argv[index]
            if option in _RESTORE_OPTIONS:
                args[_RESTORE_OPTIONS[option]] = inline
        elif token in _BOOLEAN_OPTIONS:
            pass
        elif token.startswith("-"):
            return "unknown", ()
        elif command is None:
            command = token
            if command in _NONINTERACTIVE:
                return "noninteractive", ()
            if command not in ("resume", "fork"):
                break  # a positional prompt starts a local TUI
        # After resume/fork, positional tokens are an id/name/prompt. Options
        # still follow, so keep parsing instead of guessing the resume id.
        index += 1
    return "interactive", tuple(value for pair in args.items() for value in pair)


def normalize_cwd(cwd: str) -> str:
    if cwd.startswith("\\\\?\\UNC\\"):
        return "\\\\" + cwd[8:]
    return cwd[4:] if cwd.startswith("\\\\?\\") else cwd


def is_cli_thread(row: dict[str, Any]) -> bool:
    return row.get("source") == "cli" and row.get("thread_source") in (None, "user")


def windows_observations(mode: str, paths: Sequence[str], timeout: float) -> list[dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(SCRIPT), "-Mode", mode],
        input=json.dumps(list(paths)).encode("utf-8"), capture_output=True,
        timeout=timeout, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Codex Windows reader failed (exit {result.returncode})")
    payload = json.loads(result.stdout.decode("utf-8-sig"))
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("Codex Windows reader returned an invalid array")
    if mode == "Locks" and (len(payload) != len(paths)
                            or {row.get("path") for row in payload} != set(paths)):
        raise ValueError("Codex lock reader changed or combined requested paths")
    return payload


def _database(home: Path, prefix: str) -> Path | None:
    matches = []
    for path in home.glob(f"{prefix}_*.sqlite"):
        match = re.fullmatch(rf"{prefix}_(\d+)\.sqlite", path.name)
        if match:
            matches.append((int(match.group(1)), path))
    return max(matches)[1] if matches else None


def _connect(path: Path, deadline: float) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


def read_threads(home: Path, ids: Sequence[str], deadline: float) -> dict[str, dict]:
    path = _database(home, "state")
    if path is None:
        raise ValueError("Codex state database missing")
    connection = _connect(path, deadline)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if not {"id", "source", "cwd"}.issubset(columns):
            raise ValueError("Unsupported Codex threads schema")
        wanted = [name for name in (
            "id", "source", "thread_source", "cwd", "name", "first_user_message", "title",
            "rollout_path", "archived", "updated_at_ms",
        ) if name in columns]
        projection = [f"substr({name}, 1, 512) AS {name}"
                      if name in ("name", "first_user_message", "title") else name
                      for name in wanted]
        rows = connection.execute(
            "SELECT " + ",".join(projection) + " FROM threads WHERE id IN ("
            + ",".join("?" for _ in ids) + ")", list(ids),
        )
        return {row["id"]: dict(row) for row in rows}
    finally:
        connection.close()


def read_header(path: Path) -> dict | None:
    """At most 20 records / 128 KiB decompressed, including zstd rollouts."""
    stream_errors = (EOFError,)
    if path.suffix == ".zst":
        try:
            from compression import zstd
        except ImportError as exc:
            raise ValueError("Compressed Codex fallback needs Python zstd support") from exc
        stream_errors += (zstd.ZstdError,)
    try:
        stream = zstd.open(path, "rb") if path.suffix == ".zst" else path.open("rb")
        with stream:
            remaining = MAX_HEADER_BYTES
            for _ in range(20):
                line = stream.readline(remaining + 1)
                if not line:
                    break
                if len(line) > remaining:
                    raise ValueError("Codex rollout header exceeds read budget")
                remaining -= len(line)
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("Codex rollout record must be an object")
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        raise ValueError("Codex session metadata must be an object")
                    return payload
    except stream_errors as exc:
        # Both capture fallback and restore isolate ValueError per metadata read.
        raise ValueError("Codex rollout compressed stream is damaged") from exc
    return None


def fallback_thread(home: Path, thread_id: str) -> dict | None:
    paths = list((home / "sessions").glob(f"*/*/*/*-{thread_id}.jsonl*"))
    paths += list((home / "archived_sessions").glob(f"*-{thread_id}.jsonl*"))
    if len(paths) > 2:
        raise ValueError("Ambiguous Codex rollout fallback")
    for path in sorted(paths, key=lambda value: value.suffix == ".zst"):
        row = read_header(path)
        if isinstance(row, dict) and row.get("id") == thread_id:
            return {**row, "rollout_path": str(path),
                    "archived": path.parent.name == "archived_sessions"}
    return None


def read_names(home: Path, ids: set[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    path = home / "session_index.jsonl"
    if not path.exists():
        return names
    with path.open("rb") as stream:
        remaining = MAX_INDEX_BYTES
        while remaining > 0:
            line = stream.readline(min(65537, remaining + 1))
            if not line:
                return names
            if len(line) > min(65536, remaining):
                raise ValueError("Codex name index exceeds read budget")
            remaining -= len(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # a concurrent writer may leave an incomplete last line
            if not isinstance(row, dict):
                raise ValueError("Codex name index record must be an object")
            thread_id, name = row.get("id"), row.get("thread_name")
            if ((thread_id is not None and not isinstance(thread_id, str))
                    or (name is not None and not isinstance(name, str))):
                raise ValueError("Codex name index id and name must be strings")
            if thread_id in ids and name:
                names[thread_id] = name
    raise ValueError("Codex name index exceeds read budget")


def join_sessions(
    processes: Sequence[dict], threads: dict[str, dict], locks: Sequence[dict],
    names: dict[str, str],
) -> tuple[list[dict], list[str]]:
    """Exact, pure join with PID reuse and multiple-lock ambiguity guards."""
    interactive = {}
    errors = []
    for process in processes:
        if process.get("error"):
            errors.append("Codex process changed or was unreadable during capture")
            continue
        kind, resume_args = classify_argv(process.get("argv", []))
        if kind == "unknown":
            errors.append("Unrecognized Codex command line")
        if kind == "interactive":
            if not process.get("console"):
                errors.append("Interactive Codex process has no verifiable console")
                continue
            interactive[process["pid"]] = (process, resume_args)
    by_pid: dict[int, list[str]] = {}
    for lock in locks:
        thread_id = Path(lock["path"]).stem
        row = threads.get(thread_id)
        if row is None or not is_cli_thread(row):
            continue
        if lock.get("error"):
            errors.append(f"Could not read Codex lock ownership: {thread_id}")
            continue
        owners = [owner for owner in lock.get("holders", [])
                  if owner.get("pid") in interactive
                  and owner.get("procStart")
                  == interactive[owner["pid"]][0].get("procStart")]
        if len(owners) == 1:
            by_pid.setdefault(owners[0]["pid"], []).append(thread_id)
        elif owners:
            errors.append(f"Ambiguous Codex lock ownership: {thread_id}")
    sessions = []
    for pid, (process, resume_args) in interactive.items():
        ids = by_pid.get(pid, [])
        if len(ids) != 1:
            errors.append(f"Codex PID {pid}: expected one owned CLI thread, found {len(ids)}")
            continue
        thread_id = ids[0]
        row = threads[thread_id]
        cwd = normalize_cwd(row.get("cwd") or "")
        if not cwd:
            errors.append(f"Codex thread has no working directory: {thread_id}")
            continue
        title = names.get(thread_id) or row.get("name")
        if not title:
            title = " ".join((row.get("first_user_message") or row.get("title") or "").split())
            title = title[:87] + "..." if len(title) > 90 else title
        sessions.append({
            "sessionId": thread_id, "cwd": cwd, "title": normalize_title(title or thread_id),
            "agent": "codex", "archived": bool(row.get("archived", False)),
            "rolloutPath": row.get("rollout_path"), "resumeArgs": list(resume_args),
            "pid": pid, "procStart": process["procStart"],
        })
    return sessions, errors


def corroborate_logs(home: Path, sessions: Sequence[dict], deadline: float) -> list[str]:
    """Recent same-process logs can contradict ownership, never establish it.

    Logs are optional and may lag. Restrict rows to this process lifetime to
    avoid PID reuse; read identifiers/timestamps only, never message bodies.
    """
    path = _database(home, "logs")
    if path is None or not sessions:
        return []
    errors = []
    try:
        connection = _connect(path, deadline)
    except sqlite3.Error:
        return []
    try:
        for session in sessions:
            started_seconds = int(session["procStart"]) / 10_000_000 - 11_644_473_600
            rows = list(connection.execute(
                "SELECT thread_id, ts FROM logs WHERE process_uuid LIKE ? "
                "AND ts >= ? AND thread_id IS NOT NULL ORDER BY ts DESC LIMIT 32",
                (f"pid:{session['pid']}:%", int(started_seconds)),
            ))
            if not rows:
                continue
            metadata = read_threads(home, [row["thread_id"] for row in rows], deadline)
            latest_cli = next((row["thread_id"] for row in rows
                               if is_cli_thread(metadata.get(row["thread_id"], {}))), None)
            if latest_cli is not None and latest_cli != session["sessionId"]:
                errors.append(f"Codex logs disagree with lock owner for PID {session['pid']}")
    except (sqlite3.Error, ValueError):
        # Optional corroboration is not a fallback liveness source. The exact
        # lock match remains authoritative when logs are absent or unreadable.
        pass
    finally:
        connection.close()
    return errors


def capture(
    home: Path, observe: Callable = windows_observations,
) -> dict[str, Any]:
    deadline = time.monotonic() + READ_BUDGET_SECONDS

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Codex capture read budget exceeded")
        return value

    processes = observe("Processes", [], remaining())
    # Known noninteractive processes need neither databases nor Restart Manager.
    if all(not row.get("error") and classify_argv(row.get("argv", []))[0]
           in ("noninteractive", "remote") for row in processes):
        return {"sessions": [], "errors": []}
    paths = []
    for path in (home / "thread-writer-locks").glob("*.lock"):
        try:
            UUID(path.stem)
        except ValueError:
            continue
        paths.append(path)
        if len(paths) > MAX_LOCKS:
            raise ValueError("Codex lock count exceeds read budget")
    if not paths:
        return {"sessions": [], "errors": ["Codex processes exist but thread locks are missing"]}
    ids = [path.stem for path in paths]
    errors: list[str] = []
    try:
        threads = read_threads(home, ids, deadline)
    except (sqlite3.Error, ValueError) as exc:
        threads = {}
        errors.append(str(exc))
    for thread_id in ids:
        remaining()
        if thread_id not in threads:
            try:
                fallback = fallback_thread(home, thread_id)
                if fallback:
                    threads[thread_id] = fallback
            except (OSError, ValueError) as exc:
                errors.append(f"Codex metadata fallback: {exc}")
    candidates = [str(path) for path in paths if is_cli_thread(threads.get(path.stem, {}))]
    observations = observe("Locks", candidates, remaining()) if candidates else []
    try:
        names = read_names(home, set(threads))
    except (OSError, ValueError) as exc:
        names = {}
        errors.append(str(exc))
    sessions, join_errors = join_sessions(processes, threads, observations, names)
    errors += join_errors + corroborate_logs(home, sessions, deadline)
    return {"sessions": sessions, "errors": errors}


def run_worker(home: Path | None = None) -> dict:
    """A hard timeout isolates all Codex I/O from the Claude snapshot."""
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    result = subprocess.run(
        [str(python), "-m", "termsync.capture_codex", str(home or Path.home() / ".codex")],
        capture_output=True, timeout=WORKER_LIMIT_SECONDS, check=False,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(f"Codex capture worker failed (exit {result.returncode})")
    payload = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list) \
            or not isinstance(payload.get("errors"), list):
        raise ValueError("Invalid Codex capture worker output")
    return payload


if __name__ == "__main__":
    try:
        report = capture(Path(sys.argv[1]))
    except Exception as exc:
        report = {"sessions": [], "errors": [f"{type(exc).__name__}: {exc}"]}
    print(json.dumps(report, ensure_ascii=True))
