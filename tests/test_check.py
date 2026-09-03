"""Tests for ortus check (q075.6 acceptance criteria)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import check as check_mod
from ortus.core.agent_files import MANAGED_FILES, render_block
from ortus.core.local_backend import LocalConfig, LocalServerError, serving_hint
from ortus.core.readiness import READINESS_MEMORY_KEY, readiness_memory_text

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every check in this module away from the real `~/.claude.json`.

    The MCP-registration probe reads the user scope out of the home
    directory, so a test that ran against the developer's own home would
    pass or fail by whichever machine ran the suite. Autouse rather than
    per-test: a new test here inherits the isolation instead of having to
    remember it.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))


# --- fixture helpers -------------------------------------------------------


def _fake_bd_run(*, readiness_memory: bool, memory_text: str | None = None):
    """Stand in for subprocess.run for every binary `check` shells out to.

    `bd memories --json` answers with a memory map; everything else answers
    with a version line, which is all `_binary_check` reads. `memory_text`
    stores an operator-edited body under the readiness key in place of the
    canonical pointer.
    """
    memories: dict[str, object] = {"schema_version": 1}
    if readiness_memory:
        memories[READINESS_MEMORY_KEY] = (
            readiness_memory_text() if memory_text is None else memory_text
        )

    class _CP:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def _run(args, *a, **k):
        if "memories" in args:
            return _CP(json.dumps(memories))
        return _CP("fake 1.0.0\n")

    return _run


def _compact(stdout: str) -> str:
    """Squash check's table for substring asserts.

    The table wraps long cells, so whitespace goes; a wrap inside a cell also
    threads border characters through the value, so those go too.
    """
    return "".join(ch for ch in stdout if not ch.isspace() and ch not in "│┃")


def _healthy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "healthy"
    (repo / ".beads").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps(
            {"sandbox": {"excludedCommands": ["bd", "bd *", "ortus", "ortus *"]}}
        )
    )
    _healthy_codegraph(repo)
    _healthy_agent_files(repo)
    return repo


def _healthy_agent_files(repo: Path) -> None:
    """Write the managed blocks exactly as `ortus init` would.

    Every backend is checked for them, so a repo that is otherwise green needs
    both files to stay green. The blocks render under the repo's resolved
    CodeGraph policy, which the suite pins to `auto`.
    """
    mode = check_mod._repo_codegraph_mode(repo)
    for managed in MANAGED_FILES:
        (repo / managed.filename).write_text(
            render_block(managed.block, codegraph=mode) + "\n", encoding="utf-8"
        )


def _healthy_codegraph(repo: Path) -> None:
    """Satisfy the CodeGraph prerequisite: index plus project MCP registration.

    CodeGraph defaults to `required`, so a repo that is otherwise green now
    needs both to stay green.
    """
    (repo / ".codegraph").mkdir(parents=True, exist_ok=True)
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": {"command": "codegraph"}}})
    )


def _all_binaries_present(
    monkeypatch: pytest.MonkeyPatch,
    *,
    readiness_memory: bool = True,
    memory_text: str | None = None,
) -> None:
    """Pretend bd, claude, jq are on PATH and return a version string."""
    import subprocess as _sp

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        _sp,
        "run",
        _fake_bd_run(readiness_memory=readiness_memory, memory_text=memory_text),
    )


def _fake_sandbox_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from ortus.core.sandbox import SandboxInfo

    monkeypatch.setattr(
        check_mod.sandbox,
        "smoke_test",
        lambda: SandboxInfo(platform="Linux", binary="bwrap"),
    )
    # The verifier preflight shells out; these tests replace subprocess.run
    # wholesale, so a healthy posture has to be stated rather than executed.
    monkeypatch.setattr(
        check_mod.ClaudeRunner,
        "preflight_readonly",
        lambda self, repo, **kwargs: None,
    )


# --- acceptance tests ------------------------------------------------------


def test_check_help_lists_grok_backend() -> None:
    result = runner.invoke(
        app, ["check", "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    assert "claude|codex|grok" in result.stdout


def test_check_all_green_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #1: healthy repo → exit 0."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
    assert "FAIL" not in result.stdout


def test_check_fails_on_disabled_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #2: disableAllHooks=true → exit 1 with clear FAIL."""
    repo = _healthy_repo(tmp_path)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "disableAllHooks": True,
                "sandbox": {
                    "excludedCommands": ["bd", "bd *", "ortus", "ortus *"]
                },
            }
        )
    )
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "hooks" in result.stdout


