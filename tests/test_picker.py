"""Behaviour tests for `picker.py` -- the interactive selection screen's pure
state machine (§2, work unit S5, §7 steps 1-6, §8's `[n]` affordance).

Every test here drives `apply_key`/`render`/`initial_state`/`visible_rows`
directly with plain strings and fixture `Row` objects -- no Windows, no
`msvcrt`, no real console, no clock read (this module is pure; `render`
takes `now` as a plain int). `run_picker`'s own tests at the bottom drive
the impure loop with a SCRIPTED `key_reader` (see `scripted_keys`, below)
that raises once exhausted -- `picker.read_key_msvcrt` is never called from
this file, per this unit's SAFETY section: it blocks on a real keypress,
which would hang the agent's own shell.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest

from termsync.picker import (
    KEY_LEGEND,
    PickerOutcome,
    apply_key,
    initial_state,
    render,
    run_picker,
    visible_rows,
)
from termsync.rows import Row, Rows


def test_window_headers_order_rows_and_cursor_by_captured_placement():
    late = replace(make_row(session_id="late", title="Later"), window_ordinal=4, tab_index=3)
    first = replace(make_row(session_id="first", title="First"), window_ordinal=2, tab_index=0)
    early = replace(make_row(session_id="early", title="Earlier"), window_ordinal=4, tab_index=1)
    unknown = make_row(session_id="unknown", title="Unknown")
    state = initial_state(Rows((late, unknown, first, early), ()))
    assert visible_rows(state) == (first, early, late, unknown)
    text = "\n".join(render(state, now=1000))
    assert text.index("WINDOW 2  (1 session)") < text.index("First")
    assert text.index("WINDOW 4  (2 sessions)") < text.index("Earlier") < text.index("Later")
    assert text.index("Later") < text.index("WINDOW UNKNOWN  (1 session)")
    for expected in ("first", "early", "late", "unknown"):
        current = visible_rows(state)[state.cursor]
        assert current.session_id == expected
        cursor_line = next(line for line in render(state, now=1000) if line.startswith("> "))
        assert current.title in cursor_line
        state, _ = apply_key(state, " ")
        assert expected not in state.selected
        state, _ = apply_key(state, "DOWN")
    assert state.selected == frozenset()


def test_filtered_windows_keep_source_numbers_and_count_only_visible_rows():
    first = replace(make_row(session_id="a", title="Hidden"), window_ordinal=2, tab_index=0)
    second = replace(make_row(session_id="b", title="Match"), window_ordinal=8, tab_index=5)
    third = replace(second, session_id="c", title="Hidden too", tab_index=6)
    state = type_filter(initial_state(Rows((third, first, second), ())), "match")
    text = "\n".join(render(state, now=1000))
    assert "WINDOW 8  (1 session)" in text
    assert "WINDOW 2" not in text
    assert "WINDOW 1" not in text
    assert "3 selected (2 not shown by the current filter)" in text
    assert visible_rows(state) == (second,)
    state, _ = apply_key(state, "ENTER")
    state, _ = apply_key(state, " ")
    assert state.selected == frozenset({"a", "c"})


def test_single_known_window_is_flat_and_legacy_placement_is_explicitly_unknown():
    row = replace(make_row(), window_ordinal=3, tab_index=2)
    assert "WINDOW" not in "\n".join(render(initial_state(Rows((row,), ())), now=1000))
    legacy = replace(row, window_ordinal=None, tab_index=None)
    assert "WINDOW UNKNOWN  (1 session)" in "\n".join(
        render(initial_state(Rows((legacy,), ())), now=1000)
    )


@pytest.mark.parametrize("fields", [
    {"agent": "codex"}, {"armed": True}, {"window_ordinal": True},
    {"window_ordinal": 0}, {"tab_index": None}, {"tab_index": -1},
])
def test_untrusted_placement_is_never_shown_as_a_known_window(fields):
    row = replace(make_row(), window_ordinal=3, tab_index=0)
    row = replace(row, **fields)
    text = "\n".join(render(initial_state(Rows((row,), ())), now=1000))
    assert "WINDOW UNKNOWN  (1 session)" in text
    assert "WINDOW 3" not in text


def test_unknown_archived_young_sections_keep_defaults_and_duplicate_rows():
    armed = replace(make_row(session_id="armed", title="Same"), armed=True)
    ordinary = make_row(session_id="ordinary", title="Same")
    archived = replace(make_row(session_id="archive", title="Archived"),
                       agent="codex", archived=True, window_ordinal=4, tab_index=0)
    young = replace(make_row(session_id="young", title="Young", young=True),
                    window_ordinal=5, tab_index=0)
    state = initial_state(Rows((archived, armed, ordinary), (young,)))
    text = "\n".join(render(state, now=1000))
    assert sum("Same" in line for line in text.splitlines() if line[2:5] in ("[x]", "[ ]")) == 2
    assert text.index("WINDOW UNKNOWN  (2 sessions)") < text.index("ARCHIVED CODEX")
    assert text.index("ARCHIVED CODEX") < text.index("STARTED SINCE")
    assert text.count("-- window unknown") == 2
    assert "WINDOW 4" not in text and "WINDOW 5" not in text
    assert state.selected == frozenset({"ordinary", "young"})
    filtered = type_filter(state, "absent")
    empty = "\n".join(render(filtered, now=1000))
    assert 'No rows match "absent".' in empty
    assert "WINDOW" not in empty and "ARCHIVED CODEX" not in empty

# --- fixtures / helpers ---------------------------------------------------


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


def deleted_row(session_id: str = "gone", title: str = "Deleted") -> Row:
    """A row §7 steps 4-5 make unselectable because its session file is
    gone -- `selectable=False`, `session_file=None`."""
    return make_row(session_id=session_id, title=title, session_file=None, selectable=False)


def live_row(session_id: str = "live-1", title: str = "Already open") -> Row:
    return make_row(session_id=session_id, title=title, live=True, selectable=False)


def type_filter(state, text: str):
    """Press `/` to enter filtering mode, then type `text` one character at
    a time -- the sequence every real keystroke-by-keystroke filter session
    goes through. Returns the resulting state, still in filtering mode
    (caller presses ENTER/ESC itself if it needs to leave it)."""
    state, _ = apply_key(state, "/")
    for ch in text:
        state, _ = apply_key(state, ch)
    return state


def scripted_keys(keys: list[str]) -> Callable[[], str]:
    """Same contract as `test_dry_run.py`'s own helper: returns each key in
    order, then raises rather than ever falling through to a blocking real
    read. Duplicated here (not imported from `test_dry_run.py`) because
    test files in this project are self-contained fixtures, matching every
    other test file's own `make_row`/helper pattern.
    """
    it = iter(keys)

    def read() -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("picker asked for a key beyond the scripted sequence") from None

    return read


# --- initial_state: pre-selection ------------------------------------------


def test_initial_state_preselects_selectable_rows_but_never_an_armed_one() -> None:
    """Review finding F2/H2 [HIGH], docs/reviews/2026-08-24-review-s5.md.
    This test used to be named `..._preselects_exactly_the_selectable_rows`
    and assert `state.selected == {"restorable", "armed-1"}` with the
    comment "armed rows ARE selectable" -- true, but irrelevant to
    pre-selection, and the review flagged this as the fourth green test in
    this project asserting a defect as correct behaviour.

    §2 line 85 says an armed session "is shown with an `~` marker instead
    of `[x]`/`[ ]`" and "it is restorable" -- the coordinator-approved
    reading (recorded as D11) is that "restorable" there means RECOVERABLE
    (the tab already exists, waiting for Enter), not "a selectable item
    pre-checked like any other row". §7 step 2's whole reason to exist is
    that restoring an armed session again duplicates a tab that already
    has one; pre-selecting it by default did exactly that -- demonstrated
    live by the review (P3): one armed session, zero `claude.exe`
    processes anywhere, and `termsync restore`'s Enter payload contained
    the armed id anyway. Armed rows stay `selectable=True` -- the user can
    still check one back on with `[space]` -- only the DEFAULT changes.
    """
    restorable = make_row(session_id="restorable")
    unselectable = deleted_row(session_id="gone")
    armed = make_row(session_id="armed-1", armed=True)
    live = live_row(session_id="live-1")

    state = initial_state(Rows(desk=(restorable, unselectable, armed, live), young=()))

    assert state.selected == {"restorable"}
    assert "armed-1" not in state.selected, "an armed row must never be pre-selected (D11)"
    assert "gone" not in state.selected
    assert "live-1" not in state.selected

    # But it is still SELECTABLE -- the user can check it back on by hand.
    armed_row_in_rows = next(row for row in state.rows if row.session_id == "armed-1")
    assert armed_row_in_rows.selectable is True


def test_initial_state_ignores_prompt_count_entirely() -> None:
    """§2: "the tool does not decide which of his work matters" -- prompt
    count must play no part in pre-selection. A row with a huge prompt
    count and a row with none must be treated identically as long as both
    are selectable."""
    busy = make_row(session_id="busy", prompt_count=999)
    quiet = make_row(session_id="quiet", prompt_count=None)

    state = initial_state(Rows(desk=(busy, quiet), young=()))

    assert state.selected == {"busy", "quiet"}


def test_initial_state_flattens_desk_then_young_with_cursor_at_zero() -> None:
    desk_row = make_row(session_id="desk-1")
    young_row = make_row(session_id="young-1", young=True)

    state = initial_state(Rows(desk=(desk_row,), young=(young_row,)))

    assert state.rows == (desk_row, young_row)
    assert state.cursor == 0
    assert state.filter_text == ""
    assert state.filtering is False


# --- [space]: toggle, both directions --------------------------------------


def test_space_toggles_the_row_under_the_cursor_both_directions() -> None:
    row = make_row(session_id="s1")
    state = initial_state(Rows(desk=(row,), young=()))
    assert "s1" in state.selected  # pre-selected

    state, outcome = apply_key(state, " ")
    assert outcome is None
    assert "s1" not in state.selected

    state, outcome = apply_key(state, " ")
    assert outcome is None
    assert "s1" in state.selected


def test_space_moves_only_the_row_under_the_cursor_not_every_row() -> None:
    a = make_row(session_id="a")
    b = make_row(session_id="b")
    state = initial_state(Rows(desk=(a, b), young=()))  # cursor at index 0 -> row a

    state, _ = apply_key(state, " ")

    assert "a" not in state.selected
    assert "b" in state.selected


# --- [space] on an unselectable row: HIGH-RISK, verified by mutation ------


def test_space_on_an_unselectable_row_does_not_select_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One of this unit's three mutation-verified tests (see the final
    report). §2's key legend does not say what `[space]` on an unselectable
    row does; this unit's decision is a no-op that explains itself via
    `state.message` rather than silently doing nothing."""
    row = deleted_row(session_id="gone")
    state = initial_state(Rows(desk=(row,), young=()))
    assert state.selected == frozenset()  # never pre-selected

    state, outcome = apply_key(state, " ")

    assert outcome is None
    assert state.selected == frozenset(), "an unselectable row must never become selected"
    assert state.message is not None
    assert "gone" not in state.message  # message names the TITLE, not the session id
    assert "Deleted" in state.message


