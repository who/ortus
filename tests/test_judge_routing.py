"""Route discovery and preparation never launch a worker or acquire a claim."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from ortus.core import judge_routing as routing
from ortus.core.agent import CodexRunner
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import (
    CodeGraphCapability,
    CodeGraphMode,
    CodeGraphProbe,
    CodeGraphUnavailable,
)
from ortus.core.config import Config
from ortus.core.hooks import HookConflictError
from ortus.core.judge import JudgeConfig, JudgeRoute
from ortus.core.profiles import Phase


@pytest.fixture
def setup_routes(monkeypatch):
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)
    monkeypatch.setattr(routing.shutil, "which", lambda name, **kw: f"/bin/{name}")
    hooks = Mock()
    monkeypatch.setattr(routing, "check_hooks_enabled", hooks)
    # An accidental launch is a test failure, including through inherited methods.
    monkeypatch.setattr(
        ClaudeRunner, "run", Mock(side_effect=AssertionError("worker launched"))
    )
    config = Config(
        {
            "profiles": {
                backend: {
                    phase.value: {
                        "model": f"{backend}-{phase.value}",
                        "reasoning_effort": "high",
                    }
                    for phase in (Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE)
                }
                for backend in ("claude", "codex")
            }
        }
    )
    probe = CodeGraphProbe(
        CodeGraphMode.REQUIRED,
        True,
        True,
        True,
        capability=CodeGraphCapability("/bin/codegraph"),
    )
    adapter = Mock()
    adapter.probe.return_value = probe
    return config, hooks, adapter


def prepare(config, adapter, tmp_path, plan, backend, **kwargs):
    return routing.prepare_route(
        plan,
        backend,
        repo=tmp_path,
        config=config,
        codegraph_mode=CodeGraphMode.REQUIRED,
        adapter=adapter,
        **kwargs,
    )


@pytest.mark.parametrize(
    "baseline,selected", [("claude", "codex"), ("codex", "claude")]
)
def test_switch_uses_selected_runner_profiles_prompt_and_preflights(
    setup_routes, tmp_path, baseline, selected
):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, JudgeConfig(enabled=True), baseline)
    bundle = prepare(config, adapter, tmp_path, plan, JudgeRoute(selected))
    assert type(bundle.runner) is (CodexRunner if selected == "codex" else ClaudeRunner)
    assert bundle.runner.claude_binary == f"/bin/{selected}"
    for profile, phase in (
        (bundle.implement_profile, "implement"),
        (bundle.verify_profile, "verify"),
        (bundle.finalize_profile, "finalize"),
    ):
        assert profile.backend == selected
        assert profile.model == f"{selected}-{phase}"
        assert profile.reasoning_effort == "high"
    adapter.probe.assert_called_once_with(
        tmp_path, CodeGraphMode.REQUIRED, backend=selected
    )
    if selected == "claude":
        hooks.assert_called_once_with(tmp_path)
    else:
        hooks.assert_not_called()
        assert bundle.runner.codegraph == adapter.probe.return_value.capability
    prompt = bundle.compose_prompt("Do the bound task")
    assert prompt.startswith("/goal ") == (selected == "claude")
    assert prompt.count("CodeGraph phase contract") == 1
    assert "Phase: implementation; policy: required" in prompt
    argv = bundle.runner.build_argv(prompt, profile=bundle.implement_profile)
    assert f"{selected}-implement" in argv


@pytest.mark.parametrize(
    "field,value",
    [
        ("requested_backend", "claude"),
        ("implement_model", "explicit-model"),
        ("implement_reasoning_effort", "medium"),
        ("verify_model", "explicit-verifier"),
        ("verify_reasoning_effort", "low"),
    ],
)
def test_each_explicit_flag_pins_baseline(setup_routes, field, value):
    config, _, _ = setup_routes
    overrides = routing.RouteOverrides(**{field: value})
    plan = routing.plan_routes(
        config, JudgeConfig(enabled=True), "claude", overrides=overrides
    )
    assert plan.available_workers == (JudgeRoute.CLAUDE,)
    assert plan.offered_routes == (JudgeRoute.CLAUDE, JudgeRoute.SKIP, JudgeRoute.HUMAN)


def test_environment_backend_pins_but_config_backend_does_not(
    setup_routes, monkeypatch
):
    config, _, _ = setup_routes
    config.values["backend"] = "codex"
    assert (
        len(routing.plan_routes(config, JudgeConfig(), "codex").available_workers) == 2
    )
    monkeypatch.setenv("ORTUS_BACKEND", "codex")
    plan = routing.plan_routes(config, JudgeConfig(), "codex")
    assert plan.available_workers == (JudgeRoute.CODEX,)
    assert plan.offered_routes == (JudgeRoute.CODEX, JudgeRoute.SKIP, JudgeRoute.HUMAN)


def test_overrides_resolve_independently_on_baseline(setup_routes, tmp_path):
    config, _, adapter = setup_routes
    plan = routing.plan_routes(
        config,
        JudgeConfig(),
        "codex",
        overrides=routing.RouteOverrides(
            implement_model="selected-model",
            verify_reasoning_effort="low",
        ),
    )
    bundle = prepare(config, adapter, tmp_path, plan, JudgeRoute.CODEX)
    assert bundle.implement_profile.model == "selected-model"
    assert bundle.implement_profile.reasoning_effort == "high"
    assert bundle.verify_profile.model == "codex-verify"
    assert bundle.verify_profile.reasoning_effort == "low"
    assert bundle.finalize_profile.model == "codex-finalize"


@pytest.mark.parametrize("backend", ["grok", "local", "opencode", "unknown"])
def test_unsupported_baseline_fails_before_preflights(setup_routes, backend):
    config, hooks, adapter = setup_routes
    with pytest.raises(routing.RoutePreparationError, match="only claude and codex"):
        routing.plan_routes(config, JudgeConfig(enabled=True), backend)
    hooks.assert_not_called()
    adapter.probe.assert_not_called()


@pytest.mark.parametrize("fault", ["missing", "implement", "verify", "finalize"])
def test_unavailable_optional_candidate_is_not_offered(
    setup_routes, monkeypatch, fault
):
    config, _, _ = setup_routes
    if fault == "missing":
        monkeypatch.setattr(
            routing.shutil,
            "which",
            lambda name, **kw: None if name == "codex" else "/bin/claude",
        )
    else:
        config.values["profiles"]["codex"][fault]["reasoning_effort"] = "invalid"
    plan = routing.plan_routes(config, JudgeConfig(), "claude")
    assert plan.available_workers == (JudgeRoute.CLAUDE,)
    assert JudgeRoute.CODEX not in plan.offered_routes


@pytest.mark.parametrize("fault", ["missing", "profile"])
def test_invalid_baseline_is_startup_error(setup_routes, monkeypatch, fault):
    config, _, _ = setup_routes
    if fault == "missing":
        monkeypatch.setattr(routing.shutil, "which", lambda name, **kw: None)
    else:
        config.values["profiles"]["claude"]["verify"]["model"] = "invalid model"
    with pytest.raises(routing.RoutePreparationError):
        routing.plan_routes(config, JudgeConfig(), "claude")


def test_configured_routes_filter_workers_and_keep_baseline_for_failure(
    setup_routes, tmp_path
):
    config, _, adapter = setup_routes
    judge = JudgeConfig(routes=(JudgeRoute.CODEX, JudgeRoute.HUMAN))
    plan = routing.plan_routes(config, judge, "claude")
    assert plan.offered_routes == (JudgeRoute.CODEX, JudgeRoute.HUMAN)
    assert JudgeRoute.CLAUDE in plan.available_workers
    assert (
        prepare(config, adapter, tmp_path, plan, JudgeRoute.CLAUDE).backend
        == JudgeRoute.CLAUDE
    )
    only_baseline = routing.plan_routes(
        config, JudgeConfig(routes=(JudgeRoute.CLAUDE,)), "claude"
    )
    assert only_baseline.available_workers == (JudgeRoute.CLAUDE,)


def test_no_offered_worker_is_startup_error(setup_routes):
    config, _, _ = setup_routes
    with pytest.raises(routing.RoutePreparationError, match="at least one"):
        routing.plan_routes(
            config,
            JudgeConfig(routes=(JudgeRoute.CODEX, JudgeRoute.HUMAN)),
            "claude",
            overrides=routing.RouteOverrides(requested_backend="claude"),
        )


@pytest.mark.parametrize(
    "failure", ["hooks", "codegraph", "false_probe", "binary", "profile"]
)
def test_selected_route_failure_never_tries_another_backend(
    setup_routes, tmp_path, monkeypatch, failure
):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, JudgeConfig(), "codex")
    if failure == "hooks":
        hooks.side_effect = HookConflictError(tmp_path / "settings.json")
    elif failure == "codegraph":
        adapter.probe.side_effect = CodeGraphUnavailable("missing MCP")
    elif failure == "false_probe":
        adapter.probe.return_value = replace(
            adapter.probe.return_value, available=False
        )
    elif failure == "binary":
        monkeypatch.setattr(routing.shutil, "which", lambda *a, **kw: None)
    else:
        config.values["profiles"]["claude"]["implement"]["model"] = "bad model"
    factory = Mock(wraps=routing.make_runner)
    monkeypatch.setattr(routing, "make_runner", factory)
    with pytest.raises(routing.RoutePreparationError):
        prepare(config, adapter, tmp_path, plan, JudgeRoute.CLAUDE)
    factory.assert_not_called()
    assert all(
        call.kwargs["backend"] == "claude" for call in adapter.probe.call_args_list
    )


@pytest.mark.parametrize(
    "selected", [JudgeRoute.CODEX, JudgeRoute.HUMAN, JudgeRoute.SKIP]
)
def test_unoffered_selection_cannot_bypass_pin(setup_routes, tmp_path, selected):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(
        config,
        JudgeConfig(),
        "claude",
        overrides=routing.RouteOverrides(requested_backend="claude"),
    )
    with pytest.raises(routing.RoutePreparationError, match="unavailable"):
        prepare(config, adapter, tmp_path, plan, selected)
    hooks.assert_not_called()
    adapter.probe.assert_not_called()


def test_each_iteration_rechecks_and_copies_environment(setup_routes, tmp_path):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, JudgeConfig(), "claude")
    env = {
        "UV_CACHE_DIR": "/cache/uv",
        "BEADS_DIR": "/tracker",
        "BEADS_ACTOR": "claim-owner",
    }
    first = prepare(config, adapter, tmp_path, plan, JudgeRoute.CODEX, extra_env=env)
    first.runner.extra_env["LEAK"] = "no"
    second = prepare(config, adapter, tmp_path, plan, JudgeRoute.CLAUDE, extra_env=env)
    third = prepare(config, adapter, tmp_path, plan, JudgeRoute.CODEX, extra_env=env)
    assert first.runner is not third.runner
    assert second.runner.extra_env == third.runner.extra_env == env
    assert "LEAK" not in env
    assert adapter.probe.call_count == 3
    assert hooks.call_count == 1
    default = prepare(config, adapter, tmp_path, plan, JudgeRoute.CLAUDE)
    assert default.runner.extra_env["BEADS_DIR"] == str((tmp_path / ".beads").resolve())


@pytest.mark.parametrize("mode", [CodeGraphMode.OFF, CodeGraphMode.AUTO])
def test_optional_codegraph_uses_selected_probe_policy(setup_routes, tmp_path, mode):
    config, _, adapter = setup_routes
    adapter.probe.return_value = CodeGraphProbe(
        mode, False, False, False, "not available"
    )
    plan = routing.plan_routes(config, JudgeConfig(), "codex")
    bundle = routing.prepare_route(
        plan,
        JudgeRoute.CODEX,
        repo=tmp_path,
        config=config,
        codegraph_mode=mode,
        adapter=adapter,
    )
    assert bundle.runner.codegraph is None
    assert bundle.codegraph_probe.mode == mode
    assert (
        "policy is off" if mode == CodeGraphMode.OFF else "policy: auto"
    ) in bundle.phase_contract_text
