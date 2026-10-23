"""Behaviour tests for work unit S6 (`restore.py`, `snapshot/arm.ps1`):
§7 steps 2/7/8, §13.4's `test_restore_cmdline.py` entry, D13-D18.

**SAFETY, restated from this unit's brief.** No test in this file ever
calls `restore.default_spawn` (real `subprocess.Popen(["wt.exe", ...])`),
never runs `wt --help`, never leaves `sleep` at real `time.sleep`, and
never touches `picker.read_key_msvcrt` or a real console. Every spawn goes
through a recording fake `spawner`; every wait goes through a no-op
`sleep`. `restore.restore_selected` is exercised against a real `Store`
rooted at `tmp_path` (this project's own convention -- there is no fake
`Store` anywhere in this codebase) and fake `session_file`/`cwd_exists`/
`emit_runner` callables, so nothing here ever reaches `~/.claude`,
`~/.termsync`, or a live machine.
"""

from __future__ import annotations

import base64
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from termsync import restore
from termsync.emit import Emit
from termsync.model import Pending, PendingEntry
from termsync.rows import Row, Rows
from termsync.store import Store

pytestmark = pytest.mark.usefixtures("ready_restore_desktop")

# --- fixtures / helpers ------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


def make_row(
    *,
    session_id: str = "s1",
    title: str = "Some title",
    cwd: str = r"C:\fixture\demo_project",
    prompt_count: int | None = 5,
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


def emit_of(*, boot_time: int | None = 100, entries: tuple[dict, ...] = ()) -> Emit:
    return Emit(sessions=entries, claude_process_count=len(entries), boot_time=boot_time)


def registry_entry(session_id: str, version: str = "2.1.238") -> dict:
    return {
        "sessionId": session_id,
        "alive": True,
        "procStart": "PS",
        "procStartLive": "PS",
        "kind": "interactive",
        "version": version,
    }


class RecordingSpawner:
    """A `restore.Spawner` fake that records every argv it was called with.
    Never touches `subprocess` -- SAFETY."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> None:
        self.calls.append(argv)
        return SimpleNamespace(poll=lambda: 0)


def always_present(_session_id: str) -> Path | None:
    return Path("fake.jsonl")


def always_missing(_session_id: str) -> Path | None:
    return None


def always_exists(_cwd: str) -> bool:
    return True


def never_exists(_cwd: str) -> bool:
    return False


def no_sleep(_seconds: float) -> None:
    """Never blocks -- SAFETY: this is the ONLY `sleep` any test here passes."""


# --- `build_wt_argv` (§7 step 7, §13.4's whole bullet list) -------------


def test_worked_example_argv_matches_7_step_7s_shape() -> None:
    """A synthetic example preserves the documented argument shape."""
    row = make_row(
        session_id="10000000-0000-4000-8000-000000000001",
        title="Demo page rebuild",
        cwd=r"C:\fixture\demo_project",
        prompt_count=9,
        last_prompt_at=1_000,
    )
    arm_script = Path(r"C:\fixture\tool\snapshot\arm.ps1")

    argv = restore.build_wt_argv(row, arm_script, run=False, now=1_180_000,
        window_name="desk-test-1")

    assert argv == [
        "wt.exe",
        "-w",
        "desk-test-1",
        "new-tab",
        "-d",
        r"C:\fixture\demo_project",
        "--title",
        "Demo page rebuild",
        "--suppressApplicationTitle",
        "powershell.exe",
        "-NoExit",
        "-NoProfile",
        "-File",
        r"C:\fixture\tool\snapshot\arm.ps1",
        "-SessionId",
        "10000000-0000-4000-8000-000000000001",
        "-TitleB64",
        restore._title_b64("Demo page rebuild"),
        "-Prompts",
        "9 prompts",
        "-Age",
        "19 minutes ago",
    ]
    # D19: `--title`/`--suppressApplicationTitle` are `new-tab` COMMAND
    # parameters, not global `wt` options -- they must sit AFTER `new-tab`,
    # not merely be present somewhere in the argv (the defect this test
    # used to pin: both flags were present, but before `new-tab`, which
    # Microsoft's own docs and worked example make a real two-tab risk).
    assert argv.index("new-tab") < argv.index("--title")
    assert argv.index("new-tab") < argv.index("--suppressApplicationTitle")


def test_argv_0_is_wt_exe_never_powershell_wrapping_it() -> None:
    """R17/§7 step 7: `wt.exe` is invoked directly, never through `powershell.exe`
    -- a wrapped spawn blocks on the FIRST tab until the window closes."""
    argv = restore.build_wt_argv(make_row(), Path("arm.ps1"), run=False, now=0,
        window_name="desk-test-1")
    assert argv[0] == "wt.exe"


def test_argv_contains_no_semicolon_element_at_all() -> None:
    """§3.10: zero `;` elements in the wt.exe command line, ever."""
    row = make_row(title="a; new-tab; powershell -Command evil")
    argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=0, window_name="desk-test-1")
    assert all(";" not in element for element in argv)


def test_argv_round_trips_through_list2cmdline_and_windows_arg_parsing() -> None:
    """Windows argv round-trip only; this does NOT exercise WT's parser.

    The title-semicolon regression has a separate assertion and live probe.
    Semicolon-containing paths are rejected separately before launching.
    """
    import ctypes

    command_line_to_argv_w = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv_w.restype = ctypes.POINTER(ctypes.c_wchar_p)
    command_line_to_argv_w.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]

    hostile_titles = [
        "a; new-tab; powershell -Command evil",
        """it's a "test"; new-tab powershell""",
        r'D:\some; path\with "quotes"',
        "trailing backslash\\",
        "",
    ]
    for title in hostile_titles:
        row = make_row(title=title, cwd=r'D:\some path\with "quotes"\\')
        argv = restore.build_wt_argv(row, Path(r"D:\snapshot\arm.ps1"), run=False, now=0,
            window_name="desk-test-1")

        cmdline = subprocess.list2cmdline(argv)
        argc = ctypes.c_int()
        argv_ptr = command_line_to_argv_w(cmdline, ctypes.byref(argc))
        assert argv_ptr, f"CommandLineToArgvW failed to parse: {cmdline!r}"
        try:
            parsed = [argv_ptr[i] for i in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv_ptr)

        assert parsed == argv, f"round trip lost data for title={title!r}"


