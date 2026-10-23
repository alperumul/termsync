"""Preflight selected sessions, launch fresh windows, persist, and verify.

The CLI owns selection; this module owns restore side effects. Tests must
inject launchers, desktop readers, and waits instead of their live defaults.
"""

from __future__ import annotations

import base64
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO

from termsync import health, picker
from termsync.capture import live_session_keys
from termsync.console import safe_print
from termsync.emit import Emit, run_capture as run_emit
from termsync.model import Pending, PendingEntry, Session, normalize_registry_versions
from termsync.rows import CodexLookup, CwdExists, Row, Rows, SessionFileLookup
from termsync.restore_codex import lookup_codex, validate_resume_args
from termsync.restore_windows import Launcher, Observer, launch_windows
from termsync.capture_windows import run_desktop
from termsync.store import Store
from termsync.titles import normalize_title

# Runtime helpers stay in the source checkout alongside src/.
DEFAULT_ARM_SCRIPT = Path(__file__).resolve().parents[2] / "snapshot" / "arm.ps1"

#: §7 step 8: "wait ~20 s" before re-running `-Emit` to verify.
DEFAULT_VERIFY_DELAY_S = 20.0

PromptAndAgeText = Callable[[Row, int], "tuple[str, str]"]
#: Nonblocking launch returning a pollable process; tests must inject it.
Spawner = Callable[[list[str]], Launcher]


def _default_clock() -> int:
    """Epoch milliseconds UTC. The CLI supplies its clock when orchestrating restore."""
    return int(time.time() * 1000)


def default_spawn(argv: list[str]) -> Launcher:
    """Start wt directly without blocking; readiness polls its exit separately."""
    return subprocess.Popen(argv)


def build_wt_argv(
    row: Row,
    arm_script: Path,
    run: bool,
    now: int,
    *,
    window_name: str,
    prompt_and_age_text: PromptAndAgeText = picker.prompt_and_age_text,
) -> list[str]:
    """Build a direct wt.exe invocation, without an intervening shell.

    Keep -w before new-tab and tab options after it. Normalize ASCII semicolons
    before both title copies because Terminal treats them as command separators
    even inside quoted values. Encode the PowerShell title as base64 so a leading
    hyphen cannot bind as a parameter. Windows argv round-trip tests alone do not
    prove Terminal parsing. Omit empty display arguments rather than relying on
    Terminal to preserve empty tokens; armed tabs wait for Enter unless run is set.
    """
    if row.agent not in ("claude", "codex"):
        raise ValueError("unsupported agent")
    if row.agent == "codex":
        validate_resume_args(row.resume_args)
    prompt_str, age_str = prompt_and_age_text(row, now)
    title = normalize_title(row.title)

    argv = [
        "wt.exe",
        "-w",
        window_name,
        "new-tab",
        "-d",
        row.cwd,
        "--title",
        title,
        "--suppressApplicationTitle",
        "powershell.exe",
        "-NoExit",
        "-NoProfile",
        "-File",
        str(arm_script),
        "-SessionId",
        row.session_id,
        "-TitleB64",
        _title_b64(title),
    ]
    if row.agent == "codex":
        argv += ["-Agent", "codex", "-CwdB64", _title_b64(row.cwd),
                 "-ResumeArgsB64", _title_b64(json.dumps(list(row.resume_args)))]
        if row.archived:
            argv.append("-Archived")
    if prompt_str:
        argv += ["-Prompts", prompt_str]
    if age_str:
        argv += ["-Age", age_str]
    if run:
        argv.append("-Run")
    if any(";" in argument for argument in argv):
        raise ValueError("ASCII semicolons are unsupported in restore paths or arguments")
    return argv


def _title_b64(title: str) -> str:
    """Encode UTF-8 text without a leading hyphen that could bind as a PowerShell flag."""
    return base64.b64encode(title.encode("utf-8")).decode("ascii")


