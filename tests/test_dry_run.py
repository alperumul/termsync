"""Behaviour tests for `termsync restore --dry-run` (S10, §7 step 6, §13.3, §13.4).

`dry_run` is exercised end to end against a real `Store` rooted at `tmp_path` --
no Windows, no live machine state, no real subprocess. Most tests here hand
`dry_run` a fake `row_builder`, so they do not depend on `rows.build_rows` at
all. Review finding F8/M6 [MEDIUM] added the seam that was missing before
this round: several tests below drive `dry_run` with the REAL
`rows.build_rows` instead, against a fixture `~/.claude`-shaped tree rooted
at `tmp_path` -- that is what closes the S9<->S10 integration gap the review
found (zero of the original 14 `dry_run(...)` calls exercised the real join).
`history_path` and `claude_dir` are always pointed at `tmp_path`, never at
the real `~/.claude` tree, so the whole suite runs with zero live machine
state in the loop -- exactly what §13.4 requires of this file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from termsync import cli, health
from termsync.cli import build_parser, dry_run, restore_interactive
from termsync.emit import Emit
from termsync.model import Pending, PendingEntry, Session
from termsync.rows import Row, Rows
from termsync.store import Store

pytestmark = pytest.mark.usefixtures("ready_restore_desktop")


def test_u9_dry_run_and_picker_share_sections_without_writes(store, tmp_path):
    from termsync.picker import initial_state, render

    store.write_capture(sessions=(Session("s1", "c", "t"),), captured_at=1000, boot_id=100)
    history_path, claude_dir = unused_paths(tmp_path)
    rows = Rows((
        replace(make_row(session_id="late", title="Last tab"), window_ordinal=7, tab_index=4),
        make_row(session_id="unknown", title="Unknown tab"),
        replace(make_row(session_id="first", title="First tab"), window_ordinal=2, tab_index=0),
        replace(make_row(session_id="early", title="Early tab"), window_ordinal=7, tab_index=1),
        replace(make_row(session_id="archive"), agent="codex", archived=True),
    ), (make_row(session_id="young", young=True),))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    output = StringIO()
    assert dry_run(
        store, emit_runner=lambda: emit_of(boot_time=100), clock=lambda: 2000,
        history_path=history_path, claude_dir=claude_dir,
        row_builder=lambda *_: rows, out=output,
    ) == 0
    text = output.getvalue()
    def headers(lines):
        return [line for line in lines if line.startswith(
            ("WINDOW ", "ARCHIVED CODEX", "STARTED SINCE"))]
    assert headers(text.splitlines()) == headers(render(initial_state(rows), now=2000))
    assert headers(text.splitlines())[:3] == [
        "WINDOW 2  (1 session)", "WINDOW 7  (2 sessions)", "WINDOW UNKNOWN  (1 session)",
    ]
    assert text.index("First tab") < text.index("Early tab") < text.index("Last tab")
    for row in rows.desk + rows.young:
        assert row.session_id in text and row.cwd in text
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_u9_dry_run_young_only_has_no_contradictory_empty_message(store, tmp_path):
    store.write_capture(sessions=(), captured_at=1000, boot_id=100)
    history_path, claude_dir = unused_paths(tmp_path)
    output = StringIO()
    assert dry_run(
        store, emit_runner=lambda: emit_of(), clock=lambda: 2000,
        history_path=history_path, claude_dir=claude_dir,
        row_builder=lambda *_: Rows((), (make_row(young=True),)), out=output,
    ) == 0
    assert "Nothing to restore" not in output.getvalue()
    assert "STARTED SINCE THE LAST SNAPSHOT  (1 session) -- window unknown" in output.getvalue()


@pytest.mark.parametrize("failure", ["baseline", "readiness", "record"])
def test_u8_cli_reports_failure_and_preserves_accepted_launch(
    store, tmp_path, monkeypatch, failure,
):
    from termsync.capture_windows import DesktopObservation

    store.write_capture(sessions=(Session("s1", "c", "t"),), captured_at=1000, boot_id=100)
    history_path, claude_dir = unused_paths(tmp_path)
    spawner = MagicMock(return_value=MagicMock(poll=lambda: 0))
    observations = iter([DesktopObservation((), 0), DesktopObservation(error="UIA failed")])
    if failure == "record":
        monkeypatch.setattr(store, "write_restore",
                            lambda **_: (_ for _ in ()).throw(OSError("disk full")))
    code = restore_interactive(
        store, emit_runner=lambda: emit_of(boot_time=100), clock=lambda: 2000,
        history_path=history_path, claude_dir=claude_dir,
        row_builder=lambda *_: Rows((make_row(session_id="s1"),), ()),
        key_reader=scripted_keys(["ENTER"]), is_tty=lambda: True,
        session_file=lambda _: Path("fake.jsonl"), cwd_exists=lambda _: True,
        spawner=spawner, sleep=lambda _: None,
        observe=(None if failure == "record" else
                 lambda _: DesktopObservation(error="denied") if failure == "baseline"
                 else next(observations)),
    )
    assert code == 1
    assert spawner.call_count == (0 if failure == "baseline" else 1)
    if failure != "baseline":
        assert store.read_pending().session_ids == frozenset({"s1"})

# --- fixtures / helpers -------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """§13.4: "`subprocess` is never invoked" by `--dry-run`, asserted, not assumed.

    Applied to every test in this file automatically. `emit.run_emit` (the
    default `emit_runner`) is the only place in this codebase that calls
    `subprocess.run` (verified: `grep -rn subprocess src/` finds exactly one
    call site, `emit.py:167`); patching the module-level `subprocess.run`
    object here catches it regardless of which module holds the reference,
    since `emit.py` does `import subprocess` and calls `subprocess.run(...)`
    off the same module object this fixture patches. Every test also injects
    its own fake `emit_runner`, so this is belt-and-braces proof, not the
    only thing preventing a real spawn.

    **Deliberately does NOT patch `subprocess.Popen`.** `restore.default_spawn`
    (S6's real `spawner`) calls `Popen`, and patching it here globally would
    also intercept `test_restore_interactive_real_predicate_refuses_a_nul_stdin_and_exits_fast`'s
    own, unrelated, out-of-process `subprocess.Popen` call (a real, separate
    `python -m termsync` child it spawns on purpose to reproduce H1 end to
    end) -- a monkeypatch this broad cannot tell "restore.py's spawn" apart
    from "a test's own out-of-process harness". Every test in this file that
    can reach the RESTORE outcome instead injects its own `spawner=` fake
    directly into `restore_interactive` (SAFETY: never left at
    `restore.default_spawn`, the real one) -- a narrower, per-call-site
    guarantee instead of a module-wide one.
    """
    spy = MagicMock()
    monkeypatch.setattr(subprocess, "run", spy)
    return spy


def unused_paths(tmp_path: Path) -> tuple[Path, Path]:
    """`history_path`/`claude_dir` that do not exist on this machine.

    Real fixture data lives in specific tests that need it; every other test
    just needs to prove `dry_run` never reaches for `~/.claude` directly.
    `claude_dir` (review finding F5/M2) is `~/.claude` itself, matching
    `rows.make_glob_session_file`'s own parameter -- NOT the `projects`
    subdirectory the old, now-deleted `_session_file_lookup` took.
    """
    return tmp_path / "no-such-history.jsonl", tmp_path / "no-such-claude-dir"


def make_row(
    *,
    session_id: str = "s1",
    title: str = "Some title",
    cwd: str = r"C:\fixture\demo_project",
    prompt_count: int | None = 5,
    # Review finding F9/L5: this used to default to a fixed epoch far in the
    # FUTURE relative to every test's fixed `clock` (2000, 3000, ...), so
    # `now - last_prompt_at` was always negative and every row in this file
    # rendered "just now" by accident -- the age formatter was never
    # exercised on a real, positive value by any test using this default.
    # `None` ("age unknown") is the honest default for a helper whose job is
    # NOT to test age rendering; tests that care about age pass their own.
    last_prompt_at: int | None = None,
    session_file: Path | None = Path("fake.jsonl"),
    cwd_exists: bool = True,
    armed: bool = False,
    live: bool = False,
    young: bool = False,
    selectable: bool = True,
) -> Row:
    return Row(
        session_id=session_id,
        title=title,
        cwd=cwd,
        prompt_count=prompt_count,
        last_prompt_at=last_prompt_at,
        session_file=session_file,
        cwd_exists=cwd_exists,
        armed=armed,
        live=live,
        young=young,
        selectable=selectable,
    )


def emit_of(*, boot_time: int | None = 100, entries: tuple[dict[str, Any], ...] = ()) -> Emit:
    return Emit(sessions=entries, claude_process_count=len(entries), boot_time=boot_time)


def registry_entry(session_id: str, kind: str = "interactive") -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "alive": True,
        "procStart": "PS",
        "procStartLive": "PS",
        "kind": kind,
        "titleRaw": "\u2733 live tab",
        "titleAttachOk": True,
        "cwd": "c",
        "name": "n",
    }


def line_for(out: str, needle: str) -> str:
    """The one output line containing `needle`, or fail with the whole output shown."""
    matches = [line for line in out.splitlines() if needle in line]
    assert matches, f"no output line contained {needle!r}; full output:\n{out}"
    assert len(matches) == 1, f"expected exactly one line with {needle!r}, got: {matches}"
    return matches[0]


def scripted_keys(keys: list[str]) -> Callable[[], str]:
    """A `key_reader` for `restore_interactive`/`picker.run_picker`: returns
    each key in order, then raises if asked for one more. SAFETY: this is
    what makes it safe to drive the picker's real keyboard loop from a
    test at all -- a scripted sequence that runs out fails LOUDLY with an
    `AssertionError` instead of ever falling through to a blocking real
    read (`picker.read_key_msvcrt`), which this file must never call.
    """
    it = iter(keys)

    def read() -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("picker asked for a key beyond the scripted sequence") from None

    return read


# --- prints session id, title and cwd; subprocess never invoked ---------


def test_dry_run_prints_session_id_title_and_cwd(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    row = make_row(
        session_id="a0000001-full-uuid-should-never-be-truncated",
        title="Project plan phase 3 deep review",
        cwd=r"C:\fixture\demo_project",
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(row,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    code = dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert code == 0
    row_line = line_for(out, "a0000001-full-uuid-should-never-be-truncated")
    assert "Project plan phase 3 deep review" in row_line
    assert r"C:\fixture\demo_project" in row_line
    no_subprocess.assert_not_called()


def test_dry_run_never_invokes_subprocess_even_with_real_rows_flowing_through(
    store: Store, tmp_path: Path, no_subprocess: MagicMock
) -> None:
    """A second, dedicated assertion of the §13.4 requirement, with a fuller
    desk (multiple rows, a young bucket) flowing all the way through
    rendering -- not just the single-row smoke case above."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(
            desk=(make_row(session_id="a"), make_row(session_id="b", armed=True)),
            young=(make_row(session_id="c", young=True),),
        )

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    no_subprocess.assert_not_called()


