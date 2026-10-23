"""Independent U10 regressions: damaged metadata must disable only its row."""
import json
import sqlite3

import pytest

from termsync import capture_codex, rows
from termsync.model import Layout, Session
from termsync.restore_codex import lookup_codex


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def damaged_metadata(tmp_path, damage):
    path = tmp_path / "rollout.jsonl"
    data = b"[]\n"
    if damage == "json-payload-array":
        data = b'{"type": "session_meta", "payload": []}\n'
    if damage not in ("json-array", "json-payload-array"):
        from compression import zstd

        path = path.with_suffix(".jsonl.zst")
        data = b"not a zstd stream"
        if damage == "truncated-zstd":
            raw = json.dumps({"type": "session_meta", "payload": {
                "id": SESSION_ID, "source": "cli", "cwd": str(tmp_path),
            }}).encode() + b"\n"
            data = zstd.compress(raw)[:8]
    path.write_bytes(data)
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id, source, cwd, rollout_path)")
        connection.execute("INSERT INTO threads VALUES (?, 'cli', ?, ?)",
                           (SESSION_ID, str(tmp_path), str(path)))


@pytest.mark.parametrize("damage", [
    "json-array", "json-payload-array", "invalid-zstd", "truncated-zstd",
])
def test_u10_damaged_codex_rollout_is_unavailable_without_raising(tmp_path, damage):
    damaged_metadata(tmp_path, damage)
    result = lookup_codex(SESSION_ID, tmp_path)
    assert result.path is None
    assert result.reason


def test_u10_bad_codex_metadata_does_not_block_healthy_claude_row(tmp_path):
    damaged_metadata(tmp_path, "truncated-zstd")
    layout = Layout(1, "capture", 1, 1, "unused", (
        Session("healthy-claude", str(tmp_path), "Healthy Claude"),
        Session(SESSION_ID, str(tmp_path), "Damaged Codex", agent="codex"),
    ))
    result = rows.build_rows(
        layout, {}, frozenset(), frozenset(), lambda _: tmp_path / "claude.jsonl",
        lambda _: True, codex_lookup=lambda sid: lookup_codex(sid, tmp_path),
    )
    assert result.desk[0].selectable
    assert not result.desk[1].selectable
    assert result.desk[1].unavailable_reason


@pytest.mark.parametrize("bad_record", [
    [], None, {"id": SESSION_ID, "thread_name": 123}, {"id": [], "thread_name": "name"},
])
def test_u10_bad_name_index_retains_exactly_owned_codex_session(tmp_path, bad_record):
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id, source, cwd, name)")
        connection.execute("INSERT INTO threads VALUES (?, 'cli', ?, 'Database name')",
                           (SESSION_ID, str(tmp_path)))
    lock_dir = tmp_path / "thread-writer-locks"
    lock_dir.mkdir()
    (lock_dir / (SESSION_ID + ".lock")).touch()
    (tmp_path / "session_index.jsonl").write_text(json.dumps(bad_record) + "\n")

    def observe(mode, paths, timeout):
        if mode == "Processes":
            return [{"pid": 10, "procStart": "123", "argv": ["codex.exe"], "console": True}]
        return [{"path": paths[0], "holders": [{"pid": 10, "procStart": "123"}]}]

    result = capture_codex.capture(tmp_path, observe)
    assert [row["sessionId"] for row in result["sessions"]] == [SESSION_ID]
    assert result["sessions"][0]["title"] == "Database name"
    assert result["errors"]
