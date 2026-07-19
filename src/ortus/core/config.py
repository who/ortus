"""Layered .ortusrc resolution (FR-026).

Precedence (later wins on per-key basis):
  1. Built-in defaults (DEFAULTS)
  2. User config:    ~/.ortusrc
  3. Project config: <repo>/.ortusrc

All files are TOML. Missing layers are silently skipped.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib


DEFAULTS: dict[str, Any] = {
    "owner": None,
    "prefix": None,
    "condition": None,
    "backend": "claude",
}


@dataclass(frozen=True)
class LoadedLayer:
    """A single config layer that contributed to the final Config."""

    source: str  # "defaults" | "user" | "project"
    path: Path | None
    data: dict[str, Any]


@dataclass
class Config:
    """Resolved configuration. Iterate `.layers` for provenance."""

    values: dict[str, Any] = field(default_factory=dict)
    layers: list[LoadedLayer] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(
    *,
    repo: Path | None = None,
    home: Path | None = None,
) -> Config:
    """Load layered config. Project overrides user overrides defaults."""
    if home is None:
        home = Path.home()
    cfg = Config()
    cfg.values.update(DEFAULTS)
    cfg.layers.append(LoadedLayer("defaults", None, dict(DEFAULTS)))

    user_path = home / ".ortusrc"
    if user_path.is_file():
        data = _load_toml(user_path)
        cfg.values.update(data)
        cfg.layers.append(LoadedLayer("user", user_path, data))

    if repo is not None:
        project_path = repo / ".ortusrc"
        if project_path.is_file():
            data = _load_toml(project_path)
            cfg.values.update(data)
            cfg.layers.append(LoadedLayer("project", project_path, data))

    return cfg
