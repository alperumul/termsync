"""Explicit live acceptance: new disposable sessions only; never run by pytest.

Run with the checkout venv from an ordinary owner desktop. Each invocation gets
new IDs, receipts, stores and evidence. A launch is never retried. No task/config
changes, historical probe reuse, real-store writes or shared-process kills.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
from uuid import uuid4

from live_restore_console import identity, processes, stamp, u, window_stamp
from termsync import cli
from termsync.capture import live_session_keys
from termsync.capture_windows import run_desktop
from termsync.emit import run_capture
from termsync.store import Store

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts/live_restore_worker.py"
CONSOLE = ROOT / "scripts/live_restore_console.py"


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_launch_order(launches, fixtures):
    argv = [entry["argv"] for entry in launches]
    require(len(argv) == 4, "Expected exactly four guarded production launches")
    ids = [args[args.index("-SessionId") + 1] for args in argv]
    require(
        ids == [fixture["sessionId"] for fixture in fixtures],
        "Production destination/tab launch order mismatch",
    )
    names = [args[args.index("-w") + 1] for args in argv]
    prefix = names[0].rsplit("-", 1)[0]
    require(
        names == [prefix + suffix for suffix in ("-1", "-1", "-2", "-3")],
        "Production destination numbers are not dense/ordered",
    )
    require(all("-Run" not in args for args in argv), "Armed Enter gate bypassed")


class Run:
    def __init__(self):
        self.token = "LR-" + uuid4().hex[:10]
        self.folder = (
            ROOT
            / "docs/research/live-restore"
            / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + self.token)
        )
        self.folder.mkdir(parents=True)
        self.scratch = ROOT / "scratch/live-restore" / self.token
        self.scratch.mkdir(parents=True)
        self.capture_store = Store(self.scratch / "capture-store")
        self.restore_store = Store(self.scratch / "restore-store")
        self.windows = []
        self.fixtures = []
        self.sequence = 0
        self.deadline = time.monotonic() + 900
        self.report = dict(
            result="PARTIAL",
            token=self.token,
            startedAt=time.time(),
            boundaries={},
            fixtures=self.fixtures,
            windows=self.windows,
            launches=[],
            inputs=[],
            errors=[],
            deadlines=dict(
                overallSeconds=900,
                seedSeconds=90,
                fixtureSeconds=60,
                pickerSeconds=60,
                restoreSeconds=180,
                resumeSeconds=90,
                consoleHelperSeconds=20,
                cleanupSeconds=30,
            ),
            bypasses=[],
            stores=dict(capture=str(self.capture_store.root), restore=str(self.restore_store.root)),
        )
        self.report["scriptHashes"] = {
            str(path.relative_to(ROOT)): digest(path)
            for path in (Path(__file__), WORKER, CONSOLE, ROOT / "scripts/live_restore_close.ps1")
        }
        self.flush()

    def flush(self):
        save(self.folder / "result.json", self.report)

    def boundary(self, name, result="PASS", **details):
        self.report["boundaries"][name] = dict(result=result, **details)
        self.flush()
        print(f"{name}: {result}", flush=True)

    def remaining(self, seconds):
        remaining = min(seconds, self.deadline - time.monotonic())
        require(remaining > 0, "Overall 900-second deadline exceeded")
        return remaining

    def view(self):
        desktop = run_desktop(min(12, self.remaining(12)))
        require(
            not desktop.error and desktop.windows_seen == len(desktop.windows),
            f"Incomplete desktop: {desktop.error}",
        )
        return {w["hwnd"]: [t["name"] for t in w["tabs"]] for w in desktop.windows}

    def observe(self, label):
        self.remaining(30)
        emit = run_capture()
        self.sequence += 1
        save(self.folder / f"observation-{self.sequence:03d}-{label}.json", asdict(emit))
        save(self.folder / f"{label}.json", asdict(emit))
        return emit

    def io(self, target, keys=None, required=()):
        self.sequence += 1
        path = self.folder / f"console-{self.sequence:03d}"
        req, out = path.with_suffix(".request.json"), path.with_suffix(".json")
        save(req, dict(identity=target, keys=keys, require=list(required)))
        entry = dict(
            request=str(req), output=str(out), keys=keys, requestedAt=time.time(), identity=target
        )
        if keys is not None:
            self.report["inputs"].append(entry)
            self.flush()  # An uncertain input is never automatically replayed.
        result = subprocess.run(
            [sys.executable, str(CONSOLE), str(req), str(out)],
            capture_output=True,
            timeout=self.remaining(20),
            creationflags=0x08000000,
        )
        data = json.loads(out.read_text()) if out.exists() else {}
        require(
            result.returncode == 0 and not data.get("error"),
            f"Console helper failed: {data.get('error', result.stderr.decode(errors='replace'))}",
        )
        return data

    def wait_screen(self, target, texts, seconds=60):
        end = time.monotonic() + self.remaining(seconds)
        last = ""
        while time.monotonic() < end:
            last = self.io(target)["screen"]
            if all(t in last for t in texts):
                return last
            time.sleep(0.3)
        raise TimeoutError(f"Console state deadline: {texts}; last screen: {last[-1500:]}")

    def launch(self, config, title, name, prior_titles=(), bound=None):
        before = self.view()
        argv = [
            "wt.exe",
            "-w",
            name,
            "new-tab",
            "-d",
            config["cwd"],
            "--title",
            title,
            "--suppressApplicationTitle",
            sys.executable,
            str(WORKER),
            config["path"],
        ]
        entry = dict(argv=argv, requestedAt=time.time(), title=title)
        self.report["launches"].append(entry)
        self.flush()
        process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.monotonic() + self.remaining(60)
        expected = list(prior_titles) + [title]
        while time.monotonic() < end:
            view = self.view()
            new = set(view) - set(before)
            if bound is None:
                require(len(new) <= 1, "Ambiguous new fixture windows; no launch retry")
                if new:
                    hwnd = next(iter(new))
                    # Bind immediately, before any later readiness assertion.
                    bound = dict(
                        hwnd=hwnd,
                        stamp=window_stamp(hwnd),
                        titles=expected,
                        mode=config["mode"],
                        boundAt=time.time(),
                    )
                    self.windows.append(bound)
                    entry["window"] = hwnd
                    self.flush()
            if bound is not None and bound["hwnd"] in view:
                actual = view[bound["hwnd"]]
                require(actual in (list(prior_titles), expected), "Fixture tab prefix changed")
                if actual == expected and process.poll() == 0:
                    bound["titles"] = expected
                    receipt = Path(config["path"]).with_suffix(".receipt.json")
                    while not receipt.exists() and time.monotonic() < end:
                        time.sleep(0.1)
                    require(receipt.exists(), "Worker startup receipt absent")
                    data = json.loads(receipt.read_text())
                    require(
                        os.path.normcase(data["cwd"]) == os.path.normcase(config["cwd"]),
                        "Fixture worker cwd mismatch",
                    )
                    require(
                        identity(data["identity"]["pid"]) == data["identity"],
                        "Worker identity mismatch",
                    )
                    entry.update(exitCode=0, receipt=data)
                    self.flush()
                    return bound, data["identity"]
            require(process.poll() in (None, 0), "wt exited nonzero; no launch retry")
            time.sleep(0.25)
        raise TimeoutError("Fixture launch deadline; may have opened; never retry")

    def config(self, label, mode, cwd, **values):
        path = self.scratch / f"{label}.json"
        data = dict(mode=mode, cwd=str(cwd), path=str(path), **values)
        save(path, data)
        return data

    def seed(self):
        claude = shutil.which("claude.exe")
        codex = (
            Path(os.environ["APPDATA"])
            / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
            "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
        )
        require(claude and codex.is_file(), "Installed provider binaries not found")
        for index, agent in enumerate(("claude", "claude", "claude", "codex")):
            label = f"{self.token}-{index + 1}"
            cwd = self.scratch / f"cwd-{index + 1}"
            cwd.mkdir()
            prompt = (
                f"{label} disposable restore validation. Reply exactly OK. "
                "Do not use tools or change files."
            )
            if agent == "claude":
                sid = str(uuid4())
                argv = [
                    claude,
                    "-p",
                    "--session-id",
                    sid,
                    "--name",
                    label,
                    "--tools",
                    "",
                    "--output-format",
                    "json",
                    prompt,
                ]
            else:
                sid = None
                argv = [
                    str(codex),
                    "-m",
                    "gpt-6.1-sol",
                    "-c",
                    'model_reasoning_effort="low"',
                    "-s",
                    "read-only",
                    prompt,
                ]
            fixture = dict(
                agent=agent,
                sessionId=sid,
                title=label,
                cwd=str(cwd),
                seedArgv=argv,
                seedRequestedAt=time.time(),
            )
            self.fixtures.append(fixture)
            self.flush()
            if agent == "codex":
                # exec-origin threads deliberately fail the production CLI-source
                # eligibility rule, even when resumed interactively. Create this
                # fixture in the TUI and obtain its ID from exact lock ownership.
                fixture["argv"] = argv
                continue
            # Output is saved even for failure/timeout, never repeated automatically.
            with (
                (self.folder / f"seed-{index + 1}.stdout").open("wb") as out,
                (self.folder / f"seed-{index + 1}.stderr").open("wb") as err,
            ):
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    creationflags=0x08000000,
                )
                fixture["seedPid"] = process.pid
                fixture["seedStart"] = stamp(process.pid)
                self.flush()
                try:
                    fixture["seedExit"] = process.wait(timeout=self.remaining(90))
                except subprocess.TimeoutExpired:
                    # Only the just-created native seed process, never Terminal.
                    require(stamp(process.pid) == fixture["seedStart"], "Seed PID reused")
                    process.kill()
                    process.wait(timeout=5)
                    fixture["seedTimedOut"] = True
                    self.flush()
                    raise
            require(fixture["seedExit"] == 0, f"{agent} seed failed; inspect saved output")
            output = (self.folder / f"seed-{index + 1}.stdout").read_text(encoding="utf-8")
            require(json.loads(output)["session_id"] == sid, "Claude seed ID differs")
            fixture["argv"] = [claude, "--resume", sid, "--name", label]
            self.flush()

    def close(self, window):
        hwnd = window["hwnd"]
        view = self.view()
        if hwnd not in view:
            return dict(result="PASS", hwnd=hwnd, alreadyAbsent=True)
        require(hwnd not in self.baseline, "Refusing baseline window cleanup")
        require(view[hwnd] == window["titles"], "Refusing changed/foreign tab cleanup")
        require(window_stamp(hwnd) == window["stamp"], "Refusing reused HWND/process cleanup")
        require(all(self.token in title for title in view[hwnd]), "Refusing unowned titles")
        # A current, exact observation plus run-local binding; no historical HWNDs.
        if not window.get("closeRequested"):
            window["closeRequested"] = time.time()
            self.flush()
            require(u.PostMessageW(hwnd, 0x0010, 0, 0), "Owned WM_CLOSE failed")
        if len(window["titles"]) > 1 and not window.get("confirmationRequested"):
            window["confirmationRequested"] = time.time()
            self.flush()
            request = self.folder / f"close-{hwnd}.request.json"
            save(request, window)
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "scripts/live_restore_close.ps1"),
                    "-Request",
                    str(request),
                ],
                capture_output=True,
                timeout=12,
                creationflags=0x08000000,
            )
            window["confirmationResult"] = dict(
                exitCode=result.returncode,
                stdout=result.stdout.decode("utf-8", "replace"),
                stderr=result.stderr.decode("utf-8", "replace"),
            )
            self.flush()
            require(result.returncode == 0, "Owned close confirmation failed; not retried")
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if hwnd not in self.view():
                return dict(result="PASS", hwnd=hwnd, wmClose=True)
            time.sleep(0.25)
        raise TimeoutError(f"Owned cleanup deadline for {hwnd}; not retried")

    def live_fixtures(self, emit):
        ids = {f["sessionId"] for f in self.fixtures}
        return [
            e
            for e in (*emit.sessions, *emit.codex)
            if e["sessionId"] in ids
            and (e.get("agent") == "codex" or (e.get("alive") and e.get("kind") == "interactive"))
        ]

    def run(self):
        incoming = self.observe("baseline")
        require(
            not incoming.claude_error and not incoming.codex_error, "Provider baseline incomplete"
        )
        self.baseline = self.view()
        save(self.folder / "baseline-windows.json", self.baseline)
        real = Path.home() / ".termsync"
        pending = real / "pending.json"
        pending_before = pending.read_bytes() if pending.exists() else None
        historical = {
            str(p.relative_to(real)): digest(p)
            for p in real.rglob("*")
            if p.is_file() and p.parent.name == "layouts"
        }
        save(
            self.folder / "real-store-before.json",
            dict(
                pending=json.loads(pending_before) if pending_before else None, layouts=historical
            ),
        )
        self.seed()
        first = None
        for index, fixture in enumerate(self.fixtures):
            group = 1 if index < 2 else index
            config = self.config(
                f"fixture-{index + 1}", "provider", fixture["cwd"], argv=fixture["argv"]
            )
            bound, owner = self.launch(
                config,
                fixture["title"],
                f"{self.token}-fixture-{group}",
                first["titles"] if index == 1 else (),
                first if index == 1 else None,
            )
            if index == 0:
                first = bound
            fixture.update(window=bound["hwnd"], consoleOwner=owner, config=config["path"])
            self.flush()
        # Trust/configuration prompts are not accepted or persisted by this check.
        # A new interactive Codex fixture has no caller-chosen UUID. Production
        # lock ownership plus exact console membership supplies its recorded ID.
        end = time.monotonic() + self.remaining(60)
        while time.monotonic() < end:
            captured = self.observe("fixture-observation")
            for fixture in self.fixtures:
                if fixture["sessionId"] is not None:
                    continue
                candidates = [
                    row
                    for row in captured.codex
                    if os.path.normcase(row["cwd"]) == os.path.normcase(fixture["cwd"])
                ]
                if len(candidates) == 1:
                    console = self.io(fixture["consoleOwner"])
                    require(
                        candidates[0]["pid"] in console["consolePids"],
                        "New Codex thread is not in the owned fixture console",
                    )
                    fixture["sessionId"] = candidates[0]["sessionId"]
                    self.flush()
            expected_keys = {(f["agent"], f["sessionId"]) for f in self.fixtures}
            if expected_keys <= live_session_keys(captured):
                break
            time.sleep(0.5)
        require(expected_keys <= live_session_keys(captured), "Fixture provider identity deadline")
        self.boundary("new_exact_provider_ids", ids=[f["sessionId"] for f in self.fixtures])
        require(cli.snap(self.capture_store) == 0, "Production fixture snap failed")
        layout = self.capture_store.newest()
        save(self.folder / "captured-layout.json", layout.to_json())
        sessions = {s.session_id: s for s in layout.sessions}
        a, b, cfix, codex = [sessions[f["sessionId"]] for f in self.fixtures]
        require(
            a.window_ordinal
            and a.window_ordinal == b.window_ordinal
            and (a.tab_index, b.tab_index) == (0, 1),
            "Claude first group placement mismatch",
        )
        require(
            cfix.window_ordinal and cfix.window_ordinal > a.window_ordinal and cfix.tab_index == 0,
            "Claude second group placement mismatch",
        )
        require(
            codex.window_ordinal is None and codex.tab_index is None, "Codex must remain unknown"
        )
        for fixture in self.fixtures:
            require(
                os.path.normcase(sessions[fixture["sessionId"]].cwd)
                == os.path.normcase(fixture["cwd"]),
                "Captured cwd mismatch",
            )
        self.boundary(
            "production_capture_placement",
            sourceWindows=[a.window_ordinal, cfix.window_ordinal],
            codexPlacement="unknown",
            claudeTabOrder=[0, 1],
        )
        # Copy the exact unedited production capture into a separate restore store.
        shutil.copytree(self.capture_store.root, self.restore_store.root)
        cleanup = [self.close(window) for window in self.windows if window["mode"] == "provider"]
        closed = self.observe("after-fixture-close")
        require(
            not expected_keys & live_session_keys(closed), "Fixture IDs remain live after close"
        )
        self.boundary("fixture_close", windows=cleanup)
        before_restore = self.view()
        picker_config = self.config(
            "picker",
            "picker",
            self.scratch,
            ids=[f["sessionId"] for f in self.fixtures],
            store=str(self.restore_store.root),
        )
        picker_window, picker_owner = self.launch(
            picker_config, f"{self.token}-picker", f"{self.token}-picker-window"
        )
        self.wait_screen(picker_owner, ["[space] toggle", "selected"])
        self.io(picker_owner, "a/" + self.token + "\r", ["[space] toggle", "selected"])
        self.wait_screen(picker_owner, ["0 selected", *[f["title"] for f in self.fixtures[:3]]])
        # Four visible fixture rows; real Space/j navigation, then real Enter.
        self.io(picker_owner, " j j j ", ["0 selected", self.token])
        self.wait_screen(picker_owner, ["4 selected", "[space] toggle"])
        self.io(picker_owner, "\r", ["4 selected", "[space] toggle"])
        result_path = Path(picker_config["path"]).with_suffix(".result.json")
        end = time.monotonic() + self.remaining(180)
        while not result_path.exists() and time.monotonic() < end:
            time.sleep(0.5)
        require(result_path.exists(), "Production restore deadline; no retry")
        picker_result = json.loads(result_path.read_text())
        save(self.folder / "picker-result.json", picker_result)
        view = self.view()
        destination_handles = set(view) - set(before_restore) - {picker_window["hwnd"]}
        for hwnd in destination_handles:
            # Guard cleanup even when restore reports a partial launch failure.
            titles = view[hwnd]
            if all(self.token in title for title in titles):
                self.windows.append(
                    dict(
                        hwnd=hwnd,
                        stamp=window_stamp(hwnd),
                        titles=titles,
                        mode="restored",
                        boundAt=time.time(),
                    )
                )
        self.flush()
        require(
            picker_result["exitCode"] == 0, "Production restore returned failure; inspect evidence"
        )
        expected_titles = [[a.title, b.title], [cfix.title], [codex.title]]
        actual_titles = [view[h] for h in destination_handles]
        require(
            sorted(actual_titles) == sorted(expected_titles), "Restored window/tab titles mismatch"
        )
        launchlog = Path(picker_config["path"]).with_suffix(".launches.jsonl")
        launches = [json.loads(line) for line in launchlog.read_text().splitlines()]
        validate_launch_order(launches, self.fixtures)
        self.report["productionLaunches"] = launches
        armed = self.observe("armed-before-enter")
        require(not expected_keys & live_session_keys(armed), "Provider started before Enter")
        pending_armed = self.restore_store.read_pending()
        require(
            pending_armed and expected_keys <= pending_armed.session_keys,
            "Missing armed pending IDs",
        )
        save(self.folder / "pending-armed.json", pending_armed.to_json())
        self.boundary(
            "real_picker_selection", keys="none, filter, Space/j x4, Enter", bypassed=False
        )
        self.boundary(
            "fresh_window_tab_order_titles",
            expected=expected_titles,
            actual=actual_titles,
            productionLaunches=launches,
        )
        self.boundary(
            "default_armed_gate",
            liveBeforeEnter=False,
            pendingIds=[list(k) for k in sorted(pending_armed.session_keys)],
        )
        inventory = processes()
        for fixture in self.fixtures:
            sid = fixture["sessionId"]
            matches = [
                p
                for p in inventory
                if p["Name"].lower() == "powershell.exe"
                and p["CommandLine"]
                and str(ROOT / "snapshot/arm.ps1").lower() in p["CommandLine"].lower()
                and sid in p["CommandLine"]
            ]
            require(len(matches) == 1, f"Arm console not uniquely owned for {sid}")
            owner = identity(matches[0]["ProcessId"], inventory)
            fixture["armedConsole"] = owner
            self.wait_screen(owner, ["Enter to resume", sessions[sid].title], 30)
            self.io(owner, "\r", ["Enter to resume", sessions[sid].title])
        self.flush()
        end = time.monotonic() + self.remaining(90)
        while time.monotonic() < end:
            resumed = self.observe("resumed-observation")
            if expected_keys <= live_session_keys(resumed):
                break
            time.sleep(0.5)
        require(expected_keys <= live_session_keys(resumed), "Exact resumed provider ID deadline")
        rows = self.live_fixtures(resumed)
        require(len(rows) == 4, "Exact resumed IDs are duplicated")
        for fixture in self.fixtures:
            row = next(r for r in rows if r["sessionId"] == fixture["sessionId"])
            require(
                os.path.normcase(row["cwd"]) == os.path.normcase(fixture["cwd"]),
                "Resumed cwd mismatch",
            )
            # Bind exact resumed provider PID/start to the same arm console that
            # received Enter. Screen titles alone cannot establish tab identity.
            console = self.io(fixture["armedConsole"])
            require(
                row["pid"] in console["consolePids"], "Resumed provider is in another tab console"
            )
            require(
                stamp(row["pid"]) == row.get("procStartLive", row.get("procStart")),
                "Resumed provider PID/start mismatch",
            )
        save(self.folder / "resumed-exact-rows.json", rows)
        self.boundary("enter_exact_ids_cwds_console_membership", rows=rows, runFlagUsed=False)
        require(cli.snap(self.restore_store) == 0, "Production reconciliation snap failed")
        pending_after = self.restore_store.read_pending()
        save(self.folder / "pending-after.json", pending_after.to_json() if pending_after else None)
        require(
            not pending_after or not expected_keys & pending_after.session_keys,
            "Exact live IDs did not reconcile pending",
        )
        newest = self.restore_store.newest()
        require(
            expected_keys <= {(s.agent, s.session_id) for s in newest.sessions if not s.armed},
            "Reconciled layout missing live fixture IDs",
        )
        self.boundary("production_pending_reconciliation")
        pending_now = pending.read_bytes() if pending.exists() else None
        require(
            pending_now == pending_before,
            "Real pending changed; preserve and inspect natural capture",
        )
        changed_history = [
            p for p, sha in historical.items() if not (real / p).exists() or digest(real / p) != sha
        ]
        require(not changed_history, "Existing real snapshots changed; inspect natural writer")
        self.boundary(
            "real_store_preservation",
            pendingUnchanged=True,
            historicalLayoutsUnchanged=len(historical),
            naturalNewCapturesPreserved=True,
        )

    def finish(self):
        # Cleanup has a separate grace budget even after the work deadline.
        self.deadline = max(self.deadline, time.monotonic() + 180)
        self.report["cleanupStartedAt"] = time.time()
        self.flush()
        results = []
        # Stop the picker worker first if a restore timed out. Do not permit it
        # to continue issuing launches while cleanup observes other windows.
        ordered = sorted(self.windows, key=lambda window: window["mode"] != "picker")
        for window in ordered:
            try:
                results.append(self.close(window))
            except Exception as exc:
                results.append(dict(result="PARTIAL", hwnd=window["hwnd"], error=str(exc)))
        try:
            final = self.observe("after-cleanup")
            owned = {(f["agent"], f["sessionId"]) for f in self.fixtures if f.get("sessionId")}
            absent = not (owned & live_session_keys(final))
            require(absent, "Owned fixture provider IDs still live")
            desktop = self.view()
            require(set(self.baseline) <= set(desktop), "A baseline window disappeared during run")
            leftovers = {
                hwnd: titles
                for hwnd, titles in desktop.items()
                if any(self.token in title for title in titles)
            }
            require(not leftovers, f"Unclosed/unbound fixture windows: {leftovers}")
            self.boundary(
                "cleanup",
                "PASS" if all(r["result"] == "PASS" for r in results) else "PARTIAL",
                windows=results,
                fixtureIdsAbsent=absent,
                baselineWindowsPresent=True,
            )
        except Exception as exc:
            self.boundary("cleanup", "PARTIAL", windows=results, error=str(exc))
        required = (
            "new_exact_provider_ids",
            "production_capture_placement",
            "fixture_close",
            "real_picker_selection",
            "fresh_window_tab_order_titles",
            "default_armed_gate",
            "enter_exact_ids_cwds_console_membership",
            "production_pending_reconciliation",
            "real_store_preservation",
            "cleanup",
        )
        for name in required:
            if name not in self.report["boundaries"]:
                self.report["boundaries"][name] = dict(
                    result="PARTIAL", reason="Not reached after recorded failure"
                )
        self.report["result"] = (
            "PASS"
            if not self.report["errors"]
            and all(self.report["boundaries"][n]["result"] == "PASS" for n in required)
            else "PARTIAL"
        )
        self.report["finishedAt"] = time.time()
        self.flush()
        print(json.dumps(dict(result=self.report["result"], evidence=str(self.folder))), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run = Run()
    print(f"Evidence: {run.folder}", flush=True)
    try:
        run.run()
    except Exception as exc:
        run.report["errors"].append(
            dict(
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
                at=time.time(),
            )
        )
        run.flush()
        print(run.report["errors"][-1]["error"], flush=True)
    finally:
        if hasattr(run, "baseline"):
            run.finish()
        else:
            run.flush()
    return 0 if run.report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
