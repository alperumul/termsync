"""The JSON contract.

Every timestamp in `desk` is epoch milliseconds UTC, matching ~/.claude/history.jsonl,
which the tool must interoperate with. Local formatting happens only at the display layer.
Revision 3 of the design stored ISO strings and stamped local time with a `Z`, which put
its worked example a full day out; these types exist partly to make that unrepresentable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

Source = Literal["capture", "restore"]
Outcome = Literal["ok", "degraded", "error"]


def normalize_registry_versions(values: Iterable[str] | None) -> tuple[str, ...]:
    """Collapse per-session registry-version strings into one honest set.

    The live session registry can hold entries written by different Claude
    Code versions at once -- `2.1.238` and `2.1.239` were both observed in a
    single live `-Emit` run (review finding M8). Collapsing that into one
    string would make the health line report a version half the sessions
    were not written under, so `Layout` and `Status` each store the whole
    set instead of picking one.

    Falsy entries (None, "") are dropped and duplicates collapse. The sort
    is a plain lexicographic string sort, not a version-number parser: this
    is a display set, not an ordering, and lexicographic order is
    deterministic and sufficient -- do not "fix" this into semver parsing.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        # A bare string is iterable, so without this guard `"2.1.238"` stores as
        # ['.', '1', '2', '3', '8'] -- wrong data, on disk, with no error raised,
        # in the one field whose entire job is reporting honestly. It is also the
        # natural mistake: this parameter WAS a single string until M8, and
        # `Sequence[str]` accepts `str` at the type level. Fail loudly instead.
        raise TypeError(
            f"registry versions must be a sequence of strings, not a bare string: {values!r}"
        )
    return tuple(sorted({v for v in values if v}))


@dataclass(frozen=True)
class Session:
    session_id: str
    cwd: str
    title: str
    last_prompt_at: int | None = None
    armed: bool = False
    # U7: one-based rank of an attributable window; zero-based visual tab index.
    # Unknown/ambiguous, Codex and pending placement remain null.
    window_ordinal: int | None = None
    tab_index: int | None = None
    agent: Literal["claude", "codex"] = "claude"
    archived: bool = False
    rollout_path: str | None = None
    resume_args: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "title": self.title,
            "lastPromptAt": self.last_prompt_at,
            "armed": self.armed,
            "windowOrdinal": self.window_ordinal,
            "tabIndex": self.tab_index,
            "agent": self.agent,
            "archived": self.archived,
            "rolloutPath": self.rollout_path,
            "resumeArgs": list(self.resume_args),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Session:
        agent = d.get("agent", "claude")
        if agent not in ("claude", "codex"):
            raise ValueError(f"Unsupported session agent: {agent!r}")
        resume_args = d.get("resumeArgs", [])
        if not isinstance(resume_args, list) or any(not isinstance(a, str) for a in resume_args):
            raise ValueError("resumeArgs must be a string array")
        return cls(
            session_id=d["sessionId"],
            cwd=d["cwd"],
            title=d.get("title", ""),
            last_prompt_at=d.get("lastPromptAt"),
            armed=bool(d.get("armed", False)),
            window_ordinal=d.get("windowOrdinal"),
            tab_index=d.get("tabIndex"),
            agent=agent,
            archived=bool(d.get("archived", False)),
            rollout_path=d.get("rolloutPath"),
            resume_args=tuple(resume_args),
        )


@dataclass(frozen=True)
class Layout:
    version: int
    source: Source
    captured_at: int
    # int | None (review finding F3, reversing the earlier "refuse on null
    # bootTime" policy): `-Emit`'s `LastBootUpTime` read can fail for a
    # persistent reason (WMI service down, permissions, a locked-down
    # machine -- the emitter's own guard names all three), and refusing to
    # write a layout over a missing GROUPING KEY throws away the
    # irreplaceable half of the system (§4) to protect a convenience.
    # `selection.select_target` (verified, not assumed -- see its own
    # comments and `test_selection.py`) treats `None` as an ordinary
    # dict/set key: null-boot layouts collapse into one pseudo-boot group
    # and are, at worst, excluded from the two-boot candidate window --
    # exactly the outcome of never writing them. Writing therefore only
    # ever matches or beats refusing. One accepted cost: the shrink guard's
    # "same boot" comparison (§8) will treat two null-boot layouts as the
    # same boot even when they are not -- one imprecise sentence in a `!`
    # block, not a lost desk.
    boot_id: int | None
    hash: str
    sessions: tuple[Session, ...]
    # The set of Claude Code versions the registry's sessions were written
    # under, at capture (§6, review finding M8). Not a single string: the
    # live registry can hold entries from different versions simultaneously,
    # and collapsing that into one value would be a lie. See
    # `normalize_registry_versions`.
    registry_versions: tuple[str, ...] = ()
    window_count: int | None = None  # attributable windows; null if unavailable

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "capturedAt": self.captured_at,
            "bootId": self.boot_id,
            "hash": self.hash,
            "registryVersions": list(self.registry_versions),
            "sessions": [s.to_json() for s in self.sessions],
            "windowCount": self.window_count,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Layout:
        return cls(
            version=d["version"],
            source=d["source"],
            captured_at=d["capturedAt"],
            boot_id=d["bootId"],
            hash=d["hash"],
            registry_versions=normalize_registry_versions(d.get("registryVersions")),
            sessions=tuple(Session.from_json(s) for s in d["sessions"]),
            window_count=d.get("windowCount"),
        )


