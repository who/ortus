"""Ownership tests against isolated copies of the real tracker workspace."""

from pathlib import Path

import pytest

from ortus.core.agent import BackendError
from ortus.core.bd import BdClient, BdError
from ortus.core.judge_claim import prepare_bound_issue
from ortus.core.prompts import bundled_prompt_text


@pytest.fixture
def tracker(bd_workspace):
    workspace = bd_workspace("repo", "leaf")
    return BdClient(workspace.path), workspace.issues[0]


def prepare(bd, issue_id, **kwargs):
    return prepare_bound_issue(
        bd, issue_id, goal_template=bundled_prompt_text("goal-prompt"), **kwargs
    )


def test_claim_reload_and_owned_release(tracker):
    bd, issue_id = tracker
    bound = prepare(bd, issue_id)
    assert bound.fresh
    assert bound.issue["status"] == "in_progress"
    assert bound.assignee.startswith("ortus-judge-")
    assert bd.show(issue_id)["assignee"] == bound.assignee
    original = {"BEADS_DIR": "/tmp/tracker", "UV_CACHE_DIR": "/tmp/cache", "BEADS_ACTOR": "old"}
    assert bound.worker_env(original) == {**original, "BEADS_ACTOR": bound.assignee}
    assert original["BEADS_ACTOR"] == "old"
    bound.release(bd)
    released = bd.show(issue_id)
    assert released["status"] == "open"
    assert not released.get("assignee")


def test_competing_claim_cannot_replace_owner(tracker):
    bd, issue_id = tracker
    bd.claim(issue_id, "first-run")
    with pytest.raises(BdError):
        BdClient(bd.repo).claim(issue_id, "second-run")
    assert bd.show(issue_id)["assignee"] == "first-run"


def test_atomic_claim_loses_race_without_overwriting_competitor(tracker, monkeypatch):
    bd, issue_id = tracker
    original_show = bd.show

    def show_then_compete(requested_id):
        current = original_show(requested_id)
        bd._run("--actor", "competitor", "update", issue_id, "--claim")
        return current

    monkeypatch.setattr(bd, "show", show_then_compete)
    with pytest.raises(BdError):
        bd.claim(issue_id, "late-run")
    assert original_show(issue_id)["assignee"] == "competitor"


def test_multiple_leftover_claims_stop_without_changing_either(tracker):
    bd, issue_id = tracker
    other_id = bd.create(title="Another task")
    bd.claim(issue_id, "first-run")
    bd.claim(other_id, "second-run")
    with pytest.raises(BackendError, match="leftover claims"):
        prepare(bd, issue_id)
    assert bd.show(issue_id)["assignee"] == "first-run"
    assert bd.show(other_id)["assignee"] == "second-run"


def test_inherited_claim_and_dirty_work_are_never_released(tracker):
    bd, issue_id = tracker
    bd.claim(issue_id, "previous-run")
    dirty = bd.repo / "unfinished.txt"
    dirty.write_text("unfinished work")
    resumed_client = BdClient(bd.repo)
    bound = prepare(resumed_client, issue_id)
    assert not bound.fresh
    assert bound.worker_env({}) == {"BEADS_ACTOR": "previous-run"}
    bound.release(resumed_client)
    with pytest.raises(BdError, match="not a fresh owned claim"):
        resumed_client.release_claim(issue_id, "previous-run")
    assert bd.show(issue_id)["assignee"] == "previous-run"
    assert dirty.read_text() == "unfinished work"


def test_release_rejects_wrong_owner_and_preserves_claim(tracker):
    bd, issue_id = tracker
    bd.claim(issue_id, "first-run")
    with pytest.raises(BdError):
        bd.release_claim(issue_id, "other-run")
    assert bd.show(issue_id)["status"] == "in_progress"


def test_release_guard_survives_owner_race(tracker, monkeypatch):
    bd, issue_id = tracker
    bd.claim(issue_id, "first-run")
    original_show = bd.show

    def show_then_transfer(requested_id):
        current = original_show(requested_id)
        bd._run("--actor", "first-run", "unclaim", "--if-assignee", "first-run", issue_id)
        bd._run("--actor", "second-run", "update", issue_id, "--claim")
        return current

    monkeypatch.setattr(bd, "show", show_then_transfer)
    with pytest.raises(BdError):
        bd.release_claim(issue_id, "first-run")
    assert original_show(issue_id)["assignee"] == "second-run"


@pytest.mark.parametrize("condition", ["custom goal", ""])
def test_custom_condition_rejected_before_claim(tracker, condition):
    bd, issue_id = tracker
    with pytest.raises(BackendError, match="--condition"):
        prepare(bd, issue_id, condition=condition)
    assert bd.show(issue_id)["status"] == "open"


def test_incompatible_override_rejected_before_claim(tracker):
    bd, issue_id = tracker
    with pytest.raises(BackendError, match="goal prompt override"):
        prepare_bound_issue(bd, issue_id, goal_template="Select any ready issue")
    assert bd.show(issue_id)["status"] == "open"


def test_human_issue_cannot_be_claimed(tracker):
    bd, issue_id = tracker
    bd.add_label(issue_id, "human")
    with pytest.raises(BdError):
        prepare(bd, issue_id)
    assert bd.show(issue_id)["status"] == "open"


def test_different_leftover_claim_does_not_select_another(tracker):
    bd, issue_id = tracker
    bd.claim(issue_id, "previous-run")
    with pytest.raises(BackendError, match="leftover claims"):
        prepare(bd, "some-other-id")
    assert bd.show(issue_id)["assignee"] == "previous-run"


@pytest.mark.parametrize("missing", ["claim", "unclaim"])
def test_unsupported_tracker_fails_before_mutation(tmp_path: Path, missing):
    binary = tmp_path / "old-bd"
    binary.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "update --help" ]; then\n'
        + ("  echo 'old update'\n" if missing == "claim" else "  echo '--claim'\n")
        + 'elif [ "$1 $2" = "unclaim --help" ]; then\n'
        + ("  echo 'old unclaim'\n" if missing == "unclaim" else "  echo '--if-assignee'\n")
        + "else\n  touch mutation-attempted\n  exit 1\nfi\n"
    )
    binary.chmod(0o700)
    with pytest.raises(BdError, match="atomic claim/unclaim"):
        prepare(BdClient(tmp_path, binary=str(binary)), "repo-1")
    assert not (tmp_path / "mutation-attempted").exists()
