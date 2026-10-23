"""U8 policy tests: no real launcher, provider, desktop or owner store."""
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from termsync import restore
from termsync.capture_windows import DesktopObservation
from termsync.emit import Emit
from termsync.model import Pending, PendingEntry, Session, Layout
from termsync.rows import Row, Rows, build_rows
from termsync.restore_windows import launch_windows, plan_windows
from termsync.store import Store


def row(sid="a", title="same", **kw):
    return Row(sid, title, "C:/fixture", 1, 1, Path("fake.jsonl"),
               True, False, False, False, True, **kw)


def view(mapping):
    return DesktopObservation(tuple(
        {"hwnd": h, "tabs": [{"index": i, "name": title}
                              for i, title in enumerate(titles)]}
        for h, titles in mapping.items()), len(mapping))


class Scripted:
    def __init__(self, observations, codes=None):
        self.observations = iter(observations)
        self.last = view({})
        self.time = 0.0
        self.calls = []
        self.accepted = []
        self.timeouts = []
        self.codes = iter(codes or [])
        self.code = 0

    def observe(self, timeout):
        self.timeouts.append(timeout)
        self.last = next(self.observations, self.last)
        if isinstance(self.last, Exception):
            raise self.last
        return self.last

    def spawn(self, item, name):
        self.calls.append((item, name))
        return SimpleNamespace(poll=self.poll)

    def poll(self):
        self.code = next(self.codes, self.code)
        return self.code

    def sleep(self, seconds):
        self.time += seconds

    def run(self, rows):
        return launch_windows(rows, self.spawn, self.accepted.append,
                              observe=self.observe, monotonic=lambda: self.time,
                              sleep=self.sleep)


def test_g3_selection_oracle_after_selection_and_preflight():
    oracle = json.loads((Path(__file__).parent
                        / "fixtures/windows_replay/selection-oracle.json").read_text(
                            encoding="utf-8"))
    source = [row(item["sessionId"], window_ordinal=item["windowOrdinal"],
                  tab_index=item["tabIndex"]) for item in oracle["sourceRows"]]
    for skipped, key in [(set(), "expectedSelectedPositions"),
                         ({source[0].session_id}, "afterSkippingFirstRow"),
                         ({r.session_id for r in source[:2]}, "afterSkippingFirstWindow")]:
        selected = restore._select_rows(Rows(tuple(reversed(source)), ()),
                                        frozenset(r.session_id for r in source))
        survivors, _ = restore.preflight(selected,
            lambda sid: None if sid in skipped else Path("fake.jsonl"), lambda _: True)
        groups = plan_windows(survivors)
        assert [[g.ordinal, i] for g in groups for i, _ in enumerate(g.rows)] == oracle[key]
        assert [r.session_id for g in groups for r in g.rows] == [
            r.session_id for r in source if r.session_id not in skipped]


def test_unknown_codex_armed_young_and_partial_positions_are_final_catchall():
    known = row("known", window_ordinal=7, tab_index=5)
    unknown = [row("unknown"), row("codex", agent="codex", window_ordinal=1, tab_index=0),
               replace(known, session_id="armed", armed=True),
               replace(known, session_id="young", young=True),
               row("partial", window_ordinal=1)]
    plan = plan_windows(unknown + [known])
    assert plan[0].rows == (known,)
    assert plan[1].rows == tuple(unknown)
    assert known.window_ordinal == 7 and unknown[0].window_ordinal is None


def test_row_builder_carries_capture_placement_without_inventing_young_positions():
    layout = Layout(version=1, captured_at=100, boot_id=1, source="capture", hash="fixture",
                    sessions=(Session("a", "C:/fixture", "same", window_ordinal=3,
                                      tab_index=4),))
    rows = build_rows(layout, {}, frozenset(), frozenset(),
                      lambda _: Path("fake"), lambda _: True)
    assert (rows.desk[0].window_ordinal, rows.desk[0].tab_index) == (3, 4)


