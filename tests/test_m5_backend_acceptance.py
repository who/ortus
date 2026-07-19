"""M5 acceptance: both backends generate working projects (ortus-e108, Phase 3).

Phase 3 made `agent_cli` a real fork in the template — conditional config
directories (M4), per-backend instruction files (FR-009), a backend-matched
`bd setup` task (FR-011). Each of those was tested in isolation against a
rendered fragment. This module is the end-to-end check that the fork actually
produces two *whole* projects, and that the fork stayed narrow: everything that
isn't backend-specific must be one shared file, not a claude copy and a codex
copy drifting apart.

The three conditions from the issue:

  1. both generated projects pass their smoke test — generation succeeds and
     the expected tree is present (`test_generation_produces_expected_tree`);
  2. shared files exist once, not per-backend — byte-identical across the two
     generated projects, and no backend-suffixed filenames anywhere
     (`test_shared_files_are_byte_identical`, `test_no_backend_suffixed_files`);
  3. each project's launcher starts successfully — `goal.sh --dry-run` exits 0
     and reports the backend the project was generated for
     (`test_launcher_starts_and_reports_its_backend`).

Generation runs against a pristine throwaway clone of the working tree's
*tracked* files rather than the repo itself, for two reasons. Copier resolves a
git template to its newest tag by default (v0.1.x here), which would silently
render a months-old template and pass; and against a dirty worktree copier
shells out to `git add -A`, which fails on unreadable paths under `.claude/`.
Committing the tracked files into a fresh repo and pinning `--vcs-ref=HEAD`
gives us the working tree, deterministically, on any host.
"""

from __future__ import annotations

import filecmp
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parent.parent

BACKENDS = ("claude", "codex")

# Files that carry the backend fork. Everything else in the tree must match
# byte-for-byte between the two generated projects.
BACKEND_SPECIFIC = {
    ".copier-answers.yml",
    "AGENTS.md",
    "README.md",
    "ortus/README.md",
    "ortus/lib/backend-default.sh",
}

# The backend-conditional config directory each backend owns — and must not
# receive the other's (M4).
BACKEND_CONFIG = {
    "claude": ".claude/settings.json",
    "codex": ".codex/config.toml",
}

# Shared machinery that a per-backend split would most plausibly have
# duplicated. Named explicitly so the check fails loudly if one of them stops
# being generated at all, which the identical-tree comparison alone would miss.
SHARED_FILES = (
    "ortus/goal.sh",
    "ortus/ralph.sh",
    "ortus/lib/backend.sh",
    "ortus/lib/sandbox.sh",
    "ortus/lib/cache.sh",
    "ortus/prompts/goal-prompt.md",
    "ortus/prompts/conditions/queue-zero.txt",
    "ortus/prompts/conditions/feature-approved.txt",
    "ortus/prompts/conditions/prd-decomposed.txt",
    "CLAUDE.md",
)

requires_copier = pytest.mark.skipif(
    shutil.which("copier") is None,
    reason="copier not installed; `uv pip install -e '.[dev]'` provides it",
)