def test_space_on_a_live_row_explains_it_is_already_open() -> None:
    row = live_row(session_id="live-1", title="Already running")
    state = initial_state(Rows(desk=(row,), young=()))

    state, _ = apply_key(state, " ")

    assert state.selected == frozenset()
    assert state.message is not None
    assert "already open" in state.message.lower()


def test_message_is_cleared_by_the_next_key_of_any_kind() -> None:
    """Review finding L1 [LOW]: `state.message` is documented as cleared by
    "the NEXT key of any kind", but this test used to press only `DOWN` --
    one of the keys that happened to clear it because `_move_cursor`'s own
    branch passed `message=None`. The claim was false for a key that falls
    through to the bare "unrecognised key" branch (`return state, None`),
    which used to return `state` completely UNCHANGED, message included --
    reachable via any key `read_key_msvcrt` maps to `""` (Home, PageUp, F1,
    any unmapped extended scan code -- see its own docstring). `apply_key`
    now clears `message` once, unconditionally, before dispatch, so it is
    cleared by every key, not just the ones whose branch remembered to say
    so. Exercised here across several distinct key SHAPES: an ordinary
    unrecognised letter, an empty string (the real "unmapped extended key"
    value), and a key another branch does nothing special with (`UP`)."""
    row = deleted_row()
    other = make_row(session_id="other")
    state = initial_state(Rows(desk=(row, other), young=()))

    for probe_key in ("z", "", "UP"):
        state, _ = apply_key(state, " ")  # cursor on `row` (unselectable) -> sets message
        assert state.message is not None

        state, _ = apply_key(state, probe_key)
        assert state.message is None, f"key {probe_key!r} must clear the message but did not"


