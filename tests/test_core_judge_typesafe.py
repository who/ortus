"""One batched judge request: pinned, bounded, and validated before it is used."""

from __future__ import annotations

import asyncio
import builtins
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from ortus.core.judge import JudgeConfig, JudgeRoute, JudgeState
from ortus.core.judge_state import PackedState
from ortus.core.judge_typesafe import (
    ACTION_RISK,
    NEEDS_HUMAN,
    ROUTE,
    JudgeFailure,
    JudgeUsage,
    JudgeVerdict,
    TypeSafeJudge,
    build_questions,
    route_options,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "jev" / "pre_turn.json").read_text())

STATE = JudgeState(issue_id="ortus-1234", issue_type="task", priority=1)
CLAUDE_ONLY = JudgeState(issue_id="ortus-1234", backends_available=(JudgeRoute.CLAUDE,))

SECRET = "sk-live-must-never-reach-a-log"


class FakeError(Exception):
    """An SDK error whose message quotes the response body, as the real ones do."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}: {SECRET}")
        self.status_code = status


class FakeClient:
    """Records one request; closes exactly as the official async client does."""

    def __init__(self, response=None, error=None, delay=0.0):
        self.response = response
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []
        self.closed = 0
        self.cancelled = False

    async def system_one(self, state, questions, *, model=None, timeout=None, **extra):
        self.calls.append(
            {"state": state, "questions": questions, "model": model, "timeout": timeout}
        )
        if self.delay:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        return self.response

    async def aclose(self):
        self.closed += 1


class FakeManagedClient(FakeClient):
    """The variant that prefers `async with`, which the official client supports."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.closed += 1
        return False


def judge(client, config=None, key=SECRET):
    return TypeSafeJudge(
        config or JudgeConfig(), lambda cfg: client, {"TYPESAFE_API_KEY": key}
    )


def test_one_pinned_request_asks_all_three_questions():
    client = FakeClient(CASES["valid"])
    verdict = judge(client).evaluate(STATE)

    assert verdict.failure is None
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == JudgeConfig().model == "jev-1.13.0"
    assert call["timeout"] == JudgeConfig().timeout_seconds
    assert set(call["questions"]) == {ROUTE, NEEDS_HUMAN, ACTION_RISK}
    assert call["questions"] == build_questions(JudgeConfig(), STATE)


def test_state_is_the_packed_transport_shape():
    client = FakeClient(CASES["valid"])
    judge(client).evaluate(STATE)

    assert client.calls[0]["state"] == PackedState(STATE, ()).to_payload()
    assert "ortus-1234" in json.dumps(client.calls[0]["state"])


def test_valid_answers_are_typed_and_the_noul_carries_its_own_confidence():
    verdict = judge(FakeClient(CASES["valid"])).evaluate(STATE)

    answers = verdict.answers
    assert answers.route is JudgeRoute.CLAUDE
    assert answers.route_confidence == 0.93
    assert answers.needs_human == 0.04
    assert answers.noul_confidence == pytest.approx(0.96)
    assert answers.action_risk == 0.2
    assert answers.risk_confidence == 0.88
    assert verdict.usage == JudgeUsage(input_tokens=412, output_tokens=18)


@pytest.mark.parametrize("case", ["no_usage", "partial_usage"])
def test_usage_is_reported_only_when_the_service_reports_it(case):
    verdict = judge(FakeClient(CASES[case])).evaluate(STATE)

    assert verdict.answers.route is JudgeRoute.CLAUDE
    assert verdict.usage is None


@pytest.mark.parametrize(
    "case",
    [
        "unpinned_model",
        "missing_answer",
        "extra_answer",
        "wrong_answer_type",
        "unexpected_route",
        "string_confidence",
        "infinite_confidence",
        "bool_as_noul",
        "nan_score",
        "score_out_of_range",
        "negative_confidence",
    ],
)
def test_a_malformed_response_yields_no_answers(case):
    verdict = judge(FakeClient(CASES[case])).evaluate(STATE)

    assert verdict.failure is JudgeFailure.INVALID_ANSWER
    assert verdict.answers is None


