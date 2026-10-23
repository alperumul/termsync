"""Behaviour tests for rows.py — the four-source join behind §2's picker row
(§13.2, §13.3 work unit S9, §13.4's mandated test list).

Entirely fixture-driven: no Windows interop, no real ~/.claude. Every
dependency `build_rows` needs (history, the live set, the glob, the cwd
check) is constructed here and injected directly.
"""

import json

from termsync.history import HistoryEntry
from termsync.model import Layout, Session
from termsync.rows import (
    Row,
    _fallback_title,
    build_ai_title_map,
    build_rows,
    glob_session_file,
    make_glob_session_file,
    read_ai_title,
)

CWD = r"C:\fixture\demo_project"


def make_layout(sessions, captured_at=10_000, version=1):
    return Layout(
        version=version,
        source="capture",
        captured_at=captured_at,
        boot_id=1_000,
        hash="sha256:irrelevant",
        sessions=tuple(sessions),
    )


def make_session(session_id, cwd=CWD, title="A title", armed=False):
    return Session(session_id=session_id, cwd=cwd, title=title, armed=armed)


def make_history_entry(session_id, prompt_count=1, last_prompt="hi", last_prompt_at=5_000, cwd=CWD):
    return HistoryEntry(
        session_id=session_id,
        prompt_count=prompt_count,
        last_prompt=last_prompt,
        last_prompt_at=last_prompt_at,
        cwd=cwd,
    )


def files_lookup(mapping):
    """A SessionFileLookup backed by a plain dict of {session_id: Path|None}."""

    def lookup(session_id):
        return mapping.get(session_id)

    return lookup


def cwds_lookup(existing):
    """A CwdExists backed by a set of cwd strings known to exist."""

    def exists(cwd):
        return cwd in existing

    return exists


# ---------------------------------------------------------------------------
# The join itself
# ---------------------------------------------------------------------------


def test_the_four_source_join_produces_the_row_exactly(tmp_path):
    session = make_session("s1", cwd=CWD, title="Project plan phase 3 deep review")
    layout = make_layout([session])
    history = {"s1": make_history_entry("s1", prompt_count=14, last_prompt_at=9_000)}
    session_path = tmp_path / "s1.jsonl"
    session_path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"s1": session_path}),
        cwd_exists=cwds_lookup({CWD}),
    )

    assert rows.desk == (
        Row(
            session_id="s1",
            title="Project plan phase 3 deep review",
            cwd=CWD,
            prompt_count=14,
            last_prompt_at=9_000,
            session_file=session_path,
            cwd_exists=True,
            armed=False,
            live=False,
            young=False,
            selectable=True,
        ),
    )
    assert rows.young == ()


def test_a_session_whose_file_the_glob_cannot_find_is_greyed_and_unselectable():
    layout = make_layout([make_session("missing-file")])

    rows = build_rows(
        layout=layout,
        history={},
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),  # glob finds nothing
        cwd_exists=cwds_lookup({CWD}),
    )

    [row] = rows.desk
    assert row.session_file is None
    assert row.selectable is False


def test_a_session_in_pending_but_not_live_is_marked_armed(tmp_path):
    """An armed-but-not-yet-resumed session arrives already unioned into the
    layout by capture.py, with Session.armed=True (§5 step 9). build_rows must
    read that flag straight off the layout -- there is no second, independent
    source of "armed" for it to reconcile against.

    Companion test right below (`test_an_armed_session_stays_armed_even_...`)
    covers the state THIS fixture does not: in production `dry_run` always
    unions `pending.json`'s ids into `live_session_ids` (§7 step 2), so an
    armed session's own id is a member of the set passed here, not absent
    from it as `frozenset()` implies. Both are asserted so the armed-vs-live
    precedence (`_desk_row`'s docstring) is pinned in the state this fixture
    constructs AND the state production actually reaches.
    """
    session = make_session("armed-1", armed=True)
    layout = make_layout([session])
    path = tmp_path / "armed-1.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history={},
        live_session_ids=frozenset(),  # NOT live -- an armed tab hosts no process
        bg_session_ids=frozenset(),
        session_file=files_lookup({"armed-1": path}),
        cwd_exists=cwds_lookup({CWD}),
    )

    [row] = rows.desk
    assert row.armed is True
    assert row.live is False


