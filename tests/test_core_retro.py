"""The bounded advisory retrospective over run records (ortus-v8bj).

The pass itself is a model, so what is testable is everything around it: the
bounded window of records it reads, the envelope it must speak through, the
pending state its proposals land in, and the clean exits when there is nothing
to read or no model to read with. Hermetic tests drive that machinery with a
fake backend and a fake recorder; the tests that prove proposals really land
pending run against a real `bd` workspace and are marked integration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.profiles import AgentProfile, Phase
from ortus.core.retro import (
    ENVELOPE_PREFIX,
    ISSUE_KEY_PREFIX,
    MAX_PROPOSALS_PER_KIND,
    Proposal,
    RetroFailed,
    collect_records,
    parse_proposals,
    record_proposals,
    retro_prompt,
    run_retrospective,
)

TODAY = "2026-08-12"

cli = CliRunner()


def _event(text: str) -> str:
    return json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}}
    )


def _envelope(payload: dict) -> str:
    return ENVELOPE_PREFIX + " " + json.dumps(payload)


_BOTH_KINDS = {
    "lessons": [
        {
            "key": "sandbox-copy",
            "lesson": "the verification sandbox is read-only; copy before sweeping",
        }
    ],
    "issues": [
        {
            "key": "fix-flaky-wait",
            "title": "grind: the wait loop polls before the journal exists",
            "rationale": "the same symptom appears in three run logs",
        }
    ],
}


class _FakeRunner:
    """Records one retrospective invocation without launching a backend."""

    def __init__(self, *, text: str | None = None, rc: int = 0):
        self.text = text
        self.rc = rc
        self.calls: list[dict[str, object]] = []

    def configure_codegraph(self, capability: object) -> None:
        pass

    def run(self, prompt: str, **kwargs: object) -> int:
        self.calls.append({"prompt": prompt, **kwargs})
        log_path = kwargs["log_path"]
        assert isinstance(log_path, Path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            if self.text is not None:
                fh.write(_event(self.text) + "\n")
        return self.rc


class _Recorder:
    """Fake pending-proposal store: the one write surface the pass has."""

    def __init__(self, *, already_accepted: frozenset[str] = frozenset()):
        self.proposed: list[tuple[str, str]] = []
        self.already_accepted = set(already_accepted)

    def propose_lesson(self, key: str, body: str) -> bool:
        self.proposed.append((key, body))
        return key not in self.already_accepted


def _profile() -> AgentProfile:
    return AgentProfile(backend="claude", phase=Phase.FINALIZE, model="haiku")


def _record(repo: Path, relative: str, text: str, *, mtime: float) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _run(repo: Path, runner: _FakeRunner, bd: _Recorder, *, limit: int = 8):
    return run_retrospective(
        repo,
        bd=bd,
        today=TODAY,
        log_path=repo / "logs" / "retro.log",
        backend="claude",
        profile=_profile(),
        timeout=30.0,
        limit=limit,
        runner_factory=lambda *_: runner,
    )


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_reads_a_bounded_window(tmp_path: Path) -> None:
    """AC-1: the pass reads only the newest `limit` records across all three
    kinds, chosen by modification time, and clips each to its budget."""
    base = 1_700_000_000.0
    for index in range(4):
        _record(
            tmp_path,
            f"logs/grind-2026081{index}-000000.log",
            f"run log {index}\n" + "line\n" * 200,
            mtime=base + index,
        )
        _record(
            tmp_path,
            f"logs/grind-transactions/cand{index}.verifier-1.md",
            # Non-ASCII paths inside a record must not break composition.
            f"verifier report {index} rebuilt `тесты/数据.py`",
            mtime=base + 10 + index,
        )
    _record(
        tmp_path,
        "logs/grind-transaction.json",
        json.dumps({"issue_id": "ortus-xyzw"}),
        mtime=base + 20,
    )

    records, skipped = collect_records(tmp_path, limit=5, max_chars=120)

    assert skipped == ()
    assert len(records) == 5
    # Newest five: the journal, then the four verifier reports; every run log
    # is older and stays outside the window.
    assert [record.kind for record in records] == ["journal"] + ["verification"] * 4
    assert records[0].name == "grind-transaction.json"

    # A run log keeps its tail (the most recent activity), marked as clipped.
    records, _ = collect_records(tmp_path, limit=20, max_chars=120)
    log_records = [record for record in records if record.kind == "run-log"]
    assert log_records, "run logs enter a wide enough window"
    assert log_records[0].text.startswith("[…earlier output omitted]")
    assert log_records[0].text.endswith("line")
    assert len(retro_prompt(records, today=TODAY)) < 20_000


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_lessons_and_issues_are_separate(tmp_path: Path) -> None:
    """AC-2: the envelope's lessons and issues come back as distinct kinds and
    are recorded under distinct pending keys, so curation can tell a durable
    hazard from proposable work."""
    log = tmp_path / "retro.log"
    log.write_text(_event(_envelope(_BOTH_KINDS)) + "\n", encoding="utf-8")

    proposals, notes = parse_proposals(log)

    assert notes == ()
    assert [proposal.kind for proposal in proposals] == ["lesson", "issue"]
    lesson, issue = proposals
    assert lesson.pending_key == "sandbox-copy"
    assert issue.pending_key == ISSUE_KEY_PREFIX + "fix-flaky-wait"
    assert issue.body.startswith("proposed issue: ")
    assert "three run logs" in issue.body

    recorder = _Recorder()
    recorded, duplicates = record_proposals(recorder, proposals, today=TODAY)
    assert duplicates == ()
    assert recorded == proposals
    assert recorder.proposed == [
        ("sandbox-copy", f"{lesson.body} ({TODAY})"),
        (ISSUE_KEY_PREFIX + "fix-flaky-wait", f"{issue.body} ({TODAY})"),
    ]


@pytest.mark.fast
def test_a_duplicate_of_an_accepted_lesson_is_not_recorded_twice(
    tmp_path: Path,
) -> None:
    """Edge case: a proposal an accepted lesson already covers is reported as
    a duplicate, not recorded again."""
    recorder = _Recorder(already_accepted=frozenset({"sandbox-copy"}))
    proposals = (Proposal("lesson", "sandbox-copy", "copy the tree first"),)

    recorded, duplicates = record_proposals(recorder, proposals, today=TODAY)

    assert recorded == ()
    assert duplicates == proposals


# ---------------------------------------------------------------------------
# Pending state, against a real bd workspace
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_proposals_are_pending(tmp_path: Path) -> None:
    """AC-3: retrospective proposals land in exactly the pending state a
    worker's lesson proposal uses — visible to curation, invisible to the
    lesson selection that composes worker contracts."""
    from ortus.core.bd import BdClient
    from tests.conftest import copy_bd_workspace

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    _record(repo, "logs/grind-20260812-000000.log", "one run", mtime=1_700_000_000.0)
    runner = _FakeRunner(text=_envelope(_BOTH_KINDS))

    result = _run(repo, runner, BdClient(repo))

    assert result.message == ""
    client = BdClient(repo)
    pending = client.pending_proposals()
    assert set(pending) == {"sandbox-copy", ISSUE_KEY_PREFIX + "fix-flaky-wait"}
    assert all(f"({TODAY})" in body for body in pending.values())
    assert client.lessons(limit=10, max_chars=400) == ()


@pytest.mark.integration
def test_never_creates_or_accepts(tmp_path: Path) -> None:
    """AC-4: the pass files no issue and accepts nothing — after a full run,
    the issue list is untouched and every write sits under the pending prefix
    awaiting someone else's decision."""
    from ortus.core.bd import LESSON_PROPOSAL_PREFIX, BdClient
    from tests.conftest import copy_bd_workspace

    repo = copy_bd_workspace(tmp_path / "repo", "bare").path
    _record(repo, "logs/grind-20260812-000000.log", "one run", mtime=1_700_000_000.0)
    client = BdClient(repo)
    issues_before = [issue["id"] for issue in client.list_all()]

    _run(repo, _FakeRunner(text=_envelope(_BOTH_KINDS)), client)

    assert [issue["id"] for issue in client.list_all()] == issues_before
    memories = client.memories()
    assert memories, "the proposals were recorded"
    assert all(key.startswith(LESSON_PROPOSAL_PREFIX) for key in memories)


