"""Tests for `termsync.capture.build_capture` (S3, §5 steps 5-9, §13.4)."""

from __future__ import annotations

from typing import Any

import pytest

from termsync.capture import build_capture, live_session_ids
from termsync.emit import Emit, EmitError
from termsync.model import Pending, PendingEntry

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
        claude_process_count=claude_process_count
        if claude_process_count is not None
        else len(entries),
        boot_time=1787235232500,
    )


# --- step 5: alive -------------------------------------------------------


def test_dead_entry_is_dropped() -> None:
    result = build_capture(emit_of(entry(alive=False)), pending=None)
    assert result.sessions == ()


# --- step 6: procStart (also covered exhaustively by test_procstart.py) --


def test_proc_start_mismatch_is_dropped() -> None:
    result = build_capture(emit_of(entry(procStartLive="130000000000000004")), pending=None)
    assert result.sessions == ()


# --- step 7: kind ----------------------------------------------------------


def test_bg_entry_is_dropped() -> None:
    """bg sessions own no tab (§5 step 7)."""
    result = build_capture(emit_of(entry(kind="bg")), pending=None)
    assert result.sessions == ()


def test_interactive_entry_survives_all_three_filters() -> None:
    result = build_capture(emit_of(entry()), pending=None)
    assert len(result.sessions) == 1
    assert result.sessions[0].session_id == "s1"


def test_capture_normalizes_live_fallback_and_pending_titles_for_restore() -> None:
    pending = Pending(written_at=1, entries=(
        PendingEntry(session_id="armed", title="old; title", cwd="C:\\", armed_at=1),
    ))
    result = build_capture(emit_of(
        entry(titleRaw="\u2733 live; title"),
        entry(sessionId="fallback", titleAttachOk=False, name="fallback; title"),
    ), pending)
    assert [session.title for session in result.sessions] == [
        "live\uff1b title", "fallback\uff1b title", "old\uff1b title",
    ]
    assert result.title_fallbacks == 1


def test_filters_compose_a_mixed_batch_correctly() -> None:
    """One entry each: dead, procStart-mismatched, bg, and a clean survivor."""
    dead = entry(sessionId="dead", alive=False)
    mismatched = entry(sessionId="mismatched", procStartLive="130000000000000079")
    bg = entry(sessionId="bg1", kind="bg")
    good = entry(sessionId="good")

    result = build_capture(emit_of(dead, mismatched, bg, good), pending=None)

    assert [s.session_id for s in result.sessions] == ["good"]


# --- step 8: title strip + D6 fallback ------------------------------------


def test_title_is_stripped_of_its_status_glyph() -> None:
    result = build_capture(emit_of(entry(titleRaw="\u2733 termsync work")), pending=None)
    assert result.sessions[0].title == "termsync work"


def test_title_attach_failure_falls_back_to_registry_name() -> None:
    """D6, first half: titleAttachOk is False."""
    result = build_capture(
        emit_of(entry(titleAttachOk=False, titleRaw=None, name="termsync-fallback-1")),
        pending=None,
    )
    assert result.sessions[0].title == "termsync-fallback-1"
    assert result.title_fallbacks == 1


def test_empty_title_raw_falls_back_to_registry_name_even_when_attach_succeeded() -> None:
    """D6, second half: AttachConsole can succeed against a console whose title
    was never set, i.e. titleAttachOk True with an empty titleRaw."""
    result = build_capture(
        emit_of(entry(titleAttachOk=True, titleRaw="", name="termsync-fallback-2")),
        pending=None,
    )
    assert result.sessions[0].title == "termsync-fallback-2"
    assert result.title_fallbacks == 1


def test_whitespace_only_title_raw_falls_back() -> None:
    result = build_capture(
        emit_of(entry(titleAttachOk=True, titleRaw="   ", name="termsync-fallback-3")),
        pending=None,
    )
    assert result.sessions[0].title == "termsync-fallback-3"
    assert result.title_fallbacks == 1


def test_title_fallbacks_counts_both_d6_halves() -> None:
    attach_failed = entry(sessionId="a", titleAttachOk=False, titleRaw=None, name="a-name")
    empty_raw = entry(sessionId="b", titleAttachOk=True, titleRaw="", name="b-name")
    fine = entry(sessionId="c", titleRaw="\u2733 fine title")

    result = build_capture(emit_of(attach_failed, empty_raw, fine), pending=None)

    assert result.title_fallbacks == 2


def test_fallback_on_a_dropped_session_does_not_count() -> None:
    """A fallback on a session that gets dropped (dead here) is not part of
    this desk and must not inflate title_fallbacks."""
    dropped = entry(alive=False, titleAttachOk=False, titleRaw=None)
    result = build_capture(emit_of(dropped), pending=None)
    assert result.sessions == ()
    assert result.title_fallbacks == 0


def test_a_fallback_with_no_registry_name_either_falls_back_to_the_session_id() -> None:
    """Review finding F9/L4: `entry.get("name", "")` used to write an empty
    title when BOTH `titleRaw` and the registry `name` were absent/empty --
    an empty row in the picker is a worse outcome than a placeholder. The
    session id is a stable, non-empty last resort; the run is already marked
    `degraded` over the fallback itself, so this does not hide anything."""
    result = build_capture(
        emit_of(entry(sessionId="s-no-name", titleAttachOk=False, titleRaw=None, name="")),
        pending=None,
    )
    assert result.sessions[0].title == "s-no-name"
    assert result.title_fallbacks == 1


# --- step 9: pending union -------------------------------------------------


