"""Health surfacing (§8) — the sole owner of the primary error rule.

Revision 4's rule replaces revision 3's window-based form, which fired every
clean boot (Windows Terminal open, no session started yet) and false-positived
on `desk`'s own armed tabs (a window full of tabs, zero interactive registry
entries). The replacement needs no `EnumWindows` and no UIA — it is available
for free from `-Emit`'s own output — and it is correct in V0 and V1 alike:

    >=1 claude.exe process, zero readable registry entries -> error.

The rule has exactly one owner, this module. It is not implemented in
PowerShell, and no part of it is duplicated in `store.py`.

Every function here is pure and takes plain arguments (process counts, epoch
milliseconds, `Status` fields) rather than calling any Windows API or
subprocess itself, so the whole module is unit-testable on any machine —
that testability is the reason the rule lives in Python at all (§8).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from termsync.model import Outcome

REGISTRY_ERROR = "claude.exe running, zero readable registry entries"


def is_registry_error(claude_process_count: int, readable_entry_count: int) -> bool:
    """The primary error rule (§8): claude running, nothing readable in the registry.

    Deliberately does NOT fire when `claude_process_count == 0`, no matter how many
    (or how few) registry entries are readable. Revision 3's rule fired on every
    clean boot because it keyed off a visible Windows Terminal window instead of a
    live claude.exe process; this rule keys off the process directly so a machine
    that has not started a session yet is never reported as an error.

    `readable_entry_count` is the number of `~/.claude/sessions/*.json` files that
    *parsed* — i.e. `len(emit["sessions"])` from `-Emit`'s output — BEFORE the
    liveness, `procStart` and `kind` filters (§5 steps 5-7) are applied. It is not
    the alive subset, not the alive+procStart-matched subset, and not the
    interactive-only subset: passing any of those makes the rule fire on a
    legitimate state (e.g. every current session happening to be backgrounded).
    `Snapshot-Desk.ps1`'s silent `continue` on a file that fails to parse
    (`snapshot/Snapshot-Desk.ps1:141`) is what defines "readable" here — a file
    that skips there never reaches `emit["sessions"]`, so it never reaches this
    count either.
    """
    return claude_process_count >= 1 and readable_entry_count == 0


def outcome_for_capture(registry_error: bool, title_fallbacks: int) -> Outcome:
    """The outcome a run of `desk snap` writes to `status.json` (§6, §8).

    `degraded` is forced whenever any `AttachConsole` call fell back to the
    registry's derived `name` field, and NEVER downgraded to `ok`. Derived
    placeholders (for example, `demo-project-36`) may hide the session's
    purpose — a desk captured under that fallback is worse than
    Claude Code's own /resume picker, so reporting it as a healthy capture
    would hide the exact failure the freshness header exists to catch.

    The parameter is named `registry_error`, not `is_registry_error`, so it
    does not shadow the module-level `is_registry_error()` function — with the
    old name the two were impossible to use together in one scope (L7).
    """
    if registry_error:
        return "error"
    if title_fallbacks > 0:
        return "degraded"
    return "ok"


def boot_boundary_text(captured_at: int, live_boot_time: int) -> str:
    """§2 / §6: phrase how a layout's capture time relates to the CURRENT boot.

    Computed from the LIVE `LastBootUpTime` at restore, never from the layout's
    stored `bootId` alone — `bootId` only records the boot the capturer was
    running under and is always earlier than its own `capturedAt`, so it cannot
    answer "is this the boot I'm on right now?" on its own.

    If `captured_at > live_boot_time`, the layout was captured during the boot
    that is running right now (§6's "this is your current desk" case). Otherwise
    it renders the "captured N min before this boot" fragment used in §2's
    header line. This exact arithmetic was wrong by two hours in one revision
    of the design and by a full day in another, so the numbers here are the
    ones §6 verified: 1787259000000 captured, 1787259480000 live boot -> 8 min.
    """
    if captured_at > live_boot_time:
        return "this is your current desk."
    minutes = (live_boot_time - captured_at) // 60_000
    return f"captured {minutes} min before this boot"


def freshness_report(
    last_success_at: int | None,
    now: int,
    format_timestamp: Callable[[int], str] | None = None,
) -> str:
    """§8: "Freshness leads every screen, rendered from `lastSuccessAt`... never
    from the newest layout's `capturedAt`."

    Deliberately takes `last_success_at` and NOT a `Layout` — dedup means a
    successful run that leaves an unchanged desk writes no new layout, so a
    layout's `capturedAt` can be days stale while the tool is perfectly healthy.
    There is no parameter here a caller could pass a layout's `capturedAt` into
    by mistake.

    Renders in UTC *by default* so the function stays pure and
    machine-independent, but takes `format_timestamp` so the surface that
    renders a full screen can localise it -- §6: "Local formatting happens only
    at the display layer." Without that seam a screen ends up printing this
    line in UTC next to its own local timestamps, which is how `--dry-run`
    first shipped: "just now (2026-08-23 20:29)" directly above
    "Last desk -- 08-24 10:29", two zones two lines apart.
    """
    if last_success_at is None:
        return "Last successful snapshot: never"
    age_ms = now - last_success_at
    stamp = (format_timestamp or _format_utc)(last_success_at)
    return f"Last successful snapshot: {relative_age_phrase(age_ms)} ({stamp})"


def relative_age(age_ms: int) -> str:
    """Coarse "N unit(s)" phrasing for an age in milliseconds -- no "ago" attached.

    Public because work unit S10's `--dry-run` row rendering (§13.3, §13.4) is
    this function's SECOND caller, for a session's prompt age. Per the
    project's dedup rule ("extract at the second caller, never the first" --
    `patterns_dedup_reuse_discipline`), that second call site promotes this
    from `freshness_report`'s private helper to one shared implementation,
    rather than `cli.py` growing its own second age formatter. `freshness_report`
    above is unchanged: same private-in-effect behaviour, same call site, just
    reached through the public name now.

    Review finding F10/L5 [LOW]: a NEGATIVE `age_ms` -- a backwards clock
    correction, D1's own named failure class ("Python-side clocks drift
    across hibernation") -- used to fall into the `minutes < 1` bucket by
    accident and render "just now", which on this screen reads as "active
    seconds ago". That is not what a negative age means, so it gets its own
    honest word instead. Every existing `--dry-run` test fixture fed this
    function a negative value by accident (`make_row`'s old default
    `last_prompt_at` was far in the future relative to every test's fixed
    `clock`), which is exactly why this bucket went unexercised on a real,
    positive value until the review's mutation sweep (E, F) caught it.
    """
    if age_ms < 0:
        return "age unknown"
    minutes = age_ms // 60_000
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''}"


def relative_age_phrase(age_ms: int) -> str:
    """`relative_age` as a complete English phrase, "ago" included.

    Exists because "ago" is not a suffix every bucket can take: `relative_age`
    returns "just now", which is already a whole phrase, and "just now ago" is
    not English. Neither is "age unknown ago" (F10/L5's negative-age bucket).
    Both surfaces that render an age hit this -- the freshness header and
    `--dry-run`'s rows -- and both shipped the "just now ago" bug
    independently, which is the argument for the phrasing having exactly one
    owner rather than a special case repeated at each call site.
    `relative_age` stays public for callers that want the bare quantity.
    """
    age = relative_age(age_ms)
    return age if age in ("just now", "age unknown") else f"{age} ago"


def _format_utc(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def consecutive_failures_report(consecutive_failures: int, error: str | None) -> str | None:
    """§8: "N runs since then failed: <error>" — the line printed under the
    freshness header once `Status.consecutive_failures` is nonzero.

    Returns `None` when there is nothing to report (`consecutive_failures == 0`),
    so a caller can render this line only when it is present, mirroring the
    optional `!` block in §2's screen.
    """
    if consecutive_failures <= 0:
        return None
    return f'{consecutive_failures} runs since then failed: "{error}"'
