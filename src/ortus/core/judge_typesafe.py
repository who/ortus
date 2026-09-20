"""Official TypeSafe System One adapter: one batched request, validated answers.

The adapter owns provider lifecycle, the literal question definitions and
strict answer parsing. It deliberately stops short of an action: threshold
comparisons and the failure mode belong to the deterministic gate policy, so
this boundary emits typed values only.

Every failure — the extra not installed, no key in the environment, a missed
deadline, an HTTP status, a malformed body — collapses to a
:class:`JudgeFailure` member. A verdict has no free-text field at all, so
provider prose cannot reach a log, an event or a bd comment through here.

:meth:`TypeSafeJudge.evaluate` is synchronous by contract and drives the async
client with :func:`asyncio.run`, so a cancelled request closes its socket
instead of orphaning a thread. Calling it from inside a running event loop is
a programming error and raises.
"""

from __future__ import annotations

from copy import deepcopy

import asyncio
import math
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping

from ortus.core.judge import JudgeAnswers, JudgeConfig, JudgeRoute, JudgeState
from ortus.core.judge_state import PackedState

API_KEY_ENV = "TYPESAFE_API_KEY"

ROUTE = "route"
NEEDS_HUMAN = "needs_human"
ACTION_RISK = "action_risk"


class JudgeFailure(str, Enum):
    """Why no answers came back, named without quoting the provider."""

    SDK_MISSING = "sdk_missing"
    KEY_MISSING = "key_missing"
    TIMEOUT = "timeout"
    SERVICE_ERROR = "service_error"
    INVALID_ANSWER = "invalid_answer"


