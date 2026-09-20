"""Seat resolution and expanded routing stay offline and preserve host policy."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ortus.core import judge_routing as routing
from ortus.core.config import Config, load_config
from ortus.core.judge import (
    JudgeAnswers, JudgeConfig, JudgeRoute, JudgeState, parse_judge_config,
)
from ortus.core.judge_log import DecisionEvent, write_decision
from ortus.core.judge_packs import CRITERIA_VERSION, criteria_hash
from ortus.core.judge_policy import decide_pre_turn
from ortus.core.judge_state import pack_state
from ortus.core.judge_typesafe import JudgeVerdict, build_questions
from ortus.core.codegraph import CodeGraphMode, CodeGraphProbe
from ortus.core.local_backend import LocalServerError
from ortus.core.profiles import ProfileError


def config_table():
    return {
        "seat": "alpha", "route_confidence": 0.6,
        "packs": {"careful": {"route_confidence": 0.9, "include_issue_text": True,
            "question_criteria": {"needs_human": {"true": ["Needs approval"], "false": ["Approved"]}}}},
        "seats": {"alpha": {"enabled": True, "pack": "careful", "route_confidence": 0.95},
                  "beta": {"enabled": False}},
    }


def parse(table, **kwargs):
    kwargs.setdefault("environ", {})
    return parse_judge_config(Config({"judge": table}), **kwargs)


def test_layers_pack_seat_environment_cli_precedence(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "123"
    home.mkdir()
    repo.mkdir()
    (home / ".ortusrc").write_text('''[judge]
seat="alpha"
route_confidence=0.5
[judge.packs.careful]
route_confidence=0.7
[judge.seats.alpha]
enabled=true
pack="careful"
[judge.seats.beta]
enabled=false
''')
    (repo / ".ortusrc").write_text('''[judge]
route_confidence=0.6
[judge.packs.careful]
route_confidence=0.8
[judge.seats.alpha]
route_confidence=0.9
''')
    cfg = load_config(repo=repo, home=home)
    assert parse_judge_config(cfg, environ={}).route_confidence == 0.9
    beta = parse_judge_config(cfg, environ={"ORTUS_JUDGE_SEAT": "beta"})
    assert beta.seat == "beta" and not beta.enabled and beta.route_confidence == 0.6
    alpha = parse_judge_config(cfg, judge_seat="alpha", judge=False,
        environ={"ORTUS_JUDGE_SEAT": "beta", "ORTUS_JUDGE_ENABLED": "true", "ORTUS_JUDGE_MODEL": "jev-1.14.0"})
    assert alpha.seat == "alpha" and not alpha.enabled and alpha.model == "jev-1.14.0"
    assert cfg.values["judge"]["seats"]["alpha"]["enabled"] is True


def test_pack_isolation_requests_and_metadata_default():
    table = config_table()
    original = deepcopy(table)
    alpha = parse(table)
    beta = parse(table, judge_seat="beta")
    assert alpha.route_confidence == 0.95 and beta.route_confidence == 0.6
    assert alpha.enabled and not beta.enabled
    assert not beta.include_issue_text and not beta.question_criteria
    request = build_questions(alpha, JudgeState("sample"))
    assert request["needs_human"]["criteria"]["true"] == ["Needs approval"]
    request["needs_human"]["criteria"]["true"].append("changed")
    assert table == original
    assert alpha.question_criteria["needs_human"]["true"] == ["Needs approval"]
    assert not alpha.allows_issue_text(("judge-private",))
    assert parse_judge_config(Config(), environ={}) == JudgeConfig()


@pytest.mark.parametrize("alias", ["123", "../alpha", "", "a" * 65, "a/b"])
def test_invalid_aliases_fail_even_when_not_selected(alias):
    table = config_table()
    table["seats"][alias] = {}
    with pytest.raises(ProfileError):
        parse(table)


@pytest.mark.parametrize("field,value", [
    ("endpoint", "https://example.invalid"), ("model", "jev-1.14.0"),
    ("enabled", True), ("pre_tool", False), ("semantic_readiness", True),
    ("command", "echo unsafe"), ("route_confidence", float("nan")),
    ("routes", []), ("routes", ["grok", "grok"]), ("routes", ["local"]),
    ("question_criteria", {"unknown": {}}),
    ("question_criteria", {"route": {"grok": {"what": "incomplete"}}}),
    ("question_criteria", {"action_risk": [{"level": "safe", "what": "anything"}]}),
])
def test_unselected_invalid_pack_fails_closed(field, value):
    table = config_table()
    table["packs"]["unused"] = {field: value}
    with pytest.raises(ProfileError):
        parse(table)


def test_missing_references_and_unknown_seat_fields():
    table = config_table()
    with pytest.raises(ProfileError, match="missing"):
        parse(table, judge_seat="missing")
    table["seats"]["beta"]["pack"] = "missing"
    with pytest.raises(ProfileError, match="missing"):
        parse(table)
    table["seats"]["beta"] = {"endpoint": "ignored"}
    with pytest.raises(ProfileError):
        parse(table)


def test_hash_is_canonical_and_tracks_policy_and_criteria():
    config = parse(config_table())
    questions = build_questions(config, JudgeState("sample"))
    digest = criteria_hash(config, questions)
    assert len(digest) == 64
    assert digest == criteria_hash(config, dict(reversed(list(questions.items()))))
    assert digest != criteria_hash(replace(config, route_confidence=0.99), questions)
    changed = deepcopy(questions)
    changed["needs_human"]["criteria"]["true"] = ["Different condition"]
    assert digest != criteria_hash(config, changed)
    assert digest == criteria_hash(replace(config, seat="beta"), questions)


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(routing.shutil, "which", lambda name, **kw: f"/bin/{name}")
    monkeypatch.setattr(routing, "resolve_opencode_binary", lambda: Path("/bin/opencode"))
    monkeypatch.setattr(routing, "check_hooks_enabled", Mock())
    probe_models = Mock(return_value=("served",))
    monkeypatch.setattr(routing, "probe_models", probe_models)
    runner = Mock(extra_env={})
    factory = Mock(return_value=runner)
    monkeypatch.setattr(routing, "make_runner", factory)
    adapter = Mock()
    adapter.probe.return_value = CodeGraphProbe(CodeGraphMode.OFF, False, False, False, "off")
    config = Config({"local": {"model": "served"}, "profiles": {
        "local": {"implement": {"model": "local-profile"}},
        "opencode": {"implement": {"model": "opencode-profile"}},
    }})
    judge = JudgeConfig(enabled=True, routes=tuple(JudgeRoute))
    return config, judge, factory, adapter, probe_models


@pytest.mark.parametrize("baseline", ["grok", "opencode", "local"])
def test_new_routes_prepare_and_preserve_baseline_profiles(prepared, tmp_path, baseline):
    config, judge, factory, adapter, probe = prepared
    plan = routing.plan_routes(config, judge, baseline,
        overrides=routing.RouteOverrides(requested_backend=baseline), environ={})
    route = JudgeRoute("opencode" if baseline == "local" else baseline)
    assert plan.available_workers == (route,)
    bundle = routing.prepare_route(plan, route, repo=tmp_path, config=config,
        codegraph_mode=CodeGraphMode.OFF, adapter=adapter)
    assert bundle.execution_backend == baseline
    assert bundle.implement_profile.backend == baseline
    if baseline != "grok":
        assert bundle.implement_profile.model == f"{baseline}-profile"
        probe.assert_called_once()
    else:
        probe.assert_not_called()
    factory.assert_called_once_with(baseline, repo=tmp_path)
    adapter.probe.assert_called_once_with(tmp_path, CodeGraphMode.OFF, backend=baseline)
    factory.return_value.run.assert_not_called()


def test_unconfigured_opencode_and_missing_grok_are_not_offered(prepared, monkeypatch):
    _, judge, _, _, _ = prepared
    monkeypatch.setattr(routing.shutil, "which", lambda name, **kw: None if name == "grok" else "/bin/worker")
    plan = routing.plan_routes(Config(), judge, "claude", environ={})
    assert plan.available_workers == (JudgeRoute.CLAUDE, JudgeRoute.CODEX)


def test_dead_local_server_stops_before_runner_construction(prepared, tmp_path):
    config, judge, factory, adapter, probe = prepared
    plan = routing.plan_routes(config, judge, "local", environ={})
    probe.side_effect = LocalServerError("unreachable", "offline", "start server")
    with pytest.raises(routing.RoutePreparationError):
        routing.prepare_route(plan, JudgeRoute.OPENCODE, repo=tmp_path, config=config,
            codegraph_mode=CodeGraphMode.OFF, adapter=adapter)
    factory.assert_not_called()


def test_pre_tool_keeps_claude_only_and_rejects_other_baselines(prepared):
    config, judge, *_ = prepared
    judge = replace(judge, pre_tool=True)
    plan = routing.plan_routes(config, judge, "claude", environ={})
    assert plan.available_workers == (JudgeRoute.CLAUDE,)
    for baseline in ("codex", "grok", "opencode", "local"):
        with pytest.raises(routing.RoutePreparationError, match="only Claude"):
            routing.plan_routes(config, judge, baseline, environ={})


@pytest.mark.parametrize("route", [JudgeRoute.GROK, JudgeRoute.OPENCODE])
def test_request_policy_and_event_accept_prepared_new_routes(tmp_path, route):
    config = parse({"enabled": True, "routes": [route.value, "human"]})
    state = pack_state({"id": "sample"}, config, backends_available=(route,), environ={}).state
    questions = build_questions(config, state)
    assert set(questions["route"]["criteria"]) == {route.value, "human"}
    answers = JudgeAnswers(route, 0.99, 0.01, 0.99, 0, 0.99)
    decision = decide_pre_turn(config, state, JudgeVerdict(answers=answers), baseline_backend=route)
    assert decision.backend == route
    write_decision(tmp_path, config, DecisionEvent(run_id=uuid4(), seat=config.seat,
        issue_id=state.issue_id, phase=state.phase, answers=answers, decision=decision,
        model=config.model, criteria_version=CRITERIA_VERSION,
        criteria_hash=criteria_hash(config, questions), latency_ms=1))
    records = list(tmp_path.rglob("jev-decisions.jsonl"))
    event = json.loads(records[0].read_text())
    assert event["backend"] == route.value
    assert event["criteria_version"] == CRITERIA_VERSION
    assert "questions" not in event


def test_cli_exposes_seat_override():
    from ortus.cli import app
    result = CliRunner().invoke(app, ["grind", "--help"])
    assert result.exit_code == 0
    assert "--judge-seat" in result.stdout


def test_resolved_hook_config_roundtrips_without_registry():
    config = parse(config_table())
    assert parse(asdict(config)) == config


def test_request_metadata_matches_event_hash_and_byte_budget():
    from ortus.core.judge_typesafe import _transport_state
    config = parse(config_table())
    state = pack_state({"id": "sample", "title": "reviewed text"}, config, environ={}).state
    payload = _transport_state(state, config)
    assert payload["criteria_version"] == CRITERIA_VERSION
    assert payload["criteria_hash"] == criteria_hash(config, build_questions(config, state))
    assert len(json.dumps(payload).encode()) <= config.total_bytes_cap
    with pytest.raises(ValueError, match="budget"):
        _transport_state(state, replace(config, total_bytes_cap=1))


@pytest.mark.parametrize("route", ["grok", "opencode"])
def test_provider_receives_resolved_questions_and_matching_identifiers(route):
    from ortus.core.judge_typesafe import TypeSafeJudge
    table = config_table()
    table["packs"]["careful"]["routes"] = [route, "human"]
    config = parse(table)
    state = pack_state({"id": "sample"}, config, environ={}).state
    calls = []

    class Client:
        async def system_one(self, payload, questions, **kwargs):
            calls.append((payload, questions))
            return {"model": config.model, "answers": {
                "route": {"type": "choice", "choice": route, "confidence": 0.99},
                "needs_human": {"type": "noul", "noul": 0.01},
                "action_risk": {"type": "score", "score": 0, "confidence": 0.99},
            }}

    verdict = TypeSafeJudge(config, lambda _: Client(), {"TYPESAFE_API_KEY": "fixture"}).evaluate(state)
    assert verdict.answers.route.value == route
    payload, questions = calls[0]
    assert questions["needs_human"]["criteria"]["true"] == ["Needs approval"]
    assert payload["criteria_hash"] == criteria_hash(config, questions)
    assert payload["criteria_version"] == CRITERIA_VERSION


def test_small_state_budget_includes_identifiers():
    config = JudgeConfig(total_bytes_cap=400)
    packed = pack_state({"id": "sample", "labels": [f"label-{i}" for i in range(2000)]}, config, environ={})
    assert packed.state.labels == ()
    assert len(packed.to_json().encode()) <= 400
    assert len(packed.state.criteria_hash) == 64