# --- unselectable rows are marked and never presented as restorable -----


def test_unselectable_row_is_marked_with_its_reason_and_not_shown_as_restorable(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    gone = make_row(
        session_id="gone-id",
        title="Demo page rebuild",
        session_file=None,  # §7 step 4: the .jsonl was deleted
        selectable=False,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(gone,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "gone-id")
    assert "session file deleted" in row_line
    assert not row_line.strip().startswith("[x]"), "an unselectable row must never look restorable"
    assert not row_line.strip().startswith("~")


def test_unselectable_row_with_missing_cwd_reports_that_reason_specifically(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§7 step 5's distinct guard: the session file exists, but its cwd is gone."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    gone = make_row(
        session_id="cwd-gone-id",
        session_file=Path("still-here.jsonl"),
        cwd_exists=False,
        selectable=False,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(gone,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "cwd-gone-id")
    assert "cwd gone" in row_line
    assert "session file deleted" not in row_line


def test_unselectable_row_with_an_empty_cwd_says_cwd_unknown_not_cwd_gone(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding F10/L2 [LOW]: an empty/never-known cwd ("cwd unknown")
    and a real, deleted path ("cwd gone") are different facts -- on a
    crash-recovery screen the difference is "I don't know where this ran"
    versus "your work is lost". The old code had exactly one reason string
    for both. An empty cwd must also print no `()` at all, not the
    misleading `()`.
    """
    unknown = make_row(
        session_id="no-cwd-id",
        cwd="",
        session_file=Path("still-here.jsonl"),
        cwd_exists=False,
        selectable=False,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(unknown,), young=())

    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),), captured_at=1000, boot_id=100
    )
    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "no-cwd-id")
    assert "cwd unknown" in row_line
    assert "cwd gone" not in row_line
    assert "()" not in row_line, "an empty cwd must print no parens at all"


def test_armed_row_is_marked_with_a_tilde_not_x(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    armed_row = make_row(session_id="armed-id", armed=True)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(armed_row,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "armed-id")
    assert row_line.strip().startswith("~")
    assert not row_line.strip().startswith("[x]")


def test_live_row_is_marked_with_equals_and_says_live_not_dropped_from_output(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding F1/H1, D10: a session already live must STAY in the
    output, marked `[=]` and "live", not be silently removed the way an
    armed-but-never-resumed session used to be."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    live_row = make_row(session_id="live-id", live=True, selectable=False, armed=False)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(live_row,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "live-id")
    assert row_line.strip().startswith("[=]")
    assert "live" in row_line.lower()
    assert not row_line.strip().startswith("[x]")
    assert not row_line.strip().startswith("~")
    assert not row_line.strip().startswith("[ ]")


# --- --newest selects the newest layout, not the largest ----------------


def test_newest_flag_selects_the_newest_layout_rather_than_the_largest(
    store: Store, tmp_path: Path
) -> None:
    """§7 step 1: default target is the largest layout across the recent boots;
    `--newest` overrides that with the newest layout outright. Same boot for
    both writes, so `selection.select_target` has both in its candidate window
    and the choice really is the flag, not boot-window exclusion."""
    big = store.write_capture(
        sessions=tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(5)),
        captured_at=1000,
        boot_id=100,
    ).layout
    small = store.write_capture(
        sessions=(Session(session_id="only", cwd="c", title="t"),),
        captured_at=2000,
        boot_id=100,
    ).layout
    assert big.version != small.version

    seen: dict[str, Any] = {}

    def fake_builder(layout: Any, *_rest: Any, **_kwargs: Any) -> Rows:
        seen["layout"] = layout
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)

    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )
    assert seen["layout"].version == big.version, "default pick is the largest, not the newest"

    dry_run(
        store,
        newest=True,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )
    assert seen["layout"].version == small.version, "--newest must force the newest layout"


# --- freshness renders from lastSuccessAt, never a layout's capturedAt --


def test_freshness_header_uses_last_success_at_not_the_layouts_captured_at(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6: dedup means a healthy tool can carry a stale layout `capturedAt` for
    days while `lastSuccessAt` stays current. Deliberately constructed so the
    two disagree by a wide margin -- if `dry_run` were "simplified" to read
    the layout's `capturedAt` instead, this test fails."""
    now = 1_701_000_000_000
    captured_at = now - 3 * 86_400_000  # the layout: 3 days old
    last_success_at = now - 5 * 60_000  # status.json: 5 minutes ago (healthy)

    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=captured_at,
        boot_id=100,
    )
    store.record_run(ran_at=last_success_at, outcome="ok", sessions_seen=1, wrote_version=None)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: now,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    # Same formatter the CLI passes: the freshness line is localised at the
    # display layer (§6), so a UTC-rendered expectation would never match.
    # What this test pins is unchanged -- WHICH timestamp is used, not how it
    # is formatted.
    fmt = cli._format_local
    correct_line = health.freshness_report(last_success_at, now, format_timestamp=fmt)
    wrong_line = health.freshness_report(captured_at, now, format_timestamp=fmt)
    assert correct_line in out
    assert wrong_line not in out
    assert correct_line != wrong_line  # sanity: the two really do differ


# --- shrink guard --------------------------------------------------------


def test_shrink_guard_fires_only_on_a_strict_subset_from_the_same_boot(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rewritten per review finding F4 [MEDIUM] -- this is the test that
    authorisation covers, alongside the shrink-guard IMPLEMENTATION fix in
    `cli.dry_run`.

    **What this test used to assert, and why that was wrong.** The original
    fixture built the newest layout from `{only}` and the shown ("largest
    since boot") layout from `{s0..s4}` -- two DISJOINT session sets, not
    one a strict subset of the other -- and then asserted the `!`
    shrink-guard line appeared. §8's condition is a STRICT SUBSET of an
    earlier layout FROM THE SAME BOOT ("A newer snapshot holds N session(s)
    -- a strict subset of this one, same boot"); a disjoint set is not that,
    and this fixture could not tell the difference, so it pinned the
    implementation's actual defect (firing on ANY non-newest pick) as
    correct behaviour -- the same shape the phase 1-4 review's
    `test_target_ignores_a_larger_layout_from_a_previous_boot` and the S3
    review's `test_null_boot_time_writes_an_error_status_and_no_layout` were
    both caught doing; this is the THIRD occurrence in this project's
    history. Rewritten to construct a genuine strict subset: the newest
    layout (`{s0..s3}`, 4 sessions) is exactly the shown layout
    (`{s0..s4}`, 5 sessions) minus one, same `boot_id` -- so "showing the
    larger" is now a claim the code has actually verified, not merely
    asserted.
    """
    store.write_capture(
        sessions=tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(5)),
        captured_at=1000,
        boot_id=100,
    )
    store.write_capture(
        sessions=tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(4)),
        captured_at=2000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "!" in out
    assert "newer" in out.lower()
    assert "--newest" in out
    assert "strict subset" in out.lower()
    assert "same boot" in out.lower()
    assert "5 session" in out  # the SHOWN (larger) layout's real size
    assert "4 session(s) -- a strict subset" in out  # the newest (smaller) layout's real size


def test_shrink_guard_does_not_claim_strict_subset_when_the_newest_is_merely_disjoint(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding F4/F9 missing case 9: the disjoint-session-sets state
    the OLD version of the test above constructed by accident. It is a
    real, reachable state (an ordinary non-newest pick), but §8's alarm
    (`!`, "a strict subset ... same boot") must not fire for it -- there is
    no true "larger desk" relationship to report, so the guard must not
    claim one it has not checked. This is the regression test for M5's
    demonstrated false claim: the review's P11b showed the OLD condition
    printing "showing the larger" about a desk that was strictly SMALLER
    than the one it called smaller.
    """
    store.write_capture(
        sessions=tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(5)),
        captured_at=1000,
        boot_id=100,
    )
    store.write_capture(
        sessions=(Session(session_id="only", cwd="c", title="t"),),
        captured_at=2000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "!" not in out, "a non-subset must never trigger the alarm marker"
    assert "strict subset" not in out.lower()
    assert "showing the larger" not in out.lower()
    # Still informational, since a newer snapshot genuinely exists:
    assert "newer" in out.lower()
    assert "--newest" in out


def test_shrink_guard_line_is_absent_when_the_target_is_the_newest(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write_capture(
        sessions=(Session(session_id="only", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "newer" not in out.lower()


def test_shrink_guard_does_not_cry_wolf_after_an_ordinary_partial_restore(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D20 (S6 review finding F1), the exact repro: capture 10 sessions,
    then restore only 6 of them (the ordinary case -- deselecting some of
    what was offered is the picker's whole purpose). Before D20's fix in
    `selection.select_target`, the 6-session `source: "restore"` record
    became `target.newest` on its own version number, and the shrink guard
    fired "a strict subset ... same boot" on the very next restore's
    header -- the exact cry-wolf failure R12 was supposed to have already
    closed. `store.write_restore` is unchanged (the record is still
    written); `select_target` is what now refuses to compare against it.
    """
    ten = tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(10))
    store.write_capture(sessions=ten, captured_at=1000, boot_id=100)
    store.write_restore(sessions=ten[:6], restored_at=2000, boot_id=100)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "!" not in out, "the restore record must never trigger the shrink-guard alarm"
    assert "strict subset" not in out.lower()
    assert "newer" not in out.lower(), "the restore record must not even be offered as [n]"
    assert "10 session" in out  # the capture is still the shown desk


# --- empty store: useful, not a crash, and touches nothing live ---------


def test_empty_store_prints_something_useful_and_does_not_raise(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    def must_not_run() -> Emit:
        raise AssertionError("emit_runner must not be called when the store is empty")

    def must_not_build(*_args: Any, **_kwargs: Any) -> Rows:
        raise AssertionError("row_builder must not be called when the store is empty")

    code = dry_run(
        store,
        emit_runner=must_not_run,
        clock=lambda: 1000,
        row_builder=must_not_build,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert out.strip() != ""
    assert "no desk" in out.lower() or "termsync snap" in out.lower()


# --- young bucket ---------------------------------------------------------


def test_young_bucket_is_rendered_under_its_own_heading_after_the_desk_rows(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    desk_row = make_row(session_id="desk-id")
    young_row = make_row(session_id="young-id", title="Four-week progress measurement", young=True)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(desk_row,), young=(young_row,))

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "STARTED SINCE THE LAST SNAPSHOT  (1 session)" in out
    assert out.index("desk-id") < out.index("STARTED SINCE THE LAST SNAPSHOT")
    assert out.index("STARTED SINCE THE LAST SNAPSHOT") < out.index("young-id")


def test_no_young_heading_when_the_young_bucket_is_empty(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(make_row(),), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "STARTED SINCE THE LAST SNAPSHOT" not in out


# --- the empty-desk message (D10) ----------------------------------------


def test_empty_desk_message_when_the_target_layout_holds_no_sessions(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review mutation Q ("dry_run: never print the empty-desk message")
    survived all 230 tests -- several existing fixtures pass
    `Rows(desk=(), young=())` without ever asserting what `dry_run` prints
    for it. Also pins finding F1's fix to this message: under D10 the desk
    list is empty ONLY when the target layout itself holds zero sessions
    (an already-live session now stays in the list, marked `[=]`, instead
    of emptying it) -- so the old "all N captured session(s) are already
    open (N live)" branch, which asserted a relationship to `already_live`
    the code could no longer check, is gone; there is exactly one honest
    thing left to say.
    """
    store.write_capture(sessions=(), captured_at=1000, boot_id=100)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        # A live registry entry is deliberately present here: the fixed
        # message must not mention it or its count at all.
        emit_runner=lambda: emit_of(boot_time=100, entries=(registry_entry("live-1"),)),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    assert "Nothing to restore -- this desk holds no sessions." in out
    assert "already open" not in out.lower()
    assert "live)" not in out.lower()


# --- §7 step 2: the already-live set is registry UNION pending.json -----


def test_already_live_set_handed_to_the_row_builder_is_registry_union_pending(
    store: Store, tmp_path: Path
) -> None:
    """§7 step 2: "a registry-only check would let a second restore duplicate
    every armed tab." Proves `dry_run` actually computes the union, not just
    the registry half -- a session known only through `pending.json` must
    still land in the set the row builder receives."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    store.write_pending(
        Pending(
            written_at=500,
            entries=(PendingEntry(session_id="armed-1", title="t", cwd="c", armed_at=500),),
        )
    )

    seen: dict[str, Any] = {}

    def fake_builder(_layout: Any, _history: Any, live_ids: frozenset[str], *_rest: Any) -> Rows:
        seen["live_ids"] = live_ids
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100, entries=(registry_entry("live-1"),)),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    assert seen["live_ids"] == frozenset({("claude", "live-1"), ("claude", "armed-1")})


# --- _cwd_exists: an empty or relative cwd never "exists" (F3) -----------


def test_cwd_exists_rejects_empty_and_relative_paths_but_accepts_a_real_absolute_one(
    tmp_path: Path,
) -> None:
    """Review finding F3/M1 [MEDIUM]: `Path("")` normalises to `WindowsPath('.')`,
    which `is_dir()` reports `True` for -- termsync's own working directory,
    not "no cwd was recorded". An empty layout `cwd`, or a whitespace-only
    one, or a relative one, must never pass this guard; a real absolute
    directory must still pass it.
    """
    assert cli._cwd_exists("") is False
    assert cli._cwd_exists("   ") is False
    assert cli._cwd_exists(".") is False
    assert cli._cwd_exists("some\\relative\\path") is False
    assert cli._cwd_exists(str(tmp_path)) is True
    assert cli._cwd_exists(str(tmp_path / "does-not-exist")) is False


# --- rendered row text: prompt count and age (F9 mutations E, F) ---------


def test_render_row_shows_the_real_prompt_count_and_a_real_positive_age() -> None:
    """Review mutation E ("_render_row: DROP the age entirely") and mutation
    F ("_render_row: DROP the prompt count entirely") both survived all 230
    tests: `test_dry_run_prints_session_id_title_and_cwd` is the only test
    that asserts anything about a rendered row's text, and it only checks
    session id, title and cwd -- never prompt count or age, which is §13.3's
    S10 scope in full ("print session id, title, cwd, prompt count, age").
    Also the first test in this file to feed the age formatter a REAL,
    POSITIVE value (see `make_row`'s docstring on its old, accidentally
    negative default)."""
    now = 2_000_000
    row = make_row(
        session_id="s1",
        prompt_count=14,
        last_prompt_at=now - 2 * 3_600_000,  # 2 real hours before `now`
    )
    line = cli._render_row(row, now)
    assert "14 prompts" in line
    assert "2 hours ago" in line


def test_dry_run_prints_prompt_count_and_age_end_to_end(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same claim as the test above, driven through `dry_run` itself."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    now = 2_000_000
    row = make_row(session_id="s1", prompt_count=3, last_prompt_at=now - 5 * 60_000)

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(row,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    dry_run(
        store,
        emit_runner=lambda: emit_of(),
        clock=lambda: now,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr().out
    row_line = line_for(out, "s1")
    assert "3 prompts" in row_line
    assert "5 minutes ago" in row_line


# --- relative_age_phrase: the "just now ago" bug, and a negative age -----


def test_relative_age_phrase_never_says_just_now_ago() -> None:
    """Review mutation G ("relative_age_phrase: reintroduce the 'just now
    ago' bug") survived all 230 tests: the commit message names this bug --
    both age call sites once said "just now ago" -- as fixed by giving the
    phrasing ONE owner, `health.relative_age_phrase`, but no test anywhere
    exercised that function directly (`test_health.py` covers `relative_age`
    and `freshness_report`, not this one). Covers all four buckets used
    across the picker.
    """
    assert health.relative_age_phrase(0) == "just now"
    assert "ago" not in health.relative_age_phrase(0)
    assert health.relative_age_phrase(30_000) == "just now"
    assert health.relative_age_phrase(5 * 60_000) == "5 minutes ago"
    assert health.relative_age_phrase(3 * 3_600_000) == "3 hours ago"
    assert health.relative_age_phrase(2 * 86_400_000) == "2 days ago"


def test_relative_age_of_a_negative_duration_is_honestly_unknown_not_just_now() -> None:
    """Review finding F10/L5 [LOW]: a negative age -- a backwards clock
    correction, D1's own named failure class -- used to render "just now" by
    accident, because `age_ms // 60_000 < 1` is also true for every negative
    value. "just now" means "active seconds ago"; a clock anomaly is not
    that fact, so it renders honestly instead, and "ago" is never appended
    to it either."""
    assert health.relative_age(-5_000) == "age unknown"
    assert health.relative_age_phrase(-5_000) == "age unknown"
    assert "ago" not in health.relative_age_phrase(-5_000)


# --- bg exclusion wiring (F6, mutations H and L) --------------------------


def test_bg_session_ids_matches_the_literal_kind_bg_value() -> None:
    """Review mutations H ("dry_run: pass an EMPTY bg set to the row
    builder") and L ("_bg_session_ids: match kind=='background', an upstream
    rename") both survived all 230 tests -- nothing pinned the literal
    string this function keys off, or that `dry_run` builds a non-empty set
    from it at all."""
    emit = emit_of(
        entries=(
            registry_entry("interactive-1", kind="interactive"),
            registry_entry("bg-1", kind="bg"),
            registry_entry("bg-2", kind="bg"),
        )
    )
    assert cli._bg_session_ids(emit) == frozenset({"bg-1", "bg-2"})


def test_dry_run_excludes_a_bg_session_from_the_young_bucket_through_the_real_join(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutation H, driven all the way through the REAL `rows.build_rows`
    (not a fake row_builder that could not see the bg set matter): a session
    `-Emit` reports as `kind: "bg"`, with a prompt after `capturedAt`, must
    NOT appear in "started since the last snapshot" (§7 step 3)."""
    store.write_capture(sessions=(), captured_at=1000, boot_id=100)

    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "sessionId": "bg-1",
                "display": "a background job's prompt",
                "timestamp": 2000,
                "project": str(tmp_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claude_dir = tmp_path / "claude-home"
    (claude_dir / "projects").mkdir(parents=True)

    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100, entries=(registry_entry("bg-1", kind="bg"),)),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "bg-1" not in out
    assert "STARTED SINCE THE LAST SNAPSHOT" not in out


# --- a torn status.json/pending.json's warning reaches stderr (F7) -------


def test_a_torn_status_json_reaches_stderr_and_dry_run_still_exits_zero(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review finding F7 [MEDIUM]: `_print_read_warnings` used to run BEFORE
    `read_status()`/`read_pending()`, so a torn `status.json`'s warning never
    reached the user and the freshness header silently said "never" instead
    of explaining why -- while `snap`, which prints its warnings AFTER every
    read, showed it correctly on an identical store. `dry_run` now uses a
    `finally` matching `snap`'s shape."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    store.status_path.write_text("{not json", encoding="utf-8")

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    out = capsys.readouterr()
    assert code == 0
    assert "status.json is unreadable" in out.err
    # The freshness header must not silently claim "never" with no
    # explanation reaching the user anywhere in the same run.
    assert "Last successful snapshot: never" in out.out


def test_a_torn_pending_json_reaches_stderr(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of F7: `read_pending()` also runs after the old, early
    `_print_read_warnings` call site."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),),
        captured_at=1000,
        boot_id=100,
    )
    store.pending_path.write_text("{not json", encoding="utf-8")

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
    )

    err = capsys.readouterr().err
    assert code == 0
    assert "pending.json is unreadable" in err


# --- driven by the REAL rows.build_rows (F1/F8/M6 integration) -----------


def test_dry_run_through_the_real_build_rows_shows_an_armed_session_with_a_tilde(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The integration test the review's finding H1 and M6/F8 both call for:
    `dry_run` driven by the REAL `rows.build_rows` (no `row_builder=`
    override), with sessions armed via a real `pending.json`, against
    fixture `history.jsonl` and `~/.claude/projects` trees. This is the
    exact scenario the review demonstrated live: two armed tabs, ZERO
    `claude.exe` processes anywhere -- before this round, `--dry-run`
    printed neither session id and claimed "already open". It also proves
    `RowBuilder`'s alias arity actually matches what `dry_run` calls with
    (seven positional arguments): before finding F8's fix, a builder written
    to the published `RowBuilder` type raised `TypeError`, and nothing here
    would have caught it because this is the first test in the file that
    does not inject a fake `*_args, **_kwargs` builder.
    """
    store.write_pending(
        Pending(
            written_at=500,
            entries=(
                PendingEntry(
                    session_id="armed-1", title="Armed one", cwd=str(tmp_path), armed_at=500
                ),
                PendingEntry(
                    session_id="armed-2", title="Armed two", cwd=str(tmp_path), armed_at=500
                ),
            ),
        )
    )
    # Mirrors what capture.py step 9 would have written into the layout:
    # the pending entries carried through with armed=True, alongside an
    # ordinary, non-live, non-armed session.
    store.write_capture(
        sessions=(
            Session(session_id="s1", cwd=str(tmp_path), title="Ordinary session"),
            Session(session_id="armed-1", cwd=str(tmp_path), title="Armed one", armed=True),
            Session(session_id="armed-2", cwd=str(tmp_path), title="Armed two", armed=True),
        ),
        captured_at=1500,
        boot_id=100,
    )

    history_path = tmp_path / "history.jsonl"
    history_path.write_text("", encoding="utf-8")
    claude_dir = tmp_path / "claude-home"
    project_dir = claude_dir / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    for session_id in ("s1", "armed-1", "armed-2"):
        (project_dir / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")

    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),  # NOTHING live -- zero claude.exe
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        # row_builder NOT overridden -- exercises the real rows.build_rows.
    )

    out = capsys.readouterr().out
    assert code == 0
    for session_id in ("armed-1", "armed-2"):
        row_line = line_for(out, session_id)
        assert row_line.strip().startswith("~"), (
            f"an armed-not-resumed session ({session_id}) must render with ~, not vanish"
        )
    assert "already open" not in out.lower()


def test_dry_run_uses_a_real_ai_title_end_to_end(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review mutation I ("dry_run: stop passing ai_titles") survived all
    230 tests: the commit message names "the CLI never passed ai_titles" as
    an integration defect it found and fixed, but nothing pinned it, so
    dropping the wiring again is invisible (the fallback title is a
    plausible-looking string, not an obviously-wrong one). Driven through
    the REAL `row_builder` and a real `ai-title` record on disk.
    """
    store.write_capture(sessions=(), captured_at=1000, boot_id=100)

    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "sessionId": "young-1",
                "display": "raw prompt text nobody should see",
                "timestamp": 2000,
                "project": str(tmp_path),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claude_dir = tmp_path / "claude-home"
    project_dir = claude_dir / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    (project_dir / "young-1.jsonl").write_text(
        json.dumps({"type": "ai-title", "aiTitle": "AI-generated title", "sessionId": "young-1"})
        + "\n",
        encoding="utf-8",
    )

    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
    )

    out = capsys.readouterr().out
    assert code == 0
    row_line = line_for(out, "young-1")
    assert "AI-generated title" in row_line
    assert "raw prompt text" not in row_line


# --- --help is honest about what actually works --------------------------


def test_bare_restore_now_parses_now_that_s5s_picker_exists() -> None:
    """Superseded by S5, not a loosened assertion of a real defect: this
    test used to assert that `restore` with no flags was refused by
    argparse, because `--dry-run` was `required=True` while the picker
    (S5, `picker.py`) did not exist and a bare `restore` had nothing honest
    to do. S5's brief explicitly calls for changing this ("Make the bare
    `termsync restore` launch the picker"), and `cli.main`'s dispatch now
    routes a bare `restore` to `restore_interactive` instead of erroring.
    The old test therefore pinned the exact behaviour this unit exists to
    change; keeping it green would mean not shipping S5 at all."""
    parser = build_parser()
    args = parser.parse_args(["restore"])
    assert args.command == "restore"
    assert args.dry_run is False


def test_restore_help_no_longer_claims_interactive_restore_does_not_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli.py's own house rule (module docstring): a command's `--help`
    text must never lie about what it does. Before S5, the restore
    subparser's description said interactive restore (arming tabs) "does
    not exist" and `--dry-run` was "REQUIRED for now" -- both false once
    S5's picker is wired to the bare command."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "--help"])
    help_text = capsys.readouterr().out
    assert "does not exist" not in help_text
    assert "required for now" not in help_text.lower()
    assert "picker" in help_text.lower()


def test_restore_dry_run_parses_with_newest_and_store_flags() -> None:
    parser = build_parser()
    args = parser.parse_args(["restore", "--dry-run", "--newest", "--store", "C:\\scratch"])
    assert args.command == "restore"
    assert args.dry_run is True
    assert args.newest is True
    assert str(args.store) == "C:\\scratch"


def test_restore_dry_run_and_run_together_is_an_argparse_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F6: `restore --dry-run --run` used to be silently accepted and
    `--run` simply never read -- `dry_run`'s branch in `main()` returns
    before `args.run` is ever touched. Now a mutually exclusive group
    makes it an argparse error instead of a flag that looks honoured and
    is not."""
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["restore", "--dry-run", "--run"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--run" in err


# --- `restore_interactive` (bare `termsync restore`, S5) -----------------


def test_restore_interactive_refuses_to_run_when_stdin_is_not_a_tty(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    """SAFETY (this unit's brief): a non-interactive stdin must never reach
    the blocking keyboard loop -- that would hang forever with nothing able
    to interrupt it. Every dependency the loop would need is set to raise
    if called at all, so this test fails loudly rather than hanging if the
    guard is ever removed."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),), captured_at=1000, boot_id=100
    )

    def must_not_run_emit() -> Emit:
        raise AssertionError("emit_runner must not be called when stdin is not a TTY")

    def must_not_read_a_key() -> str:
        raise AssertionError("the keyboard loop must never start when stdin is not a TTY")

    code = restore_interactive(
        store,
        emit_runner=must_not_run_emit,
        clock=lambda: 2000,
        key_reader=must_not_read_a_key,
        is_tty=lambda: False,
    )

    err = capsys.readouterr().err
    assert code == 1
    assert "--dry-run" in err
    no_subprocess.assert_not_called()


# --- H1/F1: the REAL non-TTY predicate, not just the injected seam --------


def test_console_input_available_never_raises_and_reports_the_measured_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review finding H1/F1 [HIGH]: the old guard's own test
    (`test_restore_interactive_refuses_to_run_when_stdin_is_not_a_tty`,
    above) injects `is_tty=lambda: False` and can never exercise the real
    predicate, since it is only constructed when `is_tty is None` -- the
    review's own words: "nothing in the suite calls `restore_interactive`
    without injecting `is_tty`, so the predicate has zero coverage."

    This test drives `cli._console_input_available` DIRECTLY. On a normal
    Windows test runner it must return `False` here (no console attached to
    the interpreter running pytest -- confirmed independently: this
    project's own review methodology also found `GetConsoleWindow() == 0`
    for every one of its own redirected probe runs). It must also survive
    `ctypes` itself failing (the `pythonw`/non-Windows/no-handle shape,
    review finding L2) WITHOUT raising -- simulated here by making the
    import raise, since this test file must also stay collectible on a
    non-Windows CI runner.
    """
    assert cli._console_input_available() is False

    # L2: must never raise, even if the underlying Windows call itself blows up.
    import builtins

    real_import = builtins.__import__

    def raising_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "ctypes":
            raise OSError("simulated: no ctypes/windll available here")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", raising_import)
    assert cli._console_input_available() is False  # refuses cleanly; does not raise


def test_restore_interactive_real_predicate_refuses_a_nul_stdin_and_exits_fast(
    tmp_path: Path,
) -> None:
    """Review finding H1/F1 [HIGH]'s own recommended missing test:
    `restore_interactive` driven with the REAL default `is_tty` predicate
    (`cli._console_input_available`), stdin genuinely redirected from the
    NUL device (`subprocess.DEVNULL` on Windows), in a SEPARATE subprocess
    under a hard timeout -- "a hanging test is a failing test, which is the
    correct outcome here" (the review's own words). This is the exact
    reproduction that hung the review's own shell before this fix (`termsync
    restore < NUL`, `cmd.exe`, killed at 30s by an external timeout,
    exit 124) -- here it must exit 1 in well under a second, no store ever
    needing to exist (the guard fires before any store read).

    Uses `subprocess.Popen` directly rather than `subprocess.run` --
    `no_subprocess` (this file's autouse fixture) patches `subprocess.run`
    for every OTHER test in this module (asserting `dry_run`/
    `restore_interactive` never shell out); it does not patch `Popen`, and
    in any case this spawns a completely separate `python -m termsync`
    PROCESS to reproduce H1 end to end, not a call this project's own code
    makes internally.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "termsync", "restore", "--store", str(tmp_path / "store")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate(timeout=5)
        pytest.fail("restore_interactive < NUL hung -- the H1 guard did not fire")

    assert proc.returncode == 1
    assert "--dry-run" in err


def test_restore_interactive_quit_prints_nothing_restored_and_touches_no_subprocess(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),), captured_at=1000, boot_id=100
    )
    row = make_row(session_id="row-1", title="Some session")

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(row,), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        key_reader=scripted_keys(["q"]),
        is_tty=lambda: True,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Quit -- nothing restored." in out
    assert "row-1" not in out, "quitting must never print what would have been restored"
    no_subprocess.assert_not_called()


def test_restore_interactive_enter_prints_full_ids_of_only_the_still_selected_rows(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    """Drives the REAL `picker` module (no fake picker; only `row_builder`
    is faked): toggles the first (pre-selected) row OFF, moves down to the
    second, and presses Enter -- so only the second row's full session id
    must reach the "would restore" output, never the deselected one's."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd="c", title="t"),), captured_at=1000, boot_id=100
    )
    deselect_me = make_row(session_id="deselect-me-a0000001", title="Deselect this one")
    keep_me = make_row(session_id="keep-me-c0000003", title="Keep this one")

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(deselect_me, keep_me), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        key_reader=scripted_keys([" ", "j", "ENTER"]),
        is_tty=lambda: True,
        # S6's own preflight seams -- fakes, so this test never needs a real
        # `~/.claude`-shaped fixture tree; SAFETY: `spawner`/`sleep` must
        # NEVER be left at their real `restore_interactive` defaults here.
        session_file=lambda _session_id: Path("fake.jsonl"),
        cwd_exists=lambda _cwd: True,
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "deselect-me-a0000001" not in out
    row_line = line_for(out, "keep-me-c0000003")
    assert "Keep this one" in row_line
    assert "Armed 1 session(s):" in out
    no_subprocess.assert_not_called()
    spawner.assert_called_once()
    assert spawner.call_args[0][0][0] == "wt.exe"


def test_restore_interactive_n_rebuilds_rows_against_the_newest_layout(
    store: Store, tmp_path: Path, no_subprocess: MagicMock
) -> None:
    """§8's `[n]` affordance / SWITCH_LAYOUT: pressing `n` must re-enter the
    picker against `target.newest`, not merely redraw the same rows --
    mirrors `test_newest_flag_selects_the_newest_layout_rather_than_the_largest`'s
    approach for `--dry-run`, but through the interactive loop this time."""
    big = store.write_capture(
        sessions=tuple(Session(session_id=f"s{i}", cwd="c", title=f"t{i}") for i in range(5)),
        captured_at=1000,
        boot_id=100,
    ).layout
    small = store.write_capture(
        sessions=(Session(session_id="only", cwd="c", title="t"),),
        captured_at=2000,
        boot_id=100,
    ).layout
    assert big.version != small.version

    seen_versions: list[int] = []

    def fake_builder(layout: Any, *_rest: Any, **_kwargs: Any) -> Rows:
        seen_versions.append(layout.version)
        return Rows(desk=(), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        key_reader=scripted_keys(["n", "q"]),
        is_tty=lambda: True,
    )

    assert code == 0
    assert seen_versions == [big.version, small.version], (
        "the first loop iteration must show the default (largest-since-boot) "
        "pick, and pressing 'n' must switch to the newest layout for the next one"
    )
    no_subprocess.assert_not_called()


def test_restore_interactive_never_pre_selects_an_armed_session_end_to_end(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    """The integration test the review's finding H2/F2 [HIGH] calls for
    directly (its own "missing cases", item 1): `restore_interactive`
    driven by the REAL `rows.build_rows` (no `row_builder=` override), one
    session armed via a real `pending.json`, pressing Enter on the very
    first frame -- so whatever `initial_state` actually pre-selects is
    exactly what reaches the "Would restore" block. Demonstrated live by
    the review (P3): a real `Store`, a real capture, a real join, ZERO
    `claude.exe` processes anywhere, and the OLD code's Enter payload
    contained the armed id anyway. No other `restore_interactive` test in
    this file drives the real join at all -- every other one injects
    `row_builder=fake_builder`.
    """
    store.write_pending(
        Pending(
            written_at=500,
            entries=(
                PendingEntry(
                    session_id="armed-1", title="Armed one", cwd=str(tmp_path), armed_at=500
                ),
            ),
        )
    )
    store.write_capture(
        sessions=(
            Session(session_id="s1", cwd=str(tmp_path), title="Ordinary session"),
            Session(session_id="armed-1", cwd=str(tmp_path), title="Armed one", armed=True),
        ),
        captured_at=1000,
        boot_id=100,
    )

    history_path = tmp_path / "history.jsonl"
    history_path.write_text("", encoding="utf-8")
    claude_dir = tmp_path / "claude-home"
    project_dir = claude_dir / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    for session_id in ("s1", "armed-1"):
        (project_dir / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")

    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),  # zero live claude.exe processes
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        # row_builder NOT overridden -- exercises the real rows.build_rows.
        # session_file/cwd_exists NOT overridden either -- claude_dir/cwd
        # above are real, on-disk, so S6's own preflight (restore.preflight,
        # derived from THIS claude_dir) is CALLED for "s1" against the real
        # filesystem. S5 correction (test-quality defect, S6 review): this
        # comment used to say that call "exercises that seam for real
        # instead of faking it" -- true of being CALLED, but every row here
        # is built to PASS pre-flight, so a mutation replacing either
        # production default with a constant-True stub would leave this
        # test green too. Calling a seam is not testing it; the two tests
        # right after this one construct the real rejection this one
        # cannot -- the cwd/session-file vanishing between the picker frame
        # rendering and Enter's decision reaching `restore.preflight`.
        key_reader=scripted_keys(["ENTER"]),  # Enter on the very first frame
        is_tty=lambda: True,
        # SAFETY: never leave `spawner`/`sleep` at their real defaults.
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "Armed 1 session(s):" in out
    row_line = line_for(out, "s1")
    assert "Ordinary session" in row_line
    assert "armed-1" not in out, "an armed session must never reach the restore payload (D11/F2)"
    no_subprocess.assert_not_called()
    spawner.assert_called_once()


def test_restore_interactive_preflight_rejects_a_row_whose_cwd_vanishes_after_the_picker_renders(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    """S5: `restore_interactive`'s PRODUCTION `session_file`/`cwd_exists`
    defaults (derived from `claude_dir`/`_cwd_exists` when neither is
    overridden) must be able to REJECT a row, not merely be called -- the
    test above calls them, but every row in it is built to PASS, so a
    mutation replacing either default with a constant-True stub leaves it
    green. This reproduces §3.9/§7 step 2's actual re-check scenario: the
    cwd exists (so the row renders selectable) when the picker draws its
    first frame, then is deleted before Enter's decision reaches
    `restore.preflight` -- the `key_reader` fake deletes it as a side
    effect of returning "ENTER", simulating the picker sitting open while
    the world changes underneath it (exactly `preflight`'s own stated
    justification for re-checking at the spawn point instead of trusting
    the picker's render-time state).
    """
    import shutil

    live_cwd = tmp_path / "will-vanish"
    live_cwd.mkdir()
    store.write_capture(
        sessions=(Session(session_id="s1", cwd=str(live_cwd), title="Session"),),
        captured_at=1000,
        boot_id=100,
    )

    history_path = tmp_path / "history.jsonl"
    history_path.write_text("", encoding="utf-8")
    claude_dir = tmp_path / "claude-home"
    project_dir = claude_dir / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    (project_dir / "s1.jsonl").write_text("{}\n", encoding="utf-8")

    def vanishing_key_reader() -> str:
        shutil.rmtree(live_cwd)
        return "ENTER"

    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        # session_file/cwd_exists NOT overridden -- the real production
        # defaults, derived from claude_dir, are what this test exercises.
        key_reader=vanishing_key_reader,
        is_tty=lambda: True,
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    out = capsys.readouterr().out
    assert code == 1  # F5: a pre-flight skip means the run did not do what it was asked
    assert 'skipping "Session": cwd gone' in out
    no_subprocess.assert_not_called()
    spawner.assert_not_called()


def test_restore_interactive_preflight_rejects_a_row_whose_session_file_vanishes_after_render(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str], no_subprocess: MagicMock
) -> None:
    """S5, the `session_file` half of the same gap: the production
    glob-based default must also be able to REJECT, not merely pass. The
    session's `.jsonl` file exists when the picker renders (selectable),
    then is deleted before Enter's decision reaches `restore.preflight`."""
    stable_cwd = tmp_path / "stable"
    stable_cwd.mkdir()
    store.write_capture(
        sessions=(Session(session_id="s1", cwd=str(stable_cwd), title="Session"),),
        captured_at=1000,
        boot_id=100,
    )

    history_path = tmp_path / "history.jsonl"
    history_path.write_text("", encoding="utf-8")
    claude_dir = tmp_path / "claude-home"
    project_dir = claude_dir / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    session_file_path = project_dir / "s1.jsonl"
    session_file_path.write_text("{}\n", encoding="utf-8")

    def vanishing_key_reader() -> str:
        session_file_path.unlink()
        return "ENTER"

    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        key_reader=vanishing_key_reader,
        is_tty=lambda: True,
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    out = capsys.readouterr().out
    assert code == 1
    assert 'skipping "Session": session file deleted' in out
    no_subprocess.assert_not_called()
    spawner.assert_not_called()


def test_main_restore_run_flag_reaches_restore_interactive(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1: `main()`'s own `run=args.run` has zero coverage without this --
    a mutation replacing it with `run=False` stays green against every
    other test in this file, since none of them drive `cli.main()`'s own
    dispatch through to `restore_interactive` at all. `cli.restore_interactive`
    is monkeypatched here (never called for real -- SAFETY), just to
    capture what `main()` actually passed it."""
    captured: dict[str, bool] = {}

    def fake_restore_interactive(_store: Store, *, newest: bool = False, run: bool = False) -> int:
        captured["run"] = run
        captured["newest"] = newest
        return 0

    monkeypatch.setattr(cli, "restore_interactive", fake_restore_interactive)

    code = cli.main(["restore", "--run", "--store", "does-not-matter"])

    assert code == 0
    assert captured["run"] is True
    assert captured["newest"] is False


def test_restore_interactive_run_flag_reaches_restore_selected_and_appends_dash_run(
    store: Store, tmp_path: Path, no_subprocess: MagicMock
) -> None:
    """S1: the SECOND end of the same wire -- `restore_interactive`'s own
    `run` parameter must reach `restore.restore_selected` unchanged. A
    mutation replacing the cli seam's `run=run` with `run=False` stays
    green against every existing test in this file, since none of them
    pass `run=True` through the interactive picker path at all. Verified
    at the one place that can actually tell the difference: `build_wt_argv`
    only appends the trailing `-Run` element when `run` is `True` (D14).
    """
    store.write_capture(
        sessions=(Session(session_id="s1", cwd=str(tmp_path), title="Some title"),),
        captured_at=1000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(make_row(session_id="s1", title="Some title"),), young=())

    history_path, claude_dir = unused_paths(tmp_path)
    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        run=True,
        emit_runner=lambda: emit_of(
            boot_time=100, entries=(registry_entry("s1"),) if spawner.called else (),
        ),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        key_reader=scripted_keys(["ENTER"]),
        is_tty=lambda: True,
        session_file=lambda _session_id: Path("fake.jsonl"),
        cwd_exists=lambda _cwd: True,
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    assert code == 0
    spawner.assert_called_once()
    argv = spawner.call_args[0][0]
    assert "-Run" in argv
    no_subprocess.assert_not_called()


@pytest.mark.parametrize("mode", [
    "run_live", "run_missing", "run_error",
    "armed_waiting", "armed_resumed", "armed_error",
])
def test_restore_interactive_verification_exit_status(store, tmp_path, mode):
    """Verification failure reaches the CLI without retrying accepted launches."""
    store.write_capture(sessions=(Session("s1", "c", "Some title"),),
                        captured_at=1000, boot_id=100)
    history_path, claude_dir = unused_paths(tmp_path)
    spawner = MagicMock(return_value=MagicMock(poll=lambda: 0))
    out = StringIO()

    def observe_sessions():
        if not spawner.called:
            return emit_of()
        if mode.endswith("error"):
            raise RuntimeError("verification unavailable")
        observed = emit_of(entries=(registry_entry("s1"),)
                           if mode in ("run_live", "armed_resumed") else ())
        if mode == "armed_resumed":
            # Capture may reconcile the entry while restore waits to verify.
            assert store.read_pending().session_ids == {"s1"}
            assert cli.snap(store, emit_runner=lambda: observed, clock=lambda: 2100) == 0
        return observed

    code = restore_interactive(
        store, run=mode.startswith("run_"), emit_runner=observe_sessions,
        clock=lambda: 2000, history_path=history_path, claude_dir=claude_dir,
        row_builder=lambda *_: Rows((make_row(session_id="s1"),), ()),
        key_reader=scripted_keys(["ENTER"]), is_tty=lambda: True,
        session_file=lambda _: Path("fake.jsonl"), cwd_exists=lambda _: True,
        spawner=spawner, sleep=lambda _: None, out=out,
    )

    assert code == (1 if mode in ("run_missing", "run_error", "armed_error") else 0)
    spawner.assert_called_once()
    assert any(layout.source == "restore" for layout in store.all_layouts())
    printed = out.getvalue()
    if mode == "run_missing":
        assert "restored 0/1" in printed
        assert "not confirmed live at verification" in printed
    elif mode.endswith("error"):
        assert "post-restore verification failed: RuntimeError: verification unavailable" in printed
    elif mode == "run_live":
        assert "restored 1/1" in printed
    if mode in ("armed_waiting", "armed_error"):
        assert store.read_pending().session_ids == {"s1"}
    else:
        assert store.read_pending() is None
    if mode == "armed_resumed":
        assert "pending.json holds 0/1" in printed


def test_restore_interactive_returns_1_when_a_spawn_fails(
    store: Store, tmp_path: Path, no_subprocess: MagicMock
) -> None:
    """F5: `cli.py` used to discard `RestoreOutcome` and unconditionally
    `return 0`, even when every spawn failed -- contradicting D7/§13.4's
    own convention ("1 means this run did not do what it was asked"), cited
    four paragraphs earlier in this function's own docstring. A spawner
    that raises for every row means zero tabs opened -- the clearest
    possible case of that convention."""
    store.write_capture(
        sessions=(Session(session_id="s1", cwd=str(tmp_path), title="Some title"),),
        captured_at=1000,
        boot_id=100,
    )

    def fake_builder(*_args: Any, **_kwargs: Any) -> Rows:
        return Rows(desk=(make_row(session_id="s1", title="Some title"),), young=())

    def failing_spawner(_argv: list[str]) -> None:
        raise OSError("wt.exe is not installed")

    history_path, claude_dir = unused_paths(tmp_path)
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 2000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        key_reader=scripted_keys(["ENTER"]),
        is_tty=lambda: True,
        session_file=lambda _session_id: Path("fake.jsonl"),
        cwd_exists=lambda _cwd: True,
        spawner=failing_spawner,
        sleep=lambda _seconds: None,
    )

    assert code == 1
    no_subprocess.assert_not_called()


def test_restore_interactive_n_toggles_and_carries_a_deselection_across_the_switch(
    store: Store, tmp_path: Path, no_subprocess: MagicMock
) -> None:
    """H3/F3 [HIGH], the full integration claim, all three parts the brief
    names explicitly:

    1. A deselection made BEFORE pressing `[n]` must survive into the new
       layout, for any row still present there.
    2. Pressing `[n]` a SECOND time must return to the ORIGINAL layout --
       not stay stuck on newest, and not no-op.
    3. The header must keep naming "the other desk" in BOTH directions,
       not just the direction §8's existing shrink-guard alarm already
       covers.

    `big` (3 sessions, older) is the default §7 step 1 pick; `small` (2 of
    the SAME 3 sessions, newer, same boot) is a strict subset of it -- the
    ordinary shrink-guard scenario §8 already alarms on when `effective IS
    big`. The genuinely new assertion is what the header says when
    `effective IS small`: before this fix, nothing on the screen named the
    larger desk once the user had looked at newest even once.
    """
    big = store.write_capture(
        sessions=(
            Session(session_id="s0", cwd="c", title="Session 0"),
            Session(session_id="s1", cwd="c", title="Session 1"),
            Session(session_id="s2", cwd="c", title="Session 2"),
        ),
        captured_at=1000,
        boot_id=100,
    ).layout
    small = store.write_capture(
        sessions=(
            Session(session_id="s0", cwd="c", title="Session 0"),
            Session(session_id="s1", cwd="c", title="Session 1"),
        ),
        captured_at=2000,
        boot_id=100,
    ).layout
    assert big.version != small.version

    def fake_builder(layout: Any, *_rest: Any, **_kwargs: Any) -> Rows:
        if layout.version == big.version:
            return Rows(
                desk=(
                    make_row(session_id="s0", title="Session 0"),
                    make_row(session_id="s1", title="Session 1"),
                    make_row(session_id="s2", title="Session 2"),
                ),
                young=(),
            )
        return Rows(
            desk=(
                make_row(session_id="s0", title="Session 0"),
                make_row(session_id="s1", title="Session 1"),
            ),
            young=(),
        )

    history_path, claude_dir = unused_paths(tmp_path)
    out = StringIO()
    spawner = MagicMock()
    spawner.return_value.poll.return_value = 0
    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        row_builder=fake_builder,
        # cursor starts on s0; DOWN -> s1; space deselects s1; n -> `small`
        # (s1 still there, must stay deselected); n again -> back to `big`
        # (s2 must NOT silently reappear selected -- `initial_state`'s own
        # docstring: nothing not in the carried set is ever re-added);
        # ENTER restores.
        key_reader=scripted_keys(["DOWN", " ", "n", "n", "ENTER"]),
        is_tty=lambda: True,
        out=out,
        # S6's own preflight seams -- fakes, so this test never needs a real
        # `~/.claude`-shaped fixture tree. SAFETY: `spawner`/`sleep` must
        # never be left at their real defaults.
        session_file=lambda _session_id: Path("fake.jsonl"),
        cwd_exists=lambda _cwd: True,
        spawner=spawner,
        sleep=lambda _seconds: None,
    )

    printed = out.getvalue()
    assert code == 0

    # (3) the header names "the other desk" in BOTH directions.
    assert "strict subset" in printed.lower()  # while on `big` -- existing shrink guard
    assert "also available" in printed  # NEW: while on `small`, reverse direction

    # (1) + (2): after the round trip (n, n) back to `big`, s1 -- deselected
    # before the first switch -- is STILL deselected, and s2 was never
    # silently re-selected either.
    would_restore = "\n".join(line for line in printed.splitlines()
                              if line.startswith("launch accepted:"))
    assert "Session 0" in would_restore
    assert "Session 1" not in would_restore
    assert "Session 2" not in would_restore
    no_subprocess.assert_not_called()


# --- L5's screen-order guarantee, now `restore._select_rows`'s job --------
#
# The old `cli._print_would_restore` this test drove is gone -- S6 replaced
# it with `restore.restore_selected` (see that module's own docstring). The
# ordering guarantee it carried (review finding L5: a UUID sort has nothing
# to do with what the user was just looking at) moved to
# `restore._select_rows`, and is covered there instead --
# `tests/test_restore_cmdline.py::test_select_rows_preserves_screen_order_not_session_id_order`.


# --- D20 regression: a store with no READABLE capture must not crash ------


def _store_with_only_a_restore_record(store: Store) -> None:
    """Capture v1 + restore v2, then truncate v1 -- a crash mid-write, which is
    the exact event this tool exists for (§6 rule 3). `all_layouts()` skips the
    unreadable v1 and returns only the `source: "restore"` v2.
    """
    sessions = [Session(session_id="s1", cwd=r"D:\w", title="T")]
    store.write_capture(sessions=sessions, captured_at=1000, boot_id=100, registry_versions=())
    store.write_restore(sessions=sessions, restored_at=2000, boot_id=100, registry_versions=())
    oldest = min((store.root / "layouts").glob("*.json"))
    oldest.write_text('{"version": 1, "sess', encoding="utf-8")


def test_dry_run_reports_instead_of_crashing_when_no_capture_is_readable(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D20 made `select_target` return None on a NON-empty layout list, which
    the `assert target is not None` at this call site declared impossible.
    Reproduced before the fix as a bare `AssertionError` out of `dry_run` (and,
    under `python -O` with asserts stripped, an `AttributeError` on `None`).
    §6 rule 3 requires the truncated-layout path to report and NOT throw.
    """
    _store_with_only_a_restore_record(store)
    history_path, claude_dir = unused_paths(tmp_path)

    code = dry_run(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "No readable captured desk" in out
    # The restore record must not be offered as a desk to restore from.
    assert "s1" not in out


def test_restore_interactive_reports_instead_of_crashing_when_no_capture_is_readable(
    store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same D20 regression at `restore_interactive`'s own call site. Driven
    with a `key_reader` that raises if consulted: reaching the picker at all
    would itself be the bug, since there is no desk to render.
    """
    _store_with_only_a_restore_record(store)
    history_path, claude_dir = unused_paths(tmp_path)

    def never_called() -> str:
        raise AssertionError("the picker must not run with no readable capture")

    code = restore_interactive(
        store,
        emit_runner=lambda: emit_of(boot_time=100),
        clock=lambda: 3000,
        history_path=history_path,
        claude_dir=claude_dir,
        key_reader=never_called,
        is_tty=lambda: True,
        spawner=MagicMock(return_value=MagicMock(poll=lambda: 0)),  # never real
        sleep=lambda _seconds: None,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "No readable captured desk" in out
