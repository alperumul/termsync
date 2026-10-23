"""Behaviour tests for restore target selection (§7 step 1).

`select_target` is pure: these tests hand it a series of `Layout` objects with no
filesystem, no store and no clock. That is the point of the seam — the unmeasured
mass-disappearance threshold (§11 open item 9) will be tuned against exactly this
kind of hand-written series.
"""

from dataclasses import FrozenInstanceError

import pytest

from termsync.model import Layout, Session
from termsync.selection import Target, select_target

BOOT_A = 1_787_000_000_000
BOOT_B = 1_787_235_232_500
BOOT_C = 1_787_400_000_000


def layout(version, boot_id, session_count, captured_at, source="capture"):
    sessions = tuple(
        Session(session_id=f"v{version}s{i}", cwd=r"C:\fixture\demo_project", title=f"t{i}")
        for i in range(session_count)
    )
    return Layout(
        version=version,
        source=source,
        captured_at=captured_at,
        boot_id=boot_id,
        hash=f"sha256:fake{version}",
        sessions=sessions,
    )


def test_no_layouts_means_no_target():
    assert select_target([], boot_id=BOOT_B) is None


def test_a_single_layout_is_both_the_pick_and_the_alternative():
    only = layout(1, BOOT_B, 3, 1787259000000)

    result = select_target([only], boot_id=BOOT_B)

    assert result == Target(layout=only, newest=only)


def test_the_largest_layout_of_the_current_boot_wins_over_the_newest():
    """§7 step 1: the crash the rule exists for, inside one boot.

    An earlier capture holds 10 sessions; a later capture from the *same* boot
    holds 2 because the terminal died. The pick must be the 10-session layout and
    the newest (2-session) layout must still be offered separately as `[n]`.
    """
    ten = layout(1, BOOT_B, 10, 1787259000000)
    two = layout(2, BOOT_B, 2, 1787259600000)

    result = select_target([ten, two], boot_id=BOOT_B)

    assert result.layout is ten
    assert result.newest is two


def test_a_post_reboot_remnant_does_not_hide_the_desk_from_before_the_reboot():
    """H2: the founding scenario. The 10-session desk must stay reachable.

    22:50 capture of 10 sessions (previous boot); 22:55 crash remnant of 2 (same
    previous boot); reboot; 23:05 the poll captures the 1 session reopened so far
    under the new boot. Selecting only within the current boot returns that
    1-session layout as both the pick and its own alternative, which is how the
    10-session desk became unreachable through any public method.
    """
    ten = layout(1, BOOT_A, 10, 1787259000000)
    crash_remnant = layout(2, BOOT_A, 2, 1787259300000)
    after_reboot = layout(3, BOOT_B, 1, 1787259900000)

    result = select_target([ten, crash_remnant, after_reboot], boot_id=BOOT_B)

    assert result.layout.version == ten.version, "the 10-session desk must be the pick"
    assert len(result.layout.sessions) == 10
    assert result.newest is after_reboot, "the newest layout is still the [n] alternative"


def test_no_layout_from_the_current_boot_falls_back_to_the_stored_boots_not_to_the_newest():
    """The branch that decides the founding use case, previously untested.

    Right after a reboot, before the first poll, nothing carries the current boot.
    "The newest" is the rule §7 step 1 opens by rejecting, so the fallback must
    still be a largest-of, not a latest-of.
    """
    big = layout(1, BOOT_A, 10, 1787259000000)
    small = layout(2, BOOT_B, 3, 1787300000000)

    result = select_target([big, small], boot_id=BOOT_C)

    assert result.layout is big
    assert result.newest is small


def test_a_desk_two_boots_back_is_history_and_does_not_win():
    """Widening the candidate set stops at the previous boot boundary.

    A 20-session desk from two boots ago is history: the user rebooted, worked a
    whole session, and rebooted again. Only the current boot and the one before it
    compete, so the current boot's 5-session desk wins over it.
    """
    ancient = layout(1, BOOT_A, 20, 1787100000000)
    middle = layout(2, BOOT_B, 2, 1787259000000)
    current = layout(3, BOOT_C, 5, 1787400000000)

    result = select_target([ancient, middle, current], boot_id=BOOT_C)

    assert result.layout is current
    assert result.newest is current
    assert ancient.version not in {result.layout.version, result.newest.version}


def test_equal_sized_candidates_tie_break_on_the_later_version():
    first = layout(1, BOOT_B, 4, 1787259000000)
    second = layout(2, BOOT_B, 4, 1787259600000)

    result = select_target([first, second], boot_id=BOOT_B)

    assert result.layout is second


def test_the_input_order_does_not_matter():
    """The store hands back version order; a caller must not be able to break it."""
    ten = layout(1, BOOT_A, 10, 1787259000000)
    remnant = layout(3, BOOT_B, 1, 1787259900000)
    two = layout(2, BOOT_A, 2, 1787259300000)

    shuffled = select_target([remnant, two, ten], boot_id=BOOT_B)
    ordered = select_target([ten, two, remnant], boot_id=BOOT_B)

    assert shuffled == ordered
    assert shuffled.layout.version == 1
    assert shuffled.newest.version == 3


