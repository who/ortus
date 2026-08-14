"""Hermetic tests for the live CodeGraph handshake gate.

Host capability is one MCP tools/call inside the required probe. Implementation
handshake is a live tool_result observed on the tee before grind judges bd
status. Codex no longer launches a separate handshake agent to scrape a log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core.agent import CodexRunner
from ortus.core.codegraph import (
    CodeGraphMode,
    CodeGraphProbe,
    CodeGraphUnavailable,
    require_handshake,
)
from tests.test_grind import (
    _bd_repo,
    _CloseWithoutClaimsRunner,
    _create_ready_issue,
    _fake_sandbox,
    _grind_log,
    _issue,
)

runner = CliRunner()
_GRIND_PY = Path(__file__).resolve().parents[1] / "src" / "ortus" / "commands" / "grind.py"


def _available_probe(mode: object) -> CodeGraphProbe:
    return CodeGraphProbe(mode, True, True, True)


class _AvailableCodeGraph:
    def probe(self, repo: Path, mode: object, *, backend: str = "claude") -> object:
        return _available_probe(mode)

    def refresh(self, repo: Path, probe: object) -> tuple[str, int]:
        return ("fresh", 1)


def _emit_query(log_path: Path, phase: str) -> None:
    """One successful `codegraph_explore` call in the agent's JSONL stream."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
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


class _HandshakeThenCloseRunner(_CloseWithoutClaimsRunner):
    def run(self, prompt: str, **kwargs: object) -> int:
        log_path = kwargs.get("log_path")
        if isinstance(log_path, Path):
            _emit_query(log_path, "implementation")
        return super().run(prompt, **kwargs)


class _SpyCodexRunner(_CloseWithoutClaimsRunner):
    def __init__(self, host: Path) -> None:
        super().__init__(host)
        self.handshake_calls = 0

    def run_codegraph_handshake(self, **kwargs: object) -> int:
        self.handshake_calls += 1
        return 0


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


@pytest.mark.slow
@pytest.mark.codegraph_default
def test_implementation_tool_result_is_handshake_success_before_bd_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a live CodeGraph tool_result is handshake-success before bd status."""
    repo = _bd_repo(tmp_path, "live-handshake")
    issue_id = _create_ready_issue(repo, "close after handshake")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _HandshakeThenCloseRunner(repo)
    )
    monkeypatch.setattr(grind_mod, "_make_codegraph", lambda: _AvailableCodeGraph())
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _issue(repo, issue_id)["status"] == "closed"
    log = _grind_log(repo)
    succeeded = log.find("implementation CodeGraph handshake succeeded")
    judged = log.find(f"worker closed {issue_id}")
    assert succeeded != -1, log
    assert judged != -1, log
    assert succeeded < judged


@pytest.mark.slow
@pytest.mark.codegraph_default
def test_silent_required_worker_fails_handshake_even_if_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: no CodeGraph tool_result fails required handshake even if closed."""
    repo = _bd_repo(tmp_path, "silent-required")
    issue_id = _create_ready_issue(repo, "close silently")
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda *a, **k: _CloseWithoutClaimsRunner(repo)
    )
    monkeypatch.setattr(grind_mod, "_make_codegraph", lambda: _AvailableCodeGraph())
    result = runner.invoke(
        app, ["grind", str(repo), "--tasks", "1", "--idle-sleep", "0"]
    )
    combined = result.stdout + result.stderr
    assert result.exit_code == 1, combined
    assert "no CodeGraph MCP" in combined
    assert _issue(repo, issue_id)["status"] == "closed"
    log = _grind_log(repo)
    assert f"worker closed {issue_id}" not in log


def test_codex_no_longer_launches_handshake_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: Codex does not spawn a separate handshake agent to scrape a log."""
    repo = _bd_repo(tmp_path, "no-codex-handshake")
    _create_ready_issue(repo, "close without extra agent")
    spy = _SpyCodexRunner(repo)
    _fake_sandbox(monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    monkeypatch.setattr(grind_mod, "_make_runner", lambda *a, **k: spy)
    result = runner.invoke(
        app,
        [
            "grind",
            str(repo),
            "--backend",
            "codex",
            "--tasks",
            "1",
            "--idle-sleep",
            "0",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert spy.handshake_calls == 0
    assert not hasattr(CodexRunner, "run_codegraph_handshake")
    source = _GRIND_PY.read_text(encoding="utf-8")
    assert "_codex_codegraph_handshake" not in source
    assert "run_codegraph_handshake" not in source


def test_require_handshake_runs_before_judged_status() -> None:
    """The live gate sits before f2he.2 reads bd status, not after continue."""
    source = _GRIND_PY.read_text(encoding="utf-8")
    handshake = source.find("require_handshake(implementation_summary)")
    judged = source.find('judged_status = str(judged.get("status") or "open")')
    assert handshake != -1
    assert judged != -1
    assert handshake < judged


def test_silent_transcript_still_fails_required_handshake(tmp_path: Path) -> None:
    from ortus.core.codegraph import CodeGraphPhase, parse_transcript

    empty = tmp_path / "silent.jsonl"
    empty.write_text('{"type":"turn.completed"}\n')
    summary = parse_transcript(
        empty,
        phase=CodeGraphPhase.IMPLEMENTATION,
        probe=_available_probe(CodeGraphMode.REQUIRED),
    )
    with pytest.raises(CodeGraphUnavailable, match="capability"):
        require_handshake(summary)
