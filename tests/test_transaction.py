from __future__ import annotations

from pathlib import Path

from ortus.core.transaction import CandidateJournal, JournalStore


def test_journal_round_trip_and_baseline_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "operator.txt").write_text("operator baseline\n")
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-123",
        base_head="abc123",
        baseline_paths={"operator.txt"},
    ).with_candidate({"candidate.py"}, phase="verification-timeout")
    store = JournalStore(tmp_path)

    store.save(journal)

    assert store.load() == journal
    assert journal.baseline_is_unchanged(tmp_path)
    (tmp_path / "operator.txt").write_text("changed\n")
    assert not journal.baseline_is_unchanged(tmp_path)
    store.clear()
    assert store.load() is None
