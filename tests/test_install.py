"""Behaviour tests for work unit S8 (`install.py`, `termsync.cmd`, and the
narrow `cli.snap` prune-wiring edit): §4, §13.3 S8, §13.4's scheduled-task
row, §11 open item 10, D24-D30.

**D30 (windowless-task fix round).** `install()` now registers the task's
action as `pythonw.exe -m termsync snap`, not `termsync.cmd snap` -- a
`.cmd` action under `/IT` makes Task Scheduler launch a visible `cmd.exe`
console every run, which can steal foreground focus.
`install()`'s `termsync_cmd` parameter is renamed `pythonw`
throughout this file to match; `termsync.cmd` itself is untouched (it stays
the interactive launcher) and its own tests below are
unaffected.

**SAFETY, restated from this unit's brief.** No test in this file ever calls
`install.default_runner` (real `subprocess.run(["schtasks.exe", ...])`) --
not with a real argv, not with a synthetic one; F9's `timeout=`/`stdin=`
addition is production-only insurance, deliberately untested here for that
reason (see `install.default_runner`'s own docstring). Every registration
path is exercised through `install.install(...)` with an injected `runner`
fake that never touches `subprocess`.

The `test_cli_main_install_*` tests below DO call `cli_module.main(["install",
...])` without `--dry-run` (closing F1, the S8 review's own "zero test
coverage" finding on the dispatch line) -- but every one of them first
`monkeypatch.setattr(install, "install", fake_install)`, replacing the WHOLE
function `cli.py`'s `install` branch calls (an attribute lookup on the
`install` module at call time, the same seam `test_dry_run.py:1706` already
uses for `cli.restore_interactive`). The real `install.install` -- and so the
real `install.default_runner` -- is never reached by any of them.

The `termsync.cmd` tests below DO run a real `subprocess.run`, but only ever
against a scratch copy of the launcher pointed at a scratch `--store` (never
`~/.termsync`) or at an invalid subcommand that argparse rejects before any
I/O -- see each test's own docstring.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from termsync import cli as cli_module
from termsync import install
from termsync.cli import snap
from termsync.emit import Emit
from termsync.install import CommandResult
from termsync.install import install as run_install
from termsync.model import Session
from termsync.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
PROC_START = "130000000000000003"


# --- shared helpers (mirrors test_snap.py's own shape) ---------------------


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sessionId": "s1",
        "cwd": r"C:\fixture\tool",
        "alive": True,
        "procStart": PROC_START,
        "procStartLive": PROC_START,
        "kind": "interactive",
        "titleRaw": "termsync work",
        "titleAttachOk": True,
        "name": "termsync-1",
        "version": "2.1.238",
    }
    base.update(overrides)
    return base


def emit_of(*entries: dict[str, Any], boot_time: int | None = 1000) -> Emit:
    return Emit(sessions=tuple(entries), claude_process_count=len(entries), boot_time=boot_time)


def clock_returning(*values: int):
    # Existing tests model a fixed wall clock; timing tests supply both readings.
    it = iter(values)
    last = values[-1]

    def _clock() -> int:
        return next(it, last)

    return _clock


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "store")


# --- `build_register_argv` (points 1, 2, 4, 5 of this unit's brief) --------


NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_xml_task_action_is_quoted_pythonw_with_separate_arguments() -> None:
    launcher = Path(r"D:\repo with space\.venv\Scripts\pythonw.exe")
    root = ET.fromstring(install.build_task_xml(launcher, "EXAMPLE\\user"))
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=NS) == f'"{launcher}"'
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS) == "-m termsync snap"
    assert root.find("t:Actions", NS).attrib == {"Context": "Author"}


def test_xml_sets_explicit_interactive_principal_and_escapes_account() -> None:
    root = ET.fromstring(install.build_task_xml(Path("pythonw.exe"), "DOMAIN\\a&b"))
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal.attrib == {"id": "Author"}
    assert principal.findtext("t:LogonType", namespaces=NS) == "InteractiveToken"
    assert principal.findtext("t:UserId", namespaces=NS) == "DOMAIN\\a&b"


def test_register_argv_uses_xml_and_forces_idempotent_reregistration() -> None:
    xml_path = Path(r"D:\with space\task.xml")
    assert install.build_register_argv(xml_path) == [
        "schtasks.exe", "/Create", "/TN", "termsync-snap", "/XML", str(xml_path), "/F"
    ]


def test_register_argv_uses_the_given_task_name() -> None:
    assert install.build_register_argv(Path("task.xml"), task_name="custom-name") == [
        "schtasks.exe", "/Create", "/TN", "custom-name", "/XML", "task.xml", "/F"
    ]


def test_query_argv_reads_back_the_same_task_as_xml() -> None:
    argv = install.build_query_argv("custom-name")

    assert argv == ["schtasks.exe", "/Query", "/TN", "custom-name", "/XML", "ONE"]


def test_default_pythonw_points_at_the_repo_venvs_windowless_host() -> None:
    """D30: `install()`'s production default (`DEFAULT_PYTHONW`, wired
    through `cli.py`'s bare `install.install(...)` call with no
    `pythonw=` override) must resolve to the checkout's own
    `.venv\\Scripts\\pythonw.exe` -- the whole point of D30 is that the
    scheduled task launches THIS windowless host, not `termsync.cmd`.
    Mirrors `test_emit.py`'s/`test_restore_cmdline.py`'s own precedent of
    pinning a module-level `_DEFAULT_*` path constant directly."""
    assert install.DEFAULT_PYTHONW == REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"


# --- `parse_logon_type` / `parse_task_action` (point 3, F3) ------------------


EXPECTED_SETTINGS = {
    "DisallowStartIfOnBatteries": "false",
    "StopIfGoingOnBatteries": "false",
    "StartWhenAvailable": "true",
    "ExecutionTimeLimit": "PT1M",
    "MultipleInstancesPolicy": "IgnoreNew",
    "WakeToRun": "false",
    "RunOnlyIfIdle": "false",
    "Enabled": "true",
    "Hidden": "false",
}
EXPECTED_QUERIES = {
    "shutdown": "*[System[Provider[@Name='User32'] and EventID=1074]]",
    "sleep": "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=42]]"
             " and *[EventData[Data[@Name='TargetState'] != 6]]",
    "resume": "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]",
}


def _fixture_events_and_settings() -> tuple[str, str]:
    events = []
    for name, query in EXPECTED_QUERIES.items():
        event = ET.Element("EventTrigger", id=name)
        ET.SubElement(event, "Enabled").text = "true"
        ET.SubElement(event, "Subscription").text = (
            '<QueryList><Query Id="0" Path="System"><Select Path="System">'
            + query + '</Select></Query></QueryList>'
        )
        if name == "resume":
            ET.SubElement(event, "Delay").text = "PT5S"
        events.append(ET.tostring(event, encoding="unicode"))
    settings = "<Settings>" + "".join(
        f"<{name}>{value}</{name}>" for name, value in EXPECTED_SETTINGS.items()
    ) + "</Settings>"
    return "".join(events), settings


def _task_xml(
    logon_type: str,
    *,
    command: str = r"C:\fixture\tool\.venv\Scripts\pythonw.exe",
    arguments: str = install.TASK_ACTION_ARGS,
    interval_minutes: int | None = 10,
) -> str:
    """The real shape measured against `CreateExplorerShellUnelevatedTask`
    on the development machine for `<Principals>`/`<Actions>` (this unit's
    report cites the raw probe). The `<Triggers>/<Repetition>/<Interval>`
    shape is Task Scheduler's documented ISO-8601 duration format
    (`PTxxM`) -- NOT independently measured against a real `/SC MINUTE`
    task, since registering one is forbidden by this unit's SAFETY rules.

    D30: `command`/`arguments` default to the windowless action
    (`pythonw.exe` / `-m termsync snap`) `install()` now registers -- the
    live-measured `termsync.cmd`/`snap` shape moved into
    `test_parse_task_action_strips_task_schedulers_own_quotes_around_the_command`,
    which preserves that measured shape with a synthetic command path
    rather than the current default."""
    interval_xml = (
        f"      <Repetition>\r\n        <Interval>PT{interval_minutes}M</Interval>\r\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\r\n"
        "      </Repetition>\r\n"
        if interval_minutes is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        "  <Triggers>\r\n"
        "    <TimeTrigger>\r\n"
        "      <Enabled>true</Enabled>\r\n"
        "      <StartBoundary>2000-01-01T00:00:00</StartBoundary>\r\n"
        f"{interval_xml}"
        "    </TimeTrigger>\r\n"
        + _fixture_events_and_settings()[0]
        + "  </Triggers>\r\n"
        + _fixture_events_and_settings()[1]
        + "  <Principals>\r\n"
        '    <Principal id="Author">\r\n'
        "      <UserId>S-1-5-21-0000000000-0000000000-000000000-1001</UserId>\r\n"
        f"      <LogonType>{logon_type}</LogonType>\r\n"
        "    </Principal>\r\n"
        "  </Principals>\r\n"
        '  <Actions Context="Author">\r\n'
        "    <Exec>\r\n"
        f"      <Command>{command}</Command>\r\n"
        f"      <Arguments>{arguments}</Arguments>\r\n"
        "    </Exec>\r\n"
        "  </Actions>\r\n"
        "</Task>\r\n"
    )


def test_parse_logon_type_reads_the_real_measured_xml_shape() -> None:
    assert install.parse_logon_type(_task_xml("InteractiveToken")) == "InteractiveToken"


def test_parse_logon_type_reports_a_non_interactive_type_verbatim() -> None:
    """A task that landed 'run whether logged on or not' (§4's failure mode)
    reads back `Password` or `S4U`, not `InteractiveToken` -- the function
    must report the REAL value, not silently coerce it."""
    assert install.parse_logon_type(_task_xml("Password")) == "Password"
    assert install.parse_logon_type(_task_xml("S4U")) == "S4U"


def test_parse_logon_type_returns_none_on_unparseable_xml() -> None:
    assert install.parse_logon_type("not xml at all") is None


def test_parse_logon_type_returns_none_when_the_element_is_missing() -> None:
    xml_no_logon_type = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Principals><Principal/></Principals></Task>"
    )
    assert install.parse_logon_type(xml_no_logon_type) is None


def test_parse_logon_type_returns_none_for_an_empty_element_rather_than_raising() -> None:
    """F10/M4, S8 fix round: `if node is None or not node.text:` -- drop the
    empty-text guard and this raises `AttributeError` instead of returning
    `None` (probed directly: `<LogonType></LogonType>` parses to a node
    whose `.text` is `None`, and `.strip()` on `None` raises).
    `test_parse_logon_type_returns_none_when_the_element_is_missing` above
    only exercises the `node is None` half of the guard -- this pins the
    other half, an element that exists but is empty."""
    xml_empty_logon_type = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Principals><Principal><LogonType></LogonType></Principal></Principals></Task>"
    )
    assert install.parse_logon_type(xml_empty_logon_type) is None


def test_parse_logon_type_strips_surrounding_whitespace() -> None:
    """F10/M5, S8 fix round: drop `.strip()` and a whitespace-padded value
    round-trips with the padding still attached, which would never compare
    equal to `INTERACTIVE_LOGON_TYPE` in `install()`."""
    xml_padded = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Principals><Principal><LogonType>  InteractiveToken  </LogonType>"
        "</Principal></Principals></Task>"
    )
    assert install.parse_logon_type(xml_padded) == "InteractiveToken"


def test_parse_logon_type_ignores_the_wrong_declared_encoding() -> None:
    """Measured against real `schtasks` output (the module docstring): the
    XML prolog always declares `encoding="UTF-16"` but the actual bytes are
    single-byte-per-character. F10, S8 fix round: the ORIGINAL version of
    this test asserted only `isinstance(text, str)` on an f-string literal
    -- tautological, and never actually exercised the claimed mismatch. This
    version shows the two actual halves of the claim: the raw BYTES really
    do raise (the declared encoding is wrong), and `default_runner` handing
    a decoded `str` to `parse_logon_type` (never the raw bytes) is what
    makes the mismatch harmless -- `ElementTree` ignores a `str` input's
    declared encoding entirely."""
    xml_bytes = _task_xml("InteractiveToken").encode("utf-8")
    with pytest.raises(ET.ParseError, match="encoding"):
        ET.fromstring(xml_bytes)

    assert install.parse_logon_type(_task_xml("InteractiveToken")) == "InteractiveToken"


def test_parse_task_action_reads_the_command_and_arguments() -> None:
    action = install.parse_task_action(
        _task_xml("InteractiveToken", command=r"C:\repo\termsync.cmd", arguments="snap")
    )
    assert action == (r"C:\repo\termsync.cmd", "snap")


def test_parse_task_action_returns_none_when_the_command_is_missing() -> None:
    xml_no_action = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        '<Actions Context="Author"><Exec><Arguments>snap</Arguments></Exec></Actions>'
        "</Task>"
    )
    assert install.parse_task_action(xml_no_action) is None


def test_parse_task_action_returns_none_on_unparseable_xml() -> None:
    assert install.parse_task_action("not xml at all") is None


# --- `_default_run_as_user` (F4, S8 fix round) ------------------------------


def test_default_run_as_user_with_both_env_vars_set_qualifies_with_the_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only one of the four combinations schtasks accepts (measured,
    F4)."""
    monkeypatch.setenv("USERDOMAIN", "EXAMPLE")
    monkeypatch.setenv("USERNAME", "user")
    assert install._default_run_as_user() == "EXAMPLE\\user"


def test_default_run_as_user_raises_when_userdomain_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured: a bare username with no domain is rejected outright by
    `schtasks /Create` -- `(43,4):UserId:`. Must never return `user` alone."""
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setenv("USERNAME", "user")
    with pytest.raises(install.RunAsUserUnresolvedError, match="USERDOMAIN"):
        install._default_run_as_user()


def test_default_run_as_user_raises_when_username_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured: `DOMAIN\\` with no username is rejected the same way --
    `(43,4):UserId:`. Must never return `f"{domain}\\"`."""
    monkeypatch.setenv("USERDOMAIN", "EXAMPLE")
    monkeypatch.delenv("USERNAME", raising=False)
    with pytest.raises(install.RunAsUserUnresolvedError, match="USERNAME"):
        install._default_run_as_user()


def test_default_run_as_user_raises_when_both_env_vars_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catastrophic case (F4, upgraded to HIGH): schtasks' own
    `/Create /?` text documents an empty `/RU` value as THE SYSTEM ACCOUNT.
    Must never return `""`."""
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    with pytest.raises(install.RunAsUserUnresolvedError, match="USERDOMAIN and USERNAME"):
        install._default_run_as_user()


def test_default_run_as_user_raises_when_either_var_is_present_but_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty string is not "set" for this purpose -- `os.environ.get(...,
    "")` cannot tell "absent" from "present and empty" apart, and neither
    should this function treat them differently."""
    monkeypatch.setenv("USERDOMAIN", "")
    monkeypatch.setenv("USERNAME", "user")
    with pytest.raises(install.RunAsUserUnresolvedError, match="USERDOMAIN"):
        install._default_run_as_user()


def test_install_uses_default_run_as_user_from_the_environment_when_not_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7, S8 fix round: the default `run_as_user` branch (`install()`
    called with `run_as_user=None`) must actually reach
    `_default_run_as_user()` and use its qualified result -- not silently
    fall back to `""`. Every other `install()` test in this file passes
    `run_as_user=` explicitly, so this is the one test that exercises
    `install()`'s own `else _default_run_as_user()` branch at all."""
    monkeypatch.setenv("USERDOMAIN", "EXAMPLE")
    monkeypatch.setenv("USERNAME", "user")
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
            stderr="",
        ),
    )

    underlying_runner = runner
    captured_users = []

    def runner(argv: list[str]) -> CommandResult:
        if "/Create" in argv:
            root = ET.fromstring(Path(argv[argv.index("/XML") + 1]).read_text(encoding="utf-16"))
            captured_users.append(root.findtext("t:Principals/t:Principal/t:UserId", namespaces=NS))
        return underlying_runner(argv)

    code = run_install(pythonw=launcher, run_as_user=None, runner=runner)
    assert captured_users == ["EXAMPLE\\user"]

    assert code == 0
    assert calls[0][4] == "/XML"