@dataclass(frozen=True)
class Skip:
    """One selected row pre-flight rejected at the spawn point (§7 step 2,
    §3.9): the session file or cwd it had when the picker rendered it is
    gone by the time Enter's decision reaches here."""

    row: Row
    reason: str


@dataclass(frozen=True)
class SpawnFailure:
    """The row where launch/readiness failed; later rows are not attempted."""

    row: Row
    error: str


@dataclass(frozen=True)
class RestoreOutcome:
    """What `restore_selected` actually did, for a caller (or a test) that
    wants structured data instead of parsed text.

    `spawned` records process creation, not readiness. An accepted launch
    whose exit/readiness failed remains here and in `unverified`; a row
    that failed preflight or whose spawn raised never appears here.
    """

    spawned: tuple[Row, ...]
    skipped: tuple[Skip, ...]
    failed: tuple[SpawnFailure, ...]
    run: bool
    ready: tuple[Row, ...] = ()
    unverified: tuple[Row, ...] = ()
    not_attempted: tuple[Row, ...] = ()
    record_error: str | None = None
    verification_error: str | None = None


def _select_rows(row_set: Rows, selected_ids: frozenset[str]) -> list[Row]:
    """Resolve selected IDs in source-row order; destination planning follows."""
    return [row for row in (*row_set.desk, *row_set.young) if row.session_id in selected_ids]


def provider_read_errors(emit: Emit) -> dict[str, str]:
    """A missing live identity is not evidence of absence when its reader failed."""
    claude_error = emit.claude_error
    if not claude_error and health.is_registry_error(emit.claude_process_count, len(emit.sessions)):
        claude_error = health.REGISTRY_ERROR
    return {agent: error for agent, error in (
        ("claude", claude_error), ("codex", emit.codex_error),
    ) if error}


def preflight(
    rows_to_check: list[Row],
    session_file: SessionFileLookup,
    cwd_exists: CwdExists,
    codex_lookup: CodexLookup = lookup_codex,
) -> tuple[list[Row], list[Skip]]:
    """Recheck session files and working directories immediately before launching.

    Either may have disappeared while the picker was open. Codex metadata and
    resume arguments are validated again too. Liveness is not repolled here:
    a session becoming live while the picker is open remains an accepted limit.
    """
    ok: list[Row] = []
    skipped: list[Skip] = []
    for row in rows_to_check:
        if row.agent == "codex":
            info = codex_lookup(row.session_id)
            reason = info.reason
            if info.path is None:
                reason = reason or "Codex rollout missing"
            if info.archived and not row.archived:
                reason = "Codex session archived since selection; reopen the picker"
            try:
                validate_resume_args(row.resume_args)
            except ValueError as exc:
                reason = str(exc)
            if reason:
                skipped.append(Skip(row, reason))
                continue
            row = replace(row, session_file=info.path, archived=info.archived)
        elif row.agent != "claude":
            skipped.append(Skip(row, "unsupported agent"))
            continue
        elif session_file(row.session_id) is None:
            skipped.append(Skip(row, "session file deleted"))
            continue
        if not row.cwd:
            skipped.append(Skip(row, "cwd unknown"))
        elif ";" in row.cwd:
            skipped.append(Skip(row, "ASCII semicolons are unsupported in working directories"))
        elif not cwd_exists(row.cwd):
            skipped.append(Skip(row, "cwd gone"))
        else:
            ok.append(row)
    return ok, skipped


def _record_restore(store: Store, spawned: list[Row], run: bool, now: int, emit: Emit) -> None:
    """Record accepted launches, including uncertain ones, with unknown placement.

    Pending entries have already been persisted per launch before readiness.
    This action record is not used as a future restore target.
    """
    if not spawned:
        return

    registry_versions = normalize_registry_versions(entry.get("version") for entry in emit.sessions)
    sessions = tuple(
        Session(
            session_id=row.session_id,
            cwd=row.cwd,
            title=row.title,
            last_prompt_at=row.last_prompt_at,
            armed=not run,  # D18: armed spawns armed=True; --run spawns armed=False
            agent=row.agent,
            archived=row.archived,
            rollout_path=str(row.session_file) if row.agent == "codex" else None,
            resume_args=row.resume_args,
        )
        for row in spawned
    )
    store.write_restore(
        sessions=sessions,
        restored_at=now,
        boot_id=emit.boot_time,
        registry_versions=registry_versions,
    )