def test_check_fails_on_missing_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #3: bwrap missing → exit 1 with sandbox FAIL."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    from ortus.core.sandbox import SandboxUnavailable

    def _boom() -> None:
        raise SandboxUnavailable("Sandbox prerequisite missing: bubblewrap (bwrap)\n  install hint")

    monkeypatch.setattr(check_mod.sandbox, "smoke_test", _boom)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "sandbox" in result.stdout
    assert "FAIL" in result.stdout


def test_check_reports_a_verifier_sandbox_that_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ortus-dyio AC-2: the blocked-execution condition is visible before a run."""
    from ortus.core.claude import ReadOnlyExecutionBlocked

    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)

    def _blocked(self, repo: Path, **kwargs: object) -> None:
        raise ReadOnlyExecutionBlocked(
            "read-only verifier execution probe failed: mkdir: cannot create "
            "directory: Read-only file system\n  agent session-env: /nowhere"
        )

    monkeypatch.setattr(check_mod.ClaudeRunner, "preflight_readonly", _blocked)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    # The table wraps long cells across rows, so drop the borders too.
    compact = "".join(c for c in result.stdout if not c.isspace() and c != "│")
    assert "verifiersandbox" in compact
    assert "executionprobefailed" in compact
    assert "Read-onlyfilesystem" in compact


def test_check_skips_the_verifier_probe_for_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Codex verifier is unwrapped, so there is no posture to probe."""
    repo = tmp_path / "codex-probe"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codegraph").mkdir()
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    _healthy_agent_files(repo)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)

    def _never(self, repo: Path, **kwargs: object) -> None:
        raise AssertionError("the Codex backend must not run the Claude preflight")

    monkeypatch.setattr(check_mod.ClaudeRunner, "preflight_readonly", _never)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "verifier sandbox" not in result.stdout


def test_provisioned_backend_rows_are_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling backend's gaps surface as info-level rows with a remediation."""
    repo = _healthy_repo(tmp_path)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: None)
    row = check_mod.check_provisioned_backend(repo, "codex")
    assert row.level == "info"
    assert not row.ok
    assert "codex CLI not on PATH" in row.message
    assert "install" in row.message

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    row = check_mod.check_provisioned_backend(repo, "codex")
    assert row.ok
    assert row.level == "info"
    assert "runnable" in row.message


def test_check_exit_code_ignores_provisioned_backend_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN rows for sibling backends never fail the run backend's check."""
    repo = _healthy_repo(tmp_path)
    # Provisioned dir whose config file is gone: a gap, but not the run
    # backend's problem — check reports it and still exits 0.
    (repo / ".grok").mkdir()
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    assert "FAIL" not in result.stdout


def _snapshot_mtimes(root: Path) -> dict[str, tuple[float, int]]:
    snap: dict[str, tuple[float, int]] = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p)] = (st.st_mtime, st.st_size)
    return snap


def test_check_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance #4 + #5: NFR-006 read-only — no filesystem mutations."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    before = _snapshot_mtimes(repo)
    runner.invoke(app, ["check", str(repo)])
    after = _snapshot_mtimes(repo)
    assert before == after, "check must be strictly read-only (NFR-006)"


def test_check_reports_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: None)

    def _absent(args, *a, **k):
        raise FileNotFoundError(f"no such binary: {args[0]}")

    monkeypatch.setattr(check_mod.subprocess, "run", _absent)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "bd" in result.stdout and "FAIL" in result.stdout


def test_check_reports_missing_excluded_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    # Wipe excludedCommands so the check fails.
    (repo / ".claude" / "settings.json").write_text(json.dumps({}))
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "excludedCommands" in result.stdout or "sandbox" in result.stdout


def test_check_reports_missing_beads_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "no-beads"
    repo.mkdir()
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert ".beads/" in result.stdout


def test_check_reports_prompt_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "goal-prompt.md").write_text("custom")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0
    assert "goal-prompt.md" in result.stdout


def test_check_reports_stale_override_missing_the_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: an override predating $readiness_spec is reported, not failed."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "plan-prompt.md").write_text("frozen contract, no placeholder\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    # Rich wraps long cells, so compare with whitespace removed.
    compact = "".join(result.stdout.split())
    assert "stale" in compact
    assert "$readiness_spec" in compact


def test_check_leaves_a_current_override_out_of_the_stale_override_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override carrying the placeholder is an ordinary override."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "plan-prompt.md").write_text("custom preamble\n$readiness_spec\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "plan-prompt.md" in compact
    assert "stale" not in compact


