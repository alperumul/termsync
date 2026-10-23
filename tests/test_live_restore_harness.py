"""Fail-closed ownership checks; these tests never attach or open a Terminal."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "live_restore_harness", scripts / "Check-LiveRestore.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_console_identity_mismatch_rejects_before_attach(harness, monkeypatch):
    console = sys.modules["live_restore_console"]
    calls = []
    monkeypatch.setattr(console, "identity", lambda pid: {"pid": pid, "start": "new"})
    monkeypatch.setattr(console.k, "AttachConsole", lambda pid: calls.append(pid))
    with pytest.raises(RuntimeError, match="ownership changed"):
        console.console({"identity": {"pid": 123, "start": "old"}, "keys": "\r"})
    assert not calls


@pytest.mark.parametrize("reason", ["baseline", "titles", "stamp", "token"])
def test_cleanup_refuses_foreign_or_changed_window(harness, monkeypatch, reason):
    run = object.__new__(harness.Run)
    run.token = "LR-test"
    run.baseline = {123: ["LR-test"]} if reason == "baseline" else {}
    window = {"hwnd": 123, "stamp": {"pid": 2, "start": "original"}, "titles": ["LR-test"]}
    current = ["other"] if reason == "titles" else ["LR-test"]
    if reason == "token":
        current = window["titles"] = ["foreign"]
    run.view = lambda: {123: current}
    monkeypatch.setattr(
        harness,
        "window_stamp",
        lambda hwnd: {"pid": 2, "start": "reused" if reason == "stamp" else "original"},
    )
    calls = []
    monkeypatch.setattr(harness.u, "PostMessageW", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="Refusing"):
        run.close(window)
    assert not calls


def test_absent_owned_window_needs_no_close(harness, monkeypatch):
    run = object.__new__(harness.Run)
    run.view = lambda: {}
    calls = []
    monkeypatch.setattr(harness.u, "PostMessageW", lambda *args: calls.append(args))
    assert run.close({"hwnd": 123}) == {"result": "PASS", "hwnd": 123, "alreadyAbsent": True}
    assert not calls


def test_expired_main_deadline_rejects_new_work(harness):
    run = object.__new__(harness.Run)
    run.deadline = 0
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        run.remaining(20)


def test_capture_rows_do_not_accept_dead_or_background_claude(harness):
    run = object.__new__(harness.Run)
    run.fixtures = [{"sessionId": "owned"}]
    emit = SimpleNamespace(
        sessions=(
            {"sessionId": "owned", "alive": False, "kind": "interactive"},
            {"sessionId": "owned", "alive": True, "kind": "bg"},
            {"sessionId": "foreign", "alive": True, "kind": "interactive"},
        ),
        codex=(),
    )
    assert run.live_fixtures(emit) == []


@pytest.mark.parametrize("change", ["id-order", "window-number", "run-gate"])
def test_launch_order_or_gate_mismatch_is_rejected(harness, change):
    fixtures = [{"sessionId": str(i)} for i in range(4)]
    launches = [
        {"argv": ["wt.exe", "-w", "desk-fresh-" + group, "-SessionId", str(i)]}
        for i, group in enumerate(("1", "1", "2", "3"))
    ]
    harness.validate_launch_order(launches, fixtures)
    if change == "id-order":
        launches[0], launches[1] = launches[1], launches[0]
    elif change == "window-number":
        launches[-1]["argv"][2] = "desk-fresh-9"
    else:
        launches[-1]["argv"].append("-Run")
    with pytest.raises(RuntimeError):
        harness.validate_launch_order(launches, fixtures)
