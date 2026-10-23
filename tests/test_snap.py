"""End-to-end tests for `cli.snap` (S3, §5 steps 10-13, §13.4).

Runs the whole pipeline against a fixture `Emit` and a real `Store` rooted at
`tmp_path` -- no Windows, no live machine state, no real subprocess. The
emit-runner and clock are injected per `snap`'s signature, which is the
whole point of that injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from termsync import cli as cli_module
from termsync import health
from termsync.cli import BOOT_ID_UNKNOWN_WARNING, snap
from termsync.emit import Emit, EmitError
from termsync.model import Pending, PendingEntry
from termsync.selection import select_target
from termsync.store import Store

PROC_START = "130000000000000003"


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sessionId": "s1",
        "cwd": r"C:\fixture\tool",
        "alive": True,
        "procStart": PROC_START,
        "procStartLive": PROC_START,
        "kind": "interactive",
        "titleRaw": "\u2733 termsync work",
        "titleAttachOk": True,
        "name": "termsync-1",
        "version": "2.1.238",
    }
    base.update(overrides)
    return base


def emit_of(*entries: dict[str, Any], claude_process_count: int | None = None) -> Emit:
    return Emit(
        sessions=tuple(entries),
        claude_process_count=(
            claude_process_count if claude_process_count is not None else len(entries)
        ),
        boot_time=1787235232500,
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def clock_returning(*values: int):
    # Existing tests model a fixed wall clock; timing tests supply both readings.
    it = iter(values)
    last = values[-1]

    def _clock() -> int:
        return next(it, last)

    return _clock


@pytest.mark.parametrize("mode", ["ok", "degraded", "registry_error", "reader_error"])
def test_snap_records_start_before_reader_and_finish_before_status_write(
    store: Store, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    events = []
    readings = iter([1000, 1250])

    def clock() -> int:
        value = next(readings)
        events.append(("clock", value))
        return value

    def reader() -> Emit:
        events.append(("reader", None))
        assert events == [("clock", 1000), ("reader", None)]
        if mode == "reader_error":
            raise EmitError("test reader failed")
        if mode == "registry_error":
            return emit_of(claude_process_count=1)
        return emit_of(entry(titleAttachOk=mode != "degraded"))

    original_write = store._atomic_write_text

    def write(path: Path, text: str) -> None:
        if path == store.status_path:
            events.append(("status_write", None))
            assert events[-2] == ("clock", 1250)
        original_write(path, text)

    monkeypatch.setattr(store, "_atomic_write_text", write)
    code = snap(store, emit_runner=reader, clock=clock)
    assert code == (1 if mode.endswith("error") else 0)
    status = store.read_status()
    assert status is not None
    assert status.started_at == status.ran_at == 1000
    assert status.finished_at == 1250
    assert status.duration_ms == 250
    assert status.last_success_at == (None if code else 1000)
    assert events == [("clock", 1000), ("reader", None), ("clock", 1250), ("status_write", None)]


def test_deduplicated_run_updates_timing_but_keeps_freshness_on_ran_at(store: Store) -> None:
    for start, finish in [(1000, 1250), (2000, 2400)]:
        assert snap(store, emit_runner=lambda: emit_of(entry()),
                    clock=clock_returning(start, finish)) == 0
    status = store.read_status()
    assert status is not None
    assert status.wrote_version is None
    assert (status.started_at, status.finished_at, status.duration_ms) == (2000, 2400, 400)
    assert status.ran_at == status.last_success_at == 2000
    assert health.freshness_report(status.last_success_at, 62_000).startswith(
        "Last successful snapshot: 1 minute ago"
    )


# --- the error path: primary error rule -----------------------------------


def test_registry_error_writes_a_status_record_and_no_layout(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(claude_process_count=2)  # >=1 claude.exe, zero readable entries

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 1
    status = store.read_status()
    assert status is not None
    assert status.outcome == "error"
    assert status.error == health.REGISTRY_ERROR
    assert store.newest() is None


def test_the_error_rule_gets_the_pre_filter_count_not_the_surviving_one(store: Store) -> None:
    """Review finding F7 [MEDIUM]. §13.4 names this explicitly: the count
    `cli.py` passes to `health.is_registry_error` must be `len(emit["sessions"])`
    (D3/M9), taken BEFORE the alive/procStart/kind filters -- NOT
    `len(result.sessions)`, the fully-filtered survivor count, which is
    precisely the substitution D3 forbids: passing it fires the rule
    whenever every current session happens to be backgrounded, a legitimate
    state. Every OTHER case in this file has `readable_entry_count ==
    len(result.sessions)` by construction (the error tests use zero
    entries, the success tests use one clean survivor), so the wrong wiring
    was invisible to the rest of this suite -- this is the one case that
    tells the two counts apart: three entries that all PARSE but all fail
    the `kind` filter, with `claude_process_count >= 1`.

    Mutation-verified by the coordinator: temporarily wiring
    `len(result.sessions)` into `cli.snap`'s call to `is_registry_error`
    makes this test fail with `AssertionError: assert 'error' == 'ok'`
    (`status.outcome` becomes `"error"`); reverted before finishing.
    """

    def emit_runner() -> Emit:
        return emit_of(
            entry(sessionId="a", kind="bg"),
            entry(sessionId="b", kind="bg"),
            entry(sessionId="c", kind="bg"),
            claude_process_count=3,
        )

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    status = store.read_status()
    assert status is not None
    assert status.outcome == "ok"


# --- degraded on title fallback ---------------------------------------------


def test_run_with_fallback_records_degraded_and_still_writes_a_layout(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(entry(titleAttachOk=False, titleRaw=None, name="termsync-fallback"))

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    status = store.read_status()
    assert status is not None
    assert status.outcome == "degraded"
    assert status.title_fallbacks == 1
    layout = store.newest()
    assert layout is not None
    assert layout.sessions[0].title == "termsync-fallback"


def test_run_with_no_fallback_records_ok(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(entry())

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    status = store.read_status()
    assert status is not None
    assert status.outcome == "ok"


# --- dedup + lastSuccessAt advances -----------------------------------------


def test_unchanged_desk_dedups_and_still_advances_last_success_at(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(entry())

    code1 = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))
    assert code1 == 0
    first_status = store.read_status()
    assert first_status is not None
    assert first_status.wrote_version == 1
    first_layout = store.newest()
    assert first_layout is not None
    assert first_layout.version == 1

    code2 = snap(store, emit_runner=emit_runner, clock=clock_returning(2000))
    assert code2 == 0
    second_status = store.read_status()
    assert second_status is not None

    # Same session/cwd/armed -> same hash -> dedup: no new version allocated.
    assert second_status.wrote_version is None
    second_layout = store.newest()
    assert second_layout is not None
    assert second_layout.version == 1  # still version 1, refreshed in place

    # lastSuccessAt still advances even though dedup wrote no new layout (§6).
    assert second_status.last_success_at == 2000
    assert second_status.last_success_at > first_status.last_success_at


# --- unhandled exception still writes a status record -----------------------


def test_unhandled_exception_still_writes_a_status_record(store: Store) -> None:
    def emit_runner() -> Emit:
        raise RuntimeError("boom: something in the pipeline broke")

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 1
    status = store.read_status()
    assert status is not None
    assert status.outcome == "error"
    assert "boom: something in the pipeline broke" in status.error
    assert store.newest() is None


def test_emit_error_from_the_emit_runner_still_writes_a_status_record(store: Store) -> None:
    """EmitError (e.g. H1's empty-stdout case) is an unhandled-exception path too."""

    def emit_runner() -> Emit:
        raise EmitError("-Emit produced empty stdout")

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 1
    status = store.read_status()
    assert status is not None
    assert status.outcome == "error"
    assert "empty stdout" in status.error


def test_a_raising_clock_still_leaves_a_status_record_via_the_default_clock_fallback(
    store: Store,
) -> None:
    """Review finding F9/L7: `clock()` used to be called BEFORE the `try`, so
    a raising clock escaped `snap` with NO status record at all -- D7's
    "every path" promise held for every path except this one. `clock()` is
    called inside the `try` now, and the exception handler falls back to
    `_default_clock()` (real wall-clock time) when `now` was never
    successfully read, so the record still lands with SOME timestamp rather
    than none.
    """

    def raising_clock() -> int:
        raise RuntimeError("clock source unavailable")

    def emit_runner() -> Emit:
        return emit_of(entry())

    code = snap(store, emit_runner=emit_runner, clock=raising_clock)

    assert code == 1
    status = store.read_status()
    assert status is not None
    assert status.outcome == "error"
    assert "clock source unavailable" in status.error
    assert status.ran_at is not None  # SOME timestamp, not the crash


# --- bootTime null -----------------------------------------------------------


def test_null_boot_time_writes_the_layout_with_a_null_boot_id(store: Store) -> None:
    """Review finding F3 [MEDIUM]. This used to refuse to write a layout at
    all when `bootTime` was null: no layout, `outcome: "error"`, exit 1, and
    `test_null_boot_time_writes_an_error_status_and_no_layout` (this test's
    former self) asserted that refusal as CORRECT behaviour -- the same
    shape of defect as `test_target_ignores_a_larger_layout_from_a_previous_boot`
    from the phase 1-4 review, a test that locked in a policy the code
    should never have had.

    The corrected reasoning: `selection.select_target` treats a null
    `boot_id` as an ordinary value throughout (verified in
    `test_selection.py`'s null-boot-id cases, not assumed) -- a null-boot
    layout is, at worst, excluded from the two-boot candidate window, which
    is BYTE-FOR-BYTE the outcome of never writing it. So writing only ever
    matches or beats refusing. Refusing instead means a PERSISTENT
    `LastBootUpTime` failure (the emitter's own guard names WMI-down,
    permissions, and a locked-down machine as causes) produces zero
    recorded desks forever -- the store is the one thing this tool cannot
    reconstruct (§4), and that is a worse trade than one imprecise sentence
    in the shrink guard's `!` block (§8) when two null-boot layouts happen
    to land from genuinely different boots.
    """

    def emit_runner() -> Emit:
        return Emit(sessions=(entry(),), claude_process_count=1, boot_time=None)

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    status = store.read_status()
    assert status is not None
    assert status.outcome == "ok"
    assert status.wrote_version == 1

    layout = store.newest()
    assert layout is not None
    assert layout.boot_id is None
    assert len(layout.sessions) == 1

    # Reachable through the same public path a restore would use.
    target = select_target(store.all_layouts(), boot_id=None)
    assert target is not None
    assert target.layout.version == layout.version


def test_null_boot_time_prints_a_warning_but_does_not_error(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    def emit_runner() -> Emit:
        return Emit(sessions=(entry(),), claude_process_count=1, boot_time=None)

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    captured = capsys.readouterr()
    assert BOOT_ID_UNKNOWN_WARNING in captured.err


# --- exit codes per D7 -------------------------------------------------------


def test_exit_code_is_zero_on_ok(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(entry())

    assert snap(store, emit_runner=emit_runner, clock=clock_returning(1000)) == 0


def test_exit_code_is_zero_on_degraded(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(entry(titleAttachOk=False, titleRaw=None))

    assert snap(store, emit_runner=emit_runner, clock=clock_returning(1000)) == 0


def test_exit_code_is_one_on_error(store: Store) -> None:
    def emit_runner() -> Emit:
        return emit_of(claude_process_count=1)

    assert snap(store, emit_runner=emit_runner, clock=clock_returning(1000)) == 1


# --- pending union reaches the store -----------------------------------------


def test_pending_entries_are_unioned_and_survive_into_the_written_layout(store: Store) -> None:
    store.write_pending(
        Pending(
            written_at=1,
            entries=(
                PendingEntry(
                    session_id="armed-1",
                    title="Armed session",
                    cwd=r"C:\fixture\tool",
                    armed_at=1,
                ),
            ),
        )
    )

    def emit_runner() -> Emit:
        return emit_of(entry(sessionId="live-1"))

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))
    assert code == 0

    layout = store.newest()
    assert layout is not None
    ids = {s.session_id for s in layout.sessions}
    assert ids == {"live-1", "armed-1"}


def test_a_resumed_pending_entry_is_expired_before_capture(store: Store) -> None:
    """§5 step 9 / §7 step 2: clear a pending entry once its session is live."""

    store.write_pending(
        Pending(
            written_at=1,
            entries=(PendingEntry(session_id="s1", title="was armed", cwd="c", armed_at=1),),
        )
    )

    def emit_runner() -> Emit:
        # s1 has now come back live in the registry -- the same id as the pending entry.
        return emit_of(entry(sessionId="s1"))

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))
    assert code == 0

    layout = store.newest()
    assert layout is not None
    assert len(layout.sessions) == 1
    assert layout.sessions[0].armed is False  # the live entry, not a duplicated armed one
    assert store.read_pending() is None  # expired