def test_a_hostile_title_arrives_as_exactly_one_argv_element_at_title_and_dash_titleb64() -> None:
    """Old stored titles are normalized identically for tab and armed text."""
    hostile = """it's a "test"; new-tab powershell"""
    row = make_row(title=hostile)
    argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=0, window_name="desk-test-1")

    title_index = argv.index("--title")
    assert argv[title_index + 1] == hostile.replace(";", "\uff1b")
    assert argv[title_index + 2] == "--suppressApplicationTitle"

    dash_titleb64_index = argv.index("-TitleB64")
    encoded = argv[dash_titleb64_index + 1]
    assert base64.b64decode(encoded).decode("utf-8") == hostile.replace(";", "\uff1b")
    # Exactly one element: the token right after -TitleB64's value is the next FLAG.
    assert argv[dash_titleb64_index + 2] in ("-Prompts", "-Age", "-Run")


def test_a_title_that_collides_with_a_powershell_parameter_name_survives_encoding() -> None:
    """D23/F3: the concrete collision set the review measured against the
    real, unmodified `arm.ps1` -- a bare `-Word` that prefix-matches one of
    its own declared parameter names broke plain string param binding
    (`arm.ps1 : Missing an argument for parameter 'Title'`). Base64's
    alphabet (A-Za-z0-9+/=) never produces a leading `-`, so none of these
    can ever be misread as a parameter name once encoded."""
    for hostile_title in ("-Run", "-R", "-Title", "-T", "-SessionId", "-Age", "-A", "-", "--"):
        row = make_row(title=hostile_title)
        argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=0,
            window_name="desk-test-1")
        encoded = argv[argv.index("-TitleB64") + 1]
        assert not encoded.startswith("-")
        assert base64.b64decode(encoded).decode("utf-8") == hostile_title


