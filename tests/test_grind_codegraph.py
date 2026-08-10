"""Hermetic tests for the pre-edit Codex CodeGraph handshake gate.

The second half covers the loop-level gate in `grind`: it must stay fatal for
an iteration that actually spawns an implementation worker, and must not fire
on a resume from `candidate-captured`, which reuses the prior candidate and
never asks an agent to perform the handshake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.commands.grind import _codex_codegraph_handshake
from ortus.core.codegraph import (
    CodeGraphCapability,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphProbe,
    CodeGraphUnavailable,
)
from ortus.core.profiles import AgentProfile, Phase
from tests.test_grind_recovery import (
    CANDIDATE,
    INHERITED,
    ScriptedRunner,
    _grind,
    _install,
    _issue,
    _log,
    _pass_verdict,
    _seed,
    _stage_journal,
)

runner = CliRunner()


def _probe(mode: CodeGraphMode) -> CodeGraphProbe:
    return CodeGraphProbe(
        mode,
        True,
        True,
        True,
        capability=CodeGraphCapability("/bin/codegraph"),
    )


def _emit_query(log_path: Path, phase: str) -> None:
    """One successful `codegraph_explore` call in the agent's JSONL stream."""
    with log_path.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": phase,
                        "type": "mcp_tool_call",
                        "server": "codegraph",
                        "tool": "codegraph_explore",
                        "arguments": {"query": f"{phase} orientation"},
                        "result": {"results": [{"symbol": "ok"}]},
                    },
                }
            )
            + "\n"
        )


class _HandshakeRunner:
    def run_codegraph_handshake(
        self, *, phase: str, log_path: Path, **kwargs: object
    ) -> int:
        _emit_query(log_path, phase)
        return 0


@pytest.mark.parametrize(
    ("phase", "profile_phase"),
    [
        (CodeGraphPhase.IMPLEMENTATION, Phase.IMPLEMENT),
        (CodeGraphPhase.VERIFICATION, Phase.VERIFY),
    ],
)
def test_codex_handshake_succeeds_for_both_fresh_worker_postures(
    tmp_path: Path, phase: CodeGraphPhase, profile_phase: Phase
) -> None:
    log = tmp_path / "grind.log"
    result = _codex_codegraph_handshake(
        _HandshakeRunner(),  # type: ignore[arg-type]
        repo=tmp_path,
        log_path=log,
        phase=phase,
        probe=_probe(CodeGraphMode.REQUIRED),
        profile=AgentProfile("codex", profile_phase),
        timeout=10,
    )
    assert result.available
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(record.get("kind") == "handshake" and record["success"] for record in records)
    assert any(record.get("kind") == "query" for record in records)


def test_auto_child_missing_records_precise_fallback(tmp_path: Path) -> None:
    result = _codex_codegraph_handshake(
        object(),  # type: ignore[arg-type]
        repo=tmp_path,
        log_path=tmp_path / "grind.log",
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_probe(CodeGraphMode.AUTO),
        profile=AgentProfile("codex", Phase.IMPLEMENT),
        timeout=10,
    )
    assert not result.available
    assert result.reason == "Codex runner does not support the CodeGraph child handshake"


@pytest.mark.codegraph_default
@pytest.mark.parametrize("verb", ["grind", "plan"])
def test_default_mode_required_aborts_at_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """AC-8: no `--codegraph` flag and no config key means `required`.

    The repo has a bd workspace but no `.codegraph/`, so both verbs must stop
    at the probe with the remediation text rather than launching an agent.
    """
    repo = tmp_path / "unindexed"
    (repo / ".beads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    args = [verb, str(repo)] + (["--dry-run"] if verb == "grind" else [])
    result = runner.invoke(app, args)
    assert result.exit_code == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    compact = "".join(combined.split())
    assert "CodeGraphrequiredbutunavailable" in compact, combined
    assert "codegraphinit" in compact, combined


def test_required_child_missing_halts_at_handshake_gate(tmp_path: Path) -> None:
    with pytest.raises(CodeGraphUnavailable, match="runner does not support"):
        _codex_codegraph_handshake(
            object(),  # type: ignore[arg-type]
            repo=tmp_path,
            log_path=tmp_path / "grind.log",
            phase=CodeGraphPhase.IMPLEMENTATION,
            probe=_probe(CodeGraphMode.REQUIRED),
            profile=AgentProfile("codex", Phase.IMPLEMENT),
            timeout=10,
        )


# ---------------------------------------------------------------------------
# the loop-level implementation gate: a phase that never ran cannot shake hands
# ---------------------------------------------------------------------------

class _FakeAdapter:
    """Outer prerequisites satisfied without an index or a CLI on PATH."""

    def probe(
        self, repo: Path, mode: CodeGraphMode, *, backend: str = "claude"
    ) -> CodeGraphProbe:
        if mode is CodeGraphMode.OFF:
            return CodeGraphProbe(mode, False, False, False, "disabled by policy")
        return CodeGraphProbe(mode, True, True, True)

    def refresh(self, repo: Path, probe: CodeGraphProbe) -> tuple[str, int | None]:
        return ("fresh", 1) if probe.available else ("not-supported", None)


def _querying_verify(repo: Path, log_path: Path) -> int:
    """A verifier that does engage CodeGraph, then returns a passing verdict."""
    _emit_query(log_path, CodeGraphPhase.VERIFICATION.value)
    return _pass_verdict(repo, log_path)


def _silent_implement(repo: Path, log_path: Path) -> int:
    """A worker turn that edits but never calls a CodeGraph tool."""
    (repo / CANDIDATE).write_text("SHIPPED = True\n")
    return 0


def _install_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: ScriptedRunner
) -> None:
    _install(monkeypatch, tmp_path, backend)
    monkeypatch.setattr(grind_mod, "_make_codegraph", _FakeAdapter)


