"""Shared pytest skip helpers for platform-specific tests."""

from __future__ import annotations

import platform
import shutil

import pytest

IS_WINDOWS = platform.system() == "Windows"

skip_on_windows_bash_shim = pytest.mark.skipif(
    IS_WINDOWS,
    reason="test uses POSIX sh / bash shim; not supported on Windows runners",
)

skip_unless_bd = pytest.mark.skipif(
    shutil.which("bd") is None,
    reason="bd binary not on PATH",
)
