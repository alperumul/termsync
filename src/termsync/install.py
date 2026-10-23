"""Register and verify the interactive, windowless capture task.

Resolve the complete account before registration. Scheduler I/O uses an
injectable runner; tests must never invoke the real scheduler. schtasks can
return single-byte stdout with a UTF-16 XML declaration, so decode to text
before parsing. Registration files themselves are written as UTF-16.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from termsync.console import safe_print

#: §13.3 S8 / D24: the task registered by this module.
TASK_NAME = "termsync-snap"

# Windowless task action; the source checkout's venv must remain in place.
TASK_ACTION_ARGS = "-m termsync snap"

TRIGGER_INTERVAL_MINUTES = 10

# Capture needs access to the logged-in user's desktop.
INTERACTIVE_LOGON_TYPE = "InteractiveToken"

_TASK_SCHEDULER_XML_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"

# Resolve the windowless interpreter from the source checkout, not caller cwd.
DEFAULT_PYTHONW = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "pythonw.exe"


@dataclass(frozen=True)
class CommandResult:
    """The command, exit code, and decoded output of a scheduler call."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


# Exact argv in; decoded command result out.
CommandRunner = Callable[[list[str]], CommandResult]


class RunAsUserUnresolvedError(RuntimeError):
    """The full task account cannot be determined safely; install reports exit 1."""


def default_runner(argv: list[str]) -> CommandResult:
    """Run a real scheduler command without a shell; tests must inject a fake.

    Close stdin and bound the wait so unattended prompts or stalled RPC cannot
    block installation indefinitely. Installation inherits the caller's console;
    periodic capture uses the separate windowless Python action.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            argv=argv,
            returncode=1,
            stdout=exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "",
            stderr=(
                f"{argv[0]} did not return within 30s -- treating this as a failure rather "
                "than blocking install() forever. If this recurs, check whether Task "
                "Scheduler is waiting on input (a credential prompt, an elevation prompt) "
                "that a non-interactive caller can never satisfy."
            ),
        )
    return CommandResult(
        argv=argv,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace"),
        stderr=result.stderr.decode("utf-8", errors="replace"),
    )


def _default_run_as_user() -> str:
    """Resolve DOMAIN\\user without guessing an account.

    Require both environment components: an empty /RU can select SYSTEM.
    An explicit --run-as-user is handled by install before this fallback.
    """
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    missing = [name for name, value in (("USERDOMAIN", domain), ("USERNAME", user)) if not value]
    if missing:
        joined = " and ".join(missing)
        verb = "is" if len(missing) == 1 else "are"
        raise RunAsUserUnresolvedError(
            f"cannot determine the account to register the task as: {joined} {verb} missing "
            "or empty in the environment. An empty/partial value is never safe here -- "
            "schtasks documents an empty /RU as the SYSTEM account, and a partial one is "
            "rejected outright. Pass the account explicitly: "
            "termsync install --run-as-user DOMAIN\\user"
        )
    return f"{domain}\\{user}"


# D31: a fixed past local boundary anchors the indefinite ten-minute poll.
# Reinstalling or printing XML therefore does not move its cadence.
START_BOUNDARY = "2000-01-01T00:00:00"
TASK_SETTINGS = {
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
#: Documented Task Scheduler defaults for the settings it omits when read back.
_SETTING_DEFAULTS = {
    "MultipleInstancesPolicy": "IgnoreNew",
    "WakeToRun": "false",
    "RunOnlyIfIdle": "false",
    "Enabled": "true",
    "Hidden": "false",
}
EVENT_QUERIES = (
    ("shutdown", "*[System[Provider[@Name='User32'] and EventID=1074]]", None),
    (
        "sleep",
        (
            "*[System[Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=42]]"
            " and *[EventData[Data[@Name='TargetState'] != 6]]"
        ),
        None,
    ),
    (
        "resume",
        "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]",
        "PT5S",
    ),
)


def _subscription(query: str) -> str:
    root = ET.Element("QueryList")
    item = ET.SubElement(root, "Query", Id="0", Path="System")
    ET.SubElement(item, "Select", Path="System").text = query
    return ET.tostring(root, encoding="unicode")


def build_task_xml(pythonw: Path, run_as_user: str) -> str:
    """Pure XML definition; Subscription contains escaped query XML.

    Microsoft Learn's task-scheduler-schema XSD: triggerBaseType's sequence
    precedes eventTriggerType's Subscription, then Delay. Settings and the
    task's top-level children use xs:all (no ordering constraint).
    """
    root = ET.Element("Task", version="1.3", xmlns=_TASK_SCHEDULER_XML_NS[1:-1])
    triggers = ET.SubElement(root, "Triggers")
    timer = ET.SubElement(triggers, "TimeTrigger")
    ET.SubElement(timer, "Enabled").text = "true"
    ET.SubElement(timer, "StartBoundary").text = START_BOUNDARY
    repetition = ET.SubElement(timer, "Repetition")
    ET.SubElement(repetition, "Interval").text = f"PT{TRIGGER_INTERVAL_MINUTES}M"
    ET.SubElement(repetition, "StopAtDurationEnd").text = "false"
    for name, query, delay in EVENT_QUERIES:
        event = ET.SubElement(triggers, "EventTrigger", id=name)
        ET.SubElement(event, "Enabled").text = "true"
        ET.SubElement(event, "Subscription").text = _subscription(query)
        if delay is not None:
            ET.SubElement(event, "Delay").text = delay
    principal = ET.SubElement(ET.SubElement(root, "Principals"), "Principal", id="Author")
    ET.SubElement(principal, "UserId").text = run_as_user
    ET.SubElement(principal, "LogonType").text = INTERACTIVE_LOGON_TYPE
    settings = ET.SubElement(root, "Settings")
    for name, value in TASK_SETTINGS.items():
        ET.SubElement(settings, name).text = value
    action = ET.SubElement(ET.SubElement(root, "Actions", Context="Author"), "Exec")
    # D24/D30: preserve the quoted executable path, separately from arguments.
    ET.SubElement(action, "Command").text = f'"{pythonw}"'
    ET.SubElement(action, "Arguments").text = TASK_ACTION_ARGS
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")


def build_register_argv(xml_path: Path, task_name: str = TASK_NAME) -> list[str]:
    """Pure argv; the account and interactive logon type live in the XML."""
    return ["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"]


def build_query_argv(task_name: str = TASK_NAME) -> list[str]:
    """The read-back argv (point 3): structured XML, not `/FO LIST` text --
    see this module's docstring for why. Pure, mirrors `build_register_argv`.
    """
    return ["schtasks.exe", "/Query", "/TN", task_name, "/XML", "ONE"]


def parse_logon_type(xml_text: str) -> str | None:
    """Extract `<Principals><Principal><LogonType>` from a
    `schtasks /Query .../XML ONE` read-back. Pure -- no I/O.

    Returns `None` on unparseable XML or a missing/empty element -- never
    raises. `install()` treats `None` the same as a wrong value: both mean
    "the logon type could not be confirmed interactive," and point 3's whole
    reason for existing is to fail loudly rather than assume.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    node = root.find(f".//{_TASK_SCHEDULER_XML_NS}LogonType")
    if node is None or not node.text:
        return None
    return node.text.strip()


