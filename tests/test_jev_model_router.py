"""Judge vectors pick one bead's implementation model without gating the bead.

The router is a tier selection, not a policy: no threshold in it can stop, skip
or escalate a bead, and every path that cannot map a tier falls open onto the
pinned profile. The A/B these tests guard is decided on cost per closed bead
with close rate as the guardrail, so the flag-off arm must stay byte-identical
to the pinned behaviour and every routed bead must leave a joinable record.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from ortus.core import judge_log
from ortus.core.agent import CodexRunner
from ortus.core.claude import ClaudeRunner
from ortus.core.config import DEFAULTS, Config, load_config
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgePhase,
    JudgeRoute,
)
from ortus.core.judge_log import (
    DecisionEvent, ModelRouteEvent, OutcomeEvent, OutcomeStatus, write_decision,
    write_model_route, write_outcome,
)
from ortus.core.judge_replay import validate_event
from ortus.core.judge_routing import (
    RouteOverrides, RouteReason, RouterTier, jev_router_enabled,
    route_implement_profile,
)
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict
from ortus.core.profiles import SUPPORTED_EFFORTS, Phase
from tests.test_grind_judge import gate as gate

CONFIG = JudgeConfig(enabled=True)
PHASES = (Phase.PLAN, Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE)

#: The verdict recorded in this arm's `jev_router_ab` metadata. `DEFAULTS`
#: follows this, never the other way round. It is still False because the
#: comparison has not produced data yet, not because off was preferred: an
#: unmeasured arm keeps the control, and flipping it is the whole job of the
#: bead the treatment's `measured_by` names.
MEASURED_VERDICT = False


def config_for(*backends: str, effort: str = "medium") -> Config:
    """A config that pins a distinct model per backend and phase."""
    return Config(
        {
            "profiles": {
                backend: {
                    phase.value: {
                        "model": f"{backend}-{phase.value}",
                        "reasoning_effort": effort,
                    }
                    for phase in PHASES
                }
                for backend in backends
            }
        }
    )


def answers(
    *,
    needs_human: float = 0.0,
    noul_confidence: float = 1.0,
    action_risk: float = 0.0,
    risk_confidence: float = 1.0,
) -> JudgeAnswers:
    return JudgeAnswers(
        route=JudgeRoute.CLAUDE,
        route_confidence=1.0,
        needs_human=needs_human,
        noul_confidence=noul_confidence,
        action_risk=action_risk,
        risk_confidence=risk_confidence,
    )


def route(config: Config, backend: str, verdict: JudgeAnswers | None, **kwargs):
    baseline = config.resolve_profile(
        backend,
        Phase.IMPLEMENT,
        model=kwargs.get("overrides", RouteOverrides()).implement_model,
        reasoning_effort=kwargs.get(
            "overrides", RouteOverrides()
        ).implement_reasoning_effort,
    )
    return baseline, route_implement_profile(
        config, backend, baseline, verdict, **kwargs
    )


def route_records(repo) -> list[dict]:
    path = repo / "logs" / judge_log.ROUTE_LOG_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- AC-1: the flag is off by default and off changes nothing --------------


def test_flag_off_leaves_the_pinned_implement_profile_identical():
    config = config_for("claude")
    baseline, routed = route(config, "claude", answers(needs_human=1.0), enabled=False)
    assert routed.profile == baseline
    assert routed.profile.model == "claude-implement"
    assert routed.profile.reasoning_effort == "medium"
    assert routed.tier is RouterTier.BASELINE
    assert routed.reason is RouteReason.FLAG_OFF
    assert not routed.routed
    # A bead the router never scored records no vectors at all.
    assert (routed.needs_frontier, routed.action_risk, routed.difficulty) == (
        None,
        None,
        None,
    )


def test_flag_off_is_what_an_untouched_ortusrc_resolves_to(tmp_path):
    config = load_config(repo=tmp_path, home=tmp_path)
    assert config.get("jev_model_router") is False
    assert jev_router_enabled(config, environ={}) is False
    assert jev_router_enabled(None, environ={}) is False


def test_flag_off_survives_a_value_neither_layer_can_parse():
    config = Config({"jev_model_router": "sometimes"})
    assert jev_router_enabled(config, environ={}) is False
    assert jev_router_enabled(config, environ={"ORTUS_JEV_ROUTER": "perhaps"}) is False


def test_flag_off_can_be_exported_over_an_ortusrc_that_pins_it_on():
    config = Config({"jev_model_router": True})
    assert jev_router_enabled(config, environ={}) is True
    assert jev_router_enabled(config, environ={"ORTUS_JEV_ROUTER": "0"}) is False
    assert (
        jev_router_enabled(Config({}), environ={"ORTUS_JEV_ROUTER": "yes"}) is True
    )


# --- AC-2: vectors map to model and effort, and never gate the bead --------


def test_mapping_sends_a_confidently_hard_bead_to_the_frontier_tier():
    config = config_for("claude")
    _, routed = route(config, "claude", answers(needs_human=0.9))
    assert routed.tier is RouterTier.FRONTIER
    assert routed.reason is RouteReason.MAPPED
    assert routed.profile.model == "claude-plan"
    assert routed.profile.reasoning_effort == "max"
    assert routed.profile.phase is Phase.IMPLEMENT
    assert routed.needs_frontier == pytest.approx(0.9)
    assert routed.difficulty == pytest.approx(0.9)


def test_mapping_sends_a_confidently_easy_bead_to_the_cheap_tier():
    config = config_for("claude")
    _, routed = route(config, "claude", answers())
    assert routed.tier is RouterTier.CHEAP
    assert routed.profile.model == "claude-finalize"
    assert routed.profile.reasoning_effort == "low"
    assert routed.difficulty == pytest.approx(0.0)


def test_mapping_keeps_an_unsure_judge_on_the_baseline_without_escalating():
    config = config_for("claude")
    baseline, routed = route(
        config,
        "claude",
        answers(
            needs_human=1.0, noul_confidence=0.0, action_risk=2.0, risk_confidence=0.0
        ),
    )
    # A vector with no confidence behind it carries no information, so it lands
    # on the coin flip. The bead still runs: the router owns no human path.
    assert routed.difficulty == pytest.approx(0.5)
    assert routed.tier is RouterTier.BASELINE
    assert routed.reason is RouteReason.MAPPED
    assert routed.profile == baseline


def test_mapping_reaches_the_frontier_tier_on_the_risk_vector_alone():
    config = config_for("claude")
    _, routed = route(config, "claude", answers(action_risk=2.0))
    assert routed.action_risk == pytest.approx(1.0)
    assert routed.needs_frontier == pytest.approx(0.0)
    assert routed.tier is RouterTier.FRONTIER


def test_mapping_lets_an_operator_pin_win_over_the_chosen_tier():
    config = config_for("claude")
    overrides = RouteOverrides(implement_model="operator-pin")
    baseline, routed = route(
        config, "claude", answers(needs_human=0.9), overrides=overrides
    )
    assert baseline.model == "operator-pin"
    # The tier is still recorded, and still moves the field the operator left
    # open, but it does not overwrite the pin.
    assert routed.tier is RouterTier.FRONTIER
    assert routed.profile.model == "operator-pin"
    assert routed.profile.reasoning_effort == "max"

    both = RouteOverrides(
        implement_model="operator-pin", implement_reasoning_effort="low"
    )
    baseline, routed = route(
        config, "claude", answers(needs_human=0.9), overrides=both
    )
    assert routed.tier is RouterTier.FRONTIER
    assert routed.profile == baseline
    assert (routed.profile.model, routed.profile.reasoning_effort) == (
        "operator-pin",
        "low",
    )


def test_mapping_climbs_each_backends_own_effort_ladder():
    config = config_for("claude", "codex")
    _, claude = route(config, "claude", answers(needs_human=1.0))
    _, codex = route(config, "codex", answers(needs_human=1.0))
    assert claude.profile.reasoning_effort == "max"
    assert codex.profile.reasoning_effort == "xhigh"
    assert codex.profile.model == "codex-plan"


def test_mapping_falls_open_to_the_baseline_when_the_judge_answered_nothing():
    config = config_for("claude")
    baseline, routed = route(config, "claude", None)
    assert routed.profile == baseline
    assert routed.tier is RouterTier.BASELINE
    assert routed.reason is RouteReason.JUDGE_UNAVAILABLE
    assert routed.difficulty is None


def test_mapping_falls_open_when_a_tiers_configured_model_is_invalid():
    config = Config(
        {
            "profiles": {
                "claude": {
                    Phase.IMPLEMENT.value: {"model": "claude-implement"},
                    Phase.PLAN.value: {"model": "not a model name"},
                }
            }
        }
    )
    baseline, routed = route(config, "claude", answers(needs_human=0.9))
    assert routed.profile == baseline
    assert routed.tier is RouterTier.BASELINE
    assert routed.reason is RouteReason.UNMAPPABLE_PROFILE
    # The vectors that chose the unreachable tier are still reported.
    assert routed.difficulty == pytest.approx(0.9)


def test_mapping_falls_open_when_a_backend_defines_no_known_effort_rung(monkeypatch):
    monkeypatch.setitem(SUPPORTED_EFFORTS, "grok", frozenset({"turbo"}))
    config = Config(
        {
            "profiles": {
                "grok": {
                    Phase.IMPLEMENT.value: {"model": "grok-implement"},
                    Phase.PLAN.value: {"model": "grok-plan"},
                }
            }
        }
    )
    baseline, routed = route(config, "grok", answers(needs_human=0.9))
    assert routed.profile == baseline
    assert routed.reason is RouteReason.UNMAPPABLE_PROFILE


# --- AC-3: the planner stays frontier however the workers are routed -------


def test_planner_frontier_profile_is_untouched_by_a_cheap_worker():
    config = config_for("claude")
    before = config.resolve_profile("claude", Phase.PLAN)
    _, routed = route(config, "claude", answers())
    assert routed.tier is RouterTier.CHEAP
    assert config.resolve_profile("claude", Phase.PLAN) == before
    assert before.model == "claude-plan"
    assert before.reasoning_effort == "medium"


def test_planner_frontier_model_is_the_ceiling_a_worker_can_be_routed_to():
    config = config_for("claude")
    planner = config.resolve_profile("claude", Phase.PLAN)
    cheap = config.resolve_profile("claude", Phase.FINALIZE)
    _, hard = route(config, "claude", answers(needs_human=1.0))
    _, easy = route(config, "claude", answers())
    # Neither tier invents a model name: each borrows one the operator already
    # declared for a phase, which is why no routing can move the planner.
    assert hard.profile.model == planner.model
    assert easy.profile.model == cheap.model
    assert hard.profile.phase is easy.profile.phase is Phase.IMPLEMENT


# --- AC-4: every routing decision is logged and joinable -------------------


def test_logging_records_vectors_model_effort_bead_and_backend(tmp_path, capsys):
    config = config_for("claude")
    _, routed = route(config, "claude", answers(needs_human=0.9, action_risk=1.0))
    event = ModelRouteEvent(
        run_id=uuid4(), decision_id=uuid4(), seat="product",
        issue_id="ortus-example", backend="claude", tier=routed.tier.value,
        reason=routed.reason.value, model=routed.profile.model,
        reasoning_effort=routed.profile.reasoning_effort,
        needs_frontier=routed.needs_frontier, action_risk=routed.action_risk,
        difficulty=routed.difficulty,
    )
    assert write_model_route(tmp_path, CONFIG, event) == event.decision_id
    (record,) = route_records(tmp_path)
    assert set(record) == {
        "schema_version", "event", "timestamp", "run_id", "decision_id", "seat",
        "issue_id", "backend", "tier", "reason", "model", "reasoning_effort",
        "needs_frontier", "action_risk", "difficulty", "applied",
    }
    # An enforcing launch is the applied case; a shadow seat is not.
    assert record["applied"] is True
    assert record["event"] == "model_route"
    assert record["run_id"] == str(event.run_id)
    assert record["decision_id"] == str(event.decision_id)
    assert record["issue_id"] == "ortus-example"
    assert record["backend"] == "claude"
    assert record["tier"] == "frontier"
    assert record["reason"] == "mapped"
    assert record["model"] == "claude-plan"
    assert record["reasoning_effort"] == "max"
    assert record["needs_frontier"] == pytest.approx(0.9)
    assert record["action_risk"] == pytest.approx(0.5)
    assert record["difficulty"] == pytest.approx(0.9)
    assert "tier=frontier" in capsys.readouterr().err


def test_logging_records_a_missing_vector_as_null_and_never_as_zero(tmp_path):
    config = config_for("claude")
    _, routed = route(config, "claude", None)
    write_model_route(tmp_path, CONFIG, ModelRouteEvent(
        run_id=uuid4(), decision_id=uuid4(), seat="product",
        issue_id="ortus-example", backend="claude", tier=routed.tier.value,
        reason=routed.reason.value, model=routed.profile.model,
        reasoning_effort=routed.profile.reasoning_effort,
        needs_frontier=routed.needs_frontier, action_risk=routed.action_risk,
        difficulty=routed.difficulty,
    ))
    (record,) = route_records(tmp_path)
    assert record["reason"] == "judge_unavailable"
    assert record["needs_frontier"] is None
    assert record["action_risk"] is None
    assert record["difficulty"] is None


def test_logging_leaves_the_decision_log_replay_valid(tmp_path):
    decision = DecisionEvent(
        run_id=uuid4(), seat="product", issue_id="ortus-example",
        phase=JudgePhase.PRE_TURN, answers=answers(needs_human=0.9),
        decision=GateDecision(
            GateAction.PROCEED, JudgeRoute.CLAUDE, GateReason.ROUTED
        ),
        model="jev-1.13.0", criteria_version="v1", criteria_hash="a" * 64,
        latency_ms=12.5,
    )
    write_decision(tmp_path, CONFIG, decision)
    write_model_route(tmp_path, CONFIG, ModelRouteEvent(
        run_id=decision.run_id, decision_id=decision.decision_id, seat="product",
        issue_id="ortus-example", backend="claude", tier="frontier",
        reason="mapped", model="claude-plan", reasoning_effort="max",
        needs_frontier=0.9, action_risk=0.5, difficulty=0.9,
    ))
    write_outcome(tmp_path, CONFIG, OutcomeEvent(
        decision.run_id, decision.decision_id, OutcomeStatus.CLOSED, 1250,
    ))
    decisions = [
        json.loads(line)
        for line in (tmp_path / "logs" / judge_log.LOG_NAME).read_text().splitlines()
    ]
    # The routing record lives in its own file, so the reader that rejects an
    # unknown event name still validates every line of the decision log.
    assert [event["event"] for event in decisions] == ["decision", "outcome"]
    for event in decisions:
        validate_event(event)
    (routing,) = route_records(tmp_path)
    assert routing["decision_id"] == decisions[0]["decision_id"]


def test_logging_is_skipped_while_the_judge_is_disabled(tmp_path):
    event = ModelRouteEvent(
        run_id=uuid4(), decision_id=uuid4(), seat="product",
        issue_id="ortus-example", backend="claude", tier="baseline",
        reason="flag_off",
    )
    assert write_model_route(tmp_path, JudgeConfig(), event) is None
    assert not (tmp_path / "logs" / judge_log.ROUTE_LOG_NAME).exists()


# --- AC-5: the mapped fields reach each backend's launch argv --------------


def test_argv_carries_the_mapped_model_and_effort_to_claude():
    config = config_for("claude")
    _, routed = route(config, "claude", answers(needs_human=1.0))
    argv = ClaudeRunner(claude_binary="/bin/claude").build_argv(
        "task", profile=routed.profile
    )
    assert argv[argv.index("--model") + 1] == "claude-plan"
    assert argv[argv.index("--effort") + 1] == "max"


def test_argv_carries_the_mapped_model_and_effort_to_codex():
    config = config_for("codex")
    _, routed = route(config, "codex", answers(needs_human=1.0))
    argv = CodexRunner(codex_binary="/bin/codex").build_argv(
        "task", profile=routed.profile
    )
    assert argv[argv.index("-m") + 1] == "codex-plan"
    assert "model_reasoning_effort=xhigh" in argv


def test_argv_of_a_cheap_bead_differs_from_its_pinned_baseline():
    config = config_for("claude")
    baseline, routed = route(config, "claude", answers())
    runner = ClaudeRunner(claude_binary="/bin/claude")
    pinned = runner.build_argv("task", profile=baseline)
    cheap = runner.build_argv("task", profile=routed.profile)
    assert pinned[pinned.index("--model") + 1] == "claude-implement"
    assert cheap[cheap.index("--model") + 1] == "claude-finalize"
    assert cheap[cheap.index("--effort") + 1] == "low"


# --- AC-1: the default is the measured verdict, and the kill switch wins ----


def test_default_is_the_verdict_the_ab_recorded_not_a_preference(tmp_path):
    """An untouched repo runs the arm the recorded comparison chose.

    The flag exists to be settled by measurement, so the default and the
    verdict recorded on the bead are the same fact written twice. Pinning the
    expectation here means a later edit to `DEFAULTS` has to argue with the
    numbers rather than slip past them, and an arm with no numbers yet cannot
    be flipped on by preference alone.
    """
    assert DEFAULTS["jev_model_router"] is MEASURED_VERDICT
    config = load_config(repo=tmp_path, home=tmp_path)
    assert config.get("jev_model_router") is MEASURED_VERDICT
    assert jev_router_enabled(config, environ={}) is MEASURED_VERDICT


def test_default_still_yields_to_the_per_run_kill_switch(tmp_path):
    """Whatever the default becomes, one export turns the router off."""
    config = load_config(repo=tmp_path, home=tmp_path)
    assert jev_router_enabled(config, environ={"ORTUS_JEV_ROUTER": "0"}) is False
    pinned = Config({"jev_model_router": True})
    assert jev_router_enabled(pinned, environ={"ORTUS_JEV_ROUTER": "0"}) is False


# --- AC-6: a tier boundary is not a gate ----------------------------------


def test_no_confidence_floor_parks_or_escalates_any_bead():
    """Every vector combination still returns a profile a worker can run."""
    config = config_for("claude")
    for needs_human in (0.0, 0.5, 1.0):
        for risk in (0.0, 1.0, 2.0):
            for confidence in (0.0, 0.5, 1.0):
                _, routed = route(
                    config,
                    "claude",
                    answers(
                        needs_human=needs_human,
                        noul_confidence=confidence,
                        action_risk=risk,
                        risk_confidence=confidence,
                    ),
                )
                assert routed.tier in tuple(RouterTier)
                assert routed.profile.model
                assert routed.reason is RouteReason.MAPPED


def test_no_confidence_floor_exists_in_the_routers_vocabulary():
    """The router can only name tiers, so it cannot name a human or a park."""
    assert {tier.value for tier in RouterTier} == {"cheap", "baseline", "frontier"}
    reasons = {reason.value for reason in RouteReason}
    assert not reasons & {"human", "needs_human", "skip", "park", "low_confidence"}
    # A judge that is certain a bead needs a human buys it a bigger model and
    # nothing else: the bead still runs, on the frontier tier.
    config = config_for("claude")
    _, routed = route(config, "claude", answers(needs_human=1.0))
    assert routed.tier is RouterTier.FRONTIER
    assert routed.profile.model == "claude-plan"


# --- AC-2/AC-3: shadow observes, and a judge failure changes nothing -------


def pinned_profiles() -> dict:
    return {
        "claude": {
            phase.value: {
                "model": f"claude-{phase.value}",
                "reasoning_effort": "high" if phase is Phase.IMPLEMENT else "medium",
            }
            for phase in PHASES
        }
    }


def test_shadow_logs_the_route_it_would_have_picked_without_applying_it(gate):
    """A shadow seat accumulates router evidence while running the pinned model."""
    gate.config.values["judge"].update(mode="shadow")
    gate.config.values["profiles"] = pinned_profiles()

    result = gate.invoke("--tasks", "1")

    assert result.exit_code == 0, result.output + str(result.exception)
    records = route_records(gate.repo)
    assert len(records) == 1
    assert records[0]["applied"] is False
    assert records[0]["tier"] == RouterTier.CHEAP.value
    assert records[0]["reason"] == RouteReason.MAPPED.value
    # The would-be model is recorded, which is the whole point of observing.
    assert records[0]["model"] == "claude-finalize"
    # The worker still launched on the profile the operator pinned.
    profile = gate.worker.run.call_args.kwargs["profile"]
    assert (profile.model, profile.reasoning_effort) == ("claude-implement", "high")


def test_shadow_observes_nothing_when_the_judge_is_unavailable(gate):
    """No vectors is not evidence, and an unreachable judge is not a crash."""
    gate.config.values["judge"].update(mode="shadow")
    gate.judge.evaluate.side_effect = RuntimeError("provider down")

    result = gate.invoke("--tasks", "1")

    assert result.exit_code == 0, result.output + str(result.exception)
    assert not (gate.repo / "logs" / judge_log.ROUTE_LOG_NAME).exists()
    gate.worker.run.assert_called_once()


def test_fail_open_keeps_the_pinned_profile_when_the_judge_times_out(gate):
    """A judge that answered nothing costs the bead its routing and nothing else."""
    gate.config.values["jev_model_router"] = True
    gate.config.values["profiles"] = pinned_profiles()
    gate.judge.evaluate.return_value = JudgeVerdict(failure=JudgeFailure.TIMEOUT)

    result = gate.invoke("--tasks", "1")

    assert result.exit_code == 0, result.output + str(result.exception)
    bundle = gate.bundles[JudgeRoute.CLAUDE]
    profile = bundle.runner.run.call_args.kwargs["profile"]
    assert (profile.model, profile.reasoning_effort) == ("claude-implement", "high")
    records = route_records(gate.repo)
    assert len(records) == 1
    assert records[0]["applied"] is True
    assert records[0]["tier"] == RouterTier.BASELINE.value
    assert records[0]["reason"] == RouteReason.JUDGE_UNAVAILABLE.value
    assert records[0]["difficulty"] is None
    # Falling open is not an escalation: the bead is neither parked nor flagged.
    assert "human" not in gate.bd.rows["demo-1"].get("labels", [])
    assert gate.bd.rows["demo-1"]["status"] == "closed"
