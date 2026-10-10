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
import json
import math
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable, Mapping

from ortus.core import output
from ortus.core.user_env import judge_environment

from ortus.core.judge import WORKER_ROUTES, JudgeAnswers, JudgeConfig, JudgeRoute, JudgeState
from ortus.core.judge_questions import (
    HAS_BASIS, NONE, basis_question, noul_question, router_questions,
    shuffled_choice, split_state, with_none,
)
from ortus.core.judge_state import PackedState

API_KEY_ENV = "TYPESAFE_API_KEY"

ROUTE = "route"
#: The pre-v3 rubric names. They survive as the legacy answer fields every
#: decision record still carries, and as override keys that are now ignored.
NEEDS_HUMAN = "needs_human"
ACTION_RISK = "action_risk"

#: pre_turn's narrow questions beside the route pick (question set pre-turn-v3).
NEEDS_OPERATOR_INPUT = "needs_operator_input"
DESTRUCTIVE_SCOPE = "destructive_scope"

#: State fields that carry issue text, which travel in the untrusted block.
_UNTRUSTED = ("title", "objective", "acceptance", "design", "proposed_tool")


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
    #: The route options in the order this request presented them.
    option_order: tuple[str, ...] = ()

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


_REPORTED: set[tuple[str, str]] = set()


def report_ignored(table: str, overrides: Mapping[str, object], removed: tuple[str, ...]) -> None:
    """Name, once per process, an override for a question that no longer exists.

    The override still loads, so an existing `.ortusrc` keeps working; it just
    has no question left to apply to.
    """
    for key in removed:
        if key in overrides and (table, key) not in _REPORTED:
            _REPORTED.add((table, key))
            output.progress("judge", f"judge.{table}.{key} names a retired question; ignored")


_NEEDS_OPERATOR_TRUE = [
    "The step needs a credential, key or account the seat does not already hold.",
    "The step needs an access grant only the operator can give.",
    "The step needs a product decision the operator has not made.",
]
_NEEDS_OPERATOR_FALSE = [
    "The spec already resolves its decisions and the worker's sandbox holds what it needs.",
]
_DESTRUCTIVE_TRUE = [
    "The spec asks to delete, migrate or rewrite data other people or services share.",
]
_DESTRUCTIVE_FALSE = [
    "The change edits the repository's own code, docs and tests.",
]


def build_questions(config: JudgeConfig, state: JudgeState) -> dict[str, dict[str, Any]]:
    """The narrow pre_turn questions about one issue, as one System One request.

    One decision per question: the route pick (with `none`) and its basis, two
    separate operator-input and destructive-scope yes/nos, and the model
    router's own questions. The route options come back in canonical order;
    the request shuffles them per decision (see :meth:`TypeSafeJudge.evaluate`)
    so the criteria hash stays one value for one wording.

    Raw dictionaries rather than SDK models: the question text is reviewable
    here without the optional extra installed.
    """
    if state.phase.value == "semantic_readiness":
        from ortus.core.judge_readiness import readiness_questions

        return readiness_questions(config, state)
    criteria = config.question_criteria
    report_ignored("question_criteria", criteria, (NEEDS_HUMAN, ACTION_RISK))
    return {
        ROUTE: {
            "type": "choice",
            "instructions": "Which listed path should handle the next turn on this issue? "
                            "Pick none when the facts give no option a basis.",
            "criteria": with_none({
                route.value: deepcopy(criteria.get(ROUTE, {}).get(route.value, _ROUTE_CRITERIA[route]))
                for route in route_options(config, state)
            }),
        },
        HAS_BASIS: basis_question("the path picked for the next turn"),
        NEEDS_OPERATOR_INPUT: noul_question(
            "Does the next turn need something only the operator can give: "
            "a credential, an access grant, or a product decision?",
            _NEEDS_OPERATOR_TRUE, _NEEDS_OPERATOR_FALSE,
        ),
        DESTRUCTIVE_SCOPE: noul_question(
            "Does the spec ask to delete, migrate, or rewrite shared data?",
            _DESTRUCTIVE_TRUE, _DESTRUCTIVE_FALSE,
        ),
        **router_questions(),
    }


def presented(questions: Mapping[str, dict[str, Any]], seed: str) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """The questions as sent, with the route options in this decision's order."""
    if ROUTE not in questions:
        return dict(questions), ()
    shuffled, order = shuffled_choice(questions[ROUTE]["criteria"], seed)
    return {**questions, ROUTE: {**questions[ROUTE], "criteria": shuffled}}, tuple(order)


