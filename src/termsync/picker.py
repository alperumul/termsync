"""Interactive restore selection with a pure state machine and renderer.

initial_state, apply_key, visible_rows, and render perform no I/O.
run_picker drives them with injected key input and output; read_key_msvcrt
is the blocking Windows console adapter and must never run in tests.
Shared display helpers live here so the dependency remains cli -> picker.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
import os
import shutil
from pathlib import PureWindowsPath
from typing import IO, Literal

from termsync import health
from termsync.rows import Row, Rows

# Normal-mode commands; filtering has its own legend.
KEY_LEGEND = "[space] toggle  [/] filter  [enter] restore selected  [a] none  [n] newest  [q] quit"

FILTER_LEGEND = "[esc] cancel filter  [enter] apply and stop typing  [backspace] erase"

OutcomeKind = Literal["restore", "quit", "switch_layout"]


@dataclass(frozen=True)
class PickerOutcome:
    """A restore selection, quit, or layout-switch request.

    The caller performs restore and layout reads. A switch carries the current
    selection so rebuilding the view does not undo the user's choices.
    """

    kind: OutcomeKind
    selected_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PickerState:
    """Immutable state between keystrokes.

    Rows follow captured window/tab order, then unknown, archived, and young
    sections. The cursor indexes visible_rows, not rows; headers are not rows.
    An empty or filtered-out list keeps cursor zero without accessing a row.
    """

    rows: tuple[Row, ...]
    selected: frozenset[str]
    filter_text: str
    cursor: int
    filtering: bool
    # One-shot explanation, cleared by the next key of any kind.
    message: str | None = None


def initial_state(rows: Rows, carried_selection: frozenset[str] | None = None) -> PickerState:
    """Order rows and select available sessions except armed and archived ones.

    Armed rows need explicit selection to avoid duplicating tabs waiting for
    Enter. A carried selection overrides defaults and is intersected with
    selectable rows, preserving deselection across layout switches.
    """
    all_rows = tuple(
        row for _, group in row_sections(tuple(rows.desk) + tuple(rows.young)) for row in group
    )
    if carried_selection is None:
        selected = frozenset(
            row.session_id for row in all_rows
            if row.selectable and not row.armed and not row.archived
        )
    else:
        selectable_ids = frozenset(row.session_id for row in all_rows if row.selectable)
        selected = carried_selection & selectable_ids
    return PickerState(
        rows=all_rows,
        selected=selected,
        filter_text="",
        cursor=0,
        filtering=False,
        message=None,
    )


def visible_rows(state: PickerState) -> tuple[Row, ...]:
    """Filter titles and directories case-insensitively; keep selection unchanged."""
    if not state.filter_text:
        return state.rows
    needle = state.filter_text.casefold()
    return tuple(
        row for row in state.rows if needle in row.title.casefold() or needle in row.cwd.casefold()
    )


def apply_key(state: PickerState, key: str) -> tuple[PickerState, PickerOutcome | None]:
    """Apply one symbolic key without I/O or clock reads.

    Clear the previous one-shot message before dispatch. CTRL_C quits in either
    mode when delivered; console input does not guarantee delivery while blocked.
    """
    if state.message is not None:
        state = replace(state, message=None)
    if key == "CTRL_C":
        return state, PickerOutcome(kind="quit")
    if state.filtering:
        return _apply_key_filtering(state, key), None
    return _apply_key_normal(state, key)


def _apply_key_filtering(state: PickerState, key: str) -> PickerState:
    """Treat printable keys as filter text, including normal-mode commands.

    Enter keeps the filter and ends typing; Escape clears it and ends typing.
    Arrow keys move through matches without becoming filter text.
    """
    if key == "ENTER":
        return replace(state, filtering=False)
    if key == "ESC":
        return _reset_cursor(replace(state, filtering=False, filter_text=""))
    if key == "BACKSPACE":
        return _reset_cursor(replace(state, filter_text=state.filter_text[:-1]))
    if key in ("UP", "DOWN"):
        return _move_cursor(state, -1 if key == "UP" else 1)
    if len(key) == 1 and key.isprintable():
        return _reset_cursor(replace(state, filter_text=state.filter_text + key))
    return state  # an unrecognised control code while typing: ignore it (message already cleared)


def _apply_key_normal(state: PickerState, key: str) -> tuple[PickerState, PickerOutcome | None]:
    """Keys when not typing a filter -- §2's whole key legend."""
    if key == "/":
        return replace(state, filtering=True), None
    if key == " ":
        return _toggle_cursor_row(state), None
    if key == "a":
        # Clear all selections, including rows hidden by the filter.
        return replace(state, selected=frozenset()), None
    if key in ("UP", "k"):
        return _move_cursor(state, -1), None
    if key in ("DOWN", "j"):
        return _move_cursor(state, 1), None
    if key == "n":
        # Carry selection so changing layouts does not undo deselection.
        return state, PickerOutcome(kind="switch_layout", selected_ids=state.selected)
    if key == "q":
        return state, PickerOutcome(kind="quit")
    if key == "ENTER":
        return state, PickerOutcome(kind="restore", selected_ids=state.selected)
    return state, None  # unrecognised key: no-op, never raises (message already cleared)