def parse_task_action(xml_text: str) -> tuple[str, str] | None:
    """Read the task action from decoded scheduler XML.

    Strip an optional pair of command quotes; retain arguments for exact
    verification against the configured windowless capture command.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    command_node = root.find(
        f".//{_TASK_SCHEDULER_XML_NS}Actions/{_TASK_SCHEDULER_XML_NS}Exec"
        f"/{_TASK_SCHEDULER_XML_NS}Command"
    )
    if command_node is None or not command_node.text:
        return None
    arguments_node = root.find(
        f".//{_TASK_SCHEDULER_XML_NS}Actions/{_TASK_SCHEDULER_XML_NS}Exec"
        f"/{_TASK_SCHEDULER_XML_NS}Arguments"
    )
    arguments = (
        arguments_node.text.strip() if arguments_node is not None and arguments_node.text else ""
    )
    # Scheduler read-back can retain quotes around the executable path.
    command = command_node.text.strip()
    if len(command) >= 2 and command.startswith('"') and command.endswith('"'):
        command = command[1:-1]
    return command, arguments


def _xml_shape(node: ET.Element) -> tuple:
    """Compare subscription content without depending on XML formatting."""
    return (
        node.tag,
        sorted(node.attrib.items()),
        (node.text or "").strip(),
        tuple(_xml_shape(child) for child in node),
    )


def verify_task_xml(xml_text: str, pythonw: Path) -> str | None:
    """D29 extended: return the first specific mismatch, or None on success.

    D35: compare subscription structure, ignoring formatting and event order.
    D36: omitted default-valued settings and trigger fields retain their
    schema defaults. Explicit empty or wrong values still fail verification.
    Accept omitted or zero immediate delay.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "read-back is not parseable XML"
    ns = {"t": _TASK_SCHEDULER_XML_NS[1:-1]}
    if parse_logon_type(xml_text) != INTERACTIVE_LOGON_TYPE:
        return f"logon type is not {INTERACTIVE_LOGON_TYPE!r} (run only when logged on)"
    if parse_task_action(xml_text) != (str(pythonw), TASK_ACTION_ARGS):
        return "action command or arguments do not match the requested pythonw snap action"
    if len(root.findall("t:Actions/t:Exec", ns)) != 1:
        return "expected exactly one Exec action"
    for name, expected in TASK_SETTINGS.items():
        actual = root.findtext(f"t:Settings/t:{name}", namespaces=ns)
        # The scheduler drops a setting that equals its documented default
        # from the read-back (live: WakeToRun=false comes back absent), so an
        # absent element is the default, not a mismatch.
        if actual is None:
            actual = _SETTING_DEFAULTS.get(name)
        if actual != expected:
            return f"setting {name} reads {actual!r}, expected {expected!r}"
    timers = root.findall("t:Triggers/t:TimeTrigger", ns)
    if len(timers) != 1:
        return "expected exactly one TimeTrigger"
    for path, expected, default in (
        ("Enabled", "true", "true"),
        ("Repetition/Interval", f"PT{TRIGGER_INTERVAL_MINUTES}M", None),
        ("Repetition/StopAtDurationEnd", "false", "false"),
    ):
        actual = timers[0].findtext(
            "/".join("t:" + part for part in path.split("/")), default=default, namespaces=ns
        )
        if actual != expected:
            return f"TimeTrigger {path} reads {actual!r}, expected {expected!r}"
    events = root.findall("t:Triggers/t:EventTrigger", ns)
    if len(events) != 3:
        return "expected exactly three event triggers (shutdown, sleep, resume)"
    for name, query, delay in EVENT_QUERIES:
        expected_shape = _xml_shape(ET.fromstring(_subscription(query)))
        matches = []
        for event in events:
            try:
                shape = _xml_shape(ET.fromstring(event.findtext("t:Subscription", "", ns)))
            except ET.ParseError:
                continue
            if shape == expected_shape:
                matches.append(event)
        if len(matches) != 1:
            return f"{name} event trigger subscription missing, duplicated, or different"
        event = matches[0]
        if event.findtext("t:Enabled", default="true", namespaces=ns) != "true":
            return f"{name} event trigger is not enabled"
        actual_delay = event.findtext("t:Delay", namespaces=ns)
        if actual_delay != delay and not (delay is None and actual_delay in ("PT0S", "PT0M")):
            return f"{name} event trigger Delay reads {actual_delay!r}, expected {delay!r}"
    triggers = root.find("t:Triggers", ns)
    if triggers is None or len(triggers) != 4:
        return "unexpected extra trigger (only time, shutdown, sleep, resume are allowed)"
    return None