def test_install_fails_loudly_when_run_as_user_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F4: a broken environment must fail before anything else runs -- the
    runner is never called, not even for `--dry-run` (a `--dry-run` line
    built around a guessed/SYSTEM account would be worse than useless)."""
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked

    def runner(argv: list[str]) -> CommandResult:
        raise AssertionError("must not run schtasks when run_as_user cannot be resolved")

    code = run_install(pythonw=launcher, run_as_user=None, dry_run=True, runner=runner)

    assert code == 1
    err = capsys.readouterr().err
    assert "USERDOMAIN" in err
    assert "USERNAME" in err
    assert "--run-as-user" in err


# --- `install()` end to end, injected runner only ---------------------------


def _runner_from(*results: CommandResult):
    calls: list[list[str]] = []
    remaining = list(results)

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        return remaining.pop(0)

    return runner, calls


def _ok(argv: list[str] = ()) -> CommandResult:  # type: ignore[assignment]
    return CommandResult(argv=list(argv), returncode=0, stdout="", stderr="")


def test_dry_run_prints_the_argv_and_never_calls_the_runner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D27: `--dry-run` registers nothing. The injected runner is never
    invoked -- checked directly, not inferred from the return code."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        calls.append(argv)
        raise AssertionError("dry-run must never call the runner")

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", dry_run=True, runner=runner)

    assert code == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "schtasks.exe" in out
    assert "snap" in out


def test_dry_run_still_reports_a_missing_launcher_before_printing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F13, S8 fix round: the `pythonw.is_file()` guard used to run
    AFTER the `--dry-run` branch, so the one command whose job is "review
    the install before it lands" could never tell you the launcher was
    missing. Moved ahead of `--dry-run` -- this must now report the missing
    launcher and never print a would-run line."""
    missing_launcher = tmp_path / "does-not-exist.exe"

    def runner(argv: list[str]) -> CommandResult:
        raise AssertionError("must not run schtasks against a missing launcher")

    code = run_install(
        pythonw=missing_launcher, run_as_user="EXAMPLE\\user", dry_run=True, runner=runner
    )

    assert code == 1
    out, err = capsys.readouterr()
    assert "does not exist" in err
    assert "Would run" not in out


