"""Exercise the source launcher without desktop, provider, or scheduler I/O."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import venv


ROOT = Path(__file__).resolve().parents[1]


def test_bare_launcher_dispatches_restore_and_propagates_fake_picker_exit(tmp_path, monkeypatch):
    checkout = tmp_path / "source checkout"
    checkout.mkdir()
    shutil.copy2(ROOT / "termsync.cmd", checkout / "termsync.cmd")
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "from termsync import cli\n"
        "def fake_picker(store, *, newest, run):\n"
        "    assert newest is False and run is False\n"
        "    print('fake picker: armed restore')\n"
        "    return 23\n"
        "cli.restore_interactive = fake_picker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(hooks), str(ROOT / "src"))))
    result = subprocess.run(
        [str(checkout / "termsync.cmd")], cwd=tmp_path,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 23
    assert "fake picker: armed restore" in result.stdout


def test_source_launcher_help_from_another_directory(tmp_path):
    result = subprocess.run(
        [str(ROOT / "termsync.cmd"), "--help"], cwd=tmp_path,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0
    assert "snap" in result.stdout and "restore" in result.stdout and "install" in result.stdout
