"""Tests for ortus triage (idzn.2 + ortus-sr0b envelope-driven flow)."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import triage as triage_mod
from ortus.core.claude import ClaudeRunner
from tests._shims import make_inline_python_shim, shim_path

pytestmark = pytest.mark.integration
runner = CliRunner()

FAKE = shim_path("fake-claude-interview")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    subprocess.run(
        ["bd", "init", "--prefix", "tr"],
        cwd=str(tmp_path), check=True, capture_output=True,
    )
    return tmp_path


def _make_human_flagged(workspace: Path, title: str) -> str:
    return subprocess.run(
        [
            "bd", "create", "--silent", "--title", title, "--type", "task",
            "--priority", "2", "--labels", "human",
        ],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    ).stdout.strip()


def _swap_runner_with_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        triage_mod, "_make_runner",
        lambda: ClaudeRunner(claude_binary=str(shim)),
    )


def _swap_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    _swap_runner_with_shim(monkeypatch, FAKE)


def _envelope_writer_shim(tmp_path: Path, issue_ids: list[str], recommend: str = "skip") -> Path:
    """Build an inline shim that writes a canned envelope for each issue_id.

    Use this instead of the bundled triage-walk-queue shim when a test
    needs to drive a specific recommended_disposition (so the wrapper's
    apply path can be exercised under operator-prompt input).
    """
    issue_payload = json.dumps([
        {"issue_id": iid, "title": f"issue {iid}", "priority": 2, "status": "open",
         "context_summary": "test envelope", "recommended_disposition": recommend,
         "rationale": "test"}
        for iid in issue_ids
    ])
    body = textwrap.dedent(f'''
        import json, os
        from pathlib import Path

        print('{{"type":"system","subtype":"start","session_id":"triage-inline"}}', flush=True)
        envs = {issue_payload}
        out = Path(os.getcwd()) / "logs" / "triage-envelopes.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            for env in envs:
                fh.write(json.dumps(env) + "\\n")
        print('{{"type":"assistant","message":{{"content":"wrote envelopes"}}}}', flush=True)
        print('{{"type":"system","subtype":"end"}}', flush=True)
    ''').lstrip()
    return make_inline_python_shim(tmp_path, "triage-envelope-writer", body)


def test_triage_exits_zero_with_message_when_queue_empty(workspace: Path) -> None:
    """Acceptance #2: no human-flagged issues → exit 0 + message, no claude."""
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 0
    assert "no human-queue items" in result.stdout
    assert not (workspace / "logs" / "triage.log").exists()


def test_triage_prompt_bundled() -> None:
    """Acceptance #3: triage-prompt.md ships in the package."""
    from ortus.core.prompts import resolve_prompt
    res = resolve_prompt("triage-prompt", repo=Path("/tmp"))
    assert res.source == "bundled"
    assert "Triage Prompt" in res.text


def test_triage_prompt_forbids_askuserquestion() -> None:
    """ortus-sr0b regression: prompt must not instruct agent to call AskUserQuestion."""
    from ortus.core.prompts import resolve_prompt
    res = resolve_prompt("triage-prompt", repo=Path("/tmp"))
    # The prompt explains *why* AskUserQuestion is dead under -p and bans
    # its use; the agent must not be instructed to call it.
    assert "DO NOT call `AskUserQuestion`" in res.text
    assert "logs/triage-envelopes.jsonl" in res.text


def test_triage_fails_when_no_envelopes_written(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If claude exits 0 but never writes envelopes, wrapper errors loudly."""
    _make_human_flagged(workspace, "needs decision")
    # The default fake-claude-interview shim exits 0 without touching the file.
    _swap_runner(monkeypatch)
    result = runner.invoke(app, ["triage", str(workspace)])
    assert result.exit_code == 2
    assert "did not write any envelopes" in result.stderr


def test_triage_count_in_starting_line(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    iid_a = _make_human_flagged(workspace, "a")
    iid_b = _make_human_flagged(workspace, "b")
    iid_c = _make_human_flagged(workspace, "c")
    shim = _envelope_writer_shim(tmp_path, [iid_a, iid_b, iid_c])
    _swap_runner_with_shim(monkeypatch, shim)
    # Three issues, three "skip" choices needed (default is index 5, so
    # empty input would also accept; pass explicit "5" for clarity).
    result = runner.invoke(app, ["triage", str(workspace)], input="5\n5\n5\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "3 human-flagged issue(s)" in result.stdout


def test_triage_operator_is_actually_prompted(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ortus-sr0b acceptance: operator is actually prompted for a decision.

    Two human-flagged issues; canned claude writes envelopes; operator
    input picks skip for both. Wrapper must print the per-issue
    disposition menu (proves prompting happened).
    """
    iid_a = _make_human_flagged(workspace, "needs decision A")
    iid_b = _make_human_flagged(workspace, "needs decision B")
    shim = _envelope_writer_shim(tmp_path, [iid_a, iid_b])
    _swap_runner_with_shim(monkeypatch, shim)
    result = runner.invoke(app, ["triage", str(workspace)], input="5\n5\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    # The disposition menu must appear once per issue. Match on the
    # menu prompt header so we know the wrapper hit the operator I/O
    # path (not just printed the cards).
    assert result.stdout.count(f"Disposition for {iid_a}") == 1
    assert result.stdout.count(f"Disposition for {iid_b}") == 1
    # And the per-issue card header confirms envelope rendering.
    assert "Issue 1 of 2" in result.stdout
    assert "Issue 2 of 2" in result.stdout


def test_triage_close_disposition_applies_bd_close(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Choosing Close on an issue actually closes it via bd."""
    iid = _make_human_flagged(workspace, "will be closed")
    shim = _envelope_writer_shim(tmp_path, [iid], recommend="close")
    _swap_runner_with_shim(monkeypatch, shim)
    # Input: "2" = Close, then "2" = "Already resolved" reason.
    result = runner.invoke(app, ["triage", str(workspace)], input="2\n2\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    # bd should now show the issue as closed.
    show = subprocess.run(
        ["bd", "show", iid, "--json"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    )
    payload = json.loads(show.stdout)
    issue = payload[0] if isinstance(payload, list) else payload
    assert issue["status"] == "closed"


def test_triage_dismiss_removes_human_label(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Choosing Dismiss strips the human label and reopens the issue."""
    iid = _make_human_flagged(workspace, "will be dismissed")
    shim = _envelope_writer_shim(tmp_path, [iid], recommend="dismiss")
    _swap_runner_with_shim(monkeypatch, shim)
    # Input: "4" = Dismiss.
    result = runner.invoke(app, ["triage", str(workspace)], input="4\n")
    assert result.exit_code == 0, result.stdout + result.stderr

    show = subprocess.run(
        ["bd", "show", iid, "--json"],
        cwd=str(workspace), check=True, capture_output=True, text=True,
    )
    payload = json.loads(show.stdout)
    issue = payload[0] if isinstance(payload, list) else payload
    assert "human" not in (issue.get("labels") or [])
    assert issue["status"] == "open"


@pytest.mark.smoke
def test_triage_smoke_with_canned_response(
    workspace: Path, claude_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke: triage against the canned envelope-writer scenario walks the queue."""
    _make_human_flagged(workspace, "smoke triage 1")
    shim = claude_mock("triage-walk-queue")
    monkeypatch.setattr(
        triage_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )
    result = runner.invoke(app, ["triage", str(workspace)], input="5\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    envelopes = (workspace / "logs" / "triage-envelopes.jsonl").read_text(
        encoding="utf-8"
    )
    assert envelopes.strip(), "canned shim should have written at least one envelope"
