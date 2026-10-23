"""Behaviour tests for the layout store.

These exercise Store's public interface only. Nothing here reaches into the
on-disk format directly except the tests that deliberately corrupt a file to
simulate a crash mid-write — that corruption is the behaviour under test.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from termsync.model import Layout, Pending, PendingEntry, Session, normalize_registry_versions
from termsync.selection import select_target
from termsync.store import Store, layout_hash

DAY_MS = 86_400_000


def test_old_status_file_without_timing_fields_still_loads(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.status_path.write_text(json.dumps({
        "ranAt": 1000, "outcome": "ok", "sessionsSeen": 1,
        "wroteVersion": 1, "lastSuccessAt": 1000,
    }), encoding="utf-8")
    status = store.read_status()
    assert status is not None
    assert status.ran_at == status.last_success_at == 1000
    assert status.started_at is status.finished_at is status.duration_ms is None


@pytest.mark.parametrize("finish,duration", [(1350, 350), (900, -100)])
def test_record_run_round_trips_timing_without_changing_freshness(
    tmp_path: Path, finish: int, duration: int
) -> None:
    store = Store(tmp_path)
    status = store.record_run(
        ran_at=1000, outcome="ok", sessions_seen=1, wrote_version=None,
        started_at=1000, finished_at=finish,
    )
    assert status.started_at == 1000
    assert status.finished_at == finish
    assert status.duration_ms == duration
    assert status.last_success_at == status.ran_at == 1000
    assert store.read_status() == status
    raw = json.loads(store.status_path.read_text(encoding="utf-8"))
    assert (raw["startedAt"], raw["finishedAt"], raw["durationMs"]) == (1000, finish, duration)
    failure = store.record_run(
        ran_at=2000, outcome="error", sessions_seen=0, wrote_version=None,
        started_at=2000, finished_at=2100, error="test error",
    )
    assert failure.last_success_at == 1000
    assert (failure.started_at, failure.finished_at, failure.duration_ms) == (2000, 2100, 100)
    assert failure.consecutive_failures == 1


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def sessions(*specs):
    """Build sessions from (id, title) pairs, with a shared cwd."""
    return [
        Session(
            session_id=sid, cwd=r"C:\fixture\demo_project", title=title, last_prompt_at=None
        )
        for sid, title in specs
    ]


def test_the_stored_hash_is_the_section_6_formula_under_the_section_6_label():
    """L2: §6's example stores `"hash": "sha256:<hash>"`; the code stored bare hex.

    U7 adds placement — this pins both halves together so a future reader
    cannot satisfy one and miss the other. Anything comparing hashes across the
    boundary sees one extra allocated version, once, and never again.
    """
    import hashlib

    desk = [
        Session(session_id="b0000002", cwd=r"D:\code", title="Demo module"),
        Session(session_id="a0000001", cwd=r"C:\fixture\demo_project", title="Project plan"),
    ]
    documented = "\n".join(
        sorted(
            [
                r"claude|b0000002|D:\code|0|None|None",
                r"claude|a0000001|C:\fixture\demo_project|0|None|None",
            ]
        )
    )

    result = layout_hash(desk)

    assert result.startswith("sha256:"), "§6's stored form is labelled with its algorithm"
    assert result == "sha256:" + hashlib.sha256(documented.encode("utf-8")).hexdigest()


def test_the_hash_ignores_titles_but_not_arming():
    """§6: titles are AI-generated and evolve, so they are out; `armed` is in."""
    plain = [Session(session_id="a", cwd=r"D:\code", title="one")]
    retitled = [Session(session_id="a", cwd=r"D:\code", title="two")]
    armed = [Session(session_id="a", cwd=r"D:\code", title="one", armed=True)]

    assert layout_hash(plain) == layout_hash(retitled)
    assert layout_hash(plain) != layout_hash(armed)


def test_the_layout_contract_round_trips_every_field_section_6_specifies(store):
    """L1: `windowCount`, `windowOrdinal` and `tabIndex` are in §6's layout example.

    `model.py` calls itself "the JSON contract", so a spec-shaped layout must
    survive `from_json` -> `to_json` unchanged. Dropping fields silently is the
    worst shape for a contract module: V1 populates these, and a V0 read/rewrite
    cycle would erase them from a persisted layout with nothing to notice.
    """
    spec_shaped = {
        "version": 42,
        "source": "capture",
        "capturedAt": 1787259000000,
        "bootId": 1787235232500,
        "hash": "sha256:deadbeef",
        "registryVersions": ["2.1.238", "2.1.239"],
        "sessions": [
            {
                "sessionId": "a0000001",
                "cwd": r"C:\fixture\demo_project",
                "title": "Project plan phase 3 deep review",
                "lastPromptAt": 1787251800000,
                "armed": False,
                "windowOrdinal": 1,
                "tabIndex": 3,
                "agent": "claude",
                "archived": False,
                "rolloutPath": None,
                "resumeArgs": [],
            }
        ],
        "windowCount": 1,
    }

    round_tripped = Layout.from_json(spec_shaped).to_json()

    assert round_tripped == spec_shaped


def test_the_v1_only_fields_default_to_null_rather_than_being_absent(store):
    """V0 does not know window ordinals (§15 excludes EnumWindows and UIA), so it
    writes them as null — present in the shape, empty in the value."""
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )

    on_disk = json.loads((store.layouts_dir / "000001.json").read_text(encoding="utf-8"))

    assert on_disk["windowCount"] is None
    assert on_disk["sessions"][0]["windowOrdinal"] is None
    assert on_disk["sessions"][0]["tabIndex"] is None


def test_status_carries_windows_seen(store):
    """§6 and §8 both list `windowsSeen` in the `status.json` shape, "null in V0 and
    populated in V1". S3 writes this file; the field has to exist to be written."""
    store.record_run(ran_at=1, outcome="ok", sessions_seen=10, wrote_version=1)
    assert json.loads(store.status_path.read_text(encoding="utf-8"))["windowsSeen"] is None

    store.record_run(ran_at=2, outcome="ok", sessions_seen=10, wrote_version=None, windows_seen=2)

    on_disk = json.loads(store.status_path.read_text(encoding="utf-8"))
    assert on_disk["windowsSeen"] == 2
    assert store.read_status().windows_seen == 2


