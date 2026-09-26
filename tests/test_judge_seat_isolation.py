"""Exercise documented seat rollout through real config, transport and log paths."""

from copy import deepcopy
import json
from pathlib import Path
import re
import socket
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.config import load_config
from ortus.core.init_render import RenderContext, render_template
from ortus.core.judge import parse_judge_config
from ortus.core.judge_packs import criteria_hash
from ortus.core.judge_routing import ExecutionBundle, plan_routes
from ortus.core.judge_typesafe import TypeSafeJudge
from ortus.core.profiles import Phase
from tests.test_grind_judge import Tracker, gate as gate


ROOT = Path(__file__).resolve().parents[1]


def rollout_example():
    guide = (ROOT / "docs/judge.md").read_text()
    section = guide.split("[//]: # (BEGIN two-seat rollout example)", 1)[1]
    return section.split("```toml\n", 1)[1].split("```", 1)[0]


@pytest.fixture
def seats(tmp_path, monkeypatch):
    """One shared user layer, two numeric directories, no ambient credentials."""
    for name in ("ORTUS_JUDGE_ENABLED", "ORTUS_JUDGE_MODEL", "ORTUS_JUDGE_SEAT",
                 "ORTUS_BACKEND", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def offline(*args, **kwargs):
        raise AssertionError("seat isolation tests must never use the network")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    home = tmp_path / "shared-home"
    home.mkdir()
    (home / ".ortusrc").write_text(
        '[judge]\nenabled = true\nseat = "birch"\n[judge.seats.birch]\nenabled = true\n'
    )
    repos = {}
    for number, alias in enumerate(("atlas", "birch"), start=1):
        repo = tmp_path / f"{number:02}"
        repo.mkdir()
        (repo / ".beads").mkdir()
        text = rollout_example().replace('seat = "atlas"', f'seat = "{alias}"')
        (repo / ".ortusrc").write_text('codegraph = "off"\n' + text)
        repos[alias] = repo
    return home, repos


def test_documented_seats_override_shared_enablement_without_mutating_layers(seats):
    home, repos = seats
    configs = {alias: load_config(repo=repo, home=home) for alias, repo in repos.items()}
    before = deepcopy({alias: cfg.values for alias, cfg in configs.items()})
    atlas = parse_judge_config(configs["atlas"], environ={})
    birch = parse_judge_config(configs["birch"], environ={})
    assert atlas.enabled and not birch.enabled
    assert atlas.mode.value == birch.mode.value == "shadow"
    assert atlas.route_confidence == 0.9 and birch.route_confidence == 0.95
    assert atlas.sensitive_paths == ("atlas-private/",)
    assert birch.sensitive_paths == ("birch-private/",)
    assert not any((atlas.pre_tool, atlas.post_turn, birch.pre_tool, birch.post_turn))
    killed = parse_judge_config(configs["atlas"], judge=False,
                                environ={"ORTUS_JUDGE_ENABLED": "true"})
    assert not killed.enabled
    assert parse_judge_config(configs["birch"], judge=True, environ={}).enabled
    assert {alias: cfg.values for alias, cfg in configs.items()} == before
    assert parse_judge_config(load_config(home=home), environ={}).enabled


@pytest.mark.parametrize("backend", ["claude", "codex", "grok", "local", "opencode"])
def test_rendered_template_and_commented_seat_recipe_load(tmp_path, backend):
    rendered = render_template(".ortusrc", RenderContext(
        prefix="sample", backend=backend, local_model="served-model",
    ))
    target = tmp_path / ".ortusrc"
    target.write_text(rendered)
    assert not parse_judge_config(load_config(repo=tmp_path, home=tmp_path / "home"),
                                  environ={}).enabled
    # Activate just the documented optional judge example, preserving local TOML.
    before, block = rendered.split("# [judge]\n", 1)
    block, after = block.split("\n\n", 1)
    lines = ["[judge]"]
    for line in block.splitlines():
        value = line.removeprefix("# ")
        if value.startswith("[") or re.match(r"^[a-z_]+ = ", value):
            lines.append(value)
    example = "\n".join(lines).replace('seat = "default"', 'seat = "atlas"')
    target.write_text(before + example + "\n\n" + after)
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    for alias, threshold in (("atlas", 0.9), ("birch", 0.95)):
        selected = parse_judge_config(cfg, judge_seat=alias, environ={})
        assert not selected.enabled and selected.route_confidence == threshold
        assert not selected.pre_tool and not selected.post_turn


@pytest.mark.parametrize("first", ["atlas", "birch"])
@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_cli_invocations_keep_credentials_requests_and_logs_in_their_seat(
    gate, seats, monkeypatch, first, mode,
):
    home, repos = seats
    if mode == "enforce":
        for repo in repos.values():
            path = repo / ".ortusrc"
            path.write_text(path.read_text().replace('mode = "shadow"', 'mode = "enforce"'))
    trackers = {repo: Tracker() for repo in repos.values()}
    workers = {}
    for repo, tracker in trackers.items():
        worker = Mock(extra_env={})
        worker.run.side_effect = lambda *a, tracker=tracker, **kw: (
            tracker.rows["demo-1"].update(status="closed") or 0
        )
        workers[repo] = worker
    monkeypatch.setattr(grind_mod, "load_config", lambda *, repo: load_config(repo=repo, home=home))
    monkeypatch.setattr(grind_mod, "_make_bd", lambda repo: trackers[repo])
    monkeypatch.setattr(grind_mod, "plan_routes", plan_routes)
    monkeypatch.setattr("ortus.core.judge_routing._backend_binary",
                        lambda name, **kw: f"/synthetic/{name}")
    current_repo = None
    monkeypatch.setattr(grind_mod, "_make_runner",
                        lambda *a, repo=None, **kw: workers[repo or current_repo])
    calls = []

    class Client:
        def __init__(self, alias):
            self.alias = alias

        async def system_one(self, payload, questions, **kwargs):
            calls.append((self.alias, deepcopy(payload), deepcopy(questions)))
            return {"model": "jev-1.13.0", "answers": {
                "route": {"type": "choice", "choice": "claude", "confidence": 0.99},
                "needs_human": {"type": "noul", "noul": 0.01},
                "action_risk": {"type": "score", "score": 0, "confidence": 0.99},
            }}

    def adapter(cfg):
        def client_factory(config):
            # Each SDK construction sees only the current process credential.
            import os
            assert os.environ["TYPESAFE_API_KEY"] == f"synthetic-{config.seat}-secret"
            return Client(config.seat)
        return TypeSafeJudge(cfg, client_factory)

    monkeypatch.setattr(grind_mod, "TypeSafeJudge", adapter)

    def prepare(plan, route, *, repo, config, **kwargs):
        return ExecutionBundle(route, workers[repo],
                               *(config.resolve_profile(route.value, phase) for phase in
                                 (Phase.IMPLEMENT, Phase.VERIFY, Phase.FINALIZE)),
                               grind_mod._make_codegraph().probe())

    monkeypatch.setattr(grind_mod, "prepare_route", prepare)

    def invoke(alias, *flags):
        nonlocal current_repo
        repo = repos[alias]
        current_repo = repo
        trackers[repo].rows = Tracker().rows
        workers[repo].run.reset_mock()
        result = CliRunner().invoke(app, ["grind", str(repo), "--judge-seat", alias,
                                         "--tasks", "1", "--iterations", "1",
                                         "--idle-sleep", "0", *flags])
        assert result.exit_code == 0, result.output + str(result.exception)
        return result

    # The shared user's true never activates the explicitly disabled repository.
    invoke("birch")
    assert not calls and not (repos["birch"] / "logs/jev-decisions.jsonl").exists()
    workers[repos["birch"]].run.assert_called_once()

    log_bytes = {}
    hashes = {}
    for alias in (first, "birch" if first == "atlas" else "atlas"):
        monkeypatch.setenv("TYPESAFE_API_KEY", f"synthetic-{alias}-secret")
        invoke(alias, "--judge")
        assert calls[-1][0] == alias and calls[-1][1]["seat"] == alias
        _, payload, questions = calls[-1]
        assert alias in questions["needs_human"]["criteria"]["true"][0]
        cfg = parse_judge_config(load_config(repo=repos[alias], home=home), environ={})
        hashes[alias] = payload["criteria_hash"]
        assert hashes[alias] == criteria_hash(cfg, questions)
        assert payload["title"] == payload["objective"] == payload["acceptance"] == ""
        path = repos[alias] / "logs/jev-decisions.jsonl"
        log_bytes[alias] = path.read_bytes()
        events = [json.loads(line) for line in log_bytes[alias].splitlines()]
        decision, outcome = events
        assert decision["seat"] == alias and decision["criteria_hash"] == hashes[alias]
        if mode == "shadow":
            assert decision["mode"] == "shadow" and decision["effective_action"] == "baseline"
        else:
            assert decision["effective_action"] == "proceed"
        assert decision["decision_id"] == outcome["decision_id"]
        assert path.stat().st_mode & 0o777 == 0o600
        for prior, saved in log_bytes.items():
            assert (repos[prior] / "logs/jev-decisions.jsonl").read_bytes() == saved
    assert len(calls) == 2 and hashes["atlas"] != hashes["birch"]
    for alias in repos:
        assert f"synthetic-{alias}-secret" not in json.dumps(calls) + str(log_bytes)

    # A missing key in active atlas does not reuse birch's earlier credential.
    monkeypatch.delenv("TYPESAFE_API_KEY")
    invoke("atlas")
    assert len(calls) == 2
    events = [json.loads(line) for line in
              (repos["atlas"] / "logs/jev-decisions.jsonl").read_text().splitlines()]
    assert events[-2]["failure"] == "key_missing"
    assert (repos["birch"] / "logs/jev-decisions.jsonl").read_bytes() == log_bytes["birch"]
    atlas_log = (repos["atlas"] / "logs/jev-decisions.jsonl").read_bytes()
    invoke("birch")
    monkeypatch.setenv("ORTUS_JUDGE_ENABLED", "true")
    invoke("atlas", "--no-judge")
    assert len(calls) == 2
    assert (repos["atlas"] / "logs/jev-decisions.jsonl").read_bytes() == atlas_log
    assert (repos["birch"] / "logs/jev-decisions.jsonl").read_bytes() == log_bytes["birch"]