def test_an_armed_pending_entry_reappearing_as_a_bg_kind_is_not_expired_and_not_duplicated(
    store: Store,
) -> None:
    """Review finding M3/F4: the position this test's neighbour above implies
    it covers was actually untested -- the `entry(sessionId="s1")` case
    above is alive+interactive+procStart-matched, so it passes identically
    whether `cli.py` builds its "live" set from the raw `alive` flag or from
    `capture.live_session_ids`'s filtered one. This is the case where the
    two definitions used to disagree: `s1` reappears as `kind: "bg"` --
    alive, but not carried into the layout by `build_capture` (bg owns no
    tab). With one shared filter (`capture.live_session_ids`), `expire_pending`
    must NOT clear the pending entry here, so the armed tab survives in
    `pending.json` rather than vanishing from both files at once.
    """
    store.write_pending(
        Pending(
            written_at=1,
            entries=(PendingEntry(session_id="s1", title="was armed", cwd="c", armed_at=1),),
        )
    )

    def emit_runner() -> Emit:
        return emit_of(entry(sessionId="s1", kind="bg"))

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))
    assert code == 0

    layout = store.newest()
    assert layout is not None
    ids = {s.session_id: s for s in layout.sessions}
    assert "s1" in ids, "the armed session must not vanish from the layout"
    assert ids["s1"].armed is True

    pending = store.read_pending()
    assert pending is not None
    assert pending.session_ids == frozenset({"s1"}), (
        "the armed session must not ALSO vanish from pending.json"
    )


