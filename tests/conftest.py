"""Structural sandboxing for every real `subprocess.run` call in this suite
(S8 fix round, F8).

**The incident this fixture exists to close.** Two safety incidents shared
one root cause in one session: a real subprocess spawned by a test inherited
real machine state because nothing pinned it -- first `PATH` (an earlier
round), then `cwd` (S8's own `termsync.cmd` missing-venv test, which once
resolved the real repo's real `.venv` and ran a REAL `termsync snap` against
the real `~/.termsync` on the development machine before the omission was
caught -- see `test_termsync_cmd_refuses_a_missing_venv_python_loudly_and_
without_fallback`'s own docstring in `test_install.py`). Both times the only
defence was per-test discipline -- remembering to pass `cwd=`/`env=` -- and
that discipline has now failed twice. This fixture makes the protection
structural instead of advisory: it does not rely on any individual test
remembering anything.

**Why this works even though `DEFAULT_STORE_ROOT` is bound at import time.**
`DEFAULT_STORE_ROOT = Path.home() / ".termsync"` (`cli.py:47`) is evaluated
once, when `cli.py` is first imported -- in THIS pytest process, long before
any individual test runs. An in-process `monkeypatch.setattr(Path, "home",
...)` here would be too late to change that already-bound module constant.
But every real `subprocess.run` call in this suite launches a BRAND NEW
Python interpreter (`termsync.cmd` -> `python.exe -m termsync`), and that
child process evaluates `Path.home()` -- and so `DEFAULT_STORE_ROOT` -- for
itself, fresh, at ITS OWN import time, reading `USERPROFILE`/`HOME` from the
environment it was launched with. `subprocess.run(..., env=None)` (the
default -- no test in this suite passes its own `env=`) inherits the CALLING
process's `os.environ` verbatim. So overriding `USERPROFILE`/`HOME` in THIS
process's `os.environ`, before any subprocess is spawned, redirects every
child's own `Path.home()` without needing any test to opt in.

**Autouse, not opt-in.** Scoped to every test in the package (no directory
restriction), so a test that forgets to pass `--store` or `cwd=` still
cannot reach the real `~\\.termsync` or `~\\.claude` -- it reaches a
`tmp_path`-rooted sandbox home that does not exist instead, and fails
loudly (a "does not exist" / "not found" style error from whatever tried to
read or write there) rather than silently touching the real user profile.
This does not remove the value of a test ALSO passing `--store` explicitly
where it can -- that stays the first line of defence, asserting the CORRECT
store was used, not just a harmless one -- this fixture is the second line,
for the state that has no `--store`-shaped knob (`~/.claude/history.jsonl`,
any code path that reaches `Path.home()` without an override at all).
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _sandbox_subprocess_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect `USERPROFILE`/`HOME` to a sandboxed, per-test directory
    under `tmp_path` for the duration of each test. Every real subprocess
    spawned without its own explicit `env=` inherits this automatically."""
    sandbox_home = tmp_path / "sandboxed-home"
    sandbox_home.mkdir(exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(sandbox_home))
    monkeypatch.setenv("HOME", str(sandbox_home))


@pytest.fixture(autouse=True)
def _never_launch_terminal(monkeypatch):
    original = subprocess.Popen

    def guarded(args, *a, **kw):
        executable = args[0] if isinstance(args, (list, tuple)) else args.split()[0]
        if Path(executable).name.lower() in {"wt", "wt.exe"}:
            raise AssertionError("Tests must never launch Windows Terminal")
        return original(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", guarded)


@pytest.fixture
def ready_restore_desktop(monkeypatch):
    """Immediate fake display for older command/persistence/CLI tests.

    U8 transition and failure tests inject independent scripted observations.
    No test opting into this fixture can reach real desktop APIs.
    """
    from termsync import restore
    from termsync.capture_windows import DesktopObservation

    windows = {}
    original = restore.build_wt_argv

    def build(*args, **kwargs):
        argv = original(*args, **kwargs)
        windows.setdefault(argv[2], []).append(argv[argv.index("--title") + 1])
        return argv

    def observe(_timeout):
        return DesktopObservation(tuple(
            {"hwnd": i, "tabs": [{"index": j, "name": title}
                                 for j, title in enumerate(titles)]}
            for i, titles in enumerate(windows.values(), 1)), len(windows))

    monkeypatch.setattr(restore, "build_wt_argv", build)
    monkeypatch.setattr(restore, "run_desktop", observe)
