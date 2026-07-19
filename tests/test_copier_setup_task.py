"""Tests for the backend-conditional `bd setup` _tasks step (FR-011, ortus-sfpa).

copier.yaml's _tasks list installs beads' editor integration into the generated
project. bd ships a recipe per backend — `claude` writes .claude/settings.json
hooks, `codex` appends a section to AGENTS.md — so the step has to pick the
recipe matching agent_cli, or a codex project (which deliberately ships no
.claude/) gets a Claude settings file it never asked for (M4).

The step also has to survive a bd that predates the recipe: generation must
degrade to a printed manual instruction rather than aborting the copy (copier
deletes the destination when a task fails).

The rendered command is exercised against a stub `bd` on PATH rather than the
real one — these assert the branch logic, not beads' behaviour.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
COPIER_YAML = REPO_ROOT / "copier.yaml"

# What `bd setup --list` prints. The stub reproduces the real two-space-indented
# "<name>  <description>  (built-in)" layout, since the step greps for it.
RECIPE_LIST = """\
Available recipes:
  claude        Claude Code hooks (SessionStart, PreCompact)  (built-in)
  codex         Codex CLI AGENTS.md section  (built-in)
"""


@pytest.fixture(scope="module")
def init_task() -> str:
    """The one _tasks entry that runs `bd init`, unrendered."""
    tasks = yaml.safe_load(COPIER_YAML.read_text())["_tasks"]
    matches = [t for t in tasks if "bd init" in t]
    assert len(matches) == 1, f"expected exactly one bd init task, got {matches}"
    return matches[0]


@pytest.fixture(scope="module")
def setup_task() -> str:
    """The one _tasks entry that runs `bd setup`, unrendered."""
    tasks = yaml.safe_load(COPIER_YAML.read_text())["_tasks"]
    matches = [t for t in tasks if "bd setup" in t]
    assert len(matches) == 1, f"expected exactly one bd setup task, got {matches}"
    return matches[0]


def _render(task: str, agent_cli: str) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(task).render(agent_cli=agent_cli)


def _run(task: str, agent_cli: str, tmp_path: Path, recipes: str) -> subprocess.CompletedProcess:
    """Run the rendered step with a stub bd that reports `recipes`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "bd"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "setup" ] && [ "$2" = "--list" ]; then\n'
        f"  cat <<'EOF'\n{recipes}EOF\n"
        "  exit 0\n"
        "fi\n"
        'echo "CALLED: $*"\n'
    )
    stub.chmod(0o755)
    return subprocess.run(
        ["bash", "-c", _render(task, agent_cli)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )


def test_bd_init_skips_agent_files_only_for_codex(init_task):
    """`bd init` writes a .claude/settings.json unless told not to (M4).

    The codex backend ships no .claude/, so its init must pass --skip-agents;
    the claude backend must keep the existing behaviour (NFR-001).
    """
    assert "--skip-agents" in _render(init_task, "codex")
    assert "--skip-agents" not in _render(init_task, "claude")


@pytest.mark.parametrize("agent_cli", ["claude", "codex"])
def test_the_step_installs_the_recipe_matching_the_backend(setup_task, agent_cli, tmp_path):
    out = _run(setup_task, agent_cli, tmp_path, RECIPE_LIST)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"CALLED: setup {agent_cli}"


def test_a_codex_project_never_runs_the_claude_recipe(setup_task, tmp_path):
    """Acceptance #1 — the Claude profile must not touch a codex project."""
    out = _run(setup_task, "codex", tmp_path, RECIPE_LIST)
    assert "claude" not in out.stdout


@pytest.mark.parametrize("agent_cli", ["claude", "codex"])
def test_a_missing_recipe_degrades_instead_of_failing_generation(
    setup_task, agent_cli, tmp_path
):
    """Acceptance #2 — an older bd must not abort the copy."""
    out = _run(setup_task, agent_cli, tmp_path, "Available recipes:\n  cursor  Cursor\n")
    assert out.returncode == 0, out.stderr
    assert "CALLED:" not in out.stdout
    assert f"bd setup {agent_cli}" in out.stderr


def test_the_recipe_probe_does_not_match_a_substring(setup_task, tmp_path):
    """`codex` must not be satisfied by a recipe merely containing it."""
    out = _run(
        setup_task, "codex", tmp_path, "Available recipes:\n  codexlike  Not it\n"
    )
    assert out.returncode == 0, out.stderr
    assert "CALLED:" not in out.stdout
