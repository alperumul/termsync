"""Bounded, read-only desktop observation and conservative Claude placement.

HWNDs exist only inside one observation. They are never persisted as identity.
Sequential UIA reads are not atomic; an incomplete scan cannot prove global
title uniqueness, even in a window that was read successfully.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess

from termsync.titles import normalize_title

SCRIPT = Path(__file__).resolve().parents[2] / "snapshot" / "Read-Windows.ps1"
DESKTOP_TIMEOUT_S = 12.0


@dataclass(frozen=True)
class DesktopObservation:
    windows: tuple[dict, ...] = ()
    windows_seen: int | None = None
    error: str | None = None


def parse_desktop(payload: dict) -> DesktopObservation:
    """Validate a full scan, retaining failure details and an honest count.

    A vanished window contributes neither tabs nor an error. A newly appearing
    window, missing scan, invalid tab array, or UIA error invalidates the join.
    ``windows_seen`` counts the final physical enumeration, including plain
    windows. It is unknown if the envelope itself cannot be trusted.
    """
    def handles(value):
        if (not isinstance(value, list)
                or any(type(h) is not int or h <= 0 for h in value)
                or len(set(value)) != len(value)):
            raise ValueError("Invalid desktop window enumeration")
        return set(value)

    if not isinstance(payload, dict):
        raise ValueError("Desktop envelope must be an object")
    before, after = handles(payload.get("before")), handles(payload.get("after"))
    scans = payload.get("windows")
    if not isinstance(scans, list) or any(not isinstance(s, dict) for s in scans):
        raise ValueError("Desktop scans must be an array of objects")
    scanned = [s.get("hwnd") for s in scans]
    if (any(type(h) is not int for h in scanned)
            or len(set(scanned)) != len(scanned) or set(scanned) != before):
        raise ValueError("Desktop scans do not match enumerated windows")
    errors = []
    if after - before:
        errors.append("Desktop window set changed: new windows appeared during scan")
    usable = []
    for scan in scans:
        hwnd = scan["hwnd"]
        if hwnd not in after:
            continue  # vanished, including after a successful tab read
        state = scan.get("state")
        if state != "ok" or scan.get("error"):
            errors.append(f"Window {hwnd}: {state}: {scan.get('error') or 'unreadable'}")
            continue
        tabs = scan.get("tabs")
        if (not isinstance(tabs, list) or not tabs
                or any(not isinstance(t, dict) or type(t.get("index")) is not int
                       or t["index"] != i or not isinstance(t.get("name"), str)
                       for i, t in enumerate(tabs))):
            errors.append(f"Window {hwnd}: empty or invalid tab array")
            continue
        usable.append(scan)
    return DesktopObservation(tuple(usable), len(after), "; ".join(errors) or None)


def run_desktop(timeout_s: float = DESKTOP_TIMEOUT_S) -> DesktopObservation:
    """One killable helper; its total budget does not grow with window count.

    The helper starts no children, so subprocess.run's timeout kills the entire
    UIA read, including a hung provider call. Provider session reads are separate.
    """
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT)],
        capture_output=True, timeout=timeout_s, check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Desktop reader: " + result.stderr.decode("utf-8", "replace")[:2000])
    return parse_desktop(json.loads(result.stdout.decode("utf-8-sig")))


def placements(entries: tuple[dict, ...], desktop: DesktopObservation | None,
               claude_error: str | None) -> tuple[dict[str, tuple[int, int]], int | None]:
    """Match unique live console titles; rank only attributable windows.

    A window's earliest valid epoch-ms start sorts first. Missing/invalid times
    sort after known times; ties use the sorted session UUIDs in that window.
    Neither enumeration order, tab order, nor HWND breaks a timestamp tie.
    """
    if desktop is None or desktop.error or claude_error or desktop.windows_seen is None:
        return {}, None
    titles = [normalize_title(e.get("titleRaw") or "") for e in entries]
    if any(e.get("titleAttachOk") is not True or not title.strip()
           for e, title in zip(entries, titles)):
        # An unreadable live title could be a duplicate of any known title.
        return {}, None
    counts = Counter(titles)
    session_counts = Counter(entry["sessionId"] for entry in entries)
    tabs = defaultdict(list)
    for window in desktop.windows:
        for tab in window["tabs"]:
            tabs[normalize_title(tab["name"])].append((window["hwnd"], tab["index"]))
    matched = {}
    members = defaultdict(list)
    for entry, title in zip(entries, titles):
        if (entry.get("titleAttachOk") is not True or not title.strip()
                or counts[title] != 1 or len(tabs[title]) != 1
                or session_counts[entry["sessionId"]] != 1):
            # A resumed ID may occur in multiple live registry processes.
            # The session-keyed model cannot assign it one observed location.
            continue
        hwnd, index = tabs[title][0]
        matched[entry["sessionId"]] = (hwnd, index)
        members[hwnd].append(entry)

    def rank(hwnd):
        starts = [e.get("startedAt") for e in members[hwnd]
                  if type(e.get("startedAt")) is int and e["startedAt"] > 0]
        return (not starts, min(starts) if starts else 0,
                tuple(sorted(e["sessionId"] for e in members[hwnd])))

    ordinals = {hwnd: i for i, hwnd in enumerate(sorted(members, key=rank), 1)}
    return {sid: (ordinals[h], i) for sid, (h, i) in matched.items()}, len(ordinals)