def test_duplicates_require_count_and_order_after_every_append():
    a, b, c = row("a"), row("b"), row("c", "last")
    s = Scripted([view({9: ["survivor"]}), view({9: ["survivor"], 10: ["same"]}),
                  view({10: ["same"]}), view({10: ["same"]}),
                  view({10: ["same", "same"]}), view({10: ["same", "same"]}),
                  view({10: ["same", "same", "last"]})])
    result = s.run([a, b, c])
    assert result.ready == (a, b, c) and not result.failures
    assert s.time == 0.25
    assert len({name for _, name in s.calls}) == 1
    assert s.accepted == [a, b, c]


def test_multiple_destinations_and_invocations_have_fresh_names():
    a = row("a", window_ordinal=3, tab_index=0)
    b = row("b", window_ordinal=8, tab_index=0)
    scripts = []
    for _ in range(2):
        s = Scripted([view({}), view({10: ["same"]}), view({10: ["same"]}),
                      view({10: ["same"], 11: ["same"]})])
        assert s.run([b, a]).ready == (a, b)
        scripts.append(s)
    names = [name for s in scripts for _, name in s.calls]
    assert len(set(names)) == 4
    assert all(name.startswith("desk-") for name in names)
    assert names[0].endswith("-1") and names[1].endswith("-2")


@pytest.mark.parametrize("bad", [
    view({10: ["same"], 11: ["same"]}),
    view({10: ["wrong"]}),
    view({10: ["same", "same"]}),
    DesktopObservation(error="UIA unavailable"),
    DesktopObservation((), 1),
    RuntimeError("reader failed"),
])
def test_first_append_failure_preserves_launch_and_stops(bad):
    a, b = row(), row("b")
    s = Scripted([view({}), bad])
    result = s.run([a, b])
    assert result.launched == (a,) and result.ready == ()
    assert result.not_attempted == (b,) and result.failures
    assert s.accepted == [a] and len(s.calls) == 1


@pytest.mark.parametrize("bad", [view({}), view({10: ["renamed"]}),
    view({10: ["same"], 11: ["other"]})])
def test_preappend_change_stops_before_next_spawn(bad):
    a, b = row(), row("b")
    s = Scripted([view({}), view({10: ["same"]}), bad])
    result = s.run([a, b])
    assert result.ready == (a,) and result.launched == (a,)
    assert result.not_attempted == (b,) and len(s.calls) == 1


@pytest.mark.parametrize("bad", [view({}), view({10: ["other", "same"]}),
    view({10: ["same", "same", "extra"]}), view({10: ["same"], 11: ["same"]})])
def test_append_mutation_is_not_retried(bad):
    a, b, c = row(), row("b"), row("c")
    s = Scripted([view({}), view({10: ["same"]}), view({10: ["same"]}), bad])
    result = s.run([a, b, c])
    assert result.launched == (a, b) and result.ready == (a,)
    assert result.not_attempted == (c,) and len(s.calls) == 2


def test_baseline_error_does_not_launch_or_persist():
    s = Scripted([DesktopObservation(error="denied")])
    result = s.run([row()])
    assert not result.launched and not s.calls and not s.accepted
    assert result.not_attempted == (row(),)


@pytest.mark.parametrize("codes,observations", [
    ([], [view({}), view({})]),  # no new window
    ([None] * 1000, [view({}), view({10: ["same"]})]),  # launcher never exits
])
def test_wait_is_bounded_and_never_retries(codes, observations):
    s = Scripted(observations, codes)
    result = s.run([row(), row("b")])
    assert s.time == 20.0 and len(s.calls) == 1
    assert "TimeoutError" in result.failures[0][1]
    assert all(0 < timeout <= 12 for timeout in s.timeouts)
    assert s.timeouts[-1] == 0.25


@pytest.mark.parametrize("codes", [[7], [None, 7]])
def test_nonzero_exit_before_or_after_read_is_unverified(codes):
    s = Scripted([view({}), view({10: ["same"]})], codes)
    result = s.run([row(), row("b")])
    assert result.launched == (row(),) and not result.ready
    assert "exited 7" in result.failures[0][1]


def test_exit_zero_alone_cannot_authorize_next_append():
    s = Scripted([view({}), view({}), view({10: ["same"]})])
    result = s.run([row()])
    assert result.ready == (row(),) and s.time == 0.25


