"""Behaviour tests for health surfacing (§8).

`health.py` is the sole owner of the primary error rule (§8, line 549): it is
evaluated in Python from `-Emit`'s output, and nothing in this repo may
duplicate it in PowerShell or re-implement any part of it in `store.py`.
"""

from termsync import health


def test_the_primary_error_rule_fires_when_claude_runs_and_no_entries_are_readable():
    """Revision 4's rule (§8): >=1 claude.exe, zero readable registry entries."""
    assert health.is_registry_error(claude_process_count=1, readable_entry_count=0) is True


def test_the_primary_error_rule_does_not_fire_when_entries_are_readable():
    assert health.is_registry_error(claude_process_count=3, readable_entry_count=9) is False


def test_the_primary_error_rule_does_not_fire_on_a_clean_boot():
    """Zero claude.exe, zero registry entries is the normal state every morning
    before the first session starts — revision 3's rule fired here daily and
    trained the user to ignore the banner (§8)."""
    assert health.is_registry_error(claude_process_count=0, readable_entry_count=0) is False


def test_the_primary_error_rule_does_not_fire_when_claude_is_not_running_but_entries_are_readable():
    """Zero claude.exe processes but entries readable from a prior boot/registry
    state — the rule only ever fires on the running+empty quadrant.

    Renamed from `..._does_not_fire_on_desks_own_armed_tabs`: §8's armed-tabs
    false-positive concern was specifically "a window full of tabs and zero
    interactive registry entries" — i.e. (0 processes, 0 readable entries) —
    which `test_the_primary_error_rule_does_not_fire_on_a_clean_boot` already
    covers. This test exercises a different, also-legitimate quadrant
    (0 processes, 5 readable entries); the old name claimed the armed-tabs
    concern without testing it. All four quadrants are covered across this
    file — this rename makes the name match what is actually asserted.
    """
    assert health.is_registry_error(claude_process_count=0, readable_entry_count=5) is False


def test_the_primary_error_rule_does_not_fire_on_desks_own_armed_tabs():
    """§8's actual armed-tabs concern: a window full of tabs and zero
    interactive registry entries, with no Claude process running at all —
    (0 processes, 0 readable entries)."""
    assert health.is_registry_error(claude_process_count=0, readable_entry_count=0) is False


def test_any_title_fallback_forces_degraded_never_ok():
    """§3.5 / §8: the registry `name` may be a derived placeholder
    (for example, `demo-project-36`), so a capture where even
    one AttachConsole call failed stores a desk of meaningless labels — worse
    than Claude Code's own /resume picker — and must never report success."""
    assert health.outcome_for_capture(registry_error=False, title_fallbacks=1) == "degraded"


def test_a_clean_capture_with_no_fallbacks_is_ok():
    assert health.outcome_for_capture(registry_error=False, title_fallbacks=0) == "ok"


def test_a_registry_error_outranks_a_title_fallback():
    assert health.outcome_for_capture(registry_error=True, title_fallbacks=1) == "error"


def test_boot_boundary_phrasing_before_the_current_boot():
    """§6's corrected worked example, exact numbers: a layout captured 8 minutes
    before the live boot time renders the §2 fragment verbatim. This subtraction
    was wrong by two hours in one revision and by a full day in another — the
    test exists so it cannot regress silently."""
    captured_at = 1787259000000
    live_boot_time = 1787259480000
    assert (
        health.boot_boundary_text(captured_at, live_boot_time) == "captured 8 min before this boot"
    )


def test_boot_boundary_phrasing_during_the_current_boot():
    """Same `capturedAt` as the prior test, but a `liveBootTime` earlier than it —
    the layout was captured under the boot that is running right now."""
    captured_at = 1787259000000
    live_boot_time = 1787235232500  # §6's measured `LastBootUpTime` (§14 claim 11/20)
    assert health.boot_boundary_text(captured_at, live_boot_time) == "this is your current desk."


def test_boot_boundary_phrasing_exactly_at_the_boundary():
    """`captured_at == live_boot_time` — untested in either direction before this.

    The function's own condition is `captured_at > live_boot_time`, so an exact
    tie takes the "before this boot" branch (0 minutes before), not the
    "current desk" branch. Pinning this locks in which side of the boundary
    wins rather than leaving it to fall out of the `>` vs `>=` choice unnoticed.
    """
    captured_at = 1787259000000
    live_boot_time = 1787259000000
    assert (
        health.boot_boundary_text(captured_at, live_boot_time) == "captured 0 min before this boot"
    )


def test_freshness_uses_last_success_at_not_a_stale_layouts_captured_at():
    """§6: `lastSuccessAt` is the only honest source for freshness. Deriving it
    from the newest layout's `capturedAt` is wrong because dedup means a
    successful run that sees an unchanged desk writes NO new layout — a user who
    leaves the same ten sessions open all weekend, with dedup writing no layout
    for three days, must still be told the tool is healthy. `freshness_report`
    therefore does not even accept a layout: it can only read `last_success_at`.
    """
    now = 1787600000000
    recent_last_success_at = now - 5 * 60_000  # a dedup run 5 minutes ago

    report = health.freshness_report(recent_last_success_at, now)

    assert report == "Last successful snapshot: 5 minutes ago (2026-08-24 19:28)"


def test_freshness_reports_days_ago_when_genuinely_stale():
    """§8's own worked example: "Last successful snapshot: 6 days ago"."""
    now = 1787259000000
    six_days_ms = 6 * 86_400_000
    last_success_at = now - six_days_ms

    report = health.freshness_report(last_success_at, now)

    assert "Last successful snapshot: 6 days ago" in report


def test_consecutive_failures_line_matches_the_section_8_example():
    """§8's own worked example: '47 runs since then failed: "claude.exe running,
    zero readable registry entries"'."""
    line = health.consecutive_failures_report(consecutive_failures=47, error=health.REGISTRY_ERROR)
    assert line == '47 runs since then failed: "claude.exe running, zero readable registry entries"'


def test_consecutive_failures_line_is_absent_when_the_streak_is_zero():
    assert health.consecutive_failures_report(consecutive_failures=0, error=None) is None


def test_freshness_report_when_a_snapshot_has_never_succeeded():
    """A machine that has never run `termsync snap` successfully — `last_success_at`
    is `None`. Untested before this; the function's own `if last_success_at is None`
    branch had no test exercising it."""
    assert health.freshness_report(None, now=1787600000000) == "Last successful snapshot: never"
