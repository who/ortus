"""Unit tests for GitHub-issue → readiness-v1 bead ingest.

No live Grok call. The helper is driven through ``ingest_github_issue`` and
``main`` with an injected store and drafter.
"""

from __future__ import annotations

import json
from pathlib import Path

from ortus.core.github_bead import (
    ALLOWED_AUTHOR,
    INGEST_FAILED_PREFIX,
    GrokDraftError,
    MemoryBeadStore,
    assemble_issue,
    grok_user_prompt,
    ingest_github_issue,
    main,
    parse_json_object,
    readiness_failure_comment,
)
from tests.test_readiness import ready_issue


def _payload(
    *,
    login: str = ALLOWED_AUTHOR,
    number: int = 14,
    title: str = "Translate a GitHub issue",
    body: str = "Match the spec and test the pipeline.",
    labels: tuple[str, ...] = ("bead",),
) -> dict:
    return {
        "action": "labeled",
        "label": {"name": "bead"},
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "user": {"login": login},
            "labels": [{"name": name} for name in labels],
        },
    }


def _valid_draft() -> dict:
    issue = ready_issue()
    return {
        "title": "Ship the bounded behavior",
        "issue_type": "task",
        "priority": 2,
        "description": issue["description"],
        "design": issue["design"],
        "acceptance_criteria": issue["acceptance_criteria"],
    }


class _RecordingDrafter:
    def __init__(self, draft: dict | BaseException) -> None:
        self.draft = draft
        self.calls: list[tuple[str, str, int]] = []

    def __call__(
        self,
        *,
        title: str,
        body: str,
        number: int,
        comments: list[str] | None = None,
    ) -> dict:
        self.calls.append((title, body, number, tuple(comments or ())))
        if isinstance(self.draft, BaseException):
            raise self.draft
        return self.draft


def test_maps_payload_and_creates_when_validate_passes() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(_valid_draft())
    payload = _payload(title="Preview flag", body="Add a preview path.")
    result = ingest_github_issue(payload, store=store, drafter=drafter)
    assert result.status == "created"
    assert result.created
    assert result.close_issue
    assert result.comment == f"filed as {result.bead_id}"
    assert result.bead_id is not None
    assert len(store.created) == 1
    created = store.created[0]
    assert created["external_ref"] == "gh-14"
    packet = created["packet"]
    assert packet["title"] == "Ship the bounded behavior"
    assert "## Objective" in packet["description"]
    assert "## Readiness schema" in packet["design"]
    assert "AC-1" in packet["acceptance_criteria"]
    assert drafter.calls == [("Preview flag", "Add a preview path.", 14, ())]


def test_validate_failure_does_not_create() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter({"title": "not a packet", "description": "no sections"})
    result = ingest_github_issue(_payload(), store=store, drafter=drafter)
    assert result.status == "validate_failed"
    assert not result.created
    assert result.close_issue is False
    assert result.comment is not None
    assert result.comment.startswith(INGEST_FAILED_PREFIX)
    assert "Reason:" in result.comment
    assert "re-runs ingest" in result.comment
    assert store.created == []


def test_second_ingest_same_external_ref_is_idempotent() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(_valid_draft())
    first = ingest_github_issue(_payload(), store=store, drafter=drafter)
    assert first.status == "created"
    drafter_again = _RecordingDrafter(_valid_draft())
    second = ingest_github_issue(_payload(), store=store, drafter=drafter_again)
    assert second.status == "skipped_idempotent"
    assert second.bead_id == first.bead_id
    assert second.created is False
    assert second.close_issue is False
    assert second.comment is None
    assert len(store.created) == 1
    assert drafter_again.calls == []


def test_existing_filed_comment_is_idempotent() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(_valid_draft())
    result = ingest_github_issue(
        _payload(),
        comments=["filed as ortus-abcd"],
        store=store,
        drafter=drafter,
    )
    assert result.status == "skipped_idempotent"
    assert store.created == []
    assert drafter.calls == []
    assert result.comment is None
    assert result.close_issue is False


