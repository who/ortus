"""Judge configuration fails locally, before any provider or claim is needed."""

from __future__ import annotations

import builtins
from dataclasses import asdict
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

from ortus.core.config import Config, load_config, tomllib
from ortus.core.judge import (
    FailureMode,
    GateAction,
    GateDecision,
    GateReason,
    JudgeAnswers,
    JudgeConfig,
    JudgeRoute,
    JudgeState,
    LowConfidence,
    parse_judge_config,
)
from ortus.core.profiles import Phase, ProfileError


def parse(table=None, **kwargs):
    return parse_judge_config(Config(values={"judge": table or {}}), environ={}, **kwargs)


def test_absent_defaults_do_not_import_sdk_or_read_credentials(tmp_path, monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert not name.startswith("typesafe")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-must-not-be-retained")
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    judge = parse_judge_config(cfg, environ={})
    assert judge == JudgeConfig()
    assert judge.enabled is False
    assert judge.model == "jev-1.13.0"
    assert judge.seat == "default"
    assert judge.failure_mode is FailureMode.OPEN
    assert judge.low_confidence is LowConfidence.HUMAN
    assert judge.timeout_seconds == 1.5
    assert (judge.route_confidence, judge.noul_confidence, judge.risk_confidence) == (0.8,) * 3
    assert (judge.human_threshold, judge.risk_threshold) == (0.8, 1.5)
    assert (judge.objective_cap, judge.acceptance_cap, judge.title_cap, judge.tool_cap,
            judge.total_bytes_cap) == (1024, 1024, 160, 512, 8192)
    assert not judge.include_log_tail
    assert not judge.include_issue_text
    assert judge.sensitive_paths == ()
    assert "secret-must-not-be-retained" not in repr(judge)
    assert cfg.get("backend") == "claude"
    assert cfg.get("verification") == "full"
    assert "judge" not in cfg.values


def test_layers_and_environment_and_flag_precedence(tmp_path):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    (home / ".ortusrc").write_text(
        '[judge]\nenabled=true\ntimeout_seconds=2.5\nmodel="jev-1.12.0"\n'
        'sensitive_paths=["private/**"]\n'
        '[profiles.codex.implement]\nmodel="worker-model"\n'
    )
    (repo / ".ortusrc").write_text(
        '[judge]\nenabled=false\nmodel="jev-1.13.0"\nroutes=["codex","human"]\n'
    )
    cfg = load_config(repo=repo, home=home)
    result = parse_judge_config(cfg, environ={})
    assert not result.enabled
    assert result.model == "jev-1.13.0"
    assert result.timeout_seconds == 2.5
    assert result.sensitive_paths == ("private/**",)
    assert result.routes == (JudgeRoute.CODEX, JudgeRoute.HUMAN)
    assert cfg.resolve_profile("codex", Phase.IMPLEMENT).model == "worker-model"
    env = {"ORTUS_JUDGE_ENABLED": "true", "ORTUS_JUDGE_MODEL": "jev-1.14.0"}
    assert parse_judge_config(cfg, environ=env).enabled
    overridden = parse_judge_config(cfg, environ=env, judge=False)
    assert not overridden.enabled
    assert overridden.model == "jev-1.14.0"
    assert parse_judge_config(cfg, environ={"ORTUS_JUDGE_ENABLED": "0"}, judge=True).enabled
    assert cfg.get("judge")["model"] == "jev-1.13.0"


def test_environment_is_read_by_default(monkeypatch):
    monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "1")
    monkeypatch.setenv("ORTUS_JUDGE_MODEL", "jev-1.14.2")
    result = parse_judge_config(Config())
    assert result.enabled
    assert result.model == "jev-1.14.2"


@pytest.mark.parametrize("value,expected", [(True, True), (False, False),
    ("true", True), ("false", False), ("1", True), ("0", False)])
def test_boolean_values(value, expected):
    assert parse({"enabled": value}).enabled is expected


@pytest.mark.parametrize("value", ["yes", "TRUE", "", " true ", 1, 0, None, []])
def test_invalid_boolean_values(value):
    with pytest.raises(ProfileError, match="judge.enabled"):
        parse({"enabled": value})


