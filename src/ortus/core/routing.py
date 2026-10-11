"""Resolve and prepare the worker route a grind run executes on.

A route is the configured backend plus the three phase profiles it launches
with. Discovery checks that the baseline backend can run at all — its binary
is on PATH, its profiles validate — without installing, authenticating or
launching anything. Preparation repeats those preflights for one turn and
builds a fresh runner. Preparation failures require human handling; they are
not provider failures eligible for a second backend.

Every bead runs on the configured backend with its configured profiles:
there is no per-bead model choice here, and an operator pin from the command
line applies to the baseline exactly as `config.resolve_profile` reads it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, cast

from ortus.core.agent import Backend, BackendError, compose_worker_prompt, make_runner
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphUnavailable,
    phase_contract,
)
from ortus.core.config import Config
from ortus.core.hooks import HookConflictError, check_hooks_enabled
from ortus.core.local_backend import (
    LocalServerError, OpenCodeBinaryError, load_local_config,
    probe_models, resolve_opencode_binary,
)
from ortus.core.profiles import AgentProfile, Phase, ProfileError


class WorkerRoute(str, Enum):
    """A backend that can run a worker turn."""

    CLAUDE = "claude"
    CODEX = "codex"
    GROK = "grok"
    OPENCODE = "opencode"


WORKER_ROUTES = tuple(WorkerRoute)


def _backend_binary(name: str, *, path: str | None) -> str | None:
    """Resolve a worker CLI on ``path``; the seam tests patch instead of shutil."""
    return shutil.which(name, path=path)


class RoutePreparationError(BackendError):
    """Stop before claiming or launching; the selected route cannot run."""


@dataclass(frozen=True)
class RouteOverrides:
    requested_backend: str | None = None
    implement_model: str | None = None
    implement_reasoning_effort: str | None = None
    verify_model: str | None = None
    verify_reasoning_effort: str | None = None

    def pinned(self, environ: Mapping[str, str]) -> bool:
        return bool(self.requested_backend or environ.get("ORTUS_BACKEND")) or any(
            value is not None
            for value in (
                self.implement_model,
                self.implement_reasoning_effort,
                self.verify_model,
                self.verify_reasoning_effort,
            )
        )


@dataclass(frozen=True)
class RoutePlan:
    baseline: WorkerRoute
    available_workers: tuple[WorkerRoute, ...]
    overrides: RouteOverrides
    baseline_backend: str = ""

    def execution_backend(self, route: WorkerRoute) -> str:
        return self.baseline_backend if route == self.baseline and self.baseline_backend else route.value


def _profiles(
    config: Config,
    backend: str,
    overrides: RouteOverrides,
) -> tuple[AgentProfile, AgentProfile, AgentProfile]:
    """The implement, verify and finalize profiles one backend launches with."""
    return (
        config.resolve_profile(
            backend,
            Phase.IMPLEMENT,
            model=overrides.implement_model,
            reasoning_effort=overrides.implement_reasoning_effort,
        ),
        config.resolve_profile(
            backend,
            Phase.VERIFY,
            model=overrides.verify_model,
            reasoning_effort=overrides.verify_reasoning_effort,
        ),
        config.resolve_profile(backend, Phase.FINALIZE),
    )


def phase_profiles(
    config: Config, backend: str, overrides: RouteOverrides = RouteOverrides(),
) -> tuple[AgentProfile, AgentProfile, AgentProfile]:
    """Resolve a run's implement, verify and finalize profiles for ``backend``.

    Raises ``ProfileError`` as `config.resolve_profile` does, so a caller that
    reports a bad profile at startup keeps doing so.
    """
    return _profiles(config, backend, overrides)


def plan_routes(
    config: Config,
    baseline_backend: str,
    *,
    overrides: RouteOverrides = RouteOverrides(),
    environ: Mapping[str, str] | None = None,
) -> RoutePlan:
    """Check the baseline worker can run, without installing or launching it.

    The baseline is the only route: a missing binary or an invalid profile is
    a startup error rather than a reason to try another backend.
    """
    canonical = "opencode" if baseline_backend == "local" else baseline_backend
    if canonical not in {route.value for route in WORKER_ROUTES}:
        raise RoutePreparationError("baseline is not a supported worker backend")
    baseline = WorkerRoute(canonical)
    environment = os.environ if environ is None else environ
    try:
        if baseline == WorkerRoute.OPENCODE:
            load_local_config(config)
            resolve_opencode_binary()
        elif _backend_binary(baseline.value, path=environment.get("PATH")) is None:
            raise ProfileError(f"{baseline.value} executable is not on PATH")
        _profiles(config, baseline_backend, overrides)
    except (ProfileError, OpenCodeBinaryError) as exc:
        raise RoutePreparationError(str(exc)) from exc
    return RoutePlan(baseline, (baseline,), overrides, baseline_backend)


@dataclass(frozen=True)
class ExecutionBundle:
    backend: WorkerRoute
    runner: ClaudeRunner
    implement_profile: AgentProfile
    verify_profile: AgentProfile
    finalize_profile: AgentProfile
    codegraph_probe: CodeGraphProbe
    execution_backend: str = ""

    @property
    def phase_contract_text(self) -> str:
        """Pass to grind's work-prompt composer for this route's implementation."""
        return phase_contract(CodeGraphPhase.IMPLEMENTATION, self.codegraph_probe)

    def compose_prompt(self, task: str) -> str:
        """Wrap a logical task exactly once for this backend's execution surface."""
        return compose_worker_prompt(
            cast(Backend, self.execution_backend or self.backend.value),
            task + self.phase_contract_text,
        )