# --- [a]: select none, global -----------------------------------------------


def test_a_deselects_every_selection_including_ones_hidden_by_a_filter() -> None:
    matched = make_row(session_id="matched", title="Demo page rebuild")
    hidden = make_row(session_id="hidden", title="Something else entirely")
    state = initial_state(Rows(desk=(matched, hidden), young=()))
    assert state.selected == {"matched", "hidden"}

    # Filter the list down so `hidden` is not currently visible...
    state = type_filter(state, "page")
    state, _ = apply_key(state, "ENTER")  # stop typing, keep the filter
    assert visible_rows(state) == (matched,)

    # ...then [a] must still clear BOTH selections, per §2 ("none" is an
    # absolute, not "none of what you can currently see").
    state, outcome = apply_key(state, "a")

    assert outcome is None
    assert state.selected == frozenset()


# --- [q]: quit ---------------------------------------------------------------


def test_q_yields_quit_with_no_selection_ids() -> None:
    row = make_row(session_id="s1")
    state = initial_state(Rows(desk=(row,), young=()))

    _, outcome = apply_key(state, "q")

    assert outcome == PickerOutcome(kind="quit")
    assert outcome.selected_ids == frozenset()


# --- CTRL_C: global abort, either mode (L4) ----------------------------------


def test_ctrl_c_yields_quit_in_normal_mode() -> None:
    """L4 [LOW]: `read_key_msvcrt` translates a literal Ctrl+C byte to the
    symbolic key `"CTRL_C"`; `apply_key` must treat it as an immediate quit."""
    row = make_row(session_id="s1")
    state = initial_state(Rows(desk=(row,), young=()))

    _, outcome = apply_key(state, "CTRL_C")

    assert outcome == PickerOutcome(kind="quit")


def test_ctrl_c_yields_quit_even_while_filtering() -> None:
    """Unlike `q` (which becomes filter text while typing -- see
    `test_letter_commands_are_inert_while_filtering`), Ctrl+C is a global
    abort and must fire in EITHER mode: a user mid-filter with a stuck
    picker cannot be expected to first cancel the filter to quit."""
    row = make_row(session_id="s1")
    state = initial_state(Rows(desk=(row,), young=()))
    state = type_filter(state, "some te")
    assert state.filtering is True

    _, outcome = apply_key(state, "CTRL_C")

    assert outcome == PickerOutcome(kind="quit")


# --- ENTER: restore ----------------------------------------------------------


def test_enter_yields_restore_with_exactly_the_selected_ids() -> None:
    a = make_row(session_id="a")
    b = make_row(session_id="b")
    state = initial_state(Rows(desk=(a, b), young=()))
    state, _ = apply_key(state, " ")  # deselect `a` (cursor starts on it)

    _, outcome = apply_key(state, "ENTER")

    assert outcome is not None
    assert outcome.kind == "restore"
    assert outcome.selected_ids == frozenset({"b"})


# --- [n]: switch layout -------------------------------------------------------


def test_n_yields_switch_layout_carrying_the_current_selection() -> None:
    """H3/F3 [HIGH]: `[n]` used to carry an empty `selected_ids`, so
    re-entering the picker re-derived the selection from scratch and threw
    away every deselection the user had just made. It must carry
    `state.selected` instead -- `restore_interactive` intersects that
    against the new layout's rows (`initial_state`'s `carried_selection`)."""
    row = make_row(session_id="s1")
    other = make_row(session_id="s2")
    state = initial_state(Rows(desk=(row, other), young=()))
    state, _ = apply_key(state, " ")  # deselect s1 (cursor starts on it)
    assert state.selected == {"s2"}

    _, outcome = apply_key(state, "n")

    assert outcome == PickerOutcome(kind="switch_layout", selected_ids=frozenset({"s2"}))