def _reset_cursor(state: PickerState) -> PickerState:
    """Reset to the first visible row after a filter changes its membership."""
    return replace(state, cursor=0)


def _move_cursor(state: PickerState, delta: int) -> PickerState:
    """Move through visible rows without wrapping; empty lists keep cursor zero."""
    visible = visible_rows(state)
    if not visible:
        return replace(state, cursor=0)
    new_cursor = state.cursor + delta
    new_cursor = max(0, min(new_cursor, len(visible) - 1))
    return replace(state, cursor=new_cursor)


def _toggle_cursor_row(state: PickerState) -> PickerState:
    """Toggle the visible row, or explain why an unavailable row cannot be selected."""
    visible = visible_rows(state)
    if not visible or not (0 <= state.cursor < len(visible)):
        return state  # message already cleared by apply_key
    row = visible[state.cursor]
    if not row.selectable:
        return replace(state, message=_unselectable_reason(row))
    if row.session_id in state.selected:
        selected = state.selected - {row.session_id}
    else:
        selected = state.selected | {row.session_id}
    return replace(state, selected=selected)


def _unselectable_reason(row: Row) -> str:
    """Why `[space]` just did nothing on this row -- checked in the same
    priority order as `Row.selectable`'s own definition (`not live` first,
    then the glob, then the cwd check), and using the same "cwd unknown"
    vs "cwd gone" distinction `cli._render_row` already makes (review
    finding L2) rather than collapsing both into one misleading reason.
    """
    if row.live:
        return f'"{row.title}" is already open -- nothing to restore'
    if row.unavailable_reason:
        return f'"{row.title}" cannot be selected -- {row.unavailable_reason}'
    if row.session_file is None:
        return f'"{row.title}" cannot be selected -- its session file was deleted'
    if not row.cwd:
        return f'"{row.title}" cannot be selected -- its working directory is unknown'
    if not row.cwd_exists:
        return f'"{row.title}" cannot be selected -- its working directory is gone'
    # defensive fallback; Row.selectable's own definition is exhaustive above
    return f'"{row.title}" cannot be selected'


# --- rendering ---------------------------------------------------------


