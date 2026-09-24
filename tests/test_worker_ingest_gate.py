"""The gate a worker's follow-up passes on its way into the queue.

A grind worker that files leftover work with a bare `bd create` writes
whatever it assembled, gaps and all. The bead then fails readiness at claim
time, is labeled `human`, and sits out of the queue until somebody repairs it
by hand. These tests pin the other path: the same validate-then-create gate
`ortus ingest` has always applied, exercised with the two packet shapes a
live worker actually produced — a design body missing required sections, and
an acceptance criterion with no runnable command. Both create nothing and
hand the diagnostic straight back, in the session that can still fix it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from tests._shims import ready_issue_args
from tests.conftest import copy_bd_workspace

pytestmark = pytest.mark.integration
runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _flat(text: str) -> str:
    """Stderr with rich's wrapping and colour folded away."""
    return " ".join(_ANSI.sub("", text).split())


def _packet(**overrides: str) -> dict[str, str]:
    """A worker-shaped follow-up packet, readiness-complete before overrides.

    Built from the one hand-authored schema v1 body the suite already trusts,
    so an override is the only difference between a packet that files and one
    that bounces.
    """
    args = ready_issue_args()
    flags = dict(zip(args[::2], args[1::2]))
    packet = {
        "title": "Restore the pinned compatibility date",
        "issue_type": "task",
        "description": flags["--description"],
        "design": flags["--design"],
        "acceptance_criteria": flags["--acceptance"],
    }
    packet.update(overrides)
    return packet


def _without(design: str, *headings: str) -> str:
    """Drop whole `## ` sections from a design body."""
    for heading in headings:
        start = design.index(heading)
        end = design.find("\n## ", start + 1)
        design = design[:start] + (design[end + 1 :] if end != -1 else "")
    return design


#: The acceptance body the live worker filed: a criterion the reader can see
#: and no command anything can run.
_UNRUNNABLE_ACCEPTANCE = """## Observable criteria
- AC-1: The follow-up behavior is observable.

## Criterion checks
- AC-1: Confirm the new behavior by inspection.

## Targeted tests
Run `uv run pytest tests/test_ingest_cli.py -q`."""


def _all_ids(repo: Path) -> list[str]:
    """Every issue id in the workspace, closed ones included."""
    listing = subprocess.run(
        ["bd", "list", "--all", "--limit", "0", "--json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [str(item["id"]) for item in json.loads(listing or "[]")]


def _shown(repo: Path, issue_id: str) -> dict:
    return json.loads(
        subprocess.run(
            ["bd", "show", issue_id, "--json"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )[0]


@pytest.mark.parametrize(
    "overrides, section",
    [
        pytest.param(
            {
                "design": _without(
                    _packet()["design"],
                    "## Compatibility constraints",
                    "## Dependencies",
                    "## Plan-gap guidance",
                )
            },
            "design/compatibility constraints",
            id="design-sections-missing",
        ),
        pytest.param(
            {"acceptance_criteria": _UNRUNNABLE_ACCEPTANCE},
            "acceptance_criteria/criterion checks",
            id="criterion-without-a-command",
        ),
    ],
)
def test_unready_bounces_and_files_nothing(
    tmp_path: Path, overrides: dict[str, str], section: str
) -> None:
    """AC-1: an unready follow-up creates no bead and names its own gaps."""
    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    before = _all_ids(repo)

    result = runner.invoke(
        app, ["ingest", str(repo), "--stdin"], input=json.dumps(_packet(**overrides))
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    row = result.stdout.strip()
    assert row.startswith("UNREADY <packet>: "), row
    assert section in row
    assert _all_ids(repo) == before
    # The bounce is answerable where it landed: the session that filed the
    # packet completes it, rather than leaving a bead for an operator.
    hint = _flat(result.stderr)
    assert "no bead was created and nothing was labeled human" in hint
    assert "run `ortus ingest --stdin` again in this session" in hint


def test_unready_bounces_without_reaching_the_store() -> None:
    """AC-1: the refusal happens before any bd call, not after one."""
    from ortus.commands.ingest import build_candidate, file_if_ready

    class _RecordingBd:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create(self, **kwargs: object) -> str:
            self.calls.append("create")
            return "ortus-never"

        def add_label(self, *args: object) -> None:
            self.calls.append("add_label")

    bd = _RecordingBd()
    candidate = build_candidate(
        _packet(acceptance_criteria=_UNRUNNABLE_ACCEPTANCE),
        title=None,
        issue_type=None,
        priority=None,
    )

    verdict = file_if_ready(candidate, bd=bd)

    assert not verdict.filed
    assert verdict.bead_id is None
    assert "AC-1: no runnable command" in verdict.diagnostic
    assert verdict.row().startswith("UNREADY ")
    assert bd.calls == []


def test_ready_creates_an_open_bead_with_no_human_label(tmp_path: Path) -> None:
    """AC-2: a complete follow-up lands open, ready, and in the queue."""
    from ortus.core.readiness import validate_issue

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path

    result = runner.invoke(
        app, ["ingest", str(repo), "--stdin"], input=json.dumps(_packet())
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    issue_id = result.stdout.strip()
    assert issue_id in _all_ids(repo)

    shown = _shown(repo, issue_id)
    assert shown["status"] == "open"
    # The filing path never routes to an operator: the gate refuses ahead of
    # the create, so nothing it files arrives needing repair.
    assert "human" not in [str(label) for label in (shown.get("labels") or [])]
    assert validate_issue(shown).ready


def test_ready_creates_nothing_for_an_epic_container(tmp_path: Path) -> None:
    """AC-2: readiness exempts an epic, so the gate refuses it outright."""
    from ortus.commands.ingest import build_candidate, file_if_ready

    candidate = build_candidate(
        _packet(), title=None, issue_type="epic", priority=None
    )

    verdict = file_if_ready(candidate, bd=None)

    assert not verdict.filed
    assert "carries no work spec" in verdict.diagnostic