def test_initial_state_carried_selection_survives_the_switch_but_never_grows() -> None:
    """H3/F3 [HIGH], the `initial_state` half of the fix: a carried
    selection from a previous layout is intersected against what is
    actually selectable NOW -- an id the new row set does not have drops
    silently, and nothing not in the carried set is ever added back just
    because it happens to be selectable in the new layout (the whole point
    of `[n]` no longer being a "reset every deselection" one-way door)."""
    stayed = make_row(session_id="stayed")
    reappeared = make_row(session_id="reappeared")  # was NOT in the carried set
    new_rows = Rows(desk=(stayed, reappeared), young=())

    state = initial_state(new_rows, carried_selection=frozenset({"stayed", "vanished"}))

    assert state.selected == {"stayed"}, "only ids BOTH carried and selectable now survive"
    assert "vanished" not in state.selected
    assert "reappeared" not in state.selected, "nothing is silently re-selected"


# --- `/` filter: fields matched, case sensitivity, selection-vs-view -------


def test_filter_matches_title_case_insensitively() -> None:
    row = make_row(session_id="s1", title="Project Plan Phase 3", cwd=r"D:\x")
    other = make_row(session_id="s2", title="Unrelated session", cwd=r"D:\y")
    state = initial_state(Rows(desk=(row, other), young=()))

    state = type_filter(state, "project plan")

    assert visible_rows(state) == (row,)


def test_filter_matches_an_uppercase_needle_against_a_lowercase_title() -> None:
    """Review finding F8/M5 mutation M9 [MEDIUM]: the case-insensitivity
    fix drops `.casefold()` from the NEEDLE only, keeping it on the
    haystack (`row.title.casefold()`), and every existing filter test
    typed an all-lower-case needle -- so the mutated comparison
    (`"project plan" in "project plan phase 3"`) stayed `True` and the
    test stayed green. What actually breaks is untested: an UPPER-case
    needle against a lower-case title matches nothing under the mutation."""
    row = make_row(session_id="s1", title="project plan review", cwd=r"D:\x")
    state = initial_state(Rows(desk=(row,), young=()))

    state = type_filter(state, "PROJECT")

    assert visible_rows(state) == (row,)


def test_filter_matches_cwd_not_just_title() -> None:
    row = make_row(session_id="s1", title="Session one", cwd=r"C:\fixture\demo_project")
    other = make_row(session_id="s2", title="Session two", cwd=r"C:\fixture\tool")
    state = initial_state(Rows(desk=(row, other), young=()))

    state = type_filter(state, "demo")

    assert visible_rows(state) == (row,)


def test_filter_does_not_match_session_id() -> None:
    """Decision recorded in `picker.visible_rows`'s own docstring: the
    filter matches title/cwd, never the session id -- typing a UUID
    fragment into the picker is not a supported way to find a row."""
    row = make_row(session_id="a0000001-uuid", title="A session", cwd=r"D:\x")
    state = initial_state(Rows(desk=(row,), young=()))

    state = type_filter(state, "a000")

    assert visible_rows(state) == ()


def test_a_row_filtered_out_of_view_keeps_its_selection_state() -> None:
    """§2's requirement, verified by mutation (see the final report):
    filtering must never silently change the *selection* -- a row hidden
    by the filter stays selected (or stays deselected) exactly as it was."""
    kept_selected = make_row(session_id="a", title="Alpha review")
    stays_deselected = make_row(session_id="b", title="Bravo review")
    state = initial_state(Rows(desk=(kept_selected, stays_deselected), young=()))
    # Deselect `b` before filtering (cursor starts on `a`; move to `b` first).
    state, _ = apply_key(state, "DOWN")
    state, _ = apply_key(state, " ")
    assert state.selected == {"a"}

    state = type_filter(state, "alpha")
    assert visible_rows(state) == (kept_selected,)  # `b` is now hidden
    assert state.selected == {"a"}, "hiding `b` must not touch its (deselected) state"

    # Clear the filter again: `b` reappears, still deselected; `a` still selected.
    state, _ = apply_key(state, "ESC")
    assert visible_rows(state) == (kept_selected, stays_deselected)
    assert state.selected == {"a"}


# --- filter is cleared/escaped sensibly ------------------------------------


def test_escape_clears_the_filter_and_exits_typing_mode() -> None:
    row = make_row(session_id="a", title="Alpha")
    other = make_row(session_id="b", title="Bravo")
    state = initial_state(Rows(desk=(row, other), young=()))

    state = type_filter(state, "alpha")
    assert state.filter_text == "alpha"
    assert state.filtering is True

    state, outcome = apply_key(state, "ESC")

    assert outcome is None
    assert state.filter_text == ""
    assert state.filtering is False
    assert visible_rows(state) == (row, other)


def test_enter_while_filtering_exits_typing_mode_but_keeps_the_filter() -> None:
    row = make_row(session_id="a", title="Alpha")
    other = make_row(session_id="b", title="Bravo")
    state = initial_state(Rows(desk=(row, other), young=()))

    state = type_filter(state, "alpha")
    state, outcome = apply_key(state, "ENTER")

    assert outcome is None, "ENTER while filtering must stop typing, not restore"
    assert state.filtering is False
    assert state.filter_text == "alpha"
    assert visible_rows(state) == (row,)


def test_backspace_erases_one_character_at_a_time() -> None:
    row = make_row(session_id="a", title="Alpha")
    state = initial_state(Rows(desk=(row,), young=()))
    state = type_filter(state, "alp")
    assert state.filter_text == "alp"

    state, _ = apply_key(state, "BACKSPACE")

    assert state.filter_text == "al"
    assert state.filtering is True