# ---------------------------------------------------------------------------
# Clean exits and skipped records
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_no_records_reports_nothing(tmp_path: Path) -> None:
    """AC-6: a repository with no run records proposes nothing, says so, and
    never launches the model or touches the tracker."""
    runner = _FakeRunner(text=_envelope(_BOTH_KINDS))
    recorder = _Recorder()

    result = _run(tmp_path, runner, recorder)

    assert "no run records" in result.message
    assert result.recorded == ()
    assert runner.calls == []
    assert recorder.proposed == []


@pytest.mark.fast
def test_unparseable_record_is_skipped(tmp_path: Path) -> None:
    """AC-7: a record that cannot be decoded or parsed is skipped with a note;
    the pass still runs over what it could read."""
    _record(tmp_path, "logs/grind-20260812-000000.log", "good run", mtime=1_700_000_000.0)
    bad = tmp_path / "logs" / "grind-transactions" / "cand.verifier-1.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"\xff\xfe not utf-8")
    _record(
        tmp_path, "logs/grind-transaction.json", "{not json", mtime=1_700_000_100.0
    )

    records, skipped = collect_records(tmp_path)
    assert [record.kind for record in records] == ["run-log"]
    assert len(skipped) == 2
    assert any("not valid JSON" in note for note in skipped)
    assert any("unreadable" in note for note in skipped)

    result = _run(tmp_path, _FakeRunner(text=_envelope(_BOTH_KINDS)), _Recorder())
    assert result.message == ""
    assert len(result.recorded) == 2