def test_late_observation_cannot_pass_after_deadline():
    s = Scripted([view({}), view({10: ["same"]})])
    original = s.observe

    def late(timeout):
        result = original(timeout)
        if s.calls:
            s.time += 21
        return result

    s.observe = late
    assert not s.run([row()]).ready


def test_spawn_exception_does_not_claim_launch():
    s = Scripted([view({})])
    s.spawn = lambda *_: (_ for _ in ()).throw(OSError("spawn denied"))
    result = s.run([row(), row("b")])
    assert not result.launched and not s.accepted
    assert result.not_attempted == (row("b"),)


def test_first_destination_disappears_while_launcher_is_running():
    s = Scripted([view({}), view({10: ["same"]}), view({})], [None, None, None])
    result = s.run([row()])
    assert "disappeared" in result.failures[0][1]
    assert not result.ready


def test_empty_restore_never_reads_desktop():
    s = Scripted([AssertionError("must not observe")])
    assert not s.run([]).launched
    assert s.timeouts == [] and s.calls == []


def restore_fixture(tmp_path, s, *, run=False, store=None):
    store = store or Store(tmp_path / "store")
    output = StringIO()
    items = (row(), row("b"), row("c"))
    empty = Emit(sessions=(), claude_process_count=0, boot_time=1)

    def spawn(argv):
        return s.spawn(next(r for r in items if r.session_id ==
                           argv[argv.index("-SessionId") + 1]), argv[2])

    outcome = restore.restore_selected(frozenset(r.session_id for r in items),
        Rows(items, ()), store, empty, run=run, session_file=lambda _: Path("fake"),
        cwd_exists=lambda _: True, spawner=spawn, observe=s.observe,
        monotonic=lambda: s.time, sleep=s.sleep, clock=lambda: 100_000_000,
        verify_delay_s=0, emit_runner=lambda: empty, out=output)
    return outcome, store, output.getvalue()


def test_partial_restore_retains_exact_pending_and_unknown_placement(tmp_path):
    store = Store(tmp_path / "store")
    old = PendingEntry("old", "old title", "C:/old", 1)
    store.write_pending(Pending(1, (old,)))
    s = Scripted([view({}), view({10: ["same"]}), view({10: ["same"]}),
                  view({10: ["wrong"]})])
    outcome, store, output = restore_fixture(tmp_path, s, store=store)
    assert [r.session_id for r in outcome.spawned] == ["a", "b"]
    assert outcome.ready == (row(),) and outcome.unverified == (row("b"),)
    assert outcome.not_attempted == (row("c"),)
    pending = store.read_pending()
    assert pending.entries[0] == old  # even expired old entry waits for normal capture
    assert pending.session_ids == frozenset({"old", "a", "b"})
    layout = store.newest()
    assert {r.session_id for r in layout.sessions} == {"a", "b"}
    assert all(r.window_ordinal is None and r.tab_index is None for r in layout.sessions)
    assert "launched but unverified: [claude] b" in output
    assert "not attempted: [claude] c" in output


def test_pending_is_written_before_first_readiness_observation(tmp_path):
    store = Store(tmp_path / "store")
    s = Scripted([view({}), RuntimeError("stop after first append")])
    original = s.observe

    def observe(timeout):
        if s.calls:
            assert store.read_pending().session_ids == frozenset({"a"})
        return original(timeout)

    s.observe = observe
    restore_fixture(tmp_path, s, store=store)


def test_pending_write_failure_stops_and_still_records_action(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    monkeypatch.setattr(store, "merge_pending",
                        lambda _: (_ for _ in ()).throw(OSError("disk full")))
    s = Scripted([view({})])
    outcome, store, output = restore_fixture(tmp_path, s, store=store)
    assert outcome.unverified == (row(),) and len(s.calls) == 1
    assert "disk full" in output and store.newest().sessions[0].session_id == "a"


def test_run_mode_keeps_existing_pending_unchanged(tmp_path):
    store = Store(tmp_path / "store")
    pending = Pending(1, (PendingEntry("old", "old", "C:/old", 1),))
    store.write_pending(pending)
    s = Scripted([view({}), view({10: ["wrong"]})])
    outcome, store, _ = restore_fixture(tmp_path, s, run=True, store=store)
    assert outcome.unverified == (row(),) and store.read_pending() == pending