def test_suppress_application_title_is_present() -> None:
    argv = restore.build_wt_argv(make_row(), Path("arm.ps1"), run=False, now=0,
        window_name="desk-test-1")
    assert "--suppressApplicationTitle" in argv


def test_dash_d_receives_the_recorded_cwd() -> None:
    row = make_row(cwd=r"C:\fixture\demo")
    argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=0, window_name="desk-test-1")
    d_index = argv.index("-d")
    assert argv[d_index + 1] == r"C:\fixture\demo"


def test_run_and_armed_forms_differ_only_in_arm_ps1s_arguments() -> None:
    """D14: `--run` and the armed form must differ ONLY in what's passed to
    `arm.ps1` -- specifically, only the trailing `-Run` switch. Everything
    up to and including the `-d <cwd>` half (the `wt.exe`/`new-tab` level)
    must be byte-for-byte identical."""
    row = make_row()
    armed_argv = restore.build_wt_argv(row, Path("arm.ps1"), run=False, now=5_000,
        window_name="desk-test-1")
    run_argv = restore.build_wt_argv(row, Path("arm.ps1"), run=True, now=5_000,
        window_name="desk-test-1")

    assert run_argv == [*armed_argv, "-Run"]

    wt_level = armed_argv[: armed_argv.index("-d") + 2]
    assert run_argv[: len(wt_level)] == wt_level


def test_prompts_and_age_are_omitted_entirely_when_their_phrasing_is_empty() -> None:
    """The brief: "Omit -Prompts / -Age from the argv entirely when unknown
    rather than passing an empty string" -- an untested risk through wt's
    tokenizer. `prompt_and_age_text` never actually returns "" in
    production (its own contract: "no prompt history" / "age unknown" at
    worst), so this branch is exercised here via an injected fake."""
    argv = restore.build_wt_argv(
        make_row(),
        Path("arm.ps1"),
        run=False,
        now=0,
        prompt_and_age_text=lambda _row, _now: ("", ""),
     window_name="desk-test-1")
    assert "-Prompts" not in argv
    assert "-Age" not in argv
    assert "" not in argv  # never an empty argv element either
    assert "-SessionId" in argv and "-TitleB64" in argv  # the rest of the tail is unaffected


def test_prompts_and_age_are_present_when_their_phrasing_is_non_empty() -> None:
    """The mirror of the omission test -- the ordinary, always-true-in-production case."""
    argv = restore.build_wt_argv(
        make_row(),
        Path("arm.ps1"),
        run=False,
        now=0,
        prompt_and_age_text=lambda _row, _now: ("3 prompts", "2 hours ago"),
     window_name="desk-test-1")
    prompts_index = argv.index("-Prompts")
    assert argv[prompts_index + 1] == "3 prompts"
    age_index = argv.index("-Age")
    assert argv[age_index + 1] == "2 hours ago"


# --- `_select_rows` -- screen order, not id order (formerly L5's test) --


def test_select_rows_preserves_screen_order_not_session_id_order() -> None:
    """Review finding L5 [LOW], carried into S6's `_select_rows`: session
    ids are UUIDs, so a sort by id has nothing to do with what the user was
    just looking at. Desk order (then young) must be preserved."""
    row_z = make_row(session_id="zzz-last-alphabetically", title="First on screen")
    row_a = make_row(session_id="aaa-first-alphabetically", title="Second on screen")
    row_set = Rows(desk=(row_z, row_a), young=())

    selected = restore._select_rows(
        row_set, frozenset({"zzz-last-alphabetically", "aaa-first-alphabetically"})
    )

    assert [row.title for row in selected] == ["First on screen", "Second on screen"]


# --- `preflight` ---------------------------------------------------------