def _record_pending(store: Store, spawned: list[Row], run: bool, now: int) -> None:
    """Preserve accepted armed launches without expiring unrelated pending rows."""
    if run or not spawned:
        return
    entries = tuple(
        PendingEntry(
            session_id=row.session_id, title=row.title, cwd=row.cwd, armed_at=now,
            agent=row.agent, archived=row.archived,
            rollout_path=str(row.session_file) if row.agent == "codex" else None,
            resume_args=row.resume_args,
        )
        for row in spawned
    )
    # Merge under the store's transaction lock so capture cannot expire a stale
    # copy over this launch, nor can concurrent restores drop each other's rows.
    store.merge_pending(Pending(written_at=now, entries=entries))


def verify(
    store: Store,
    spawned: list[Row],
    run: bool,
    emit_runner: Callable[[], Emit],
    sleep: Callable[[float], None],
    verify_delay_s: float,
) -> tuple[list[str], str | None]:
    """Return display lines and an optional verification error after waiting.

    Under `--run`, requested provider identities must appear in the fresh
    observation. Armed pending counts are informational: capture may already
    have reconciled a resumed tab. Display readiness is checked separately.
    With no launches, return no lines/error and perform no reads or waits.
    """
    if not spawned:
        return [], None

    sleep(verify_delay_s)
    fresh = emit_runner()

    if run:
        live_ids = live_session_keys(fresh)
        missing = [row for row in spawned if (row.agent, row.session_id) not in live_ids]
        came_up = len(spawned) - len(missing)
        lines = [f"restored {came_up}/{len(spawned)}"]
        for row in missing:
            lines.append(f'  -- "{row.title}" not confirmed live at verification')
        error = (f"{len(missing)}/{len(spawned)} sessions not confirmed live at verification"
                 if missing else None)
        return lines, error

    pending = store.read_pending()
    pending_ids = pending.session_keys if pending is not None else frozenset()
    confirmed = sum(1 for row in spawned if (row.agent, row.session_id) in pending_ids)
    noun = "entry" if confirmed == 1 else "entries"
    summary = (
        f"pending.json holds {confirmed}/{len(spawned)} {noun} "
        "for requested armed launches."
    )
    caveat = (
        "    This does NOT confirm a session came up -- an armed tab hosts no "
        "process yet. Display readiness does not prove exact session identity."
    )
    return [summary, caveat], None


