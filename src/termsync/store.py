"""The versioned layout store.

The store holds the only irreplaceable data in the system: what was open, then.
Claude Code's own session registry is live-only — it was observed pruning from nine
entries to two within minutes of a crash — so nothing else on disk records a dead desk.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from termsync.model import (
    Layout,
    Outcome,
    Pending,
    Session,
    Source,
    Status,
    normalize_registry_versions,
)

T = TypeVar("T")

# Version claims live under this prefix inside layouts/, deliberately outside the
# NNNNNN.json namespace that `_layout_paths` globs. See `_claim_version`.
CLAIM_PREFIX = ".claim-"


@dataclass(frozen=True)
class WriteResult:
    """What a write did — the layout, and whether a version was allocated for it.

    Both branches of a write produce a `Layout`, so returning one told the caller
    nothing about which branch ran. §6 makes `wroteVersion` the record of whether
    *this run* allocated a version, and a status stream that says `wroteVersion: 1`
    on every deduplicated run is unreadable as a growth signal. The refreshed layout
    still comes back, because the caller usually wants it too.
    """

    layout: Layout
    allocated: bool

    @property
    def wrote_version(self) -> int | None:
        """`status.json`'s `wroteVersion` — the version number, or None on a dedup."""
        return self.layout.version if self.allocated else None


