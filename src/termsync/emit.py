"""Provider subprocess boundaries and independent capture-reader orchestration.

The Claude boundary runs the emitter and decodes its bytes
(``run_emit``), and parse the resulting JSON envelope (``parse_emit``). It
filters nothing and interprets nothing about the *content* of a session
entry -- no liveness check, no ``procStart`` comparison, no ``kind`` filter,
no title stripping. All of that is policy and lives in ``capture.py``
(§5 steps 5-9, D8). Keeping the boundary here at "decode and validate the
envelope" is what makes every policy rule in ``capture.py`` unit-testable
with no subprocess and no Windows interop at all: a test can construct an
``Emit`` directly, or hand ``parse_emit`` a JSON string, without ever
touching ``run_emit``. U7's combined reader additionally runs the bounded Codex
and desktop readers concurrently; placement policy remains outside this module.
"""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from termsync.capture_windows import DesktopObservation, run_desktop

# Resolved relative to this installed/checked-out package: src/termsync/emit.py
# -> parents[0] = src/termsync, parents[1] = src, parents[2] = repo root. This
# repo-relative resolution is why V0 only runs from the checkout -- packaging
# Snapshot-Desk.ps1 alongside an installed wheel so this resolves correctly
# outside a checkout is S8's problem (install.py / termsync.cmd), not this
# module's.
_DEFAULT_SCRIPT = Path(__file__).resolve().parents[2] / "snapshot" / "Snapshot-Desk.ps1"

# Keep a non-zero-exit failure message readable in a terminal / log line
# rather than dumping an entire PowerShell stack trace into status.json's
# `error` field.
_STDERR_TRUNCATE = 2000

_REQUIRED_KEYS = ("sessions", "claudeProcessCount", "bootTime")

# Review finding F1 [HIGH]: every key the pipeline's decisions depend on,
# per session entry. §5 steps 5-7's filters all default to *drop*
# (`entry.get("alive", False)`, a missing `procStart`/`procStartLive`,
# `entry.get("kind") != "interactive"`), so a field rename or disappearance
# upstream is indistinguishable from "every session closed" -- capture.py's
# drop-by-default policy is correct per-entry (§5), but it means a *schema*
# change silently produces `sessionsSeen: 0`, `outcome: "ok"` forever. §8
# names this in so many words: "an internal change at Anthropic turns `desk`
# into a machine that silently overwrites good layouts with empty ones while
# reporting `outcome: "ok"` forever -- the worst reachable state in the
# design." Enforcing this here, at the subprocess boundary, turns that into
# a loud `EmitError` (D7 -> `outcome: "error"`, no layout, retried at the
# next poll) the moment the shape drifts, rather than a silent empty desk.
#
# Values may be null (`procStartLive` is legitimately null when the H1
# guard fires; `bootTime` may be null per D1) -- only the KEY's presence is
# required. Unknown EXTRA keys are never rejected: the emitter is allowed to
# grow, and §13.2's design note makes `model.py`/the contract test the place
# that notices additions, not this boundary.
_REQUIRED_SESSION_KEYS = (
    "sessionId",
    "alive",
    "procStart",
    "procStartLive",
    "kind",
    "titleRaw",
    "titleAttachOk",
    "cwd",
    "name",
)


class EmitError(RuntimeError):
    """`-Emit` could not be run, or its output could not be parsed as the envelope."""


@dataclass(frozen=True)
class Emit:
    """The parsed `-Emit` envelope (§5 step 4), verbatim.

    `sessions` holds each registry entry as reported, plus the emitter's own
    additions (`alive`, `procStartLive`, `titleRaw`, `titleAttachOk`) --
    unfiltered, uninterpreted dicts. `capture.py` is the only module that
    reads into their contents.
    """

    sessions: tuple[dict[str, Any], ...]
    claude_process_count: int
    boot_time: int | None  # may be null -- see D1; a missing boot read must
    # never cost the whole snapshot (H1's failure class), so the emitter
    # guards the CIM read and reports null rather than dying. What null
    # means for a capture is `cli.py`'s decision, not this module's.
    codex: tuple[dict[str, Any], ...] = ()
    codex_error: str | None = None
    claude_error: str | None = None
    desktop: DesktopObservation | None = None


def run_capture(claude_runner=None, codex_runner=None, desktop_runner=None) -> Emit:
    """Independent, concurrent readers with process deadlines of 30/20/12s.

    UIA lives in its own killable process, never in a console-attached helper.
    Concurrent reads leave about half the scheduled task's minute for storage
    and status. This bounds external reads, not OS scheduling or filesystem I/O.
    """
    from dataclasses import replace

    from termsync.capture_codex import run_worker
    from termsync.model import Session

    with ThreadPoolExecutor(max_workers=3) as pool:
        claude_read = pool.submit(claude_runner or run_emit)
        codex_read = pool.submit(codex_runner or run_worker)
        desktop_read = pool.submit(desktop_runner or run_desktop)

    claude_error = None
    try:
        observed = claude_read.result()
        claude_error = observed.claude_error
    except Exception as exc:
        claude_error = f"{type(exc).__name__}: {exc}"
        observed = Emit(sessions=(), claude_process_count=0, boot_time=None)
    codex_rows = ()
    codex_error = None
    try:
        report = codex_read.result()
        for raw in report["sessions"]:
            session = Session.from_json(raw)
            if session.agent != "codex" or not session.session_id or not session.cwd:
                raise ValueError("Invalid Codex session identity")
        codex_rows = tuple(report["sessions"])
        codex_error = "; ".join(report["errors"]) or None
    except Exception as exc:
        codex_error = f"{type(exc).__name__}: {exc}"
    try:
        desktop = desktop_read.result()
        if not isinstance(desktop, DesktopObservation):
            raise ValueError("Invalid desktop observation")
    except Exception as exc:
        desktop = DesktopObservation(error=f"{type(exc).__name__}: {exc}")
    return replace(observed, codex=codex_rows, codex_error=codex_error,
                   claude_error=claude_error, desktop=desktop)


