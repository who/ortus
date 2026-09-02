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

from ortus.core.grind_loop import DEFAULT_INTEGRATION_BRANCH
from ortus.core.local_backend import parse_local_table
from ortus.core.profiles import (
    BACKEND_NAMES_PROSE,
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


# CodeGraph is a prerequisite of an Ortus project, not an enhancement: a repo
# that omits the key inherits `required` and fails at the probe with actionable
# remediation rather than silently running every verb without an index. `off`
# stays the escape hatch for a repository CodeGraph cannot index.
DEFAULT_CODEGRAPH_MODE = "required"

# Seconds. Used when `.ortusrc` omits `merge_gate_timeout`.
DEFAULT_MERGE_GATE_TIMEOUT = 1800

# `ortus init --backend all` provisions every backend but always pins one
# concrete run backend; `all` must therefore never survive into a resolved
# run configuration, whatever layer tries to smuggle it in.
INIT_ONLY_BACKEND_MESSAGE = (
    f"backend must be {BACKEND_NAMES_PROSE}; "
    "'all' is an init provisioning option, not a run backend"
)

DEFAULTS: dict[str, Any] = {
    "owner": None,
    "prefix": None,
    "condition": None,
    "backend": "claude",
    "codegraph": DEFAULT_CODEGRAPH_MODE,
    "codegraph_refresh_blocking": False,
    # The agent reviewer is a policy step, not architecture: verification is
    # the machine pipeline, and this flag adds a read-only agent review after
    # a green machine run. Off by default per the escape-rate reversal
    # threshold in prd/PRD-lean-pipeline.md — one config line turns it back on.
    "reviewer": False,
    # Wait for the issue branch's forge checks before fast-forwarding the
    # integration branch. Off by default: the wait is minutes per landing and
    # the operator opts in. A timeout is a blocker, never a pass.
    "merge_gate": False,
    # Seconds to wait for those checks. DEFAULT_MERGE_GATE_TIMEOUT covers a
    # typical hermetic matrix; the workflow itself has no shorter job
    # timeout to inherit.
    "merge_gate_timeout": DEFAULT_MERGE_GATE_TIMEOUT,
    # Branch `grind` pins the working tree to and re-asserts each iteration.
    # "main" fits a fresh `ortus init`; a repo whose default branch is named
    # something else (e.g. "master") pins it here instead of passing
    # --integration-branch on every invocation.
    "integration_branch": DEFAULT_INTEGRATION_BRANCH,
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


# The init facts `ortus init` records into a project `.ortusrc` and must
# preserve on a forced re-init.
RECORDED_INIT_KEYS: tuple[str, ...] = ("prefix", "project_type", "backend", "codegraph")


def read_recorded_facts(repo: Path) -> dict[str, Any]:
    """Raw recorded init facts from the project `.ortusrc`, without layering.

    Deliberately not `load_config`: defaults and `~/.ortusrc` are preferences,
    not project facts, and must never be re-recorded into the project file as
    if the repo had pinned them. Missing file means an empty mapping; malformed
    TOML propagates for the caller to translate into an operator-facing error.
    """
    path = repo / ".ortusrc"
    if not path.is_file():
        return {}
    data = _load_toml(path)
    return {key: data[key] for key in RECORDED_INIT_KEYS if key in data}


def read_recorded_local(repo: Path) -> dict[str, Any]:
    """Raw `[local]` table from the project `.ortusrc`, without layering.

    Same rationale as `read_recorded_facts`: the served model is a project
    fact a forced re-init must preserve, never something to re-record from
    `~/.ortusrc`. A missing file, a missing table, or a `local` key that is
    not a table all mean an empty mapping; `load_config` is where the last
    of those becomes an error.
    """
    path = repo / ".ortusrc"
    if not path.is_file():
        return {}
    table = _load_toml(path).get("local")
    return dict(table) if isinstance(table, dict) else {}


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Recursively merge TOML tables while replacing scalar leaves."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _validate_backend(values: dict[str, Any]) -> None:
    if values.get("backend") == "all":
        raise ProfileError(INIT_ONLY_BACKEND_MESSAGE)


def _validate_profiles(values: dict[str, Any]) -> None:
    profiles = values.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ProfileError("invalid profiles configuration: expected a TOML table")
    for backend, phases in profiles.items():
        if backend not in SUPPORTED_EFFORTS:
            raise ProfileError(
                f"invalid profile backend {backend!r}; expected {BACKEND_NAMES_PROSE}"
            )
        if not isinstance(phases, dict):
            raise ProfileError(f"invalid profiles.{backend}: expected a TOML table")
        for phase_name, table in phases.items():
            try:
                phase = Phase(phase_name)
            except ValueError as exc:
                raise ProfileError(
                    f"invalid phase profiles.{backend}.{phase_name}; expected "
                    + ", ".join(member.value for member in Phase)
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


def _validate_local(values: dict[str, Any]) -> None:
    """Reject a malformed `[local]` table, or a missing one under `backend = "local"`.

    A config without the table is left alone: `local` is opt-in, and every
    existing `.ortusrc` must load exactly as it did before the table existed.
    """
    table = values.get("local")
    if table is None and values.get("backend") != "local":
        return
    parse_local_table(table)


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

    _validate_backend(cfg.values)
    _validate_profiles(cfg.values)
    _validate_local(cfg.values)
    return cfg