@pytest.mark.parametrize("response", [None, "answers: proceed", ["route"], {"answers": {}}])
def test_a_response_that_is_not_an_answer_document_is_invalid(response):
    assert judge(FakeClient(response)).evaluate(STATE).failure is JudgeFailure.INVALID_ANSWER


def test_a_route_the_seat_cannot_launch_is_invalid():
    offered = judge(FakeClient(CASES["route_not_offered"]))

    assert offered.evaluate(STATE).answers.route is JudgeRoute.CODEX
    assert offered.evaluate(CLAUDE_ONLY).failure is JudgeFailure.INVALID_ANSWER


def test_questions_offer_only_the_routes_this_cycle_has():
    config = JudgeConfig()

    assert route_options(config, CLAUDE_ONLY) == (
        JudgeRoute.CLAUDE, JudgeRoute.SKIP, JudgeRoute.HUMAN,
    )
    questions = build_questions(config, CLAUDE_ONLY)
    assert set(questions[ROUTE]["criteria"]) == {"claude", "skip", "human"}
    assert all(
        {"what", "not_for"} <= set(option) for option in questions[ROUTE]["criteria"].values()
    )
    assert set(questions[NEEDS_HUMAN]["criteria"]) == {"true", "false"}
    assert len(questions[ACTION_RISK]["criteria"]) == 3


def test_workers_only_configuration_never_offers_an_escalation_route():
    config = JudgeConfig(routes=(JudgeRoute.CLAUDE, JudgeRoute.CODEX))

    assert set(build_questions(config, STATE)[ROUTE]["criteria"]) == {"claude", "codex"}


def test_a_missing_key_is_reported_before_a_client_exists(monkeypatch):
    def refuse(config):
        raise AssertionError("the adapter built a client without a key")

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert TypeSafeJudge(JudgeConfig(), refuse, {}).evaluate(STATE).failure is (
        JudgeFailure.KEY_MISSING
    )
    assert TypeSafeJudge(JudgeConfig(), refuse).evaluate(STATE).failure is (
        JudgeFailure.KEY_MISSING
    )
    assert TypeSafeJudge(JudgeConfig(), refuse, {"TYPESAFE_API_KEY": "  "}).evaluate(
        STATE
    ).failure is JudgeFailure.KEY_MISSING


def test_the_absent_extra_is_distinct_from_a_schema_failure(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("typesafe"):
            raise ImportError(f"No module named {name!r}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    verdict = TypeSafeJudge(JudgeConfig(), environ={"TYPESAFE_API_KEY": SECRET}).evaluate(STATE)

    assert verdict.failure is JudgeFailure.SDK_MISSING
    assert verdict.failure is not JudgeFailure.INVALID_ANSWER
    assert verdict.answers is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_a_service_error_is_reported_without_its_body(status):
    client = FakeClient(error=FakeError(status))
    verdict = judge(client).evaluate(STATE)

    assert verdict.failure is JudgeFailure.SERVICE_ERROR
    assert verdict.answers is None
    assert client.closed == 1
    assert SECRET not in repr(verdict)
    assert SECRET not in json.dumps(asdict(verdict))
    assert str(status) not in json.dumps(asdict(verdict))


def test_a_slow_request_is_cancelled_at_the_configured_deadline():
    client = FakeClient(CASES["valid"], delay=30.0)
    verdict = judge(client, JudgeConfig(timeout_seconds=0.05)).evaluate(STATE)

    assert verdict.failure is JudgeFailure.TIMEOUT
    assert client.cancelled is True
    assert client.closed == 1


def test_a_context_managed_client_is_entered_and_closed():
    client = FakeManagedClient(CASES["valid"])
    verdict = judge(client).evaluate(STATE)

    assert verdict.answers.route is JudgeRoute.CLAUDE
    assert (client.entered, client.closed) == (1, 1)


def test_a_verdict_is_either_answers_or_a_failure():
    with pytest.raises(ValueError):
        JudgeVerdict()
    with pytest.raises(ValueError):
        JudgeVerdict(
            answers=judge(FakeClient(CASES["valid"])).evaluate(STATE).answers,
            failure=JudgeFailure.TIMEOUT,
        )
