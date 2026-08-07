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


def test_list_all_includes_open_and_closed_without_status_filter(
    bd_workspace: Path,
) -> None:
    client = BdClient(bd_workspace)
    open_id = client.create(title="open packet", issue_type="task")
    closed_id = client.create(title="closed packet", issue_type="task")
    client.close(closed_id)
    assert {open_id, closed_id} <= {issue["id"] for issue in client.list_all()}


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
    escalated = client.create(title="escalated to human", issue_type="task", priority=2)
    client.update_status(plain, "in_progress")
    client.update_status(escalated, "in_progress")
    client.add_label(escalated, "human")
    # Without the filter both ids appear.
    assert client.in_progress_ids() == {plain, escalated}
    # With the filter the escalated one disappears.
    assert client.in_progress_ids(exclude_labels=("human",)) == {plain}


def test_status_tracks_the_lifecycle_and_is_empty_when_unreadable(
    bd_workspace: Path,
) -> None:
    """Finalization re-validates issue identity through `status`, so an
    unreadable issue must read as "" rather than raising."""
    client = BdClient(bd_workspace)
    issue_id = client.create(title="lifecycle", issue_type="task", priority=2)
    assert client.status(issue_id) == "open"
    client.update_status(issue_id, "in_progress")
    assert client.status(issue_id) == "in_progress"
    client.close(issue_id)
    assert client.status(issue_id) == "closed"
    assert client.status("ortus-no-such-issue-id-anywhere") == ""


def test_has_comment_matches_only_the_requested_marker(bd_workspace: Path) -> None:
    """The marker is what makes a replayed report idempotent when the journal
    boundary never got written."""
    client = BdClient(bd_workspace)
    issue_id = client.create(title="commented", issue_type="task", priority=2)
    marker = "## Ortus finalization record"

    assert not client.has_comment(issue_id, marker)
    client.add_comment(issue_id, "## Independent verification — VERDICT: PASS")
    assert not client.has_comment(issue_id, marker), "a different comment is not a match"
    client.add_comment(issue_id, f"{marker}\n\nIssue: {issue_id}\n")
    assert client.has_comment(issue_id, marker)
    assert not client.has_comment("ortus-no-such-issue-id-anywhere", marker)


def test_close_once_is_idempotent_and_keeps_the_original_reason(
    bd_workspace: Path,
) -> None:
    """A restart after a close that landed must not issue a second `bd close`,
    which would overwrite the recorded reason."""
    client = BdClient(bd_workspace)
    issue_id = client.create(title="closed once", issue_type="task", priority=2)

    assert client.close_once(issue_id, reason="verified candidate")
    assert client.status(issue_id) == "closed"
    assert not client.close_once(issue_id, reason="replayed close")
    assert client.show(issue_id)["close_reason"] == "verified candidate"


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