def _pristine_template(tmp_path: Path) -> Path:
    """A fresh git repo holding this working tree's tracked files, at HEAD."""
    src = tmp_path / "template-src"
    src.mkdir()
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    for rel in filter(None, tracked):
        dest = src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)

    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=test"]
    subprocess.run([*git, "init", "-q", "."], cwd=src, check=True)
    subprocess.run([*git, "add", "-A"], cwd=src, check=True)
    subprocess.run([*git, "commit", "-qm", "acceptance fixture"], cwd=src, check=True)
    return src


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """One generated project per backend, from the same template snapshot."""
    tmp_path = tmp_path_factory.mktemp("m5")
    src = _pristine_template(tmp_path)

    projects: dict[str, Path] = {}
    for backend in BACKENDS:
        dest = tmp_path / backend
        # --skip-tasks: _tasks run `bd init` and kick off ./ortus/idea.sh, which
        # is a real agent session. Acceptance is about the rendered tree, and
        # the tasks themselves are covered by test_copier_setup_task.py.
        proc = subprocess.run(
            [
                "copier", "copy",
                "--defaults", "--trust", "--skip-tasks",
                "--vcs-ref=HEAD",
                "--data", "project_name=acme",
                f"--data=agent_cli={backend}",
                "--data", "language=python",
                str(src), str(dest),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"copier failed for agent_cli={backend}:\n{proc.stderr}"
        )
        projects[backend] = dest
    return projects


def _relative_files(root: Path) -> set[str]:
    return {
        str(p.relative_to(root).as_posix())
        for p in root.rglob("*")
        if p.is_file() and ".git/" not in p.relative_to(root).as_posix()
    }


@pytest.mark.parametrize("backend", BACKENDS)
def test_generation_produces_expected_tree(
    generated: dict[str, Path], backend: str
) -> None:
    """Condition 1: each backend generates a project with the expected files."""
    files = _relative_files(generated[backend])
    for shared in SHARED_FILES:
        assert shared in files, f"{backend}: missing {shared}"
    assert BACKEND_CONFIG[backend] in files


@pytest.mark.parametrize("backend", BACKENDS)
def test_backend_config_dirs_are_exclusive(
    generated: dict[str, Path], backend: str
) -> None:
    """M4: a codex project ships no .claude/, and vice versa."""
    other = next(b for b in BACKENDS if b != backend)
    root = generated[backend]
    assert (root / BACKEND_CONFIG[backend]).is_file()
    assert not (root / f".{other}").exists(), (
        f"{backend} project contains a .{other}/ directory"
    )


def test_shared_files_are_byte_identical(generated: dict[str, Path]) -> None:
    """Condition 2: shared files exist once, not once per backend.

    Byte equality is the observable form of "one file": if goal.sh were forked
    per backend, the two renderings would diverge here.
    """
    claude_root, codex_root = generated["claude"], generated["codex"]
    for shared in SHARED_FILES:
        assert filecmp.cmp(
            claude_root / shared, codex_root / shared, shallow=False
        ), f"{shared} differs between backends; shared files must not fork"


def test_only_known_files_differ_between_backends(
    generated: dict[str, Path],
) -> None:
    """The fork stays confined to the files that are meant to carry it.

    A new backend-conditional file is a design decision, not an accident — this
    fails until it's added to BACKEND_SPECIFIC deliberately.
    """
    claude_root, codex_root = generated["claude"], generated["codex"]
    common = _relative_files(claude_root) & _relative_files(codex_root)
    differing = {
        rel
        for rel in common
        if not filecmp.cmp(claude_root / rel, codex_root / rel, shallow=False)
    }
    assert differing == BACKEND_SPECIFIC


@pytest.mark.parametrize("backend", BACKENDS)
def test_no_backend_suffixed_files(generated: dict[str, Path], backend: str) -> None:
    """No `goal-codex.sh` / `AGENTS_claude.md` style per-backend duplicates."""
    suffixed = re.compile(r"[-_.](claude|codex)\.[^/]+$", re.IGNORECASE)
    offenders = [
        rel for rel in _relative_files(generated[backend]) if suffixed.search(rel)
    ]
    assert offenders == [], f"{backend}: backend-suffixed duplicates: {offenders}"


@pytest.mark.parametrize("backend", BACKENDS)
def test_launcher_starts_and_reports_its_backend(
    generated: dict[str, Path], backend: str
) -> None:
    """Condition 3: the launcher runs, and the copier answer reaches it.

    `--dry-run` exits before the flock and any agent CLI invocation, so this
    needs neither backend installed. It prints the resolved backend, which is
    the end of FR-002's precedence chain: the `agent_cli` answer rendered into
    lib/backend-default.sh.
    """
    proc = subprocess.run(
        ["bash", "ortus/goal.sh", "--dry-run"],
        cwd=str(generated[backend]),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"goal.sh --dry-run failed:\n{proc.stderr}"
    assert f"ORTUS_BACKEND={backend}" in proc.stdout