@dataclass(frozen=True)
class JudgeUsage:
    """Token counts exactly as reported; never inferred from response text."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class JudgeVerdict:
    answers: JudgeAnswers | None = None
    failure: JudgeFailure | None = None
    usage: JudgeUsage | None = None

    def __post_init__(self) -> None:
        if (self.answers is None) == (self.failure is None):
            raise ValueError("a verdict carries either answers or a failure")


# Contrastive option criteria (PRD 7.1). `what` and `not_for` are the pair the
# judge separates on; the examples are literal, never "use judgment".
_ROUTE_CRITERIA: dict[JudgeRoute, dict[str, Any]] = {
    JudgeRoute.CLAUDE: {
        "what": "Implementation work that needs Claude Code tools or MCP servers.",
        "not_for": "Planning-only steps, or work blocked on operator credentials.",
        "examples": [
            "edit a module named by the issue and run the checks it names",
            "trace callers through the repository index before changing a symbol",
        ],
    },
    JudgeRoute.CODEX: {
        "what": "A Codex write session on a seat that already has Codex configured.",
        "not_for": "Seats where no Codex backend is available this cycle.",
        "examples": ["apply a bounded patch the issue fully specifies"],
    },
    JudgeRoute.GROK: {
        "what": "Implementation using the configured Grok worker.",
        "not_for": "Seats without a prepared Grok backend.",
        "examples": ["implement the issue using Grok tools"],
    },
    JudgeRoute.OPENCODE: {
        "what": "Implementation using the configured operator-served model.",
        "not_for": "Seats without a reachable configured model and OpenCode tools.",
        "examples": ["apply the specified change using the local model"],
    },
    JudgeRoute.SKIP: {
        "what": "No worker turn is needed on this issue this cycle.",
        "not_for": "Ready work that states acceptance criteria and is unblocked.",
        "examples": ["the issue's outcome is already satisfied by committed work"],
    },
    JudgeRoute.HUMAN: {
        "what": "Blocked on the operator: a credential, a product call, or spend.",
        "not_for": "Ordinary coding work inside the sandbox the worker already has.",
        "examples": [
            "the step needs an account or key the seat does not hold",
            "the issue records a PLAN-GAP only the operator can resolve",
        ],
    },
}

_NEEDS_HUMAN_TRUE = [
    "The step needs a credential, key or account the seat does not already hold.",
    "The step needs an irreversible product decision the operator has not made.",
    "The step needs authorization to spend money.",
    "The issue records a PLAN-GAP that only the operator can resolve.",
]
_NEEDS_HUMAN_FALSE = [
    "The worker can finish inside the issue's acceptance criteria and its sandbox.",
    "The issue names the files and checks the work needs.",
]

# Ordered rubric: index is the score, so the highest score is len - 1 (PRD 7.3).
_RISK_LEVELS: tuple[dict[str, Any], ...] = (
    {
        "level": "routine",
        "what": "Reads and writes inside the repository work tree, and its own tests.",
    },
    {
        "level": "elevated",
        "what": "Writes outside the repository, installs software, or reaches an "
        "unknown network host.",
    },
    {
        "level": "dangerous",
        "what": "Force-pushes, deploys to production, or reads secret files such as "
        "keys, tokens or a private environment file.",
    },
)


def route_options(config: JudgeConfig, state: JudgeState) -> tuple[JudgeRoute, ...]:
    """Configured routes narrowed to what this cycle can actually do.

    A backend the seat cannot launch is never offered, so an unavailable route
    is a malformed answer rather than a decision the runner has to refuse later.
    """
    return tuple(
        route
        for route in config.routes
        if route in state.backends_available
        or route in (JudgeRoute.SKIP, JudgeRoute.HUMAN)
    )


def build_questions(config: JudgeConfig, state: JudgeState) -> dict[str, dict[str, Any]]:
    """The three atomic pre-turn questions, as one System One request mapping.

    Raw dictionaries rather than SDK models: the question text is reviewable
    here without the optional extra installed, and asking all three in one
    request keeps the added latency to a single round trip.
    """
    criteria = config.question_criteria
    return {
        ROUTE: {
            "type": "choice",
            "instructions": "Which path should handle the next turn on this issue?",
            "criteria": {
                route.value: deepcopy(criteria.get(ROUTE, {}).get(route.value, _ROUTE_CRITERIA[route]))
                for route in route_options(config, state)
            },
        },
        NEEDS_HUMAN: {
            "type": "noul",
            "instructions": "Does this step need the operator before a worker runs?",
            "criteria": deepcopy(criteria.get(NEEDS_HUMAN, {
                "true": list(_NEEDS_HUMAN_TRUE), "false": list(_NEEDS_HUMAN_FALSE),
            })),
        },
        ACTION_RISK: {
            "type": "score",
            "instructions": "How risky is the action this issue asks for next?",
            "criteria": deepcopy(criteria.get(ACTION_RISK, list(_RISK_LEVELS))),
        },
    }


def _default_client(config: JudgeConfig) -> Any:
    """Build the official async client; imported here so a disabled gate is free.

    The key is never read into Ortus: the SDK picks it up from the environment
    itself. Retries are off because the runner owns the deadline.
    """
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

    return AsyncTypeSafeClient(
        model=config.model,
        retry=RetryPolicy(max_retries=0),
        timeout=config.timeout_seconds,
    )


@dataclass(frozen=True)
class TypeSafeJudge:
    """Ask System One one batched question set and validate every answer."""

    config: JudgeConfig
    client_factory: Callable[[JudgeConfig], Any] = _default_client
    environ: Mapping[str, str] | None = None

    def evaluate(self, state: JudgeState) -> JudgeVerdict:
        env = os.environ if self.environ is None else self.environ
        # Cheapest prerequisite first: an opted-in seat without a key is
        # reported without importing an optional dependency to find out.
        if not str(env.get(API_KEY_ENV, "")).strip():
            return JudgeVerdict(failure=JudgeFailure.KEY_MISSING)
        return asyncio.run(self._ask(state))

    async def _ask(self, state: JudgeState) -> JudgeVerdict:
        offered = route_options(self.config, state)
        questions = build_questions(self.config, state)
        async with AsyncExitStack() as stack:
            try:
                client = self.client_factory(self.config)
            except ImportError:
                return JudgeVerdict(failure=JudgeFailure.SDK_MISSING)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            try:
                response = await asyncio.wait_for(
                    client.system_one(
                        _transport_state(state, self.config),
                        questions,
                        model=self.config.model,
                        timeout=self.config.timeout_seconds,
                    ),
                    self.config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                return JudgeVerdict(failure=JudgeFailure.TIMEOUT)
            except Exception:  # noqa: BLE001 - the status is the whole report
                return JudgeVerdict(failure=JudgeFailure.SERVICE_ERROR)
        try:
            answers, usage = _validated(response, self.config, offered)
        except _Invalid:
            return JudgeVerdict(failure=JudgeFailure.INVALID_ANSWER)
        return JudgeVerdict(answers=answers, usage=usage)


class _Invalid(Exception):
    """Internal control flow; it carries no provider value, only the fact."""


def _transport_state(state: JudgeState, config: JudgeConfig | None = None) -> dict[str, object]:
    # The packed transport shape belongs to judge_state, which already dropped
    # sensitive fields and fit the byte budget. Do not invent a second one.
    if config is not None:
        from ortus.core.judge_packs import CRITERIA_VERSION, criteria_hash

        state = replace(state, criteria_version=CRITERIA_VERSION,
                        criteria_hash=criteria_hash(config, build_questions(config, state)))
        if len(PackedState(state, ()).to_json().encode("utf-8")) > config.total_bytes_cap:
            raise ValueError("judge state exceeds the configured byte budget")
    return PackedState(state, ()).to_payload()


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise _Invalid
    return value


def _number(value: object, maximum: float) -> float:
    """Accept only a finite real in range; a bool is not a measurement."""
    if type(value) not in (int, float):
        raise _Invalid
    try:
        number = float(value)
    except OverflowError:
        raise _Invalid from None
    if not math.isfinite(number) or not 0.0 <= number <= maximum:
        raise _Invalid
    return number


def _answer(answers: Mapping[str, Any], name: str, kind: str) -> Mapping[str, Any]:
    body = _mapping(answers.get(name))
    if body.get("type") != kind:
        raise _Invalid
    return body


def _usage(value: object) -> JudgeUsage | None:
    if not isinstance(value, Mapping):
        return None
    counts = (value.get("input_tokens"), value.get("output_tokens"))
    if any(type(count) is not int or count < 0 for count in counts):
        return None
    return JudgeUsage(*counts)


def _validated(
    response: object, config: JudgeConfig, offered: tuple[JudgeRoute, ...]
) -> tuple[JudgeAnswers, JudgeUsage | None]:
    """Turn one response into typed answers, or refuse the whole response."""
    dump = getattr(response, "model_dump", None)
    if callable(dump):
        try:
            response = dump(mode="json")
        except Exception:  # noqa: BLE001 - a body we cannot read is invalid
            raise _Invalid from None
    body = _mapping(response)
    # The pin is part of the answer: a seat that asked for an exact version must
    # not act on whatever the service decided to serve instead.
    if body.get("model") != config.model:
        raise _Invalid
    answers = _mapping(body.get("answers"))
    if set(answers) != {ROUTE, NEEDS_HUMAN, ACTION_RISK}:
        raise _Invalid

    chosen = _answer(answers, ROUTE, "choice")
    label = chosen.get("choice")
    if not isinstance(label, str):
        raise _Invalid
    try:
        route = JudgeRoute(label)
    except ValueError:
        raise _Invalid from None
    if route not in offered:
        raise _Invalid

    noul = _number(_answer(answers, NEEDS_HUMAN, "noul").get("noul"), 1.0)
    risk = _answer(answers, ACTION_RISK, "score")
    return (
        JudgeAnswers(
            route=route,
            route_confidence=_number(chosen.get("confidence"), 1.0),
            needs_human=noul,
            # A noul is already a calibrated probability and the primitive
            # reports no separate confidence, so how far it sits from the coin
            # flip IS its confidence. Arithmetic stays in code (ZFC).
            noul_confidence=max(noul, 1.0 - noul),
            action_risk=_number(risk.get("score"), float(len(_RISK_LEVELS) - 1)),
            risk_confidence=_number(risk.get("confidence"), 1.0),
        ),
        _usage(body.get("usage")),
    )