def test_non_who_author_does_not_create_comment_or_close() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(_valid_draft())
    result = ingest_github_issue(
        _payload(login="someone-else"),
        store=store,
        drafter=drafter,
    )
    assert result.status == "skipped_author"
    assert result.created is False
    assert result.close_issue is False
    assert result.comment is None
    assert store.created == []
    assert drafter.calls == []


def test_main_non_who_does_not_need_bd(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_payload(login="stranger")), encoding="utf-8")
    # Author gate runs before BdClient is constructed, so a missing tracker
    # in tmp_path is fine — and proves the helper did not spend XAI or bd.
    code = main(["--event", str(event), "--repo", str(tmp_path)])
    assert code == 0


def test_parse_json_object_strips_prose_and_fences() -> None:
    raw = 'Here you go:\n```json\n{"title": "ok", "priority": 2}\n```\n'
    assert parse_json_object(raw) == {"title": "ok", "priority": 2}


def test_assemble_issue_joins_section_fields() -> None:
    packet = assemble_issue(
        {
            "title": "From fields",
            "objective": "Do the thing.",
            "behavioral_context": "Before vs after.",
            "scope": "This file.",
            "readiness_schema": "v1",
        },
        title_fallback="fallback",
        draft_id="gh-1-draft",
    )
    assert packet["id"] == "gh-1-draft"
    assert "## Objective" in packet["description"]
    assert "Do the thing." in packet["description"]
    assert "## Readiness schema" in packet["design"]
    assert "v1" in packet["design"]


def test_grok_error_is_plan_gap_not_create() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(GrokDraftError("XAI_API_KEY is not set"))
    result = ingest_github_issue(_payload(), store=store, drafter=drafter)
    assert result.status == "validate_failed"
    assert INGEST_FAILED_PREFIX in (result.comment or "")
    assert "XAI_API_KEY" in (result.comment or "")
    assert store.created == []


def test_drafter_receives_issue_comments() -> None:
    store = MemoryBeadStore()
    drafter = _RecordingDrafter(_valid_draft())
    notes = ["PLAN-GAP: missing command", "Add `uv run pytest tests/test_github_bead.py`"]
    result = ingest_github_issue(_payload(), comments=notes, store=store, drafter=drafter)
    assert result.status == "created"
    assert drafter.calls == [
        (
            "Translate a GitHub issue",
            "Match the spec and test the pipeline.",
            14,
            tuple(notes),
        )
    ]


def test_grok_prompt_includes_comments() -> None:
    text = grok_user_prompt(
        title="T",
        body="Body text",
        number=17,
        comments=["first note", "targeted tests: `uv run pytest tests/x.py`"],
    )
    assert "GitHub issue #17" in text
    assert "Body text" in text
    assert "first note" in text
    assert "uv run pytest tests/x.py" in text


def test_readiness_failure_comment_tells_who_to_reply() -> None:
    body = readiness_failure_comment("gh-17-draft: targeted tests: missing command")
    assert body.startswith(INGEST_FAILED_PREFIX)
    assert "gh-17-draft: targeted tests: missing command" in body
    assert "Comment on this issue (as `who`)" in body


def test_workflow_enforces_who_allowlist_and_secret() -> None:
    text = Path(".github/workflows/bead-from-issue.yml").read_text(encoding="utf-8")
    assert "github.event.issue.user.login == 'who'" in text
    assert "XAI_API_KEY" in text
    assert "BD_VERSION=\"1.2.1\"" in text
    assert "python3 -m ortus.core.github_bead" in text
    assert "label.name == 'bead'" in text
    assert "github-actions[bot]" in text
    assert "issue_comment:" in text
    assert "github.event.comment.user.login == 'who'" in text
    assert "Fail the job when readiness rejected the draft" in text
    # ortus-9zgl: hydrate a local DB from JSONL. A lone import against an
    # uninitialized tracker is the GHA failure; bootstrap is the 1.2.1
    # fresh-clone path (prefix ortus stays in .beads/config.yaml).
    assert "bd bootstrap --yes" in text
    assert "run: bd import .beads/issues.jsonl" not in text