def test_a_captured_layout_can_be_read_back(store):
    written = store.write_capture(
        sessions=sessions(("a0000001", "Project plan phase 3 deep review")),
        captured_at=1787259000000,
        boot_id=1787235232500,
        registry_versions=["2.1.238"],
    )

    assert written.allocated is True
    newest = store.newest()

    assert newest.version == written.layout.version
    assert newest.captured_at == 1787259000000
    assert newest.boot_id == 1787235232500
    assert [s.session_id for s in newest.sessions] == ["a0000001"]
    assert newest.sessions[0].title == "Project plan phase 3 deep review"


def test_a_write_goes_through_tmp_fsync_replace_and_leaves_no_stray_files(store, monkeypatch):
    """Atomic writes go to ~/.termsync/tmp/ first, fsync, then os.replace into place.

    This proves the mechanism, not just the end state: `os.replace` must be called
    with a source path inside tmp/, `os.fsync` must be called before it, and no
    leftover temp file may remain once the write returns.
    """
    import os as os_module

    calls = []
    real_replace = os_module.replace
    real_fsync = os_module.fsync

    def spy_fsync(fd):
        calls.append(("fsync", fd))
        return real_fsync(fd)

    def spy_replace(src, dst):
        calls.append(("replace", str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "fsync", spy_fsync)
    monkeypatch.setattr(os_module, "replace", spy_replace)

    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )

    kinds = [c[0] for c in calls]
    assert "fsync" in kinds, "write must fsync before replacing into place"
    assert "replace" in kinds, "write must use os.replace, not a plain write"
    assert kinds.index("fsync") < kinds.index("replace"), "fsync must precede replace"

    replace_call = next(c for c in calls if c[0] == "replace")
    tmp_dir = store.root / "tmp"
    assert str(tmp_dir) in replace_call[1], "replace source must come from tmp/"
    assert replace_call[2].endswith("000001.json")

    leftover = list(tmp_dir.glob("*")) if tmp_dir.is_dir() else []
    assert leftover == [], f"stray temp files left behind: {leftover}"

    layout_path = store.layouts_dir / "000001.json"
    assert layout_path.is_file()
    import json

    json.loads(layout_path.read_text(encoding="utf-8"))  # must parse cleanly


def test_a_claimed_version_number_is_walked_past_not_clobbered(store):
    """A scheduled capture and an interactive restore could race for version N.

    The loser of the race must retry the *next* number rather than overwrite the
    winner's layout. That retry loop is the behaviour worth testing; asserting that
    `_claim_version` raises `FileExistsError` — which the previous version of this
    test did — only restates what `O_EXCL` means and never exercises the loop.
    """
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1, boot_id=1)
    store._claim_version(2)  # another writer got there first and is still writing

    written = store.write_capture(sessions=sessions(("b", "t")), captured_at=2, boot_id=1)

    assert written.layout.version == 3, "must skip the claimed number, not reuse it"
    assert store.newest().version == 3
    assert store.newest().sessions[0].session_id == "b"


def test_completed_competing_write_cannot_be_overwritten_by_stale_allocation(store, monkeypatch):
    store.write_capture(sessions=sessions(("initial", "t")), captured_at=1, boot_id=1)
    competitor = Store(store.root)
    claim = store._claim_version
    competitor_bytes = None

    def finish_competitor_before_claim(version):
        nonlocal competitor_bytes
        if version == 2:
            competitor.write_restore(
                sessions=sessions(("competitor", "t")), restored_at=2, boot_id=1
            )
            competitor_bytes = (store.layouts_dir / "000002.json").read_bytes()
        return claim(version)

    # The contender has already selected 2 from its directory snapshot. The
    # other writer completes and removes its marker before this claim starts.
    monkeypatch.setattr(store, "_claim_version", finish_competitor_before_claim)
    written = store.write_capture(sessions=sessions(("contender", "t")), captured_at=3, boot_id=1)

    assert written.layout.version == 3
    assert (store.layouts_dir / "000002.json").read_bytes() == competitor_bytes
    assert [layout.sessions[0].session_id for layout in store.all_layouts()] == [
        "initial", "competitor", "contender"
    ]
    assert store._claim_paths() == []


def test_a_crash_between_claiming_and_writing_leaves_no_permanent_warning(store):
    """M6: the claim must not be a zero-byte file at the layout's own path.

    Dying between the claim and the write is the exact class of event this tool
    exists for. A placeholder left at `layouts/NNNNNN.json` is unparseable forever,
    so §6 rule 3's "layout 000043 is unreadable (truncated write?)" warning fires on
    every run from then on, for a file that contains no lost data at all — and §8's
    whole argument is that a warning the user always sees is one they stop reading.
    Pruning cannot remove it either, because it never parses into a layout.
    """
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1787259000000, boot_id=1)
    store._claim_version(2)  # ... and now the machine dies

    newest = store.newest()

    assert newest.version == 1
    assert store.read_warnings == [], "a claim is not corruption and must not warn"
    assert not (store.layouts_dir / "000002.json").exists(), (
        "the claim must live outside the layout namespace"
    )
    assert store.prune(now=1787259000000, keep=frozenset()) == []


def test_a_zero_byte_layout_file_is_reclaimed_rather_than_warned_about_forever(store):
    """A store written by an older build can already hold such a stub.

    A zero-byte file carries no lost data, so it is reclaimable: no warning, and
    pruning removes it. A non-empty unparseable file is real corruption and keeps
    warning — that distinction is the point, and the truncated-layout test below
    holds the other side of it.
    """
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1787259000000, boot_id=1)
    stub = store.layouts_dir / "000002.json"
    stub.touch()

    newest = store.newest()

    assert newest.version == 1
    assert store.read_warnings == [], "an empty stub is not corruption"

    store.prune(now=1787259000000, keep=frozenset())

    assert not stub.exists(), "a zero-byte stub must be reclaimable"


