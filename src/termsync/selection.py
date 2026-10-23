"""Restore target selection (§7 step 1) — pure policy, no filesystem and no clock.

This is deliberately *not* in `store.py`. The store owns a durable, crash-safe
sequence of layouts; which of them a restore should offer is the least-settled rule
in the design (§11 open item 9, "what counts as a mass disappearance"), and keeping
it here has two concrete consequences:

- `prune` no longer calls into restore policy to discover what it must protect. The
  caller states it (`prune(now, keep=...)`), so the protection is structural rather
  than an opt-in argument that defaults to protecting nothing.
- The unmeasured mass-disappearance threshold, when it is measured, lands in a
  function that can be replayed against a hand-written series of `Layout` objects —
  which is what "settled by a week of real status.json and layout history" needs.

The mass-disappearance clause itself is **not implemented**; it stays deferred.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from termsync.model import Layout

# How many boots back the candidate set reaches. See `select_target`.
CANDIDATE_BOOT_DEPTH = 2


@dataclass(frozen=True)
class Target:
    """The result of target selection.

    `layout` is the pick. `newest` is always the newest readable layout, offered as
    the alternative (`[n]` in §2) regardless of which one was picked — so the desk
    the user is sitting in front of is reachable even when it is not the pick, and
    the pick is reachable even when it is not the newest.
    """

    layout: Layout
    newest: Layout


def select_target(layouts: Sequence[Layout], boot_id: int | None) -> Target | None:
    """Choose the restore target: the largest layout of the recent boots.

    §7 step 1 says "the largest layout from the current boot". Taken literally that
    rule loses the desk it exists to find. `termsync snap` runs on a 10-minute poll,
    so within ten minutes of any reboot there is a current-boot layout — of a
    freshly-booted, nearly-empty desk. It wins "largest of the current boot"
    uncontested, and because it is also the newest layout it is offered as its own
    `[n]` alternative, leaving the pre-reboot desk unreachable.

    So the candidate set is the current boot **plus the previous boot boundary**:
    the last `CANDIDATE_BOOT_DEPTH` boots present in the series, which is the
    current boot and its predecessor when the current boot has been captured, and
    the two most recent stored boots when it has not (the usual state right after a
    reboot, before the first poll). The largest layout across that set is the pick,
    ties broken by the later version.

    The window stops at the previous boot on purpose: a desk from two boots ago is
    history — the user booted, worked a whole session, and booted again. Widening
    further would offer a desk that a completed session has already superseded.

    **Only `source == "capture"` layouts compete (D20, S6 review finding F1/D20).**
    A `source: "restore"` record is a record of an ACTION — what the user selected
    in the picker, per R12 — not a snapshot of a desk; §7 step 1's "largest layout"
    means captured desks. Before this filter, a restore record could become
    `newest` (it is written with a version number like any other layout), and
    because it deliberately holds fewer sessions than the capture it was chosen
    from (deselecting some of what was offered is the picker's whole purpose), it
    made the shrink guard (§8, `cli._header_lines`) fire "a strict subset, same
    boot" on the very next ordinary restore — the exact cry-wolf failure R12 was
    supposed to have already closed by switching what a restore record stores
    from "offered" to "selected". Recording still happens (`store.write_restore`
    is unchanged); this function is what stops that record from ever being
    compared against as if it were a captured desk. See this function's own test
    file and `docs/TERMSYNC.md` §16 D20 for the reasoning and the measured repro.
    """
    series = sorted(
        (layout for layout in layouts if layout.source == "capture"),
        key=lambda layout: layout.version,
    )
    if not series:
        return None

    newest = series[-1]
    candidate_boots = _recent_boots(series, boot_id)
    candidates = [layout for layout in series if layout.boot_id in candidate_boots]
    pick = max(candidates, key=lambda layout: (len(layout.sessions), layout.version))
    return Target(layout=pick, newest=newest)


def _recent_boots(series: Sequence[Layout], boot_id: int | None) -> frozenset[int | None]:
    """The boots that compete: the current one (if captured) and the one before it.

    Boots are ordered by their most recent layout rather than by `boot_id` value,
    because `boot_id` is a boot *time* and a clock correction could reorder two of
    them; version order is the store's own arrow of time and cannot.

    `boot_id` may be `None` (review finding F3): `None` behaves as an
    ordinary value throughout this function -- as a list/frozenset member
    and as a dict-less equality check -- so every null-boot layout collapses
    into a single pseudo-boot entry in `order`, occupying one slot of the
    window like any other boot rather than being skipped or raising.
    """
    order: list[int | None] = []
    for layout in series:
        if layout.boot_id in order:
            order.remove(layout.boot_id)
        order.append(layout.boot_id)

    if boot_id in order:
        end = order.index(boot_id) + 1
    else:
        end = len(order)
    return frozenset(order[max(0, end - CANDIDATE_BOOT_DEPTH) : end])