@dataclass(frozen=True)
class PendingEntry:
    """One tab `termsync restore` opened armed, which has not been resumed yet."""

    session_id: str
    title: str
    cwd: str
    armed_at: int
    agent: Literal["claude", "codex"] = "claude"
    archived: bool = False
    rollout_path: str | None = None
    resume_args: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "title": self.title,
            "cwd": self.cwd,
            "armedAt": self.armed_at,
            "agent": self.agent,
            "archived": self.archived,
            "rolloutPath": self.rollout_path,
            "resumeArgs": list(self.resume_args),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PendingEntry:
        session = Session.from_json({"cwd": "", **d})
        return cls(
            session_id=d["sessionId"],
            title=d.get("title", ""),
            cwd=d.get("cwd", ""),
            armed_at=d["armedAt"],
            agent=session.agent,
            archived=session.archived,
            rollout_path=session.rollout_path,
            resume_args=session.resume_args,
        )


@dataclass(frozen=True)
class Pending:
    """`pending.json` — the armed tabs of the last restore (§6).

    An armed tab hosts no Claude process, so it writes no registry entry. Without
    this file three things go wrong at once (§13.6): the next capture records the
    desk *minus* every armed tab, so an armed restore leaves the user less
    recoverable than before it; a second `termsync restore` duplicates every armed
    tab, because §7 step 2's already-live set is registry ∪ pending, not the
    registry alone; and post-restore verification has nothing to check against.
    """

    written_at: int
    entries: tuple[PendingEntry, ...] = ()

    @property
    def session_ids(self) -> frozenset[str]:
        """The ids §7 step 2 unions with the registry to get the already-live set."""
        return frozenset(entry.session_id for entry in self.entries)

    @property
    def session_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset((entry.agent, entry.session_id) for entry in self.entries)

    def to_json(self) -> dict[str, Any]:
        return {
            "writtenAt": self.written_at,
            "entries": [entry.to_json() for entry in self.entries],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Pending:
        return cls(
            written_at=d["writtenAt"],
            entries=tuple(PendingEntry.from_json(e) for e in d.get("entries", [])),
        )


@dataclass(frozen=True)
class Status:
    """Written on every run of `desk snap`, success or failure (§6, §8).

    `last_success_at` is a field of its own, not derived from the newest layout's
    `captured_at`: dedup means a successful run that sees an unchanged desk writes
    no layout, and deriving freshness from the newest layout would then wrongly
    report a healthy tool as stale.
    """

    ran_at: int
    outcome: Outcome
    sessions_seen: int
    wrote_version: int | None
    error: str | None = None
    consecutive_failures: int = 0
    last_success_at: int | None = None
    # Same reasoning as `Layout.registry_versions` (§6, M8): the registry can
    # hold entries from more than one Claude Code version at once, so a run's
    # health record carries the whole set, not one collapsed string.
    registry_versions: tuple[str, ...] = ()
    title_fallbacks: int = 0
    windows_seen: int | None = None  # physical windows, including plain/SSH

    # D33: additive timing fields; legacy files/callers have unknown timing.
    started_at: int | None = None
    finished_at: int | None = None
    duration_ms: int | None = None
    codex_sessions_seen: int = 0
    codex_error: str | None = None
    claude_error: str | None = None
    window_error: str | None = None
    window_unknown_sessions: int = 0  # all captured rows, including Codex/pending

    def to_json(self) -> dict[str, Any]:
        return {
            "ranAt": self.ran_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "durationMs": self.duration_ms,
            "codexSessionsSeen": self.codex_sessions_seen,
            "codexError": self.codex_error,
            "claudeError": self.claude_error,
            "windowError": self.window_error,
            "windowUnknownSessions": self.window_unknown_sessions,
            "outcome": self.outcome,
            "lastSuccessAt": self.last_success_at,
            "sessionsSeen": self.sessions_seen,
            "windowsSeen": self.windows_seen,
            "wroteVersion": self.wrote_version,
            "error": self.error,
            "consecutiveFailures": self.consecutive_failures,
            "titleFallbacks": self.title_fallbacks,
            "registryVersions": list(self.registry_versions),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Status:
        return cls(
            ran_at=d["ranAt"],
            started_at=d.get("startedAt"),
            finished_at=d.get("finishedAt"),
            duration_ms=d.get("durationMs"),
            codex_sessions_seen=d.get("codexSessionsSeen", 0),
            codex_error=d.get("codexError"),
            claude_error=d.get("claudeError"),
            window_error=d.get("windowError"),
            window_unknown_sessions=d.get("windowUnknownSessions", 0),
            outcome=d["outcome"],
            sessions_seen=d["sessionsSeen"],
            wrote_version=d.get("wroteVersion"),
            error=d.get("error"),
            consecutive_failures=d.get("consecutiveFailures", 0),
            last_success_at=d.get("lastSuccessAt"),
            registry_versions=normalize_registry_versions(d.get("registryVersions")),
            title_fallbacks=d.get("titleFallbacks", 0),
            windows_seen=d.get("windowsSeen"),
        )