def test_all_layouts_is_public_and_hands_back_the_whole_series_in_version_order(store):
    """Target selection lives outside the store, so the series must be reachable.

    While `_all_layouts` was private, the only view of the store was `newest()` and
    whatever `Store.target()` chose to expose — which is why a layout the selection
    rule declined to pick could not be reached by any caller at all.
    """
    for i in range(3):
        store.write_capture(
            sessions=sessions(*[(f"s{i}_{j}", "t") for j in range(i + 1)]),
            captured_at=1787259000000 + i * 600_000,
            boot_id=1787235232500,
        )

    series = store.all_layouts()

    assert [layout.version for layout in series] == [1, 2, 3]
    assert [len(layout.sessions) for layout in series] == [1, 2, 3]


def test_the_desk_from_before_a_reboot_is_reachable_through_public_methods(store):
    """H2, end to end: store.all_layouts() -> select_target() finds the big desk.

    The store no longer decides this; it only has to make the series visible. This
    is the assertion that failed before the seam moved: with target selection
    inside the store and the series private, the 10-session desk was unreachable.
    """
    old_boot = 1787000000000
    current_boot = 1787235232500
    store.write_capture(
        sessions=sessions(*[(f"s{i}", f"title{i}") for i in range(10)]),
        captured_at=1787259000000,
        boot_id=old_boot,
    )
    store.write_capture(
        sessions=sessions(("s0", "title0"), ("s1", "title1")),
        captured_at=1787259300000,
        boot_id=old_boot,
    )
    store.write_capture(
        sessions=sessions(("s0", "title0")),
        captured_at=1787259900000,
        boot_id=current_boot,
    )

    result = select_target(store.all_layouts(), boot_id=current_boot)

    assert result.layout.version == 1, "the 10-session desk must be the pick"
    assert len(result.layout.sessions) == 10
    assert result.newest.version == 3, "the newest layout is still the [n] alternative"


def test_dedup_writes_no_new_version_and_refreshes_titles_in_place(store):
    """Same session set (same hash) -> no new version, but titles still refresh.

    Titles are excluded from the hash (they are AI-generated and evolve), so a
    capture with only changed titles must not allocate a new version. It must
    instead rewrite the existing newest layout's titles atomically in place.
    """
    first = store.write_capture(
        sessions=sessions(("a0000001", "Project plan phase 3")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )

    second = store.write_capture(
        sessions=sessions(("a0000001", "Project plan phase 4 - renamed")),
        captured_at=1787259600000,
        boot_id=1787235232500,
    )

    assert second.layout.version == first.layout.version, (
        "a title-only change must not allocate a new version"
    )
    assert store._layout_paths() == [store.layouts_dir / "000001.json"]

    newest = store.newest()
    assert newest.sessions[0].title == "Project plan phase 4 - renamed"


def test_a_write_says_whether_it_allocated_a_version(store):
    """M1: the caller cannot compute `wroteVersion` unless the store tells it.

    §6 makes `wroteVersion` the record of whether this run allocated a version, and
    §13.4 tests that a dedup run still advances `lastSuccessAt` *because* it wrote
    nothing. With a plain `Layout` back from both branches the only way to tell was
    to compare `captured_at` against what was passed in — an inference off a field
    the dedup branch happened not to refresh, which is now refreshed (M2).
    """
    allocated = store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )

    assert allocated.allocated is True
    assert allocated.wrote_version == 1
    assert allocated.layout.version == 1

    deduped = store.write_capture(
        sessions=sessions(("a0000001", "Project plan - renamed")),
        captured_at=1787259600000,
        boot_id=1787235232500,
    )

    assert deduped.allocated is False, "an unchanged session set allocates nothing"
    assert deduped.wrote_version is None, "wroteVersion must be null on a dedup run"
    assert deduped.layout.version == 1, "the refreshed layout still comes back to the caller"
    assert deduped.layout.sessions[0].title == "Project plan - renamed"


def test_dedup_refreshes_the_capture_timestamp_so_the_young_session_boundary_moves(store):
    """M2(b): `capturedAt` means "this desk, as of now", not "first sighted at".

    §7 step 3 uses `capturedAt` as the boundary for "Started since the last
    snapshot". Freezing it on dedup breaks that: the user leaves the same ten
    sessions open Friday 18:00 to Monday, dedup writes no new version all weekend,
    and Monday's restore screen computes "sessions with a prompt after Friday
    18:00" — which is all ten sessions the user is already looking at. Every open
    session then appears twice, once as a restorable row and once as young.
    """
    friday = 1787259000000
    monday = friday + 3 * DAY_MS
    desk = sessions(("a0000001", "Project plan"))

    store.write_capture(sessions=desk, captured_at=friday, boot_id=1787235232500)
    weekend = store.write_capture(sessions=desk, captured_at=monday, boot_id=1787300000000)

    assert weekend.allocated is False, "the desk did not change, so no version is allocated"
    assert weekend.layout.captured_at == monday, "the young-session boundary must advance"
    assert weekend.layout.boot_id == 1787300000000, "the boot it was last seen under"
    assert store.newest().captured_at == monday, "and it must be on disk, not just returned"


def test_a_capture_never_deduplicates_into_a_restore_record(store):
    """M2(a): the shrink guard's reference record must not be rewritten by a capture.

    Right after a restore the hash matches by construction — `armed` is in the hash
    and nothing has changed yet — so the next capture would dedup straight into the
    `source: "restore"` record and rewrite it. §8's shrink guard compares against
    "the most recent restore record (which holds what the user *selected*)"; that
    reference cannot be a record a different process mutates.
    """
    desk = sessions(("a0000001", "Project plan"))
    restore_record = store.write_restore(
        sessions=desk, restored_at=1787259480000, boot_id=1787235232500
    )

    capture = store.write_capture(
        sessions=sessions(("a0000001", "NEW TITLE")),
        captured_at=1787260000000,
        boot_id=1787235232500,
    )

    assert capture.allocated is True, "a capture must allocate rather than rewrite a restore"
    assert capture.layout.version != restore_record.layout.version
    assert capture.layout.source == "capture"

    kept = next(
        layout for layout in store.all_layouts() if layout.version == restore_record.layout.version
    )
    assert kept.source == "restore"
    assert kept.captured_at == 1787259480000, "the restore record must be untouched"
    assert kept.sessions[0].title == "Project plan"

    # and the follow-up capture dedups normally against the capture record
    again = store.write_capture(
        sessions=sessions(("a0000001", "NEWER TITLE")),
        captured_at=1787260600000,
        boot_id=1787235232500,
    )
    assert again.allocated is False, "only the source change forces an allocation, not every run"


