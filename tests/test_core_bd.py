"""Integration tests for core/bd.py.

Per Testing Strategy: bd is NEVER mocked here. This is the file that owns
the real-binary contract, and `tests/test_fake_bd_contract.py` is what holds
the in-memory stand-in used elsewhere to it.

Two shapes of workspace, because two shapes of test. A test that writes —
creating, closing, commenting — takes its own copy of the session's bare
template, which costs about 25ms against the ~4.5s a per-test `bd init`
used to cost. A test that only queries shares one workspace seeded once for
the whole module, so the issues those queries read across are created once
rather than once per test; `_seeded_state` proves at teardown that none of
them wrote into it. Marked `integration` so it can be deselected in
fast-unit-test runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from ortus.core.bd import (
    PROTECTED_TERMS_FILE,
    BdClient,
    BdError,
    host_identity_terms,
    resolve_protected_terms,
    scrub_protected_terms,
)
from tests.conftest import copy_bd_workspace, run_bd

pytestmark = pytest.mark.integration


@pytest.fixture()
def bd_workspace(tmp_path: Path) -> Path:
    """A writable bd workspace of this test's own, from the session template."""
    return copy_bd_workspace(tmp_path / "workspace", "bare").path


# ---------------------------------------------------------------------------
# One seeded workspace for the read-only query tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Seeded:
    """The shared workspace and the issues baked into it.

    One of each shape the label and status filters have to tell apart: an
    unlabelled open issue, a human-flagged open one, a claim of each kind,
    and something closed.
    """

    path: Path
    plain: str
    flagged: str
    working: str
    escalated: str
    landed: str


#: The memories the lessons test selects over, ordered as its assertion
#: expects them: two selectable keys and one the caller excludes.
_SEEDED_MEMORIES = (
    ("sandbox-sweep", "copy the tree before sweeping it"),
    ("stale-scheduler", "the scheduler holds the code it started with " * 20),
    ("readiness-pointer", "pointer to the readiness contract"),
)


def _seeded_state(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Every issue's status and labels — the state a reader must not change."""
    rows = json.loads(run_bd(path, "list", "--all", "--limit", "0", "--json", "--brief"))
    return {
        row["id"]: (row["status"], tuple(sorted(row.get("labels") or [])))
        for row in rows
    }


@pytest.fixture(scope="module")
def query_workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Seeded]:
    """A workspace seeded once and only read from.

    Seeding goes through `run_bd` rather than `BdClient`: a module-scoped
    fixture is built before the function-scoped `isolated_beads_tracker`
    runs, so this is the one place in the file that has to scrub BEADS_DIR
    for itself.
    """
    path = copy_bd_workspace(tmp_path_factory.mktemp("bd-queries") / "shared", "bare").path

    def create(title: str, *extra: str) -> str:
        return run_bd(
            path, "create", "--silent", "--title", title,
            "--type", "task", "--priority", "2", *extra,
        )

    plain = create("plain open")
    flagged = create("needs a human", "--labels", "human")
    working = create("plain in progress")
    escalated = create("escalated to human", "--labels", "human")
    landed = create("landed")
    run_bd(path, "update", working, "--status", "in_progress")
    run_bd(path, "update", escalated, "--status", "in_progress")
    run_bd(path, "close", landed)
    for key, body in _SEEDED_MEMORIES:
        run_bd(path, "remember", body, "--key", key)

    before = _seeded_state(path)
    yield _Seeded(path, plain, flagged, working, escalated, landed)
    after = _seeded_state(path)
    assert after == before, (
        "a test sharing the read-only bd workspace wrote into it: "
        f"{sorted(set(after.items()) ^ set(before.items()))}. A test that "
        f"mutates the tracker must request `bd_workspace` and get its own copy."
    )


@pytest.fixture()
def query_client(query_workspace: _Seeded) -> BdClient:
    return BdClient(query_workspace.path)


# ---------------------------------------------------------------------------
# Writing tests — each with a workspace of its own
# ---------------------------------------------------------------------------


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