def test_preflight_skips_a_row_whose_session_file_is_gone() -> None:
    row = make_row(session_id="s1")
    ok, skipped = restore.preflight([row], always_missing, always_exists)
    assert ok == []
    assert skipped == [restore.Skip(row, "session file deleted")]


def test_preflight_skips_a_row_whose_cwd_is_gone() -> None:
    row = make_row(session_id="s1", cwd=r"D:\somewhere")
    ok, skipped = restore.preflight([row], always_present, never_exists)
    assert ok == []
    assert skipped == [restore.Skip(row, "cwd gone")]


def test_preflight_passes_a_row_whose_file_and_cwd_are_both_present() -> None:
    row = make_row(session_id="s1")
    ok, skipped = restore.preflight([row], always_present, always_exists)
    assert ok == [row]
    assert skipped == []


# --- `restore_selected` end to end ---------------------------------------


def test_subprocess_never_invoked_when_every_selected_row_fails_preflight(store: Store) -> None:
    row = make_row(session_id="dead")
    row_set = Rows(desk=(row,), young=())
    spawner = RecordingSpawner()

    outcome = restore.restore_selected(
        frozenset({"dead"}),
        row_set,
        store,
        emit_of(),
        run=False,
        session_file=always_missing,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=emit_of,
    )

    assert spawner.calls == []
    assert outcome.spawned == ()
    assert len(outcome.skipped) == 1
    assert store.read_pending() is None
    assert store.all_layouts() == []  # write_restore never called either


def test_a_preflight_skipped_session_does_not_appear_in_the_write_restore_record(
    store: Store,
) -> None:
    """D18: `write_restore` records what was SPAWNED, never merely selected --
    a pre-flight skip is the same class of gap R12 already closed for
    offered-vs-selected."""
    good = make_row(session_id="good", title="Good session")
    bad = make_row(session_id="bad", title="Bad session", cwd=r"D:\gone")
    row_set = Rows(desk=(good, bad), young=())
    spawner = RecordingSpawner()

    def session_file(session_id: str) -> Path | None:
        return Path("f.jsonl") if session_id == "good" else None

    restore.restore_selected(
        frozenset({"good", "bad"}),
        row_set,
        store,
        emit_of(boot_time=42),
        run=False,
        session_file=session_file,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 2_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=42),
    )

    [layout] = store.all_layouts()
    assert layout.source == "restore"
    assert {s.session_id for s in layout.sessions} == {"good"}
    assert len(spawner.calls) == 1


def test_an_explicitly_selected_armed_row_is_spawned_and_its_armed_at_refreshed(
    store: Store,
) -> None:
    """D13: an armed row the user explicitly re-selects is honoured -- spawned
    again, and its `pending.json` entry's `armedAt` refreshed to the new
    restore's clock, not left at the old value."""
    store.write_pending(
        Pending(
            written_at=100,
            entries=(PendingEntry(session_id="armed-1", title="Old title", cwd="c", armed_at=100),),
        )
    )
    row = make_row(session_id="armed-1", title="Armed one", armed=True)
    row_set = Rows(desk=(row,), young=())
    spawner = RecordingSpawner()

    restore.restore_selected(
        frozenset({"armed-1"}),
        row_set,
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 999_999,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    assert len(spawner.calls) == 1
    pending = store.read_pending()
    assert pending is not None
    [entry] = pending.entries
    assert entry.session_id == "armed-1"
    assert entry.armed_at == 999_999  # refreshed, not the stale 100


def test_run_writes_no_pending_entries(store: Store) -> None:
    """D16: `--run` tabs reach the registry within seconds; recording them
    in `pending.json` would make the next capture assert `armed: true`
    about a session that is, by then, ordinarily live."""
    row = make_row(session_id="s1")
    row_set = Rows(desk=(row,), young=())
    spawner = RecordingSpawner()

    restore.restore_selected(
        frozenset({"s1"}),
        row_set,
        store,
        emit_of(boot_time=1),
        run=True,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1, entries=(registry_entry("s1"),)),
    )

    assert len(spawner.calls) == 1
    assert store.read_pending() is None
    [layout] = store.all_layouts()
    assert layout.sessions[0].armed is False


