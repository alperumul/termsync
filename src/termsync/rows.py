"""The four-source join behind §2's picker row (§13.2, work unit S9).

§2's row is `[x] <title>  <cwd-leaf>  <n> prompts  <age> ago`, and its fields
come from four different places: title and cwd from the **layout**, prompt
count and age from **history.jsonl**, existence from a **glob**, and
already-live from **`-Emit` ∪ `pending.json`**. Revision 3 of the design had
no owner for that join -- `picker.py` was described as presentation and
`restore.py` was already carrying four responsibilities -- so the join would
have landed by default in the riskiest module in the plan, making it also the
widest.

`build_rows` is pure: no filesystem, no clock, no subprocess. Everything that
touches the disk is injected, which is what makes a fixture-driven picker real
rather than nominal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from termsync.history import HistoryEntry, sessions_active_after
from termsync.model import Layout, Session
from termsync.restore_codex import CodexRestoreInfo, lookup_codex, validate_resume_args


@dataclass(frozen=True)
class Row:
    """One line of §2's screen, with everything the renderer needs already joined.

    **`live` (added post-freeze, per D10 / review finding F1/H1).** The `Row`
    contract was frozen so S9 and S10 could be built in parallel; that work is
    now done and committed, and S5/S6 (the next callers) do not exist yet, so
    changing the contract now is a migration of zero live callers -- doing it
    later, after S5/S6 exist, would be a real migration. `live` is `True` when
    the session is already live (registry) at restore time; it is
    deliberately independent of `pending.json`/`armed` -- see `_desk_row`'s
    docstring for why an armed session is never also `live`. A live row is
    never `selectable` (you cannot restore what is already open), but it is
    never REMOVED from `desk` either: see `build_rows`'s docstring for why
    deleting it was the defect."""

    session_id: str
    title: str
    cwd: str
    prompt_count: int | None  # None: history has no entry for this session
    last_prompt_at: int | None  # epoch ms UTC; None: unknown. Formatting is display-layer
    session_file: Path | None  # the glob's result; None means deleted -> greyed
    cwd_exists: bool  # §7 step 5
    armed: bool  # opened by a previous restore, never resumed (§6, §7 step 7)
    live: bool  # already live (registry) at restore time (D10) -- never also armed
    young: bool  # "started since the last snapshot" (§7 step 3)
    selectable: bool  # not live, and session_file is not None and cwd_exists (§7 steps 4-5)
    agent: str = "claude"
    archived: bool = False
    resume_args: tuple[str, ...] = ()
    unavailable_reason: str | None = None
    window_ordinal: int | None = None
    tab_index: int | None = None


@dataclass(frozen=True)
class Rows:
    """§2's two lists. `young` is the "STARTED SINCE THE LAST SNAPSHOT" bucket."""

    desk: tuple[Row, ...]
    young: tuple[Row, ...]


#: session id -> the session's `.jsonl` file, or None when the glob finds nothing.
#: ALWAYS a glob of `projects/*/<sessionId>.jsonl` -- never a slug derivation (§7 step 4).
SessionFileLookup = Callable[[str], "Path | None"]

#: absolute cwd -> does that directory still exist (§7 step 5).
CwdExists = Callable[[str], bool]
CodexLookup = Callable[[str], CodexRestoreInfo]

#: session id -> its resolved title (e.g. from `build_ai_title_map`). `None`
#: when the caller supplied no batch at all -- every young row then falls
#: back to its last-prompt title. Named so `cli.RowBuilder` (review finding
#: M6/F8) can reference the real 7th parameter of `build_rows` instead of
#: silently omitting it.
AiTitles = Mapping[str, str] | None

# A young row's fallback title (no ai-title available) is the last prompt text,
# collapsed to its first line and trimmed to a sensible single-line length for a
# picker row. Kept as plain ASCII (a "..." marker, not a Unicode ellipsis) because
# this string can end up in a pytest failure diff printed to this machine's cp1252
# console -- see the SAFETY note this module was written under.
_FALLBACK_TITLE_MAX_LEN = 60


def _fallback_title(last_prompt: str) -> str:
    """A young row's title when no `ai-title` is available for it.

    Roughly four in five young rows use this path (§ "Young-row titles"
    decision: ai-title coverage is ~17%, 20/120 measured). `last_prompt` is
    collapsed to its first line -- a prompt can itself be multi-line -- and
    truncated so it fits a single picker row.
    """
    first_line = last_prompt.strip().splitlines()[0] if last_prompt.strip() else ""
    if len(first_line) > _FALLBACK_TITLE_MAX_LEN:
        return first_line[: _FALLBACK_TITLE_MAX_LEN - 3].rstrip() + "..."
    return first_line


