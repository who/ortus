"""Tests for the shared readiness repair pass (ortus-xhrj.6)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ortus.core.profiles import AgentProfile, Phase
from ortus.core.readiness import ReadinessFailure, ReadinessReport
from ortus.core.repair import (
    RepairCreatedReplacements,
    guard_no_replacements,
    readiness_repair_prompt,
    repair_readiness,
    replacement_ids,
)


def _report(issue_id: str) -> ReadinessReport:
    return ReadinessReport(
        issue_id=issue_id,
        exempt=False,
        failures=(
            ReadinessFailure(
                "ordered_steps",
                "design",
                "ordered steps",
                "must contain a numbered implementation step",
            ),
        ),
    )


class _SpyRunner:
    """Records one repair invocation without launching a backend."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []
        self.capabilities: list[object] = []

    def configure_codegraph(self, capability: object) -> None:
        self.capabilities.append(capability)

    def run(self, prompt: str, **kwargs: object) -> int:
        self.prompts.append(prompt)
        self.kwargs.append(kwargs)
        return 0


def test_repair_pass_is_importable_without_a_command_module() -> None:
    """AC-1: core.repair must not drag ortus.commands in behind it."""

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ortus.core.repair as repair; "
            "print(repair.repair_readiness.__module__); "
            "print(sorted(m for m in sys.modules if m.startswith('ortus.commands')))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    module, commands = probe.stdout.splitlines()[:2]
    assert module == "ortus.core.repair"
    assert commands == "[]"


def test_replacement_guard_rejects_a_created_issue_id() -> None:
    """AC-3: a repair that adds an id is a hard error, not silent growth."""

    with pytest.raises(RepairCreatedReplacements) as excinfo:
        guard_no_replacements({"fixture-1"}, {"fixture-1", "fixture-2"})
    assert excinfo.value.issue_ids == ("fixture-2",)
    assert "replacement issue" in str(excinfo.value)
    assert "fixture-2" in str(excinfo.value)


def test_replacement_guard_allows_an_update_in_place_pass() -> None:
    ids = {"fixture-1", "fixture-2"}
    guard_no_replacements(ids, ids)
    # A closed or deleted issue is not a replacement, so it must not trip it.
    guard_no_replacements(ids, {"fixture-1"})
    assert replacement_ids(ids, ids) == ()


def test_repair_prompt_names_every_id_and_its_exact_failures() -> None:
    prompt = readiness_repair_prompt((_report("fixture-1"), _report("fixture-2")))
    assert "Repair ONLY these existing issue IDs: fixture-1, fixture-2" in prompt
    assert "must contain a numbered implementation step" in prompt
    # The pass may only update the named packets in place.
    assert "Do not run `bd create`" in prompt
    assert "readiness schema v1" in prompt


def test_repair_runs_one_pass_on_the_supplied_profile(tmp_path: Path) -> None:
    spy = _SpyRunner()
    profile = AgentProfile("claude", Phase.PLAN, "opus", "high")
    rc = repair_readiness(
        tmp_path,
        (_report("fixture-1"),),
        log_path=tmp_path / "repair.log",
        backend="claude",
        profile=profile,
        contract="\n\nCONTRACT",
        capability=None,
        runner_factory=lambda: spy,
    )
    assert rc == 0
    assert len(spy.prompts) == 1
    assert "Repair ONLY these existing issue IDs: fixture-1" in spy.prompts[0]
    assert spy.prompts[0].endswith("CONTRACT")
    assert spy.kwargs[0]["profile"] is profile
    assert spy.kwargs[0]["repo"] == tmp_path
    assert spy.capabilities == [None]


def test_repair_appends_caller_context_before_the_contract(tmp_path: Path) -> None:
    """Grind has no PRD, so it grounds the pass in the parent epic instead."""

    spy = _SpyRunner()
    repair_readiness(
        tmp_path,
        (_report("fixture-1"),),
        log_path=tmp_path / "repair.log",
        backend="claude",
        profile=AgentProfile("claude", Phase.PLAN, "opus", "high"),
        contract="\n\nCONTRACT",
        capability=None,
        context="CONTEXT. no PRD here.",
        runner_factory=lambda: spy,
    )
    prompt = spy.prompts[0]
    assert prompt.index("Repair ONLY") < prompt.index("CONTEXT. no PRD here.")
    assert prompt.endswith("CONTRACT")
    assert "timeout" not in spy.kwargs[0]


def test_repair_forwards_a_timeout_only_when_the_caller_bounds_it(
    tmp_path: Path,
) -> None:
    spy = _SpyRunner()
    repair_readiness(
        tmp_path,
        (_report("fixture-1"),),
        log_path=tmp_path / "repair.log",
        backend="claude",
        profile=AgentProfile("claude", Phase.PLAN, "opus", "high"),
        contract="",
        capability=None,
        timeout=90,
        runner_factory=lambda: spy,
    )
    assert spy.kwargs[0]["timeout"] == 90


def test_repair_with_no_reports_is_a_no_op(tmp_path: Path) -> None:
    spy = _SpyRunner()

    def _factory(*args: object) -> _SpyRunner:
        raise AssertionError("empty reports must not launch a subprocess")

    assert (
        repair_readiness(
            tmp_path,
            (),
            log_path=tmp_path / "repair.log",
            backend="claude",
            profile=AgentProfile("claude", Phase.PLAN, "opus", "high"),
            contract="",
            capability=None,
            runner_factory=_factory,
        )
        == 0
    )
    assert spy.prompts == []


def test_repair_selects_the_codex_runner_for_the_codex_backend(
    tmp_path: Path,
) -> None:
    requested: list[tuple[object, ...]] = []
    spy = _SpyRunner()

    def _factory(*args: object) -> _SpyRunner:
        requested.append(args)
        return spy

    repair_readiness(
        tmp_path,
        (_report("fixture-1"),),
        log_path=tmp_path / "repair.log",
        backend="codex",
        profile=AgentProfile("codex", Phase.PLAN, "gpt-5", "high"),
        contract="",
        capability=None,
        runner_factory=_factory,
    )
    assert requested == [("codex",)]