def test_selection_touches_no_filesystem(monkeypatch):
    """Purity, asserted rather than asserted-about: `open` must never be called."""

    def forbidden(*args, **kwargs):
        raise AssertionError("select_target must not touch the filesystem")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("os.open", forbidden)

    result = select_target(
        [layout(1, BOOT_A, 10, 1787259000000), layout(2, BOOT_B, 1, 1787259900000)],
        boot_id=BOOT_B,
    )

    assert result.layout.version == 1


def test_a_null_boot_id_layout_is_excluded_from_the_window_never_corrupts_it():
    """Review finding F3 [MEDIUM], the reasoning behind reversing `cli.snap`'s
    "refuse to write a layout when `bootTime` is null" policy. `None`
    behaves as an ordinary dict/set key throughout `_recent_boots`: a run of
    null-boot layouts collapses into ONE pseudo-boot group rather than being
    skipped, raising, or corrupting ordering. Reproduces the review's P8,
    series 1: a null-boot layout with 10 sessions, an old-boot layout with
    2, and a new-boot layout with 1 -- selecting under the new boot picks
    the old-boot layout, and `[n]` offers the new one; the null-boot layout
    is excluded from the window, which is exactly what NOT writing it would
    also have produced. No arrangement makes writing it worse than refusing.
    """
    null_boot = layout(1, None, 10, 1787259000000)
    old_boot = layout(2, BOOT_A, 2, 1787259300000)
    new_boot = layout(3, BOOT_B, 1, 1787259900000)

    result = select_target([null_boot, old_boot, new_boot], boot_id=BOOT_B)

    assert result.layout is old_boot
    assert result.newest is new_boot


def test_null_boot_id_layouts_collapse_into_one_pseudo_boot_not_one_slot_each():
    """P8, series 3: several null-boot layouts occupy ONE slot of the
    two-boot candidate window (they all share the `None` boot key), not one
    slot each -- so a real boot from before them is not pushed out of the
    window by null-boot noise."""
    real_boot = layout(1, BOOT_A, 10, 1787259000000)
    null_one = layout(2, None, 1, 1787259300000)
    null_two = layout(3, None, 2, 1787259600000)

    result = select_target([real_boot, null_one, null_two], boot_id=None)

    assert result.layout is real_boot, "the real boot must still compete, not be pushed out"
    assert result.newest is null_two


def test_a_current_null_boot_still_picks_the_largest_layout_of_that_boot():
    """When the CURRENT boot itself is a null (`boot_id=None` passed to
    `select_target`, mirroring a live run whose own `bootTime` read failed),
    selection still works: the null-boot layouts are the current boot's own
    candidates."""
    small = layout(1, None, 1, 1787259000000)
    large = layout(2, None, 5, 1787259600000)

    result = select_target([small, large], boot_id=None)

    assert result.layout is large
    assert result.newest is large


def test_a_restore_record_is_never_the_pick_or_the_newest_alternative():
    """D20 (S6 review finding F1): a `source: "restore"` record is a record
    of an ACTION -- what the user selected in the picker -- not a snapshot
    of a desk, so it must never compete for `layout` or `newest`. Before
    this filter, a restore record written smaller than the capture it was
    chosen from (the ordinary case -- deselecting some of what was offered
    is the picker's whole purpose) could become `newest` on its own
    version number, and the shrink guard (§8, `cli._header_lines`) would
    then compare the shown capture against it and cry "strict subset, same
    boot" on every ordinary restore. The ten-session capture is the ONLY
    real candidate here; the six-session restore record must be invisible
    to selection entirely, even though it has the higher version number.
    """
    ten = layout(1, BOOT_B, 10, 1787259000000, source="capture")
    six_restored = layout(2, BOOT_B, 6, 1787259600000, source="restore")

    result = select_target([ten, six_restored], boot_id=BOOT_B)

    assert result.layout is ten
    assert result.newest is ten
    assert six_restored.version not in {result.layout.version, result.newest.version}


def test_a_store_with_only_restore_records_and_no_capture_has_no_target():
    """The edge case D20's brief names explicitly: filtering out every
    `source: "restore"` layout can leave nothing. `select_target` must
    return `None` in that case -- `cli` already renders that as "No desk
    captured yet", the same message an empty store produces."""
    only_a_restore = layout(1, BOOT_B, 3, 1787259000000, source="restore")

    assert select_target([only_a_restore], boot_id=BOOT_B) is None


def test_the_pick_and_the_alternative_are_the_only_public_shape():
    """`Target` carries exactly what a caller needs to protect from pruning."""
    ten = layout(1, BOOT_B, 10, 1787259000000)
    two = layout(2, BOOT_B, 2, 1787259600000)

    result = select_target([ten, two], boot_id=BOOT_B)

    assert {result.layout.version, result.newest.version} == {1, 2}
    with pytest.raises(FrozenInstanceError):
        result.layout = ten  # frozen: policy output is not mutable state