def parse_emit(text: str) -> Emit:
    """Parse `-Emit`'s JSON envelope. Pure: no subprocess, no filesystem, no clock.

    Validates the envelope shape -- that the text is JSON, that its top
    level is an object, that `sessions` is an array, and that all three
    required top-level keys (`sessions`, `claudeProcessCount`, `bootTime`)
    are present. `bootTime: null` is accepted without complaint (D1); what to
    do about a null boot time is the *capture* path's decision, not this
    parser's.

    Also validates the PER-SESSION contract (F1): every entry must carry
    `_REQUIRED_SESSION_KEYS`, or this raises `EmitError` naming the missing
    key(s) and the offending entry. This is not "interpreting" a field --
    nothing here reads what `alive` or `kind` mean, only that the key
    exists. See `_REQUIRED_SESSION_KEYS`'s comment for why this boundary,
    and not a downstream default, is where the check has to live.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EmitError(f"-Emit produced unparseable JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise EmitError(f"-Emit's top level must be a JSON object, got {type(payload).__name__}")

    for key in _REQUIRED_KEYS:
        if key not in payload:
            raise EmitError(f"-Emit's output is missing required top-level key {key!r}")
    if payload.get("claudeError") is not None and not isinstance(payload["claudeError"], str):
        raise EmitError("-Emit's claudeError must be a string or null")

    sessions = payload["sessions"]
    if not isinstance(sessions, list):
        raise EmitError(f"-Emit's 'sessions' must be a JSON array, got {type(sessions).__name__}")

    for index, raw_entry in enumerate(sessions):
        if not isinstance(raw_entry, dict):
            raise EmitError(
                f"-Emit session entry {index} must be a JSON object, got {type(raw_entry).__name__}"
            )
        missing = [key for key in _REQUIRED_SESSION_KEYS if key not in raw_entry]
        if missing:
            label = raw_entry.get("sessionId", f"entry {index}")
            raise EmitError(
                f"-Emit session {label!r} is missing required key(s): {', '.join(missing)}"
            )

    return Emit(
        sessions=tuple(sessions),
        claude_process_count=payload["claudeProcessCount"],
        boot_time=payload["bootTime"],
        claude_error=payload.get("claudeError"),
    )


def run_emit(script: Path | None = None, timeout_s: float = 30.0) -> Emit:
    """Run `Snapshot-Desk.ps1 -Emit` and parse its output.

    Uses `powershell.exe` (5.1), never `pwsh` -- every measurement in §14 and
    every smoke run in this repo used 5.1, and switching interpreters is not
    this unit's call to make.

    Decodes stdout as UTF-8 **explicitly**, by calling `subprocess.run`
    without `text=True` and decoding the captured bytes ourselves. This
    machine's Python locale encoding is cp1252, and console titles carry
    ✳-family status glyphs (§3.5); `text=True` decodes through that
    locale and mangles or raises. This is not hypothetical: a probe of the
    golden fixture hit exactly this `UnicodeDecodeError` before this fix.

    **D30: `creationflags=subprocess.CREATE_NO_WINDOW`.** `termsync snap`
    now runs, via the scheduled task, under `pythonw.exe` -- a windowless
    host with no console of its own (see `install.py`'s D30). Spawning
    `powershell.exe` from a console-less parent makes Windows allocate that
    child its OWN new, visible console window; without this flag the
    windowless-task fix (installing against `pythonw.exe` instead of
    `termsync.cmd`) would just move the popping window one process down,
    from `cmd.exe` to this `powershell.exe` call, rather than removing it.
    `CREATE_NO_WINDOW` suppresses that allocation. This is scoped to THIS
    call only -- `restore.default_spawn`'s `wt.exe` spawn is deliberately
    left untouched (its window is the entire point of `termsync restore`).
    """
    script_path = script if script is not None else _DEFAULT_SCRIPT
    if not script_path.is_file():
        # A missing script must not surface as a confusing "file not found"
        # error from deep inside subprocess/OS machinery -- name the path so
        # the failure is legible from a scheduled-task log with no attached
        # terminal.
        raise EmitError(f"-Emit script not found: {script_path}")

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-Emit",
            ],
            capture_output=True,
            timeout=timeout_s,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise EmitError(f"powershell.exe not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EmitError(f"-Emit timed out after {timeout_s}s") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:_STDERR_TRUNCATE]
        raise EmitError(f"-Emit exited with code {result.returncode}: {stderr}")

    if not result.stdout:
        # H1's exact signature (docs/reviews/2026-08-23-review-phases-1-4.md,
        # finding H1): a registry pid resolving to a process whose
        # `.StartTime` is unreadable used to kill the whole script under
        # `$ErrorActionPreference = 'Stop'`, printing nothing at all. The
        # PowerShell side now guards that read (see Snapshot-Desk.ps1's H1
        # comments), but an empty stdout is still the unambiguous signature
        # of "the emitter died and printed nothing" on the Python side, from
        # any cause -- so it stays a hard error here rather than being read
        # as "an empty desk".
        raise EmitError(
            "-Emit produced empty stdout; check the PowerShell helper and its read permissions."
        )

    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EmitError(f"-Emit's stdout was not valid UTF-8: {exc}") from exc

    return parse_emit(text)
