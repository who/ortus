"""Cache-relocation helpers (ported from ortus/lib/cache.sh).

The OS sandbox profile mounts ~/.cache read-only, blocking package-manager
writes. apply_cache_dirs() points XDG and per-tool cache dirs into a
project-local .cache/ inside the sandbox-writable filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path

SUBDIRS = ("uv", "pip", "npm", "cargo", "go-mod", "go-build")

ENV_VAR_MAP = {
    "XDG_CACHE_HOME": "",         # repo/.cache root
    "UV_CACHE_DIR": "uv",
    "PIP_CACHE_DIR": "pip",
    "npm_config_cache": "npm",
    "CARGO_HOME": "cargo",
    "GOMODCACHE": "go-mod",
    "GOCACHE": "go-build",
}


def cache_root(repo: Path) -> Path:
    return repo / ".cache"


def ensure_cache_dirs(repo: Path) -> Path:
    root = cache_root(repo)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def env_overrides(repo: Path) -> dict[str, str]:
    """Return the cache env vars without mutating os.environ."""
    root = cache_root(repo)
    overrides: dict[str, str] = {}
    for var, sub in ENV_VAR_MAP.items():
        overrides[var] = str(root if not sub else root / sub)
    return overrides


def apply_to_env(repo: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a new env dict with overrides applied; does not mutate caller env."""
    base = dict(env if env is not None else os.environ)
    base.update(env_overrides(repo))
    return base