def test_a_second_restore_merges_rather_than_replaces_pending(store: Store) -> None:
    """D17: `expire_pending` runs first, then the newly armed set unions in,
    deduped by session_id, new `armed_at` winning -- a second restore must
    not drop the first restore's still-armed tabs out of §7 step 2's
    already-live union."""
    row_a = make_row(session_id="a", title="Session A")
    row_b = make_row(session_id="b", title="Session B")
    spawner = RecordingSpawner()

    restore.restore_selected(
        frozenset({"a"}),
        Rows(desk=(row_a,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        # zero live claude.exe: "a" stays armed through expire_pending.
        emit_runner=lambda: emit_of(boot_time=1),
    )
    assert store.read_pending().session_ids == frozenset({"a"})

    restore.restore_selected(
        frozenset({"b"}),
        Rows(desk=(row_b,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 2_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    assert store.read_pending().session_ids == frozenset({"a", "b"})
    assert len(spawner.calls) == 2


def test_verify_wording_under_arming_never_claims_a_session_came_up(store: Store) -> None:
    """Rewritten (test-quality defect #2, S6 review): the old assertion
    greped one literal after string surgery
    (`.replace("does not confirm a session came up", "")`), so rewording
    the summary to e.g. "2 sessions are live" would have passed vacuously.
    This pins the EXACT honest wording arming must use, and separately
    asserts the STRONG, `--run`-only claim (`verify`'s own literal
    `f"restored {came_up}/{len(spawned)}"` template, plus its
    `"not confirmed live at verification"` per-row line) never appears here at all -- tying
    the check to which code path's wording is on screen, not to a
    substring that survives any future rewording of either one."""
    row = make_row(session_id="s1", title="Some session")
    out = StringIO()

    restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    printed = out.getvalue()
    assert "pending.json holds 1/1 entry for requested armed launches." in printed
    assert (
        "This does NOT confirm a session came up -- an armed tab hosts no "
        "process yet. Display readiness does not prove exact session identity."
    ) in printed
    assert "restored " not in printed  # the --run-only strong-check template
    assert "not confirmed live at verification" not in printed


def test_verify_under_run_reports_sessions_that_did_not_come_up(store: Store) -> None:
    """Under `--run`, verify's check IS strong: a session either shows up in
    the fresh `-Emit` or it is named as missing."""
    came_up_row = make_row(session_id="came-up", title="Came up")
    missing_row = make_row(session_id="missing", title="Never showed")
    out = StringIO()

    outcome = restore.restore_selected(
        frozenset({"came-up", "missing"}),
        Rows(desk=(came_up_row, missing_row), young=()),
        store,
        emit_of(boot_time=1),
        run=True,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        # Only "came-up" shows up live in the post-restore -Emit.
        emit_runner=lambda: emit_of(boot_time=1, entries=(registry_entry("came-up"),)),
        out=out,
    )

    printed = out.getvalue()
    assert "restored 1/2" in printed
    assert '"Never showed" not confirmed live at verification' in printed
    assert outcome.verification_error == "1/2 sessions not confirmed live at verification"
    assert len(outcome.spawned) == 2 and outcome.failed == ()


def test_spawn_failure_is_collected_and_reported_not_raised(store: Store) -> None:
    row = make_row(session_id="s1", title="Some session")

    def failing_spawner(_argv: list[str]) -> None:
        raise OSError("wt.exe not found")

    outcome = restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=failing_spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    assert outcome.spawned == ()
    assert len(outcome.failed) == 1
    assert "wt.exe not found" in outcome.failed[0].error
    assert store.all_layouts() == []  # nothing spawned -> nothing recorded


def test_nothing_selected_prints_nothing_selected_and_touches_no_store(store: Store) -> None:
    spawner = RecordingSpawner()
    out = StringIO()

    outcome = restore.restore_selected(
        frozenset(),
        Rows(desk=(make_row(),), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )
    assert outcome.spawned == ()
    assert store.all_layouts() == []
    assert spawner.calls == []
    printed = out.getvalue()
    assert "Nothing selected -- nothing to restore." in printed
    # A mutation that skips the early "nothing spawned" return falls through
    # to the "Armed N session(s):" summary block even when N is 0 -- this
    # line must never appear when nothing was ever spawned.
    assert "session(s):" not in printed


def test_every_row_failing_preflight_prints_nothing_was_restored_not_nothing_selected(
    store: Store,
) -> None:
    """The two "nothing spawned" messages are NOT interchangeable: "nothing
    selected" means the user picked nothing; "nothing was restored" means
    they picked something and pre-flight (or every spawn) rejected all of
    it. Collapsing them would tell the user their selection was silently
    ignored, which it was not."""
    row = make_row(session_id="dead")
    out = StringIO()

    restore.restore_selected(
        frozenset({"dead"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_missing,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    printed = out.getvalue()
    assert "Nothing was restored" in printed
    assert "Nothing selected -- nothing to restore." not in printed


# --- F4: arm.ps1 must exist before anything is spawned -------------------


def test_restore_selected_refuses_to_spawn_when_arm_script_is_missing(
    store: Store, tmp_path: Path
) -> None:
    """D23/F4: `restore.py` never checked `arm_script.is_file()` before this
    fix, while `emit.py` does exactly that for its own script. A missing or
    moved `arm.ps1` used to produce N spawns of a broken command (F3's
    failure shape on every tab) while `pending.json` still recorded
    success. Checked once, before the spawn loop: `spawner` must never be
    called at all."""
    missing_script = tmp_path / "does-not-exist" / "arm.ps1"
    row = make_row(session_id="s1", title="Some session")
    spawner = RecordingSpawner()
    out = StringIO()

    outcome = restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        arm_script=missing_script,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    assert spawner.calls == []
    assert outcome.spawned == ()
    assert store.all_layouts() == []
    printed = out.getvalue()
    assert "arm.ps1" in printed and "not found" in printed


def test_default_arm_script_resolves_to_a_real_file_in_this_source_checkout() -> None:
    """D23/F4: the brief's own required assertion. `DEFAULT_ARM_SCRIPT`'s
    `parents[2]` derivation only resolves inside a source checkout --
    `pyproject.toml`'s wheel target ships only `src/termsync`, not
    `snapshot/` -- so this is a real, meaningful check in THIS repo, not a
    tautology."""
    assert restore.DEFAULT_ARM_SCRIPT.exists()


# --- `snapshot/arm.ps1`, static content -----------------------------------


def test_arm_ps1_never_forks_resumes_by_id_and_starts_with_a_utf8_bom() -> None:
    """No test anywhere executed, parsed, or even READ `snapshot/arm.ps1`
    before this one. The `--fork-session` guarantee -- which the brief says
    would "silently duplicate every session forever" if it regressed --
    rested on nothing but the file's own comment."""
    raw = restore.DEFAULT_ARM_SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        "PS 5.1 needs a UTF-8 BOM to read this file's section-sign comment"
    )
    text = raw.decode("utf-8-sig")
    lines = [line.strip() for line in text.splitlines()]
    # The actual invocation line, not a comment mentioning the flag by name
    # (this file's own `.DESCRIPTION` and a trailing `#` comment both say
    # "Never --fork-session" -- checking the whole text for the substring
    # would make this test pass or fail on prose, not on the real command).
    assert "claude --resume $SessionId" in lines
    assert "claude --resume $SessionId --fork-session" not in lines
    invocation_lines = [line for line in lines if line.startswith("claude ")]
    assert len(invocation_lines) == 1
    assert "--fork-session" not in invocation_lines[0]


# --- S2: §7 step 8's arming verification, behaviourally ------------------


def test_verify_arming_confirmed_count_reflects_actual_pending_not_a_hardcoded_n(
    store: Store,
) -> None:
    """S2: `confirmed` must be the ACTUAL count of `spawned` session ids
    present in `pending.json`, not `len(spawned)` -- a mutation that
    hardcodes the latter would report N/N even when `pending.json` is
    missing an entry. Constructed directly against `verify`: `pending.json`
    holds only one of the two rows handed to it, as if a torn write or a
    race dropped the other."""
    row_a = make_row(session_id="a", title="A")
    row_b = make_row(session_id="b", title="B")
    store.write_pending(
        Pending(
            written_at=1,
            entries=(PendingEntry(session_id="a", title="A", cwd="c", armed_at=1),),
        )
    )

    lines, error = restore.verify(
        store,
        [row_a, row_b],
        run=False,
        emit_runner=lambda: emit_of(boot_time=1),
        sleep=no_sleep,
        verify_delay_s=20.0,
    )

    assert any("1/2" in line for line in lines)
    assert error is None  # Pending shortfalls alone do not fail an armed restore.


def test_verify_runs_after_record_restore_not_before(store: Store) -> None:
    """S2: `_record_restore` must run BEFORE `verify` -- `verify`'s arming
    check reads `pending.json` fresh, so if the ordering were reversed, the
    entry `_record_restore` is about to write would not exist yet and every
    armed restore would report `0/N` no matter how many actually armed."""
    row = make_row(session_id="s1", title="Some title")
    out = StringIO()

    restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    printed = out.getvalue()
    assert "pending.json holds 1/1 entry" in printed


def test_verify_sleeps_the_configured_delay_before_checking(store: Store) -> None:
    """S2: the ~20s settle (§7 step 8) must actually be invoked -- a
    mutation that deletes the `sleep(verify_delay_s)` call stays green
    against every other assertion in this file, since none of them check
    that `sleep` was called at all."""
    row = make_row(session_id="s1", title="Some title")
    sleep_calls: list[float] = []

    def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=recording_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        verify_delay_s=17.5,
    )

    assert sleep_calls == [17.5]


# --- S3: young rows must be restorable, not just displayed ----------------


def test_a_young_row_can_be_selected_and_spawned(store: Store) -> None:
    """S3: `_select_rows` reads BOTH `row_set.desk` and `row_set.young` --
    §7 step 3's "started since the last snapshot" bucket must be
    restorable too, not merely displayed. No test in this file put a row
    in `Rows.young` before this one; a mutation dropping `row_set.young`
    from `_select_rows` stayed green against the whole suite."""
    young_row = make_row(session_id="young-1", title="Started after the last snapshot", young=True)
    row_set = Rows(desk=(), young=(young_row,))
    spawner = RecordingSpawner()

    outcome = restore.restore_selected(
        frozenset({"young-1"}),
        row_set,
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    assert len(spawner.calls) == 1
    assert outcome.spawned[0].session_id == "young-1"


# --- U8: preserve unrelated pending entries until normal capture --------


def test_restore_defers_live_pending_expiry_to_capture(store: Store) -> None:
    """U8: preserve unrelated pending entries; the picker observation can be stale."""
    store.write_pending(
        Pending(
            written_at=100,
            entries=(PendingEntry(session_id="came-back", title="t", cwd="c", armed_at=100),),
        )
    )
    row = make_row(session_id="new-one", title="New one")
    spawner = RecordingSpawner()

    restore.restore_selected(
        frozenset({"new-one"}),
        Rows(desk=(row,), young=()),
        store,
        # "came-back" now shows up live in the registry passed to this restore.
        emit_of(boot_time=1, entries=(registry_entry("came-back"),)),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 2_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    pending = store.read_pending()
    assert pending is not None
    assert pending.session_ids == frozenset({"new-one", "came-back"})


def test_restore_defers_ttl_pending_expiry_to_capture(store: Store) -> None:
    """U8: normal capture expires pending entries; restore only merges its launches."""
    ttl_ms = 24 * 60 * 60 * 1000
    store.write_pending(
        Pending(
            written_at=100,
            entries=(PendingEntry(session_id="stale", title="t", cwd="c", armed_at=100),),
        )
    )
    row = make_row(session_id="new-one", title="New one")
    spawner = RecordingSpawner()

    restore.restore_selected(
        frozenset({"new-one"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=1),  # zero live -- "stale" ages out on time alone
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=spawner,
        clock=lambda: 100 + ttl_ms + 1,  # just past the 24h TTL
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
    )

    pending = store.read_pending()
    assert pending is not None
    assert pending.session_ids == frozenset({"new-one", "stale"})


# --- S6: pre-flight skips must reach the user, even alongside a success ---


def test_preflight_skip_line_reaches_the_user_alongside_a_successful_spawn(store: Store) -> None:
    """S6/§3.9: a pre-flight skip must be PRINTED, never silent -- even when
    other rows in the same restore succeed. A mutation that deletes the
    `skipping "<title>": <reason>` line entirely stays green against every
    other test in this file, since none of them assert its presence when
    something else in the same call also spawns successfully."""
    good = make_row(session_id="good", title="Good session")
    bad = make_row(session_id="bad", title="Bad session", cwd=r"D:\gone")
    out = StringIO()

    def session_file(session_id: str) -> Path | None:
        return Path("f.jsonl") if session_id == "good" else None

    restore.restore_selected(
        frozenset({"good", "bad"}),
        Rows(desk=(good, bad), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=session_file,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    printed = out.getvalue()
    assert 'skipping "Bad session": session file deleted' in printed
    assert "Armed 1 session(s):" in printed


# --- S7: the restore record's CONTENT, not just its id set + armed flag ---


def test_write_restore_record_carries_boot_id_title_last_prompt_at_and_registry_versions(
    store: Store,
) -> None:
    """S7: nothing in this file asserted the CONTENT of the `source:
    "restore"` record beyond its id set and `armed` flag -- `boot_id`,
    `title`, `last_prompt_at` and `registryVersions` could all be blanked
    and stay green. This matters more given D20: before that fix a restore
    record could become the picker's OWN target, at which point these
    fields are what the user reads."""
    row = make_row(
        session_id="s1", title="A real title", last_prompt_at=12_345, cwd=r"D:\somewhere"
    )

    restore.restore_selected(
        frozenset({"s1"}),
        Rows(desk=(row,), young=()),
        store,
        emit_of(boot_time=987, entries=(registry_entry("s1", version="2.1.238"),)),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 5_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=987),
    )

    [layout] = store.all_layouts()
    assert layout.boot_id == 987
    assert layout.registry_versions == ("2.1.238",)
    [session] = layout.sessions
    assert session.title == "A real title"
    assert session.last_prompt_at == 12_345


# --- L5, restored to where it now matters: the PRINTED order --------------


def test_restore_selected_prints_in_screen_order_not_selection_or_id_order(store: Store) -> None:
    """Restores L5's guarantee to where it now actually reaches the user:
    the PRINTED output, not merely `_select_rows`'s return value.
    `test_select_rows_preserves_screen_order_not_session_id_order` (above)
    cannot see a sort introduced in `restore_selected`'s OWN print loop,
    since it never looks at printed text at all."""
    row_z = make_row(session_id="zzz-last-alphabetically", title="First on screen")
    row_a = make_row(session_id="aaa-first-alphabetically", title="Second on screen")
    out = StringIO()

    restore.restore_selected(
        frozenset({"zzz-last-alphabetically", "aaa-first-alphabetically"}),
        Rows(desk=(row_z, row_a), young=()),
        store,
        emit_of(boot_time=1),
        run=False,
        session_file=always_present,
        cwd_exists=always_exists,
        spawner=RecordingSpawner(),
        clock=lambda: 1_000,
        sleep=no_sleep,
        emit_runner=lambda: emit_of(boot_time=1),
        out=out,
    )

    printed = out.getvalue()
    first_pos = printed.index("First on screen")
    second_pos = printed.index("Second on screen")
    assert first_pos < second_pos
