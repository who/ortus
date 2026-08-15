"""Contract tests for the who-only hosted grind workflow.

The workflow is YAML, not a Python entry point. These tests read the shipped
``.github/workflows/grind.yml`` the same way ``test_github_bead.py`` pins
``bead-from-issue.yml``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GRIND_WORKFLOW = REPO_ROOT / ".github/workflows/grind.yml"
_JOB_HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.M)
_ON_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):", re.M)


def _workflow_text() -> str:
    return GRIND_WORKFLOW.read_text(encoding="utf-8")


def _on_triggers(text: str) -> set[str]:
    """Top-level event names under the workflow ``on:`` mapping."""

    lines = text.splitlines()
    in_on = False
    triggers: set[str] = set()
    for line in lines:
        if line.startswith("on:"):
            in_on = True
            rest = line[3:].strip()
            if rest and not rest.startswith("#"):
                triggers.add(rest.rstrip(":"))
            continue
        if not in_on:
            continue
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        match = _ON_KEY.match(line)
        if match:
            triggers.add(match.group(1))
    return triggers


def _jobs(text: str) -> dict[str, str]:
    """Map job id → raw job body, including the header line."""

    marker = "\njobs:"
    start = text.find(marker)
    assert start != -1, "grind.yml must declare jobs"
    body = text[start + 1 :]
    matches = list(_JOB_HEADER.finditer(body))
    jobs: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        jobs[match.group(1)] = body[match.start() : end]
    return jobs


def test_grind_workflow_exists_and_is_dispatch_only() -> None:
    text = _workflow_text()
    assert GRIND_WORKFLOW.is_file()
    assert _on_triggers(text) == {"workflow_dispatch"}
    assert "github.actor == 'who'" in text


def test_non_who_cannot_spawn_grind() -> None:
    text = _workflow_text()
    jobs = _jobs(text)
    grind_jobs = [
        name for name, body in jobs.items() if "uv run ortus grind" in body
    ]
    assert grind_jobs, "expected a job that invokes uv run ortus grind"
    for name in grind_jobs:
        body = jobs[name]
        assert "github.actor == 'who'" in body
        assert "if: github.actor == 'who'" in body
        assert "ortus grind" in body
    assert "allowlist" in jobs
    assert "no-op failure" in jobs["allowlist"]
    assert "uv run ortus grind" not in jobs["allowlist"]


def test_grind_workflow_hydrates_installs_and_invokes_with_inputs() -> None:
    text = _workflow_text()
    jobs = _jobs(text)
    grind = jobs["grind"]
    assert "bd bootstrap --yes" in grind
    assert "run: bd import .beads/issues.jsonl" not in grind
    assert "@colbymchenry/codegraph@1.5.0" in grind
    assert "bubblewrap" in grind
    assert "uv run ortus grind" in grind
    assert "--backend" in grind
    assert "--tasks" in grind
    assert "--worker-timeout" in grind
    grind_invocation = grind[grind.index("uv run ortus grind") :]
    assert "--repair-unready" not in grind_invocation
    assert "inputs.backend" in grind
    assert "inputs.tasks" in grind
    assert "inputs.worker_timeout" in grind
    assert 'group: grind-main' in text
    assert "cancel-in-progress: false" in text
    assert "BD_VERSION=\"1.2.1\"" in grind
    assert "actions/checkout@v7" in grind
    assert "ref: main" in grind
    assert "ANTHROPIC_API_KEY" in grind
    assert "XAI_API_KEY" in grind
    assert "OPENAI_API_KEY" in grind
    assert "CODEX_API_KEY" in grind
    assert "github-actions[bot]" in grind
    assert "actions/upload-artifact@v7" in grind