def _stage_resume(repo: Path, issue_id: str) -> None:
    """A prior run that captured a candidate and stopped before verification."""
    (repo / INHERITED).write_text("the prior attempt at this issue\n")
    _stage_journal(
        repo, issue_id, phase="candidate-captured", paths=frozenset({INHERITED})
    )


def _phases(backend: ScriptedRunner) -> list[str]:
    return [phase for phase, _ in backend.prompts]


def _records(repo: Path) -> list[dict]:
    entries = []
    for line in _log(repo).splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


@pytest.mark.integration
@pytest.mark.slow
def test_resume_candidate_skips_handshake_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: a resume from `candidate-captured` reaches the verifier.

    No implementation worker runs, so the iteration's transcript segment is
    empty by construction. Asserting a handshake against it made every such
    resume unrunnable under the default policy.
    """
    repo, issue_id = _seed(tmp_path, "cgres1")
    _stage_resume(repo, issue_id)
    backend = ScriptedRunner(verify=_querying_verify)
    _install_loop(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1", "--codegraph", "required")

    assert result.exit_code == 0, result.stdout + result.stderr
    assert Phase.IMPLEMENT.value not in _phases(backend)
    assert Phase.VERIFY.value in _phases(backend)
    assert _issue(repo, issue_id)["status"] == "closed"


@pytest.mark.integration
@pytest.mark.slow
def test_fresh_worker_without_codegraph_still_halts_at_handshake_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2, the other direction: the gate stays fatal where a worker did run.

    Same policy, same repository — the only difference is that an
    implementation worker was actually spawned and answered without touching
    CodeGraph. That is the case `required` exists for.
    """
    repo, issue_id = _seed(tmp_path, "cgres2")
    backend = ScriptedRunner(implement=_silent_implement, verify=_querying_verify)
    _install_loop(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1", "--codegraph", "required")

    assert result.exit_code == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "reported no CodeGraph MCP" in combined, combined
    assert Phase.VERIFY.value not in _phases(backend)
    assert _issue(repo, issue_id)["status"] == "open"


@pytest.mark.integration
@pytest.mark.slow
def test_resume_logs_skip_instead_of_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the operator sees a skipped phase, not a degraded one.

    `implementation CodeGraph fallback: ...` reads as a broken MCP server and
    sent the original incident chasing a correctly configured one.
    """
    repo, issue_id = _seed(tmp_path, "cgres3")
    _stage_resume(repo, issue_id)
    backend = ScriptedRunner(verify=_querying_verify)
    _install_loop(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1", "--codegraph", "required")

    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "implementation CodeGraph fallback" not in combined, combined
    log = _log(repo)
    assert "implementation CodeGraph handshake not required" in log, log
    assert "no implementation worker turn ran" in log, log


@pytest.mark.integration
@pytest.mark.slow
def test_resume_still_records_phase_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: skipping the assertion must not skip the summary it read.

    Later consumers — the isolation report, the refresh bookkeeping, and the
    verification summary fallback — all read `implementation_summary`.
    """
    repo, issue_id = _seed(tmp_path, "cgres4")
    _stage_resume(repo, issue_id)
    backend = ScriptedRunner(verify=_querying_verify)
    _install_loop(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1", "--codegraph", "required")

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "CodeGraph implementation summary: queries=0" in _log(repo)
    summaries = [
        record
        for record in _records(repo)
        if record.get("kind") == "phase_summary"
        and record.get("phase") == CodeGraphPhase.IMPLEMENTATION.value
    ]
    assert summaries, "the implementation phase summary was never appended"
    assert "agent MCP capability handshake not observed" in summaries[0]["fallbacks"]


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("mode", ["auto", "off"])
@pytest.mark.parametrize("resumed", [True, False])
def test_modes_unchanged_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, resumed: bool
) -> None:
    """AC-5: `auto` and `off` behave as before, resumed or fresh.

    Neither mode ever asserted the handshake, so the guard must be invisible
    here — including the new log line, which `off` must not emit at all.
    """
    repo, issue_id = _seed(tmp_path, f"cg{mode}{int(resumed)}")
    backend = ScriptedRunner(
        implement=None if resumed else _silent_implement, verify=_pass_verdict
    )
    if resumed:
        _stage_resume(repo, issue_id)
    _install_loop(monkeypatch, tmp_path, backend)

    result = _grind(repo, "--tasks", "1", "--codegraph", mode)

    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    log = _log(repo)
    if mode == "off" or not resumed:
        assert "implementation CodeGraph handshake not required" not in log, log
    else:
        assert "implementation CodeGraph handshake not required" in log, log
