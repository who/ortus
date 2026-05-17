"""Integration tests for --orphan-policy={warn,revert,escalate} (ortus-3ico #4).

Each test seeds a tiny bd workspace, installs a fake claude that CLAIMS
an issue but doesn't close it, then verifies the configured policy is
honored by inspecting bd state after the iteration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import grind as grind_mod
from ortus.core import sandbox as sandbox_mod
from ortus.core.claude import ClaudeRunner
from ortus.core.sandbox import SandboxInfo
from tests._shims import make_inline_python_shim


pytestmark = pytest.mark.integration
runner = CliRunner()


def _stub_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_mod, "smoke_test", lambda: SandboxInfo(platform="Linux", binary="bwrap")
    )


def _seed_repo(tmp_path: Path) -> tuple[Path, str]:
    """Returns (repo, issue_id) — one ready issue."""
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    repo = tmp_path / "orphan-policy"
    repo.mkdir()
    subprocess.run(
        ["bd", "init", "--prefix", "op"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    issue_id = subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", "orphan-policy test",
            "--type", "task",
            "--priority", "2",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"sandbox": {"excludedCommands": ["bd", "bd *"]}}))
    return repo, issue_id


def _claim_only_shim(tmp_path: Path) -> Path:
    return make_inline_python_shim(
        tmp_path,
        "claude-orphans",
        textwrap.dedent(
            """\
            import json
            import subprocess
            ready = json.loads(subprocess.run(
                ["bd", "ready", "--json"], check=True, capture_output=True, text=True
            ).stdout)
            first = next((i["id"] for i in ready if i.get("issue_type") != "epic"), None)
            if first:
                subprocess.run(
                    ["bd", "update", first, "--status", "in_progress"],
                    check=True, stdout=subprocess.DEVNULL,
                )
                print(f"claude (orphan-test) claimed {first} and bailed", flush=True)
            """
        ),
    )


def _install_shim(monkeypatch: pytest.MonkeyPatch, shim: Path) -> None:
    monkeypatch.setattr(
        grind_mod, "_make_runner", lambda: ClaudeRunner(claude_binary=str(shim))
    )


def _force_fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


def _bd_show(repo: Path, issue_id: str) -> dict:
    """Read one issue's full JSON via `bd show <id> --json`."""
    proc = subprocess.run(
        ["bd", "show", issue_id, "--json"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout)
    if isinstance(data, list):
        return data[0]
    return data


def _bd_labels(repo: Path, issue_id: str) -> list[str]:
    return _bd_show(repo, issue_id).get("labels") or []


# --- warn (default) -------------------------------------------------------


def test_orphan_policy_warn_default_leaves_issue_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default orphan-policy=warn: log only; bd state stays in_progress."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(monkeypatch, _claim_only_shim(tmp_path))

    result = runner.invoke(
        app,
        ["grind", str(repo), "--iterations", "1", "--idle-sleep", "0"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _bd_show(repo, issue_id)
    assert issue["status"] == "in_progress", (
        f"warn policy should NOT mutate bd state; got status={issue['status']}"
    )
    log = sorted((repo / "logs").glob("grind-*.log"))[-1].read_text()
    assert "WARN orphan claim" in log
    assert f"warn: orphan claim on {issue_id}" in log


# --- revert ---------------------------------------------------------------


def test_orphan_policy_revert_returns_issue_to_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--orphan-policy=revert: bd update <id> --status=open is called."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(monkeypatch, _claim_only_shim(tmp_path))

    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--orphan-policy", "revert",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    issue = _bd_show(repo, issue_id)
    assert issue["status"] == "open", (
        f"revert policy should restore status to open; got status={issue['status']}"
    )
    log = sorted((repo / "logs").glob("grind-*.log"))[-1].read_text()
    assert f"revert: {issue_id}" in log


# --- escalate -------------------------------------------------------------


def test_orphan_policy_escalate_labels_issue_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--orphan-policy=escalate: bd label add <id> human is called."""
    repo, issue_id = _seed_repo(tmp_path)
    _stub_sandbox(monkeypatch)
    _force_fake_home(monkeypatch, tmp_path)
    _install_shim(monkeypatch, _claim_only_shim(tmp_path))

    result = runner.invoke(
        app,
        [
            "grind", str(repo),
            "--iterations", "1",
            "--idle-sleep", "0",
            "--orphan-policy", "escalate",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr

    labels = _bd_labels(repo, issue_id)
    assert "human" in labels, (
        f"escalate policy should add 'human' label; got labels={labels}"
    )
    log = sorted((repo / "logs").glob("grind-*.log"))[-1].read_text()
    assert f"escalate: {issue_id}" in log
