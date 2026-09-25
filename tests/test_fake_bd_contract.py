"""One set of cases run against real `bd` and against `FakeBdClient`.

`tests/_fake_bd.py` exists so a test whose contract is a grind loop decision
does not pay a second of subprocess time per tracker read. That trade is only
honest while the two answer the same questions the same way, and nothing in
the fake can prove that about itself. So every case below is parametrized
over both backends and asserts exclusively through the typed `BdClient`
surface: a fake that drifts from bd fails here, in the file whose job that
is, rather than by quietly changing what some converted loop test observes.

Seeding goes through `seed()` for the few shapes the typed surface cannot
express, so the two backends are set up by the same argument lists too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.bd import BdClient, BdError
from tests._fake_bd import FakeBdClient, seed
from tests.conftest import copy_bd_workspace

pytestmark = pytest.mark.integration


@pytest.fixture(params=["real", "fake"])
def client(request: pytest.FixtureRequest, tmp_path: Path) -> BdClient:
    """A tracker client of each kind, each with a workspace of its own."""
    if request.param == "fake":
        return FakeBdClient(tmp_path)
    return BdClient(copy_bd_workspace(tmp_path / "real", "bare").path)


def _ready_ids(client: BdClient, **kwargs: object) -> list[str]:
    return [row["id"] for row in client.list_ready(**kwargs)]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# lifecycle and the error surface
# ---------------------------------------------------------------------------


def test_contract_lifecycle_and_close_once(client: BdClient) -> None:
    """Create, the status ladder, and a close that only lands once."""
    issue_id = client.create(
        title="full packet",
        issue_type="task",
        priority=1,
        description="desc here",
        design="design here",
        acceptance="acc here",
        notes="notes here",
        labels=["alpha", "beta"],
    )
    shown = client.show(issue_id)
    assert shown["id"] == issue_id
    assert shown["title"] == "full packet"
    assert shown["status"] == "open"
    assert shown["priority"] == 1
    assert shown["description"] == "desc here"
    assert shown["design"] == "design here"
    assert shown["acceptance_criteria"] == "acc here"
    assert shown["notes"] == "notes here"
    assert set(shown["labels"]) == {"alpha", "beta"}

    client.update_status(issue_id, "in_progress")
    assert client.status(issue_id) == "in_progress"

    assert client.close_once(issue_id, reason="verified candidate") is True
    assert client.status(issue_id) == "closed"
    assert client.close_once(issue_id, reason="replayed close") is False
    assert client.show(issue_id)["close_reason"] == "verified candidate"


def test_contract_error_surface_on_an_unknown_id(client: BdClient) -> None:
    """A read of an id the tracker does not hold raises, and `status` absorbs it."""
    with pytest.raises(BdError) as raised:
        client.show("no-such-issue-anywhere")
    assert raised.value.returncode != 0
    assert raised.value.stderr.strip()

    with pytest.raises(BdError):
        client.close("no-such-issue-anywhere")

    assert client.status("no-such-issue-anywhere") == ""
    assert client.has_comment("no-such-issue-anywhere", "## marker") is False


# ---------------------------------------------------------------------------
# labels, excluded labels, and the id sets the loop diffs
# ---------------------------------------------------------------------------


def test_contract_label_filters_across_every_view(client: BdClient) -> None:
    """The label filters grind passes mean the same thing on both backends."""
    plain = client.create(title="plain", issue_type="task", priority=2)
    flagged = client.create(title="operator's", issue_type="task", priority=2)
    claimed = client.create(title="claimed and flagged", issue_type="task", priority=2)
    landed = client.create(title="landed", issue_type="task", priority=2)

    client.add_label(flagged, "human")
    client.add_label(claimed, "human")
    client.update_status(claimed, "in_progress")
    client.update_status(plain, "in_progress")
    client.close(landed)

    assert sorted(client.labels_of(flagged)) == ["human"]
    assert client.labels_of(landed) == []

    assert client.in_progress_ids() == {plain, claimed}
    assert client.in_progress_ids(exclude_labels=("human",)) == {plain}
    assert client.open_ids() == {flagged}
    assert client.open_ids(labels=("human",)) == {flagged}
    assert client.open_ids(labels=("nobody",)) == set()
    assert client.closed_ids() == {landed}

    assert client.count_by_status("open") == 1
    assert client.count_by_status("open", exclude_labels=("human",)) == 0
    assert client.count_by_status("in_progress", exclude_labels=("human",)) == 1
    assert client.count_by_status("closed") == 1

    assert {row["id"] for row in client.list_open()} == {flagged}
    assert {row["id"] for row in client.list_all()} == {plain, flagged, claimed, landed}

    client.remove_label(flagged, "human")
    assert client.labels_of(flagged) == []
    assert client.count_by_status("open", exclude_labels=("human",)) == 1


# ---------------------------------------------------------------------------
# ready ordering, epics, and dependency blocking
# ---------------------------------------------------------------------------


def test_contract_ready_ordering_and_blocking(client: BdClient) -> None:
    """`bd ready` is priority-ordered, keeps epics, and hides blocked work."""
    epic = client.create(title="container", issue_type="epic", priority=1)
    urgent = client.create(title="urgent", issue_type="task", priority=0)
    blocker = client.create(title="must land first", issue_type="task", priority=3)
    blocked = client.create(title="waits", issue_type="task", priority=2)
    seed(client, "dep", "add", blocked, blocker)

    assert _ready_ids(client) == [urgent, epic, blocker]
    assert blocked not in _ready_ids(client)

    client.add_label(urgent, "human")
    assert _ready_ids(client, exclude_labels=("human",)) == [epic, blocker]

    client.close(blocker)
    assert blocked in _ready_ids(client)
    assert blocker not in _ready_ids(client)


def test_contract_parent_child_subtree(client: BdClient) -> None:
    """`children` is the direct subtree and keeps a child after it closes."""
    epic = client.create(title="epic", issue_type="epic", priority=1)
    kid = seed(
        client,
        "create", "--silent", "--title", "kid", "--type", "task",
        "--priority", "2", "--parent", epic,
    )

    assert [row["id"] for row in client.children(epic)] == [kid]
    assert client.children(kid) == []

    client.close(kid)
    kids = client.children(epic)
    assert len(kids) == 1
    assert kids[0]["status"] == "closed"


# ---------------------------------------------------------------------------
# comments and the snapshot block that memoizes them
# ---------------------------------------------------------------------------


def test_contract_comment_thread_and_markers(client: BdClient) -> None:
    """A thread reads back in order, and `has_comment` matches only its marker."""
    issue_id = client.create(title="commented", issue_type="task", priority=2)
    marker = "## Ortus finalization record"

    assert client.comments(issue_id) == []
    assert client.has_comment(issue_id, marker) is False

    client.add_comment(issue_id, "## Independent verification — VERDICT: PASS")
    assert client.has_comment(issue_id, marker) is False

    client.add_comment(issue_id, f"{marker}\n\nIssue: {issue_id}\n")
    assert client.has_comment(issue_id, marker) is True

    bodies = [str(entry.get("text") or "") for entry in client.comments(issue_id)]
    assert len(bodies) == 2
    assert bodies[0].startswith("## Independent verification")
    assert bodies[1].startswith(marker)


def test_contract_snapshot_block_agrees_with_direct_reads(client: BdClient) -> None:
    """A reading answers what the tracker would, and a write through it invalidates."""
    plain = client.create(title="plain", issue_type="task", priority=2)
    flagged = client.create(title="flagged", issue_type="task", priority=2)
    client.add_label(flagged, "human")
    client.update_status(plain, "in_progress")
    client.update_status(flagged, "in_progress")

    with client.snapshot():
        assert client.in_progress_ids() == {plain, flagged}
        assert client.in_progress_ids(exclude_labels=("human",)) == {plain}
        assert client.count_by_status("in_progress") == 2
        assert sorted(client.labels_of(flagged)) == ["human"]
        assert client.show(plain)["title"] == "plain"
        # An invalidating write drops what the reading holds, so the read
        # after it observes the write instead of the listing taken before.
        client.add_label(plain, "human")
        assert client.labels_of(plain) == ["human"]
        assert client.in_progress_ids(exclude_labels=("human",)) == set()
        assert client.count_by_status("in_progress", exclude_labels=("human",)) == 0
        # A close is one of those writes. The reading is deliberately taken
        # again between the label write and the close, so the close is the
        # only write standing between the listing and the reads below: every
        # view derived from it has to observe the close inside the block, not
        # only once the block ends, and the memoized `show` behind `status`
        # — the read that makes `close_once` idempotent — has to go with them.
        client.remove_label(plain, "human")
        assert client.in_progress_ids() == {plain, flagged}
        assert client.status(plain) == "in_progress"
        client.close(plain)
        assert client.closed_ids() == {plain}
        assert client.count_by_status("closed") == 1
        assert client.in_progress_ids() == {flagged}
        assert client.status(plain) == "closed"

    assert client.closed_ids() == {plain}
    assert client.in_progress_ids() == {flagged}


def test_contract_memories_feed_bounded_lessons(client: BdClient) -> None:
    """`memories` reads back what was remembered; `lessons` clips and bounds it."""
    seed(client, "remember", "copy the tree before sweeping it", "--key", "sandbox-sweep")
    seed(client, "remember", "the scheduler holds what it started with " * 20,
         "--key", "stale-scheduler")
    seed(client, "remember", "pointer to the readiness contract", "--key", "pointer")

    memories = client.memories()
    assert memories["sandbox-sweep"] == "copy the tree before sweeping it"
    assert "schema_version" not in memories

    lessons = client.lessons(
        exclude_keys=frozenset({"pointer"}), limit=2, max_chars=60
    )
    assert [key for key, _ in lessons] == ["sandbox-sweep", "stale-scheduler"]
    assert dict(lessons)["stale-scheduler"].endswith(" […]")
    assert all(len(body) <= 60 + len(" […]") for _, body in lessons)