def _young_title(session_id: str, last_prompt: str, ai_titles: Mapping[str, str] | None) -> str:
    """§ "Young-row titles" decision: `ai-title` when supplied for this session,
    else the last-prompt fallback. `ai_titles` is a pre-computed map the caller
    supplies -- `build_rows` never reads a session file itself (see
    `read_ai_title` below for why that read is impure and kept out of here)."""
    if ai_titles is not None:
        ai_title = ai_titles.get(session_id)
        if ai_title:
            return ai_title
    return _fallback_title(last_prompt)


def _desk_row(
    session: Session,
    history: Mapping[str, HistoryEntry],
    session_file: SessionFileLookup,
    cwd_exists: CwdExists,
    live_session_ids: frozenset[str | tuple[str, str]],
    codex_lookup: CodexLookup = lookup_codex,
) -> Row:
    """One row of the desk list (§2's main list) for every session in the
    target layout -- D10 (review finding F1/H1): NO session is dropped from
    this list any more, live or not.

    `live` -- session already live (registry) right now -- takes its value
    from `live_session_ids`, EXCEPT an armed session is never marked live:
    `session.armed` is sourced from the layout alone (there is no second,
    independent source to reconcile it against -- see `build_rows`'s
    docstring), and an armed tab hosts no Claude process, so it structurally
    cannot be a member of the registry-derived live set at the moment it was
    captured. If its id nonetheless appears in `live_session_ids` -- which it
    always will in production, because §7 step 2's union folds
    `pending.json`'s ids into the very set `dry_run` hands this function --
    `armed` wins: the row still renders `~`, not `[=]`.
    """
    entry = history.get(session.session_id) if session.agent == "claude" else None
    info = codex_lookup(session.session_id) if session.agent == "codex" else None
    file = session_file(session.session_id) if info is None else info.path
    reason = info.reason if info else None
    if session.agent == "codex":
        try:
            validate_resume_args(session.resume_args)
        except ValueError as exc:
            reason = str(exc)
    exists = cwd_exists(session.cwd)
    live = (not session.armed) and (
        (session.agent, session.session_id) in live_session_ids
        or (session.agent == "claude" and session.session_id in live_session_ids)
    )
    return Row(
        session_id=session.session_id,
        title=session.title,
        cwd=session.cwd,
        prompt_count=entry.prompt_count if entry is not None else None,
        last_prompt_at=(session.last_prompt_at if session.agent == "codex"
                        else entry.last_prompt_at if entry is not None else None),
        session_file=file,
        cwd_exists=exists,
        armed=session.armed,
        live=live,
        young=False,
        selectable=(not live) and file is not None and exists and not reason,
        agent=session.agent,
        archived=info.archived if info else session.archived,
        resume_args=session.resume_args,
        unavailable_reason=reason,
        window_ordinal=session.window_ordinal,
        tab_index=session.tab_index,
    )


def _young_row(
    entry: HistoryEntry,
    session_file: SessionFileLookup,
    cwd_exists: CwdExists,
    ai_titles: Mapping[str, str] | None,
) -> Row:
    """One row of the "STARTED SINCE THE LAST SNAPSHOT" bucket (§7 step 3).

    A young session was never captured, so its `cwd` has no layout source --
    it comes from `HistoryEntry.cwd` (the `project` field of the record that
    won as "last prompt" -- see `history.py`), which can be `None` when the
    real `history.jsonl` carries a torn/blank `project` on that record. A
    session with no known cwd cannot be checked for existence and cannot be
    restored into the right directory (§7 step 7), so it is unselectable
    rather than guessed at.
    """
    cwd = entry.cwd or ""
    file = session_file(entry.session_id)
    exists = cwd_exists(cwd) if cwd else False
    return Row(
        session_id=entry.session_id,
        title=_young_title(entry.session_id, entry.last_prompt, ai_titles),
        cwd=cwd,
        prompt_count=entry.prompt_count,
        last_prompt_at=entry.last_prompt_at,
        session_file=file,
        cwd_exists=exists,
        armed=False,  # never captured, so never carried through pending.json (§6)
        live=False,  # a currently-live young session is filtered upstream -- see build_rows
        young=True,
        selectable=file is not None and exists,
    )