def test_install_registers_then_verifies_the_logon_type(tmp_path: Path) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, calls = _runner_from(
        _ok(),  # /Create
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
            stderr="",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 0
    assert len(calls) == 2
    assert calls[0][:2] == ["schtasks.exe", "/Create"]
    assert calls[1][:2] == ["schtasks.exe", "/Query"]


def test_install_success_line_reports_verified_action_and_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F3, S8 fix round: the success line used to restate the task's action
    and interval from `install()`'s OWN inputs, with only the logon type
    actually confirmed by the read-back. Now all three come from the
    verified read-back -- this pins the wording that claims them."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, _calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
            stderr="",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 0
    out = capsys.readouterr().out
    assert "verified" in out.lower()
    assert "10 minutes" in out
    assert "snap" in out
    assert "InteractiveToken" in out


def test_install_fails_when_the_registered_action_does_not_match(tmp_path: Path) -> None:
    """F3, S8 fix round: the read-back used to check only `<LogonType>`;
    `/TR`'s quoting bug (F2/M1) could silently register the wrong action
    and `install()` would still print "verified". This is the missing
    half of point 3 -- a wrong action, on its own, must now fail."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, _calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml(
                "InteractiveToken", command=r"C:\Windows\explorer.exe", arguments="/NoUACCheck"
            ),
            stderr="",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1


def test_install_fails_when_the_registered_trigger_interval_does_not_match(tmp_path: Path) -> None:
    """F3, S8 fix round: the read-back used to never look at the trigger at
    all. A registered task on the wrong schedule (e.g. hourly instead of
    every 10 minutes) must now fail, not print "verified"."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, _calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"', interval_minutes=60),
            stderr="",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1


def test_install_fails_when_registration_itself_fails(tmp_path: Path) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, calls = _runner_from(
        CommandResult(argv=[], returncode=1, stdout="", stderr="access denied")
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1
    assert len(calls) == 1  # the query is never reached


def test_install_fails_when_the_readback_query_fails(tmp_path: Path) -> None:
    """The query's OWN returncode must be checked, independent of whatever
    its stdout happens to contain. `stdout` here is deliberately a
    perfectly valid, interactive-logon-type XML document -- if `install`
    only inspected `parse_logon_type`'s result and never checked
    `query_result.returncode` itself, this exact scenario (the command
    failed, but something parseable was still sitting in the captured
    buffer) would slip through as a false success. Mutation-verified: with
    the `query_result.returncode != 0` check deleted, a query returning
    EMPTY stdout still failed this test via the (correct, but different)
    logon-type-mismatch branch -- this scenario is what actually isolates
    the returncode check on its own.
    """
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, calls = _runner_from(
        _ok(),  # /Create succeeds
        CommandResult(
            argv=[],
            returncode=1,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
            stderr="task not found",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1
    assert len(calls) == 2


def test_install_fails_when_the_registered_logon_type_is_not_interactive(tmp_path: Path) -> None:
    """Point 3: the exact scenario §13.5 rates medium-likelihood,
    every-run-fails-forever -- `/IT` silently did not land. Must fail loudly."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, _calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[], returncode=0,
            stdout=_task_xml("Password", command=str(launcher)), stderr="",
        ),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1


def test_install_fails_when_the_readback_cannot_be_parsed_at_all(tmp_path: Path) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, _calls = _runner_from(
        _ok(),
        CommandResult(argv=[], returncode=0, stdout="garbage, not xml", stderr=""),
    )

    code = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1


def test_install_refuses_when_the_launcher_does_not_exist(tmp_path: Path) -> None:
    """§7's own D23/F4 precedent (a missing `arm.ps1`): refuse rather than
    register a task pointed at nothing. The runner must never be called."""
    missing_launcher = tmp_path / "does-not-exist.exe"

    def runner(argv: list[str]) -> CommandResult:
        raise AssertionError("must not run schtasks against a missing launcher")

    code = run_install(pythonw=missing_launcher, run_as_user="EXAMPLE\\user", runner=runner)

    assert code == 1


def test_install_second_call_with_identical_arguments_reuses_the_same_force_argv(
    tmp_path: Path,
) -> None:
    """D24 point 5. Renamed from "...is_idempotent..." (F10, S8 fix round):
    this compares two calls of a PURE function (`build_register_argv`) with
    identical arguments, which cannot fail unless that function becomes
    nondeterministic -- it is a smoke test for the whole `install()` path
    (it killed M8/M11/M14/M16 during this unit's mutation pass), not a test
    of idempotence. This project's own SAFETY rules forbid ever registering
    a real task, so idempotent overwrite itself (`/F`'s documented
    behaviour) is not, and cannot be, verified against a live scheduler
    here."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked

    def make_runner():
        return _runner_from(
            _ok(),
            CommandResult(
                argv=[],
                returncode=0,
                stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
                stderr="",
            ),
        )

    runner1, calls1 = make_runner()
    code1 = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner1)
    runner2, calls2 = make_runner()
    code2 = run_install(pythonw=launcher, run_as_user="EXAMPLE\\user", runner=runner2)

    assert code1 == 0
    assert code2 == 0
    create1, create2 = calls1[0], calls2[0]
    # The temporary filename changes; registration policy and task name do not.
    assert create1[:5] == create2[:5]
    assert create1[6:] == create2[6:]
    assert "/F" in create1


def test_install_uses_the_given_task_name_for_both_register_and_readback(tmp_path: Path) -> None:
    """F12/M2/M3, S8 fix round: every other `install()` test in this file
    uses the default task name, so "register X, read back X" was never
    distinguished from "register X, read back the default" -- the review's
    own "what if `/Query` returns XML for a DIFFERENT task?" hole. Pinned
    with a non-default name on both calls."""
    launcher = tmp_path / "pythonw.exe"
    launcher.write_bytes(b"")  # stand-in for pythonw.exe -- never executed, only .is_file() checked
    runner, calls = _runner_from(
        _ok(),
        CommandResult(
            argv=[],
            returncode=0,
            stdout=_task_xml("InteractiveToken", command=f'"{launcher}"'),
            stderr="",
        ),
    )

    code = run_install(
        pythonw=launcher, run_as_user="EXAMPLE\\user", task_name="custom-name", runner=runner
    )

    assert code == 0
    register_call, query_call = calls
    assert register_call[register_call.index("/TN") + 1] == "custom-name"
    assert query_call[query_call.index("/TN") + 1] == "custom-name"


# --- `cli.main(["install", ...])` dispatch (F1, S8 fix round) ---------------


def test_cli_main_install_dispatches_dry_run_false_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M23: SURVIVED against the original suite (`install(dry_run=True)`
    always). `cli.main(["install"])`, no flag, must reach `install.install`
    with `dry_run=False`. `install.install` is replaced WHOLESALE here
    (SAFETY: the real function, and so the real `default_runner`, is never
    reached)."""
    calls: list[bool] = []

    def fake_install(*, dry_run: bool, run_as_user: str | None = None, **_kwargs: object) -> int:
        calls.append(dry_run)
        return 0

    monkeypatch.setattr(install, "install", fake_install)

    code = cli_module.main(["install"])

    assert code == 0
    assert calls == [False]


def test_cli_main_install_dispatches_dry_run_true_when_the_flag_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M24: SURVIVED against the original suite (`install(dry_run=False)`
    regardless of the flag) -- in production, with the real
    `install.default_runner`, that mutation means `termsync install
    --dry-run` registers a REAL scheduled task (D27's whole reason for
    existing). `install.install` is replaced wholesale here; the real
    function is never reached."""
    calls: list[bool] = []

    def fake_install(*, dry_run: bool, run_as_user: str | None = None, **_kwargs: object) -> int:
        calls.append(dry_run)
        return 0

    monkeypatch.setattr(install, "install", fake_install)

    code = cli_module.main(["install", "--dry-run"])

    assert code == 0
    assert calls == [True]


def test_cli_main_install_dispatches_run_as_user_and_the_real_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--run-as-user` (F4's CLI seam) must reach `install.install`, and the
    dispatch must return whatever `install.install` returns, not swallow or
    hardcode it."""
    calls: list[str | None] = []

    def fake_install(*, dry_run: bool, run_as_user: str | None = None, **_kwargs: object) -> int:
        calls.append(run_as_user)
        return 1

    monkeypatch.setattr(install, "install", fake_install)

    code = cli_module.main(["install", "--dry-run", "--run-as-user", "EXAMPLE\\user"])

    assert code == 1
    assert calls == ["EXAMPLE\\user"]


# --- `termsync.cmd` -- real subprocess, scratch dir / scratch store only ---


@pytest.fixture
def scratch_cmd(tmp_path: Path) -> Path:
    """A copy of the real `termsync.cmd`, in its own scratch directory, so
    `%~dp0` resolves to `tmp_path` -- never the real repo -- for the
    missing-venv test below."""
    dest = tmp_path / "termsync.cmd"
    shutil.copy(REPO_ROOT / "termsync.cmd", dest)
    return dest


@pytest.fixture
def scratch_cmd_in_a_space_containing_dir(tmp_path: Path) -> Path:
    """A copy of the real `termsync.cmd` in a directory whose name contains
    a space -- M30/M31 (S8 fix round, F2's `termsync.cmd` twin). Dropping
    either quoting site (`if not exist "%TERMSYNC_PYTHON%"`, or the
    invocation `"%TERMSYNC_PYTHON%" -m termsync`) is invisible under this
    a checkout path without spaces -- both mutations
    survived the original suite for exactly that reason. A space-containing
    scratch dir is required to make the difference observable at all."""
    space_dir = tmp_path / "with space"
    space_dir.mkdir()
    dest = space_dir / "termsync.cmd"
    shutil.copy(REPO_ROOT / "termsync.cmd", dest)
    return dest


def test_termsync_cmd_forwards_arguments_and_propagates_exit_code_nonzero() -> None:
    """Run against the REAL repo checkout (so the real venv resolves) with
    an invalid subcommand -- argparse rejects it (exit 2) before any file
    I/O, so this never touches `~/.termsync`. Proves both halves at once:
    the argument reached `python -m termsync` (its own error names it), and
    the child's exit code reached this process, not a code termsync.cmd
    invented on its own. Mutation-verified against a hardcoded `exit /b 0`
    (this test goes red -- returncode 0 instead of 2); NOT verified by
    deleting the `exit /b %ERRORLEVEL%` line outright, because with nothing
    following it in this script, cmd.exe's own implicit "last command's exit
    code" behaviour makes that particular deletion a no-op here -- worth
    knowing, not a gap this test can close by construction.

    **Structural safety (F8, S8 fix round).** This call passes no `env=`,
    so it inherits `os.environ` from THIS process -- and the autouse fixture
    in `tests/conftest.py` has already redirected `USERPROFILE`/`HOME` in
    that environment to a sandboxed `tmp_path` for the duration of this
    test. Before that fixture existed, this test was safe only *by
    reasoning* (argparse exits before any I/O); it is now safe by
    construction too, the same shape as the `cwd` incident this fix round's
    brief names.
    """
    result = subprocess.run(
        [str(REPO_ROOT / "termsync.cmd"), "not-a-real-subcommand"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT.parent),  # deliberately NOT the repo -- %~dp0 must still resolve
    )

    assert result.returncode == 2
    assert "not-a-real-subcommand" in (result.stdout + result.stderr)


def test_termsync_cmd_forwards_arguments_and_propagates_exit_code_zero(tmp_path: Path) -> None:
    """A safe, zero-I/O-on-the-real-store path: `restore --dry-run` against
    an empty SCRATCH store (never `~/.termsync`, per SAFETY)."""
    scratch_store = tmp_path / "store"
    result = subprocess.run(
        [str(REPO_ROOT / "termsync.cmd"), "restore", "--dry-run", "--store", str(scratch_store)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT.parent),
    )

    assert result.returncode == 0
    assert "No desk captured yet" in result.stdout


def test_termsync_cmd_refuses_a_missing_venv_python_loudly_and_without_fallback(
    scratch_cmd: Path, tmp_path: Path
) -> None:
    """The scratch copy's directory has no `.venv` at all -- `%~dp0` still
    resolves to IT, not the real repo, so this exercises the missing-venv
    branch without ever touching the real `.venv`. Must name the expected
    path and exit non-zero; must NOT silently fall back to a bare `python`
    on PATH (this test's own PATH still has one -- proving the branch fires
    on existence, not on `python`'s absence).

    **SAFETY, defense in depth.** `cwd` is pinned to `tmp_path` (never the
    repo) and `--store` is passed explicitly, EVEN THOUGH the correct
    script never reaches `python -m termsync` at all here. This is not
    redundant: a mutation of `termsync.cmd` that drops `%~dp0` (resolving
    `.venv\\Scripts\\python.exe` relative to the CALLER's cwd instead of the
    script's own location) was caught live during this unit's own mutation
    pass -- and because an earlier version of this exact test omitted `cwd`,
    that mutation resolved the real repo's real `.venv` and ran a REAL
    `termsync snap` against the real `~/.termsync` on the development
    machine before the omission was caught. Both defences here close that
    specific hole: a broken `%~dp0` now has no real venv to fall back onto
    (`tmp_path` has none), and even if it somehow did, `--store` would still
    point away from `~/.termsync`. `tests/conftest.py`'s autouse fixture
    (F8) is a THIRD, structural layer on top of both.
    """
    result = subprocess.run(
        [str(scratch_cmd), "snap", "--store", str(tmp_path / "store")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "venv python not found" in combined
    assert str(scratch_cmd.parent / ".venv" / "Scripts" / "python.exe") in combined


def test_termsync_cmd_if_not_exist_check_is_quoted_so_a_space_in_the_path_does_not_break_it(
    scratch_cmd_in_a_space_containing_dir: Path, tmp_path: Path
) -> None:
    """M31, S8 fix round: dropping the quotes around `%TERMSYNC_PYTHON%` in
    `if not exist "%TERMSYNC_PYTHON%" (` survived the original suite
    because no scratch path used in it contained a space. Probed directly
    (this fix round's own report): under a space-containing path, the
    UNQUOTED form makes cmd.exe emit an extra, spurious
    "The system cannot find the path specified." line on stderr before the
    script's own (correctly-worded, but now preceded by noise) error --
    while the QUOTED form (the real, shipped script) never does. That
    spurious line is the mutation's fingerprint."""
    space_launcher = scratch_cmd_in_a_space_containing_dir
    result = subprocess.run(
        [str(space_launcher), "snap", "--store", str(tmp_path / "store")],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(space_launcher.parent),
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "venv python not found" in combined
    assert "The system cannot find the path specified." not in combined


def test_termsync_cmd_invocation_is_quoted_so_a_space_in_the_path_does_not_split_the_program(
    scratch_cmd_in_a_space_containing_dir: Path, tmp_path: Path
) -> None:
    """M30, S8 fix round: dropping the quotes around `%TERMSYNC_PYTHON%` in
    the invocation line (`"%TERMSYNC_PYTHON%" -m termsync %*`) survived the
    original suite for the same reason as M31. Probed directly: with a
    `.venv\\Scripts\\python.exe` stand-in present (any real executable
    satisfies the `if exist` check -- this uses a copy of the system
    `hostname.exe`, never the real venv) at a space-containing path, the
    UNQUOTED invocation makes cmd.exe split the path at the first space and
    try to run everything before it as the program name, failing with
    "... is not recognized as an internal or external command" -- while the
    QUOTED form (the real, shipped script) actually launches the target."""
    space_launcher = scratch_cmd_in_a_space_containing_dir
    venv_scripts = space_launcher.parent / ".venv" / "Scripts"
    venv_scripts.mkdir(parents=True)
    # A real, always-present system executable stands in for python.exe --
    # it only has to exist and run; it need not understand `-m termsync`.
    shutil.copy(Path(r"C:\Windows\System32\hostname.exe"), venv_scripts / "python.exe")

    result = subprocess.run(
        [str(space_launcher), "foo", "bar"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(space_launcher.parent),
        stdin=subprocess.DEVNULL,
    )

    combined = result.stdout + result.stderr
    assert "is not recognized as an internal or external command" not in combined


# --- prune wiring (D26), via the real `cli.snap` -----------------------------


class RecordingPruneStore(Store):
    """A REAL `Store` (this project keeps no fake `Store` anywhere) whose
    `prune` is wrapped to record every call before delegating -- mirrors
    `test_restore_cmdline.py`'s `RecordingSpawner` shape."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.prune_calls: list[tuple[int, frozenset[int]]] = []

    def prune(self, now: int, keep: frozenset[int]) -> list[int]:
        self.prune_calls.append((now, keep))
        return super().prune(now, keep)


class RaisingPruneStore(Store):
    """A REAL `Store` whose `prune` always raises -- for the "a prune
    failure must never fail the capture" requirement."""

    def prune(self, now: int, keep: frozenset[int]) -> list[int]:
        raise RuntimeError("simulated prune failure")


def test_snap_prunes_with_keep_containing_the_target_and_the_just_written_version(
    tmp_path: Path,
) -> None:
    """Two boots: boot 1000's capture holds 2 sessions (the working desk);
    boot 2000's capture (this run) holds 1 session (freshly booted, small).
    `select_target` picks the LARGER same-recent-boot layout (§7 step 1) --
    version 1 -- as `target.layout`, while version 2 (this run's own write)
    is `target.newest`. `keep` must hold both, proving `_prune_after_capture`
    reads a layout list that includes the version THIS run just wrote, not
    a stale one from before the write.
    """
    rec_store = RecordingPruneStore(tmp_path / "store")

    code1 = snap(
        rec_store,
        emit_runner=lambda: emit_of(entry(sessionId="a"), entry(sessionId="b"), boot_time=1000),
        clock=clock_returning(1_000_000),
    )
    assert code1 == 0
    v1 = rec_store.newest()
    assert v1 is not None and v1.version == 1

    code2 = snap(
        rec_store,
        emit_runner=lambda: emit_of(entry(sessionId="c"), boot_time=2000),
        clock=clock_returning(2_000_000),
    )
    assert code2 == 0
    v2 = rec_store.newest()
    assert v2 is not None and v2.version == 2

    assert len(rec_store.prune_calls) == 2
    now2, keep2 = rec_store.prune_calls[1]
    assert now2 == 2_000_000
    assert keep2 == frozenset({1, 2})


def test_snap_also_protects_the_newest_restore_record_from_pruning(tmp_path: Path) -> None:
    """This unit's own decision on the S6/D20 cross-unit question (see
    `cli._prune_after_capture`'s docstring): the newest `source: "restore"`
    version is added to `keep` too, even though `select_target` (D20) can
    never surface it as `target.layout`/`target.newest` itself.

    F5, S8 fix round: the ORIGINAL version of this test built exactly ONE
    restore record, so `min(restore_versions) == max(restore_versions)` and
    the word "newest" in the test's own name was never actually asserted
    (M19 -- `max` mutated to `min` -- survived unnoticed). This version
    builds TWO restore records with different session sets (so neither
    dedups into the other -- `Store._can_dedup_into`'s first condition,
    same hash, would otherwise collapse them into one version) at different
    versions, and asserts the NEWER one is protected while the OLDER one
    is NOT -- which is exactly what `max` vs `min` disagree on.
    """
    rec_store = RecordingPruneStore(tmp_path / "store")

    snap(
        rec_store,
        emit_runner=lambda: emit_of(entry(sessionId="a"), boot_time=2000),
        clock=clock_returning(1_000_000),
    )
    v1 = rec_store.newest()
    assert v1 is not None and v1.version == 1

    older_restore = rec_store.write_restore(
        sessions=(Session(session_id="a", cwd=r"C:\fixture\tool", title="t"),),
        restored_at=1_200_000,
        boot_id=2000,
    )
    assert older_restore.layout.source == "restore"
    assert older_restore.layout.version == 2

    newer_restore = rec_store.write_restore(
        # A DIFFERENT session set than the older restore record's, so this
        # allocates a new version instead of dedup-refreshing version 2's.
        sessions=(Session(session_id="b", cwd=r"C:\fixture\tool", title="t"),),
        restored_at=1_500_000,
        boot_id=2000,
    )
    assert newer_restore.layout.source == "restore"
    assert newer_restore.layout.version == 3

    snap(
        rec_store,
        emit_runner=lambda: emit_of(entry(sessionId="a"), entry(sessionId="d"), boot_time=2000),
        clock=clock_returning(2_000_000),
    )
    v4 = rec_store.newest()
    assert v4 is not None and v4.version == 4

    _now, keep = rec_store.prune_calls[-1]
    assert 3 in keep  # the NEWEST restore record -- this unit's own decision
    assert 2 not in keep  # the OLDER restore record -- NOT protected by this mechanism
    assert 4 in keep  # this run's own just-written capture


def test_snap_a_raising_prune_does_not_fail_the_capture_or_change_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store_that_always_fails_to_prune = RaisingPruneStore(tmp_path / "store")

    code = snap(
        store_that_always_fails_to_prune,
        emit_runner=lambda: emit_of(entry(sessionId="a"), boot_time=1000),
        clock=clock_returning(1_000_000),
    )

    assert code == 0
    status = store_that_always_fails_to_prune.read_status()
    assert status is not None
    assert status.outcome == "ok"
    assert status.consecutive_failures == 0
    err = capsys.readouterr().err
    assert "prune failed" in err


def test_prune_after_capture_on_an_empty_layout_list_prunes_nothing_without_raising(
    tmp_path: Path,
) -> None:
    """Direct unit test of the helper (mirrors `test_dry_run.py`'s own
    precedent of driving `cli._private_helpers` straight): `select_target`
    returns `None` on an empty series (D20's own contract), and this must
    be a no-op, never an assertion. `cli.snap` itself can never reach this
    exact state (it always calls the helper AFTER writing a layout), so it
    is verified against the helper directly rather than through `snap`.
    """
    empty_store = Store(tmp_path / "store")

    cli_module._prune_after_capture(empty_store, now=1_000_000, boot_id=1000)  # must not raise

    assert empty_store.all_layouts() == []


# --- the real Task Scheduler shape, measured (S8 setup run) --------------


def test_parse_task_action_strips_task_schedulers_own_quotes_around_the_command() -> None:
    """MEASURED against a real registered task, not assumed.

    `/TR` requires the program path double-quoted so schtasks can split
    program from arguments, and Task Scheduler stores those quotes verbatim
    inside `<Command>`. `install()` compares the read-back against
    `str(pythonw)` (this module's `termsync_cmd` parameter at the time this
    was measured, renamed by D30), which is unquoted, so without the strip
    `termsync install` fails 100% of the time -- exit 1, with the task
    correctly registered. Every success-path fixture in this file used to
    feed an UNQUOTED `<Command>`, so the whole suite agreed with itself and
    not with Windows.

    This preserves the `<Actions>` shape measured with `schtasks /Query /TN
    termsync-snap /XML ONE` before D30's windowless fix, with a synthetic
    command path. The action was still `termsync.cmd snap` at that time.
    It retains that shape rather than the current
    `pythonw.exe`/`-m termsync snap` shape: `parse_task_action`'s
    quote-stripping is generic over whatever command string schtasks hands
    back, so this XML still exercises exactly the behaviour it was measured
    to pin -- Task Scheduler quoting the `<Command>` value -- regardless of
    which action `install()` currently registers."""
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        '<Actions Context="Author"><Exec>'
        r'<Command>"C:\fixture\tool\termsync.cmd"</Command>'
        "<Arguments>snap</Arguments>"
        "</Exec></Actions></Task>"
    )

    assert install.parse_task_action(xml) == (r"C:\fixture\tool\termsync.cmd", "snap")


def test_parse_task_action_leaves_an_unquoted_command_alone() -> None:
    """The strip must not eat anything from a command that was never quoted
    -- a one-sided strip would be its own defect."""
    xml = (
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        r"<Actions><Exec><Command>C:\Windows\explorer.exe</Command>"
        "<Arguments>/NoUACCheck</Arguments></Exec></Actions></Task>"
    )

    assert install.parse_task_action(xml) == (r"C:\Windows\explorer.exe", "/NoUACCheck")


@pytest.mark.parametrize("name,expected", EXPECTED_SETTINGS.items())
def test_xml_has_each_required_setting(name: str, expected: str) -> None:
    root = ET.fromstring(install.build_task_xml(Path("pythonw.exe"), "DOMAIN\\user"))
    assert root.findtext(f"t:Settings/t:{name}", namespaces=NS) == expected


def test_xml_has_only_the_requested_triggers_with_escaped_subscriptions_in_schema_order() -> None:
    xml = install.build_task_xml(Path("pythonw.exe"), "DOMAIN\\user")
    root = ET.fromstring(xml)
    triggers = root.find("t:Triggers", NS)
    assert [node.tag.split("}")[-1] for node in triggers] == [
        "TimeTrigger", "EventTrigger", "EventTrigger", "EventTrigger"
    ]
    timer = triggers[0]
    assert [node.tag.split("}")[-1] for node in timer] == [
        "Enabled", "StartBoundary", "Repetition"
    ]
    assert timer.findtext("t:Enabled", namespaces=NS) == "true"
    assert timer.findtext("t:StartBoundary", namespaces=NS) == "2000-01-01T00:00:00"
    assert timer.findtext("t:Repetition/t:Interval", namespaces=NS) == "PT10M"
    assert timer.findtext("t:Repetition/t:StopAtDurationEnd", namespaces=NS) == "false"
    assert timer.find("t:Repetition/t:Duration", NS) is None
    for event, (name, expected_query) in zip(triggers[1:], EXPECTED_QUERIES.items(), strict=True):
        assert event.attrib == {"id": name}
        assert event.findtext("t:Enabled", namespaces=NS) == "true"
        subscription = event.find("t:Subscription", NS)
        assert len(subscription) == 0  # text, not unescaped nested elements
        query_list = ET.fromstring(subscription.text)
        assert query_list.tag == "QueryList"
        query = query_list.find("Query")
        assert query.attrib == {"Id": "0", "Path": "System"}
        assert query.find("Select").attrib == {"Path": "System"}
        assert query.findtext("Select") == expected_query
        children = [node.tag.split("}")[-1] for node in event]
        assert children == ["Enabled", "Subscription"] + (["Delay"] if name == "resume" else [])
        assert event.findtext("t:Delay", namespaces=NS) == ("PT5S" if name == "resume" else None)
    assert xml.count("&lt;QueryList&gt;") == 3
    assert "<QueryList>" not in xml


@pytest.mark.parametrize("result_code", [0, 1])
def test_install_temp_file_is_closed_utf16_and_removed_after_registration(
    tmp_path: Path, result_code: int
) -> None:
    launcher = tmp_path / "with space" / "pythonw.exe"
    launcher.parent.mkdir()
    launcher.write_bytes(b"")
    paths = []

    def runner(argv: list[str]) -> CommandResult:
        if "/Create" in argv:
            path = Path(argv[argv.index("/XML") + 1])
            paths.append(path)
            raw = path.read_bytes()
            assert raw.startswith(b"\xff\xfe")
            assert raw.decode("utf-16") == install.build_task_xml(launcher, "DOMAIN\\a\u00e4&b")
            assert ET.fromstring(raw).findtext(
                "t:Principals/t:Principal/t:UserId", namespaces=NS
            ) == "DOMAIN\\a\u00e4&b"
            # Opening for update also proves NamedTemporaryFile was closed on Windows.
            with path.open("r+b"):
                pass
            return CommandResult(argv, result_code, "", "test failure" if result_code else "")
        assert not paths[0].exists()
        return CommandResult(argv, 0, _task_xml("InteractiveToken", command=str(launcher)), "")

    assert run_install(
        pythonw=launcher, run_as_user="DOMAIN\\a\u00e4&b", runner=runner
    ) == result_code
    assert len(paths) == 1 and not paths[0].exists()


def test_install_removes_temp_file_when_runner_raises(tmp_path: Path) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.touch()
    paths = []

    def runner(argv: list[str]) -> CommandResult:
        paths.append(Path(argv[argv.index("/XML") + 1]))
        raise OSError("test runner failed")

    assert run_install(pythonw=launcher, run_as_user="DOMAIN\\user", runner=runner) == 1
    assert len(paths) == 1 and not paths[0].exists()


def _assert_readback_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str, value: str | None,
    expected_error: str,
) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.touch()
    good_xml = _task_xml("InteractiveToken", command=str(launcher))
    assert install.verify_task_xml(good_xml, launcher) is None
    root = ET.fromstring(good_xml)
    node = root.find(path, NS)
    assert node is not None
    if value is None:
        parent = next(parent for parent in root.iter() if node in parent)
        parent.remove(node)
    else:
        node.text = value
    bad_xml = ET.tostring(root, encoding="unicode")
    runner, calls = _runner_from(_ok(), CommandResult([], 0, bad_xml, ""))
    assert run_install(pythonw=launcher, run_as_user="DOMAIN\\user", runner=runner) == 1
    assert len(calls) == 2
    assert expected_error in capsys.readouterr().err


@pytest.mark.parametrize("name,expected", EXPECTED_SETTINGS.items())
def test_readback_rejects_each_wrong_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, expected: str
) -> None:
    wrong = "true" if expected == "false" else "false"
    _assert_readback_rejected(
        tmp_path, capsys, f"t:Settings/t:{name}", wrong, f"setting {name}"
    )


@pytest.mark.parametrize("name", [
    "DisallowStartIfOnBatteries", "StopIfGoingOnBatteries",
    "StartWhenAvailable", "ExecutionTimeLimit",
])
def test_readback_rejects_missing_nondefault_setting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    _assert_readback_rejected(tmp_path, capsys, f"t:Settings/t:{name}", None, f"setting {name}")


# Microsoft task-scheduler-schema defaults; also observed together in the
# real task's 2026-10-21 read-back (except MultipleInstancesPolicy, kept by Windows).
DEFAULT_VALUED_PATHS = [
    *(f"t:Settings/t:{name}" for name in (
        "WakeToRun", "RunOnlyIfIdle", "Enabled", "Hidden", "MultipleInstancesPolicy",
    )),
    "t:Triggers/t:TimeTrigger/t:Enabled",
    "t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd",
    *(f"t:Triggers/t:EventTrigger[@id='{name}']/t:Enabled"
      for name in ("shutdown", "sleep", "resume")),
]


@pytest.mark.parametrize("paths", [[path] for path in DEFAULT_VALUED_PATHS]
                         + [DEFAULT_VALUED_PATHS], ids=None)
def test_install_accepts_omitted_defaults_from_scheduler(
    tmp_path: Path, paths: list[str]
) -> None:
    launcher = tmp_path / "pythonw.exe"
    launcher.touch()
    root = ET.fromstring(_task_xml("InteractiveToken", command=str(launcher)))
    for path in paths:
        node = root.find(path, NS)
        assert node is not None
        next(parent for parent in root.iter() if node in parent).remove(node)
    runner, calls = _runner_from(_ok(), CommandResult([], 0, ET.tostring(root, "unicode"), ""))
    assert run_install(pythonw=launcher, run_as_user="DOMAIN\\user", runner=runner) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("path", DEFAULT_VALUED_PATHS)
def test_readback_rejects_explicit_empty_default_valued_fields(path: str) -> None:
    launcher = Path("pythonw.exe")
    root = ET.fromstring(_task_xml("InteractiveToken", command=str(launcher)))
    root.find(path, NS).text = ""
    assert install.verify_task_xml(ET.tostring(root, "unicode"), launcher) is not None


@pytest.mark.parametrize("name", ["shutdown", "sleep", "resume"])
@pytest.mark.parametrize("change", ["remove", "subscription", "disabled", "delay"])
def test_readback_rejects_each_missing_changed_disabled_or_delayed_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, change: str
) -> None:
    path = f"t:Triggers/t:EventTrigger[@id='{name}']"
    if change == "remove":
        value, message = None, "exactly three event triggers"
    elif change == "subscription":
        path += "/t:Subscription"
        # Well-formed query XML whose predicate is wrong, not merely unparseable XML.
        value = (
            '<QueryList><Query Id="0" Path="System"><Select Path="System">'
            + EXPECTED_QUERIES[name].replace("EventID=", "EventID=9")
            + '</Select></Query></QueryList>'
        )
        message = f"{name} event trigger subscription"
    elif change == "disabled":
        path += "/t:Enabled"
        value, message = "false", f"{name} event trigger is not enabled"
    elif name == "resume":
        path += "/t:Delay"
        value, message = "PT6S", "resume event trigger Delay"
    else:
        # Undelayed events need their own fixture with a newly added Delay.
        launcher = tmp_path / "pythonw.exe"
        launcher.touch()
        xml = _task_xml("InteractiveToken", command=str(launcher))
        assert install.verify_task_xml(xml, launcher) is None
        root = ET.fromstring(xml)
        ET.SubElement(root.find(path, NS), "{" + NS["t"] + "}Delay").text = "PT5S"
        runner, _ = _runner_from(_ok(), CommandResult([], 0, ET.tostring(root, "unicode"), ""))
        assert run_install(pythonw=launcher, run_as_user="DOMAIN\\user", runner=runner) == 1
        assert f"{name} event trigger Delay" in capsys.readouterr().err
        return
    _assert_readback_rejected(tmp_path, capsys, path, value, message)


@pytest.mark.parametrize("path,value,message", [
    ("t:Triggers/t:TimeTrigger", None, "exactly one TimeTrigger"),
    ("t:Triggers/t:TimeTrigger/t:Enabled", "false", "TimeTrigger Enabled"),
    ("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", "PT11M", "Repetition/Interval"),
    ("t:Triggers/t:TimeTrigger/t:Repetition/t:StopAtDurationEnd", "true", "StopAtDurationEnd"),
    ("t:Actions/t:Exec/t:Arguments", "snap", "action command or arguments"),
    ("t:Principals/t:Principal/t:LogonType", "S4U", "logon type"),
])
def test_readback_rejects_time_trigger_action_and_principal_mismatches_in_isolation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str, value: str | None, message: str
) -> None:
    _assert_readback_rejected(tmp_path, capsys, path, value, message)


def test_print_xml_outputs_only_xml_without_runner_or_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: object, **kwargs: object):
        raise AssertionError("print-xml must not register or write a temp file")

    monkeypatch.setattr(install.tempfile, "NamedTemporaryFile", forbidden)
    launcher = tmp_path / "missing pythonw.exe"
    assert run_install(
        pythonw=launcher, run_as_user="DOMAIN\\user", print_xml=True, runner=forbidden
    ) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert out == install.build_task_xml(launcher, "DOMAIN\\user") + "\n"
    assert ET.fromstring(out).tag == "{" + NS["t"] + "}Task"


def test_cli_main_install_dispatches_print_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_install(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(install, "install", fake_install)
    assert cli_module.main(["install", "--print-xml"]) == 0
    assert calls == [{"dry_run": False, "print_xml": True, "run_as_user": None}]


def test_readback_rejects_sleep_subscription_without_hybrid_shutdown_exclusion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wrong_query = EXPECTED_QUERIES["sleep"].split(" and *[EventData")[0]
    subscription = (
        '<QueryList><Query Id="0" Path="System"><Select Path="System">'
        + wrong_query + '</Select></Query></QueryList>'
    )
    _assert_readback_rejected(
        tmp_path, capsys, "t:Triggers/t:EventTrigger[@id='sleep']/t:Subscription",
        subscription, "sleep event trigger subscription",
    )


def test_readback_rejects_extra_lock_trigger(tmp_path: Path) -> None:
    launcher = tmp_path / "pythonw.exe"
    xml = _task_xml("InteractiveToken", command=str(launcher))
    assert install.verify_task_xml(xml, launcher) is None
    root = ET.fromstring(xml)
    ET.SubElement(root.find("t:Triggers", NS), "{" + NS["t"] + "}SessionStateChangeTrigger")
    mismatch = install.verify_task_xml(ET.tostring(root, "unicode"), launcher)
    assert "unexpected extra trigger" in mismatch


@pytest.mark.parametrize("user", ["", "   "])
def test_install_refuses_explicit_empty_account_before_runner(user: str, tmp_path: Path) -> None:
    def forbidden(argv: list[str]) -> CommandResult:
        raise AssertionError("empty account must not reach scheduler")

    assert run_install(
        pythonw=tmp_path / "pythonw.exe", run_as_user=user, print_xml=True, runner=forbidden
    ) == 1


def test_print_xml_still_refuses_an_unresolved_default_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)

    def forbidden(argv: list[str]) -> CommandResult:
        raise AssertionError("unresolved account must not reach scheduler")

    assert run_install(pythonw=tmp_path / "pythonw.exe", print_xml=True, runner=forbidden) == 1