def test_dedup_never_rewrites_a_layout_that_is_not_the_highest_version_on_disk(store):
    """A crash-damaged newest layout must not turn dedup into history rewriting.

    `newest()` deliberately falls back past an unreadable layout. Deduping against
    that fallback would rewrite an *older* version with a newer timestamp while a
    higher version number still sits on disk — mutating a record the user may still
    be shown, to save one file.
    """
    desk = sessions(("a0000001", "Project plan"))
    store.write_capture(sessions=desk, captured_at=1787259000000, boot_id=1787235232500)
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan"), ("b0000002", "Demo module")),
        captured_at=1787259600000,
        boot_id=1787235232500,
    )
    victim = store.layouts_dir / "000002.json"
    victim.write_text(victim.read_text(encoding="utf-8")[:40], encoding="utf-8")

    result = store.write_capture(sessions=desk, captured_at=1787260000000, boot_id=1787235232500)

    assert result.allocated is True, "must allocate rather than rewrite v1 behind a damaged v2"
    assert result.layout.version == 3
    first = next(layout for layout in store.all_layouts() if layout.version == 1)
    assert first.captured_at == 1787259000000, "the older layout must be untouched"


def test_write_restore_stores_what_was_selected_not_what_was_offered(store):
    """§6 `source`: a restore record holds what the user SELECTED.

    Storing the offered set would make the shrink guard fire on every ordinary
    restore, because deselecting is the picker's whole point. write_restore's
    signature takes only the selected sessions -- there is no "offered" param
    to pass by mistake -- and the resulting layout must reflect only those.
    """
    offered = sessions(*[(f"s{i}", f"title{i}") for i in range(10)])
    selected = offered[:6]

    written = store.write_restore(
        sessions=selected,
        restored_at=1787259480000,
        boot_id=1787235232500,
    )

    assert written.layout.source == "restore"
    assert [s.session_id for s in written.layout.sessions] == [f"s{i}" for i in range(6)]
    assert written.layout.hash == layout_hash(selected)

    newest = store.newest()
    assert len(newest.sessions) == 6


def test_status_round_trips_and_a_real_dedup_run_still_advances_last_success_at(store):
    """§6 `lastSuccessAt`: a run that dedups (writes no new layout) is still success.

    Deriving `lastSuccessAt` from the newest layout's `capturedAt` is wrong,
    because dedup means an unchanged desk writes no layout at all -- a user who
    leaves the same ten sessions open all weekend would wrongly be told the
    tool has not run in days. `lastSuccessAt` must advance on every ok run,
    version written or not.

    The second run here performs an actual dedup and feeds the store's own answer
    into `record_run`. The previous version of this test hand-fed `wrote_version=
    None` -- the one value the store could not compute -- so it papered over M1
    instead of catching it.
    """
    desk = sessions(*[(f"s{i}", f"title{i}") for i in range(10)])

    first_write = store.write_capture(
        sessions=desk, captured_at=1787259000000, boot_id=1787235232500
    )
    first = store.record_run(
        ran_at=1787259000000,
        outcome="ok",
        sessions_seen=10,
        wrote_version=first_write.wrote_version,
        registry_versions=["2.1.238"],
    )
    assert first.wrote_version == 1
    assert first.last_success_at == 1787259000000

    read_back = store.read_status()
    assert read_back == first

    # ten minutes later the same desk is still open: a real dedup, no new version
    second_write = store.write_capture(
        sessions=desk, captured_at=1787259600000, boot_id=1787235232500
    )
    second = store.record_run(
        ran_at=1787259600000,
        outcome="ok",
        sessions_seen=10,
        wrote_version=second_write.wrote_version,
        registry_versions=["2.1.238"],
    )

    assert len(store._layout_paths()) == 1, "the dedup run must not have allocated a version"
    assert second.wrote_version is None, "wroteVersion is null when nothing was allocated"
    assert second.last_success_at == 1787259600000, "dedup must still advance lastSuccessAt"


