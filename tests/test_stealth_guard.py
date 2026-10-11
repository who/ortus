"""The stealth push guard: nothing Ortus-shaped leaves a stealth fork.

Every test drives real git: a fork-shaped checkout whose origin is a bare
repository in `tmp_path`, so a refused push can be proven by origin's refs
staying exactly where they were. The installed hooks call the `ortus` on
PATH, which is this checkout's own entry point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import check as check_mod
from ortus.commands.check import check_stealth_rows
from ortus.commands.grind import _enforce_branch_discipline
from ortus.core.git import GitClient
from ortus.core.stealth import (
    STEALTH_EXCLUDE_PATTERNS,
    STEALTH_HOOK_MARKER,
    ZERO_SHA,
    install_stealth_hooks,
    merge_exclude,
    push_violations,
    staged_gitignore_violation,
)

pytestmark = pytest.mark.integration
runner = CliRunner()

PREFIX = "tal"


def _run(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env
    )


def _git(repo: Path, *args: str) -> str:
    proc = _run(repo, *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _commit(repo: Path, name: str, message: str, *, force: bool = False) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(f"{message}\n", encoding="utf-8")
    _git(repo, "add", *(["-f"] if force else []), name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_ortusrc(repo: Path, issue_ref: str | None = None) -> None:
    lines = ["stealth = true", f'prefix = "{PREFIX}"']
    if issue_ref is not None:
        lines.append(f'commit_issue_ref = "{issue_ref}"')
    (repo / ".ortusrc").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _origin_refs(origin: Path) -> str:
    return _git(origin, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture(autouse=True)
def ortus_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hooks exec `ortus`; put this interpreter's scripts first on PATH."""
    bindir = Path(sys.executable).parent
    if not (bindir / "ortus").exists():
        pytest.skip("no ortus entry point beside the interpreter")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("ORTUS_STEALTH_ALLOW_GITIGNORE", raising=False)
    monkeypatch.delenv("BEADS_DIR", raising=False)


@pytest.fixture()
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    return bare


@pytest.fixture()
def fork(tmp_path: Path, origin: Path) -> Path:
    """A stealth fork whose upstream history is already on origin."""
    repo = tmp_path / "fork"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _commit(repo, "README.md", "upstream history")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "origin", "main")
    _write_ortusrc(repo)
    merge_exclude(repo, STEALTH_EXCLUDE_PATTERNS)
    _git(repo, "config", "--local", "ortus.stealth", "true")
    (repo / ".beads").mkdir()
    (repo / ".beads" / "issues.jsonl").write_text("{}\n", encoding="utf-8")
    assert install_stealth_hooks(repo) == []
    return repo


def _refspec(repo: Path, remote_sha: str | None = None) -> list[tuple[str, str, str, str]]:
    local = _git(repo, "rev-parse", "HEAD")
    if remote_sha is None:
        remote_sha = _git(repo, "rev-parse", "refs/remotes/origin/main")
    return [("refs/heads/main", local, "refs/heads/main", remote_sha)]


# AC-1


def test_force_added_tracker_file_blocks_the_push(fork: Path, origin: Path) -> None:
    before = _origin_refs(origin)
    _commit(fork, ".beads/issues.jsonl", "Record data", force=True)

    violations = push_violations(fork, "origin", _refspec(fork))
    assert any(".beads/issues.jsonl" in v and "git rm --cached" in v for v in violations)

    proc = _run(fork, "push", "origin", "main")
    assert proc.returncode != 0
    assert ".beads/issues.jsonl" in proc.stderr
    assert _origin_refs(origin) == before


def test_force_added_check_ignores_upstream_force_tracked_files(fork: Path) -> None:
    # Upstream's own ignored-but-tracked file is not an Ortus path.
    (fork / "node_modules").mkdir()
    _commit(fork, "node_modules/vendored.js", "Vendor a dependency", force=True)
    assert push_violations(fork, "origin", _refspec(fork)) == []


# AC-2