def test_an_armed_session_stays_armed_even_when_its_id_is_also_in_live_session_ids(tmp_path):
    """§7 step 2's union, exercised for real: `dry_run` computes
    `live_session_ids(emit) | pending.session_ids`, so an armed session's id
    is ALWAYS also a member of the set `build_rows` is handed in production
    -- the exact state review finding H1 named as the one the old
    `frozenset()`-based test above never constructed. `session.armed` (set
    once, at capture, from the layout -- see `_desk_row`'s docstring) must
    win: an armed tab hosts no process, so it must never render as live
    merely because its id also sits in the union.
    """
    session = make_session("armed-1", armed=True)
    layout = make_layout([session])
    path = tmp_path / "armed-1.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history={},
        live_session_ids=frozenset({"armed-1"}),  # the REAL production state (§7 step 2's union)
        bg_session_ids=frozenset(),
        session_file=files_lookup({"armed-1": path}),
        cwd_exists=cwds_lookup({CWD}),
    )

    [row] = rows.desk
    assert row.armed is True
    assert row.live is False
    assert row.selectable is True


def test_a_session_live_in_the_registry_stays_on_the_desk_marked_live_and_unselectable():
    """Rewritten per review finding F1/H1 and D10 (`docs/TERMSYNC.md` §16),
    a deliberate, recorded spec change -- NOT an F4-style "this test pinned
    a defect" rewrite, but a necessary consequence of implementing F1's fix:
    this test used to assert that a live-in-the-registry session was
    EXCLUDED from `rows.desk` entirely. That is exactly the shape of the
    defect the review's finding H1 demonstrated live: an armed-but-never-
    resumed session (also carried into the live-ids set, via `pending.json`'s
    union at §7 step 2) vanished from the desk list the same way, and
    `--dry-run` then reported it as "already open" while two `claude.exe`
    processes were, in fact, zero. D10 reverses the exclusion: a session
    already live STAYS in the desk list, marked `live=True` and
    `selectable=False`, rather than being deleted from it -- excluding an
    already-live session from actually OPENING a tab (the reason §7 step 2's
    exclusion exists) is a restore-time concern that belongs to S5/S6, not
    to this pure join.
    """
    layout = make_layout([make_session("live-1"), make_session("not-live")])

    rows = build_rows(
        layout=layout,
        history={},
        live_session_ids=frozenset({"live-1"}),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"live-1": None, "not-live": None}),
        cwd_exists=cwds_lookup({CWD}),
    )

    desk_ids = {row.session_id for row in rows.desk}
    assert desk_ids == {"live-1", "not-live"}, "a live session stays ON the desk now, not deleted"

    [live_row] = [row for row in rows.desk if row.session_id == "live-1"]
    assert live_row.live is True
    assert live_row.selectable is False

    [other_row] = [row for row in rows.desk if row.session_id == "not-live"]
    assert other_row.live is False


def test_a_session_with_no_history_entry_gets_none_prompt_count_and_is_still_selectable(tmp_path):
    layout = make_layout([make_session("no-history")])
    path = tmp_path / "no-history.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history={},  # no entry for "no-history" at all
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"no-history": path}),
        cwd_exists=cwds_lookup({CWD}),
    )

    [row] = rows.desk
    assert row.prompt_count is None
    assert row.last_prompt_at is None
    assert row.selectable is True


def test_a_session_whose_cwd_no_longer_exists_is_unselectable(tmp_path):
    layout = make_layout([make_session("deleted-cwd", cwd=r"C:\fixture\gone")])
    path = tmp_path / "deleted-cwd.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history={},
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"deleted-cwd": path}),
        cwd_exists=cwds_lookup(set()),  # nothing exists
    )

    [row] = rows.desk
    assert row.session_file is not None  # the file exists...
    assert row.cwd_exists is False  # ...but the cwd it would restore into doesn't
    assert row.selectable is False


def test_a_desk_rows_cwd_comes_from_the_layout_not_history_when_they_disagree(tmp_path):
    """Question 2 / review mutation D: on disagreement between the layout's
    cwd and history's cwd for the same session, the LAYOUT wins -- it is the
    value the registry reported AT CAPTURE, which is what a restore would
    reopen (`build_rows`'s own design note, and §5's `HistoryEntry.cwd`
    docstring: history's value can be a worktree or subdirectory the session
    has since moved on from). Mutation D ("take cwd from HISTORY instead")
    survived all 230 tests because no fixture before this one made the two
    sources disagree -- every other test in this file uses the same `CWD`
    constant for both.
    """
    layout_cwd = r"C:\fixture\demo_project"
    history_cwd = r"C:\fixture\other-project"
    session = make_session("s1", cwd=layout_cwd)
    layout = make_layout([session])
    history = {"s1": make_history_entry("s1", cwd=history_cwd)}
    path = tmp_path / "s1.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"s1": path}),
        cwd_exists=cwds_lookup({layout_cwd, history_cwd}),
    )

    [row] = rows.desk
    assert row.cwd == layout_cwd