def _stamped_override(repo: Path, stem: str, *, of_text: str | None = None) -> Path:
    """Write an ejected-style override; of_text swaps in a drifted source."""
    from ortus.core.prompts import bundled_prompt_text, eject_stamp

    bundled = bundled_prompt_text(stem)
    stamped_source = bundled if of_text is None else of_text
    path = repo / ".ortus" / "prompts" / f"{stem}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(eject_stamp("0.0.0-test", stamped_source) + "\n" + bundled)
    return path


def test_check_warns_when_eject_stamp_predates_bundled_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a stamp hash that no longer matches bundled text warns, exit 0."""
    repo = _healthy_repo(tmp_path)
    _stamped_override(repo, "goal-prompt", of_text="an older bundled body\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    assert "FAIL" not in result.stdout
    compact = "".join(result.stdout.split())
    assert "goal-prompt.md" in compact
    assert "moved" in compact


def test_check_warns_on_unstamped_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a hand-created override with no provenance stamp warns, exit 0."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "goal-prompt.md").write_text("hand-rolled override\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    assert "FAIL" not in result.stdout
    # Rich wraps cells at hyphens, so match on one unbreakable word.
    compact = "".join(result.stdout.split())
    assert "provenance" in compact


def test_check_warns_on_unknown_override_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a typo'd filename in .ortus/prompts/ warns that it never loads."""
    repo = _healthy_repo(tmp_path)
    overrides = repo / ".ortus" / "prompts"
    overrides.mkdir(parents=True)
    (overrides / "gaol-prompt.md").write_text("never loaded\n")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    assert "FAIL" not in result.stdout
    compact = "".join(result.stdout.split())
    assert "gaol-prompt.md" in compact
    assert "neverloaded" in compact


