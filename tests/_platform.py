"""Shared pytest skip helpers for platform-specific tests."""

from __future__ import annotations

import platform
import shutil
import subprocess
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


def _bwrap_can_build_a_namespace() -> bool:
    """Whether bwrap can actually start here, not merely whether it exists.

    GitHub runners, hardened kernels and some containers deny unprivileged user
    namespaces, so bwrap is on PATH and fails with `setting up uid map:
    Permission denied`. Tests that assert on a live sandbox posture have to
    skip there rather than fail (ortus-dyio).
    """

    if shutil.which("bwrap") is None:
        return False
    try:
        probe = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--", "true"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


BWRAP_USABLE = _bwrap_can_build_a_namespace()

skip_unless_bwrap_usable = pytest.mark.skipif(
    not BWRAP_USABLE,
    reason=(
        "bwrap cannot build a namespace here (no unprivileged user namespaces); "
        "the live read-only posture cannot be exercised"
    ),
)