def test_space_while_filtering_is_typed_text_not_a_toggle() -> None:
    """Mode decision, documented in `_apply_key_filtering`'s own docstring:
    while typing after `/`, EVERY printable character (space included) is
    filter text, never a command -- otherwise a multi-word title could
    never be typed as a filter."""
    row = make_row(session_id="a", title="Project plan review")
    state = initial_state(Rows(desk=(row,), young=()))
    assert "a" in state.selected  # pre-selected

    state = type_filter(state, "control ")

    assert state.filter_text == "control "
    assert "a" in state.selected, "space while typing must never toggle the row under the cursor"


def test_letter_commands_are_inert_while_filtering() -> None:
    """`a`/`j`/`k`/`n`/`q` must all become filter text while typing, never
    fire their normal-mode command -- otherwise no title containing those
    letters could be typed as a filter."""
    row = make_row(session_id="a")
    state = initial_state(Rows(desk=(row,), young=()))
    state, _ = apply_key(state, "/")

    for ch in "ajnkq":
        state, outcome = apply_key(state, ch)
        assert outcome is None

    assert state.filter_text == "ajnkq"
    assert state.filtering is True
    assert "a" in state.selected  # `a`'s "select none" command never fired


# --- cursor movement: both ends, empty/fully-filtered lists ----------------


def test_cursor_movement_stops_at_both_ends() -> None:
    rows = tuple(make_row(session_id=f"s{i}") for i in range(3))
    state = initial_state(Rows(desk=rows, young=()))
    assert state.cursor == 0

    state, _ = apply_key(state, "UP")  # already at the top
    assert state.cursor == 0

    state, _ = apply_key(state, "DOWN")
    state, _ = apply_key(state, "DOWN")
    assert state.cursor == 2

    state, _ = apply_key(state, "DOWN")  # already at the bottom
    assert state.cursor == 2

    state, _ = apply_key(state, "j")
    assert state.cursor == 2  # j is DOWN; still clamped
    state, _ = apply_key(state, "k")
    assert state.cursor == 1  # k is UP


# --- HIGH-RISK: cursor never indexes out of range, empty/fully-filtered ---


def test_cursor_never_indexes_out_of_range_on_an_empty_list() -> None:
    """Second of this unit's three mutation-verified tests. An empty desk
    (`Rows(desk=(), young=())`) must never raise on ANY key -- this is the
    "single most likely crash in this unit" per the brief."""
    state = initial_state(Rows(desk=(), young=()))

    for key in (" ", "UP", "DOWN", "j", "k", "a", "n", "q", "ENTER"):
        state, _outcome = apply_key(state, key)
        assert state.cursor == 0
        assert visible_rows(state) == ()

    # render() must not raise either, and must say something honest.
    lines = render(state, now=1000)
    assert "Nothing to restore" in "\n".join(lines)

    # Entering the filter and typing on an empty list must not crash either.
    state = type_filter(state, "x")
    assert state.cursor == 0
    assert visible_rows(state) == ()
    lines = render(state, now=1000)
    assert 'No rows match "x".' in "\n".join(lines)


def test_cursor_never_indexes_out_of_range_on_a_fully_filtered_list() -> None:
    """Third of this unit's three mutation-verified tests. A filter that
    matches nothing must leave the cursor at a safe 0, and every navigation
    key must remain a no-op rather than raising `IndexError`."""
    row = make_row(session_id="a", title="Alpha")
    state = initial_state(Rows(desk=(row,), young=()))

    state = type_filter(state, "zzz-does-not-match-anything")
    assert visible_rows(state) == ()
    assert state.cursor == 0

    state, outcome = apply_key(state, "ENTER")  # stop typing; filter stays applied
    assert outcome is None
    assert state.filtering is False

    # Now exercise navigation/toggle/restore on the (still empty) visible
    # list with typing mode OFF, where " " and "ENTER" mean toggle/restore
    # rather than more filter text.
    for key in ("UP", "DOWN", " "):
        state, outcome = apply_key(state, key)
        assert outcome is None
        assert state.cursor == 0
        assert visible_rows(state) == ()

    _, restore_outcome = apply_key(state, "ENTER")
    assert restore_outcome is not None
    assert restore_outcome.kind == "restore"
    # `a` was pre-selected before the filter hid it, and nothing visible
    # was ever toggled -- its selection must survive untouched (§2).
    assert restore_outcome.selected_ids == frozenset({"a"})

    lines = render(state, now=1000)
    assert 'No rows match "zzz-does-not-match-anything".' in "\n".join(lines)


# --- F4: empty-state messages scope to what is actually empty --------------


def test_a_filter_matching_only_a_young_row_does_not_claim_no_rows_match() -> None:
    """Review finding F4/M1 [MEDIUM]: `render`'s "no rows match" message
    used to be computed from the DESK count alone, so a filter that matches
    only a young row printed `No rows match "..."` directly above the
    matching row two lines later -- immediately self-contradicting."""
    desk_row = make_row(session_id="desk-1", title="Desk session")
    young_row = make_row(session_id="young-1", title="Progress measurement", young=True)
    state = initial_state(Rows(desk=(desk_row,), young=(young_row,)))

    state = type_filter(state, "progress")

    text = "\n".join(render(state, now=1000))
    assert 'No rows match "progress".' not in text
    assert "Progress measurement" in text