def prepare_route(
    plan: RoutePlan,
    backend: WorkerRoute,
    *,
    repo: Path,
    config: Config,
    codegraph_mode: CodeGraphMode,
    extra_env: Mapping[str, str] | None = None,
    adapter: CodeGraphAdapter | None = None,
) -> ExecutionBundle:
    """Repeat selected-route preflights and construct a fresh per-turn runner.

    Raise RoutePreparationError for the caller's human path. No worker starts,
    no claim changes, and no fallback route is selected here.
    """
    if backend not in plan.available_workers:
        raise RoutePreparationError(f"worker route {backend!r} is unavailable")
    backend = WorkerRoute(backend)
    execution_backend = plan.execution_backend(backend)
    try:
        env = dict(extra_env or {})
        if backend == WorkerRoute.OPENCODE:
            binary = str(resolve_opencode_binary())
            probe_models(load_local_config(config))
        else:
            binary = _backend_binary(
                backend.value, path=env.get("PATH", os.environ.get("PATH"))
            )
        if binary is None:
            raise RoutePreparationError(
                f"{backend.value} executable is no longer on PATH"
            )
        profiles = _profiles(config, execution_backend, plan.overrides)
        if backend == WorkerRoute.CLAUDE:
            check_hooks_enabled(repo)
        probe = (adapter or CodeGraphAdapter()).probe(
            repo, codegraph_mode, backend=execution_backend
        )
        if codegraph_mode == CodeGraphMode.REQUIRED and not probe.available:
            raise CodeGraphUnavailable(
                probe.reason or "required CodeGraph is unavailable"
            )
        runner = make_runner(cast(Backend, execution_backend), repo=repo)
        runner.claude_binary = binary
        configure = getattr(runner, "configure_codegraph", None)
        if callable(configure):
            configure(probe.capability)
        runner.extra_env.update(env)
        runner.extra_env.setdefault("BEADS_DIR", str((repo / ".beads").resolve()))
        return ExecutionBundle(backend, runner, *profiles, probe, execution_backend)
    except (BackendError, ProfileError, HookConflictError, CodeGraphUnavailable,
            LocalServerError, OpenCodeBinaryError) as exc:
        raise RoutePreparationError(
            f"cannot prepare {backend.value} route: {exc}"
        ) from exc


def baseline_implement_profile(config: Config, plan: RoutePlan) -> AgentProfile:
    """The implementation profile the baseline worker launches with."""
    return _profiles(
        config, plan.execution_backend(plan.baseline), plan.overrides
    )[0]