def install(
    *,
    pythonw: Path = DEFAULT_PYTHONW,
    task_name: str = TASK_NAME,
    run_as_user: str | None = None,
    dry_run: bool = False,
    print_xml: bool = False,
    runner: CommandRunner = default_runner,
    out: IO[str] | None = None,
) -> int:
    """Register from UTF-16 XML, then verify the task through /XML ONE.

    D32: --print-xml needs a resolved account but no installed launcher;
    --dry-run retains the missing-launcher guard and shows XML plus a
    registration argv with a temporary-file placeholder. Neither calls
    the runner or writes a temporary file.
    """
    out_stream = out if out is not None else sys.stdout
    err_stream = out if out is not None else sys.stderr

    def _out(text: str) -> None:
        safe_print(text, file=out_stream)

    def _err(text: str) -> None:
        safe_print(text, file=err_stream)

    try:
        user = run_as_user if run_as_user is not None else _default_run_as_user()
    except RunAsUserUnresolvedError as exc:
        _err(f"error: {exc}")
        return 1
    # D34: an explicitly empty account is as unsafe as an unresolved default.
    if not user.strip():
        _err("error: --run-as-user must not be empty; refusing an implicit SYSTEM account")
        return 1
    xml = build_task_xml(pythonw, user)
    if print_xml:
        _out(xml)
        return 0
    if not pythonw.is_file():
        _err(
            f"error: {pythonw} does not exist -- refusing to register a task whose "
            "action points at a missing launcher. Follow the source-checkout setup in "
            "README.md to recreate the venv before installing the capture task."
        )
        return 1
    if dry_run:
        _out("Would run (schtasks.exe, not executed -- --dry-run):")
        _out("  " + subprocess.list2cmdline(
            build_register_argv(Path("<temporary UTF-16 XML file>"), task_name)
        ))
        _out(xml)
        return 0

    # D31: close before schtasks opens the file (Windows sharing rules), and
    # always remove it, including failed registrations and raising runners.
    xml_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".xml", mode="w", encoding="utf-16", newline="", delete=False
        ) as handle:
            xml_path = Path(handle.name)
            handle.write(xml)
        result = runner(build_register_argv(xml_path, task_name))
    except OSError as exc:
        _err(f"error: could not register task from XML: {exc}")
        return 1
    finally:
        if xml_path is not None:
            xml_path.unlink(missing_ok=True)
    if result.returncode != 0:
        _err(f"error: schtasks /Create failed (exit {result.returncode}): {result.stderr.strip()}")
        return 1
    query_result = runner(build_query_argv(task_name))
    if query_result.returncode != 0:
        _err(
            "error: task registered, but could not be read back to verify it "
            f"(exit {query_result.returncode}): {query_result.stderr.strip()}"
        )
        return 1
    mismatch = verify_task_xml(query_result.stdout, pythonw)
    if mismatch is not None:
        _err(f"error: task {task_name!r} registered, but verification failed: {mismatch}.")
        return 1
    _out(
        f"Registered {task_name!r}: verified action `{Path(pythonw).name} "
        f"{TASK_ACTION_ARGS}`, every 10 minutes plus shutdown/sleep/resume (5s delay), "
        f"logon type {INTERACTIVE_LOGON_TYPE!r}; battery operation, catch-up, "
        "1-minute limit, IgnoreNew, no wake/idle requirement, enabled and visible."
    )
    return 0
