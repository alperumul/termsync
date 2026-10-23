"""Tests for `termsync.emit` (S3, §5 steps 1-4, §13.4).

Glyphs are written as ``\\uXXXX`` escapes throughout, never as literal
characters, and nothing here calls ``print()`` on a glyph-bearing string --
this machine's Python console is cp1252 and raises ``UnicodeEncodeError`` on
these codepoints, which has nothing to do with the code under test.

`run_emit`'s subprocess handling is exercised by monkeypatching
`termsync.emit.subprocess.run` with a fake -- nothing here shells out to a
real PowerShell process.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from termsync import emit as emit_module
from termsync.emit import Emit, EmitError, parse_emit, run_emit

GLYPH_TITLE = "\u2733 termsync S3"


def _envelope(**overrides: Any) -> dict[str, Any]:
    base = {
        "sessions": [
            {
                "sessionId": "s1",
                "cwd": r"C:\fixture\tool",
                "alive": True,
                "procStart": "130000000000000003",
                "procStartLive": "130000000000000003",
                "kind": "interactive",
                "titleRaw": GLYPH_TITLE,
                "titleAttachOk": True,
                "name": "termsync-1",
                "version": "2.1.238",
            }
        ],
        "claudeProcessCount": 1,
        "bootTime": 1787235232500,
    }
    base.update(overrides)
    return base


# --- parse_emit --------------------------------------------------------


def test_parse_emit_decodes_a_glyph_prefixed_title() -> None:
    text = json.dumps(_envelope())
    result = parse_emit(text)
    assert result.sessions[0]["titleRaw"] == GLYPH_TITLE


def test_parse_emit_returns_the_envelope_fields() -> None:
    result = parse_emit(json.dumps(_envelope()))
    assert isinstance(result, Emit)
    assert result.claude_process_count == 1
    assert result.boot_time == 1787235232500
    assert len(result.sessions) == 1


def test_parse_emit_accepts_null_boot_time() -> None:
    """D1: the emitter guards the CIM read and emits null rather than dying."""
    result = parse_emit(json.dumps(_envelope(bootTime=None)))
    assert result.boot_time is None


def test_parse_emit_raises_on_malformed_json() -> None:
    with pytest.raises(EmitError):
        parse_emit("{not valid json")


def test_parse_emit_raises_when_a_required_top_level_key_is_missing() -> None:
    payload = _envelope()
    del payload["claudeProcessCount"]
    with pytest.raises(EmitError, match="claudeProcessCount"):
        parse_emit(json.dumps(payload))


def test_parse_emit_raises_when_sessions_key_is_missing() -> None:
    payload = _envelope()
    del payload["sessions"]
    with pytest.raises(EmitError, match="sessions"):
        parse_emit(json.dumps(payload))


def test_parse_emit_raises_when_boot_time_key_is_missing() -> None:
    payload = _envelope()
    del payload["bootTime"]
    with pytest.raises(EmitError, match="bootTime"):
        parse_emit(json.dumps(payload))


def test_parse_emit_raises_when_top_level_is_not_an_object() -> None:
    with pytest.raises(EmitError):
        parse_emit(json.dumps([1, 2, 3]))


def test_parse_emit_raises_when_sessions_is_not_a_list() -> None:
    with pytest.raises(EmitError):
        parse_emit(json.dumps(_envelope(sessions={"not": "a list"})))


def test_parse_emit_passes_session_entries_through_verbatim() -> None:
    """emit.py filters and interprets nothing about session content (capture.py's job)."""
    payload = _envelope()
    payload["sessions"][0]["kind"] = "bg"
    payload["sessions"][0]["alive"] = False
    result = parse_emit(json.dumps(payload))
    assert result.sessions[0]["kind"] == "bg"
    assert result.sessions[0]["alive"] is False


# --- F1: the per-session contract check ---------------------------------
#
# §8 names the failure this closes in so many words: "an internal change at
# Anthropic turns `desk` into a machine that silently overwrites good
# layouts with empty ones while reporting `outcome: "ok"` forever -- the
# worst reachable state in the design." Every §5 step 5-7 filter defaults
# to *drop*, so a field rename or disappearance upstream used to be
# indistinguishable from "every session closed" -- reproduced by the
# coordinator on a real capture (renaming `alive` -> `isAlive` on three live
# sessions gave `sessionsSeen: 0`, `outcome: "ok"`). These tests pin that
# every required key's absence is caught HERE, loudly, before it can reach
# that state.

_REQUIRED_SESSION_KEYS = (
    "sessionId",
    "alive",
    "procStart",
    "procStartLive",
    "kind",
    "titleRaw",
    "titleAttachOk",
    "cwd",
    "name",
)


@pytest.mark.parametrize("missing_key", _REQUIRED_SESSION_KEYS)
def test_parse_emit_raises_when_a_required_session_key_is_missing(missing_key: str) -> None:
    payload = _envelope()
    del payload["sessions"][0][missing_key]
    with pytest.raises(EmitError, match=missing_key):
        parse_emit(json.dumps(payload))


def test_parse_emit_accepts_a_null_valued_proc_start_live() -> None:
    """`procStartLive` is legitimately null when the emitter's H1 guard fires
    (`.StartTime` itself could not be read) -- the KEY must still be present,
    but its VALUE may be null. Only a missing key is a contract violation."""
    payload = _envelope()
    payload["sessions"][0]["procStartLive"] = None
    result = parse_emit(json.dumps(payload))
    assert result.sessions[0]["procStartLive"] is None


def test_parse_emit_accepts_a_null_valued_name() -> None:
    """Same principle as `procStartLive`: presence, not truthiness, is required."""
    payload = _envelope()
    payload["sessions"][0]["name"] = None
    result = parse_emit(json.dumps(payload))
    assert result.sessions[0]["name"] is None


def test_parse_emit_does_not_reject_an_unknown_extra_session_key() -> None:
    """The emitter is allowed to grow (§13.2's design note): an unrecognised
    extra field must never fail the contract check -- only a MISSING required
    key does."""
    payload = _envelope()
    payload["sessions"][0]["windowCount"] = 3
    result = parse_emit(json.dumps(payload))
    assert result.sessions[0]["windowCount"] == 3


def test_parse_emit_raises_when_a_session_entry_is_not_an_object() -> None:
    payload = _envelope()
    payload["sessions"][0] = "not-an-object"
    with pytest.raises(EmitError, match="entry 0"):
        parse_emit(json.dumps(payload))


# --- run_emit ------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_emit_raises_when_script_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.ps1"
    with pytest.raises(EmitError, match=r"does-not-exist\.ps1"):
        run_emit(script=missing)


def test_run_emit_decodes_utf8_explicitly_not_through_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UTF-8 decode this test guards: `text=True` decodes via the
    process locale, which is cp1252 on this machine and mangles/raises on
    the \\u2733-family glyphs. `run_emit` must decode the raw bytes as UTF-8
    itself, regardless of locale."""
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    stdout_bytes = json.dumps(_envelope()).encode("utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        assert "text" not in kwargs
        assert "encoding" not in kwargs
        return _FakeCompletedProcess(returncode=0, stdout=stdout_bytes)

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    result = run_emit(script=script)
    assert result.sessions[0]["titleRaw"] == GLYPH_TITLE


def test_run_emit_uses_powershell_exe_not_pwsh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["argv"] = argv
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(_envelope()).encode("utf-8"))

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    run_emit(script=script)
    assert captured["argv"][0] == "powershell.exe"
    assert "-Emit" in captured["argv"]
    assert str(script) in captured["argv"]


def test_run_emit_passes_create_no_window_so_the_child_pops_no_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D30: `termsync snap` now runs, via the scheduled task, under
    `pythonw.exe` -- a windowless parent with no console of its own.
    Spawning `powershell.exe` from a console-less parent makes Windows
    allocate that child its OWN new, visible console window (this is the
    exact mechanism the windowless-task fix would otherwise just relocate,
    from `cmd.exe` to this `powershell.exe` call, rather than remove).
    `creationflags=subprocess.CREATE_NO_WINDOW` suppresses that allocation.
    Pinned on the literal flag value, not just "some creationflags kwarg
    was passed" -- a mutation swapping in an unrelated flag (or `0`) would
    still pass a weaker assertion."""
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess(returncode=0, stdout=json.dumps(_envelope()).encode("utf-8"))

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    run_emit(script=script)
    assert captured["kwargs"]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_run_emit_raises_on_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=1, stdout=b"", stderr=b"boom")

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    with pytest.raises(EmitError, match="boom"):
        run_emit(script=script)


def test_run_emit_raises_on_empty_stdout_even_with_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1's exact signature: the emitter died and printed nothing."""
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=0, stdout=b"")

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    with pytest.raises(EmitError):
        run_emit(script=script)


def test_run_emit_raises_on_malformed_json_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        return _FakeCompletedProcess(returncode=0, stdout=b"{not json")

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    with pytest.raises(EmitError):
        run_emit(script=script)


def test_run_emit_accepts_null_boot_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        payload = json.dumps(_envelope(bootTime=None)).encode("utf-8")
        return _FakeCompletedProcess(returncode=0, stdout=payload)

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    result = run_emit(script=script)
    assert result.boot_time is None


def test_run_emit_raises_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "Snapshot-Desk.ps1"
    script.write_text("# fake", encoding="utf-8")

    def fake_run(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
        raise subprocess.TimeoutExpired(cmd="powershell.exe", timeout=30.0)

    monkeypatch.setattr(emit_module.subprocess, "run", fake_run)
    with pytest.raises(EmitError, match="timed out"):
        run_emit(script=script, timeout_s=30.0)
