"""A `python` for the worker PATH on hosts that only ship `python3`.

Workers reach for `python - <<EOF` to make edits. On a host with only
`python3` that fails with exit 127 and the worker spends turns retrying under
`uv run python`. Grind instead prepends a run-scoped directory holding one
`python` symlink, pointed at the repository's own virtualenv when there is one
(it carries the project's dependencies) and at `python3` otherwise. A host that
already resolves `python` is left alone.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _which(name: str, path: str) -> str | None:
    """Resolve ``name`` against ``path`` only, never the process PATH."""

    return shutil.which(name, path=path)


def python_target(repo: Path, path: str) -> Path | None:
    """What a shim `python` should point at, or None when no shim is needed.

    None when `python` already resolves on ``path``, on Windows, and when
    neither the repository's `.venv/bin/python` nor a `python3` on ``path``
    exists. A dangling or non-executable venv python falls back to `python3`.
    """

    if os.name == "nt" or _which("python", path) is not None:
        return None
    venv = repo / ".venv" / "bin" / "python"
    if venv.is_file() and os.access(venv, os.X_OK):
        return venv
    python3 = _which("python3", path)
    return None if python3 is None else Path(python3)


def python_shim_dir(repo: Path, path: str, shim_root: Path) -> Path | None:
    """Create the shim directory under ``shim_root`` and return it.

    Returns None, writing nothing, when ``python_target`` says no shim is
    needed. ``shim_root`` is the caller's run-scoped temp directory; nothing is
    written under ``repo``.
    """

    target = python_target(repo, path)
    if target is None:
        return None
    shim_dir = shim_root / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    link = shim_dir / "python"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return shim_dir


def with_python_shim(repo: Path, path: str, shim_root: Path) -> str:
    """``path`` with the shim directory prepended, or ``path`` unchanged."""

    shim_dir = python_shim_dir(repo, path, shim_root)
    if shim_dir is None:
        return path
    return f"{shim_dir}{os.pathsep}{path}" if path else str(shim_dir)