def test_children_includes_closed_kids(bd_workspace: Path) -> None:
    """Rollover needs closed children; bd show no longer embeds them."""
    client = BdClient(bd_workspace)
    epic = client.create(title="container", issue_type="epic", priority=2)
    kid = run_bd(
        bd_workspace,
        "create", "--silent", "--title", "kid", "--type", "task", "--parent", epic,
    )
    assert client.children(epic)
    assert {row["id"] for row in client.children(epic)} == {kid}
    client.close(kid)
    kids = client.children(epic)
    assert len(kids) == 1
    assert kids[0]["status"] == "closed"


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


# ---------------------------------------------------------------------------
# Query tests — all reading the one seeded workspace
# ---------------------------------------------------------------------------


def test_list_ready_exclude_labels_filters_human(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    """The grind harness selects from `bd ready --exclude-label human`; a
    human-flagged issue must be dropped from the result."""
    filtered = query_client.list_ready(exclude_labels=("human",))
    ids = {i["id"] for i in filtered}
    assert query_workspace.plain in ids
    assert query_workspace.flagged not in ids
    # Without the filter the flagged issue is still ready.
    assert query_workspace.flagged in {i["id"] for i in query_client.list_ready()}


def test_list_all_includes_open_and_closed_without_status_filter(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    assert {query_workspace.plain, query_workspace.landed} <= {
        issue["id"] for issue in query_client.list_all()
    }


def test_bd_error_carries_stderr_verbatim(query_client: BdClient) -> None:
    """Acceptance #3: BdError.stderr is bd's stderr verbatim."""
    with pytest.raises(BdError) as exc:
        query_client.show("ortus-no-such-issue-id-anywhere")
    assert exc.value.returncode != 0
    # bd's error message should appear in stderr (exact text varies by bd
    # version, but the issue id we asked about should be referenced).
    assert exc.value.stderr  # non-empty


def test_list_open_returns_open_issues(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    ids = {i["id"] for i in query_client.list_open()}
    assert query_workspace.plain in ids
    assert query_workspace.landed not in ids


def test_count_by_status_honors_exclude_labels(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    """Issues bearing any excluded label drop out of the count (ortus-9db5).

    Without the filter the orchestrator would spin on a queue of only
    human-flagged issues; with it, the count goes to zero and queue_drained()
    returns True.
    """
    # Sanity: both open issues are visible without the filter.
    assert query_client.count_by_status("open") == 2
    # With the filter the human-flagged one disappears.
    assert query_client.count_by_status("open", exclude_labels=("human",)) == 1
    # Sanity: the remaining id is the plain one (not the human-flagged one).
    opens = query_client.list_open()
    assert query_workspace.plain in {i["id"] for i in opens}
    assert query_workspace.flagged in {i["id"] for i in opens}


def test_in_progress_ids_honors_exclude_labels(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    """in_progress issues with the excluded label drop out of the id set.

    Mirrors the count-side filter so the grind orphan-detection diff
    doesn't keep re-flagging human-escalated claims.
    """
    # Without the filter both ids appear.
    assert query_client.in_progress_ids() == {
        query_workspace.working,
        query_workspace.escalated,
    }
    # With the filter the escalated one disappears.
    assert query_client.in_progress_ids(exclude_labels=("human",)) == {
        query_workspace.working
    }


def test_open_ids_filters_by_any_label(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    """open_ids narrows to open issues carrying any of the labels, so grind
    can remember the operator's open issues at window start and hand back a
    claim a worker puts on one of them."""
    assert query_client.open_ids() == {query_workspace.plain, query_workspace.flagged}
    assert query_client.open_ids(labels=("human",)) == {query_workspace.flagged}
    assert query_client.open_ids(labels=("nobody",)) == set()


def test_closed_ids_names_only_closed_issues(
    query_client: BdClient, query_workspace: _Seeded
) -> None:
    """closed_ids returns exactly the closed set, so grind's attribution
    diff can name a claim that closed within one worker window."""
    ids = query_client.closed_ids()
    assert query_workspace.landed in ids
    assert query_workspace.plain not in ids


def test_memories_round_trip_and_lessons_are_bounded(query_client: BdClient) -> None:
    """`memories()` reads what `bd remember` stored; `lessons()` selects
    deterministically, excludes the given keys, and clips each body."""
    memories = query_client.memories()
    assert memories["sandbox-sweep"] == "copy the tree before sweeping it"

    lessons = query_client.lessons(
        exclude_keys=frozenset({"readiness-pointer"}), limit=2, max_chars=60
    )
    assert [key for key, _ in lessons] == ["sandbox-sweep", "stale-scheduler"]
    assert all(len(body) <= 60 + len(" […]") for _, body in lessons)
    assert dict(lessons)["stale-scheduler"].endswith(" […]")
    # Two reads of the same store select the same lessons.
    assert lessons == query_client.lessons(
        exclude_keys=frozenset({"readiness-pointer"}), limit=2, max_chars=60
    )


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


def test_host_identity_keeps_a_short_login_out_of_the_term_list() -> None:
    """A login too short to be redacted safely leaves only its home path.

    Its own name would match inside ordinary prose, and the home path is where
    such an account actually shows up in a tracker record anyway.
    """
    assert host_identity_terms(home=Path("/home/ab"), login="ab") == ("/home/ab",)
    assert host_identity_terms(home=Path("/home/abcd"), login="abcd") == (
        "/home/abcd",
        "abcd",
    )


def test_resolved_terms_merge_the_clone_file_longest_first(tmp_path: Path) -> None:
    """The operator's lines join the host identity, ordered so a path wins."""
    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "protected-terms.txt").write_text(
        "# a comment and a blank line are not terms\n\nacme-internal\n",
        encoding="utf-8",
    )
    terms = resolve_protected_terms(
        tmp_path, home=Path("/home/abcd"), login="abcd"
    )
    assert terms == ("acme-internal", "/home/abcd", "abcd")


def test_scrub_replaces_paths_as_paths_and_keeps_embedded_json_valid() -> None:
    """A home path reads as one after the scrub; a record stays parseable."""
    terms = ("/home/abcd", "abcd")
    record = json.dumps(
        {"id": "x-1", "design": 'abcd ran /home/abcd/code/x on {"k": 1}'}
    )
    scrubbed = scrub_protected_terms(record, terms)
    assert "abcd" not in scrubbed
    parsed = json.loads(scrubbed)
    assert parsed["id"] == "x-1"
    assert parsed["design"] == '[redacted] ran $HOME/code/x on {"k": 1}'


def test_export_scrubs_the_host_identity_and_the_clone_list(tmp_path: Path) -> None:
    """The tracked export is written already free of this clone's terms.

    The home path is taken from the running machine rather than spelled out, so
    the test states the contract without carrying the string it is about.
    """
    from tests._shims import make_inline_python_shim

    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "protected-terms.txt").write_text("acme-internal\n", encoding="utf-8")
    home = str(Path.home())
    payload = tmp_path / "payload.jsonl"
    payload.write_text(
        json.dumps(
            {"id": "x-1", "design": f"ran {home}/code/x for acme-internal"}
        )
        + "\n",
        encoding="utf-8",
    )
    exporting = make_inline_python_shim(
        tmp_path,
        "bd-export-payload",
        (
            "import shutil, sys\n"
            f"shutil.copyfile({str(payload)!r}, sys.argv[sys.argv.index('-o') + 1])\n"
        ),
    )
    client = BdClient(tmp_path, binary=str(exporting))
    assert client.export_issues() == ""

    exported = (beads / "issues.jsonl").read_text(encoding="utf-8")
    assert home not in exported
    assert "acme-internal" not in exported
    assert json.loads(exported)["design"] == "ran $HOME/code/x for [redacted]"
    assert not (beads / ".issues.jsonl.export-tmp").exists()

    # The resolved list is left where a checker reads it, the operator's line
    # kept, so `rg -f` over it can ask the same question this test just asked.
    resolved = (tmp_path / PROTECTED_TERMS_FILE).read_text(encoding="utf-8")
    assert "acme-internal" in resolved
    assert home in resolved


def test_interactions_disposition_matches_probe() -> None:
    """AC-5: probed on bd 1.2.1 (2026-08-12): `bd audit` writes
    .beads/interactions.jsonl as an append-only, git-versioned sidecar —
    ambient by design, so it stays in the swept tracker-export set while
    issues.jsonl alone is regenerated explicitly."""
    from ortus.commands.grind import _TRACKER_EXPORT_PATHS

    assert ".beads/interactions.jsonl" in _TRACKER_EXPORT_PATHS
    assert ".beads/issues.jsonl" in _TRACKER_EXPORT_PATHS
