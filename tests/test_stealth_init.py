"""`ortus init --stealth`: Ortus in a fork without a trace in git.

Every test drives a real git repository shaped like a fork (history, an
upstream `.gitignore`, an origin remote). bd itself is stubbed at the init
seams: the stealth contract under test is what Ortus does around bd, and bd's
own `--stealth` behavior was probed by hand against 1.2.1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.check import check_agent_file, check_tracker_hooks
from ortus.core.agent_files import MANAGED_FILES, AgentFileError, BlockOutcome, read_block
from ortus.core.config import resolve_commit_issue_ref
from ortus.core.stealth import (
    STEALTH_EXCLUDE_PATTERNS,
    bd_hooks_path,
    exclude_path,
    is_stealth,
    merge_exclude,
    render_exclude_block,
    tracked_ortus_paths,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

pytestmark = pytest.mark.integration
runner = CliRunner()

ORIGIN_URL = "https://github.com/upstream/project.git"
UPSTREAM_GITIGNORE = "node_modules/\n*.pyc\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def fork(tmp_path: Path) -> Path:
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(UPSTREAM_GITIGNORE, encoding="utf-8")
    (repo / "README.md").write_text("upstream\n", encoding="utf-8")
    _commit_all(repo, "upstream history")
    _git(repo, "remote", "add", "origin", ORIGIN_URL)
    return repo


@pytest.fixture(autouse=True)
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Stub bd, CodeGraph and backend lookups at init's seams."""
    import ortus.commands.init as init_mod

    monkeypatch.delenv("BEADS_DIR", raising=False)
    seen: dict[str, list] = {"bd_init_stealth": [], "no_git_ops": [], "remote": []}

    def bd_init_stealth(repo: Path, prefix: str | None) -> None:
        seen["bd_init_stealth"].append(prefix)
        (repo / ".beads").mkdir()
        (repo / ".beads" / "config.yaml").write_text("no-git-ops: true\n")

    def plain_bd_init(repo: Path, prefix: str | None) -> None:
        raise AssertionError("stealth init ran the plain bd init")

    monkeypatch.setattr(init_mod, "_bd_init_stealth", bd_init_stealth)
    monkeypatch.setattr(init_mod, "_bd_init", plain_bd_init)
    monkeypatch.setattr(
        init_mod, "_bd_no_git_ops", lambda repo: seen["no_git_ops"].append(repo)
    )
    monkeypatch.setattr(
        init_mod, "_bd_add_tracker_remote", lambda repo, url: seen["remote"].append(url)
    )
    monkeypatch.setattr(init_mod, "_bd_remember", lambda repo: None)
    monkeypatch.setattr(
        init_mod, "_bd_hooks_install", lambda repo: pytest.fail("hooks installed")
    )
    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kw: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")
    return seen


def _init(repo: Path, *extra: str):
    result = runner.invoke(app, ["init", str(repo), *extra])
    return result, (result.stdout or "") + (result.stderr or "")


def _ortusrc(repo: Path) -> dict:
    return tomllib.loads((repo / ".ortusrc").read_text(encoding="utf-8"))


def _exclude_text(repo: Path) -> str:
    return (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")


# AC-1


def test_fork_like_stealth_init_leaves_git_untouched(fork: Path) -> None:
    head = _git(fork, "rev-parse", "HEAD")
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert _git(fork, "rev-parse", "HEAD") == head
    assert (fork / ".gitignore").read_text(encoding="utf-8") == UPSTREAM_GITIGNORE
    assert subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=fork, capture_output=True
    ).returncode == 1
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""
    assert tracked_ortus_paths(fork) == ()
    assert _exclude_text(fork).count("# BEGIN ortus stealth v1") == 1
    assert _ortusrc(fork)["stealth"] is True
    assert _git(fork, "config", "--local", "--get", "ortus.stealth") == "true"
    # The files Ortus wrote are there, just invisible to git.
    assert (fork / "AGENTS.md").is_file() and (fork / ".ortusrc").is_file()
    assert is_stealth(fork)


# AC-2


def test_local_only_alias_matches_stealth(fork: Path, calls: dict) -> None:
    result, out = _init(fork, "--local-only")
    assert result.exit_code == 0, out
    assert calls["bd_init_stealth"] == ["fork"]
    assert _ortusrc(fork)["stealth"] is True
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""


