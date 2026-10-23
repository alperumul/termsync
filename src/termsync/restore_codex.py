"""Read-only Codex restore eligibility, separate from live lock ownership."""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from termsync.capture_codex import fallback_thread, is_cli_thread, read_header, read_threads


@dataclass(frozen=True)
class CodexRestoreInfo:
    path: Path | None = None
    archived: bool = False
    reason: str | None = None


def validate_resume_args(args: tuple[str, ...]) -> None:
    """Only the captured model, sandbox and approval pairs may cross the launcher."""
    if len(args) % 2:
        raise ValueError("invalid Codex resume flags")
    seen = set()
    for flag, value in zip(args[::2], args[1::2]):
        if flag not in ("-m", "-s", "-a") or flag in seen:
            raise ValueError("invalid Codex resume flags")
        if not value or value.startswith("-") or any(c in value for c in "\x00\r\n"):
            raise ValueError("invalid Codex resume flag value")
        seen.add(flag)


def lookup_codex(session_id: str, home: Path | None = None) -> CodexRestoreInfo:
    """Resolve current archive/path state by UUID, validating the rollout's identity.

    A saved path may have moved on archive or compression. Never use it as
    authority or search by name/cwd. Database failures fail closed; an absent
    database or row can use the bounded U5 rollout-header fallback.
    """
    try:
        if str(UUID(session_id)) != session_id.lower():
            raise ValueError("invalid Codex session id")
        home = home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        try:
            record = read_threads(home, [session_id], time.monotonic() + 2).get(session_id)
        except ValueError as exc:
            if str(exc) != "Codex state database missing":
                raise
            record = None
        if record is None:
            record = fallback_thread(home, session_id)
        if not record or not is_cli_thread(record):
            return CodexRestoreInfo(reason="Codex CLI session unavailable")
        archived = bool(record.get("archived", False))
        path = Path(record["rollout_path"]) if record.get("rollout_path") else None
        if path is None or not path.is_file():
            return CodexRestoreInfo(archived=archived, reason="Codex rollout missing")
        header = read_header(path)
        if (not isinstance(header, dict) or header.get("id") != session_id
                or not is_cli_thread(header)):
            return CodexRestoreInfo(archived=archived, reason="Codex rollout identity mismatch")
        return CodexRestoreInfo(path=path, archived=archived)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return CodexRestoreInfo(reason=f"Codex metadata unreadable ({type(exc).__name__})")
