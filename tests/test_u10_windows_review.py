"""Independent U10 regressions. Pure capture inputs; no live desktop/store."""
import pytest

from termsync.capture import build_capture
from termsync.capture_windows import parse_desktop
from termsync.emit import Emit


@pytest.mark.parametrize("reverse", [False, True])
def test_repeated_live_session_id_never_inherits_one_process_placement(reverse):
    def entry(sid, title, pid, started):
        return {"sessionId": sid, "pid": pid, "cwd": "D:/fixture", "kind": "interactive",
                "alive": True, "procStart": str(pid), "procStartLive": str(pid),
                "titleRaw": title, "titleAttachOk": True, "startedAt": started}

    entries = [entry("same-id", "First copy", 101, 1),
               entry("same-id", "Second copy", 102, 2),
               entry("independent", "Independent", 103, 3)]
    if reverse:
        entries.reverse()
    windows = [{"hwnd": h, "state": "ok", "tabs": [{"index": 0, "name": title}]}
               for h, title in [(10, "First copy"), (20, "Second copy"), (30, "Independent")]]
    observation = parse_desktop({"before": [10, 20, 30], "after": [10, 20, 30],
                                 "windows": windows})
    result = build_capture(Emit(tuple(entries), 3, 1, desktop=observation), None)
    ambiguous = [s for s in result.sessions if s.session_id == "same-id"]
    assert ambiguous, "Ambiguous sessions must remain recoverable"
    assert all(s.window_ordinal is None and s.tab_index is None for s in ambiguous)
    independent = next(s for s in result.sessions if s.session_id == "independent")
    assert (independent.window_ordinal, independent.tab_index) == (1, 0)
    assert result.window_count == 1