def test_recording_a_run_survives_a_status_file_it_cannot_read(store):
    """M3: the error-recording path must not be the path that dies.

    `record_run` reads the previous status first, and it is the *only* way any
    outcome — including `outcome: "error"` — is ever recorded. If that read can
    raise, one unreadable `status.json` makes every subsequent `termsync snap`
    throw and write nothing, so the freshness header goes permanently silent
    instead of red — §8's primary trust surface, dark exactly when it matters.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    store.status_path.write_text('{"ranAt": 1787259000000, "outcome"', encoding="utf-8")

    recorded = store.record_run(
        ran_at=1787259600000, outcome="error", sessions_seen=0, wrote_version=None, error="boom"
    )

    assert recorded.outcome == "error"
    assert recorded.consecutive_failures == 1, "an unreadable history counts as no history"
    assert recorded.last_success_at is None
    assert any("status.json" in w for w in store.read_warnings), (
        "degrading silently would hide the corruption entirely"
    )
    assert store.read_status() == recorded, "and the run must have left a readable record"


def test_a_status_file_from_a_different_version_does_not_raise(store):
    """A `status.json` missing a key that is required today is the same class of
    problem as a truncated one: both dependencies auto-update (§8)."""
    store.root.mkdir(parents=True, exist_ok=True)
    store.status_path.write_text('{"outcome": "ok", "sessionsSeen": 3}', encoding="utf-8")

    assert store.read_status() is None
    assert any("status.json" in w for w in store.read_warnings)

    recovered = store.record_run(ran_at=5, outcome="ok", sessions_seen=3, wrote_version=None)
    assert recovered.last_success_at == 5


def test_consecutive_failures_resets_on_success_and_increments_on_error(store):
    store.record_run(ran_at=1, outcome="error", sessions_seen=0, wrote_version=None, error="boom")
    second = store.record_run(
        ran_at=2, outcome="error", sessions_seen=0, wrote_version=None, error="boom again"
    )
    assert second.consecutive_failures == 2
    assert second.last_success_at is None, "no success has ever happened yet"

    recovered = store.record_run(ran_at=3, outcome="ok", sessions_seen=5, wrote_version=1)
    assert recovered.consecutive_failures == 0
    assert recovered.last_success_at == 3

    failed_again = store.record_run(
        ran_at=4, outcome="error", sessions_seen=0, wrote_version=None, error="boom"
    )
    assert failed_again.consecutive_failures == 1, "must count from the reset, not accumulate"
    assert failed_again.last_success_at == 3, "must keep the last real success, not clear it"


def test_pending_round_trips_through_the_exact_json_shape_section_6_specifies(store):
    """§6 `pending.json`: `{writtenAt, entries:[{sessionId, title, cwd, armedAt}]}`.

    S3 (§5 step 9) and §7 step 2 both read this file, and §13.6 names it as the
    mechanism four separate correctness properties depend on. The key names are a
    contract, so they are asserted on disk rather than only through the dataclass.
    """
    pending = Pending(
        written_at=1787259480000,
        entries=(
            PendingEntry(
                session_id="a0000001",
                title="Project plan phase 3 deep review",
                cwd=r"C:\fixture\demo_project",
                armed_at=1787259480000,
            ),
        ),
    )

    store.write_pending(pending)

    on_disk = json.loads((store.root / "pending.json").read_text(encoding="utf-8"))
    assert on_disk["writtenAt"] == 1787259480000
    assert on_disk["entries"] == [
        {
            "sessionId": "a0000001",
            "title": "Project plan phase 3 deep review",
            "cwd": r"C:\fixture\demo_project",
            "armedAt": 1787259480000,
                "agent": "claude",
                "archived": False,
                "rolloutPath": None,
                "resumeArgs": [],
        }
    ]
    assert store.read_pending() == pending
    assert store.read_pending().session_ids == frozenset({"a0000001"})


def test_reading_pending_before_any_restore_is_not_an_error(store):
    """No restore has ever armed a tab, so there is no file. That is a normal state."""
    assert store.read_pending() is None


@pytest.mark.parametrize("operation", ["expire", "merge"])
def test_pending_updates_serialize_across_processes(store, operation):
    now = 2 * DAY_MS
    store.write_pending(Pending(0, (PendingEntry("old", "t", "c", 0),)))
    script = """
import sys
from pathlib import Path
from termsync.model import Pending, PendingEntry
from termsync.store import Store
class PausedStore(Store):
    def read_pending(self):
        result = super().read_pending()
        print('read', flush=True)
        assert sys.stdin.readline().strip() == 'release'
        return result
root, operation, pause, identity, now = sys.argv[1:]
store = (PausedStore if pause == 'yes' else Store)(Path(root))
print('started', flush=True)
if operation == 'expire':
    store.expire_pending(int(now), frozenset())
else:
    store.merge_pending(Pending(int(now), (PendingEntry(identity, 't', 'c', int(now)),)))
print('done', flush=True)
"""
    children = []

    def start(action, pause, identity):
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(store.root), action, pause,
             identity, str(now)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        children.append(child)
        assert child.stdout.readline().strip() == "started"
        return child

    try:
        first = start(operation, "yes", "first")
        assert first.stdout.readline().strip() == "read"
        second = start("merge", "no", "second")
        with pytest.raises(subprocess.TimeoutExpired):
            second.communicate(timeout=0.2)
        first_out, first_err = first.communicate("release\n", timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        assert first.returncode == 0, first_err
        assert second.returncode == 0, second_err
        assert "done" in first_out and "done" in second_out
        expected = {"second"} if operation == "expire" else {"old", "first", "second"}
        assert store.read_pending().session_ids == expected
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)


def test_pending_lock_is_bounded_and_released_after_process_death(store):
    script = """
import sys
from pathlib import Path
from termsync.store import Store
with Store(Path(sys.argv[1]))._pending_transaction():
    print('locked', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(store.root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError, match="pending armed sessions"):
            with store._pending_transaction(timeout_s=0.05):
                pytest.fail("A second process must not enter the transaction")
    finally:
        child.kill()
        child.communicate(timeout=10)
    pending = Pending(1, (PendingEntry("new", "t", "c", 1),))
    assert store.merge_pending(pending) == pending
    assert (store.root / ".pending.lock").is_file()


def test_pending_merge_rearms_only_matching_provider_identity(store):
    store.write_pending(Pending(1, (
        PendingEntry("same", "Claude", "c", 1),
        PendingEntry("same", "Codex", "c", 1, agent="codex"),
    )))
    result = store.merge_pending(Pending(2, (PendingEntry("same", "Rearmed", "c", 2),)))
    assert [(e.agent, e.title, e.armed_at) for e in result.entries] == [
        ("claude", "Rearmed", 2), ("codex", "Codex", 1)
    ]


def test_an_unreadable_pending_file_degrades_to_none_with_a_warning(store):
    """Same tolerance as a truncated layout: `pending.json` is written atomically
    but read by a path that must never take the whole run down with it."""
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "pending.json").write_text('{"writtenAt": 178725948', encoding="utf-8")

    assert store.read_pending() is None
    assert any("pending.json" in w for w in store.read_warnings)


def test_pending_is_written_through_the_atomic_path(store, monkeypatch):
    """§6 crash-atomicity rule 1 names `pending.json` explicitly."""
    import os as os_module

    calls = []
    real_replace = os_module.replace

    def spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os_module, "replace", spy_replace)

    store.write_pending(Pending(written_at=1, entries=()))

    assert calls, "pending.json must not be written in place"
    assert str(store.tmp_dir) in calls[0][0]
    assert calls[0][1].endswith("pending.json")


def test_expiring_pending_clears_entries_that_came_back_to_life(store):
    """§5 step 9: an armed tab is pending until the session appears live."""
    store.write_pending(
        Pending(
            written_at=1787259480000,
            entries=(
                PendingEntry("resumed", "t", "c", armed_at=1787259480000),
                PendingEntry("still-armed", "t", "c", armed_at=1787259480000),
            ),
        )
    )

    remaining = store.expire_pending(
        now=1787259480000 + 60_000, live_session_ids=frozenset({"resumed"})
    )

    assert remaining.session_ids == frozenset({"still-armed"})
    assert store.read_pending().session_ids == frozenset({"still-armed"})