# ---------------------------------------------------------------------------
# The young bucket
# ---------------------------------------------------------------------------


def test_young_bucket_contains_only_sessions_active_after_captured_at_and_no_bg_ids(tmp_path):
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "young-real": make_history_entry("young-real", last_prompt_at=15_000, cwd=CWD),
        "stale": make_history_entry("stale", last_prompt_at=5_000, cwd=CWD),  # before capture
        "young-bg": make_history_entry("young-bg", last_prompt_at=16_000, cwd=CWD),  # bg-excluded
    }
    files = {sid: tmp_path / f"{sid}.jsonl" for sid in history}
    for path in files.values():
        path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset({"young-bg"}),
        session_file=files_lookup(files),
        cwd_exists=cwds_lookup({CWD}),
    )

    young_ids = {row.session_id for row in rows.young}
    assert young_ids == {"young-real"}
    assert all(row.young is True for row in rows.young)


def test_young_bucket_is_sorted_newest_first(tmp_path):
    """Review mutation K ("sort the young bucket OLDEST-first") survived all
    230 tests -- nothing pinned the order at all."""
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "oldest": make_history_entry("oldest", last_prompt_at=15_000, cwd=CWD),
        "newest": make_history_entry("newest", last_prompt_at=25_000, cwd=CWD),
        "middle": make_history_entry("middle", last_prompt_at=20_000, cwd=CWD),
    }

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),
        cwd_exists=cwds_lookup({CWD}),
    )

    assert [row.session_id for row in rows.young] == ["newest", "middle", "oldest"]


def test_young_bucket_excludes_a_session_already_live_or_already_on_the_desk():
    captured_at = 10_000
    already_desk = make_session("in-desk", title="already captured")
    layout = make_layout([already_desk], captured_at=captured_at)
    history = {
        # Same id as the layout's own session -- must not be double-listed.
        "in-desk": make_history_entry("in-desk", last_prompt_at=15_000, cwd=CWD),
        # Live (e.g. still shown by -Emit even though not in this layout) --
        # must not be offered as "started since the last snapshot" either.
        "already-live": make_history_entry("already-live", last_prompt_at=15_000, cwd=CWD),
    }

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset({"already-live"}),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),
        cwd_exists=cwds_lookup({CWD}),
    )

    assert rows.young == ()
    assert {row.session_id for row in rows.desk} == {"in-desk"}


# ---------------------------------------------------------------------------
# Young-row titles
# ---------------------------------------------------------------------------


def test_a_young_sessions_title_uses_ai_title_when_supplied():
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "young-1": make_history_entry(
            "young-1", last_prompt="raw prompt text nobody should see", last_prompt_at=15_000
        ),
    }

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),
        cwd_exists=cwds_lookup({CWD}),
        ai_titles={"young-1": "AI-generated session title"},
    )

    [row] = rows.young
    assert row.title == "AI-generated session title"


def test_a_young_sessions_title_falls_back_to_the_last_prompt_when_no_ai_title():
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "young-1": make_history_entry(
            "young-1", last_prompt="what should I build next", last_prompt_at=15_000
        ),
    }

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),
        cwd_exists=cwds_lookup({CWD}),
        ai_titles=None,  # nothing supplied for this batch
    )

    [row] = rows.young
    assert row.title == "what should I build next"


def test_a_young_sessions_title_falls_back_when_ai_titles_map_has_no_entry_for_it():
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "young-1": make_history_entry(
            "young-1", last_prompt="fallback prompt text", last_prompt_at=15_000
        ),
    }

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({}),
        cwd_exists=cwds_lookup({CWD}),
        ai_titles={"some-other-session": "irrelevant"},
    )

    [row] = rows.young
    assert row.title == "fallback prompt text"


def test_fallback_title_truncates_a_long_first_line_with_an_ascii_ellipsis():
    """Review mutation O ("_fallback_title: never truncate") survived all 230
    tests -- nothing exercised `_fallback_title` directly, or with a prompt
    longer than `_FALLBACK_TITLE_MAX_LEN`."""
    long_prompt = "x" * 80
    result = _fallback_title(long_prompt)
    assert len(result) == 60
    assert result.endswith("...")
    assert "\u2026" not in result  # ASCII "..." marker, not a Unicode ellipsis -- module docstring


def test_fallback_title_collapses_to_the_first_line_of_a_multiline_prompt():
    assert _fallback_title("first line\nsecond line") == "first line"


def test_fallback_title_short_prompt_is_unchanged():
    assert _fallback_title("short prompt") == "short prompt"


