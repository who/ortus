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
    contract_packet_changes,
    issue_packet_hash,
    moved_sealed_paths,
    restore_sealed_path,
    seal_paths,
    sha256_bytes,
)


def _claimed_packet(**overrides: object) -> dict[str, object]:
    """A realistic `bd show --json` packet for a claimed issue."""

    packet: dict[str, object] = {
        "id": "repo-znhv",
        "title": "Hash the issue contract",
        "description": "## Objective\nStop metadata edits from aborting a grind.",
        "design": "## Scope\nInvert the filter to an allowlist.",
        "acceptance_criteria": "- AC-1: a label edit leaves the hash unchanged.",
        "issue_type": "bug",
        "status": "in_progress",
        "priority": 0,
        "owner": "operator@example.com",
        "created_by": "operator",
        "created_at": "2026-08-09T01:56:58Z",
        "updated_at": "2026-08-09T02:35:04Z",
        "started_at": "2026-08-09T02:35:04Z",
        "estimated_minutes": 90,
        "labels": ["dx", "orchestration"],
        "dependencies": [],
        "dependents": [],
        "notes": "Two incidents on 2026-08-08.",
        "comments": [],
    }
    packet.update(overrides)
    return packet


def test_packet_hash_ignores_labels() -> None:
    """AC-1: the escalation convention writes a label; that must not be fatal.

    An implementation worker that measures a criterion as unmeetable is told by
    this repo to add the `human` label. Hashing labels made following that
    instruction end the session.
    """

    claimed = _claimed_packet()
    baseline = issue_packet_hash(claimed)

    flagged = _claimed_packet(labels=["dx", "orchestration", "human"])
    unlabelled = _claimed_packet(labels=[])

    assert issue_packet_hash(flagged) == baseline
    assert issue_packet_hash(unlabelled) == baseline
    assert contract_packet_changes(claimed, flagged) == ()


def test_packet_hash_ignores_scheduling_metadata() -> None:
    """AC-2: priority, dependency edges and notes are bookkeeping, not contract.

    `dependents` is the sharpest of the three: `bd dep add <other> <claimed>`
    writes it onto an issue that was not even an argument to the command, so
    hashing it let an unrelated edit poison a claimed issue.
    """

    claimed = _claimed_packet()
    baseline = issue_packet_hash(claimed)

    for mutated in (
        _claimed_packet(priority=2),
        _claimed_packet(dependents=["repo-j3xw"]),
        _claimed_packet(dependencies=["repo-kwfm"]),
        _claimed_packet(notes="Measured 6.9% against a 25% threshold."),
        _claimed_packet(estimated_minutes=45),
        _claimed_packet(owner="someone-else@example.com"),
        _claimed_packet(field_bd_added_later="whatever"),
    ):
        assert issue_packet_hash(mutated) == baseline
        assert contract_packet_changes(claimed, mutated) == ()


def test_packet_hash_tracks_contract_fields() -> None:
    """AC-3: a genuine edit to what the issue asks for still moves the hash."""

    claimed = _claimed_packet()
    baseline = issue_packet_hash(claimed)

    for field, value in (
        ("title", "A different ask"),
        ("description", "## Objective\nSomething else entirely."),
        ("design", "## Scope\nA different plan."),
        ("acceptance_criteria", "- AC-1: something else is observable."),
        ("issue_type", "feature"),
    ):
        mutated = _claimed_packet(**{field: value})
        assert issue_packet_hash(mutated) != baseline, field
        changed = contract_packet_changes(claimed, mutated)
        assert len(changed) == 1 and changed[0].startswith(f"{field}: "), changed

    # A worker that moved a contract field *and* a label is still caught, and
    # the report names only the contract field.
    both = _claimed_packet(title="A different ask", labels=["human"])
    assert issue_packet_hash(both) != baseline
    assert [
        entry.split(":", 1)[0] for entry in contract_packet_changes(claimed, both)
    ] == ["title"]


def test_packet_hash_treats_a_missing_contract_field_as_empty() -> None:
    """An absent contract field and an empty one are the same contract."""

    absent = _claimed_packet()
    del absent["design"]

    assert issue_packet_hash(absent) == issue_packet_hash(_claimed_packet(design=""))
    assert issue_packet_hash(absent) == issue_packet_hash(_claimed_packet(design=None))
    assert contract_packet_changes(absent, _claimed_packet(design="")) == ()


def test_contract_packet_changes_truncates_and_names_no_author() -> None:
    """AC-5: the report carries a bounded before and after, and no culprit."""

    before = _claimed_packet(description="old " * 200)
    after = _claimed_packet(description="new " * 200)

    (entry,) = contract_packet_changes(before, after, width=20)

    assert entry.startswith("description: ")
    assert "…" in entry, entry
    assert len(entry) < 120, entry
    assert "worker" not in entry


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


