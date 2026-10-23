# Developing and checking Termsync

Work from a source checkout on Windows. Runtime PowerShell helpers are resolved
relative to `src/termsync`, so keep `snapshot/` beside `src/`. The current wheel
configuration alone does not provide those helpers.

## Isolated tests

Create the venv using the README, then install the pinned development extra:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

The verified interpreter is Python 3.14.2 on Windows; there is no tested
cross-version or cross-platform matrix. Runtime dependencies are empty. Pytest
9.0.2 and Ruff 0.15.5 are the declared development tools.

Tests redirect subprocess homes into temporary directories and reject real
Windows Terminal launches. Any test reaching capture, restore, or install must
inject fake readers/launchers/scheduler runners and isolated stores. Never use
the live desktop or real scheduler to make a unit test pass. Windows sandbox
temporary-directory access failures are setup failures, not product assertions;
use a fresh permitted `--basetemp` in an appropriate execution context.

Static [fixtures](../tests/fixtures/README.md) are sanitized derivatives, not raw
measurements. All eight window replays and the selection oracle run without
`docs/research`, user-profile data, or a live fixture desk.

## Optional diagnostic harnesses

From the checkout, after installing the development extra:

```powershell
powershell.exe -NoProfile -File .\scripts\Check-Termsync.ps1 -Python "$PWD\.venv\Scripts\python.exe"
```

This reads the existing task and capture status, then runs isolated focused
tests. It changes no task and opens no provider tabs. Pass `-Python` as above to
use this checkout's development environment. Without it, the helper retains its
legacy Python 3.14 location under `%LOCALAPPDATA%\Python\pythoncore-3.14-64`.
Reports stay under `scratch/verification`. An old snapshot after sleep can produce a diagnostic
failure even when the offline tests pass.

The separate live harness is deliberately outside the ordinary test command:

```powershell
.\.venv\Scripts\python.exe .\scripts\Check-LiveRestore.py
```

Run it only when intending to open disposable windows and send four fixture
prompts through authenticated Claude/Codex sessions. It exercises capture,
picker input, armed Enter, exact identity checks, and ownership-checked cleanup.
It uses isolated stores under `scratch/live-restore` and writes local evidence
under `docs/research/live-restore`. Inspect a partial result and its cleanup
record before starting another run. Scheduled capture may record these fixtures.
The harness does not qualify installed command routing or arbitrary desk sizes.

This optional live check has narrower requirements than normal Termsync use.
It currently looks for `claude.exe` on PATH and the npm-installed Windows x64
Codex executable under `%APPDATA%\npm\node_modules\@openai\codex`. Its Codex
fixture requests `gpt-6.1-sol`, so the account must have access to that model.
The close-dialog checks expect English Windows Terminal labels. Other Codex
install layouts and UI languages need separate qualification; the offline test
suite does not require those installations or a live account.

Offline tests exercise capture and restore against synthetic observations. They
do not establish native UI behavior or power-transition timing. A live result
also applies only to the tested desktop and fixture size; check other setups
separately.

## Source files and local evidence

A complete source checkout contains:

- `README.md`, `LICENSE`, `.gitignore`, `pyproject.toml`, and `termsync.cmd`;
- `src/termsync/*.py`, `snapshot/*.ps1`, and the source scripts in `scripts/`;
- `tests/*.py` and `tests/fixtures/**`;
- this document, `docs/USAGE.md`, and the interface examples in `docs/images/`.

The [picker images](images/provenance.md) come from the production renderer with
synthetic session data. They illustrate the interface without reading a real
desktop or provider profile.

Keep local inventories and generated reports out of source releases. The live
scripts write into `scratch/` and `docs/research/`, both ignored by Git. Historical
development notes and raw measurements are not distributed with this checkout.
Some source comments still refer to those records as provenance; no runtime or
test loader needs them. Keep the live-test scripts when excluding their output.

An ignore rule only affects untracked files. If local evidence has already been
committed to a development repository, check its tracked files and history before
sharing it. A fresh repository can start from the reviewed source files while
the development checkout retains its local evidence.

To check a standalone source copy, assemble only the files listed above in a
separate directory. Create its own venv, install `.[dev]`, and clear any
`PYTHONPATH` pointing into another checkout. Confirm that imports and PowerShell
helper paths resolve inside the copy, then run the full suite and Ruff there.
The launcher tests also exercise calls from another working directory. You do
not need to register a task or open provider sessions for this check.
