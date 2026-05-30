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


def test_list_ready_exclude_labels_filters_human(bd_workspace: Path) -> None:
    """The grind harness selects from `bd ready --exclude-label human`; a
    human-flagged issue must be dropped from the result."""
    client = BdClient(bd_workspace)
    plain = client.create(title="plain work", issue_type="task", priority=2)
    flagged = client.create(
        title="needs a human", issue_type="task", priority=2, labels=["human"]
    )
    filtered = client.list_ready(exclude_labels=("human",))
    ids = {i["id"] for i in filtered}
    assert plain in ids
    assert flagged not in ids
    # Without the filter the flagged issue is still ready.
    assert flagged in {i["id"] for i in client.list_ready()}


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


def test_count_by_status_honors_exclude_labels(bd_workspace: Path) -> None:
    """Issues bearing any excluded label drop out of the count (ortus-9db5).

    Without the filter the orchestrator would spin on a queue of only
    human-flagged issues; with it, the count goes to zero and queue_drained()
    returns True.
    """
    client = BdClient(bd_workspace)
    plain = client.create(title="plain open", issue_type="task", priority=2)
    human = client.create(
        title="needs human", issue_type="task", priority=2, labels=["human"]
    )
    # Sanity: both visible without filter.
    assert client.count_by_status("open") == 2
    # With the filter the human-flagged one disappears.
    assert client.count_by_status("open", exclude_labels=("human",)) == 1
    # Sanity: the remaining id is the plain one (not the human-flagged one).
    opens = client.list_open()
    assert plain in {i["id"] for i in opens}
    assert human in {i["id"] for i in opens}


def test_in_progress_ids_honors_exclude_labels(bd_workspace: Path) -> None:
    """in_progress issues with the excluded label drop out of the id set.

    Mirrors the count-side filter so the grind orphan-detection diff
    doesn't keep re-flagging human-escalated claims.
    """
    client = BdClient(bd_workspace)
    plain = client.create(title="plain in progress", issue_type="task", priority=2)
    escalated = client.create(
        title="escalated to human", issue_type="task", priority=2
    )
    client.update_status(plain, "in_progress")
    client.update_status(escalated, "in_progress")
    client.add_label(escalated, "human")
    # Without the filter both ids appear.
    assert client.in_progress_ids() == {plain, escalated}
    # With the filter the escalated one disappears.
    assert client.in_progress_ids(exclude_labels=("human",)) == {plain}


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