def build_rows(
    layout: Layout,
    history: Mapping[str, HistoryEntry],
    live_session_ids: frozenset[str | tuple[str, str]],
    bg_session_ids: frozenset[str],
    session_file: SessionFileLookup,
    cwd_exists: CwdExists,
    ai_titles: Mapping[str, str] | None = None,
    *,
    codex_lookup: CodexLookup = lookup_codex,
) -> Rows:
    """Join a layout, history, a glob and the live set into §2's two lists.

    `live_session_ids` is the already-live set as defined by §7 step 2: the
    UNION of the registry and `pending.json`, computed by the caller (this
    function does not know how to compute it -- it has no `-Emit` and no
    `Pending` in its signature, deliberately, so it stays a pure join).

    **The desk list -- D10 (review findings F1/H1), a deliberate, recorded
    spec change.** Every `layout.sessions` entry becomes a row; NONE are
    dropped for being already-live any more. The original design used
    `live_session_ids` as a delete-list here, which reads correctly as a
    RESTORE filter (you cannot open a tab for a session that already has
    one) but is composed with `dry_run`'s union of the registry AND
    `pending.json` -- so an armed-but-never-resumed session, whose whole
    point is that it hosts no process, was excluded on exactly the same
    line as a genuinely live one. Demonstrated live during review: two
    armed tabs, zero `claude.exe` processes anywhere, and the tool printed
    "already open" while naming neither session id -- for the entire
    24-hour window §6's `pending.json` expiry gives an armed tab to be
    resumed in. `--dry-run` opens nothing, so the delete-list's own
    justification (§7 step 2: "a registry-only check would let a second
    `termsync restore` duplicate every armed tab") never reaches it either.

    The fix: every row stays, and `_desk_row` marks `live` instead (see its
    own docstring for why an armed session is never also marked live).
    Excluding an already-live session from a path that actually SPAWNS a
    tab -- S5's picker pre-selection, S6's restore -- is correct and still
    belongs there; `live_session_ids` keeps flowing through this function
    unchanged so that code can be built without a second join.

    Title, cwd and `armed` come straight from the layout (`capture.py` is
    where `armed` gets set from the `pending.json` union, per §5 step 9 --
    there is no second source to consult here). Prompt count and last-prompt
    time come from `history`; a session with no history entry gets `None`
    for both, which is an ordinary state (§2 renders it as "--"), not an
    error.

    **The young list** -- sessions from `history.sessions_active_after`
    (prompts after `layout.captured_at`, `bg_session_ids` excluded so a
    background job is never offered as a tab that never existed), minus
    anything already in the desk list or already live. Unlike the desk
    list, a currently-live young session is still filtered OUT entirely
    (not marked) -- it was never captured, so there is nothing in the
    layout for it to "stay on"; showing it as a live row here would need
    the same title/cwd sourcing the desk list gets from the layout, which
    a young session structurally does not have. `young=True` always;
    `armed` and `live` are always `False` (a young session was never
    captured, so it can never have been carried into `pending.json`, and if
    it were currently live it would already be excluded above).

    Both lists apply the same glob / cwd rules: `selectable` is
    `(not live) and session_file is not None and cwd_exists` (§7 steps 4-5,
    D10).
    """
    desk_rows = tuple(
        _desk_row(session, history, session_file, cwd_exists, live_session_ids, codex_lookup)
        for session in layout.sessions
    )

    excluded_ids = {row.session_id for row in desk_rows} | {
        key[1] if isinstance(key, tuple) else key for key in live_session_ids
        if not isinstance(key, tuple) or key[0] == "claude"
    }
    active = sessions_active_after(dict(history), layout.captured_at, bg_session_ids)
    young_entries = sorted(
        (entry for entry in active if entry.session_id not in excluded_ids),
        key=lambda e: (-e.last_prompt_at, e.session_id),
    )
    young_rows = tuple(
        _young_row(entry, session_file, cwd_exists, ai_titles) for entry in young_entries
    )

    # Preserve order within each section. Archived rows form an explicit opt-in section.
    desk_rows = tuple(sorted(desk_rows, key=lambda row: row.archived))
    return Rows(desk=desk_rows, young=young_rows)


# ---------------------------------------------------------------------------
# Impure helpers. `build_rows` above never calls any of these -- it only
# consumes their *results* via the `session_file` / `ai_titles` parameters it
# is injected. Keeping the impure boundary here, clearly marked, is what lets
# `test_rows.py` exercise the whole join with fixtures only, no Windows and
# no real `~/.claude`.
# ---------------------------------------------------------------------------


