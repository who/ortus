"""Layered .ortusrc resolution (FR-026).

Precedence (later wins on per-key basis):
  1. Built-in defaults (DEFAULTS)
  2. User config:    ~/.ortusrc
  3. Project config: <repo>/.ortusrc

Nested tables are recursively merged, so a project can override one profile
field without discarding the rest of its user-level profile. Missing layers
are silently skipped.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ortus.core.profiles import (
    AgentProfile,
    Phase,
    ProfileError,
    SUPPORTED_EFFORTS,
    validate_profile_values,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib


DEFAULTS: dict[str, Any] = {
    "owner": None,
    "prefix": None,
    "condition": None,
    "backend": "claude",
    "codegraph": "auto",
    "codegraph_refresh_blocking": False,
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

    def resolve_profile(
        self,
        backend: str,
        phase: Phase,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentProfile:
        """Resolve CLI field overrides over project, user, then provider defaults."""
        table = self.values.get("profiles", {}).get(backend, {}).get(phase.value, {})
        return validate_profile_values(
            backend,
            phase,
            model=model if model is not None else table.get("model"),
            reasoning_effort=(
                reasoning_effort
                if reasoning_effort is not None
                else table.get("reasoning_effort")
            ),
        )


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Recursively merge TOML tables while replacing scalar leaves."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _validate_profiles(values: dict[str, Any]) -> None:
    profiles = values.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ProfileError("invalid profiles configuration: expected a TOML table")
    for backend, phases in profiles.items():
        if backend not in SUPPORTED_EFFORTS:
            raise ProfileError(
                f"invalid profile backend {backend!r}; expected claude or codex"
            )
        if not isinstance(phases, dict):
            raise ProfileError(f"invalid profiles.{backend}: expected a TOML table")
        for phase_name, table in phases.items():
            try:
                phase = Phase(phase_name)
            except ValueError as exc:
                raise ProfileError(
                    f"invalid phase profiles.{backend}.{phase_name}; expected plan, "
                    "implement, or verify"
                ) from exc
            if not isinstance(table, dict):
                raise ProfileError(
                    f"invalid profiles.{backend}.{phase_name}: expected a TOML table"
                )
            unknown = set(table) - {"model", "reasoning_effort"}
            if unknown:
                raise ProfileError(
                    f"invalid profiles.{backend}.{phase_name} field(s): "
                    f"{', '.join(sorted(unknown))}; expected model or reasoning_effort"
                )
            validate_profile_values(
                backend,
                phase,
                model=table.get("model"),
                reasoning_effort=table.get("reasoning_effort"),
            )


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
        _merge(cfg.values, data)
        cfg.layers.append(LoadedLayer("user", user_path, data))

    if repo is not None:
        project_path = repo / ".ortusrc"
        if project_path.is_file():
            data = _load_toml(project_path)
            _merge(cfg.values, data)
            cfg.layers.append(LoadedLayer("project", project_path, data))

    _validate_profiles(cfg.values)
    return cfg