def test_force_rerun_is_idempotent(fork: Path, calls: dict) -> None:
    assert _init(fork, "--stealth")[0].exit_code == 0
    before = _exclude_text(fork)
    # No flag on the re-run: the recorded `stealth = true` keeps it stealthy.
    result, out = _init(fork, "--force")
    assert result.exit_code == 0, out
    assert calls["bd_init_stealth"] == ["fork"]
    assert len(calls["no_git_ops"]) == 1
    assert _exclude_text(fork) == before
    assert _exclude_text(fork).count("# BEGIN ortus stealth") == 1
    assert "already current" in out
    assert not (fork / ".gitignore").read_text(encoding="utf-8").count("ortus")
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""


# AC-3


def test_stale_bd_hookspath_is_unset(fork: Path) -> None:
    _git(fork, "config", "core.hooksPath", str(fork / ".beads" / "hooks"))
    assert bd_hooks_path(fork)
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert "unset core.hooksPath" in out
    assert bd_hooks_path(fork) == ""
    assert subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=fork, capture_output=True
    ).returncode == 1


def test_relative_bd_hookspath_is_recognized(fork: Path) -> None:
    _git(fork, "config", "core.hooksPath", ".beads/hooks/")
    assert bd_hooks_path(fork) == ".beads/hooks/"


def test_foreign_hookspath_is_kept(fork: Path) -> None:
    _git(fork, "config", "core.hooksPath", ".husky")
    assert bd_hooks_path(fork) == ""
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert _git(fork, "config", "--get", "core.hooksPath") == ".husky"


# AC-4


def test_tracked_upstream_agent_files_redirect(fork: Path) -> None:
    (fork / "AGENTS.md").write_text("# Upstream agents\n", encoding="utf-8")
    (fork / "CLAUDE.md").write_text("# Upstream claude\n", encoding="utf-8")
    _commit_all(fork, "upstream agent files")
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert (fork / "AGENTS.md").read_text(encoding="utf-8") == "# Upstream agents\n"
    assert (fork / "CLAUDE.md").read_text(encoding="utf-8") == "# Upstream claude\n"
    assert read_block(fork / "AGENTS.override.md", "agents") is not None
    assert read_block(fork / "CLAUDE.local.md", "pointer") is not None
    lines = _exclude_text(fork).splitlines()
    assert "/AGENTS.md" not in lines and "/CLAUDE.md" not in lines
    assert "/AGENTS.override.md" in lines and "/CLAUDE.local.md" in lines
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""


def test_tracked_upstream_claude_dir_writes_only_settings_local(fork: Path) -> None:
    (fork / ".claude").mkdir()
    (fork / ".claude" / "settings.json").write_text('{"upstream": true}\n')
    _commit_all(fork, "upstream claude settings")
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert (fork / ".claude" / "settings.json").read_text() == '{"upstream": true}\n'
    assert (fork / ".claude" / "settings.local.json").is_file()
    lines = _exclude_text(fork).splitlines()
    assert "/.claude/" not in lines
    assert "/.claude/settings.local.json" in lines
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""


def test_tracked_upstream_config_of_run_backend_is_refused(fork: Path) -> None:
    (fork / ".codex").mkdir()
    (fork / ".codex" / "config.toml").write_text("model = 'x'\n")
    _commit_all(fork, "upstream codex config")
    result, out = _init(fork, "--stealth", "--backend", "codex")
    assert result.exit_code == 1
    assert ".codex/config.toml" in out
    assert not (fork / ".beads").exists()


def test_tracked_upstream_config_of_other_backend_is_skipped(fork: Path) -> None:
    (fork / ".codex").mkdir()
    (fork / ".codex" / "config.toml").write_text("model = 'x'\n")
    _commit_all(fork, "upstream codex config")
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    assert "upstream tracks .codex/config.toml; skipped" in out
    assert (fork / ".codex" / "config.toml").read_text() == "model = 'x'\n"
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""


# AC-5


def test_settings_local_carries_no_attribution(fork: Path) -> None:
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    settings = json.loads((fork / ".claude" / "settings.local.json").read_text())
    assert settings["includeCoAuthoredBy"] is False
    assert settings["attribution"] == {"commit": "", "pr": ""}