def read_ai_title(path: Path, max_lines: int = 40) -> str | None:
    """IMPURE: read at most the first `max_lines` lines of a session `.jsonl`
    file looking for its `ai-title` record, e.g.::

        {"type": "ai-title", "aiTitle": "...", "sessionId": "..."}

    Returns the `aiTitle` string, or `None` if the file has no `ai-title`
    record within the line budget (or cannot be opened at all).

    **Why a bounded read, against §3.5's own warning.** §3.5 says reading
    `ai-title` "means scanning a file that can reach 22 MB" and gives that as
    the reason `desk` stores the console title at capture instead of reading
    `ai-title` for the desk list. That reasoning is about reading a file to
    its *end* looking for something that might not be there. It is not an
    argument against a *bounded* read for the young bucket, which has no
    console-title source at all (the session was never captured).

    Measured directly for this purpose: `ai-title` records sit near the head
    of a session file when present at all --

        checked 120 files in 1.37s; ai-title found in 20
        line positions of ai-title (first 12): [15, 16, 17, 16, 16, 16, 17, 19, 16, 0, 15, 16]

    -- so a read bounded to the first ~40 lines finds it in every observed
    case at essentially zero cost, and the 22 MB figure never applies: that
    number is the cost of reading a file to the end, and this function never
    does that. Coverage (~17%, 20/120) matches §3.5's own 107/552 -- most
    young rows will NOT have an `ai-title` and fall back to the last-prompt
    title in `rows.py`, which is the expected, documented common case, not a
    defect in this function.

    The next reader who sees §3.5's "22 MB" warning and "fixes" this into a
    full-file scan would be reintroducing the exact cost this function exists
    to avoid -- `test_read_ai_title_stops_after_its_line_budget` in
    `test_rows.py` fails if that happens, on a fixture file whose `ai-title`
    line sits past `max_lines`.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "ai-title":
                    continue
                ai_title = record.get("aiTitle")
                if isinstance(ai_title, str) and ai_title.strip():
                    return ai_title
    except OSError:
        return None
    return None


def build_ai_title_map(
    session_ids: Iterable[str],
    session_file: SessionFileLookup,
    max_lines: int = 40,
) -> dict[str, str]:
    """IMPURE: the `ai_titles` map `build_rows` takes, built for a batch of
    (young) session ids. Resolves each id's file through the same
    `SessionFileLookup` (glob, never slug derivation -- §7 step 4) `build_rows`
    itself is injected, then bounded-reads it via `read_ai_title`. A session
    with no file, or no `ai-title` within the line budget, is simply absent
    from the returned map -- `build_rows`' `_young_title` falls back to the
    last-prompt title for it.
    """
    titles: dict[str, str] = {}
    for session_id in session_ids:
        path = session_file(session_id)
        if path is None:
            continue
        title = read_ai_title(path, max_lines=max_lines)
        if title is not None:
            titles[session_id] = title
    return titles


def glob_session_file(claude_dir: Path, session_id: str) -> Path | None:
    """IMPURE: the default `SessionFileLookup` -- `projects/*/<sessionId>.jsonl`
    under `claude_dir` (typically `~/.claude`).

    **Never derive the project slug.** §7 step 4 is emphatic and measured:
    the slug transform Claude Code uses internally is neither injective nor
    case-stable (`C--` and `c--` both exist; `_`, `.`, `\\` and `:` all
    collapse to `-`, so `demo_project` and `demo-project` produce the
    same slug), and a slightly-wrong derivation greys every row while
    `status.json` stays green -- "the cleanest silent-failure path in the
    design." Globbing measured exactly one match per live session, zero
    collisions, across all 552 files on the design machine.

    Returns the first match if more than one is somehow found (a session id
    is a UUID; a second file for the same id has never been observed and
    would itself be worth investigating, but this function does not raise
    over it -- picking one deterministically keeps the picker usable).
    """
    matches = sorted(claude_dir.glob(f"projects/*/{session_id}.jsonl"))
    return matches[0] if matches else None


def make_glob_session_file(claude_dir: Path) -> SessionFileLookup:
    """IMPURE: bind `glob_session_file` to a `claude_dir`, producing the
    `SessionFileLookup` callable `build_rows` (and `build_ai_title_map`)
    expect."""

    def lookup(session_id: str) -> Path | None:
        return glob_session_file(claude_dir, session_id)

    return lookup
