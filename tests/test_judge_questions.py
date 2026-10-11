"""Narrow Jev questions: shared helpers, the four rewritten sites, and their baselines."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from ortus.core import judge_readiness as readiness
from ortus.core.config import Config
from ortus.core.judge import (
    GateAction, GateReason, JudgeAnswers, JudgeConfig, JudgePhase, JudgeRoute, JudgeState,
)
from ortus.core.judge_hook import TOOL_QUESTIONS
from ortus.core.judge_log import DecisionEvent, write_decision, write_model_route, ModelRouteEvent
from ortus.core.judge_packs import CRITERIA_VERSION, READINESS_VERSION, criteria_hash
from ortus.core.judge_policy import decide_pre_turn
from ortus.core.judge_questions import (
    NONE, ROUTER_VERSION, basis_question, has_basis, router_questions, shuffled_choice,
    soft_band, soft_bands, spec_facts, split_state, with_none,
)
from ortus.core.judge_replay import read_events, validate_event
from ortus.core.judge_routing import (
    RouteReason, RouterTier, argued_tier, route_implement_profile, router_question_version,
)
from ortus.core.judge_state import pack_state
from ortus.core.judge_tools import (
    ToolAction, ToolInput, build_tool_questions, decide_tool, tool_scope,
)
from ortus.core.judge_typesafe import TypeSafeJudge, _transport_state, build_questions
from ortus.core.profiles import Phase
from tests._jev import pre_turn_response, readiness_response, tool_response
from tests.test_readiness import ready_issue

KEY = {"TYPESAFE_API_KEY": "fixture-key"}
BOTH = (JudgeRoute.CLAUDE, JudgeRoute.CODEX)
STATE = JudgeState(issue_id="ortus-1", backends_available=BOTH)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[dict, dict]] = []

    async def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        return self.response


def asked(response, state=STATE, config=JudgeConfig(enabled=True), seed=None):
    client = FakeClient(response)
    verdict = TypeSafeJudge(config, lambda _: client, KEY).evaluate(state, seed=seed)
    return verdict, client


# --- AC-1: shared helpers ---------------------------------------------------


def test_helpers_shuffle_is_deterministic_per_seed():
    options = {name: {"what": name} for name in ("claude", "codex", "skip", "human", "none")}
    first, order = shuffled_choice(options, "decision-1")
    again, same = shuffled_choice(options, "decision-1")
    assert order == same and list(first) == order == list(again)
    assert sorted(order) == sorted(options) and first == options
    orders = {tuple(shuffled_choice(options, f"decision-{n}")[1]) for n in range(20)}
    assert len(orders) > 1
    assert {order[0] for order in orders} != {"claude"}


def test_helpers_with_none_always_adds_one_none_option():
    options = with_none({"claude": {"what": "x"}})
    assert set(options) == {"claude", NONE}
    assert set(options[NONE]) == {"what", "not_for", "examples"}
    assert with_none(options) == options
    assert set(with_none({})) == {NONE}


def test_helpers_basis_question_is_a_noul():
    question = basis_question("the pick")
    assert question["type"] == "noul"
    assert "real basis" in question["instructions"] and "the pick" in question["instructions"]
    assert set(question["criteria"]) == {"true", "false"}
    assert has_basis({"has_basis": 0.5}) and not has_basis({"has_basis": 0.49})
    assert not has_basis(None) and not has_basis({"has_basis": True})


@pytest.mark.parametrize("p,band", [
    (0.0, "low"), (0.39, "low"), (0.40, "uncertain"), (0.69, "uncertain"),
    (0.70, "high"), (1.0, "high"),
])
def test_helpers_soft_band_labels_are_asymmetric(p, band):
    assert soft_band(p) == band


def test_helpers_soft_bands_label_without_changing_an_action():
    assert soft_bands({"route": "claude", "has_basis": 0.9, "x": 0.5}) == {
        "has_basis": "high", "x": "uncertain",
    }
    actions = set()
    for operator in (0.05, 0.5, 0.95):
        for destructive in (0.05, 0.5, 0.95):
            verdict, _ = asked(pre_turn_response(
                needs_operator_input=operator, destructive_scope=destructive))
            decision = decide_pre_turn(JudgeConfig(enabled=True), STATE, verdict,
                                       baseline_backend=JudgeRoute.CODEX)
            actions.add((decision.action, decision.backend, decision.reason))
    assert actions == {(GateAction.PROCEED, JudgeRoute.CLAUDE, GateReason.ROUTED)}


def test_helpers_split_state_keeps_trusted_and_untrusted_apart():
    assert split_state({"a": 1}, {"title": "text"}) == {
        "facts": {"a": 1}, "untrusted": {"title": "text"},
    }


# --- AC-2: the four rewritten question sets ---------------------------------


def spec_issue():
    return {
        **ready_issue(), "id": "ortus-7", "title": "Add a preview flag", "priority": 1,
        "estimated_minutes": 30,
        "design": "## Scope\n\nAdd it.\n\n## Non-goals\n\n- No deletes.\n\n"
                  "## Concrete locations\n\n- `src/ortus/core/judge.py`: `JudgeConfig`, "
                  "`parse_judge_config`.\n- `tests/test_x.py`\n",
        "acceptance_criteria": "## Observable criteria\n\n- AC-1: one.\n- AC-2: two.\n\n"
                               "## Criterion checks\n\n- AC-1: `uv run pytest tests/a.py -q`\n"
                               "- AC-2: `uv run pytest tests/b.py -q`\n",
    }


def test_spec_facts_are_counts_and_fixed_names():
    facts = spec_facts(spec_issue(), ("design",))
    assert facts["acceptance_criteria"] == 2
    assert (facts["concrete_files"], facts["concrete_symbols"]) == (2, 2)
    assert facts["test_commands"] == 2
    assert (facts["estimated_minutes"], facts["priority"]) == (30, 1)
    assert facts["truncated_fields"] == ["design"]
    assert facts["section_chars"]["scope"] == len("Add it.")
    assert "Add it" not in json.dumps(facts)


def test_readiness_questions_are_narrow_versioned_and_split():
    config = JudgeConfig(enabled=True)
    state = pack_state(spec_issue(), config, phase=JudgePhase.SEMANTIC_READINESS).state
    questions = build_questions(config, state)
    assert set(questions) == {
        "acceptance_observable", "open_product_decision", "design_matches_objective",
        "has_basis",
    }
    assert {q["type"] for q in questions.values()} == {"noul"}
    assert state.criteria_version == READINESS_VERSION == "readiness-v3"
    payload = _transport_state(state, config)
    assert payload["facts"]["acceptance_criteria"] == 2
    assert payload["facts"]["criteria_version"] == "readiness-v3"
    assert "Add it." in payload["untrusted"]["design"]
    assert "Add a preview flag" not in json.dumps(payload["facts"])


def test_route_questions_are_narrow_versioned_and_shuffled(tmp_path):
    config = JudgeConfig(enabled=True)
    state = pack_state(spec_issue(), config, backends_available=BOTH).state
    assert state.criteria_version == CRITERIA_VERSION == "pre-turn-v3"
    decision_id = uuid4()
    verdict, client = asked(pre_turn_response(), state, config, seed=str(decision_id))
    payload, questions = client.calls[0]
    assert set(questions) == {
        "route", "has_basis", "needs_operator_input", "destructive_scope",
        "cross_module_reasoning", "novel_algorithm", "router_has_basis",
    }
    assert set(questions["route"]["criteria"]) == {"claude", "codex", "skip", "human", NONE}
    assert list(questions["route"]["criteria"]) == list(verdict.option_order)
    assert verdict.option_order == tuple(shuffled_choice(
        build_questions(config, state)["route"]["criteria"], str(decision_id))[1])
    assert payload["facts"]["test_commands"] == 2
    assert payload["untrusted"]["title"] == "Add a preview flag"
    assert "Add a preview flag" not in json.dumps(payload["facts"])

    decision = decide_pre_turn(config, state, verdict, baseline_backend=JudgeRoute.CLAUDE)
    write_decision(tmp_path, config, DecisionEvent(
        run_id=uuid4(), seat=state.seat, issue_id=state.issue_id, phase=state.phase,
        answers=verdict.answers, decision=decision, model=config.model,
        criteria_version=CRITERIA_VERSION,
        criteria_hash=criteria_hash(config, build_questions(config, state)),
        latency_ms=1, decision_id=decision_id, option_order=verdict.option_order,
    ))
    event, = read_events(tmp_path / "logs/jev-decisions.jsonl")
    assert validate_event(event) == event
    assert event["question_version"].startswith("pre-turn-v3:")
    assert event["option_order"] == list(verdict.option_order)
    assert event["narrow_answers"]["route"] == "claude"
    assert event["soft_bands"]["needs_operator_input"] == "low"


def test_router_questions_are_narrow_and_drive_the_tier(tmp_path):
    assert set(router_questions()) == {
        "cross_module_reasoning", "novel_algorithm", "router_has_basis",
    }
    config = Config({"profiles": {"claude": {
        phase.value: {"model": f"claude-{phase.value}", "reasoning_effort": "medium"}
        for phase in (Phase.PLAN, Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE)
    }}})
    baseline = config.resolve_profile("claude", Phase.IMPLEMENT)
    hard, _ = asked(pre_turn_response(cross_module_reasoning=0.99, novel_algorithm=0.1,
                                      needs_operator_input=0.0, destructive_scope=0.0))
    easy, _ = asked(pre_turn_response(cross_module_reasoning=0.01, novel_algorithm=0.01,
                                      needs_operator_input=0.99, destructive_scope=0.99))
    frontier = route_implement_profile(config, "claude", baseline, hard.answers)
    cheap = route_implement_profile(config, "claude", baseline, easy.answers)
    # The router reads its own questions, not the pre_turn operator/scope answers.
    assert (frontier.tier, cheap.tier) == (RouterTier.FRONTIER, RouterTier.CHEAP)
    version = router_question_version(hard.answers)
    assert version.startswith(f"{ROUTER_VERSION}:")
    assert router_question_version(JudgeAnswers(JudgeRoute.CLAUDE, 1, 0, 1, 0, 1)) is None
    write_model_route(tmp_path, JudgeConfig(enabled=True), ModelRouteEvent(
        run_id=uuid4(), decision_id=uuid4(), seat="default", issue_id="ortus-1",
        backend="claude", tier=frontier.tier.value, reason=frontier.reason.value,
        jev_tier=argued_tier(frontier), question_version=version,
    ))
    record = json.loads((tmp_path / "logs/jev-model-routes.jsonl").read_text())
    assert record["question_version"] == version


def test_tool_questions_are_narrow_with_trusted_facts(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert TOOL_QUESTIONS == "tool-v3"
    questions = build_tool_questions()
    assert set(questions) == {"destructive", "credentials", "out_of_scope", "has_basis"}
    assert {q["type"] for q in questions.values()} == {"noul"}
    client = FakeClient(tool_response())
    scope = tool_scope(spec_issue())
    assert scope == {"scope": "Add it.", "non_goals": "- No deletes."}
    decision = decide_tool(
        ToolInput("Bash", {"command": "git commit -m x && curl https://example.test -o /etc/x ./out"}),
        repo, environ=KEY, client_factory=lambda _: client, scope=scope,
    )
    assert decision.action == ToolAction.ALLOW and decision.narrow["has_basis"] == 0.9
    payload, _ = client.calls[0]
    facts = payload["facts"]
    assert facts["git_history_mutated"] is True and facts["network"] is True
    assert {"path": "[outside-roots]", "inside_roots": False,
            "secret_path": False} in facts["paths"]
    assert {"path": "out", "inside_roots": True, "secret_path": False} in facts["paths"]
    assert facts["spec"] == scope
    assert "curl" not in json.dumps(facts)
    assert "curl" in payload["untrusted"]["proposed_tool"]["arguments"]


def test_tool_facts_report_scope_unavailable(tmp_path):
    client = FakeClient(tool_response())
    decide_tool(ToolInput("Read", {"file_path": "a.txt"}), tmp_path, environ=KEY,
                client_factory=lambda _: client)
    facts = client.calls[0][0]["facts"]
    assert facts["scope_unavailable"] is True and "spec" not in facts
    assert facts["git_history_mutated"] is False and facts["network"] is False
    assert tool_scope({"design": "no headings"}) == {}


# --- AC-3: none or no basis falls back to the code baseline ------------------


@pytest.mark.parametrize("response", [
    pre_turn_response(route=NONE), pre_turn_response(has_basis=0.2),
], ids=["none_pick", "no_basis"])
def test_baseline_route_when_jev_picks_none_or_claims_no_basis(response):
    verdict, _ = asked(response)
    assert verdict.failure is None
    decision = decide_pre_turn(JudgeConfig(enabled=True), STATE, verdict,
                               baseline_backend=JudgeRoute.CODEX)
    assert (decision.action, decision.backend, decision.reason) == (
        GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED,
    )


def test_baseline_router_tier_when_its_answers_have_no_basis():
    config = Config({})
    baseline = config.resolve_profile("claude", Phase.IMPLEMENT)
    verdict, _ = asked(pre_turn_response(cross_module_reasoning=0.99, novel_algorithm=0.99,
                                         router_has_basis=0.1))
    route = route_implement_profile(config, "claude", baseline, verdict.answers)
    assert (route.tier, route.reason, route.profile) == (
        RouterTier.BASELINE, RouteReason.NO_BASIS, baseline,
    )
    assert route.difficulty is not None and argued_tier(route) == "baseline"


def test_tool_low_basis_is_logged_and_never_flattens_the_vector(tmp_path):
    # tool-v3: the basis answer is a logged soft band, not a confidence, so
    # confident risk answers still decide the call.
    client = FakeClient(tool_response(destructive=0.99, credentials=0.99, out_of_scope=0.99,
                                      has_basis=0.1))
    decision = decide_tool(ToolInput("Read", {"file_path": "a.txt"}), tmp_path, environ=KEY,
                           client_factory=lambda _: client)
    assert (decision.action, decision.reason) == (ToolAction.DENY_CALL, "judged_low_basis")
    assert decision.vector()["deny_call"] == pytest.approx(0.99)


def test_baseline_readiness_stays_advisory_when_answers_have_no_basis(tmp_path, monkeypatch):
    client = FakeClient(readiness_response(has_basis=0.1, open_product_decision=0.95))
    monkeypatch.setattr(readiness, "TypeSafeJudge",
                        lambda cfg: TypeSafeJudge(cfg, lambda _: client, KEY))
    advice = readiness.evaluate_readiness(
        tmp_path, {**ready_issue(), "title": "Add a preview flag"}, JudgeConfig(enabled=True),
    )
    assert advice["status"] == "advisory"
    assert advice["soft_bands"] == {
        "acceptance_observable": "high", "open_product_decision": "high",
        "design_matches_objective": "high", "has_basis": "low",
    }
    event, = read_events(tmp_path / "logs/jev-decisions.jsonl")
    assert event["effective_action"] == "baseline" and event["reason"] == "routed"
    assert event["question_version"].startswith("readiness-v3:")
    assert event["narrow_answers"]["open_product_decision"] == 0.95