@pytest.mark.fast
def test_no_model_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-8: with no model available the verb reports and exits zero — a
    retrospective is advisory and its absence is never an error state."""
    (tmp_path / ".beads").mkdir()
    _record(
        tmp_path, "logs/grind-20260812-000000.log", "one run", mtime=1_700_000_000.0
    )
    monkeypatch.setattr("shutil.which", lambda *_args, **_kwargs: None)

    result = cli.invoke(app, ["retro", str(tmp_path)])

    assert result.exit_code == 0
    assert "no model configured" in result.output


# ---------------------------------------------------------------------------
# Failure modes of the pass itself
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize(
    "runner, reason",
    [
        (_FakeRunner(text=None), "found 0"),
        (_FakeRunner(text=_envelope(_BOTH_KINDS), rc=2), "exited 2"),
        (_FakeRunner(text=ENVELOPE_PREFIX + " {not json"), "malformed"),
    ],
)
def test_a_pass_that_produced_nothing_usable_raises(
    tmp_path: Path, runner: _FakeRunner, reason: str
) -> None:
    _record(tmp_path, "logs/grind-20260812-000000.log", "run", mtime=1_700_000_000.0)
    with pytest.raises(RetroFailed) as excinfo:
        _run(tmp_path, runner, _Recorder())
    assert reason in str(excinfo.value)


@pytest.mark.fast
def test_malformed_entries_are_dropped_with_notes(tmp_path: Path) -> None:
    """A bad entry costs itself a note, never the envelope's other findings,
    and each kind is capped rather than growing with the model's appetite."""
    payload = {
        "lessons": [
            {"key": "Not A Slug", "lesson": "text"},
            {"key": "empty-lesson", "lesson": "  "},
            "not an object",
        ]
        + [
            {"key": f"lesson-{index}", "lesson": f"hazard {index}"}
            for index in range(MAX_PROPOSALS_PER_KIND + 2)
        ],
        "issues": "not an array",
    }
    log = tmp_path / "retro.log"
    log.write_text(_event(_envelope(payload)) + "\n", encoding="utf-8")

    proposals, notes = parse_proposals(log)

    assert len(proposals) == MAX_PROPOSALS_PER_KIND
    assert all(proposal.kind == "lesson" for proposal in proposals)
    assert any("kebab-case" in note for note in notes)
    assert any("no lesson text" in note for note in notes)
    assert any("ceiling" in note for note in notes)
    assert any("'issues' is not an array" in note for note in notes)