def test_journal_records_handoffs_and_deduplicates_disowned_paths(
    tmp_path: Path,
) -> None:
    """A repeatedly-resumed transaction accumulates audit records, not state: the
    disowned set stays deduplicated and the handoff log stays bounded."""
    (tmp_path / "inherited.txt").write_text("prior engineer work\n")
    journal = CandidateJournal.start(
        repo=tmp_path,
        issue_id="repo-handoff",
        base_head="abc123",
        baseline_paths=(),
    )

    resumed = journal.with_handoff(
        repo=tmp_path,
        paths={"inherited.txt"},
        notes=("HEAD moved from abc123 to def456",),
        base_head="def456",
    )

    assert resumed.base_head == "def456"
    assert resumed.handoff_paths == ("inherited.txt",)
    assert resumed.handoff_fingerprints["inherited.txt"]
    assert resumed.handoffs[-1]["notes"] == ["HEAD moved from abc123 to def456"]
    assert resumed.handoffs[-1]["resumed_phase"] == "implementation"

    disowned = resumed.with_unrelated({"inherited.txt"}).with_unrelated(
        {"inherited.txt"}
    )
    assert disowned.unrelated_paths == ("inherited.txt",)

    for _ in range(12):
        disowned = disowned.with_handoff(repo=tmp_path, paths={"inherited.txt"})
    assert len(disowned.handoffs) == 8

    store = JournalStore(tmp_path)
    store.save(disowned)
    assert store.load() == disowned


def test_newer_or_corrupt_journal_reports_context_instead_of_blocking(
    tmp_path: Path,
) -> None:
    """AC-4: a journal a newer Ortus wrote still names the issue that owns the
    uncommitted work, so it loads with notes; only a journal with no usable
    identity at all loads as None."""
    store = JournalStore(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "schema": JOURNAL_SCHEMA + 5,
                "issue_id": "repo-future",
                "base_head": "abc123",
                "baseline_paths": [],
                "baseline_fingerprints": {},
                "candidate_paths": ["candidate.py"],
                "phase": "incomplete-candidate",
                "field_from_the_future": {"unreadable": True},
            }
        )
    )

    journal, notes = store.load_state()

    assert journal is not None and journal.issue_id == "repo-future"
    assert journal.candidate_paths == ("candidate.py",)
    rendered = " ".join(notes)
    assert f"journal schema {JOURNAL_SCHEMA + 5}" in rendered
    assert "field_from_the_future" in rendered

    store.path.write_text("{not json at all")
    unusable, why = store.load_state()
    assert unusable is None
    assert "not valid JSON" in " ".join(why)

    store.clear()
    assert store.load_state() == (None, ())


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


# ---------------------------------------------------------------------------
# ortus-9yh9 — sealing a candidate so a rebuilt path can be put back
# ---------------------------------------------------------------------------


def test_seal_restores_content_mode_symlinks_and_absences(tmp_path: Path) -> None:
    """Every worktree shape a candidate path can take goes back as sealed.

    Byte-exact rather than text: an artifact is as likely to be an image as a
    transpiled module, and a restore that round-tripped through text would
    corrupt the first while looking correct on the second.
    """
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "logo.png").write_bytes(b"\x89PNG\x00\xff built\x00")
    (tmp_path / "build.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (tmp_path / "build.sh").chmod(0o755)
    (tmp_path / "current").symlink_to("dist/logo.png")
    paths = ["absent.lock", "build.sh", "current", "dist/logo.png"]

    sealed = seal_paths(tmp_path, paths)
    assert moved_sealed_paths(tmp_path, sealed) == ()

    (tmp_path / "dist" / "logo.png").write_bytes(b"\x89PNG\x00\xff rebuilt\x00\x00")
    (tmp_path / "build.sh").chmod(0o644)
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to("build.sh")
    (tmp_path / "absent.lock").write_text("written by a dependency install\n")

    assert moved_sealed_paths(tmp_path, sealed) == tuple(paths)
    for path in paths:
        restore_sealed_path(tmp_path, path, sealed[path])

    assert moved_sealed_paths(tmp_path, sealed) == ()
    assert (tmp_path / "dist" / "logo.png").read_bytes() == b"\x89PNG\x00\xff built\x00"
    assert (tmp_path / "build.sh").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "current").readlink() == Path("dist/logo.png")
    assert not (tmp_path / "absent.lock").exists()


def test_restore_reports_a_path_it_cannot_put_back(tmp_path: Path) -> None:
    """A candidate Ortus cannot restore raises rather than reporting success —
    continuing there would commit bytes no reviewer ever saw."""
    (tmp_path / "artifact.js").write_text("// built\n")
    sealed = seal_paths(tmp_path, ["artifact.js"])
    (tmp_path / "artifact.js").unlink()
    (tmp_path / "artifact.js").mkdir()

    with pytest.raises(OSError):
        restore_sealed_path(tmp_path, "artifact.js", sealed["artifact.js"])
