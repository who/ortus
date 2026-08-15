from __future__ import annotations

from pathlib import Path

import pytest

from ortus.core.transaction import (
    candidate_diff,
    contract_packet_changes,
    issue_packet_hash,
    moved_sealed_paths,
    restore_sealed_path,
    seal_paths,
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
