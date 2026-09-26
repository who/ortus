"""Prepare execution routes for the opt-in gate before claiming work.

The gate caller discovers routes before asking the judge, then prepares the
chosen route before acquiring a claim. Preparation failures require human
handling; they are not provider failures eligible for a second backend.
Ungated grind keeps its existing runner construction and never calls here.

This module also holds the per-bead model router. Route selection answers
*which backend* runs a bead; the router answers *how much model* that bead is
worth on the backend already chosen. The judge is System One: it reports
calibrated probabilities about the claimed bead and nothing about models.
:func:`route_implement_profile` is System Two: it shrinks those probabilities
by the confidence reported with them, reduces them to one difficulty scalar,
and maps that scalar to an implementation model and reasoning effort. No
threshold here can stop or escalate a bead — a vector only ever moves the
worker between model tiers, so the router has no confidence floor and no
needs-human gate of its own.

The router is a flag, `jev_model_router` in `.ortusrc` with
`ORTUS_JEV_ROUTER` as a per-run override. It defaults on, because the arms
were run and the routed one won; off resolves the pinned
`[profiles.<backend>.implement]` profile exactly as before, which is both the
kill switch and the control arm the comparison ran against. The arms were
compared on **cost per closed bead** across the whole tree — planner plus
every worker — with **close rate** as the guardrail: a cheaper arm that closes
fewer beads has lost, and prompt or token counts settle nothing either way. Every decision is logged so the
tier thresholds can be rewritten from observed outcomes rather than argued
from taste.
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
from ortus.core.judge import WORKER_ROUTES, JudgeAnswers, JudgeConfig, JudgeRoute
from ortus.core.local_backend import (
    LocalServerError, OpenCodeBinaryError, load_local_config,
    probe_models, resolve_opencode_binary,
)
from ortus.core.profiles import (
    SUPPORTED_EFFORTS, AgentProfile, Phase, ProfileError, validate_profile_values,
)

_WORKERS = WORKER_ROUTES


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
            elif _backend_binary(backend.value, path=environment.get("PATH")) is None:
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
            binary = _backend_binary(
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


#: `.ortusrc` key and environment variable that enable the per-bead router.
JEV_ROUTER_CONFIG_KEY = "jev_model_router"
JEV_ROUTER_ENV = "ORTUS_JEV_ROUTER"

# The same vocabulary the prompt-prefix flag accepts, so an operator who
# exports one A/B flag does not have to remember a second spelling.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})

#: Highest `action_risk` the judge can report: the index of its last risk
#: level, the same ceiling the decision log validates that field against.
RISK_CEILING = 2.0

#: Difficulty below which a bead is cheap enough for the economy tier, and at
#: or above which it is worth the frontier tier. Tier boundaries only, never
#: gates: a bead outside either band still runs, on the middle tier.
CHEAP_CEILING = 0.34
FRONTIER_FLOOR = 0.67

# Ascending reasoning effort. `max` sits above `xhigh` because the backends
# that define both treat it as a thinking-budget setting beyond the effort
# names; a backend that omits a rung simply skips it.
_EFFORT_LADDER: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)


class RouterTier(str, Enum):
    """How much model one bead is worth, before it is mapped to a profile."""

    #: The phase profile a cheap model already suits, at the lowest effort.
    CHEAP = "cheap"
    #: The pinned implementation profile, untouched.
    BASELINE = "baseline"
    #: The planner's model at the highest effort the backend offers.
    FRONTIER = "frontier"


class RouteReason(str, Enum):
    """Why a bead landed on its tier, for the log the A/B is rewritten from."""

    FLAG_OFF = "flag_off"
    JUDGE_UNAVAILABLE = "judge_unavailable"
    MAPPED = "mapped"
    UNMAPPABLE_PROFILE = "unmappable_profile"


@dataclass(frozen=True)
class ModelRoute:
    """One bead's implementation profile and the vectors that chose it.

    The vectors are ``None`` whenever no answers reached the router. A missing
    vector is recorded as missing rather than as a zero, so a run that never
    consulted the judge cannot read later as a run the judge scored at the
    bottom of every scale.
    """

    tier: RouterTier
    reason: RouteReason
    profile: AgentProfile
    needs_frontier: float | None = None
    action_risk: float | None = None
    difficulty: float | None = None

    @property
    def routed(self) -> bool:
        """True when the vectors, not the pinned profile, chose this model."""
        return self.reason is RouteReason.MAPPED


def _flag(value: object) -> bool | None:
    """A configured or exported flag value, or None when it is unreadable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUTHY:
            return True
        if text in _FALSEY:
            return False
    return None


