"""Pure logic over ~/.claude/history.jsonl (§3.5, §7 steps 3-4).

`history.jsonl` is the fallback for sessions started since the last snapshot: it has
no `capturedAt`-style boundary of its own, so `desk` reads it once per run and
aggregates per session — prompt count, last prompt text, and last prompt timestamp.

The file is read exactly once (`load_history`): it can reach several MB, and a
restore needs several facts from it at the same time, so five separate re-reads
would be five times the I/O for no benefit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HistoryEntry:
    """Per-session aggregate over every prompt line in history.jsonl."""

    session_id: str
    prompt_count: int
    last_prompt: str
    last_prompt_at: int
    # The on-disk key is `project`, not `cwd` -- renamed here to match this
    # codebase's own vocabulary (Session.cwd, Layout row `cwd`, etc). It is an
    # absolute working directory (synthetic example: `C:\fixture\demo_project`).
    # This field shape was verified against a 4,000-line history.jsonl sample (100% of sampled
    # lines carry `project`, `display`, `timestamp`, `sessionId`).
    #
    # Taken from the SAME record that wins as "last prompt" (the record with
    # the greatest `timestamp` seen for this session), so it reflects where the
    # session most recently ran -- not its first-ever cwd, which could be a
    # worktree or subdirectory the session has since moved on from. A young
    # session (rows.py) has no other source for its cwd: it was never
    # captured, so the layout carries no Session for it at all.
    #
    # `None` when the winning record's `project` is missing or blank -- this
    # is `load_history`'s pre-existing tolerance (a genuine torn record was
    # observed at line 842 of the real file) applied to this field too: a
    # missing/blank `project` on the winning record must not skip the line
    # (it still counts toward `prompt_count` and can still win as the last
    # prompt), it just leaves the cwd unknown rather than fabricating one.
    cwd: str | None = None


def load_history(path: Path) -> dict[str, HistoryEntry]:
    """Read `path` once, aggregating one HistoryEntry per sessionId.

    Lines are append-ordered in the real file, but ordering is not assumed here:
    the entry that wins as "last" is the one with the greatest timestamp seen.

    A line that fails to parse, or parses but is missing a required field, is
    skipped rather than raised: the file is appended to continuously by a process
    that can be killed mid-write, so a truncated final line is a normal event,
    not an exceptional one, and it must not lose every other line in the file.
    That includes a torn write that lands mid-multibyte-sequence rather than
    mid-JSON: the file is opened with errors="replace" so decoding itself never
    raises — a mangled line decodes to replacement characters, fails
    json.loads, and is skipped by the same handler as a truncated JSON line.

    A missing file (a machine where Claude Code has never run) is likewise a
    normal empty state, not an exceptional one: returns `{}` rather than
    letting `FileNotFoundError` escape.
    """
    state: dict[str, dict] = {}

    if not path.exists():
        return {}

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
                session_id = record["sessionId"]
                display = record["display"]
                timestamp = int(record["timestamp"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

            bucket = state.setdefault(
                session_id,
                {"count": 0, "last_prompt": "", "last_prompt_at": None, "cwd": None},
            )
            bucket["count"] += 1
            if bucket["last_prompt_at"] is None or timestamp >= bucket["last_prompt_at"]:
                bucket["last_prompt"] = display
                bucket["last_prompt_at"] = timestamp
                # `project` is read loosely (`.get`, never a required key) so a
                # missing/blank value on the winning record does not raise and
                # does not skip the line -- see HistoryEntry.cwd's docstring.
                project = record.get("project")
                bucket["cwd"] = project if isinstance(project, str) and project.strip() else None

    return {
        session_id: HistoryEntry(
            session_id=session_id,
            prompt_count=bucket["count"],
            last_prompt=bucket["last_prompt"],
            last_prompt_at=bucket["last_prompt_at"],
            cwd=bucket["cwd"],
        )
        for session_id, bucket in state.items()
    }


def sessions_active_after(
    entries: dict[str, HistoryEntry],
    after_ts: int,
    bg_session_ids: frozenset[str],
) -> list[HistoryEntry]:
    """Sessions with a prompt after `after_ts` — the "started since the last
    snapshot" bucket (§7 step 3). Checking `last_prompt_at` alone is sufficient:
    it is the max timestamp seen for the session, so if any prompt is after
    `after_ts`, `last_prompt_at` is too.

    `history.jsonl` carries no `kind` field, so a background session's prompts
    look identical to an interactive session's. `bg_session_ids` — the set of
    ids ever recorded as `kind: "bg"` — must come from the caller, and the only
    place that field exists is `-Emit`'s per-entry `kind` output (§5 step 3):
    `model.py`'s `Session` has no `kind`, and §5 step 7 drops non-interactive
    entries before they ever reach the store, so the store cannot be the
    source. There is no default: a caller with no bg set must say so
    deliberately (an empty `frozenset()`), rather than this function silently
    treating "not supplied" as "there are none."
    """
    return [
        entry
        for entry in entries.values()
        if entry.last_prompt_at > after_ts and entry.session_id not in bg_session_ids
    ]