@pytest.mark.parametrize(
    ("issue_ref", "message", "blocked"),
    [
        ("none", f"{PREFIX}-abc1: Fix the parser", True),
        ("none", f"Fix the parser\n\nFollows up {PREFIX}-abc1.2 in prose.", True),
        ("trailer", f"Fix the parser\n\nBody text.\n\nOrtus-Issue: {PREFIX}-abc1", False),
        ("trailer", f"{PREFIX}-abc1: Fix the parser\n\nOrtus-Issue: {PREFIX}-abc1", True),
        ("subject", f"{PREFIX}-abc1: Fix the parser", False),
        ("none", "Fix metal-abc1 and my_tal-x9 handling", False),
    ],
)
def test_bead_id_in_a_pushed_message(
    fork: Path, issue_ref: str, message: str, blocked: bool
) -> None:
    _write_ortusrc(fork, issue_ref)
    _commit(fork, "src.txt", message)
    violations = push_violations(fork, "origin", _refspec(fork))
    assert bool(violations) is blocked, violations
    if blocked:
        assert f"{PREFIX}-abc1" in violations[0]


def test_bead_id_blocks_through_the_hook(fork: Path, origin: Path) -> None:
    before = _origin_refs(origin)
    _commit(fork, "src.txt", f"{PREFIX}-q7f6: Fix the parser")
    proc = _run(fork, "push", "origin", "main")
    assert proc.returncode != 0
    assert f"{PREFIX}-q7f6" in proc.stderr
    assert _origin_refs(origin) == before


def test_bead_id_scan_covers_a_first_push(fork: Path) -> None:
    _git(fork, "checkout", "-q", "-b", "topic")
    _commit(fork, "src.txt", f"{PREFIX}-abc1: Start a topic")
    local = _git(fork, "rev-parse", "HEAD")
    refspecs = [("refs/heads/topic", local, "refs/heads/topic", ZERO_SHA)]
    violations = push_violations(fork, "origin", refspecs)
    assert len(violations) == 1 and f"{PREFIX}-abc1" in violations[0]


def test_bead_id_scan_skips_a_deletion(fork: Path) -> None:
    refspecs = [("(delete)", ZERO_SHA, "refs/heads/old", _git(fork, "rev-parse", "HEAD"))]
    assert push_violations(fork, "origin", refspecs) == []