def test_an_empty_desk_with_a_live_young_bucket_does_not_claim_nothing_to_restore() -> None:
    """Review finding F4/M1 [MEDIUM]: an empty desk with a non-empty young
    bucket used to open on "Nothing to restore -- this desk holds no
    sessions." directly above a row Enter would in fact restore -- the
    exact post-reboot state §7 step 3's "started since the last snapshot"
    bucket exists to show."""
    young_row = make_row(session_id="young-1", title="Four-week progress", young=True)
    state = initial_state(Rows(desk=(), young=(young_row,)))

    text = "\n".join(render(state, now=1000))
    assert "Nothing to restore -- this desk holds no sessions." not in text
    assert "Four-week progress" in text


# --- F5: the legend reflects the active mode --------------------------------


def test_key_legend_matches_2s_string_verbatim() -> None:
    """Review finding F8/M5 mutation M19 [MEDIUM]: every existing assertion
    about `KEY_LEGEND` was `assert KEY_LEGEND in text`, comparing the
    module constant to output rendered from that SAME constant -- a
    tautology true for any value of `KEY_LEGEND`, including nonsense.
    Pinned here against §2's own literal text (§13.1: the V0 exit
    criterion), character for character."""
    assert KEY_LEGEND == (
        "[space] toggle  [/] filter  [enter] restore selected  [a] none  [n] newest  [q] quit"
    )


def test_filter_mode_legend_names_esc_and_enter_not_the_dead_normal_mode_keys() -> None:
    """Review finding F5/M2 [MEDIUM]: while typing a filter, all six of
    `KEY_LEGEND`'s commands are dead (every one becomes filter text -- see
    `_apply_key_filtering`), and the two keys that DO work (`ESC`, `ENTER`)
    used to appear nowhere. The legend must switch with the mode."""
    row = make_row(session_id="a", title="Alpha")
    state = initial_state(Rows(desk=(row,), young=()))
    state = type_filter(state, "al")

    text = "\n".join(render(state, now=1000))

    assert "esc" in text.lower()
    assert "[q] quit" not in text
    assert KEY_LEGEND not in text


# --- F6: a selected count, and whether a filter is hiding some of it -------


def test_selected_count_line_shows_zero_when_nothing_is_selected() -> None:
    row = make_row(session_id="a")
    state = initial_state(Rows(desk=(row,), young=()))
    state, _ = apply_key(state, " ")  # deselect the only row

    text = "\n".join(render(state, now=1000))

    assert "0 selected" in text


def test_selected_count_line_names_rows_hidden_by_the_current_filter() -> None:
    """Review finding F6/M3 [MEDIUM]: filtering never touches the
    selection (correct, kept as-is), but nothing on screen used to say how
    many of the selected rows a filter was hiding -- Enter restores the
    whole selection while the screen shows only a narrowed view of it."""
    matched = make_row(session_id="matched", title="Demo page rebuild")
    hidden_selected = make_row(session_id="hidden", title="Something else")
    state = initial_state(Rows(desk=(matched, hidden_selected), young=()))
    assert state.selected == {"matched", "hidden"}

    state = type_filter(state, "page")
    state, _ = apply_key(state, "ENTER")  # stop typing, keep the filter
    assert visible_rows(state) == (matched,)

    text = "\n".join(render(state, now=1000))
    assert "2 selected" in text
    assert "1 not shown by the current filter" in text


def test_selected_count_line_omits_the_hidden_clause_with_no_filter_active() -> None:
    row = make_row(session_id="a")
    state = initial_state(Rows(desk=(row,), young=()))

    text = "\n".join(render(state, now=1000))

    assert "1 selected" in text
    assert "not shown" not in text


# --- F7: cwd is the leaf, padded/truncated to a column ----------------------


def test_row_shows_the_cwd_leaf_not_the_full_path() -> None:
    """Review finding F7/M4 [MEDIUM]: §2 shows `demo_project`, the
    leaf, not the full `C:\\fixture\\demo_project` path the row used
    to print in parentheses."""
    row = make_row(session_id="a", title="Some session", cwd=r"C:\fixture\demo_project")
    state = initial_state(Rows(desk=(row,), young=()))

    line = next(line for line in render(state, now=1000) if "Some session" in line)

    assert "demo_project" in line
    assert r"C:\fixture\demo_project" not in line
    assert "(" not in line  # the parenthesised full-path form is gone entirely


def test_render_row_pins_the_real_prompt_count_and_age_exactly() -> None:
    """Review finding F8/M5 mutations M16/M17 [MEDIUM]: the age and prompt
    count could both be deleted from every selectable row and the suite
    stayed green, because `make_row`'s default `last_prompt_at=None` means
    `health.relative_age_phrase` was never exercised from this file at all
    (the previous review's own E/F mutations, reproduced in this new
    renderer). Pinned here against a REAL, POSITIVE age and a real count."""
    now = 1_792_750_000_000
    row = make_row(
        session_id="s1",
        title="Real session",
        prompt_count=14,
        last_prompt_at=now - 2 * 3_600_000,  # 2 real hours before `now`
    )
    state = initial_state(Rows(desk=(row,), young=()))

    line = next(line for line in render(state, now=now) if "Real session" in line)

    assert "14 prompts" in line
    assert "2 hours ago" in "\n".join(render(state, now=now))
    assert datetime.fromtimestamp(row.last_prompt_at / 1000).strftime("%Y-%m-%d %H:%M") in line


# --- render: cursor / selection / unselectable states, distinguishably ----