def test_check_passes_a_current_ejected_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a stamped copy of the current bundled text is an ordinary PASS."""
    repo = _healthy_repo(tmp_path)
    _stamped_override(repo, "goal-prompt")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" not in result.stdout
    assert "FAIL" not in result.stdout


def test_check_reports_present_readiness_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the pointer memory is reported as present when bd has it."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    # The table wraps long cells, so compare with whitespace removed.
    compact = "".join(result.stdout.split())
    assert f"key={READINESS_MEMORY_KEY}" in compact
    assert "FAIL" not in result.stdout


def test_check_reports_missing_readiness_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a workspace without the pointer fails and names the add command."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch, readiness_memory=False)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    compact = _compact(result.stdout)
    assert "bdremember" in compact
    assert f"--key{READINESS_MEMORY_KEY}" in compact


def test_check_reports_stale_readiness_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a body edited to drop `ortus spec` fails and names the refresh.

    The substring is the whole gate — an operator who rewrote the sentence
    while keeping `ortus spec` must stay green, so only its absence fails.
    """
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(
        monkeypatch, memory_text="Author issues with the usual ortus headings."
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    compact = _compact(result.stdout)
    assert "stale" in compact
    assert "bdremember" in compact
    assert f"--key{READINESS_MEMORY_KEY}" in compact


def test_check_accepts_edited_memory_that_keeps_the_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: an operator rewrite that still says `ortus spec` passes."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(
        monkeypatch, memory_text="House rule: run ortus spec before authoring."
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert f"key={READINESS_MEMORY_KEY}" in compact


def test_check_readiness_memory_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR-006: the memory query must not take any bd write path."""
    repo = _healthy_repo(tmp_path)
    seen: list[list[str]] = []
    real = _fake_bd_run(readiness_memory=True)

    def _record(args, *a, **k):
        seen.append(list(args))
        return real(args, *a, **k)

    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(check_mod.subprocess, "run", _record)
    _fake_sandbox_ok(monkeypatch)
    runner.invoke(app, ["check", str(repo)])
    memory_calls = [args for args in seen if "memories" in args]
    assert memory_calls, seen
    for args in memory_calls:
        assert "--readonly" in args and "--sandbox" in args, args
        assert "remember" not in args, args


@pytest.mark.codegraph_default
def test_codegraph_result_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: the table carries CLI, index, and MCP registration."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "codegraph" in compact
    assert "mode=required" in compact
    assert "CLI=ok" in compact
    assert "index=present" in compact
    assert "codegraphserverregistered" in compact


@pytest.mark.codegraph_default
def test_codegraph_required_missing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a required policy with no index fails and names the remediation.

    Asserted against the CheckResult rather than the table: Rich truncates a
    long Details cell, and the remediation text is the point of the row.
    """
    repo = _healthy_repo(tmp_path)
    (repo / ".codegraph").rmdir()
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = check_mod.check_codegraph(repo, "claude")
    assert not result.ok
    assert "index=missing" in result.message
    assert check_mod.CODEGRAPH_INDEX_HINT in result.message
    # and the verb still exits non-zero without raising
    assert runner.invoke(app, ["check", str(repo)]).exit_code == 1


@pytest.mark.codegraph_default
def test_codegraph_unregistered_mcp_is_reported_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A just-init'd repo has an index but no MCP registration ortus can read.

    Only the file-backed scopes are observable, so the row names the
    remediation without failing the check — the phase handshake enforces it.
    """
    repo = _healthy_repo(tmp_path)
    (repo / ".mcp.json").unlink()
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = check_mod.check_codegraph(repo, "claude")
    assert result.ok, result.message
    assert check_mod.CODEGRAPH_MCP_HINT in result.message
    assert runner.invoke(app, ["check", str(repo)]).exit_code == 0


@pytest.mark.codegraph_default
def test_codegraph_required_missing_cli_is_reported_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index present but CLI absent has a different remediation than the reverse."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    real_which = check_mod.shutil.which
    monkeypatch.setattr(
        check_mod.shutil,
        "which",
        lambda binary: None if binary == "codegraph" else real_which(binary),
    )
    result = check_mod.check_codegraph(repo, "claude")
    assert not result.ok
    assert "CLI=missing" in result.message
    assert "index=present" in result.message
    assert check_mod.CODEGRAPH_INSTALL_HINT in result.message
    assert check_mod.CODEGRAPH_INDEX_HINT not in result.message


def test_codegraph_off_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: `off` is an informational pass with no index or CLI requirement."""
    repo = _healthy_repo(tmp_path)
    (repo / ".codegraph").rmdir()
    (repo / ".ortusrc").write_text('codegraph = "off"\n')
    # The blocks teach the pinned policy, so a policy change re-renders them.
    _healthy_agent_files(repo)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "mode=off" in compact
    assert "FAIL" not in result.stdout


def test_codegraph_auto_reports_the_fallback_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto` keeps today's best-effort posture: reported, not failed."""
    repo = _healthy_repo(tmp_path)
    (repo / ".codegraph").rmdir()
    (repo / ".ortusrc").write_text('codegraph = "auto"\n')
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    assert runner.invoke(app, ["check", str(repo)]).exit_code == 0
    result = check_mod.check_codegraph(repo, "claude")
    assert result.ok
    assert "auto fallback" in result.message
    assert check_mod.CODEGRAPH_INDEX_HINT in result.message


def test_codegraph_invalid_mode_reports_rather_than_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unparseable policy is a FAIL row, matching check_ortusrc's posture."""
    repo = _healthy_repo(tmp_path)
    (repo / ".ortusrc").write_text('codegraph = "sometimes"\n')
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    compact = "".join(result.stdout.split())
    assert "invalidcodegraphmode" in compact


def test_codegraph_codex_registration_is_the_injected_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex needs no user MCP config: ortus injects the capability itself."""
    repo = tmp_path / "codex-graph"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codegraph").mkdir()
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    _healthy_agent_files(repo)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "injectedperchildbyortus" in compact


# --- managed AGENTS.md / CLAUDE.md blocks ----------------------------------


def test_check_reports_the_managed_blocks_as_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = "".join(result.stdout.split())
    assert "AGENTS.md" in compact
    assert "CLAUDE.md" in compact
    assert "block=agentsschema=1current" in compact


@pytest.mark.parametrize("filename", ["AGENTS.md", "CLAUDE.md"])
def test_check_fails_when_an_agent_file_has_no_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    """AC-2: host prose with no ortus block is a failure with a repair command."""
    repo = _healthy_repo(tmp_path)
    (repo / filename).write_text("# House rules\n", encoding="utf-8")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    compact = "".join(result.stdout.split())
    assert "ortusinit--force" in compact
    assert "FAIL" in result.stdout


def test_check_fails_when_an_agent_file_is_gitignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a hidden AGENTS.md is a contract nobody else in the repo receives."""
    repo = _healthy_repo(tmp_path)
    (repo / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "gitignored" in "".join(result.stdout.split())


def test_check_reports_same_schema_content_drift(tmp_path: Path) -> None:
    """AC-2: an edited body at the current schema says to re-run init."""
    repo = _healthy_repo(tmp_path)
    edited = render_block("agents").replace(
        "All work goes through bd.", "All work goes through vibes."
    )
    (repo / "AGENTS.md").write_text(edited + "\n", encoding="utf-8")
    result = check_mod.check_agent_file(repo, MANAGED_FILES[0])
    assert not result.ok
    assert "content drift" in result.message
    assert "ortus init --force" in result.message


def test_check_leaves_a_newer_schema_alone_with_a_warning(tmp_path: Path) -> None:
    """A block from a newer ortus is reported, never failed or rewritten."""
    repo = _healthy_repo(tmp_path)
    (repo / "AGENTS.md").write_text(
        "<!-- BEGIN ortus block=agents schema=99 generated-by=ortus@9.9.9 -->\n"
        "from the future\n"
        "<!-- END ortus block=agents -->\n",
        encoding="utf-8",
    )
    result = check_mod.check_agent_file(repo, MANAGED_FILES[0])
    assert result.ok
    assert "warning" in result.message
    assert "upgrade ortus" in result.message


def test_check_reports_malformed_markers_with_a_line_number(tmp_path: Path) -> None:
    repo = _healthy_repo(tmp_path)
    (repo / "AGENTS.md").write_text(
        "# House rules\n\n<!-- BEGIN ortus block=agents schema=1 -->\nbody\n",
        encoding="utf-8",
    )
    result = check_mod.check_agent_file(repo, MANAGED_FILES[0])
    assert not result.ok
    assert "malformed markers" in result.message
    assert "AGENTS.md:3" in result.message


def test_check_reports_duplicate_headings_as_info(tmp_path: Path) -> None:
    """AC-2: a stale pre-marker copy earns an info row naming its headings."""
    path = _healthy_repo(tmp_path) / "AGENTS.md"
    path.write_text(
        "### Session-close protocol\n\nstale copy\n\n"
        + path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    row = check_mod.check_agent_file_duplicates(tmp_path / "healthy", MANAGED_FILES[0])
    assert row is not None
    assert not row.ok
    assert row.level == "info"
    assert "duplicates managed-block headings" in row.message
    assert "Session-close protocol" in row.message
    assert "outside the ortus markers" in row.message


def test_check_agent_file_duplicates_adds_no_row_when_clean(tmp_path: Path) -> None:
    """Repos with clean files see no new output."""
    repo = _healthy_repo(tmp_path)
    for managed in MANAGED_FILES:
        assert check_mod.check_agent_file_duplicates(repo, managed) is None


def test_check_duplicate_headings_warn_keeps_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: the row renders as WARN and never drives the exit code."""
    repo = _healthy_repo(tmp_path)
    path = repo / "AGENTS.md"
    path.write_text(
        "### Session-close protocol\n\nstale copy\n\n"
        + path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    compact = _compact(result.stdout)
    assert "duplicatesmanaged-blockheadings" in compact
    # The strict row for the same file still reports the block as current.
    assert "block=agentsschema=1current" in compact


def test_check_agent_files_make_no_writes(tmp_path: Path) -> None:
    """NFR-006: the strict block check reads; `ortus init` is what repairs."""
    repo = _healthy_repo(tmp_path)
    (repo / "CLAUDE.md").unlink()
    before = _snapshot_mtimes(repo)
    for managed in MANAGED_FILES:
        check_mod.check_agent_file(repo, managed)
    assert _snapshot_mtimes(repo) == before


def _healthy_grok_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "grok-healthy"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codegraph").mkdir()
    grok_cfg = repo / ".grok" / "config.toml"
    grok_cfg.parent.mkdir()
    grok_cfg.write_text(
        "[mcp_servers.codegraph]\n"
        'command = "codegraph"\n'
        'args = ["serve", "--mcp"]\n'
        "enabled = true\n"
    )
    (repo / ".ortusrc").write_text('backend = "grok"\n')
    _healthy_agent_files(repo)
    return repo


def test_check_grok_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: `ortus check --backend grok` reports the grok binary and config."""
    repo = _healthy_grok_repo(tmp_path)
    seen: list[str] = []

    def which(binary: str) -> str:
        seen.append(binary)
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(check_mod.shutil, "which", which)
    monkeypatch.setattr(
        check_mod.subprocess, "run", _fake_bd_run(readiness_memory=True)
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo), "--backend", "grok"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "grok" in seen
    assert "claude" not in seen
    assert ".grok/config.toml" in result.stdout
    assert ".claude/settings.json" not in result.stdout
    assert "hooks" not in result.stdout
    compact = "".join(result.stdout.split())
    assert "CLI=ok" in compact
    assert "index=present" in compact
    assert "codegraphserverregistered" in compact


def test_check_grok_binary_missing_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing `grok` on PATH fails the same way missing `claude` does."""
    repo = _healthy_grok_repo(tmp_path)

    def which(binary: str) -> str | None:
        if binary == "grok":
            return None
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(check_mod.shutil, "which", which)
    monkeypatch.setattr(
        check_mod.subprocess, "run", _fake_bd_run(readiness_memory=True)
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo), "--backend", "grok"])
    assert result.exit_code == 1
    compact = "".join(c for c in result.stdout if not c.isspace() and c != "│")
    assert "grok" in compact
    assert "notonPATH" in compact
    assert "FAIL" in result.stdout


def test_check_grok_binary_on_claude_tree_fails_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Claude-inited tree checked as grok fails the Grok settings probe."""
    repo = _healthy_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo), "--backend", "grok"])
    assert result.exit_code == 1
    assert ".grok/config.toml" in result.stdout
    assert "FAIL" in result.stdout
    assert "missing" in result.stdout


def test_check_grok_codegraph_ignores_claude_mcp_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grok MCP reporting is file-backed project config, not Claude scopes."""
    repo = _healthy_grok_repo(tmp_path)
    (repo / ".grok" / "config.toml").write_text("# no mcp_servers on purpose\n")
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"codegraph": {"command": "codegraph"}}})
    )
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    result = check_mod.check_codegraph(repo, "grok")
    assert result.ok, result.message
    assert "CLI=ok" in result.message
    assert "index=present" in result.message
    assert check_mod.CODEGRAPH_MCP_HINT in result.message
    assert "codegraph server registered" not in result.message


def test_check_codex_uses_codex_binary_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "codex-project"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codegraph").mkdir()
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text('backend = "codex"\n')
    _healthy_agent_files(repo)
    seen: list[str] = []

    def which(binary: str) -> str:
        seen.append(binary)
        return f"/usr/bin/{binary}"

    monkeypatch.setattr(check_mod.shutil, "which", which)
    monkeypatch.setattr(
        check_mod.subprocess, "run", _fake_bd_run(readiness_memory=True)
    )
    _fake_sandbox_ok(monkeypatch)
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "codex" in seen
    assert "claude" not in seen
    assert ".codex/config.toml" in result.stdout
    assert "hooks" not in result.stdout


# --- local backend rows ----------------------------------------------------


_LOCAL_TABLE = '[local]\nbase_url = "http://127.0.0.1:8080/v1"\nmodel = "qwen3:4b"\n'
_LOCAL_CONFIG = LocalConfig(base_url="http://127.0.0.1:8080/v1", model="qwen3:4b")


def _local_repo(
    tmp_path: Path, *, ortusrc: str = 'backend = "local"\n' + _LOCAL_TABLE
) -> Path:
    """A codex-provisioned repo whose `.ortusrc` is whatever the test needs."""
    repo = tmp_path / "local-project"
    (repo / ".beads").mkdir(parents=True)
    (repo / ".codegraph").mkdir()
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text(
        'sandbox_mode = "workspace-write"\napproval_policy = "never"\n'
    )
    (repo / ".ortusrc").write_text(ortusrc)
    _healthy_agent_files(repo)
    return repo


def _patch_probes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: object = ("qwen3:4b",),
    tools: object = None,
    context: object = 32768,
) -> list[str]:
    """Replace the three probe seams in `check.py`; returns the call order.

    Each outcome is returned from its fake probe, or raised when it is an
    exception, so one helper covers the green path and every verdict.
    """
    calls: list[str] = []

    def _seam(name: str, outcome: object):
        def _probe(config, **kwargs):
            calls.append(name)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        return _probe

    monkeypatch.setattr(check_mod, "probe_models", _seam("models", models))
    monkeypatch.setattr(check_mod, "probe_tool_calling", _seam("tools", tools))
    monkeypatch.setattr(check_mod, "probe_context_size", _seam("context", context))
    return calls


def _never_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if any local probe touches the (absent) server."""

    def _boom(config, **kwargs):
        raise AssertionError("no local probe may run here")

    for seam in ("probe_models", "probe_tool_calling", "probe_context_size"):
        monkeypatch.setattr(check_mod, seam, _boom)


def test_check_help_lists_local_backend() -> None:
    """AC-1: `local` is a backend the verb admits to knowing."""
    result = runner.invoke(
        app, ["check", "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}
    )
    assert result.exit_code == 0
    assert "claude|codex|grok|local" in result.stdout


def test_check_local_all_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: four local rows follow `.codex/config.toml`; the verb exits 0."""
    repo = _local_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    calls = _patch_probes(monkeypatch)
    results = check_mod._run_all(repo, "local")
    names = [r.name for r in results]
    start = names.index(".codex/config.toml") + 1
    assert tuple(names[start : start + 4]) == check_mod.LOCAL_ROW_NAMES
    assert all(r.ok for r in results), [(r.name, r.message) for r in results if not r.ok]
    assert calls == ["models", "tools", "context"]
    rows = {r.name: r for r in results}
    assert rows["[local]"].message == (
        "base_url=http://127.0.0.1:8080/v1 model=qwen3:4b key=none"
    )
    assert rows["local endpoint"].message == "reachable; model qwen3:4b served"
    assert rows["local tools"].message == "function call returned"
    assert rows["local context"].message == "n_ctx=32768"
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    # The `[local]` row name prints literally and the status glyph keeps its
    # styling instead of arriving as escaped markup.
    assert "[local]" in result.stdout
    assert "✓" in result.stdout
    assert "[green]" not in result.stdout
    assert "localendpoint" in _compact(result.stdout)


def test_check_local_server_down_fails_with_serving_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: a refused connection names the serving command and exits 1."""
    repo = _local_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    down = LocalServerError(
        "unreachable",
        "local server unreachable at http://127.0.0.1:8080/v1: Connection refused",
        serving_hint(_LOCAL_CONFIG),
    )
    calls = _patch_probes(monkeypatch, models=down)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    endpoint = rows["local endpoint"]
    assert not endpoint.ok
    assert "Connection refused" in endpoint.message
    assert "llama-server" in endpoint.message
    assert "--jinja" in endpoint.message
    assert not rows["local tools"].ok
    assert rows["local tools"].message == "skipped: endpoint failed"
    assert not rows["local context"].ok
    assert rows["local context"].message == "skipped: endpoint failed"
    assert rows["local context"].level == "info"
    assert calls == ["models"]
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout


def test_check_local_wrong_model_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the served ids are in the row, so the fix is a copy-paste."""
    repo = _local_repo(tmp_path)
    missing = LocalServerError(
        "model-missing",
        "model 'qwen3:4b' is not served; served: gemma4:26b, qwen3-coder:30b",
        "set local.model to a served id or load the model",
    )
    calls = _patch_probes(monkeypatch, models=missing)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    assert not rows["local endpoint"].ok
    assert "gemma4:26b, qwen3-coder:30b" in rows["local endpoint"].message
    assert "set local.model" in rows["local endpoint"].message
    assert calls == ["models"]


def test_check_local_tools_unsupported_names_jinja(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: a server that narrates instead of calling is a `--jinja` FAIL."""
    repo = _local_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    narrated = LocalServerError(
        "tools-unsupported",
        "server answered without calling the tool",
        "restart llama-server with --jinja (and a tool-capable chat template)",
    )
    calls = _patch_probes(monkeypatch, tools=narrated)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    assert rows["local endpoint"].ok
    assert not rows["local tools"].ok
    assert "--jinja" in rows["local tools"].message
    # A tools failure is not an outage: the context row still reports.
    assert rows["local context"].ok
    assert calls == ["models", "tools", "context"]
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout


def test_check_local_small_context_warns_not_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: a window under the recommendation is a WARN row and exit 0."""
    repo = _local_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    _patch_probes(monkeypatch, context=8192)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    context = rows["local context"]
    assert not context.ok
    assert context.level == "info"
    assert "n_ctx=8192" in context.message
    assert "--ctx-size 32768" in context.message
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout
    assert "FAIL" not in result.stdout


def test_check_local_context_not_exposed_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama has no `/props`: an unknown window is a PASS, not a guess."""
    repo = _local_repo(tmp_path)
    _patch_probes(monkeypatch, context=None)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    assert rows["local context"].ok
    assert rows["local context"].level == "info"
    assert "not exposed" in rows["local context"].message


def test_check_local_invalid_config_skips_server_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a bad table fails the first row and probes nothing."""
    repo = _local_repo(
        tmp_path,
        ortusrc='[local]\nbase_url = "127.0.0.1:8080/v1"\nmodel = "qwen3:4b"\n',
    )
    _never_probe(monkeypatch)
    rows = check_mod.check_local_rows(repo)
    assert [r.name for r in rows] == list(check_mod.LOCAL_ROW_NAMES)
    assert not rows[0].ok
    assert "local.base_url" in rows[0].message
    for row in rows[1:]:
        assert not row.ok
        assert row.message == "skipped: [local] config invalid"
    assert rows[1].level == "strict"
    assert rows[3].level == "info"


def test_check_local_missing_api_key_env_fails_config_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named key variable that is not exported fails the config row by name.

    The value, once exported, is never printed: the row shows the variable
    name and the probes read it only into a request header.
    """
    repo = _local_repo(
        tmp_path, ortusrc=_LOCAL_TABLE + 'api_key_env = "ORTUS_TEST_LOCAL_KEY"\n'
    )
    monkeypatch.delenv("ORTUS_TEST_LOCAL_KEY", raising=False)
    _patch_probes(monkeypatch)
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    assert not rows["[local]"].ok
    assert "api_key_env=ORTUS_TEST_LOCAL_KEY is not set" in rows["[local]"].message
    # The probes still run: the server may not demand the key at all.
    assert rows["local endpoint"].ok

    monkeypatch.setenv("ORTUS_TEST_LOCAL_KEY", "hunter2-never-printed")
    rows = {r.name: r for r in check_mod.check_local_rows(repo)}
    assert rows["[local]"].ok
    assert "key=ORTUS_TEST_LOCAL_KEY" in rows["[local]"].message
    assert "hunter2" not in " ".join(r.message for r in rows.values())


def test_check_local_provisioned_row_never_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: a codex run backend reports `[local]` as provisioned, offline."""
    repo = _local_repo(tmp_path, ortusrc='backend = "codex"\n' + _LOCAL_TABLE)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    _never_probe(monkeypatch)
    row = check_mod.check_provisioned_backend(repo, "local")
    assert row.ok
    assert row.level == "info"
    assert "not probed" in row.message
    assert "ortus check --backend local" in row.message
    result = runner.invoke(app, ["check", str(repo)])
    assert result.exit_code == 0, result.stdout + result.stderr
    compact = _compact(result.stdout)
    assert "local(provisioned)" in compact
    assert "localendpoint" not in compact


def test_check_local_provisioned_row_names_the_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The offline row still catches a missing codex config or a bad table."""
    repo = _local_repo(
        tmp_path, ortusrc='backend = "codex"\n[local]\nmodel = "two words"\n'
    )
    (repo / ".codex" / "config.toml").unlink()
    monkeypatch.setattr(check_mod.shutil, "which", lambda binary: None)
    _never_probe(monkeypatch)
    row = check_mod.check_provisioned_backend(repo, "local")
    assert not row.ok
    assert row.level == "info"
    assert ".codex/config.toml missing" in row.message
    assert "codex CLI not on PATH" in row.message
    assert "local.model" in row.message
    assert "ortus init --backend local" in row.message


def test_check_codex_table_has_no_local_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: a codex repo without a `[local]` table renders exactly as before."""
    repo = _local_repo(tmp_path, ortusrc='backend = "codex"\n')
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    _never_probe(monkeypatch)
    results = check_mod._run_all(repo, "codex")
    assert not any("local" in r.name for r in results)


def test_codegraph_local_uses_injected_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: local is the Codex CLI, so CodeGraph is injected per child too."""
    repo = _local_repo(tmp_path)
    _all_binaries_present(monkeypatch)
    _fake_sandbox_ok(monkeypatch)
    _patch_probes(monkeypatch)
    local = check_mod.check_codegraph(repo, "local")
    assert local.ok, local.message
    assert local.message == check_mod.check_codegraph(repo, "codex").message
    assert "injected per child by ortus" in local.message
    result = runner.invoke(app, ["check", str(repo), "--backend", "local"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "injectedperchildbyortus" in _compact(result.stdout)


def test_backend_provisioned_opencode_is_the_file_itself(tmp_path: Path) -> None:
    """opencode.json sits at the repo root, so its parent dir proves nothing."""
    repo = _healthy_repo(tmp_path)
    assert not check_mod.backend_provisioned(repo, "opencode")
    (repo / "opencode.json").write_text('{"provider": {}}\n')
    assert check_mod.backend_provisioned(repo, "opencode")
