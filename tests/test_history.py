"""Behaviour tests for history.py — pure logic over ~/.claude/history.jsonl (§3.5, §7).

Fixtures are built here, one JSON object per line, matching the real file's shape:
`display`, `pastedContents`, `timestamp`, `project`, `sessionId`. Never reads the
user's real history.jsonl — a fixture must pass on any machine.
"""

import json

from termsync.history import load_history, sessions_active_after


def write_history(tmp_path, records):
    """Write `records` as a history.jsonl fixture and return its path."""
    path = tmp_path / "history.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(record) + "\n" for record in records)
    return path


def entry(session_id, display, timestamp, project=r"C:\fixture\demo_project"):
    return {
        "display": display,
        "pastedContents": {},
        "timestamp": timestamp,
        "project": project,
        "sessionId": session_id,
    }


def test_prompt_count_and_last_prompt_per_session(tmp_path):
    path = write_history(
        tmp_path,
        [
            entry("abc", "first message", 1000),
            entry("abc", "second message", 2000),
            entry("def", "only message", 1500),
        ],
    )

    entries = load_history(path)

    assert entries["abc"].prompt_count == 2
    assert entries["abc"].last_prompt == "second message"
    assert entries["def"].prompt_count == 1
    assert entries["def"].last_prompt == "only message"


def test_last_prompt_at_is_coerced_from_the_string_form(tmp_path):
    """`timestamp` is coerced to int defensively, not because real records need it:
    measured over all 12,601 records in the real history.jsonl, every `timestamp`
    is a JSON int and none are strings. The coercion guards a shape that has not
    been observed, not one that has."""
    path = write_history(
        tmp_path,
        [
            entry("abc", "first message", "1000"),
            entry("abc", "second message", "2000"),
        ],
    )

    entries = load_history(path)

    assert entries["abc"].last_prompt_at == 2000
    assert isinstance(entries["abc"].last_prompt_at, int)


def test_sessions_active_after_a_timestamp(tmp_path):
    """The "started since the last snapshot" bucket (§7 step 3)."""
    path = write_history(
        tmp_path,
        [
            entry("stale", "before the snapshot", 1000),
            entry("fresh", "after the snapshot", 5000),
        ],
    )
    entries = load_history(path)

    active = sessions_active_after(entries, after_ts=3000, bg_session_ids=frozenset())

    assert [e.session_id for e in active] == ["fresh"]


def test_a_bg_session_is_excluded_even_though_history_cannot_tell_it_apart(tmp_path):
    """history.jsonl has no `kind` field (§7 step 3) — bg ids must come from the caller.

    A bg session with history entries looks exactly like an interactive one in this
    file. Offering it in the "started since the last snapshot" bucket would open a
    tab that never existed, so exclusion is by session id. The only place `kind`
    exists is `-Emit`'s per-entry output (§5 step 3) — not the layout store, which
    has no `kind` field at all — so the caller here stands in for that source.
    """
    path = write_history(
        tmp_path,
        [
            entry("bg-session", "forked background prompt", 5000),
            entry("real-session", "interactive prompt", 5000),
        ],
    )
    entries = load_history(path)

    active = sessions_active_after(entries, after_ts=1000, bg_session_ids={"bg-session"})

    assert [e.session_id for e in active] == ["real-session"]


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    """A file appended to constantly by a killable process can end mid-write.

    One truncated line must not lose the other, well-formed lines around it.
    """
    path = tmp_path / "history.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry("before", "a fine prompt", 1000)) + "\n")
        f.write('{"display": "cut off mid-writ\n')  # truncated, invalid JSON
        f.write(json.dumps(entry("after", "another fine prompt", 2000)) + "\n")

    entries = load_history(path)

    assert set(entries) == {"before", "after"}
    assert entries["after"].last_prompt == "another fine prompt"


def test_a_non_utf8_byte_sequence_is_skipped_not_fatal(tmp_path):
    """A torn write can land mid-multibyte-sequence, not just mid-JSON (H3).

    `for line in f` decodes at the loop header under strict UTF-8, so a
    UnicodeDecodeError there escapes load_history entirely unless the file is
    opened with errors="replace" — the mangled line then fails json.loads and
    is skipped by the existing handler, same as a torn JSON line.
    """
    path = tmp_path / "history.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry("before", "a fine prompt", 1000)) + "\n")
    with open(path, "ab") as f:
        f.write(b"\xff\xfe not valid utf-8 \x00\n")  # invalid byte sequence
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry("after", "another fine prompt", 2000)) + "\n")

    entries = load_history(path)

    assert set(entries) == {"before", "after"}
    assert entries["after"].last_prompt == "another fine prompt"


def test_cwd_is_taken_from_the_record_that_wins_as_last_prompt(tmp_path):
    """HistoryEntry.cwd tracks where the session MOST RECENTLY ran, not its
    first-seen cwd -- the two records below carry different `project` values,
    and the later-timestamped one must win, regardless of line order in the
    file."""
    path = write_history(
        tmp_path,
        [
            entry("abc", "first message", 1000, project=r"C:\fixture\old_dir"),
            entry("abc", "second message", 2000, project=r"C:\fixture\new_dir"),
        ],
    )

    entries = load_history(path)

    assert entries["abc"].cwd == r"C:\fixture\new_dir"


def test_a_missing_project_on_the_winning_record_does_not_skip_the_line(tmp_path):
    """The tolerance load_history already has for a torn record (line 842 of
    the real file) must extend to `project`: a record with no `project` key
    at all must still count toward prompt_count and may still win as the last
    prompt -- it just leaves `cwd` unknown (`None`) rather than raising or
    being dropped."""
    path = tmp_path / "history.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(entry("abc", "first message", 1000)) + "\n")
        # A record with sessionId/display/timestamp but NO `project` key --
        # the shape of the genuine torn record this tolerance protects.
        f.write(
            json.dumps(
                {
                    "display": "second message",
                    "pastedContents": {},
                    "timestamp": 2000,
                    "sessionId": "abc",
                }
            )
            + "\n"
        )

    entries = load_history(path)

    assert entries["abc"].prompt_count == 2
    assert entries["abc"].last_prompt == "second message"
    assert entries["abc"].cwd is None


def test_a_blank_project_on_the_winning_record_is_treated_as_unknown(tmp_path):
    """A present-but-empty `project` string is the same "unknown" case as a
    missing key -- both must leave `cwd` as `None`, never an empty string
    that would silently pass a truthiness check downstream."""
    path = write_history(
        tmp_path,
        [entry("abc", "only message", 1000, project="   ")],
    )

    entries = load_history(path)

    assert entries["abc"].cwd is None


def test_load_history_on_a_missing_file_returns_empty(tmp_path):
    """A machine where Claude Code has never run has no history.jsonl at all —
    that is a normal empty state, not a fatal one."""
    path = tmp_path / "does-not-exist" / "history.jsonl"

    assert load_history(path) == {}
