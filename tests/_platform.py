"""Shared pytest skip helpers for platform-specific tests."""

from __future__ import annotations

import platform
import shutil
from pathlib import Path

import pytest

IS_WINDOWS = platform.system() == "Windows"

# `Path.resolve()` rewrites a symlinked prefix before any comparison, so on a
# host where /tmp is a symlink (macOS points it at /private/tmp) no real path
# is ever `relative_to("/tmp")`. Code paths guarded by that check are therefore
# unreachable there — which is harmless in production, because the Linux-only
# branches that use it never execute on such a host.
TMP_IS_CANONICAL = Path("/tmp").resolve() == Path("/tmp")

skip_unless_tmp_is_canonical = pytest.mark.skipif(
    not TMP_IS_CANONICAL,
    reason=(
        "/tmp resolves elsewhere here (macOS symlinks it to /private/tmp), so "
        "the wrapper's relative_to('/tmp') branch cannot be reached with any "
        "real path; it is exercised on Linux runners instead"
    ),
)

skip_on_windows_bash_shim = pytest.mark.skipif(
    IS_WINDOWS,
    reason="test uses POSIX sh / bash shim; not supported on Windows runners",
)

skip_unless_bd = pytest.mark.skipif(
    shutil.which("bd") is None,
    reason="bd binary not on PATH",
)