def layout_hash(sessions: Iterable[Session]) -> str:
    """Identity of a desk, for deduplication.

    Titles are deliberately excluded: they are AI-generated and evolve during a
    conversation, so including them would defeat dedup entirely. A desk is the same
    desk when the same sessions are open in the same places.

    U7 appends window ordinal and visual tab index (including explicit None),
    intentionally allocating one version when upgrading a pre-U7 hash.
    """
    parts = sorted(
        f"{s.agent}|{s.session_id}|{s.cwd}|{int(s.armed)}|{s.window_ordinal}|{s.tab_index}"
        for s in sessions
    )
    return "sha256:" + hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.layouts_dir = self.root / "layouts"
        self.tmp_dir = self.root / "tmp"
        self.status_path = self.root / "status.json"
        self.pending_path = self.root / "pending.json"
        self.read_warnings: list[str] = []

    def write_capture(
        self,
        sessions: Iterable[Session],
        captured_at: int,
        boot_id: int | None,
        registry_versions: Sequence[str] = (),
        window_count: int | None = None,
    ) -> WriteResult:
        """Write a new capture, or dedup against the newest layout.

        The hash excludes titles (§6), so an unchanged session set with only
        different titles must not allocate a new version — it refreshes the
        newest layout's titles in place instead.

        `boot_id` accepts `None` (review finding F3): a run whose `bootTime`
        read failed still has an irreplaceable session set to record, and
        `Layout.boot_id`'s docstring / `selection.py` cover why storing the
        null is safe rather than refusing to write.
        """
        return self._write_layout_version(
            sessions=sessions,
            captured_at=captured_at,
            boot_id=boot_id,
            source="capture",
            registry_versions=registry_versions,
            window_count=window_count,
        )

    def write_restore(
        self,
        sessions: Iterable[Session],
        restored_at: int,
        boot_id: int | None,
        registry_versions: Sequence[str] = (),
    ) -> WriteResult:
        """Record what the user SELECTED in the picker, never what was offered.

        Recording the offered set would make the shrink guard (§8) fire on every
        ordinary restore, since deselecting some of the offered sessions is the
        picker's entire purpose. This signature has no "offered" parameter to
        pass by mistake -- callers can only ever record a selection.
        """
        return self._write_layout_version(
            sessions=sessions,
            captured_at=restored_at,
            boot_id=boot_id,
            source="restore",
            registry_versions=registry_versions,
        )

    def _write_layout_version(
        self,
        sessions: Iterable[Session],
        captured_at: int,
        boot_id: int | None,
        source: Source,
        registry_versions: Sequence[str],
        window_count: int | None = None,
    ) -> WriteResult:
        sessions = tuple(sessions)
        new_hash = layout_hash(sessions)
        normalized_versions = normalize_registry_versions(registry_versions)

        existing = self.newest()
        if self._can_dedup_into(existing, new_hash, source):
            # `registry_versions` takes the current run's value unconditionally,
            # same as `sessions`, `captured_at` and `boot_id` above -- this field
            # describes the run that most recently observed this desk, not the
            # run that first saw the unchanged session set (M8's policy: no
            # carry-over from `existing`).
            refreshed = replace(
                existing,
                sessions=sessions,
                captured_at=captured_at,
                boot_id=boot_id,
                registry_versions=normalized_versions,
                window_count=window_count,
            )
            self._write_layout(refreshed)
            return WriteResult(layout=refreshed, allocated=False)

        layout = Layout(
            version=self._next_version(),
            source=source,
            captured_at=captured_at,
            boot_id=boot_id,
            hash=new_hash,
            registry_versions=normalized_versions,
            sessions=sessions,
            window_count=window_count,
        )
        self._write_layout(layout)
        return WriteResult(layout=layout, allocated=True)

    def _can_dedup_into(self, existing: Layout | None, new_hash: str, source: Source) -> bool:
        """Whether this write may refresh `existing` in place instead of allocating.

        Three conditions, and only the first is about the desk being unchanged:

        1. Same hash — otherwise it is a different desk (§6).
        2. Same `source`. A capture that dedups into a `source: "restore"` record
           rewrites it, and §8's shrink guard uses the most recent restore record as
           its reference for what the user *selected*. That reference must not be
           mutated by the capture path. Right after a restore the hashes match by
           construction, so this is the normal case, not a corner.
        3. `existing` is the highest version on disk. `newest()` falls back past an
           unreadable layout, so without this the write would rewrite an older
           version, with a newer timestamp, behind a damaged newer one.
        """
        if existing is None or existing.hash != new_hash or existing.source != source:
            return False
        paths = self._layout_paths()
        return bool(paths) and int(paths[-1].stem) == existing.version

    def newest(self) -> Layout | None:
        """The most recent layout that can actually be read.

        A layout can be truncated if the machine died mid-write — which is precisely
        the event this tool exists for, so it is a normal case, not an exceptional one.
        Unreadable layouts are skipped and recorded in `read_warnings`; the caller is
        expected to surface them rather than quietly show an older desk.
        """
        layouts = self.all_layouts()
        return layouts[-1] if layouts else None

    def record_run(
        self,
        ran_at: int,
        outcome: Outcome,
        sessions_seen: int,
        wrote_version: int | None,
        error: str | None = None,
        registry_versions: Sequence[str] = (),
        title_fallbacks: int = 0,
        windows_seen: int | None = None,
        started_at: int | None = None,
        finished_at: int | None = None,
        codex_sessions_seen: int = 0,
        codex_error: str | None = None,
        claude_error: str | None = None,
        window_error: str | None = None,
        window_unknown_sessions: int = 0,
    ) -> Status:
        """Write `status.json` for one run of `termsync snap`, success or failure.

        `last_success_at` and `consecutive_failures` are derived here from the
        previous status record, not passed in, because both are cumulative:
        `last_success_at` must advance on every non-error run *even when dedup
        wrote no new layout* (§6), and `consecutive_failures` must reset on
        success and increment on error rather than being recomputed by the
        caller each time.
        """
        previous = self.read_status()
        if outcome == "error":
            consecutive_failures = (previous.consecutive_failures + 1) if previous else 1
            last_success_at = previous.last_success_at if previous else None
        else:
            consecutive_failures = 0
            last_success_at = ran_at

        status = Status(
            ran_at=ran_at,
            started_at=started_at,
            finished_at=finished_at,
            # D33: retain signed wall-clock elapsed time, including clock corrections.
            duration_ms=(
                finished_at - started_at
                if started_at is not None and finished_at is not None else None
            ),
            outcome=outcome,
            sessions_seen=sessions_seen,
            wrote_version=wrote_version,
            error=error,
            consecutive_failures=consecutive_failures,
            last_success_at=last_success_at,
            registry_versions=normalize_registry_versions(registry_versions),
            title_fallbacks=title_fallbacks,
            windows_seen=windows_seen,
            codex_sessions_seen=codex_sessions_seen,
            codex_error=codex_error,
            claude_error=claude_error,
            window_error=window_error,
            window_unknown_sessions=window_unknown_sessions,
        )
        self._atomic_write_text(self.status_path, json.dumps(status.to_json(), indent=2))
        return status

    def read_status(self) -> Status | None:
        """The last recorded status, or None if `termsync snap` has never run.

        An unreadable or schema-changed `status.json` also yields None, plus a
        warning — see `_read_json_record` for why that tolerance is not optional
        here. A missing history and an unreadable one are treated alike: both mean
        "nothing to carry forward", which `record_run` already handles.
        """
        return self._read_json_record(self.status_path, Status.from_json)

    def read_pending(self) -> Pending | None:
        """The tabs the last restore armed, or None if there are none.

        Absence is the normal state — no restore has run, or every armed tab has
        been resumed — so it is not an error. An unreadable file degrades the same
        way a truncated layout does: None plus a warning, never an exception into
        a caller whose job is to keep running unattended.
        """
        return self._read_json_record(self.pending_path, Pending.from_json)

    def write_pending(self, pending: Pending) -> Pending:
        """Replace pending under the shared transaction lock; merge new launches instead."""
        with self._pending_transaction():
            return self._write_pending(pending)

    def _write_pending(self, pending: Pending) -> Pending:
        self._atomic_write_text(self.pending_path, json.dumps(pending.to_json(), indent=2))
        return pending

    def merge_pending(self, pending: Pending) -> Pending:
        """Atomically add/rearm provider-qualified identities without losing other launches."""
        with self._pending_transaction():
            existing = self.read_pending()
            merged = {(e.agent, e.session_id): e for e in (existing.entries if existing else ())}
            merged.update(((e.agent, e.session_id), e) for e in pending.entries)
            return self._write_pending(Pending(pending.written_at, tuple(merged.values())))

    @contextmanager
    def _pending_transaction(self, timeout_s: float = 5.0):
        """Serialize pending updates across processes, releasing automatically on exit/crash.

        Keep the lock file permanently: unlinking it could give another process
        a different file to lock. The OS lock, not file existence, means busy.
        Readers still see complete pending files through atomic replacement.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / ".pending.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                def acquire():
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "Timed out waiting to update pending armed sessions"
                        ) from exc
                    time.sleep(min(0.05, remaining))
            yield
        finally:
            # Closing releases the OS lock even if the transaction raised.
            os.close(fd)

    def expire_pending(
        self,
        now: int,
        live_session_ids: frozenset[str | tuple[str, str]],
        ttl_ms: int = 24 * 60 * 60 * 1000,
    ) -> Pending | None:
        """Drop armed tabs that came back to life or timed out (§5 step 9).

        "Clear when the session appears live, or after 24 hours." Both halves matter:
        without the first, a resumed session stays in §7 step 2's already-live set
        and is silently withheld from the next restore; without the second, a tab the
        user never resumed withholds it forever.

        `live_session_ids` has no default — a caller with no live set has to say so.
        Returns what remains, or None once nothing does (the file is removed, so
        "no armed tabs" has one representation on disk, not two).
        """
        with self._pending_transaction():
            pending = self.read_pending()
            if pending is None:
                return None

            kept = tuple(
                entry
                for entry in pending.entries
                if (entry.agent, entry.session_id) not in live_session_ids
                and not (entry.agent == "claude" and entry.session_id in live_session_ids)
                and (now - entry.armed_at) < ttl_ms
            )
            if kept == pending.entries:
                return pending
            if not kept:
                self.pending_path.unlink(missing_ok=True)
                return None
            return self._write_pending(Pending(written_at=now, entries=kept))

    def prune(self, now: int, keep: frozenset[int]) -> list[int]:
        """Keep every version for 30 days, then one per day for a year (§6).

        - age < 30 days: kept, always.
        - 30 days <= age < 1 year: only the LARGEST desk of each UTC day survives,
          ties broken by the later capture (see the comment on the grouping below —
          §6 does not say which one to keep, and "the latest" is the wrong answer).
        - age >= 1 year: deleted.

        Zero-byte layout stubs left by an older build are reclaimed here too.

        `keep` is the set of versions the caller requires to survive regardless of
        age — normally `{target.layout.version, target.newest.version}` from
        `selection.select_target`. It has no default on purpose: the store does not
        know what a restore would pick, and the previous shape (an optional
        `boot_id` that switched the protection on) meant the shorter, more natural
        call protected nothing at all. Returns the pruned versions.
        """
        protected = set(keep)
        self._reclaim_stubs()

        thirty_days = 30 * 86_400_000
        one_year = 365 * 86_400_000

        by_day: dict[str, list[Layout]] = {}
        to_delete: list[Layout] = []
        for layout in self.all_layouts():
            if layout.version in protected:
                continue
            age = now - layout.captured_at
            if age < thirty_days:
                continue
            if age < one_year:
                day_key = datetime.fromtimestamp(layout.captured_at / 1000, tz=UTC).strftime(
                    "%Y-%m-%d"
                )
                by_day.setdefault(day_key, []).append(layout)
            else:
                to_delete.append(layout)

        for group in by_day.values():
            # Keep the LARGEST desk of the day, ties broken by the later capture.
            # §8: the last capture of a day is usually the *tidied* desk — "an
            # ordinary evening where he closes windows as he finishes" — so keeping
            # the latest would systematically destroy the working desks that §7
            # step 1's largest-of rule exists to find, and keep the small ones.
            group.sort(key=lambda layout: (len(layout.sessions), layout.captured_at))
            to_delete.extend(group[:-1])

        pruned_versions = []
        for layout in to_delete:
            path = self.layouts_dir / f"{layout.version:06d}.json"
            path.unlink(missing_ok=True)
            pruned_versions.append(layout.version)
        return sorted(pruned_versions)

    def all_layouts(self) -> list[Layout]:
        """Every layout that parses, in version order, and warnings for those that don't.

        This is the store's whole view of itself: `newest()` is its last element and
        `selection.select_target` takes the list. Unreadable layouts are reported in
        `read_warnings` in §6 rule 3's form — naming the version shown *instead*,
        because silently skipping one hides exactly the corruption the rule exists to
        detect, and because a layout dropped here is a layout dropped from the
        restore candidate set.

        Review finding F8 [MEDIUM]: the exception tuple here used to be narrower
        than `_read_json_record`'s (missing `TypeError` and `OSError`), so a
        layout whose JSON parsed but whose *shape* was wrong (`TypeError` out of
        `Layout.from_json`), or one that could not be read at all (a locked file
        -- live on Windows, where `snap` and an interactive `restore` can touch
        the directory at once), escaped uncaught through `newest()` ->
        `write_capture()` and stopped the tool writing ANY further layout,
        permanently. Both tuples now match, and `path.stat()` moved inside the
        `try` for the same reason -- it can raise `OSError` too.
        """
        result: list[Layout] = []
        for path in self._layout_paths():
            try:
                if path.stat().st_size == 0:
                    continue  # a reclaimable stub, not corruption -- see `_reclaim_stubs`
                parsed = Layout.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, OSError) as exc:
                shown = f"showing {result[-1].version:06d}" if result else "none older"
                # Review finding F2/M1: this warning is machine output printed
                # verbatim by `cli._print_read_warnings`, not prose -- an
                # em dash here is unencodable on ordinary Windows console
                # codepages (cp437, cp850), so it stays plain ASCII.
                self._warn(f"layout {path.stem} is unreadable ({type(exc).__name__}) -- {shown}")
                continue
            result.append(parsed)
        return result

    # --- internals -------------------------------------------------------

    def _read_json_record(self, path: Path, parse: Callable[[dict], T]) -> T | None:
        """Read one small JSON record tolerantly: None plus a warning, never a raise.

        `status.json` and `pending.json` are read by paths that must keep running on
        a machine where something else went wrong — `record_run` is the *only* way
        an `outcome: "error"` is ever recorded, so if reading the previous status
        can throw, the error-recording path is the path that dies. A record written
        by an older or newer `termsync` (missing a key that is required today) is
        the same class of problem as a truncated one and gets the same treatment.
        """
        if not path.is_file():
            return None
        try:
            return parse(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, OSError) as exc:
            self._warn(f"{path.name} is unreadable ({type(exc).__name__}); ignored")
            return None

    def _warn(self, message: str) -> None:
        """Record a read problem once, for the life of this `Store`.

        Warnings accumulate rather than being reset per read: `record_run` reads
        `status.json` and then the layouts, so a reset inside the layout read would
        silently drop the status warning — a warning being swallowed by an unrelated
        later read is the failure this whole warning channel exists to prevent.
        Repeats are dropped so a caller that reads twice does not see doubles.
        """
        if message not in self.read_warnings:
            self.read_warnings.append(message)

    def _layout_paths(self) -> list[Path]:
        if not self.layouts_dir.is_dir():
            return []
        return sorted(self.layouts_dir.glob("[0-9]" * 6 + ".json"))

    def _next_version(self) -> int:
        """Claim the next free version number, racing safely against another writer.

        There is one writer today, but claiming with O_EXCL costs nothing and turns
        a concurrent-write race (scheduled capture vs. interactive restore) from a
        silent overwrite into an impossible outcome: the loser retries the next
        number instead of clobbering the winner's layout.
        """
        paths = self._layout_paths()
        version = int(paths[-1].stem) + 1 if paths else 1
        while True:
            try:
                self._claim_version(version)
                return version
            except FileExistsError:
                version += 1  # someone else holds this number; the next one is ours

    def _claim_version(self, version: int) -> Path:
        """Exclusively claim `version` with a marker *outside* the layout namespace.

        Raises FileExistsError if the version is already claimed. The marker is
        `layouts/.claim-NNNNNN`, not the layout file itself: claiming at the target
        path means a crash before the write leaves a permanent zero-byte
        `NNNNNN.json` that never parses, so the "layout is unreadable" warning fires
        on every run forever, over a file that lost no data — and `prune` cannot
        remove it, because it never becomes a layout. The marker is deleted once the
        layout is written; one left behind by a crash costs a skipped version number
        and nothing else.
        """
        self.layouts_dir.mkdir(parents=True, exist_ok=True)
        path = self.layouts_dir / f"{CLAIM_PREFIX}{version:06d}"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        # A competing writer may have finished after our directory snapshot
        # and removed its claim. Owning the marker alone does not mean the
        # final version is free; preserve even an unreadable existing layout.
        try:
            (self.layouts_dir / f"{version:06d}.json").stat()
        except FileNotFoundError:
            return path
        except OSError:
            path.unlink(missing_ok=True)
            raise
        path.unlink(missing_ok=True)
        raise FileExistsError(f"Layout version {version} already exists")

    def _claim_paths(self) -> list[Path]:
        if not self.layouts_dir.is_dir():
            return []
        return sorted(self.layouts_dir.glob(CLAIM_PREFIX + "[0-9]" * 6))

    def _reclaim_stubs(self) -> None:
        """Remove zero-byte layout files and claim markers whose layout landed.

        A zero-byte layout file carries no lost data (the real write path is
        tmp -> fsync -> os.replace, so a genuine partial layout is unreachable);
        it can only be a claim placeholder from an older build. A non-empty
        unparseable file is real corruption and is left alone so it keeps warning.
        """
        for path in self._layout_paths():
            if path.stat().st_size == 0:
                path.unlink(missing_ok=True)
        for claim in self._claim_paths():
            version = int(claim.name[len(CLAIM_PREFIX) :])
            if (self.layouts_dir / f"{version:06d}.json").is_file():
                claim.unlink(missing_ok=True)

    def _write_layout(self, layout: Layout) -> None:
        self.layouts_dir.mkdir(parents=True, exist_ok=True)
        path = self.layouts_dir / f"{layout.version:06d}.json"
        self._atomic_write_text(path, json.dumps(layout.to_json(), indent=2))
        (self.layouts_dir / f"{CLAIM_PREFIX}{layout.version:06d}").unlink(missing_ok=True)

    def _atomic_write_text(self, target: Path, text: str) -> None:
        """Write to tmp/<name>.<pid>.tmp, fsync, then os.replace into `target`.

        tmp/ lives on the same volume as `target` (both under self.root), so
        os.replace is atomic: readers of `target` see either the old complete
        file or the new complete file, never a partial write.
        """
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.tmp_dir / f"{target.name}.{os.getpid()}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        finally:
            tmp_path.unlink(missing_ok=True)