# --- F2/M1: printing must not be able to decide the outcome ------------------


def test_a_successful_runs_printing_failure_does_not_flip_outcome_to_error(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding F2 [HIGH], demonstrated by the coordinator: `_print_summary`
    ran INSIDE `snap`'s `try`, after a successful `record_run`. When it
    raised (the trigger measured: a store read-warning's em dash, unencodable
    on cp437/cp850), the exception fell into the D7 handler, which wrote a
    SECOND status record over the correct one -- a run that captured a
    session and wrote layout `000001.json` was recorded as `outcome:
    "error"`, `sessionsSeen: 0`, `wroteVersion: null`. `snap`'s `recorded`
    flag makes the handler a no-op for recording once a true record exists
    for this run; `exit_code` is tracked alongside it so the process exit
    code does not contradict a status that says `ok`. This monkeypatches
    `_print_summary` directly, bypassing `_safe_print`'s own encoding fix,
    to isolate the `recorded`-flag mechanism from the encoding-specific one
    (see `test_safe_print_survives_a_stream_that_cannot_encode_the_text`
    below for that half).
    """

    def emit_runner() -> Emit:
        return emit_of(entry())

    def boom_summary(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("stdout exploded")

    monkeypatch.setattr(cli_module, "_print_summary", boom_summary)

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    status = store.read_status()
    assert status is not None
    assert status.outcome == "ok"
    assert status.sessions_seen == 1
    assert status.wrote_version == 1
    assert store.newest() is not None
    assert code == 0, "the TRUE outcome was ok; a print failure must not flip the exit code"


def test_two_consecutive_registry_errors_do_not_triple_count_when_the_print_raises(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry-error branch's own error `print` must not be able to
    double-record via the exception handler either. Measured by the review
    at 1 -> 3 across two failed runs under the OLD handler; must now be
    1 -> 2.
    """

    def emit_runner() -> Emit:
        return emit_of(claude_process_count=2)  # >=1 claude.exe, zero readable entries

    def boom_print(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("stdout exploded")

    monkeypatch.setattr(cli_module, "_safe_print", boom_print)

    snap(store, emit_runner=emit_runner, clock=clock_returning(1000))
    snap(store, emit_runner=emit_runner, clock=clock_returning(2000))

    status = store.read_status()
    assert status is not None
    assert status.consecutive_failures == 2, "not 3 -- a print failure must not double-record"


def test_safe_print_survives_a_stream_that_cannot_encode_the_text() -> None:
    """`_safe_print`'s own half of F2: a `UnicodeEncodeError` from the stream
    must be caught and retried with lossy substitution, never raised."""
    import io

    class NarrowStream(io.StringIO):
        encoding = "ascii"

        def write(self, s: str) -> int:
            s.encode("ascii")  # raises UnicodeEncodeError on anything non-ASCII
            return super().write(s)

    stream = NarrowStream()
    cli_module._safe_print("caf\u00e9 au lait", file=stream)  # café
    assert "caf" in stream.getvalue()
    assert "au lait" in stream.getvalue()


# --- D4: store read warnings never flip outcome -------------------------------


def test_a_store_read_warning_is_reported_but_never_flips_outcome(store: Store) -> None:
    """Review finding L5/F9: D4 -- "store read warnings are surfaced but
    never `degraded`" -- had no test at the `cli.snap` level. This is also
    the test that would have caught F2's success-path status overwrite,
    since the corrupt-layout warning is exactly what triggers `_print_read_warnings`
    to print something (and, before the em-dash fix, something unencodable).
    """
    store.layouts_dir.mkdir(parents=True, exist_ok=True)
    (store.layouts_dir / "000001.json").write_text("{not json", encoding="utf-8")

    def emit_runner() -> Emit:
        return emit_of(entry())

    code = snap(store, emit_runner=emit_runner, clock=clock_returning(1000))

    assert code == 0
    status = store.read_status()
    assert status is not None
    assert status.outcome == "ok"
    assert any("000001" in warning for warning in store.read_warnings)


def test_snap_help_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    """A smoke check that argv parsing/dispatch is wired -- the real
    `python -m termsync snap --help` invocation is exercised live, this just
    pins that `cli.build_parser` accepts the `snap` subcommand with `--store`."""
    from termsync.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["snap", "--store", "C:\\scratch"])
    assert args.command == "snap"
    assert str(args.store) == "C:\\scratch"