def restore_selected(
    selected_ids: frozenset[str],
    row_set: Rows,
    store: Store,
    emit: Emit,
    *,
    run: bool,
    session_file: SessionFileLookup,
    cwd_exists: CwdExists,
    codex_lookup: CodexLookup = lookup_codex,
    arm_script: Path = DEFAULT_ARM_SCRIPT,
    spawner: Spawner = default_spawn,
    observe: Observer | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], int] = _default_clock,
    sleep: Callable[[float], None] = time.sleep,
    emit_runner: Callable[[], Emit] = run_emit,
    verify_delay_s: float = DEFAULT_VERIFY_DELAY_S,
    prompt_and_age_text: PromptAndAgeText = picker.prompt_and_age_text,
    out: IO[str] | None = None,
) -> RestoreOutcome:
    """Preflight, plan fresh destinations, persist launches, then verify.

    Every append waits for bounded display readiness. A failure stops further
    launches and preserves accepted rows, including uncertain append outcomes.
    Exact live session identity remains a separate provider check.
    """
    stream = out
    now = clock()

    candidates = _select_rows(row_set, selected_ids)
    read_errors = provider_read_errors(emit)
    skipped = [Skip(row, f"current {row.agent} live check failed: {read_errors[row.agent]}")
               for row in candidates if row.agent in read_errors]
    spawn_ready, preflight_skips = preflight(
        [row for row in candidates if row.agent not in read_errors],
        session_file, cwd_exists, codex_lookup,
    )
    skipped.extend(preflight_skips)

    for skip in skipped:
        safe_print(f'skipping "{skip.row.title}": {skip.reason}', file=stream)

    if spawn_ready and not arm_script.is_file():
        # D23/F4: a missing/moved arm.ps1 produces F3's failure shape on
        # EVERY tab (a red "file not found" error under -NoExit) while
        # pending.json still gets written and `verify` still reports
        # success -- refuse to spawn anything rather than open N broken
        # tabs. `pyproject.toml`'s wheel target ships only `src/termsync`,
        # not `snapshot/`, so `DEFAULT_ARM_SCRIPT`'s `parents[2]` derivation
        # only resolves inside a source checkout -- this is the guard for
        # exactly that path being wrong or the file having moved.
        safe_print(f"error: arm.ps1 script not found: {arm_script}", file=stream)
        skipped.extend(Skip(row, "arm.ps1 script not found") for row in spawn_ready)
        spawn_ready = []

    def accepted(row: Row) -> None:
        safe_print(f"launch accepted: [{row.agent}] {row.session_id}  {row.title}  ({row.cwd})",
                   file=stream)
        _record_pending(store, [row], run, clock())

    result = launch_windows(
        spawn_ready,
        lambda row, name: spawner(build_wt_argv(
            row, arm_script, run, now, window_name=name,
            prompt_and_age_text=prompt_and_age_text)),
        accepted, observe=observe or run_desktop, monotonic=monotonic, sleep=sleep,
    )
    spawned = list(result.launched)
    failed = [SpawnFailure(row, error) for row, error in result.failures]
    unverified = tuple(row for row in result.launched if row not in result.ready)
    for failure in failed:
        safe_print(f'restore stopped at "{failure.row.title}": {failure.error}', file=stream)
    for row in unverified:
        safe_print(f"launched but unverified: [{row.agent}] {row.session_id}", file=stream)
    for row in result.not_attempted:
        safe_print(f"not attempted: [{row.agent}] {row.session_id}", file=stream)

    if not spawned:
        safe_print("Nothing selected -- nothing to restore." if not candidates
                   else "Nothing was restored -- pre-flight or launch readiness failed.",
                   file=stream)
        return RestoreOutcome((), tuple(skipped), tuple(failed), run,
                              not_attempted=result.not_attempted)

    verb = "Launches accepted for" if unverified else "Ran" if run else "Armed"
    safe_print(f"{verb} {len(spawned)} session(s):", file=stream)
    safe_print(f"display readiness observed {len(result.ready)}/{len(spawned)}; "
               "titles/count do not prove session identity.", file=stream)

    record_error = None
    try:
        _record_restore(store, spawned, run, now, emit)
    except Exception as exc:  # noqa: BLE001 -- D21: tabs are already open and
        # named on screen; a raise here must become a legible line, not a
        # traceback that erases them.
        record_error = f"{type(exc).__name__}: {exc}"
        safe_print(f"error: failed to record this restore: {record_error}", file=stream)

    verification_error = None
    try:
        lines, verification_error = verify(store, spawned, run, emit_runner, sleep, verify_delay_s)
        for line in lines:
            safe_print(line, file=stream)
    except Exception as exc:  # noqa: BLE001 -- D21: same reasoning as above
        verification_error = f"{type(exc).__name__}: {exc}"
        safe_print(f"error: post-restore verification failed: {verification_error}", file=stream)

    return RestoreOutcome(
        spawned=tuple(spawned), skipped=tuple(skipped), failed=tuple(failed), run=run,
        ready=result.ready, unverified=unverified, not_attempted=result.not_attempted,
        record_error=record_error, verification_error=verification_error,
    )