def row_sections(
    rows: Sequence[Row], *, filter_text: str = "",
) -> tuple[tuple[str | None, tuple[Row, ...]], ...]:
    """Captured placement, not a preview of post-preflight destinations.

    Group before filtering so source ordinals and single-window presentation
    stay stable. Counts describe visible sessions only; plain tabs are absent.
    Unknown, archived and young rows keep their relative order and eligibility.
    """
    known: dict[int, list[Row]] = {}
    unknown: list[Row] = []
    archived: list[Row] = []
    young: list[Row] = []
    for row in rows:
        if row.young:
            young.append(row)
        elif row.archived:
            archived.append(row)
        elif (row.agent == "claude" and not row.armed
              and type(row.window_ordinal) is int and row.window_ordinal > 0
              and type(row.tab_index) is int and row.tab_index >= 0):
            known.setdefault(row.window_ordinal, []).append(row)
        else:
            unknown.append(row)
    grouped = len(known) > 1 or bool(unknown or archived or young)
    groups = [
        (f"WINDOW {ordinal}" if grouped else None,
         sorted(known[ordinal], key=lambda row: row.tab_index))
        for ordinal in sorted(known)
    ]
    groups += [
        ("WINDOW UNKNOWN", unknown),
        ("ARCHIVED CODEX SESSIONS (select explicitly to unarchive)", archived),
        ("STARTED SINCE THE LAST SNAPSHOT", young),
    ]
    needle = filter_text.casefold()
    sections = []
    for title, group in groups:
        visible = tuple(row for row in group
                        if needle in row.title.casefold() or needle in row.cwd.casefold())
        if not visible:
            continue
        if title is not None:
            noun = "session" if len(visible) == 1 else "sessions"
            title += f"  ({len(visible)} {noun})"
            if group is archived or group is young:
                title += " -- window unknown"
        sections.append((title, visible))
    return tuple(sections)


def prompt_and_age_text(row: Row, now: int) -> tuple[str, str]:
    """The `<n> prompts, <age> ago` half of a row -- shared by every marker
    that has anything to say about it. Moved here from `cli.py` (S10's
    private `_prompt_and_age_text`) because this module is its second
    caller: per this project's dedup rule ("extract at the second caller,
    never the first" -- `patterns_dedup_reuse_discipline`), that is exactly
    the moment to promote it, not before. `cli._render_row` (`--dry-run`,
    S10) now calls this same function instead of carrying its own copy.
    """
    if row.prompt_count is None:
        prompt_str = "no prompt history"
    elif row.prompt_count == 1:
        prompt_str = "1 prompt"
    else:
        prompt_str = f"{row.prompt_count} prompts"

    if row.last_prompt_at is None:
        age_str = "age unknown"
    else:
        age_str = health.relative_age_phrase(now - row.last_prompt_at)

    return prompt_str, age_str


# Color is an additional cue; labels and selection markers remain readable without it.
_AGENT_COLORS = {"codex": "38;5;81", "claude": "38;5;209"}
_REPO_COLORS = (110, 114, 141, 150, 173, 180, 183, 186, 210, 215, 218, 223)


def _paint(text: str, style: str, color: bool) -> str:
    return f"\x1b[{style}m{text}\x1b[0m" if color else text


def _repository_color(cwd: str) -> str:
    # Unlike hash(), SHA-256 is stable across Python processes. Windows paths
    # differing only in case or separators identify the same working folder.
    key = str(PureWindowsPath(cwd)).casefold().encode("utf-8")
    index = int.from_bytes(sha256(key).digest()[:4], "big") % len(_REPO_COLORS)
    return f"38;5;{_REPO_COLORS[index]}"


def _truncate_pad(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) > width:
        return text[:max(width - 3, 0)].rstrip().ljust(max(width - 3, 0)) + "." * min(3, width)
    return text.ljust(width)