def test_expiring_pending_drops_entries_older_than_a_day_and_keeps_younger_ones(store):
    """§5 step 9's other half: "or after 24 hours". An armed tab the user never
    resumed must not suppress that session from the restore list forever."""
    now = 1787259480000
    store.write_pending(
        Pending(
            written_at=now - 2 * DAY_MS,
            entries=(
                PendingEntry("stale", "t", "c", armed_at=now - DAY_MS - 1),
                PendingEntry("fresh", "t", "c", armed_at=now - DAY_MS + 1),
            ),
        )
    )

    remaining = store.expire_pending(now=now, live_session_ids=frozenset())

    assert remaining.session_ids == frozenset({"fresh"})


def test_expiring_the_last_pending_entry_removes_the_file(store):
    """ "No armed tabs" is the absence of the file, not an empty list on disk --
    so §7 step 2's registry-union has exactly one representation of nothing."""
    now = 1787259480000
    store.write_pending(
        Pending(written_at=now, entries=(PendingEntry("gone", "t", "c", armed_at=now),))
    )

    assert store.expire_pending(now=now, live_session_ids=frozenset({"gone"})) is None
    assert not (store.root / "pending.json").is_file()
    assert store.read_pending() is None


def test_expiring_pending_writes_nothing_when_nothing_expired(store, monkeypatch):
    """The 10-minute poll calls this every run; an unchanged set must not rewrite."""
    import os as os_module

    now = 1787259480000
    store.write_pending(
        Pending(written_at=now, entries=(PendingEntry("armed", "t", "c", armed_at=now),))
    )

    calls = []
    real_replace = os_module.replace
    monkeypatch.setattr(
        os_module, "replace", lambda src, dst: (calls.append(dst), real_replace(src, dst))[1]
    )

    unchanged = store.expire_pending(now=now + 60_000, live_session_ids=frozenset())

    assert unchanged.session_ids == frozenset({"armed"})
    assert calls == [], "an unchanged pending set must not be rewritten"


def test_prune_keeps_everything_within_30_days(store):
    """§6 pruning: keep every version for 30 days."""
    now = 1_800_000_000_000
    for i in range(5):
        store.write_capture(
            sessions=sessions((f"s{i}", f"title{i}")),
            captured_at=now - i * DAY_MS,  # 0..4 days old
            boot_id=1,
        )

    pruned = store.prune(now=now, keep=frozenset())

    assert pruned == []
    assert len(store._layout_paths()) == 5


def test_prune_keeps_one_per_day_after_30_days_then_deletes_after_a_year(store):
    """§6 pruning: one per day from 30 days to a year, nothing past a year.

    M4: the survivor of a day is the *largest* desk of that day, not the last one.
    §8 says what the last capture of a day usually is — "an ordinary evening where
    he closes windows as he finishes, making the last snapshot the *tidied* desk
    rather than the working one" — so keeping the latest systematically destroys
    the layouts §7 step 1's largest-of rule exists to find.

    The two same-day layouts here are deliberately ordered *against* each other:
    the working desk is the earlier and larger one, the tidied desk the later and
    smaller. Under "keep the latest" this test goes red, which the previous version
    (where the later capture was also the larger) could not do.
    """
    now = 1_800_000_000_000
    # two captures on the same day, 45 days ago (within the one-per-day band):
    # the working desk at 20:00, then the tidied desk at 23:00
    old_day_working = store.write_capture(
        sessions=sessions(*[(f"w{i}", "t") for i in range(10)]),
        captured_at=now - 45 * DAY_MS,
        boot_id=1,
    )
    old_day_tidied = store.write_capture(
        sessions=sessions(("w0", "t")),
        captured_at=now - 45 * DAY_MS + 3 * 3600_000,
        boot_id=1,
    )
    # a capture 400 days ago -- past the one-year cutoff, must go entirely
    ancient = store.write_capture(
        sessions=sessions(("a", "t"), ("b", "t"), ("c", "t")),
        captured_at=now - 400 * DAY_MS,
        boot_id=1,
    )
    # something recent to anchor "newest" away from the pruning candidates
    recent = store.write_capture(
        sessions=sessions(("z", "t")), captured_at=now - 1 * DAY_MS, boot_id=1
    )

    pruned = store.prune(now=now, keep=frozenset())

    remaining_versions = {int(p.stem) for p in store._layout_paths()}
    assert old_day_working.layout.version in remaining_versions, (
        "keep the largest desk of the day -- the working one, not the tidied one"
    )
    assert old_day_tidied.layout.version not in remaining_versions, (
        "drop the smaller same-day capture even though it is the later one"
    )
    assert ancient.layout.version not in remaining_versions, "past a year -- gone entirely"
    assert recent.layout.version in remaining_versions
    assert set(pruned) == {old_day_tidied.layout.version, ancient.layout.version}


def test_prune_breaks_a_same_size_day_tie_on_the_later_capture(store):
    """Size decides; time only breaks ties. Two equal-sized captures on one day
    leave the later one, which is the fresher record of the same desk."""
    now = 1_800_000_000_000
    early = store.write_capture(
        sessions=sessions(("a", "t"), ("b", "t")), captured_at=now - 45 * DAY_MS, boot_id=1
    )
    late = store.write_capture(
        sessions=sessions(("c", "t"), ("d", "t")),
        captured_at=now - 45 * DAY_MS + 3600_000,
        boot_id=1,
    )

    pruned = store.prune(now=now, keep=frozenset())

    assert pruned == [early.layout.version]
    assert {int(p.stem) for p in store._layout_paths()} == {late.layout.version}


