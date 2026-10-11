"""Route discovery and preparation never launch a worker or acquire a claim."""

import ast
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from ortus.core import routing
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
from ortus.core.profiles import Phase
from ortus.core.routing import WorkerRoute

SRC = Path(__file__).resolve().parents[1] / "src" / "ortus"


def _judge_imports(path: Path) -> list[str]:
    """Every `ortus.core.judge*` module a file imports, at any depth."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
            if node.module == "ortus.core":
                names = [f"ortus.core.{alias.name}" for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.extend(
            name for name in names
            if name == "ortus.core.judge" or name.startswith("ortus.core.judge_")
        )
    return found


def test_no_judge_imports_in_commands_prefix_eval_or_routing():
    paths = sorted((SRC / "commands").glob("*.py")) + [
        SRC / "core" / "prompt_prefix.py",
        SRC / "core" / "harness_eval.py",
        SRC / "core" / "routing.py",
    ]
    offenders = {
        str(path.relative_to(SRC)): imports
        for path in paths
        if (imports := _judge_imports(path))
    }
    assert offenders == {}


def test_grind_resolves_its_profiles_through_routing():
    grind = SRC / "commands" / "grind.py"
    tree = ast.parse(grind.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ortus.core.routing"
        for alias in node.names
    }
    assert {"RouteOverrides", "phase_profiles"} <= imported


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


@pytest.mark.parametrize("baseline", ["claude", "codex"])
def test_baseline_uses_its_runner_profiles_prompt_and_preflights(
    setup_routes, tmp_path, baseline
):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, baseline)
    assert plan.available_workers == (WorkerRoute(baseline),)
    bundle = prepare(config, adapter, tmp_path, plan, WorkerRoute(baseline))
    assert type(bundle.runner) is (CodexRunner if baseline == "codex" else ClaudeRunner)
    assert bundle.runner.claude_binary == f"/bin/{baseline}"
    for profile, phase in (
        (bundle.implement_profile, "implement"),
        (bundle.verify_profile, "verify"),
        (bundle.finalize_profile, "finalize"),
    ):
        assert profile.backend == baseline
        assert profile.model == f"{baseline}-{phase}"
        assert profile.reasoning_effort == "high"
    assert bundle.implement_profile == routing.baseline_implement_profile(config, plan)
    adapter.probe.assert_called_once_with(
        tmp_path, CodeGraphMode.REQUIRED, backend=baseline
    )
    if baseline == "claude":
        hooks.assert_called_once_with(tmp_path)
    else:
        hooks.assert_not_called()
        assert bundle.runner.codegraph == adapter.probe.return_value.capability
    prompt = bundle.compose_prompt("Do the task")
    assert prompt.startswith("/goal ") == (baseline == "claude")
    assert prompt.count("CodeGraph phase contract") == 1
    assert "Phase: implementation; policy: required" in prompt
    argv = bundle.runner.build_argv(prompt, profile=bundle.implement_profile)
    assert f"{baseline}-implement" in argv


def test_other_configured_backends_are_never_offered(setup_routes):
    config, _, _ = setup_routes
    plan = routing.plan_routes(config, "claude")
    assert plan.available_workers == (WorkerRoute.CLAUDE,)


def test_overrides_resolve_independently_on_baseline(setup_routes, tmp_path):
    config, _, adapter = setup_routes
    overrides = routing.RouteOverrides(
        implement_model="selected-model",
        verify_reasoning_effort="low",
    )
    plan = routing.plan_routes(config, "codex", overrides=overrides)
    bundle = prepare(config, adapter, tmp_path, plan, WorkerRoute.CODEX)
    assert bundle.implement_profile.model == "selected-model"
    assert bundle.implement_profile.reasoning_effort == "high"
    assert bundle.verify_profile.model == "codex-verify"
    assert bundle.verify_profile.reasoning_effort == "low"
    assert bundle.finalize_profile.model == "codex-finalize"
    assert routing.phase_profiles(config, "codex", overrides) == (
        bundle.implement_profile, bundle.verify_profile, bundle.finalize_profile,
    )


def test_phase_profiles_match_config_resolution(setup_routes):
    config, _, _ = setup_routes
    implement, verify, finalize = routing.phase_profiles(config, "claude")
    assert implement == config.resolve_profile("claude", Phase.IMPLEMENT)
    assert verify == config.resolve_profile("claude", Phase.VERIFY)
    assert finalize == config.resolve_profile("claude", Phase.FINALIZE)


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
def test_each_explicit_flag_reads_as_pinned(field, value):
    assert routing.RouteOverrides(**{field: value}).pinned({})
    assert not routing.RouteOverrides().pinned({})
    assert routing.RouteOverrides().pinned({"ORTUS_BACKEND": "codex"})


def test_unsupported_baseline_fails_before_preflights(setup_routes):
    config, hooks, adapter = setup_routes
    with pytest.raises(routing.RoutePreparationError, match="not a supported worker"):
        routing.plan_routes(config, "unknown")
    hooks.assert_not_called()
    adapter.probe.assert_not_called()


@pytest.mark.parametrize("fault", ["missing", "profile"])
def test_invalid_baseline_is_startup_error(setup_routes, monkeypatch, fault):
    config, _, _ = setup_routes
    if fault == "missing":
        monkeypatch.setattr(routing.shutil, "which", lambda name, **kw: None)
    else:
        config.values["profiles"]["claude"]["verify"]["model"] = "invalid model"
    with pytest.raises(routing.RoutePreparationError):
        routing.plan_routes(config, "claude")


@pytest.mark.parametrize(
    "failure", ["hooks", "codegraph", "false_probe", "binary", "profile"]
)
def test_route_failure_never_tries_another_backend(
    setup_routes, tmp_path, monkeypatch, failure
):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, "claude")
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
        prepare(config, adapter, tmp_path, plan, WorkerRoute.CLAUDE)
    factory.assert_not_called()
    assert all(
        call.kwargs["backend"] == "claude" for call in adapter.probe.call_args_list
    )


def test_unplanned_route_cannot_be_prepared(setup_routes, tmp_path):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, "claude")
    with pytest.raises(routing.RoutePreparationError, match="unavailable"):
        prepare(config, adapter, tmp_path, plan, WorkerRoute.CODEX)
    hooks.assert_not_called()
    adapter.probe.assert_not_called()


def test_each_preparation_rechecks_and_copies_environment(setup_routes, tmp_path):
    config, hooks, adapter = setup_routes
    plan = routing.plan_routes(config, "claude")
    env = {
        "UV_CACHE_DIR": "/cache/uv",
        "BEADS_DIR": "/tracker",
        "BEADS_ACTOR": "claim-owner",
    }
    first = prepare(config, adapter, tmp_path, plan, WorkerRoute.CLAUDE, extra_env=env)
    first.runner.extra_env["LEAK"] = "no"
    second = prepare(config, adapter, tmp_path, plan, WorkerRoute.CLAUDE, extra_env=env)
    assert first.runner is not second.runner
    assert second.runner.extra_env == env
    assert "LEAK" not in env
    assert adapter.probe.call_count == 2
    assert hooks.call_count == 2
    default = prepare(config, adapter, tmp_path, plan, WorkerRoute.CLAUDE)
    assert default.runner.extra_env["BEADS_DIR"] == str((tmp_path / ".beads").resolve())


@pytest.mark.parametrize("mode", [CodeGraphMode.OFF, CodeGraphMode.AUTO])
def test_optional_codegraph_uses_selected_probe_policy(setup_routes, tmp_path, mode):
    config, _, adapter = setup_routes
    adapter.probe.return_value = CodeGraphProbe(
        mode, False, False, False, "not available"
    )
    plan = routing.plan_routes(config, "codex")
    bundle = routing.prepare_route(
        plan,
        WorkerRoute.CODEX,
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
