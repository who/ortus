"""Offline MVP contracts. Replay responses are authored examples, not recordings."""

import builtins
from copy import deepcopy
import json
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.agent import CodexRunner
from ortus.core.claude import ClaudeRunner
from ortus.core.judge import JudgeRoute
from ortus.core.judge_routing import plan_routes
from ortus.core.judge_typesafe import TypeSafeJudge
from tests.test_grind_judge import gate as gate  # Reuse the isolated tracker/runner seam.


CASES = [json.loads(line) for line in (
    Path(__file__).parent / "fixtures/jev/replay.jsonl"
).read_text().splitlines()]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No installed SDK or ambient credential may turn a canned test live."""
    original = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "typesafe_sdk" or name.startswith("typesafe_sdk."):
            raise ImportError("optional SDK deliberately unavailable")
        return original(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        raise AssertionError("MVP contracts must not connect to a network")

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("ORTUS_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)


@pytest.mark.parametrize("backend", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["absent", "disabled", "env_off", "flag_off"])
def test_opt_out_matches_ungated_worker_contract(gate, monkeypatch, backend, mode):
    monkeypatch.setattr(grind_mod, "resolve_backend", lambda *a, **kw: backend)
    forbidden = Mock(side_effect=AssertionError("disabled gate was entered"))
    monkeypatch.setattr(grind_mod, "TypeSafeJudge", forbidden)
    monkeypatch.setattr(grind_mod, "plan_routes", forbidden)
    initial = deepcopy(gate.bd.rows)
    gate.config.values.pop("judge")
    runner = ClaudeRunner() if backend == "claude" else CodexRunner()

    def observe(*flags):
        result = gate.invoke("--tasks", "1", *flags)
        assert result.exit_code == 0, result.output + str(result.exception)
        gate.worker.run.assert_called_once()
        call = gate.worker.run.call_args
        prompt = call.args[0]
        assert (prompt.startswith("/goal ")) == (backend == "claude")
        assert "Bound issue contract" not in prompt
        assert gate.events() == []
        log = "\n".join(p.read_text() for p in (gate.repo / "logs").glob("grind-*"))
        assert "iters_run=1" in log and "tasks_completed=1" in log
        return (
            prompt,
            runner.build_argv(prompt, profile=call.kwargs["profile"]),
            deepcopy(gate.bd.calls),
            deepcopy(gate.bd.rows),
            dict(gate.worker.extra_env),
            call.kwargs["timeout"],
        )

    baseline = observe()
    gate.worker.run.reset_mock()
    gate.bd.rows = deepcopy(initial)
    gate.bd.calls.clear()
    for path in (gate.repo / "logs").glob("grind-*"):
        path.unlink()
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-unused-key")
    flags = []
    if mode == "disabled":
        gate.config.values["judge"] = {"enabled": False}
    elif mode == "env_off":
        gate.config.values["judge"] = {"enabled": True}
        monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "false")
    elif mode == "flag_off":
        gate.config.values["judge"] = {"enabled": True}
        monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "true")
        flags = ["--no-judge"]
    assert observe(*flags) == baseline
    forbidden.assert_not_called()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_synthetic_replay_through_adapter_and_grind(gate, monkeypatch, case):
    assert case["provenance"] == "synthetic-authored"
    gate.config.values["judge"].update(case.get("config", {}))
    gate.config.values["judge"]["seat"] = "ortus"
    calls = []

    class CannedClient:
        async def system_one(self, state, questions, **kwargs):
            calls.append((state, questions, kwargs))
            if case.get("failure") == "service_error":
                raise RuntimeError("synthetic provider prose must never enter logs")
            return deepcopy(case["response"])

    monkeypatch.setattr(grind_mod, "TypeSafeJudge", lambda cfg: TypeSafeJudge(
        cfg, lambda cfg: CannedClient(), {"TYPESAFE_API_KEY": "synthetic-test-key"},
    ))
    # Exercise actual availability filtering without consulting installed binaries.
    monkeypatch.setattr("ortus.core.judge_routing._backend_binary", lambda name, **kw:
                        "/synthetic/" + name if name in case["available"] else None)
    monkeypatch.setattr(grind_mod, "plan_routes", plan_routes)
    result = gate.invoke("--tasks", "1")
    assert result.exit_code == 0, result.output + str(result.exception)
    assert len(calls) == 1
    state, questions, request = calls[0]
    assert state["seat"] == "ortus"
    # Reviewed text travels by default, but only the screened first lines do.
    assert state["title"] == ""
    assert state["objective"] == "Ship the bounded behavior."
    assert state["acceptance"] == (
        "- AC-1: Preview performs no writes.\n- AC-2: Normal execution is unchanged."
    )
    packed = json.dumps(state)
    assert "Behavioral context" not in packed and "Criterion checks" not in packed
    assert set(questions) == {"route", "needs_human", "action_risk"}
    offered = set(questions["route"]["criteria"])
    assert offered == set(case["available"]) | {"skip", "human"}
    assert request["model"] == "jev-1.13.0"
    expected = case["expected"]
    decision, outcome = gate.events()
    for key in ("effective_action", "backend", "reason", "failure"):
        assert decision[key] == expected[key]
    launched = expected["effective_action"] == "proceed"
    assert gate.worker.run.call_count == int(launched)
    row = gate.bd.rows["demo-1"]
    assert row["status"] == ("closed" if launched else "open")
    assert ("human" in row["labels"]) == (expected["effective_action"] == "human")
    assert ("release" in gate.bd.calls) == (not launched)
    assert outcome["decision_id"] == decision["decision_id"]
    assert outcome["observed_status"] == row["status"]
    if launched:
        bundle = gate.bundles[JudgeRoute(expected["backend"])]
        assert bundle.runner.run.called
        assert "Bound issue contract v1" in bundle.runner.run.call_args.args[0]
    serialized = json.dumps(gate.events())
    assert "synthetic provider prose" not in serialized
    assert "ignore the typed answers" not in serialized
    assert "synthetic-test-key" not in serialized
    assert decision["measured_cost_usd"] is None


@pytest.mark.parametrize("failure", ["key_missing", "sdk_missing"])
@pytest.mark.parametrize("mode", ["open", "closed"])
def test_missing_prerequisites_use_real_adapter_failure_policy(gate, monkeypatch, failure, mode):
    gate.config.values["judge"]["failure_mode"] = mode
    env = {} if failure == "key_missing" else {"TYPESAFE_API_KEY": "synthetic-test-key"}
    monkeypatch.setattr(grind_mod, "TypeSafeJudge", lambda cfg: TypeSafeJudge(cfg, environ=env))
    result = gate.invoke("--tasks", "1")
    assert result.exit_code == 0, result.output + str(result.exception)
    decision, outcome = gate.events()
    assert decision["failure"] == failure
    assert decision["effective_action"] == ("proceed" if mode == "open" else "human")
    assert gate.worker.run.call_count == int(mode == "open")
    assert outcome["observed_status"] == ("closed" if mode == "open" else "open")


def test_unrelated_cli_works_without_optional_sdk():
    result = CliRunner().invoke(app, ["prompt", "list"])
    assert result.exit_code == 0, result.exception
    assert "goal" in result.output
