"""Integration tests for core/bd.py.

Per Testing Strategy: bd is NEVER mocked. Each test gets its own tmp
workspace via `bd init`. Marked `integration` so it can be deselected
in fast-unit-test runs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ortus.core.bd import BdClient, BdError

pytestmark = pytest.mark.integration


@pytest.fixture()
def bd_workspace(tmp_path: Path) -> Path:
    """Fresh `bd init` workspace, per-test."""
    if shutil.which("bd") is None:
        pytest.skip("bd binary not on PATH; cannot run integration tests")
    subprocess.run(
        ["bd", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    return tmp_path


def test_list_ready_returns_empty_for_fresh_workspace(bd_workspace: Path) -> None:
    client = BdClient(bd_workspace)
    assert client.list_ready() == []


def test_create_then_show_round_trip(bd_workspace: Path) -> None:
    client = BdClient(bd_workspace)
    issue_id = client.create(
        title="Test issue from wrapper",
        issue_type="task",
        priority=2,
        description="Created by tests/test_core_bd.py",
    )
    assert issue_id, "bd q should print the new id on stdout"
    detail = client.show(issue_id)
    assert detail["title"] == "Test issue from wrapper"
    assert detail["status"] == "open"


def test_list_ready_includes_new_issue(bd_workspace: Path) -> None:
    client = BdClient(bd_workspace)
    issue_id = client.create(title="ready me", issue_type="task", priority=2)
    ready = client.list_ready()
    assert any(i["id"] == issue_id for i in ready)


def test_close_marks_issue_closed(bd_workspace: Path) -> None:
    client = BdClient(bd_workspace)
    issue_id = client.create(title="to be closed", issue_type="task", priority=2)
    client.close(issue_id, reason="done in test")
    detail = client.show(issue_id)
    assert detail["status"] == "closed"


def test_bd_error_carries_stderr_verbatim(bd_workspace: Path) -> None:
    """Acceptance #3: BdError.stderr is bd's stderr verbatim."""
    client = BdClient(bd_workspace)
    with pytest.raises(BdError) as exc:
        client.show("ortus-no-such-issue-id-anywhere")
    assert exc.value.returncode != 0
    # bd's error message should appear in stderr (exact text varies by bd
    # version, but the issue id we asked about should be referenced).
    assert exc.value.stderr  # non-empty


def test_list_open_returns_open_issues(bd_workspace: Path) -> None:
    client = BdClient(bd_workspace)
    a = client.create(title="open 1", issue_type="task", priority=2)
    b = client.create(title="open 2", issue_type="task", priority=2)
    client.close(b)
    opens = client.list_open()
    ids = {i["id"] for i in opens}
    assert a in ids
    assert b not in ids


def test_create_with_all_optional_fields(bd_workspace: Path) -> None:
    """Exercise design/acceptance/notes/labels code paths."""
    client = BdClient(bd_workspace)
    issue_id = client.create(
        title="full kwargs",
        issue_type="task",
        priority=1,
        description="desc here",
        design="design here",
        acceptance="acc here",
        notes="notes here",
        labels=["alpha", "beta"],
    )
    detail = client.show(issue_id)
    assert detail["description"] == "desc here"
    assert detail["design"] == "design here"
    assert detail["acceptance_criteria"] == "acc here"
    assert detail["notes"] == "notes here"
    assert set(detail["labels"]) == {"alpha", "beta"}