def _last_used_text(row: Row) -> str:
    if row.last_prompt_at is None:
        return "Unknown"
    try:
        return datetime.fromtimestamp(row.last_prompt_at / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "Unknown"


def _row_status(row: Row) -> str:
    if row.live:
        return "Already open"
    if not row.selectable:
        if row.unavailable_reason:
            return row.unavailable_reason
        if row.session_file is None:
            return "session file deleted"
        return "cwd unknown" if not row.cwd else "cwd gone"
    if row.armed:
        return "Waiting for Enter"
    if row.archived:
        return "Unarchive on restore"
    return "Ready to restore"


def _render_row(
    row: Row, is_cursor: bool, is_selected: bool, now: int,
    *, color: bool = False, width: int = 120,
) -> str:
    """Aligned table on wide terminals; two lines per session below 120 columns."""
    cursor = "> " if is_cursor else "  "
    box = "[=]" if row.live else "[-]" if not row.selectable else "[x]" if is_selected else "[ ]"
    marker = _paint(box, "1;36" if is_selected else "2", color)
    agent = _paint(f"{row.agent.capitalize():6}", _AGENT_COLORS.get(row.agent, "37"), color)
    leaf = PureWindowsPath(row.cwd).name if row.cwd else "Unknown"
    repo = _paint(_truncate_pad(leaf, 18), _repository_color(row.cwd), color)
    prompts = prompt_and_age_text(row, now)[0] if row.prompt_count is not None else "Unavailable"
    used = _last_used_text(row)
    status = _row_status(row)
    status = _paint(status, "33" if row.armed or (not row.selectable and not row.live)
                    else "2", color)
    if width < 120:
        title = _paint(
            _truncate_pad(row.title, max(12, width - 36)), "1", color,
        )
        prompt_label = prompts if row.prompt_count is not None else "Prompts: --"
        used_label = used if used != "Unknown" else "--"
        metadata = _paint(f"            | {prompt_label}  |  Last used: {used_label}  |  ",
                          "2", color)
        return (f"{cursor}{marker} {agent}  {title}  {repo}\n"
                f"{metadata}{status}")
    title = _paint(_truncate_pad(row.title, min(48, width - 90)), "1", color)
    metadata = _paint(f"{prompts:12}  {used:16}", "2", color)
    return f"{cursor}{marker} {agent}  {title}  {repo}  {metadata}  {status}"


def render(
    state: PickerState, now: int, header: Sequence[str] = (),
    *, color: bool = False, width: int = 120,
) -> list[str]:
    """Render one frame using the supplied timestamp and header.

    Use the same section order as cursor navigation, recount sections after
    filtering, and distinguish an empty desk from an empty filtered result.
    The status, selected count, and mode-specific key legend follow the rows.
    """
    lines = list(header)

    if state.filtering:
        lines.append(f"/{state.filter_text}")
    elif state.filter_text:
        lines.append(f"filter: {state.filter_text}  (press / to change it)")

    visible = visible_rows(state)
    desk_count = sum(1 for row in visible if not row.young)
    young_count = len(visible) - desk_count

    if desk_count == 0 and young_count == 0:
        lines.append(
            f'No rows match "{state.filter_text}".'
            if state.filter_text
            else "Nothing to restore -- this desk holds no sessions."
        )
    elif desk_count == 0:
        lines.append(
            f'No rows on the last desk match "{state.filter_text}".'
            if state.filter_text
            else "No rows on the last desk."
        )

    if visible:
        if width >= 120:
            columns = (f"      {'Agent':6}  {'Session':{min(48, width - 90)}}  "
                       f"{'Repository':18}  {'Prompts':12}  {'Last used, local':16}  Status")
            lines += ["", _paint(columns, "2", color)]
        else:
            columns = (f"      {'Agent':6}  {'Session':{max(12, width - 36)}}  Repository")
            lines += ["", _paint(columns, "2", color)]

    index = 0
    for title, group in row_sections(state.rows, filter_text=state.filter_text):
        if title is not None:
            lines += ["", _paint(title, "1", color)]
        for row in group:
            lines.append(_render_row(
                row, index == state.cursor, row.session_id in state.selected, now,
                color=color, width=width,
            ))
            index += 1

    if visible:
        focused = visible[min(state.cursor, len(visible) - 1)]
        _, age = prompt_and_age_text(focused, now)
        lines += [
            "", _paint("  " + " Focused session ".center(max(20, width - 4), "-"), "2", color),
            _paint(f"  {focused.title}", "1", color),
            _paint(f"  Folder: {focused.cwd or 'Unknown'}", "2", color),
            _paint(f"  Last used: {_last_used_text(focused)} (local time) | {age}", "2", color),
        ]
        if focused.prompt_count is None:
            lines.append("  Prompt count is unavailable from this session's history.")
        if focused.armed:
            lines.append(
                "  Waiting for Enter in its existing tab; select only to reopen a closed tab."
            )
        if focused.archived:
            lines.append("  Archived session; restoring it will unarchive it.")

    if state.message:
        lines.append(f"!  {state.message}")

    # Review finding F6/M3 [MEDIUM]: filtering deliberately never touches
    # the selection (that decision is correct and stays -- see
    # `visible_rows`'s own docstring), but nothing on the screen used to say
    # how many rows Enter would actually restore, so a ten-row selection
    # narrowed to one visible row by a filter looked, on screen, like a
    # one-row selection. Always shown (cheap, one line); names the hidden
    # count only when a filter is actually hiding a selected row.
    selected_count = sum(1 for row in state.rows if row.session_id in state.selected)
    visible_selected_count = sum(1 for row in visible if row.session_id in state.selected)
    hidden_selected = selected_count - visible_selected_count
    count_line = f"{selected_count} selected"
    if hidden_selected:
        count_line += f" ({hidden_selected} not shown by the current filter)"
    lines.append(count_line)

    lines.append("")
    lines.append(FILTER_LEGEND if state.filtering else KEY_LEGEND)
    return lines


# --- the impure shell ----------------------------------------------------

#: Clear-screen + cursor-home, understood by Windows Terminal's VT
#: processing (the only host this tool targets -- §3). Kept as a module
#: constant, not inlined, so `run_picker(..., clear_screen=False)` in a
#: test can skip it without guessing what string to search for.
_CLEAR_SCREEN = "\x1b[2J\x1b[H"


def run_picker(
    rows: Rows,
    *,
    header: Sequence[str] | Callable[[], Sequence[str]] = (),
    key_reader: Callable[[], str],
    out: IO[str],
    clock: Callable[[], int],
    clear_screen: bool = True,
    initial_selected: frozenset[str] | None = None,
) -> PickerOutcome:
    """Drive the state machine with the supplied key reader and output stream.

    Tests must use a finite scripted key source, never read_key_msvcrt. A callable
    header is refreshed every frame so freshness and row ages stay consistent.
    initial_selected carries the surviving selection across a layout switch.
    """
    state = initial_state(rows, carried_selection=initial_selected)
    header_fn: Callable[[], Sequence[str]] = header if callable(header) else (lambda: header)
    color = bool(getattr(out, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
    while True:
        frame = render(state, clock(), header=header_fn(), color=color,
                       width=shutil.get_terminal_size(fallback=(120, 30)).columns)
        if clear_screen:
            out.write(_CLEAR_SCREEN)
        out.write("\n".join(frame) + "\n")
        flush = getattr(out, "flush", None)
        if flush is not None:
            flush()

        key = key_reader()
        state, outcome = apply_key(state, key)
        if outcome is not None:
            return outcome


def read_key_msvcrt() -> str:
    """Block for one logical Windows console key; never call from tests.

    Consume both parts of extended keys before mapping their scan codes. A
    literal Ctrl+C maps to CTRL_C, but the console may suppress it while the
    read blocks; the caller must check console availability before entering.
    """
    import msvcrt  # local: this module must stay importable on a non-Windows test runner

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        scan = msvcrt.getwch()
        return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(scan, "")
    if ch == "\r":
        return "ENTER"
    if ch == "\x08":
        return "BACKSPACE"
    if ch == "\x1b":
        return "ESC"
    if ch == "\x03":
        return "CTRL_C"
    return ch
