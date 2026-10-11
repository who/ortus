"""Cache-relocation helpers (ported from ortus/lib/cache.sh).

The OS sandbox profile mounts ~/.cache read-only, blocking package-manager
writes. apply_cache_dirs() points XDG and per-tool cache dirs into a
project-local .cache/ inside the sandbox-writable filesystem.

A stealth repository keeps the caches out of the work tree instead, under
`${TMPDIR:-/tmp}/ortus-cache/<repo-id>`, so no linter or formatter that scans
the fork's tree ever walks a package cache.
"""

from __future__ import annotations

import hashlib
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


def cache_root(repo: Path, stealth: bool = False) -> Path:
    if not stealth:
        return repo / ".cache"
    repo_id = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(os.environ.get("TMPDIR") or "/tmp") / "ortus-cache" / repo_id


def ensure_cache_dirs(repo: Path, stealth: bool = False) -> Path:
    root = cache_root(repo, stealth)
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def env_overrides(repo: Path, stealth: bool = False) -> dict[str, str]:
    """Return the cache env vars without mutating os.environ."""
    root = cache_root(repo, stealth)
    overrides: dict[str, str] = {}
    for var, sub in ENV_VAR_MAP.items():
        overrides[var] = str(root if not sub else root / sub)
    return overrides


def apply_to_env(repo: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a new env dict with overrides applied; does not mutate caller env."""
    base = dict(env if env is not None else os.environ)
    base.update(env_overrides(repo))
    return base