def _default_client(config: JudgeConfig) -> Any:
    """Build the official async client; imported here so a disabled gate is free.

    The SDK picks up the key from the prepared process environment
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

    def evaluate(self, state: JudgeState, *, seed: str | None = None) -> JudgeVerdict:
        """Ask once. ``seed`` (the decision id) fixes the presented option order."""
        env = judge_environment(self.environ)
        # Cheapest prerequisite first: an opted-in seat without a key is
        # reported without importing an optional dependency to find out.
        if not str(env.get(API_KEY_ENV, "")).strip():
            return JudgeVerdict(failure=JudgeFailure.KEY_MISSING)
        return asyncio.run(self._ask(state, seed or state.issue_id))

    async def _ask(self, state: JudgeState, seed: str) -> JudgeVerdict:
        offered = route_options(self.config, state)
        questions, order = presented(build_questions(self.config, state), seed)
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
            answers, usage = _validated(response, self.config, offered, questions)
        except _Invalid:
            return JudgeVerdict(failure=JudgeFailure.INVALID_ANSWER)
        return JudgeVerdict(answers=answers, usage=usage, option_order=order)


class _Invalid(Exception):
    """Internal control flow; it carries no provider value, only the fact."""


def _transport_state(state: JudgeState, config: JudgeConfig | None = None) -> dict[str, object]:
    """The packed state split into a trusted facts block and an untrusted one.

    The packed fields belong to judge_state, which already dropped sensitive
    values and fit the byte budget; this only sorts them. Metadata and the
    code-computed spec facts are trusted, issue text is not.
    """
    if config is not None:
        from ortus.core.judge_packs import criteria_hash, criteria_version

        state = replace(state, criteria_version=criteria_version(state.phase),
                        criteria_hash=criteria_hash(config, build_questions(config, state)))
    flat = PackedState(state, ()).to_payload()
    facts = flat.pop("facts", {})
    untrusted = {key: flat.pop(key) for key in _UNTRUSTED if key in flat}
    payload = split_state({**flat, **facts}, untrusted)
    if config is not None and len(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) > config.total_bytes_cap:
        raise ValueError("judge state exceeds the configured byte budget")
    return payload


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


def _narrow(answers: Mapping[str, Any], questions: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Every asked question's typed answer, or refuse the whole response."""
    if set(answers) != set(questions):
        raise _Invalid
    narrow: dict[str, Any] = {}
    for name, question in questions.items():
        kind = question.get("type")
        body = _answer(answers, name, kind)
        if kind == "noul":
            narrow[name] = _number(body.get("noul"), 1.0)
        elif kind == "choice":
            label = body.get("choice")
            if not isinstance(label, str) or label not in question["criteria"]:
                raise _Invalid
            narrow[name] = label
            narrow[f"{name}_confidence"] = _number(body.get("confidence"), 1.0)
        else:
            raise _Invalid
    return narrow


def _lean(p: float) -> float:
    """A noul's own confidence: how far it sits from the coin flip."""
    return max(p, 1.0 - p)


def _fallback_route(offered: tuple[JudgeRoute, ...]) -> JudgeRoute:
    """The legacy `route` field for an answer that picked no route."""
    return next((route for route in offered if route in WORKER_ROUTES),
                offered[0] if offered else JudgeRoute.HUMAN)


def legacy_answers(
    narrow: dict[str, Any], offered: tuple[JudgeRoute, ...],
) -> JudgeAnswers:
    """The six-field shape every decision record keeps, derived in code.

    pre_turn: the route pick (the first offered worker for `none`), the
    operator-input noul as `needs_human` and the destructive-scope noul scaled
    onto the 0-2 risk range. Readiness: the open-decision noul as
    `needs_human`, and the weaker of the observable-acceptance and
    design-matches-objective answers, inverted, as the risk. The narrow answers
    ride along so nothing is lost in the mapping.
    """
    if ROUTE in narrow:
        label = narrow[ROUTE]
        if label == NONE:
            route = _fallback_route(offered)
        else:
            try:
                route = JudgeRoute(label)
            except ValueError:
                raise _Invalid from None
            if route not in offered:
                raise _Invalid
        operator = narrow[NEEDS_OPERATOR_INPUT]
        destructive = narrow[DESTRUCTIVE_SCOPE]
        return JudgeAnswers(
            route=route, route_confidence=narrow[f"{ROUTE}_confidence"],
            needs_human=operator, noul_confidence=_lean(operator),
            action_risk=2.0 * destructive, risk_confidence=_lean(destructive),
            narrow=narrow,
        )
    from ortus.core.judge_readiness import (
        ACCEPTANCE_OBSERVABLE, DESIGN_MATCHES_OBJECTIVE, OPEN_PRODUCT_DECISION,
    )

    open_decision = narrow[OPEN_PRODUCT_DECISION]
    sound = min(narrow[ACCEPTANCE_OBSERVABLE], narrow[DESIGN_MATCHES_OBJECTIVE])
    return JudgeAnswers(
        route=_fallback_route(offered), route_confidence=narrow[HAS_BASIS],
        needs_human=open_decision, noul_confidence=_lean(open_decision),
        action_risk=2.0 * (1.0 - sound), risk_confidence=_lean(sound),
        narrow=narrow,
    )


def _validated(
    response: object, config: JudgeConfig, offered: tuple[JudgeRoute, ...],
    questions: Mapping[str, dict[str, Any]],
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
    narrow = _narrow(_mapping(body.get("answers")), questions)
    return legacy_answers(narrow, offered), _usage(body.get("usage"))