def jev_router_enabled(
    config: Config | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """True when the judge's vectors pick this run's implementation model.

    The environment wins over `.ortusrc` so one A/B run can flip the arm
    without touching a tracked file, and a value neither layer can parse falls
    back to the pinned profile rather than guessing at an opt-in.
    """
    env = os.environ if environ is None else environ
    exported = _flag(env.get(JEV_ROUTER_ENV))
    if exported is not None:
        return exported
    if config is None:
        return False
    return bool(_flag(config.get(JEV_ROUTER_CONFIG_KEY, False)))


def _shrunk(probability: float, confidence: float) -> float:
    """Pull a probability toward the coin flip by how unsure the judge was.

    A vector reported with full confidence passes through unchanged; one
    reported with no confidence carries no information and lands on 0.5, so an
    unsure judge moves a bead toward the middle tier instead of to an extreme.

    Only the risk score arrives with a confidence the provider reported. The
    noul primitive reports none, and its confidence is derived in code as the
    distance of the probability from the coin flip, so shrinking that vector
    partly re-applies its own value. The shrink is kept uniform anyway: which
    weighting actually closes beads more cheaply is what the A/B is for, and
    both vectors stay monotonic in the probability either way.
    """
    p = min(max(probability, 0.0), 1.0)
    c = min(max(confidence, 0.0), 1.0)
    return 0.5 + (p - 0.5) * c


def _effort_ladder(backend: str) -> tuple[str, ...]:
    """The backend's supported efforts, weakest first."""
    supported = SUPPORTED_EFFORTS.get(backend, frozenset())
    return tuple(effort for effort in _EFFORT_LADDER if effort in supported)


def _tier(difficulty: float) -> RouterTier:
    if difficulty < CHEAP_CEILING:
        return RouterTier.CHEAP
    if difficulty >= FRONTIER_FLOOR:
        return RouterTier.FRONTIER
    return RouterTier.BASELINE


def baseline_implement_profile(config: Config, plan: RoutePlan) -> AgentProfile:
    """The implementation profile the baseline worker would launch with.

    Preparation resolves this while building a runner, which an observer has
    no reason to do. Reading it straight off the plan lets a shadow seat score
    a route against the same profile the enforcing seat would have started
    from, instead of against a second resolution that could drift from it.
    """
    return _profiles(
        config, plan.execution_backend(plan.baseline), plan.overrides
    )[0]


def route_implement_profile(
    config: Config,
    backend: str,
    baseline: AgentProfile,
    answers: JudgeAnswers | None,
    *,
    overrides: RouteOverrides = RouteOverrides(),
    enabled: bool = True,
) -> ModelRoute:
    """Map the judge's vectors to the claimed bead's implementation profile.

    ``baseline`` is the profile the pinned configuration already resolved for
    this backend, so it carries any operator pin. That pin wins: the router
    fills only the fields `--implement-model` and `--implement-reasoning-effort`
    left open, and an operator who pinned both gets the tier recorded and the
    profile they asked for. A tier's model is not invented here either — it is
    read from a phase the operator already declares, the cheap tier from
    `finalize` and the frontier tier from `plan`, which is why routing workers
    down never moves the planner off frontier.

    Every exit fails open onto ``baseline``: a judge that answered nothing, or
    a tier whose mapped values the backend rejects, costs the bead its routing
    and nothing else.
    """
    if not enabled:
        return ModelRoute(RouterTier.BASELINE, RouteReason.FLAG_OFF, baseline)
    if answers is None:
        return ModelRoute(RouterTier.BASELINE, RouteReason.JUDGE_UNAVAILABLE, baseline)

    needs_frontier = _shrunk(answers.needs_human, answers.noul_confidence)
    action_risk = _shrunk(answers.action_risk / RISK_CEILING, answers.risk_confidence)
    difficulty = max(needs_frontier, action_risk)
    tier = _tier(difficulty)
    vectors = (needs_frontier, action_risk, difficulty)
    if tier is RouterTier.BASELINE:
        return ModelRoute(tier, RouteReason.MAPPED, baseline, *vectors)

    ladder = _effort_ladder(backend)
    cheap = tier is RouterTier.CHEAP
    tier_phase = Phase.FINALIZE if cheap else Phase.PLAN
    try:
        tier_model = config.resolve_profile(backend, tier_phase).model
        model = (
            baseline.model
            if overrides.implement_model is not None
            else (tier_model or baseline.model)
        )
        effort = (
            baseline.reasoning_effort
            if overrides.implement_reasoning_effort is not None
            else (ladder[0] if cheap else ladder[-1])
        )
        profile = validate_profile_values(
            backend, Phase.IMPLEMENT, model=model, reasoning_effort=effort
        )
    except (IndexError, ProfileError):
        return ModelRoute(
            RouterTier.BASELINE, RouteReason.UNMAPPABLE_PROFILE, baseline, *vectors
        )
    return ModelRoute(tier, RouteReason.MAPPED, profile, *vectors)