@pytest.mark.parametrize("key,value", [
    ("model", "jev-latest"), ("model", "jev-1.13.x"), ("model", "jev-1.13"),
    ("model", ""), ("model", 123), ("mode", "observe"),
    ("failure_mode", "ignore"), ("low_confidence", "proceed"),
    ("seat", "123"), ("seat", ""), ("seat", "/tmp/repo"),
    ("timeout_seconds", 0), ("timeout_seconds", -1), ("timeout_seconds", "1.5"),
    ("routes", []), ("routes", ["skip", "human"]),
    ("routes", ["codex", "codex"]), ("routes", ["unknown"]),
    ("routes", "claude"), ("routes", [123]),
    ("objective_cap", -1), ("acceptance_cap", 1.5), ("title_cap", True),
    ("tool_cap", "512"), ("total_bytes_cap", 0),
    ("include_issue_text", "yes"), ("include_log_tail", 1),
    ("sensitive_paths", "private/**"), ("sensitive_paths", [""]),
    ("sensitive_paths", [1]), ("sensitive_paths", ["bad\x00path"]),
])
def test_invalid_fields(key, value):
    with pytest.raises(ProfileError, match=f"judge.{key}"):
        parse({key: value})


@pytest.mark.parametrize("key", ["route_confidence", "noul_confidence", "risk_confidence",
    "human_threshold", "risk_threshold", "timeout_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.1, True])
def test_numbers_are_finite_and_nonnegative(key, value):
    with pytest.raises(ProfileError, match=f"judge.{key}"):
        parse({key: value})


@pytest.mark.parametrize("key,maximum", [("route_confidence", 1), ("noul_confidence", 1),
    ("risk_confidence", 1), ("human_threshold", 1), ("risk_threshold", 2)])
def test_threshold_bounds(key, maximum):
    assert getattr(parse({key: 0}), key) == 0
    assert getattr(parse({key: maximum}), key) == maximum
    with pytest.raises(ProfileError, match=f"judge.{key}"):
        parse({key: maximum + 0.01})


@pytest.mark.parametrize("toml", ['judge="bad"', '[judge]\ntimeout_seconds=nan',
    '[judge]\nroutes=["human"]', '[judge]\nenabled=false\nmodel="jev-latest"',
    '[judge]\napi_key="never-store-a-key"', '[judge]\nTYPESAFE_API_KEY="secret"',
    '[judge]\nunknown=true'])
def test_load_rejects_invalid_table_even_when_disabled(tmp_path, toml):
    (tmp_path / ".ortusrc").write_text(toml)
    with pytest.raises(ProfileError, match="judge") as error:
        load_config(repo=tmp_path, home=tmp_path / "home")
    assert "never-store-a-key" not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("env", [{"ORTUS_JUDGE_ENABLED": "yes"},
    {"ORTUS_JUDGE_MODEL": "jev-latest"}])
def test_invalid_environment_is_configuration_error(env):
    with pytest.raises(ProfileError, match="judge"):
        parse_judge_config(Config(), environ=env)


def test_cli_override_wins_over_invalid_environment_enable():
    assert not parse_judge_config(Config(), judge=False,
        environ={"ORTUS_JUDGE_ENABLED": "yes"}).enabled


def test_private_label_always_suppresses_issue_text():
    config = parse({"include_issue_text": True})
    assert config.allows_issue_text(("task",))
    assert not config.allows_issue_text(("task", "judge-private"))
    assert not JudgeConfig().allows_issue_text(())


def test_decisions_have_worker_backends_only_for_proceed():
    assert GateDecision(GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED).backend == "codex"
    assert GateDecision(GateAction.HUMAN, None, GateReason.NEEDS_HUMAN).backend is None
    for action, backend in [(GateAction.PROCEED, None), (GateAction.SKIP, JudgeRoute.CODEX),
                            (GateAction.PROCEED, JudgeRoute.HUMAN)]:
        with pytest.raises(ValueError):
            GateDecision(action, backend, GateReason.ROUTED)
    answers = JudgeAnswers(JudgeRoute.CODEX, 0.9, 0.1, 0.9, 0.2, 0.9)
    assert all(isinstance(value, (str, float)) for value in asdict(answers).values())
    assert JudgeState(issue_id="example-task").seat == "default"


@pytest.mark.parametrize("backend", ["claude", "local", "opencode"])
def test_template_judge_example_is_valid_and_precedes_local(backend):
    root = Path(__file__).parents[1] / "src" / "ortus" / "templates"
    template = Environment(loader=FileSystemLoader(root)).get_template(".ortusrc.jinja")
    rendered = template.render(today="2026-09-20", ortus_version="test", prefix="test",
        project_type="python", backend=backend, codegraph="off",
        local_base_url="http://localhost:8080/v1", local_model="model")
    assert "judge" not in tomllib.loads(rendered)
    start = rendered.index("# [judge]")
    end = rendered.index("# include_issue_text", start)
    end = rendered.index("\n", end)
    example = "\n".join(line.removeprefix("# ") for line in rendered[start:end].splitlines())
    result = parse_judge_config(Config(values=tomllib.loads(example)), environ={})
    assert result == JudgeConfig()
    assert start < rendered.index("[local]")
