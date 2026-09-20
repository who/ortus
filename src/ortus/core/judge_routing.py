"""Prepare execution routes for the opt-in gate before claiming work.

The gate caller discovers routes before asking the judge, then prepares the
chosen route before acquiring a claim. Preparation failures require human
handling; they are not provider failures eligible for a second backend.
Ungated grind keeps its existing runner construction and never calls here.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
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
from ortus.core.judge import WORKER_ROUTES, JudgeConfig, JudgeRoute
from ortus.core.local_backend import (
    LocalServerError, OpenCodeBinaryError, load_local_config,
    probe_models, resolve_opencode_binary,
)
from ortus.core.profiles import AgentProfile, Phase, ProfileError

_WORKERS = WORKER_ROUTES


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
    baseline: JudgeRoute
    available_workers: tuple[JudgeRoute, ...]
    offered_routes: tuple[JudgeRoute, ...]
    overrides: RouteOverrides
    baseline_backend: str = ""

    def execution_backend(self, route: JudgeRoute) -> str:
        return self.baseline_backend if route == self.baseline and self.baseline_backend else route.value


def _profiles(
    config: Config,
    backend: str,
    overrides: RouteOverrides,
) -> tuple[AgentProfile, AgentProfile, AgentProfile]:
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


def plan_routes(
    config: Config,
    judge: JudgeConfig,
    baseline_backend: str,
    *,
    overrides: RouteOverrides = RouteOverrides(),
    environ: Mapping[str, str] | None = None,
) -> RoutePlan:
    """Filter configured workers without installing, authenticating or launching.

    Keep a valid baseline available for fail-open policy even if it is not an
    offered route. Optional candidates with missing binaries or invalid profiles
    are excluded. The baseline itself must always be runnable.
    """
    canonical = "opencode" if baseline_backend == "local" else baseline_backend
    if canonical not in _WORKERS:
        raise RoutePreparationError(
            "judge baseline is not a supported worker backend"
        )
    baseline = JudgeRoute(canonical)
    environment = os.environ if environ is None else environ
    pinned = overrides.pinned(environment)
    if judge.pre_tool and baseline != JudgeRoute.CLAUDE:
        raise RoutePreparationError("judge.pre_tool supports only Claude")
    available: list[JudgeRoute] = []
    for backend in _WORKERS:
        if judge.pre_tool and backend != JudgeRoute.CLAUDE:
            continue
        if backend != baseline and (pinned or backend not in judge.routes):
            continue
        try:
            if backend == JudgeRoute.OPENCODE:
                load_local_config(config)
                resolve_opencode_binary()
            elif shutil.which(backend.value, path=environment.get("PATH")) is None:
                raise ProfileError(f"{backend.value} executable is not on PATH")
            _profiles(
                config,
                baseline_backend if backend == baseline else backend.value,
                overrides if backend == baseline else RouteOverrides(),
            )
        except (ProfileError, OpenCodeBinaryError) as exc:
            if backend == baseline:
                raise RoutePreparationError(str(exc)) from exc
            continue
        available.append(backend)
    offered = tuple(
        route for route in judge.routes if route not in _WORKERS or route in available
    )
    if not any(route in _WORKERS for route in offered):
        raise RoutePreparationError(
            "judge requires at least one available configured worker route"
        )
    return RoutePlan(baseline, tuple(available), offered, overrides, baseline_backend)


@dataclass(frozen=True)
class ExecutionBundle:
    backend: JudgeRoute
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
    backend: JudgeRoute,
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
    backend = JudgeRoute(backend)
    execution_backend = plan.execution_backend(backend)
    try:
        env = dict(extra_env or {})
        if backend == JudgeRoute.OPENCODE:
            binary = str(resolve_opencode_binary())
            probe_models(load_local_config(config))
        else:
            binary = shutil.which(
                backend.value, path=env.get("PATH", os.environ.get("PATH"))
            )
        if binary is None:
            raise RoutePreparationError(
                f"{backend.value} executable is no longer on PATH"
            )
        profiles = _profiles(
            config,
            execution_backend,
            plan.overrides if backend == plan.baseline else RouteOverrides(),
        )
        if backend == JudgeRoute.CLAUDE:
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