def test_bead_id_check_also_refuses_agent_attribution(fork: Path) -> None:
    _write_ortusrc(fork, "subject")
    _commit(
        fork,
        "src.txt",
        "Fix the parser\n\nBody.\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    violations = push_violations(fork, "origin", _refspec(fork))
    assert len(violations) == 1 and "Co-Authored-By" in violations[0]


# AC-3


@pytest.mark.parametrize("ref", ["refs/dolt/data", "refs/heads/__dolt_remote_info__"])
def test_dolt_ref_push_to_origin_is_blocked(fork: Path, origin: Path, ref: str) -> None:
    before = _origin_refs(origin)
    proc = _run(fork, "push", "origin", f"HEAD:{ref}")
    assert proc.returncode != 0
    assert ref in proc.stderr
    assert _origin_refs(origin) == before


def test_dolt_ref_push_by_origin_url_is_blocked(fork: Path, origin: Path) -> None:
    before = _origin_refs(origin)
    proc = _run(fork, "push", str(origin), "HEAD:refs/dolt/data")
    assert proc.returncode != 0
    assert _origin_refs(origin) == before


def test_dolt_ref_push_to_a_private_remote_is_allowed(fork: Path, tmp_path: Path) -> None:
    private = tmp_path / "private.git"
    _git(tmp_path, "init", "-q", "--bare", str(private))
    _git(fork, "remote", "add", "ortus-private", str(private))
    proc = _run(fork, "push", "ortus-private", "HEAD:refs/dolt/data")
    assert proc.returncode == 0, proc.stderr
    assert "refs/dolt/data" in _origin_refs(private)


# AC-4


@pytest.mark.parametrize("path", [".gitignore", "docs/.gitignore"])
def test_pre_commit_refuses_a_staged_gitignore(fork: Path, path: str) -> None:
    (fork / path).parent.mkdir(parents=True, exist_ok=True)
    (fork / path).write_text("dist/\n", encoding="utf-8")
    _git(fork, "add", path)
    assert staged_gitignore_violation(fork, {}) is not None
    head = _git(fork, "rev-parse", "HEAD")

    proc = _run(fork, "commit", "-q", "-m", "Ignore dist")
    assert proc.returncode != 0
    assert "ORTUS_STEALTH_ALLOW_GITIGNORE" in proc.stderr
    assert _git(fork, "rev-parse", "HEAD") == head

    env = {**os.environ, "ORTUS_STEALTH_ALLOW_GITIGNORE": "1"}
    proc = _run(fork, "commit", "-q", "-m", "Ignore dist", env=env)
    assert proc.returncode == 0, proc.stderr
    assert _git(fork, "rev-parse", "HEAD") != head


def test_pre_commit_allows_other_files(fork: Path) -> None:
    _commit(fork, "src.txt", "Fix the parser")
    assert staged_gitignore_violation(fork, {}) is None


# AC-5


def test_backstop_refuses_a_leaking_push_and_halts(fork: Path, origin: Path) -> None:
    before = _origin_refs(origin)
    _commit(fork, ".beads/issues.jsonl", "Record data", force=True)
    log: list[str] = []
    git = GitClient(repo=fork, stealth=True)
    with pytest.raises(typer.Exit):
        _enforce_branch_discipline(git, "main", log.append, phase="post-close")
    assert any(line.startswith("stealth-guard: BLOCKED") for line in log)
    assert any("PUSH FAILED" in line for line in log)
    assert _origin_refs(origin) == before


def test_backstop_pushes_a_clean_stealth_commit(fork: Path, origin: Path) -> None:
    tip = _commit(fork, "src.txt", "Fix the parser")
    log: list[str] = []
    _enforce_branch_discipline(
        GitClient(repo=fork, stealth=True), "main", log.append, phase="post-close"
    )
    assert f"refs/heads/main {tip}" in _origin_refs(origin)
    assert not any("stealth-guard" in line for line in log)


# AC-6


def _hook(repo: Path, name: str) -> Path:
    return repo / ".git" / "hooks" / name


def test_install_writes_both_hooks_executable(fork: Path) -> None:
    for name in ("pre-push", "pre-commit"):
        hook = _hook(fork, name)
        assert STEALTH_HOOK_MARKER in hook.read_text(encoding="utf-8")
        assert os.access(hook, os.X_OK)
    # A re-install rewrites its own hooks without a warning.
    assert install_stealth_hooks(fork) == []


def test_install_never_overwrites_a_foreign_hook(fork: Path) -> None:
    foreign = "#!/bin/sh\necho upstream hook\n"
    _hook(fork, "pre-push").write_text(foreign, encoding="utf-8")
    warnings = install_stealth_hooks(fork)
    assert len(warnings) == 1 and "pre-push" in warnings[0]
    assert _hook(fork, "pre-push").read_text(encoding="utf-8") == foreign
    assert STEALTH_HOOK_MARKER in _hook(fork, "pre-commit").read_text(encoding="utf-8")


def test_install_skips_a_configured_hooks_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.hooksPath", ".husky")
    warnings = install_stealth_hooks(repo)
    assert len(warnings) == 1 and ".husky" in warnings[0]
    assert not _hook(repo, "pre-push").exists()
    assert not (repo / ".husky").exists()


def _stub_init(monkeypatch: pytest.MonkeyPatch) -> None:
    import ortus.commands.init as init_mod

    def bd_init(repo: Path, prefix: str | None) -> None:
        (repo / ".beads").mkdir(exist_ok=True)

    monkeypatch.setattr(init_mod, "_bd_init_stealth", bd_init)
    monkeypatch.setattr(init_mod, "_bd_init", bd_init)
    monkeypatch.setattr(init_mod, "_bd_no_git_ops", lambda repo: None)
    monkeypatch.setattr(init_mod, "_bd_remember", lambda repo: None)
    monkeypatch.setattr(init_mod, "_bd_hooks_install", lambda repo: None)
    monkeypatch.setattr(init_mod, "_codegraph_cli", lambda: "/usr/bin/codegraph")
    monkeypatch.setattr(
        init_mod,
        "_codegraph_index",
        lambda repo, **kw: (repo / ".codegraph").mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(init_mod, "_backend_cli", lambda name: f"/usr/bin/{name}")


def _bare_fork(tmp_path: Path) -> Path:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "README.md", "upstream history")
    _git(repo, "remote", "add", "origin", "https://github.com/upstream/project.git")
    return repo


def test_install_happens_in_stealth_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_init(monkeypatch)
    repo = _bare_fork(tmp_path)
    result = runner.invoke(app, ["init", str(repo), "--stealth"])
    assert result.exit_code == 0, result.output
    assert STEALTH_HOOK_MARKER in _hook(repo, "pre-push").read_text(encoding="utf-8")
    assert STEALTH_HOOK_MARKER in _hook(repo, "pre-commit").read_text(encoding="utf-8")


def test_install_never_happens_outside_stealth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_init(monkeypatch)
    repo = _bare_fork(tmp_path)
    result = runner.invoke(app, ["init", str(repo)])
    assert result.exit_code == 0, result.output
    for name in ("pre-push", "pre-commit"):
        hook = _hook(repo, name)
        assert not hook.exists() or STEALTH_HOOK_MARKER not in hook.read_text(
            encoding="utf-8"
        )


def test_install_guard_verbs_pass_outside_stealth(tmp_path: Path) -> None:
    repo = _bare_fork(tmp_path)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    cwd = os.getcwd()
    os.chdir(repo)
    try:
        assert runner.invoke(app, ["guard", "pre-commit"]).exit_code == 0
        line = f"refs/heads/main {_git(repo, 'rev-parse', 'HEAD')} refs/dolt/data {ZERO_SHA}\n"
        result = runner.invoke(app, ["guard", "pre-push", "origin", ""], input=line)
        assert result.exit_code == 0
    finally:
        os.chdir(cwd)


# AC-7


@pytest.fixture()
def bd_state(monkeypatch: pytest.MonkeyPatch) -> dict:
    """What the stubbed `bd ... --json` calls answer for the check rows."""
    state = {"no-git-ops": "true", "remotes": []}

    def bd_json(repo: Path, *args: str):
        if args[:2] == ("config", "get"):
            return {"key": args[2], "value": state["no-git-ops"]}
        if args[:3] == ("dolt", "remote", "list"):
            return state["remotes"]
        raise AssertionError(f"unexpected bd call {args}")

    monkeypatch.setattr(check_mod, "_bd_json", bd_json)
    return state


def _failed(repo: Path) -> dict[str, str]:
    return {r.name: r.message for r in check_stealth_rows(repo) if not r.ok}


def test_check_rows_pass_on_a_clean_stealth_repo(fork: Path, bd_state: dict) -> None:
    rows = check_stealth_rows(fork)
    assert [r.name for r in rows if not r.ok] == []
    assert {r.name for r in rows} >= {
        "stealth exclude block",
        "stealth tracked paths",
        "stealth core.hooksPath",
        "stealth pre-push hook",
        "bd no-git-ops",
        "stealth tracker remote",
        "BD_NO_REMOTE_ADOPT",
    }


def test_check_rows_are_absent_outside_stealth(tmp_path: Path) -> None:
    assert check_stealth_rows(_bare_fork(tmp_path)) == []


def test_check_rows_name_a_missing_exclude_block(fork: Path, bd_state: dict) -> None:
    (fork / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    assert "stealth exclude block" in _failed(fork)


def test_check_rows_name_a_stale_exclude_block(fork: Path, bd_state: dict) -> None:
    merge_exclude(fork, [p for p in STEALTH_EXCLUDE_PATTERNS if p != "/.beads/"])
    assert "/.beads/" in _failed(fork)["stealth exclude block"]


def test_check_rows_name_a_tracked_ortus_path(fork: Path, bd_state: dict) -> None:
    _commit(fork, ".beads/issues.jsonl", "Record data", force=True)
    assert ".beads/issues.jsonl" in _failed(fork)["stealth tracked paths"]


def test_check_rows_name_a_bd_hooks_path(fork: Path, bd_state: dict) -> None:
    _git(fork, "config", "core.hooksPath", ".beads/hooks")
    failed = _failed(fork)
    assert "stealth core.hooksPath" in failed
    assert "stealth pre-push hook" in failed


def test_check_rows_name_a_missing_pre_push_hook(fork: Path, bd_state: dict) -> None:
    _hook(fork, "pre-push").unlink()
    assert "stealth pre-push hook" in _failed(fork)


def test_check_rows_warn_for_a_foreign_hooks_path(fork: Path, bd_state: dict) -> None:
    _git(fork, "config", "core.hooksPath", ".husky")
    row = next(r for r in check_stealth_rows(fork) if r.name == "stealth pre-push hook")
    assert not row.ok and row.level == "info"


def test_check_rows_name_bd_git_ops_left_on(fork: Path, bd_state: dict) -> None:
    bd_state["no-git-ops"] = "false"
    assert "bd no-git-ops" in _failed(fork)


def test_check_rows_name_a_dolt_remote_on_origin(
    fork: Path, origin: Path, bd_state: dict
) -> None:
    bd_state["remotes"] = [{"name": "origin", "url": f"git+{origin}"}]
    assert "origin" in _failed(fork)["stealth tracker remote"]
    bd_state["remotes"] = [{"name": "ortus-private", "url": "file:///srv/private"}]
    assert "stealth tracker remote" not in _failed(fork)
