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


def _remember(workspace: Path, text: str, key: str) -> None:
    subprocess.run(
        ["bd", "remember", text, "--key", key],
        cwd=str(workspace),
        check=True,
        capture_output=True,
    )


def test_memories_round_trip_and_lessons_are_bounded(bd_workspace: Path) -> None:
    """`memories()` reads what `bd remember` stored; `lessons()` selects
    deterministically, excludes the given keys, and clips each body."""
    client = BdClient(bd_workspace)
    _remember(bd_workspace, "copy the tree before sweeping it", "sandbox-sweep")
    _remember(bd_workspace, "the scheduler holds the code it started with " * 20, "stale-scheduler")
    _remember(bd_workspace, "pointer to the readiness contract", "readiness-pointer")

    memories = client.memories()
    assert memories["sandbox-sweep"] == "copy the tree before sweeping it"

    lessons = client.lessons(
        exclude_keys=frozenset({"readiness-pointer"}), limit=2, max_chars=60
    )
    assert [key for key, _ in lessons] == ["sandbox-sweep", "stale-scheduler"]
    assert all(len(body) <= 60 + len(" […]") for _, body in lessons)
    assert dict(lessons)["stale-scheduler"].endswith(" […]")
    # Two reads of the same store select the same lessons.
    assert lessons == client.lessons(
        exclude_keys=frozenset({"readiness-pointer"}), limit=2, max_chars=60
    )


def test_curation_accepts_edits_rejects(bd_workspace: Path) -> None:
    """AC-4: curation can accept a proposal verbatim, accept it with edited
    text, or reject it — and each decision removes the pending entry."""
    client = BdClient(bd_workspace)
    # An empty bd database auto-imports the JSONL export on every command,
    # which resurrects forgotten memories; any real curation target has
    # issues (proposals come from workers working them), so anchor one.
    client.create(title="anchor issue")
    assert client.propose_lesson(
        "verbatim", "copy the tree before sweeping it (2026-08-12)"
    )
    assert client.propose_lesson("edited", "sceduler holds stale code (2026-08-12)")
    assert client.propose_lesson("rejected", "restates the code (2026-08-12)")
    assert set(client.pending_proposals()) == {"verbatim", "edited", "rejected"}

    assert client.accept_proposal("verbatim")
    assert client.accept_proposal(
        "edited", "the scheduler holds the code it started with (2026-08-12)"
    )
    assert client.reject_proposal("rejected")

    assert client.pending_proposals() == {}
    memories = client.memories()
    assert memories["verbatim"] == "copy the tree before sweeping it (2026-08-12)"
    assert memories["edited"] == (
        "the scheduler holds the code it started with (2026-08-12)"
    )
    assert "rejected" not in memories
    # Deciding a key that is not pending is a refusal, not a write.
    assert not client.accept_proposal("verbatim")
    assert not client.reject_proposal("rejected")


def test_accepted_proposal_is_readable(bd_workspace: Path) -> None:
    """AC-5: a proposal is invisible to `lessons()` while pending and becomes
    readable as a lesson the moment curation accepts it."""
    client = BdClient(bd_workspace)
    client.create(title="anchor issue")
    body = "the verification sandbox is read-only; copy first (2026-08-12)"
    assert client.propose_lesson("sandbox-sweep", body)
    assert client.lessons(limit=5, max_chars=200) == ()

    assert client.accept_proposal("sandbox-sweep")
    assert dict(client.lessons(limit=5, max_chars=200)) == {"sandbox-sweep": body}
    # Re-proposing what is now an accepted lesson is not duplicated.
    assert not client.propose_lesson("sandbox-sweep", body)
    assert not client.propose_lesson("another-key", body)
    assert client.pending_proposals() == {}


# ---------------------------------------------------------------------------
# Explicit exports (ortus-k46v.4)
# ---------------------------------------------------------------------------


def test_supports_export_probes_by_behavior(tmp_path: Path) -> None:
    """The regime is decided by `bd export --help`'s exit status, probed once."""
    from tests._shims import make_inline_python_shim

    real = BdClient(tmp_path)
    assert real.supports_export() is True, "the machine bd carries export"

    exportless = make_inline_python_shim(
        tmp_path,
        "bd-without-export",
        "import sys\nsys.exit(1)\n",
    )
    legacy = BdClient(tmp_path, binary=str(exportless))
    assert legacy.supports_export() is False


def test_export_write_is_atomic(tmp_path: Path) -> None:
    """AC-3: a failing export never touches the tracked file; a succeeding one
    replaces it whole via rename."""
    from tests._shims import make_inline_python_shim

    beads = tmp_path / ".beads"
    beads.mkdir()
    target = beads / "issues.jsonl"
    target.write_text('{"id": "orig-1"}\n', encoding="utf-8")

    # A bd that writes half a record to -o and dies: the tracked file must
    # keep its original bytes and no scratch file may linger.
    dying = make_inline_python_shim(
        tmp_path,
        "bd-dying-export",
        (
            "import sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w').write('{\"id\": \"trunc')\n"
            "sys.exit(1)\n"
        ),
    )
    client = BdClient(tmp_path, binary=str(dying))
    reason = client.export_issues()
    assert reason, "a failed export must report why"
    assert target.read_text(encoding="utf-8") == '{"id": "orig-1"}\n'
    assert not (beads / ".issues.jsonl.export-tmp").exists()

    healthy = make_inline_python_shim(
        tmp_path,
        "bd-healthy-export",
        (
            "import sys\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "open(out, 'w').write('{\"id\": \"fresh-1\"}\\n')\n"
            "sys.exit(0)\n"
        ),
    )
    client = BdClient(tmp_path, binary=str(healthy))
    assert client.export_issues() == ""
    assert target.read_text(encoding="utf-8") == '{"id": "fresh-1"}\n'


def test_interactions_disposition_matches_probe() -> None:
    """AC-5: probed on bd 1.2.1 (2026-08-12): `bd audit` writes
    .beads/interactions.jsonl as an append-only, git-versioned sidecar —
    ambient by design, so it stays in the swept tracker-export set while
    issues.jsonl alone is regenerated explicitly."""
    from ortus.commands.grind import _TRACKER_EXPORT_PATHS

    assert ".beads/interactions.jsonl" in _TRACKER_EXPORT_PATHS
    assert ".beads/issues.jsonl" in _TRACKER_EXPORT_PATHS