def test_a_young_session_with_no_known_cwd_is_unselectable(tmp_path):
    """HistoryEntry.cwd is None when history.jsonl's winning record has a
    missing/blank `project` -- a young row cannot be restored into a
    directory nobody knows, so it must not be marked selectable.

    Review mutation C ("force cwd_exists=True for an unknown cwd") survived
    all 230 tests against the ORIGINAL fixture here, because that fixture
    also mapped the session's file to `None` -- so `selectable` was already
    `False` from the FIRST conjunct (`session_file is not None`), whatever
    `cwd_exists` returned; the specific claim in this test's name was never
    actually exercised. Rewritten to give the session a real file, so the
    ONLY reason left for `selectable is False` is the unknown cwd -- the
    mutation now fails this test, because `_young_row`'s `if cwd else False`
    guard (the thing being mutated away) is the only thing still preventing
    `exists` from going `True`.
    """
    captured_at = 10_000
    layout = make_layout([], captured_at=captured_at)
    history = {
        "young-no-cwd": make_history_entry("young-no-cwd", last_prompt_at=15_000, cwd=None),
    }
    path = tmp_path / "young-no-cwd.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    rows = build_rows(
        layout=layout,
        history=history,
        live_session_ids=frozenset(),
        bg_session_ids=frozenset(),
        session_file=files_lookup({"young-no-cwd": path}),  # the FILE exists...
        cwd_exists=cwds_lookup({CWD}),
    )

    [row] = rows.young
    assert row.cwd == ""
    assert row.session_file is not None  # ...so the file is NOT why it's unselectable
    assert row.cwd_exists is False  # ...only the unknown cwd is
    assert row.selectable is False


# ---------------------------------------------------------------------------
# read_ai_title — the bounded read
# ---------------------------------------------------------------------------


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(record) + "\n" for record in records)


def test_read_ai_title_finds_it_within_the_line_budget(tmp_path):
    path = tmp_path / "session.jsonl"
    records = [{"type": "user", "n": i} for i in range(14)]
    records.append({"type": "ai-title", "aiTitle": "Found it", "sessionId": "s1"})
    _write_jsonl(path, records)

    assert read_ai_title(path, max_lines=40) == "Found it"


def test_read_ai_title_stops_after_its_line_budget(tmp_path):
    """The whole point of the bounded read: a file whose ai-title record sits
    PAST the line budget must come back None. A full-file-scan "fix" would
    find it anyway and make this test fail -- which is deliberate; see this
    function's docstring in rows.py."""
    path = tmp_path / "session.jsonl"
    records = [{"type": "user", "n": i} for i in range(45)]
    records.append({"type": "ai-title", "aiTitle": "Too late", "sessionId": "s1"})
    _write_jsonl(path, records)

    assert read_ai_title(path, max_lines=40) is None
    # Sanity check the fixture itself is well-formed: raising the budget
    # finds the very same title, proving the None above came from the bound
    # and not from a malformed record.
    assert read_ai_title(path, max_lines=100) == "Too late"


def test_read_ai_title_returns_none_for_a_missing_file(tmp_path):
    assert read_ai_title(tmp_path / "does-not-exist.jsonl", max_lines=40) is None


def test_build_ai_title_map_skips_sessions_with_no_file_and_no_title(tmp_path):
    with_title = tmp_path / "has-title.jsonl"
    _write_jsonl(with_title, [{"type": "ai-title", "aiTitle": "Has one", "sessionId": "a"}])
    no_title = tmp_path / "no-title.jsonl"
    _write_jsonl(no_title, [{"type": "user", "n": 1}])

    result = build_ai_title_map(
        ["a", "b", "c"],
        files_lookup({"a": with_title, "b": no_title, "c": None}),
    )

    assert result == {"a": "Has one"}


# ---------------------------------------------------------------------------
# The default glob lookup
# ---------------------------------------------------------------------------


def test_glob_session_file_finds_the_one_match(tmp_path):
    project_dir = tmp_path / "projects" / "C--fixture-demo-project"
    project_dir.mkdir(parents=True)
    target = project_dir / "abc-123.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    assert glob_session_file(tmp_path, "abc-123") == target


def test_glob_session_file_returns_none_when_nothing_matches(tmp_path):
    (tmp_path / "projects").mkdir()

    assert glob_session_file(tmp_path, "no-such-session") is None


def test_make_glob_session_file_is_a_valid_session_file_lookup(tmp_path):
    project_dir = tmp_path / "projects" / "some-slug"
    project_dir.mkdir(parents=True)
    target = project_dir / "xyz.jsonl"
    target.write_text("{}\n", encoding="utf-8")

    lookup = make_glob_session_file(tmp_path)

    assert lookup("xyz") == target
    assert lookup("nope") is None
