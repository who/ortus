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
from ortus.core.judge import parse_judge_config
from ortus.core.local_backend import LOCAL_TABLE_BACKENDS, parse_local_table
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

# The looping-worker signal's mode and cadence when `.ortusrc` omits them.
# Spelled here rather than imported so loading a config does not pull the judge
# transport in; `tests/test_judge_progress.py` pins both against the module
# that owns the vocabulary.
DEFAULT_PROGRESS_MODE = "shadow"
DEFAULT_PROGRESS_INTERVAL = 300

# The bar `ortus grind` holds each issue to before the worker may session-close
# it. `full` is the issue's criterion-check commands; `prototype` is the
# project's linter plus a syntax/compile gate and nothing behavioural, so an
# issue closes on a clean lint pass. Full is the default, and a `.ortusrc`
# without the key resolves to it, so no existing project changes bar.
VERIFICATION_FULL = "full"
VERIFICATION_PROTOTYPE = "prototype"
VERIFICATION_MODES: tuple[str, ...] = (VERIFICATION_FULL, VERIFICATION_PROTOTYPE)
DEFAULT_VERIFICATION_MODE = VERIFICATION_FULL

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
    # What a grind worker must prove before it session-closes an issue: the
    # issue's criterion checks (`full`) or only the project's lint and
    # syntax gate (`prototype`). `ortus grind --prototype` overrides it for
    # one run.
    "verification": DEFAULT_VERIFICATION_MODE,
    # Which variant of the two worker-facing prompt texts a run serves: the
    # legacy bundles (off) or the audited rewrite. On by default since the
    # hello-world comparison adopted the audited arm: it held the control's
    # close rate of 1.0 and finished in 684 seconds against 1030, which the
    # operator took over the 2.0027 against 1.6794 it spent per closed bead.
    # Both variants still resolve, and `ORTUS_PROMPT_AUDIT=0` restores the
    # legacy text for a single run without editing a tracked file.
    "prompt_audit": True,
    # Whether a worker prompt puts its per-bead segments behind every segment
    # that is identical across the beads of a run. On by default since the
    # hello-world comparison adopted the reordered arm: it held the control's
    # close rate of 1.0 while spending 1.5581 against 1.6794 per closed bead,
    # with the cache hit rate flat at 0.9565 against 0.9574. Both orderings
    # still compose, and `ORTUS_STABLE_PREFIX=0` restores the legacy one for a
    # single run without editing a tracked file.
    "stable_prompt_prefix": True,
    # Whether the judge's probability vectors pick the implementation model and
    # reasoning effort for the claimed bead, instead of every worker inheriting
    # the pinned `[profiles.<backend>.implement]` values. On by default since
    # the hello-world comparison adopted the routed arm: it held the control's
    # close rate while spending 1.1181 against 1.6794 per closed bead. Turning
    # it on costs an unjudged run nothing, because a bead whose vectors never
    # arrived still resolves the pinned profile, and `ORTUS_JEV_ROUTER=0`
    # forces it off for one run without editing a tracked file.
    "jev_model_router": True,
    # How much the looping-worker signal may do: `off` is today's reaper,
    # `shadow` records the decision it would have taken, `enforce` lets it end
    # a looping worker before the watchdog does. Shadow by default so the
    # signal is measured before it acts, and `ORTUS_JEV_PROGRESS` flips one run
    # without editing a tracked file. The checks also need
    # `judge.include_log_tail`, which is what sends a worker's log text.
    "jev_progress_reaper": DEFAULT_PROGRESS_MODE,
    # Seconds between those checks. Long enough that a slow-but-working step is
    # not read as a loop, short enough to save most of a worker timeout.
    "jev_progress_interval_s": DEFAULT_PROGRESS_INTERVAL,
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


def _validate_verification(values: dict[str, Any]) -> None:
    """Reject a verification mode that names neither bar.

    The mode decides what a worker may skip before closing an issue, so a
    typo must fail here with the accepted values named rather than resolve
    to either bar by accident.
    """
    mode = values.get("verification", DEFAULT_VERIFICATION_MODE)
    if mode not in VERIFICATION_MODES:
        raise ProfileError(
            f"invalid verification mode {mode!r}; expected "
            + " or ".join(VERIFICATION_MODES)
        )


def _validate_prompt_audit(values: dict[str, Any]) -> None:
    """Reject a `prompt_audit` value that is not a boolean.

    The key selects which prompt text every worker in the run is served, so a
    string that merely looks true (`prompt_audit = "yes"`) fails here instead
    of resolving to the legacy arm and quietly spoiling an A/B.
    """
    value = values.get("prompt_audit", False)
    if not isinstance(value, bool):
        raise ProfileError(
            f"invalid prompt_audit {value!r}; expected true or false"
        )


def _validate_stable_prompt_prefix(values: dict[str, Any]) -> None:
    """Reject a `stable_prompt_prefix` value that is not a boolean.

    The key decides which prompt ordering every worker in the run is served,
    and the A/B it exists for compares cache hit rates between the two
    orderings. A string that merely looks true would resolve to the control
    arm and spoil the comparison silently, so it fails here instead.
    """
    value = values.get("stable_prompt_prefix", False)
    if not isinstance(value, bool):
        raise ProfileError(
            f"invalid stable_prompt_prefix {value!r}; expected true or false"
        )


def _validate_jev_model_router(values: dict[str, Any]) -> None:
    """Reject a `jev_model_router` value that is not a boolean.

    The key decides whether a bead's implementation model and effort come from
    the judge's vectors or from the pinned profile, and the A/B it exists for
    compares cost per closed bead between those two arms. A string that merely
    looks true would resolve to the pinned arm and spoil the comparison
    silently, so it fails here instead.
    """
    value = values.get("jev_model_router", False)
    if not isinstance(value, bool):
        raise ProfileError(
            f"invalid jev_model_router {value!r}; expected true or false"
        )


def _validate_jev_progress(values: dict[str, Any]) -> None:
    """Reject a progress-reaper mode or interval the run could not honor.

    The mode decides whether a probability may end a live worker, so a value
    that merely looks like a mode fails here rather than resolving to shadow
    and leaving an operator who asked for enforcement without it. The interval
    is a cadence in seconds: zero or negative would ask Jev about every poll.
    """
    from ortus.core.judge_progress import ProgressMode

    mode = values.get("jev_progress_reaper", DEFAULT_PROGRESS_MODE)
    try:
        ProgressMode(mode)
    except (ValueError, TypeError):
        raise ProfileError(
            f"invalid jev_progress_reaper {mode!r}; expected "
            + ", ".join(item.value for item in ProgressMode)
        ) from None
    interval = values.get("jev_progress_interval_s", DEFAULT_PROGRESS_INTERVAL)
    if type(interval) not in (int, float) or interval <= 0:
        raise ProfileError(
            f"invalid jev_progress_interval_s {interval!r}; expected seconds > 0"
        )


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
    """Reject a malformed `[local]` table, or a missing one under a backend that needs it.

    Both `local` and `opencode` pin the served model there. A config without
    the table is otherwise left alone: the table is opt-in, and every existing
    `.ortusrc` must load exactly as it did before the table existed.
    """
    table = values.get("local")
    if table is None and values.get("backend") not in LOCAL_TABLE_BACKENDS:
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
    _validate_verification(cfg.values)
    _validate_prompt_audit(cfg.values)
    _validate_stable_prompt_prefix(cfg.values)
    _validate_jev_model_router(cfg.values)
    _validate_jev_progress(cfg.values)
    _validate_profiles(cfg.values)
    _validate_local(cfg.values)
    parse_judge_config(cfg, environ={})
    return cfg