def test_render_marks_cursor_selection_and_unselectable_states_distinguishably() -> None:
    selected_row = make_row(session_id="sel", title="Selected row")
    unselected_row = make_row(session_id="unsel", title="Unselected row")
    unselectable = deleted_row(session_id="gone", title="Deleted row")
    state = initial_state(Rows(desk=(selected_row, unselected_row, unselectable), young=()))
    # deselect the second row, move cursor to it
    state, _ = apply_key(state, "DOWN")
    state, _ = apply_key(state, " ")

    lines = render(state, now=1000)
    text = "\n".join(lines)

    selected_line = next(line for line in lines if "Selected row" in line)
    unselected_line = next(line for line in lines if "Unselected row" in line)
    unselectable_line = next(line for line in lines if "Deleted row" in line)

    # Every row line is a 2-char cursor prefix ("> " or "  ") followed by
    # the marker -- `.strip()` would also eat the "> " prefix and defeat
    # the cursor-presence assertions below, so slice past it explicitly.
    assert selected_line[2:].startswith("[x]")
    assert not selected_line.startswith(">")  # cursor is on the second row
    assert unselected_line[2:].startswith("[ ]")
    assert unselected_line.startswith(">")  # cursor marker present
    assert unselectable_line[2:].startswith("[-]")
    assert "session file deleted" in unselectable_line
    # the three markers must all be textually distinct from one another
    assert len({selected_line[2:5], unselected_line[2:5], unselectable_line[2:5]}) == 3
    assert KEY_LEGEND in text


def test_render_shows_live_and_armed_suffixes() -> None:
    """D11/F2 [HIGH]: an armed row starts UNCHECKED (`[ ]`) -- it is
    selectable but never pre-selected (see
    `test_initial_state_preselects_selectable_rows_but_never_an_armed_one`)
    -- and still becomes `[x]` if the user checks it back on by hand."""
    live = live_row(session_id="live-1", title="Open elsewhere")
    armed = make_row(session_id="armed-1", title="Armed tab", armed=True)
    state = initial_state(Rows(desk=(live, armed), young=()))

    lines = render(state, now=1000)

    live_line = next(line for line in lines if "Open elsewhere" in line)
    armed_line = next(line for line in lines if "Armed tab" in line)
    assert live_line[2:].startswith("[=]")
    assert "Already open" in live_line
    assert armed_line[2:].startswith("[ ]"), "an armed row must not be pre-selected (D11)"
    assert "Waiting for Enter" in armed_line

    # ...but the user CAN still check it on -- selectable, not locked out.
    state, _ = apply_key(state, " ")  # cursor starts on `live`
    state, _ = apply_key(state, "DOWN")
    state, _ = apply_key(state, " ")  # toggle `armed` on
    armed_line_after = next(line for line in render(state, now=1000) if "Armed tab" in line)
    assert armed_line_after[2:].startswith("[x]")


def test_render_shows_young_bucket_heading_after_desk_rows() -> None:
    desk_row = make_row(session_id="desk-1", title="Desk row")
    young_row = make_row(session_id="young-1", title="Young row", young=True)
    state = initial_state(Rows(desk=(desk_row,), young=(young_row,)))

    lines = render(state, now=1000)
    text = "\n".join(lines)

    assert "STARTED SINCE THE LAST SNAPSHOT  (1 session)" in text
    assert text.index("Desk row") < text.index("STARTED SINCE THE LAST SNAPSHOT")
    assert text.index("STARTED SINCE THE LAST SNAPSHOT") < text.index("Young row")


def test_render_omits_young_heading_when_the_young_bucket_is_empty() -> None:
    state = initial_state(Rows(desk=(make_row(),), young=()))

    text = "\n".join(render(state, now=1000))

    assert "STARTED SINCE THE LAST SNAPSHOT" not in text


def test_render_includes_the_injected_header_lines_verbatim() -> None:
    state = initial_state(Rows(desk=(make_row(),), young=()))

    lines = render(
        state, now=1000, header=["Last successful snapshot: 5 minutes ago (08-24 10:00)"]
    )

    assert lines[0] == "Last successful snapshot: 5 minutes ago (08-24 10:00)"


def test_render_shows_the_filter_prompt_while_typing() -> None:
    row = make_row(session_id="a", title="Alpha")
    state = initial_state(Rows(desk=(row,), young=()))
    state = type_filter(state, "al")

    text = "\n".join(render(state, now=1000))

    assert "/al" in text


# --- run_picker: the impure loop, driven with a scripted key source -------


def test_run_picker_drives_the_loop_and_returns_the_final_outcome() -> None:
    row = make_row(session_id="a")
    out = StringIO()

    outcome = run_picker(
        Rows(desk=(row,), young=()),
        header=["freshness line"],
        key_reader=scripted_keys(["q"]),
        out=out,
        clock=lambda: 1000,
        clear_screen=False,
    )

    assert outcome == PickerOutcome(kind="quit")
    written = out.getvalue()
    assert "freshness line" in written
    assert KEY_LEGEND in written


def test_run_picker_renders_one_frame_per_key_and_stops_reading_after_the_outcome() -> None:
    """SAFETY-adjacent: `run_picker` must call `key_reader` exactly as many
    times as needed and never once more -- reading past the outcome-yielding
    key would, with a real `read_key_msvcrt`, block on a key nobody is
    going to press."""
    row = make_row(session_id="a")
    out = StringIO()
    calls = {"n": 0}

    def counting_reader() -> str:
        calls["n"] += 1
        keys = [" ", "ENTER"]
        return keys[calls["n"] - 1]

    outcome = run_picker(
        Rows(desk=(row,), young=()),
        key_reader=counting_reader,
        out=out,
        clock=lambda: 1000,
        clear_screen=False,
    )

    assert calls["n"] == 2
    assert outcome == PickerOutcome(kind="restore", selected_ids=frozenset())


