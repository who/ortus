"""Tests for install.sh (0zpx.1 acceptance criteria)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests._platform import skip_on_windows_bash_shim

pytestmark = skip_on_windows_bash_shim

INSTALL_SH = Path(__file__).parent.parent / "install.sh"


def test_install_sh_exists_and_is_executable() -> None:
    """Acceptance #1 (structural): install.sh exists, is executable, POSIX sh."""
    assert INSTALL_SH.is_file()
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR
    first_line = INSTALL_SH.read_text().splitlines()[0]
    assert first_line.startswith("#!/bin/sh"), \
        f"install.sh must use POSIX sh shebang, got: {first_line!r}"


def test_install_sh_help_exits_zero() -> None:
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "--help"], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0
    assert "Usage" in proc.stdout


def test_install_sh_errors_when_uv_missing() -> None:
    """Acceptance #3: with uv absent, exit 1 + docs URL + astral hint."""
    # Use a minimal PATH that excludes the directories where uv typically
    # lives (~/.cargo/bin, ~/.local/bin, /usr/local/bin, /opt/homebrew/bin).
    # /bin and /usr/bin still provide sh and the basic POSIX tools install.sh
    # needs. We deliberately do NOT shutil.copy() /bin/sh into a tmpdir to
    # simulate isolation: macOS SIP forbids copying protected system binaries
    # and the test fails with PermissionError before it can run.
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    # Verify uv is genuinely unreachable.
    probe = subprocess.run(
        ["sh", "-c", "command -v uv || echo NOPE"],
        env=env, capture_output=True, text=True
    )
    if "NOPE" not in probe.stdout:
        pytest.skip(f"could not strip uv from PATH; got: {probe.stdout!r}")

    proc = subprocess.run(
        ["sh", str(INSTALL_SH)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 1
    assert "uv is required" in proc.stderr
    assert "docs.astral.sh" in proc.stderr
    assert "astral.sh/uv/install.sh" in proc.stderr


def test_install_sh_rejects_unknown_arg() -> None:
    proc = subprocess.run(
        ["sh", str(INSTALL_SH), "--no-such-flag"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 1
    assert "unknown argument" in proc.stderr


def test_install_sh_does_not_auto_install_uv() -> None:
    """Acceptance #4: installer must NOT invoke uv-install logic itself."""
    text = INSTALL_SH.read_text()
    # No piping astral install through sh inside the installer.
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in text, \
        "the hint string is present (as guidance) ..."
    # ... but it must appear only in the err() guidance, not as an actual exec.
    # Heuristic: the line that pipes to sh shouldn't appear bare on a line.
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("curl ") and stripped.endswith("| sh"):
            raise AssertionError(
                f"install.sh:{i} appears to actually exec a uv-install pipeline; "
                f"installer must only print the hint, not run it"
            )
