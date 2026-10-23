"""The one place a `print` that might fail on the ACTIVE CONSOLE'S ENCODING lives.

Promoted out of `cli.py` (S6 review, D22/coordinator finding C2): `restore.py`
needed exactly the same guard `cli._safe_print` already provided -- a spawn
that succeeded and opened real tabs must never have its own output
destroyed by a `UnicodeEncodeError` from an AI-generated title or this
project's own `§` caveat strings. `cli.py` imports `restore` (it calls
`restore.restore_selected` at the seam `restore_interactive` uses), so
`restore.py` importing `cli` back would be circular. Per this project's own
dedup rule ("extract at the second caller, never the first" -- the same
move that pulled `picker.prompt_and_age_text` out of `cli.py` once
`picker.py` became a second caller of it), `restore.py` becoming a second
caller of this exact function is what promotes it into a shared module,
rather than `restore.py` growing a second, drifting copy.

**Scope, stated honestly.** `cli.main()` reconfigures `sys.stdout`/`sys.stderr`
to UTF-8 (`_use_utf8_streams`) before any command runs, so on the ordinary
production path this function's `except UnicodeEncodeError` branch is
already closed off by that reconfigure -- it exists for a stream that
CANNOT be reconfigured at all (a pytest capture buffer, a pipe, a
narrow-codepage console that refused the reconfigure) or for a caller that
never went through `main()` in the first place. `restore.py` is the one
place in this codebase where such a raise costs something real: real tabs
are already open by the time this module prints anything past its
pre-flight lines, and the session ids it names are the single most
valuable thing on the screen (§2's own origin story: recovery worked last
time only because the ids had been printed earlier into a transcript).
§6 rule 3 mandates ASCII-safe (or encoding-safe) machine output for the
tool's own writes; this function is that guarantee's one implementation.
"""

from __future__ import annotations

import sys
from typing import IO


def safe_print(text: str, *, file: IO[str] | None = None) -> None:
    """Print without letting the active stream's encoding decide a run's outcome.

    Only `UnicodeEncodeError` is caught and retried, with lossy substitution
    at the SAME stream's own encoding (or `utf-8` if the stream reports
    none). A different failure -- a broken pipe, for instance -- still
    propagates; recovering from that is the caller's concern, not this
    function's.
    """
    stream = file if file is not None else sys.stdout
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_text, file=stream)