def test_settings_local_preserves_other_keys(fork: Path) -> None:
    (fork / ".claude").mkdir()
    (fork / ".claude" / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "attribution": {"x": 1}})
    )
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 0, out
    settings = json.loads((fork / ".claude" / "settings.local.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    assert settings["includeCoAuthoredBy"] is False
    assert settings["attribution"] == {"x": 1, "commit": "", "pr": ""}


# AC-6


@pytest.mark.parametrize(
    "url", [ORIGIN_URL, "https://github.com/upstream/project", ORIGIN_URL + "/"]
)
def test_tracker_remote_equal_to_origin_is_refused(fork: Path, url: str) -> None:
    result, out = _init(fork, "--stealth", "--tracker-remote", url)
    assert result.exit_code == 1
    assert "origin" in out
    assert not (fork / ".beads").exists()


def test_tracker_remote_equal_to_upstream_is_refused(fork: Path) -> None:
    _git(fork, "remote", "add", "upstream", "git@github.com:real/project.git")
    result, out = _init(
        fork, "--stealth", "--tracker-remote", "git@github.com:real/project"
    )
    assert result.exit_code == 1
    assert "upstream" in out


def test_tracker_remote_file_url_is_added(fork: Path, tmp_path: Path, calls: dict) -> None:
    url = (tmp_path / "private-tracker").as_uri()
    result, out = _init(fork, "--stealth", "--tracker-remote", url)
    assert result.exit_code == 0, out
    assert calls["remote"] == [url]
    assert _ortusrc(fork)["tracker_remote"] == url
    # A re-run keeps the recorded remote and does not add it twice.
    assert _init(fork, "--force")[0].exit_code == 0
    assert calls["remote"] == [url]
    assert _ortusrc(fork)["tracker_remote"] == url


def test_tracker_remote_without_stealth_is_usage_error(fork: Path) -> None:
    result, out = _init(fork, "--tracker-remote", "file:///tmp/x")
    assert result.exit_code == 1
    assert "only accepted with --stealth" in out


def test_tracker_remote_name_is_ortus_private(fork: Path) -> None:
    import ortus.commands.init as init_mod

    assert init_mod.TRACKER_REMOTE_NAME == "ortus-private"


# AC-7


def test_commit_issue_ref_defaults() -> None:
    assert resolve_commit_issue_ref({}, stealth=True) == "none"
    assert resolve_commit_issue_ref({}, stealth=False) == "subject"
    assert resolve_commit_issue_ref({"commit_issue_ref": "trailer"}, stealth=True) == "trailer"
    assert resolve_commit_issue_ref({"commit_issue_ref": "subject"}, stealth=True) == "subject"


def test_commit_issue_ref_rejects_other_values(fork: Path) -> None:
    with pytest.raises(ValueError, match="commit_issue_ref"):
        resolve_commit_issue_ref({"commit_issue_ref": "footer"}, stealth=False)
    result, out = _init(fork, "--stealth", "--commit-issue-ref", "footer")
    assert result.exit_code == 1
    assert "--commit-issue-ref" in out


def test_commit_issue_ref_is_recorded(fork: Path) -> None:
    assert _init(fork, "--stealth")[0].exit_code == 0
    assert _ortusrc(fork)["commit_issue_ref"] == "none"
    result, out = _init(fork, "--force", "--commit-issue-ref", "trailer")
    assert result.exit_code == 0, out
    assert _ortusrc(fork)["commit_issue_ref"] == "trailer"
    assert _init(fork, "--force")[0].exit_code == 0
    assert _ortusrc(fork)["commit_issue_ref"] == "trailer"


# AC-8


def test_check_accepts_excluded_agent_files_in_stealth(fork: Path) -> None:
    assert _init(fork, "--stealth")[0].exit_code == 0
    for managed in MANAGED_FILES:
        row = check_agent_file(fork, managed)
        assert row.ok, row.message


def test_check_reads_redirected_agent_files(fork: Path) -> None:
    (fork / "AGENTS.md").write_text("# Upstream agents\n", encoding="utf-8")
    _commit_all(fork, "upstream agents")
    assert _init(fork, "--stealth")[0].exit_code == 0
    row = check_agent_file(fork, MANAGED_FILES[0])
    assert row.ok, row.message
    assert row.name == "AGENTS.override.md"


def test_check_reports_tracker_hooks_disabled_in_stealth(fork: Path) -> None:
    assert _init(fork, "--stealth")[0].exit_code == 0
    row = check_tracker_hooks(fork)
    assert row.ok and row.message == "disabled (stealth)"


def test_check_stealth_from_git_config_alone(fork: Path) -> None:
    _git(fork, "config", "--local", "ortus.stealth", "true")
    assert is_stealth(fork)
    assert check_tracker_hooks(fork).message == "disabled (stealth)"


def test_check_rejects_excluded_agent_file_outside_stealth(fork: Path) -> None:
    (fork / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (fork / ".git" / "info" / "exclude").write_text("/AGENTS.md\n")
    row = check_agent_file(fork, MANAGED_FILES[0])
    assert not row.ok and "gitignored" in row.message


# Preflight and exclude-block edge cases


def test_stealth_refuses_a_directory_that_is_not_git(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result, out = _init(plain, "--stealth")
    assert result.exit_code == 1
    assert "not a git work tree" in out
    assert list(plain.iterdir()) == []


def test_stealth_refuses_a_repo_with_no_commits(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    result, out = _init(empty, "--stealth")
    assert result.exit_code == 1
    assert "no commits" in out


def test_stealth_refuses_unpushed_bd_init_commit(fork: Path) -> None:
    (fork / ".gitignore").write_text(UPSTREAM_GITIGNORE + ".beads/\n")
    _commit_all(fork, "bd init: initialize beads issue tracking")
    head = _git(fork, "rev-parse", "HEAD")
    result, out = _init(fork, "--stealth")
    assert result.exit_code == 1
    assert "git reset --soft HEAD~1" in out
    assert "git checkout origin/main -- .gitignore" in out
    assert _git(fork, "rev-parse", "HEAD") == head


def test_exclude_block_merges_beside_existing_lines(fork: Path) -> None:
    exclude = exclude_path(fork)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# Beads stealth mode\n.beads/\n*.log", encoding="utf-8")
    assert merge_exclude(fork, ["/a/"]) is BlockOutcome.APPENDED
    text = exclude.read_text(encoding="utf-8")
    assert text.startswith("# Beads stealth mode\n.beads/\n*.log\n\n")
    assert merge_exclude(fork, ["/a/"]) is BlockOutcome.UNCHANGED
    assert merge_exclude(fork, ["/a/", "/b/"]) is BlockOutcome.UPDATED
    assert exclude.read_text(encoding="utf-8").startswith("# Beads stealth mode\n")


def test_exclude_block_from_newer_ortus_is_left_alone(fork: Path) -> None:
    exclude = exclude_path(fork)
    newer = render_exclude_block(["/x/"]).replace("schema=1", "schema=9")
    exclude.write_text(newer + "\n", encoding="utf-8")
    assert merge_exclude(fork, list(STEALTH_EXCLUDE_PATTERNS)) is BlockOutcome.AHEAD
    assert exclude.read_text(encoding="utf-8") == newer + "\n"


def test_exclude_block_malformed_markers_fail(fork: Path) -> None:
    exclude = exclude_path(fork)
    exclude.write_text(
        render_exclude_block(["/x/"]).rsplit("\n", 1)[0] + "\n", encoding="utf-8"
    )
    with pytest.raises(AgentFileError):
        merge_exclude(fork, ["/x/"])


def test_exclude_block_goes_to_the_common_dir_of_a_worktree(fork: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    _git(fork, "worktree", "add", "-q", str(linked))
    assert exclude_path(linked).resolve() == (fork / ".git" / "info" / "exclude").resolve()
    merge_exclude(linked, ["/x/"])
    assert "/x/" in _exclude_text(fork).splitlines()


def test_extra_excludes_are_appended(fork: Path) -> None:
    assert _init(fork, "--stealth")[0].exit_code == 0
    rc = (fork / ".ortusrc").read_text(encoding="utf-8")
    assert '# stealth_extra_excludes = ["/notes/"]' in rc
    (fork / ".ortusrc").write_text(
        rc.replace('# stealth_extra_excludes = ["/notes/"]', 'stealth_extra_excludes = ["/notes/"]')
    )
    (fork / "notes").mkdir()
    (fork / "notes" / "todo.txt").write_text("x\n")
    result, out = _init(fork, "--force")
    assert result.exit_code == 0, out
    assert "/notes/" in _exclude_text(fork).splitlines()
    assert _ortusrc(fork)["stealth_extra_excludes"] == ["/notes/"]
    assert _git(fork, "status", "--porcelain", "--untracked-files=all") == ""
