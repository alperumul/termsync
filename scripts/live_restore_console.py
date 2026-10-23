"""Private, bounded console I/O for Check-LiveRestore.py; no foreground input."""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys

k = c.WinDLL("kernel32", use_last_error=True)
u = c.WinDLL("user32", use_last_error=True)
k.OpenProcess.restype = w.HANDLE
k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
k.CloseHandle.argtypes = [w.HANDLE]
k.CreateFileW.restype = w.HANDLE
k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
k.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
k.GetConsoleProcessList.argtypes = [c.POINTER(w.DWORD), w.DWORD]
k.ReadConsoleOutputCharacterW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD, c.POINTER(w.DWORD)]
u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]


def check(ok, message):
    if not ok:
        raise RuntimeError(f"{message} (Win32 {c.get_last_error()})")


def stamp(pid):
    h = k.OpenProcess(0x1000, False, pid)
    check(h, "OpenProcess")
    try:
        times = [w.FILETIME() for _ in range(4)]
        check(k.GetProcessTimes(h, *[c.byref(t) for t in times]), "GetProcessTimes")
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k.CloseHandle(h)


def processes():
    r = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
            "@(Get-CimInstance Win32_Process | Select-Object "
            "ProcessId,ParentProcessId,CommandLine,Name) | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        timeout=12,
        creationflags=0x08000000,
    )
    check(r.returncode == 0, "CIM process inventory")
    return json.loads(r.stdout.decode("utf-8-sig"))


def identity(pid, inventory=None):
    rows = [p for p in (inventory or processes()) if p["ProcessId"] == pid]
    check(len(rows) == 1 and rows[0]["CommandLine"], "Exact process command line")
    return dict(pid=pid, start=stamp(pid), commandLine=rows[0]["CommandLine"])


def window_stamp(hwnd):
    pid = w.DWORD()
    check(u.IsWindow(w.HWND(hwnd)), "IsWindow")
    check(u.GetWindowThreadProcessId(w.HWND(hwnd), c.byref(pid)), "Window process")
    return dict(pid=pid.value, start=stamp(pid.value))


class Coord(c.Structure):
    _fields_ = [("x", w.SHORT), ("y", w.SHORT)]


class Rect(c.Structure):
    _fields_ = [("left", w.SHORT), ("top", w.SHORT), ("right", w.SHORT), ("bottom", w.SHORT)]


class Info(c.Structure):
    _fields_ = [
        ("size", Coord),
        ("cursor", Coord),
        ("attr", w.WORD),
        ("window", Rect),
        ("maximum", Coord),
    ]


class Key(c.Structure):
    _fields_ = [
        ("down", w.BOOL),
        ("repeat", w.WORD),
        ("vk", w.WORD),
        ("scan", w.WORD),
        ("char", w.WCHAR),
        ("control", w.DWORD),
    ]


class Event(c.Union):
    _fields_ = [("key", Key), ("padding", c.c_byte * 16)]


class Input(c.Structure):
    _fields_ = [("type", w.WORD), ("event", Event)]


k.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, c.POINTER(Info)]
k.WriteConsoleInputW.argtypes = [w.HANDLE, c.POINTER(Input), w.DWORD, c.POINTER(w.DWORD)]


def console(request):
    target = request["identity"]
    check(identity(target["pid"]) == target, "Console ownership changed")
    # This helper has no shared console, and never changes focus or tab selection.
    k.FreeConsole()
    check(k.AttachConsole(target["pid"]), "AttachConsole")
    try:
        pids = (w.DWORD * 256)()
        count = k.GetConsoleProcessList(pids, 256)
        check(0 < count <= 256 and target["pid"] in pids[:count], "Console membership")
        check(stamp(target["pid"]) == target["start"], "Attached PID was reused")
        out = k.CreateFileW("CONOUT$", 0x80000000, 3, None, 3, 0, None)
        check(out and out != c.c_void_p(-1).value, "Open CONOUT$")
        try:
            info = Info()
            check(k.GetConsoleScreenBufferInfo(out, c.byref(info)), "Read screen dimensions")
            length = min(info.size.x * info.size.y, 200000)
            buf = c.create_unicode_buffer(length + 1)
            got = w.DWORD()
            check(k.ReadConsoleOutputCharacterW(out, buf, length, 0, c.byref(got)), "Read screen")
            raw = buf[: got.value]
            screen = "\n".join(
                raw[i : i + info.size.x].rstrip() for i in range(0, len(raw), info.size.x)
            )
        finally:
            k.CloseHandle(out)
        if request.get("keys") is not None:
            for text in request["require"]:
                check(text in screen, f"Console lacks required state {text!r}")
            check(identity(target["pid"]) == target, "Ownership changed before input")
            events = []
            for char in request["keys"]:
                vk = 13 if char == "\r" else (27 if char == "\x1b" else 0)
                for down in (True, False):
                    record = Input()
                    record.type = 1
                    record.event.key = Key(down, 1, vk, 0, char, 0)
                    events.append(record)
            inp = k.CreateFileW("CONIN$", 0x40000000, 3, None, 3, 0, None)
            check(inp and inp != c.c_void_p(-1).value, "Open CONIN$")
            try:
                array = (Input * len(events))(*events)
                written = w.DWORD()
                check(
                    k.WriteConsoleInputW(inp, array, len(array), c.byref(written))
                    and written.value == len(array),
                    "Write exact console input",
                )
            finally:
                k.CloseHandle(inp)
        return dict(
            screen=screen, consolePids=list(pids[:count]), identity=target, keys=request.get("keys")
        )
    finally:
        k.FreeConsole()


if __name__ == "__main__":
    try:
        result = console(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
        Path(sys.argv[2]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:
        Path(sys.argv[2]).write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        raise