def test_prune_keeps_exactly_what_the_caller_names_and_nothing_by_accident(store):
    """`keep` is stated by the caller, so protection cannot be forgotten silently.

    The old signature took an optional `boot_id` and protected the restore target
    only when one was passed: the shorter, more natural call `prune(now)` protected
    nothing, and two of the three prune tests used exactly that call. Requiring
    `keep` makes the omission impossible to write.
    """
    now = 1_800_000_000_000
    boot = 42
    # the pick: large, from the current boot, but old enough to be prune bait
    store.write_capture(
        sessions=sessions(*[(f"s{i}", "t") for i in range(10)]),
        captured_at=now - 400 * DAY_MS,
        boot_id=boot,
    )
    # a smaller, newer capture from the same boot (the [n] alternative)
    store.write_capture(
        sessions=sessions(("s0", "t")),
        captured_at=now - 399 * DAY_MS,
        boot_id=boot,
    )

    selection = select_target(store.all_layouts(), boot_id=boot)
    keep = frozenset({selection.layout.version, selection.newest.version})
    assert keep == {1, 2}

    pruned = store.prune(now=now, keep=keep)

    remaining_versions = {int(p.stem) for p in store._layout_paths()}
    assert pruned == []
    assert remaining_versions == {1, 2}, "neither the pick nor the alternative may be pruned"


def test_prune_cannot_be_called_without_saying_what_survives(store):
    """M5: the unsafe call must not exist. `keep` has no default."""
    store.write_capture(
        sessions=sessions(("s0", "t")),
        captured_at=1_800_000_000_000 - 400 * DAY_MS,
        boot_id=42,
    )

    with pytest.raises(TypeError):
        store.prune(now=1_800_000_000_000)

    assert store._layout_paths(), "the refused call must not have deleted anything"


def test_a_layout_truncated_by_a_crash_falls_back_to_the_previous_one(store):
    """The machine can die mid-write. The newest file is then the damaged one.

    Falling back silently would be worse than failing: the user would be shown an
    older desk with no indication it was not the last one.
    """
    good = store.write_capture(
        sessions=sessions(("a0000001", "Project plan"), ("b0000002", "Demo module")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259600000,
        boot_id=1787235232500,
    )

    # simulate the crash: the newest file exists but is half-written
    victim = max(store.layouts_dir.glob("*.json"))
    victim.write_text(victim.read_text(encoding="utf-8")[:40], encoding="utf-8")

    newest = store.newest()

    assert newest.version == good.layout.version, "must fall back to the last parseable layout"
    assert [s.session_id for s in newest.sessions] == ["a0000001", "b0000002"]
    assert store.read_warnings == [
        "layout 000002 is unreadable (JSONDecodeError) -- showing 000001"
    ], "§6 rule 3: name the version shown instead; 'skipped' is the word it argues against"


def test_the_unreadable_layout_warning_says_so_when_there_is_nothing_older(store):
    """§6 rule 3's form assumes a fallback exists. When it does not, say that
    rather than naming a version that is not there."""
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )
    victim = store.layouts_dir / "000001.json"
    victim.write_text(victim.read_text(encoding="utf-8")[:40], encoding="utf-8")

    assert store.newest() is None
    assert store.read_warnings == ["layout 000001 is unreadable (JSONDecodeError) -- none older"]


def test_a_status_warning_is_not_swallowed_by_a_later_layout_read(store):
    """Warnings accumulate for the life of the Store rather than resetting per read.

    `termsync snap` reads `status.json` and then the layouts. If the layout read
    cleared the channel, the status warning would vanish on the way to the screen —
    a warning silently dropped by an unrelated later read is the exact failure this
    channel exists to prevent.
    """
    store.root.mkdir(parents=True, exist_ok=True)
    store.status_path.write_text("{not json", encoding="utf-8")
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1787259000000, boot_id=1)

    assert store.read_status() is None
    store.all_layouts()
    store.all_layouts()

    assert store.read_warnings == ["status.json is unreadable (JSONDecodeError); ignored"], (
        "the status warning must survive, and repeated reads must not double it"
    )


def test_all_layouts_reports_a_corrupt_layout_instead_of_swallowing_it(store):
    """L3: `all_layouts` fed target selection and pruning while silently dropping
    unreadable files, so a corrupt same-boot layout could vanish from the restore
    candidate set with nothing anywhere saying so."""
    for i in range(3):
        store.write_capture(
            sessions=sessions(*[(f"s{i}_{j}", "t") for j in range(i + 1)]),
            captured_at=1787259000000 + i * 600_000,
            boot_id=1787235232500,
        )
    victim = store.layouts_dir / "000002.json"
    victim.write_text(victim.read_text(encoding="utf-8")[:40], encoding="utf-8")

    series = store.all_layouts()

    assert [layout.version for layout in series] == [1, 3]
    assert store.read_warnings == [
        "layout 000002 is unreadable (JSONDecodeError) -- showing 000001"
    ], "a layout dropped from the candidate set must be reported, not swallowed"


def test_a_shape_wrong_layout_does_not_stop_the_tool_writing_further_layouts(store):
    """F8: `all_layouts()` used to catch a narrower exception tuple than
    `_read_json_record` -- missing `TypeError` -- so a layout whose JSON
    parsed but whose SHAPE was wrong (here: `sessions` is `null`, which makes
    `Layout.from_json`'s `tuple(Session.from_json(s) for s in d["sessions"])`
    raise `TypeError: 'NoneType' object is not iterable`) escaped uncaught
    through `newest()` and `write_capture()`, permanently stopping the tool
    from writing ANY further layout. It must instead be reported as an
    ordinary unreadable-layout warning, exactly like a truncated file.
    """
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1787259000000, boot_id=1)

    victim = store.layouts_dir / "000002.json"
    victim.write_text(
        json.dumps(
            {
                "version": 2,
                "source": "capture",
                "capturedAt": 1787259600000,
                "bootId": 1,
                "hash": "sha256:whatever",
                "sessions": None,
            }
        ),
        encoding="utf-8",
    )

    newest = store.newest()

    assert newest is not None and newest.version == 1, "must fall back, not raise"
    assert store.read_warnings == ["layout 000002 is unreadable (TypeError) -- showing 000001"]

    written = store.write_capture(
        sessions=sessions(("b", "t")), captured_at=1787260000000, boot_id=1
    )
    assert written.allocated and written.layout.version == 3, (
        "the tool must keep writing layouts past the shape-wrong one, not die forever"
    )


