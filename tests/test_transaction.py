from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.transaction import (
    FINALIZATION_STEPS,
    JOURNAL_SCHEMA,
    CandidateJournal,
    JournalStore,
    candidate_diff,
    issue_packet_hash,
    sha256_bytes,
)


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


def test_schema_one_journal_loads_for_outer_migration(tmp_path: Path) -> None:
    store = JournalStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "schema": 1,
                "issue_id": "repo-legacy",
                "base_head": "abc123",
                "baseline_paths": [],
                "baseline_fingerprints": {},
                "candidate_paths": ["candidate.py"],
                "phase": "implementation-timeout",
            }
        )
    )

    journal = store.load()

    assert journal is not None
    assert journal.schema == JOURNAL_SCHEMA
    assert journal.issue_id == "repo-legacy"
    assert journal.candidate_paths == ("candidate.py",)
    assert journal.candidate_hash == ""
    assert journal.attempts[0]["migration"] == "schema-v1"
    assert journal.created_at and journal.implementation_started_at


def test_journal_records_packet_candidate_evidence_profiles_and_timestamps(
    tmp_path: Path,
) -> None:
    packet = {"id": "repo-123", "status": "in_progress", "title": "Candidate"}
    store = JournalStore(tmp_path)
    packet_hash, packet_ref = store.save_packet("repo-123", packet)
    diff_hash, diff_ref = store.save_diff(b"diff --git a/x b/x\n")
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-123",
        base_head="abc123",
        baseline_paths=(),
        packet_hash=packet_hash,
        packet_ref=packet_ref,
        profiles={"implementation": "codex/implement", "verification": "codex/verify"},
    ).with_candidate(
        {"x"}, phase="candidate-captured", candidate_hash=diff_hash, diff_ref=diff_ref
    )
    journal = journal.with_evidence({"kind": "test", "returncode": 0})
    journal = journal.begin_verification().finish_verification(
        "logs/report.md", phase="verified-pass"
    )

    store.save(journal)
    loaded = store.load()

    assert loaded == journal
    assert loaded is not None
    assert loaded.issue_packet_hash == issue_packet_hash(packet)
    # Bytewise, with no rstrip: the verifier is told to rehash this artifact,
    # so a trailing newline here would make the advertised digest unverifiable.
    assert (
        sha256_bytes((tmp_path / loaded.issue_packet_ref).read_bytes())
        == loaded.issue_packet_hash
    )
    assert loaded.candidate_hash == sha256_bytes(b"diff --git a/x b/x\n")
    assert loaded.evidence == ({"kind": "test", "returncode": 0},)
    assert loaded.verifier_refs == ("logs/report.md",)
    assert [attempt["phase"] for attempt in loaded.attempts] == [
        "implementation",
        "verification",
    ]
    assert loaded.created_at and loaded.updated_at
    assert loaded.implementation_started_at and loaded.implementation_finished_at
    assert loaded.verification_started_at and loaded.verification_finished_at


def test_candidate_diff_covers_untracked_binary_and_empty_candidate(
    tmp_path: Path,
) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("changed\n")
    (tmp_path / "binary.bin").write_bytes(b"\x00\xffcandidate")
    (tmp_path / "odd\nname.txt").write_text("odd path\n")

    bundle = candidate_diff(tmp_path, {"tracked.txt", "binary.bin", "odd\nname.txt"})

    assert b"tracked.txt" in bundle
    assert b"binary.bin" in bundle
    assert b"odd" in bundle and b"name.txt" in bundle
    assert candidate_diff(tmp_path, ()) == b""


def test_journal_bounds_large_evidence(tmp_path: Path) -> None:
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-large-evidence",
        base_head="abc123",
        baseline_paths=(),
    ).with_evidence({"kind": "transcript", "excerpt": "x" * 100_000})

    assert len(journal.evidence[0]["excerpt"]) < 17_000
    assert journal.evidence[0]["excerpt"].endswith("[truncated]")


def test_journal_records_bounded_corrections_and_plan_gap_routing(
    tmp_path: Path,
) -> None:
    """A retry transition is journaled before the correction worker runs, and a
    plan gap can only be routed once."""
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-corrections",
        base_head="abc123",
        baseline_paths=(),
    )
    assert journal.corrections == 0 and journal.plan_gap_routed is False

    first = journal.begin_correction(findings=("src/demo.py:1 wrong value",))
    second = first.begin_correction(findings=("still wrong",))

    assert (first.corrections, second.corrections) == (1, 2)
    assert second.phase == "correction"
    assert second.attempts[-1]["correction"] == 2
    assert second.attempts[-1]["findings"] == ["still wrong"]
    assert second.attempt > first.attempt

    routed = journal.route_plan_gap()
    assert routed.plan_gap_routed is True and routed.phase == "plan-gap-routed"

    store = JournalStore(tmp_path)
    store.save(second)
    assert store.load() == second


def test_journal_records_each_finalization_boundary(tmp_path: Path) -> None:
    """Boundaries are what make a killed finalization replayable."""
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-final",
        base_head="abc123",
        baseline_paths=(),
    ).with_candidate({"candidate.py"}, phase="verified-pass", candidate_hash="deadbeef")

    for step in FINALIZATION_STEPS:
        assert not journal.finalized(step)
    journal = journal.with_finalization("report")
    journal = journal.with_finalization("close")
    journal = journal.with_finalization("commit", "cafef00d")

    assert journal.finalized("report") and journal.finalized("close")
    assert journal.finalization["commit"] == "cafef00d"
    assert not journal.finalized("sync")
    assert journal.phase == "finalized-commit"

    store = JournalStore(tmp_path)
    store.save(journal)
    assert store.load() == journal

    with pytest.raises(ValueError):
        journal.with_finalization("publish")


def test_schema_two_journal_loads_without_finalization_state(tmp_path: Path) -> None:
    """A journal written before finalization existed resumes as "nothing landed
    yet"; observable bd and git state is what actually gates each replay."""
    store = JournalStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "schema": 2,
                "issue_id": "repo-v2",
                "base_head": "abc123",
                "baseline_paths": [],
                "baseline_fingerprints": {},
                "candidate_paths": ["candidate.py"],
                "candidate_hash": "deadbeef",
                "phase": "verified-pass",
            }
        )
    )

    journal = store.load()

    assert journal is not None
    assert journal.schema == JOURNAL_SCHEMA
    assert journal.finalization == {}
    assert journal.corrections == 0
    assert journal.plan_gap_routed is False
