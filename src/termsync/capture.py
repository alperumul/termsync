"""The pure capture pipeline (§5 steps 5-9).

**Why this file exists, and why it is not folded into `cli.py`.** §13.2
describes `cli.py` as "arg parsing + dispatch", but §5's capture path is nine
policy steps carrying this project's sharpest correctness traps: the
`procStart` string-equality pid-reuse guard (§5 step 6, where a stray
`float()` silently disables the guard forever) and `readable_entry_count`
(D3/M9, the input to the primary error rule, which three of the four
plausible definitions get wrong). Putting that policy in `cli.py` would make
the CLI the widest module in the package -- the exact failure §13.2's own
design note on `rows.py` warns about: "Revision 3 had no owner for that
join... so the join would have landed by default in the riskiest module in
the plan, making it also the widest." (§13.2, and restated as D8 in §16.)
This module is a pure function of its arguments: no subprocess, no
filesystem, no clock. That is what makes every one of §5's filters
unit-testable with no Windows machine anywhere near the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from termsync.emit import Emit, EmitError
from termsync.capture_windows import placements
from termsync.model import Pending, Session, normalize_registry_versions
from termsync.titles import normalize_title


@dataclass(frozen=True)
class CaptureResult:
    """The desk `cli.py` hands to `store.write_capture`, plus what it took to build it."""

    sessions: tuple[Session, ...]
    title_fallbacks: int
    registry_versions: tuple[str, ...]

    # D3 / review finding M9: `len(emit.sessions)` -- the count of registry
    # files that *parsed*, taken BEFORE the liveness (§5 step 5), procStart
    # (§5 step 6) and kind (§5 step 7) filters below. This is the exact
    # quantity `health.is_registry_error` calls `readable_entry_count`; see
    # that function's docstring for why the other three plausible
    # definitions (alive-only, alive+procStart-matched, interactive-only)
    # each make the primary error rule fire on a legitimate state -- e.g.
    # every current session happening to be backgrounded would false-positive
    # if this were the interactive-filtered count instead.
    readable_entry_count: int
    window_count: int | None = None
    window_unknown_sessions: int = 0


def _proc_start_matches(entry: dict[str, Any]) -> bool:
    """§5 step 6's pid-reuse guard -- STRING equality, ordinal, exact.

    `procStart` is an 18-character decimal FILETIME string. No numeric
    conversion of any kind: an 18-digit FILETIME exceeds IEEE-754 double
    precision, so `float(a) == float(b)` can be True for two genuinely
    different values (measured -- see `test_procstart.py`'s float-collision
    case) and it can also be False for two identical strings compared through
    a lossy path. There is no tolerance window, ever: a mismatch means the
    registry entry describes a different, since-exited process reusing the
    same pid, and the correct response is to drop the entry, not to widen
    the comparison.

    A missing or `None` `procStartLive` -- the PowerShell side's H1 guard
    fallback when `.StartTime` itself could not be read -- fails this check.
    An unverifiable entry is not a verified one.
    """
    proc_start = entry.get("procStart")
    proc_start_live = entry.get("procStartLive")
    if proc_start_live is None:
        return False
    return proc_start == proc_start_live


def _is_title_fallback(entry: dict[str, Any]) -> bool:
    """D6: a title fallback is `titleAttachOk is False` OR an empty/whitespace `titleRaw`.

    Both halves land at the same place -- the registry's derived `name`
    field -- but they are not redundant with each other: `AttachConsole` can
    succeed against a console whose title has not been set yet, which is
    `titleAttachOk: true` with an empty `titleRaw`.
    """
    if entry.get("titleAttachOk") is False:
        return True
    title_raw = entry.get("titleRaw")
    return title_raw is None or title_raw.strip() == ""


def _surviving_entries(emit: Emit) -> Iterator[dict[str, Any]]:
    """§5 steps 5-7, applied in order, over `emit.sessions` verbatim.

    This is the SINGLE implementation of "the session survived liveness +
    procStart + kind filtering" -- both `build_capture` (below) and
    `live_session_ids` call this, rather than each carrying its own copy.
    Review finding M3 [MEDIUM]: `cli.py` used to build its own "live" set
    straight from the raw `alive` flag, over ALL entries (any `kind`, no
    `procStart` check), which is a DIFFERENT definition than the one
    `build_capture` carries into the layout. The two could disagree -- a pid
    reused by a new process (procStart mismatch) or a session reappearing
    as `kind: "bg"` -- and when they did, `expire_pending` would drop the
    session from `pending.json` on the raw definition while `build_capture`
    declined to carry it on the filtered one, in the same run: the armed
    session vanished from BOTH files at once. One filter, used everywhere
    "live" is asked, makes that disagreement structurally impossible.
    """
    for entry in emit.sessions:
        if not entry.get("alive", False):
            continue  # step 5
        if not _proc_start_matches(entry):
            continue  # step 6
        if entry.get("kind") != "interactive":
            continue  # step 7
        yield entry


def live_session_ids(emit: Emit) -> frozenset[str]:
    """§5 step 9 / §7 step 2's "the session appears live" -- for `cli.py`'s
    `expire_pending` call, and nowhere else.

    Built from `_surviving_entries`, the exact filter `build_capture` uses
    to decide what a run carries into the layout (see that function's
    docstring, and M3 above). `cli.py` calls this BEFORE `build_capture` in
    the §5 step sequence -- step 9's expiry precedes step 3's pending union
    -- so this has to be computable independently of `build_capture`'s
    result; it is the same small filter run a second time over `emit.sessions`
    (cheap: one short list, no I/O), not a value extracted from a
    `CaptureResult`. That keeps `build_capture` a pure function of exactly
    `(Emit, Pending)` with no side channel for "what did the caller already
    treat as live".

    A `sessionId`-less entry contributes nothing here (review finding M4 /
    F5): `.get` rather than a hard subscript, matching `build_capture`'s own
    defensive read of the same field. With F1's per-entry contract check at
    the `emit.py` boundary, a session missing `sessionId` should never reach
    this function in production -- this is the belt-and-braces half for any
    `Emit` a caller (or a test) constructs directly, bypassing `parse_emit`.
    """
    return frozenset(
        session_id for entry in _surviving_entries(emit) if (session_id := entry.get("sessionId"))
    ) | frozenset(session.session_id for session in _codex_sessions(emit))


def live_session_keys(emit: Emit) -> frozenset[tuple[str, str]]:
    """Provider-qualified liveness for pending reconciliation and restore."""
    return frozenset(
        ("claude", entry["sessionId"]) for entry in _surviving_entries(emit)
        if entry.get("sessionId")
    ) | frozenset((s.agent, s.session_id) for s in _codex_sessions(emit))


def _codex_sessions(emit: Emit) -> tuple[Session, ...]:
    sessions = tuple(replace(Session.from_json(row), window_ordinal=None, tab_index=None)
                     for row in emit.codex)
    if any(session.agent != "codex" for session in sessions):
        raise EmitError("Codex reader returned a non-Codex session")
    return sessions


def build_capture(emit: Emit, pending: Pending | None) -> CaptureResult:
    """§5 steps 5-9, applied in order, over `emit.sessions` verbatim.

    5. Drop entries where `alive` is false.
    6. Drop entries where `procStart != procStartLive` (`_proc_start_matches`).
    7. Keep only `kind == "interactive"` -- bg sessions own no tab.
       (Steps 5-7 are `_surviving_entries`, shared with `live_session_ids` --
       see M3 on that function.)
    8. Strip the title (`titles.strip_title`), falling back to the registry
       `name` on D6's rule, counting fallbacks over the sessions that
       survived steps 5-7 (a fallback on a dropped session is not part of
       this desk). If `name` is ALSO absent or empty, fall back to the
       session id rather than an empty string (review finding F9/L4) -- an
       empty title renders as a blank, unusable row in the picker, and the
       session id is at least a stable, non-empty label the run is already
       marked `degraded` over.
    9. Union `pending.json`: any pending entry whose session id was not
       already carried through steps 5-8 is added with `armed=True`. Pending
       entries carry their own title and cwd. Clearing an entry once its
       session comes back live, or after 24 hours, is `store.expire_pending`'s
       job (called by `cli.py` before this function, against `live_session_ids`
       -- not this function's filtered output) -- this function only ever
       ADDS pending entries that are still armed by the time it runs.
    """
    readable_entry_count = len(emit.sessions)

    # `registryVersions` is gathered from ALL readable entries (§6, D2/M8),
    # not just the ones that survive steps 5-7 below. The field exists to
    # detect a Claude Code version change across the whole registry; scoping
    # it to the interactive survivors would hide a version carried only by a
    # bg entry.
    registry_versions = normalize_registry_versions(entry.get("version") for entry in emit.sessions)

    sessions: list[Session] = []
    title_fallbacks = 0
    seen_ids: set[str] = set()
    entries = tuple(_surviving_entries(emit))
    positions, window_count = placements(entries, emit.desktop, emit.claude_error)

    for entry in entries:
        # Review finding F5 [MEDIUM]: a hard `entry["sessionId"]` subscript
        # here would raise a bare `KeyError` on a malformed entry, which is a
        # worse diagnostic than a named error -- and F1's boundary check in
        # `emit.py` is what should make this unreachable in production. This
        # is the defensive fallback for an `Emit` built directly (tests, or
        # a future caller that does not go through `parse_emit`).
        session_id = entry.get("sessionId")
        if session_id is None:
            raise EmitError(f"registry entry survived §5 steps 5-7 but has no sessionId: {entry!r}")

        if _is_title_fallback(entry):
            title = entry.get("name") or session_id  # step 8, F9/L4's fallback
            title_fallbacks += 1
        else:
            title = entry["titleRaw"]  # step 8

        sessions.append(
            Session(
                session_id=session_id,
                cwd=entry.get("cwd", ""),
                title=normalize_title(title),
                last_prompt_at=None,  # V0: the history join is S9's job
                armed=False,
                window_ordinal=positions.get(session_id, (None, None))[0],
                tab_index=positions.get(session_id, (None, None))[1],
            )
        )
        seen_ids.add(session_id)

    for session in _codex_sessions(emit):
        if session.session_id in seen_ids:
            raise EmitError("Conflicting Claude/Codex session identity")
        sessions.append(session)
        seen_ids.add(session.session_id)

    if pending is not None:
        for pending_entry in pending.entries:
            if pending_entry.session_id in seen_ids:
                if any(s.session_id == pending_entry.session_id and s.agent != pending_entry.agent
                       for s in sessions):
                    raise EmitError("Conflicting Claude/Codex pending identity")
                continue  # step 9: already carried through live -- no duplicate
            sessions.append(
                Session(
                    session_id=pending_entry.session_id,
                    cwd=pending_entry.cwd,
                    title=normalize_title(pending_entry.title),
                    last_prompt_at=None,
                    armed=True,
                    window_ordinal=None,
                    tab_index=None,
                    agent=pending_entry.agent,
                    archived=pending_entry.archived,
                    rollout_path=pending_entry.rollout_path,
                    resume_args=pending_entry.resume_args,
                )
            )
            seen_ids.add(pending_entry.session_id)

    return CaptureResult(
        sessions=tuple(sessions),
        title_fallbacks=title_fallbacks,
        registry_versions=registry_versions,
        readable_entry_count=readable_entry_count,
        window_count=window_count,
        window_unknown_sessions=sum(s.window_ordinal is None for s in sessions),
    )