def test_pending_entry_not_live_is_carried_in_armed() -> None:
    pending = Pending(
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
    result = build_capture(emit_of(entry(sessionId="live-1")), pending=pending)

    ids = {s.session_id: s for s in result.sessions}
    assert ids["armed-1"].armed is True
    assert ids["armed-1"].title == "Armed session"
    assert ids["armed-1"].cwd == r"C:\fixture\tool"
    assert ids["live-1"].armed is False


def test_pending_entry_already_live_is_not_duplicated() -> None:
    pending = Pending(
        written_at=1,
        entries=(
            PendingEntry(session_id="s1", title="stale pending title", cwd="stale-cwd", armed_at=1),
        ),
    )
    result = build_capture(emit_of(entry(sessionId="s1")), pending=pending)

    assert len(result.sessions) == 1
    assert result.sessions[0].armed is False  # the LIVE entry won, not the pending one


def test_no_pending_file_is_fine() -> None:
    result = build_capture(emit_of(entry()), pending=None)
    assert len(result.sessions) == 1


def test_empty_pending_is_fine() -> None:
    result = build_capture(emit_of(entry()), pending=Pending(written_at=1, entries=()))
    assert len(result.sessions) == 1


# --- registry_versions -------------------------------------------------------


def test_registry_versions_gathers_from_all_readable_entries_including_bg() -> None:
    interactive = entry(sessionId="a", version="2.1.239")
    bg = entry(sessionId="b", kind="bg", version="2.1.238")

    result = build_capture(emit_of(interactive, bg), pending=None)

    assert result.registry_versions == ("2.1.238", "2.1.239")


def test_registry_versions_includes_dropped_dead_entries_too() -> None:
    dead = entry(sessionId="dead", alive=False, version="2.1.240")
    result = build_capture(emit_of(dead), pending=None)
    assert result.registry_versions == ("2.1.240",)


# --- readable_entry_count ----------------------------------------------------


def test_readable_entry_count_is_len_emit_sessions_unaffected_by_filters() -> None:
    """D3/M9: BEFORE liveness, procStart, and kind filtering."""
    dead = entry(sessionId="dead", alive=False)
    mismatched = entry(sessionId="mismatched", procStartLive="mismatch")
    bg = entry(sessionId="bg1", kind="bg")
    good = entry(sessionId="good")

    result = build_capture(emit_of(dead, mismatched, bg, good), pending=None)

    assert result.readable_entry_count == 4
    assert len(result.sessions) == 1  # only `good` survives every filter


def test_readable_entry_count_is_zero_for_an_empty_registry() -> None:
    result = build_capture(emit_of(), pending=None)
    assert result.readable_entry_count == 0


def test_readable_entry_count_is_unaffected_by_pending_union() -> None:
    """readable_entry_count describes the REGISTRY, not the resulting desk --
    a pending entry added at step 9 must not move this count."""
    pending = Pending(
        written_at=1,
        entries=(PendingEntry(session_id="armed-1", title="t", cwd="c", armed_at=1),),
    )
    result = build_capture(emit_of(entry()), pending=pending)
    assert result.readable_entry_count == 1
    assert len(result.sessions) == 2  # the live one + the armed pending one


# --- F5: a missing sessionId must not raise a bare KeyError ------------------


def test_a_missing_session_id_on_a_surviving_entry_raises_a_named_error_not_keyerror() -> None:
    """Review finding F5 [MEDIUM]: the coordinator probed this directly --
    both `cli.py`'s comprehension and `build_capture` raised `KeyError:
    'sessionId'` on an alive, filter-surviving entry with no `sessionId`.
    F1's contract check at the `emit.py` boundary should make this
    unreachable in production; this pins the defensive fallback for an
    `Emit` built directly (as every test in this file does)."""
    bad = entry()
    del bad["sessionId"]
    with pytest.raises(EmitError, match="sessionId"):
        build_capture(emit_of(bad), pending=None)


# --- F4/M3: live_session_ids is the SAME definition build_capture carries ----


def test_live_session_ids_matches_build_capture_survivors_on_the_ordinary_case() -> None:
    result_ids = {s.session_id for s in build_capture(emit_of(entry()), pending=None).sessions}
    assert live_session_ids(emit_of(entry())) == frozenset(result_ids)


def test_live_session_ids_excludes_a_dead_entry() -> None:
    assert live_session_ids(emit_of(entry(alive=False))) == frozenset()


def test_live_session_ids_excludes_a_proc_start_mismatch() -> None:
    """Review finding M3's reachable disagreement, half 1: a pid-reused entry
    (procStart mismatch) must not count as live for EITHER caller of this
    filter -- `cli.py`'s `expire_pending` call and `build_capture` must agree,
    or an armed session can be dropped from `pending.json` and never carried
    into the layout in the same run."""
    mismatched = entry(sessionId="armed-1", procStartLive="130000000000000079")
    assert live_session_ids(emit_of(mismatched)) == frozenset()
    result = build_capture(emit_of(mismatched), pending=None)
    assert result.sessions == ()


def test_live_session_ids_excludes_a_bg_kind_reentry() -> None:
    """M3's reachable disagreement, half 2: the same session id reappearing
    as `kind: "bg"` must not count as live either."""
    bg_reentry = entry(sessionId="armed-1", kind="bg")
    assert live_session_ids(emit_of(bg_reentry)) == frozenset()
    result = build_capture(emit_of(bg_reentry), pending=None)
    assert result.sessions == ()


def test_live_session_ids_drops_a_sessionid_less_entry_rather_than_raising() -> None:
    """M4/F5's fix for THIS function specifically: `.get`, not a hard
    subscript -- matching `build_capture`'s own defensive read."""
    no_id = entry()
    del no_id["sessionId"]
    assert live_session_ids(emit_of(no_id)) == frozenset()