def test_an_unreadable_layout_file_is_warned_about_not_raised(store, monkeypatch):
    """F8: `OSError` (a locked or otherwise unreadable file -- live on Windows,
    where `snap` and an interactive `restore` can touch the store at once) must
    degrade the same way a truncated or shape-wrong layout does, not escape
    `all_layouts()` uncaught.
    """
    store.write_capture(sessions=sessions(("a", "t")), captured_at=1787259000000, boot_id=1)
    victim = store.layouts_dir / "000002.json"
    victim.write_text('{"version": 2}', encoding="utf-8")

    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == victim:
            raise OSError("simulated: file is locked by another process")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    series = store.all_layouts()

    assert [layout.version for layout in series] == [1]
    assert store.read_warnings == ["layout 000002 is unreadable (OSError) -- showing 000001"]


def test_normalize_registry_versions_drops_falsy_dedupes_and_sorts():
    """M8: the registry can hold entries from disagreeing Claude Code versions
    simultaneously (`2.1.238` and `2.1.239` observed live in one `-Emit` run).
    `normalize_registry_versions` is the single place that turns per-session
    values into the stored set: falsy entries dropped, duplicates collapsed,
    plain lexicographic sort (not a version-number parser -- this is a display
    set, not an ordering).
    """
    assert normalize_registry_versions(["2.1.239", "2.1.238"]) == ("2.1.238", "2.1.239")
    assert normalize_registry_versions(["2.1.238", "2.1.238"]) == ("2.1.238",), (
        "duplicates must collapse to one element"
    )
    assert normalize_registry_versions(["2.1.238", None, "", "2.1.238"]) == ("2.1.238",), (
        "falsy entries must be dropped"
    )
    assert normalize_registry_versions([]) == ()
    assert normalize_registry_versions(None) == ()


def test_layout_round_trips_disagreeing_registry_versions_as_a_sorted_deduped_list():
    """§6/M8: `registryVersions` on disk is a sorted, deduplicated JSON list --
    never a single collapsed string, and never null even when empty."""
    layout = Layout(
        version=1,
        source="capture",
        captured_at=1,
        boot_id=1,
        hash="sha256:x",
        sessions=(),
        registry_versions=normalize_registry_versions(["2.1.239", "2.1.238", "2.1.238"]),
    )

    on_disk = layout.to_json()
    assert on_disk["registryVersions"] == ["2.1.238", "2.1.239"]

    round_tripped = Layout.from_json(on_disk)
    assert round_tripped.registry_versions == ("2.1.238", "2.1.239")
    assert round_tripped.to_json() == on_disk


def test_a_captured_layout_round_trips_two_disagreeing_registry_versions_through_the_store(store):
    """Store-level version of the same property: a capture that saw two
    versions live at once must read back with both, sorted and deduplicated."""
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
        registry_versions=["2.1.239", "2.1.238", "2.1.238"],
    )

    on_disk = json.loads((store.layouts_dir / "000001.json").read_text(encoding="utf-8"))
    assert on_disk["registryVersions"] == ["2.1.238", "2.1.239"]
    assert store.newest().registry_versions == ("2.1.238", "2.1.239")


def test_registry_versions_defaults_to_empty_list_never_null_on_disk(store):
    """An unspecified `registry_versions` must store `[]`, not `null` -- §6's
    field is always present in the shape (matching windowCount/windowOrdinal's
    "present, empty" convention), and `null` would break a reader expecting a list."""
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
    )

    on_disk = json.loads((store.layouts_dir / "000001.json").read_text(encoding="utf-8"))
    assert on_disk["registryVersions"] == []
    assert store.newest().registry_versions == ()


def test_dedup_overwrites_registry_versions_with_the_current_runs_value_not_the_old_one(store):
    """M8 policy correction: a title-refresh dedup takes the CURRENT run's
    `registry_versions` unconditionally, exactly like `sessions`, `captured_at`
    and `boot_id` in the same branch -- it must never carry the previous
    layout's value forward. This is the conditional a previous agent invented
    in M8's absence (`registry_version if registry_version is not None else
    existing.registry_version`); restoring any version of that carry-over,
    including a falsy-check variant, makes this test fail.
    """
    store.write_capture(
        sessions=sessions(("a0000001", "Project plan")),
        captured_at=1787259000000,
        boot_id=1787235232500,
        registry_versions=["2.1.238"],
    )

    refreshed = store.write_capture(
        sessions=sessions(("a0000001", "Project plan - renamed")),
        captured_at=1787259600000,
        boot_id=1787235232500,
        registry_versions=["2.1.239"],
    )

    assert refreshed.allocated is False, "same session set -- must dedup, not allocate"
    assert refreshed.layout.registry_versions == ("2.1.239",), (
        "the refreshed layout must carry the CURRENT run's registry_versions, "
        "overwritten, not merged or carried over from the old layout"
    )
    assert store.newest().registry_versions == ("2.1.239",)

    # And the same overwrite rule applies when the current run reports nothing:
    # it must NOT fall back to the old layout's (now stale) registry_versions.
    unspecified = store.write_capture(
        sessions=sessions(("a0000001", "Project plan - renamed again")),
        captured_at=1787260200000,
        boot_id=1787235232500,
    )
    assert unspecified.layout.registry_versions == (), (
        "an unspecified registry_versions on this run must overwrite to empty, "
        "not silently keep the previous run's stale versions"
    )


def test_a_bare_string_of_registry_versions_is_rejected_rather_than_shredded(store):
    """A bare string is iterable, so `"2.1.238"` would normalize to its own
    characters -- wrong data on disk, no exception, in the field whose only job
    is honest health reporting. It is the natural mistake to make: this
    parameter was a single string until M8, and `Sequence[str]` accepts `str`.
    """
    with pytest.raises(TypeError, match="not a bare string"):
        normalize_registry_versions("2.1.238")

    with pytest.raises(TypeError, match="not a bare string"):
        store.write_capture(
            sessions=sessions(("a0000001", "Project plan phase 3 deep review")),
            captured_at=1787259000000,
            boot_id=1787235232500,
            registry_versions="2.1.238",
        )
