"""Command parsing, capture sequencing, and restore presentation.

Capture and restore accept injected readers, clocks, and launchers so tests
can exercise the full flow without the real desktop or user store.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import IO

from termsync import health, history, install, picker, restore, rows, selection
from termsync.capture import build_capture, live_session_keys
from termsync.console import safe_print as _safe_print
from termsync.emit import Emit, run_capture as run_emit
from termsync.history import HistoryEntry
from termsync.model import Layout, Status
from termsync.rows import Row, Rows
from termsync.store import Store, WriteResult

DEFAULT_STORE_ROOT = Path.home() / ".termsync"

# Tests must override profile paths before reading provider history.
DEFAULT_HISTORY_PATH = Path.home() / ".claude" / "history.jsonl"

# Restore action records cannot replace an unreadable captured desk.
_NO_READABLE_CAPTURE = (
    "No readable captured desk -- the store holds layouts, but every "
    '`source: "capture"` one is unreadable (see the warnings below). A '
    '`source: "restore"` record is a record of an action, not a desk, so it '
    "is never restored from. Run `termsync snap` to capture a fresh desk."
)

# The shared session lookup expects the Claude root, not its projects directory.
DEFAULT_CLAUDE_DIR = Path.home() / ".claude"

# Injection contract shared by preview and interactive restore.
RowBuilder = Callable[
    [
        Layout,
        Mapping[str, HistoryEntry],
        "frozenset[str | tuple[str, str]]",
        "frozenset[str]",
        rows.SessionFileLookup,
        rows.CwdExists,
        rows.AiTitles,
    ],
    Rows,
]

# Unknown boot identity degrades grouping but must not prevent capture.
BOOT_ID_UNKNOWN_WARNING = (
    "warning: bootId unknown (LastBootUpTime read failed) -- this capture's "
    "boot grouping falls back to a single null-boot bucket (§7 step 1)"
)


def _default_clock() -> int:
    """Epoch milliseconds UTC -- the unit every timestamp in the store uses (§6)."""
    return int(time.time() * 1000)


def snap(
    store: Store,
    emit_runner: Callable[[], Emit] = run_emit,
    clock: Callable[[], int] = _default_clock,
) -> int:
    """Capture the supported desk and record its status.

    Record status once, including on failure. A later output failure must not
    overwrite a successful capture or increment its failure count. If the
    injected clock fails, use the system clock for the error record.
    """
    now: int | None = None
    recorded = False
    exit_code = 1
    try:
        # D33: keep ranAt/lastSuccessAt on this start reading; finish is a
        # second wall-clock reading immediately before recording status.
        now = clock()
        emit = emit_runner()

        # Reconcile pending identities before capture using the same live-session filter.
        store.expire_pending(now, live_session_keys(emit))

        pending = store.read_pending()
        result = build_capture(emit, pending)

        registry_error = health.is_registry_error(
            emit.claude_process_count, result.readable_entry_count
        ) or bool(emit.claude_error)
        claude_error = emit.claude_error or (health.REGISTRY_ERROR if registry_error else None)
        window_error = emit.desktop.error if emit.desktop else None
        windows_seen = emit.desktop.windows_seen if emit.desktop else None
        reader_error = "; ".join(filter(None, (
            claude_error, emit.codex_error, window_error,
        ))) or None

        if registry_error and not any(not s.armed for s in result.sessions):
            # §5 step 10: the primary error rule fired. Write status with
            # outcome "error" and write NO layout.
            store.record_run(
                ran_at=now,
                started_at=now,
                finished_at=clock(),
                outcome="error",
                sessions_seen=len(result.sessions),
                wrote_version=None,
                error=reader_error,
                claude_error=claude_error,
                codex_error=emit.codex_error,
                window_error=window_error,
                windows_seen=windows_seen,
                window_unknown_sessions=result.window_unknown_sessions,
                registry_versions=result.registry_versions,
                title_fallbacks=result.title_fallbacks,
            )
            recorded = True
            _print_read_warnings(store)
            _safe_print(f"error: {reader_error}")
            return exit_code

        # Preserve the desk even when boot identity is unavailable.
        outcome = health.outcome_for_capture(registry_error, result.title_fallbacks)
        if reader_error:
            outcome = "degraded"

        write = store.write_capture(
            sessions=result.sessions,
            captured_at=now,
            boot_id=emit.boot_time,
            registry_versions=result.registry_versions,
            window_count=result.window_count,
        )

        # Pruning failure must not turn a successful capture into a failed one.
        try:
            _prune_after_capture(store, now, emit.boot_time)
        except Exception as prune_exc:  # noqa: BLE001 -- reported, never fails the capture
            _safe_print(
                f"warning: prune failed: {type(prune_exc).__name__}: {prune_exc}",
                file=sys.stderr,
            )

        status = store.record_run(
            ran_at=now,
            started_at=now,
            finished_at=clock(),
            outcome=outcome,
            error=reader_error,
            claude_error=claude_error,
            codex_error=emit.codex_error,
            window_error=window_error,
            windows_seen=windows_seen,
            window_unknown_sessions=result.window_unknown_sessions,
            codex_sessions_seen=len(emit.codex),
            sessions_seen=len(result.sessions),
            wrote_version=write.wrote_version,
            registry_versions=result.registry_versions,
            title_fallbacks=result.title_fallbacks,
        )
        recorded = True
        exit_code = 0

        _print_read_warnings(store)  # D4: reported, but never changes `outcome`
        if reader_error:
            _safe_print(f"warning: {reader_error}", file=sys.stderr)
        if emit.boot_time is None:
            _safe_print(BOOT_ID_UNKNOWN_WARNING, file=sys.stderr)
        _print_summary(status, write)
        return exit_code

    except Exception as exc:  # noqa: BLE001 -- D7: every path writes a status record
        if not recorded:
            fallback_now = now if now is not None else _default_clock()
            try:
                finished_at = clock()
            except Exception:  # noqa: BLE001 -- record even if the injected clock failed
                finished_at = _default_clock()
            store.record_run(
                ran_at=fallback_now,
                started_at=now,
                finished_at=finished_at,
                outcome="error",
                sessions_seen=0,
                wrote_version=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            exit_code = 1
        try:
            traceback.print_exc(file=sys.stderr)
        except UnicodeEncodeError:
            _safe_print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return exit_code


def _prune_after_capture(store: Store, now: int, boot_id: int | None) -> None:
    """Prune after writing, using the fresh layout list.

    Protect the selected capture, newest capture, and newest restore record in
    addition to the retention floor. Unreadable captures may leave no target.
    """
    layouts = store.all_layouts()
    target = selection.select_target(layouts, boot_id=boot_id)

    keep: set[int] = set()
    if target is not None:
        keep.add(target.layout.version)
        keep.add(target.newest.version)

    restore_versions = [layout.version for layout in layouts if layout.source == "restore"]
    if restore_versions:
        keep.add(max(restore_versions))

    store.prune(now, frozenset(keep))


def _print_read_warnings(store: Store) -> None:
    """D4: store read warnings reach the user on stderr, and never flip `outcome`."""
    for warning in store.read_warnings:
        _safe_print(f"warning: {warning}", file=sys.stderr)


def _print_summary(status: Status, write: WriteResult) -> None:
    """The first thing the owner will ever see this tool print. Keep it short."""
    if write.allocated:
        version_note = f"wrote version {write.layout.version}"
    else:
        version_note = "unchanged desk, deduped"
    line = f"{status.outcome}: {status.sessions_seen} session(s) captured, {version_note}"
    if status.title_fallbacks:
        line += f", {status.title_fallbacks} title fallback(s)"
    _safe_print(line)


# --- `restore --dry-run` (S10, §7 step 6, §13.3) -----------------------------


def _format_local(epoch_ms: int) -> str:
    """Format UTC epoch milliseconds in local time for display only."""
    # Naive local time IS the point here (§6: "local formatting happens only
    # at the display layer"), not an oversight -- epoch_ms is UTC, and
    # omitting `tz` is what converts it to this machine's local clock.
    return datetime.fromtimestamp(epoch_ms / 1000).strftime("%m-%d %H:%M")  # noqa: DTZ006


def _bg_session_ids(emit: Emit) -> frozenset[str]:
    """Return background IDs visible in the current Claude registry.

    History has no kind field. Once a background entry is pruned from the
    registry, this read cannot establish that it was previously a background job.
    """
    return frozenset(
        session_id
        for entry in emit.sessions
        if entry.get("kind") == "bg" and (session_id := entry.get("sessionId")) is not None
    )


def _cwd_exists(cwd: str) -> bool:
    """Require an identified absolute directory; empty paths must not mean cwd."""
    if not cwd.strip():
        return False
    path = Path(cwd)
    return path.is_absolute() and path.is_dir()


def _render_row(row: Row, now: int) -> str:
    """Render a preview row with its full session ID.

    Live status takes precedence over unavailability. Distinguish an unknown
    directory from a deleted one; armed and archived rows are not preselected.
    """
    cwd_part = f"  ({row.cwd})" if row.cwd else ""
    prompt_str, age_str = picker.prompt_and_age_text(row, now)

    label = f"[{row.agent}] {row.title}"
    if row.live:
        return f"[=] {row.session_id}  {label}{cwd_part}  {prompt_str}, {age_str}  -- live"

    if not row.selectable:
        if row.unavailable_reason:
            reason = row.unavailable_reason
        elif row.session_file is None:
            reason = "session file deleted"
        elif not row.cwd:
            reason = "cwd unknown"  # L2: never known, not necessarily gone
        else:
            reason = "cwd gone"  # L2: a real path that no longer exists
        return f"[ ] {row.session_id}  {label}{cwd_part}  -- {reason}"

    marker = "~" if row.armed else ("[ ]" if row.archived else "[x]")
    suffix = "  -- armed" if row.armed else ""
    if row.archived:
        suffix += "  -- archived; selection will unarchive"
    return f"{marker} {row.session_id}  {label}{cwd_part}  {prompt_str}, {age_str}{suffix}"


def _header_lines(
    status: Status | None,
    target: selection.Target,
    effective: Layout,
    emit: Emit,
    now: int,
) -> list[str]:
    """Build the freshness, boot, and layout-selection header for both views."""
    lines = [
        health.freshness_report(
            status.last_success_at if status is not None else None,
            now,
            format_timestamp=_format_local,
        )
    ]
    failures_line = health.consecutive_failures_report(
        status.consecutive_failures if status is not None else 0,
        status.error if status is not None else None,
    )
    if failures_line is not None:
        lines.append(f"!  {failures_line}")
    for agent, error in restore.provider_read_errors(emit).items():
        lines.append(f"!  Current {agent} live check failed: {error}. "
                     f"{agent.capitalize()} restore is unavailable; resolve the error "
                     "and reopen the picker.")

    if emit.boot_time is None:
        boot_text = "boot time unknown (LastBootUpTime read failed)"
    else:
        boot_text = health.boot_boundary_text(effective.captured_at, emit.boot_time)
    lines.append(
        f"Last desk -- {_format_local(effective.captured_at)} "
        f"* {len(effective.sessions)} session(s), {boot_text}"
    )

    # Warn only for a strict subset from the same boot; other differences are informational.
    if effective.version != target.newest.version:
        newest_ids = {s.session_id for s in target.newest.sessions}
        shown_ids = {s.session_id for s in effective.sessions}
        strict_subset = newest_ids < shown_ids and effective.boot_id == target.newest.boot_id
        if strict_subset:
            lines.append(
                f"!  A newer snapshot ({_format_local(target.newest.captured_at)}) holds "
                f"{len(newest_ids)} session(s) -- a strict subset of this one, same boot. "
                f"Showing the larger {_format_local(effective.captured_at)} desk instead. "
                "Re-run with --newest to see the newer one."
            )
        else:
            lines.append(
                f"   A newer snapshot ({_format_local(target.newest.captured_at)}) holds "
                f"{len(newest_ids)} session(s); this one holds {len(shown_ids)}. "
                "Re-run with --newest to see it."
            )
    elif target.layout.version != target.newest.version:
        # Keep the larger capture discoverable after switching to the newest one.
        other = target.layout
        lines.append(
            f"   A larger {_format_local(other.captured_at)} desk "
            f"({len(other.sessions)} session(s), same boot) is also available -- "
            "press [n] to switch back to it."
        )
    return lines


def _join_rows(
    effective: Layout,
    emit: Emit,
    store: Store,
    history_path: Path,
    claude_dir: Path,
    row_builder: RowBuilder,
) -> Rows:
    """Join captured rows with live/pending identities, history, and session files.

    Read AI titles only for young history candidates that need a title fallback.
    """
    history_entries = history.load_history(history_path)
    pending = store.read_pending()
    already_live = live_session_keys(emit) | (
        pending.session_keys if pending is not None else frozenset()
    )
    bg_ids = _bg_session_ids(emit)

    lookup = rows.make_glob_session_file(claude_dir)
    # Read title fallbacks only for history candidates newer than this capture.
    young_candidates = history.sessions_active_after(history_entries, effective.captured_at, bg_ids)
    ai_titles = rows.build_ai_title_map([entry.session_id for entry in young_candidates], lookup)

    row_set = row_builder(
        effective,
        history_entries,
        already_live,
        bg_ids,
        lookup,
        _cwd_exists,
        ai_titles,
    )
    read_errors = restore.provider_read_errors(emit)

    def checked(row: Row) -> Row:
        if row.agent in read_errors:
            return replace(row, selectable=False,
                           unavailable_reason=f"current {row.agent} live check failed")
        return row

    return Rows(tuple(checked(row) for row in row_set.desk),
                tuple(checked(row) for row in row_set.young))


def dry_run(
    store: Store,
    *,
    newest: bool = False,
    emit_runner: Callable[[], Emit] = run_emit,
    clock: Callable[[], int] = _default_clock,
    history_path: Path = DEFAULT_HISTORY_PATH,
    claude_dir: Path = DEFAULT_CLAUDE_DIR,
    row_builder: RowBuilder = rows.build_rows,
    out: IO[str] | None = None,
) -> int:
    """Print the restore inventory without opening tabs or reading keys.

    An empty store returns before desktop reads. Otherwise the default reader
    observes live state, and the row builder marks live and pending identities.
    Tests inject readers and isolated paths. Report accumulated store warnings
    after all reads, including when an error interrupts the preview.
    """
    stream = out if out is not None else sys.stdout
    now = clock()

    try:
        layouts = store.all_layouts()
        if not layouts:
            _safe_print(
                "No desk captured yet -- run `termsync snap` to capture one, or "
                "`termsync install` to schedule it automatically.",
                file=stream,
            )
            return 0

        emit = emit_runner()
        target = selection.select_target(layouts, boot_id=emit.boot_time)
        if target is None:
            # Restore records alone do not provide a recoverable captured desk.
            _safe_print(_NO_READABLE_CAPTURE, file=stream)
            return 0

        effective = target.newest if newest else target.layout

        status = store.read_status()
        for line in _header_lines(status, target, effective, emit, now):
            _safe_print(line, file=stream)

        row_set = _join_rows(effective, emit, store, history_path, claude_dir, row_builder)

        for title, group in picker.row_sections(row_set.desk + row_set.young):
            if title is not None:
                _safe_print("", file=stream)
                _safe_print(title, file=stream)
            for row in group:
                _safe_print(_render_row(row, now), file=stream)

        if not row_set.desk and not row_set.young:
            # Live/armed rows remain visible (D10); young rows also prevent
            # an empty-desk message from contradicting a restorable row.
            _safe_print("Nothing to restore -- this desk holds no sessions.", file=stream)

        return 0
    finally:
        _print_read_warnings(store)


# --- `restore` (bare, interactive) (S5, §2, §13.3) ------------------------


def _console_input_available() -> bool:
    """Check for a real Windows console input buffer; fail closed on errors.

    msvcrt reads the console, not sys.stdin. isatty alone also accepts NUL,
    which would let an unattended invocation block forever on a key read.
    GetConsoleMode rejects NUL, pipes, files, and missing handles.
    """
    try:
        import ctypes

        STD_INPUT_HANDLE = -10
        kernel32 = ctypes.windll.kernel32  # AttributeError on a non-Windows runner
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == -1:
            return False
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:  # noqa: BLE001 -- a refusal is always safe; a raise here is not
        return False


def restore_interactive(
    store: Store,
    *,
    newest: bool = False,
    run: bool = False,
    emit_runner: Callable[[], Emit] = run_emit,
    clock: Callable[[], int] = _default_clock,
    history_path: Path = DEFAULT_HISTORY_PATH,
    claude_dir: Path = DEFAULT_CLAUDE_DIR,
    row_builder: RowBuilder = rows.build_rows,
    key_reader: Callable[[], str] | None = None,
    out: IO[str] | None = None,
    is_tty: Callable[[], bool] | None = None,
    session_file: rows.SessionFileLookup | None = None,
    cwd_exists: rows.CwdExists | None = None,
    spawner: restore.Spawner = restore.default_spawn,
    observe: restore.Observer | None = None,
    sleep: Callable[[float], None] = time.sleep,
    verify_delay_s: float = restore.DEFAULT_VERIFY_DELAY_S,
) -> int:
    """Run the picker, then restore its selected rows.

    Reject missing console input before store or desktop reads. Layout switching
    preserves the selection intersected with selectable rows in the new view.
    Tests must inject key input, desktop observations, launchers, and waits;
    the production defaults operate on the real desktop. Verification errors,
    preflight skips, launch failures, and record failures produce exit 1.
    """
    stream = out if out is not None else sys.stdout
    tty_check = is_tty if is_tty is not None else _console_input_available

    if not tty_check():
        _safe_print(
            "error: `termsync restore` needs a real console attached for its "
            "picker (no interactive keyboard input is available here). Run "
            "`termsync restore --dry-run` instead -- it prints the same "
            "session set without opening a keyboard loop.",
            file=sys.stderr,
        )
        return 1

    reader = key_reader if key_reader is not None else picker.read_key_msvcrt

    try:
        layouts = store.all_layouts()
        if not layouts:
            _safe_print(
                "No desk captured yet -- run `termsync snap` to capture one, or "
                "`termsync install` to schedule it automatically.",
                file=stream,
            )
            return 0

        emit = emit_runner()
        target = selection.select_target(layouts, boot_id=emit.boot_time)
        if target is None:
            # D20 made this reachable; it used to be `assert target is not None`.
            # See `_NO_READABLE_CAPTURE` for the reproduction and why this reports
            # instead of throwing (§6 rule 3).
            _safe_print(_NO_READABLE_CAPTURE, file=stream)
            return 0

        effective = target.newest if newest else target.layout
        carried_selection: frozenset[str] | None = None

        while True:
            row_set = _join_rows(effective, emit, store, history_path, claude_dir, row_builder)

            def compute_header(effective: Layout = effective) -> list[str]:
                # Refresh every frame and bind this iteration's layout.
                return _header_lines(store.read_status(), target, effective, emit, clock())

            try:
                outcome = picker.run_picker(
                    row_set,
                    header=compute_header,
                    key_reader=reader,
                    out=stream,
                    clock=clock,
                    initial_selected=carried_selection,
                )
            except KeyboardInterrupt:
                _safe_print("Cancelled -- nothing restored.", file=stream)
                return 1

            if outcome.kind == "quit":
                _safe_print("Quit -- nothing restored.", file=stream)
                return 0

            if outcome.kind == "switch_layout":
                effective = (
                    target.newest if effective.version != target.newest.version else target.layout
                )
                carried_selection = outcome.selected_ids
                continue

            sf = (
                session_file
                if session_file is not None
                else rows.make_glob_session_file(claude_dir)
            )
            ce = cwd_exists if cwd_exists is not None else _cwd_exists
            result = restore.restore_selected(
                outcome.selected_ids,
                row_set,
                store,
                emit,
                run=run,
                session_file=sf,
                cwd_exists=ce,
                spawner=spawner,
                observe=observe,
                clock=clock,
                sleep=sleep,
                emit_runner=emit_runner,
                verify_delay_s=verify_delay_s,
                out=stream,
            )
            # Report incomplete restore outcomes, including verification failure.
            return 1 if (
                result.skipped or result.failed or result.record_error or result.verification_error
            ) else 0
    finally:
        _print_read_warnings(store)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termsync")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap_parser = subparsers.add_parser(
        "snap",
        help=(
            "Capture the current desk: supported Claude Code and Codex sessions."
        ),
    )
    snap_parser.add_argument(
        "--store",
        type=Path,
        default=None,
        metavar="PATH",
        help="Store root to write to (default: ~/.termsync). Point this at a "
        "scratch directory to try the tool without touching the real store.",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore the last desk: interactive picker, or --dry-run to print it.",
        description=(
            "Restore the last desk. With no flags, launches an interactive "
            "picker: available sessions are pre-selected except armed and archived rows. "
            "[space] toggles, [/] filters, and Enter opens a tab "
            "per selected session -- armed by default (a shell waiting for "
            "Enter before it resumes), or immediately with --run. "
            "The picker needs a real interactive terminal and refuses to run "
            "otherwise (see --dry-run below). --dry-run skips the picker "
            "entirely and prints the same session set once, non-interactively, "
            "with no keyboard loop or provider launch -- use it from a script, "
            "a pipe, or anywhere stdin is not a TTY."
        ),
    )
    # Preview and immediate resume are mutually exclusive modes.
    run_mode_group = restore_parser.add_mutually_exclusive_group()
    run_mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Print session id, title, cwd, prompt count and age for every row "
        "in the desk, then exit. Opens no tabs; reads current desktop state; never "
        "launches the picker's keyboard loop. Use this in a non-interactive "
        "context (a script, a pipe, a scheduled task) where the picker cannot "
        "run at all. Mutually exclusive with --run.",
    )
    restore_parser.add_argument(
        "--newest",
        action="store_true",
        help="Show the newest layout instead of the largest-since-boot pick.",
    )
    run_mode_group.add_argument(
        "--run",
        action="store_true",
        help="Resume each selected session immediately instead of arming a tab "
        "that waits for Enter. --run tabs write no armed pending entry; "
        "their provider identities are checked after launching. "
        "Mutually exclusive with --dry-run.",
    )
    restore_parser.add_argument(
        "--store",
        type=Path,
        default=None,
        metavar="PATH",
        help="Store root to read from (default: ~/.termsync).",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Register the 10-minute scheduled task that runs `termsync snap` automatically.",
        description=(
            "Register a Task Scheduler task whose action is "
            "`pythonw.exe -m termsync snap`, every 10 minutes and on "
            "shutdown/sleep/resume events, registered "
            '"run only when the user is logged on" -- and verified as such by '
            "reading the task back after registering. Idempotent: running this "
            "twice leaves one task, not two. Use --dry-run to see the exact "
            "registration command without running it."
        ),
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the XML and schtasks.exe registration command with a "
        "temporary-file placeholder; register nothing.",
    )
    install_parser.add_argument(
        "--print-xml",
        action="store_true",
        help="Print the task XML to stdout without registering anything.",
    )
    install_parser.add_argument(
        "--run-as-user",
        default=None,
        metavar="DOMAIN\\user",
        help="The account to register the task as. Default: derived from "
        "USERDOMAIN\\USERNAME in the environment. If either is "
        "missing or empty, install refuses to guess (an empty value reads "
        "as the SYSTEM account to schtasks) -- pass it here instead.",
    )

    return parser


def _use_utf8_streams() -> None:
    """Preserve Unicode titles on console and redirected output.

    safe_print remains the fallback for streams that cannot be reconfigured.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable stream (a pytest capture buffer, a pipe)


def main(argv: list[str] | None = None) -> int:
    _use_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "snap":
        root = args.store if args.store is not None else DEFAULT_STORE_ROOT
        return snap(Store(root))

    if args.command == "restore":
        root = args.store if args.store is not None else DEFAULT_STORE_ROOT
        if args.dry_run:
            return dry_run(Store(root), newest=args.newest)
        return restore_interactive(Store(root), newest=args.newest, run=args.run)

    if args.command == "install":
        # Tests replace this module attribute before dispatching installation.
        return install.install(
            dry_run=args.dry_run, print_xml=args.print_xml, run_as_user=args.run_as_user
        )

    parser.error(f"unknown command: {args.command}")
    return 2  # pragma: no cover -- argparse.error() exits before this returns