def test_run_picker_clear_screen_writes_the_ansi_sequence_when_enabled() -> None:
    row = make_row(session_id="a")
    out = StringIO()

    run_picker(
        Rows(desk=(row,), young=()),
        key_reader=scripted_keys(["q"]),
        out=out,
        clock=lambda: 1000,
        clear_screen=True,
    )

    assert "\x1b[2J\x1b[H" in out.getvalue()


def test_run_picker_initial_selected_seeds_the_state_instead_of_the_default() -> None:
    """H3/F3: `run_picker`'s `initial_selected` is the seam
    `restore_interactive` uses to carry a selection across an `[n]` switch
    -- it must reach `initial_state` rather than every entry re-deriving
    "everything restorable"."""
    a = make_row(session_id="a")
    b = make_row(session_id="b")
    out = StringIO()

    outcome = run_picker(
        Rows(desk=(a, b), young=()),
        key_reader=scripted_keys(["ENTER"]),
        out=out,
        clock=lambda: 1000,
        clear_screen=False,
        initial_selected=frozenset({"b"}),  # NOT the default (both selectable)
    )

    assert outcome == PickerOutcome(kind="restore", selected_ids=frozenset({"b"}))


def test_run_picker_recomputes_a_callable_header_on_every_frame() -> None:
    """Review finding L3 [LOW]: `restore_interactive` used to compute the
    header ONCE per `run_picker` call (i.e. once per `[n]` switch, not once
    per keystroke) and hand this loop a frozen list -- sit in the picker
    for a while and the row ages tick upward under a header still claiming
    "just now". `header` may now be a zero-argument callable, re-invoked on
    every frame; a plain sequence (every other test in this file) still
    works unchanged via the same parameter."""
    row = make_row(session_id="a")
    out = StringIO()
    calls = {"n": 0}

    def header_fn() -> list[str]:
        calls["n"] += 1
        return [f"header call {calls['n']}"]

    run_picker(
        Rows(desk=(row,), young=()),
        header=header_fn,
        key_reader=scripted_keys(["DOWN", "q"]),
        out=out,
        clock=lambda: 1000,
        clear_screen=False,
    )

    written = out.getvalue()
    assert "header call 1" in written
    assert "header call 2" in written


def test_run_picker_never_calls_the_key_reader_before_rendering_the_first_frame() -> None:
    """A scripted reader that raises on its very first call still lets the
    caller see one rendered frame written to `out` before the exception --
    proves the loop renders THEN reads, not the other way round (drawing a
    frame the user never sees before asking for a key would be backwards).
    """
    row = make_row(session_id="a")
    out = StringIO()

    def raise_immediately() -> str:
        raise AssertionError("no keys scripted")

    with pytest.raises(AssertionError):
        run_picker(
            Rows(desk=(row,), young=()),
            key_reader=raise_immediately,
            out=out,
            clock=lambda: 1000,
            clear_screen=False,
        )

    # Review finding F8/M5/L6 [MEDIUM/LOW]: this used to be
    # `assert "Some title" in out.getvalue() or "a" in out.getvalue()`.
    # `KEY_LEGEND` contains the letter "a" (in "space", "restore", "[a]
    # none"), and the legend is appended to every frame unconditionally --
    # so the `or` branch was true even for a picker with ZERO rows, which
    # makes the half of the assertion the docstring is actually about (that
    # a ROW was drawn) unfalsifiable. F8 authorises this fix.
    assert "Some title" in out.getvalue()  # a frame WAS drawn


def test_colors_preserve_plain_text_and_repository_identity() -> None:
    import re
    from termsync.picker import _repository_color

    rows = Rows((make_row(title="Design", cwd=r"D:\Projects\termsync"),), ())
    state = initial_state(rows)
    plain = "\n".join(render(state, now=1000))
    colored = "\n".join(render(state, now=1000, color=True))
    assert "\x1b[" not in plain
    assert "\x1b[38;5;209mClaude" in colored
    # Titles stay bold even when the cursor moves to a different session.
    from termsync.picker import _render_row
    unfocused = _render_row(rows.desk[0], False, True, 1000, color=True, width=80)
    assert "\x1b[1mDesign" in unfocused
    assert "\x1b[2m            | " in unfocused
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    assert _repository_color(r"D:\Projects\termsync") == _repository_color("d:/projects/TERMSYNC/")
    codex = initial_state(Rows((replace(rows.desk[0], agent="codex"),), ()))
    assert "\x1b[38;5;81mCodex" in "\n".join(render(codex, now=1000, color=True))


def test_compact_rows_keep_metadata_and_selection() -> None:
    row = replace(make_row(title="A long session title " * 4), prompt_count=None)
    state = initial_state(Rows((row,), ()))
    frame = "\n".join(render(state, now=1000, width=80))
    lines = frame.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("> [x]"))
    assert len(lines[index]) <= 80
    assert "Prompts: --" in lines[index + 1]
    assert "Last used: --" in lines[index + 1]
    assert lines[index + 1].startswith("            | ")
    assert "Ready to restore" in lines[index + 1]
    assert row.title in frame  # full title is available in the focused detail
    assert apply_key(state, "ENTER")[1].selected_ids == frozenset({row.session_id})


def test_no_color_environment_disables_color_for_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    out = Terminal()
    monkeypatch.setenv("NO_COLOR", "1")
    run_picker(Rows((make_row(),), ()), key_reader=scripted_keys(["q"]),
               out=out, clock=lambda: 1000, clear_screen=False)
    assert "\x1b[" not in out.getvalue()
