"""Oversized output is filed and referenced, never cut down to fit (ortus-6gz3).

Every test here asks the same question of a different boundary: after the
render, is the body still somewhere a reader can get at it? A middle ellipsis
answers no, which is why none of these assertions are satisfied by one.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ortus.core import checks, spill
from ortus.core.checks import render_tracker_comment
from ortus.core.grind_loop import MAX_FIELD_CHARS, format_issue_details
from ortus.core.spill import (
    SPILL_MIN_AGE_SECONDS,
    TRUNCATION_MARKER,
    SpilledOutput,
    prune_spill_dir,
    spill_large_output,
    spill_output_file,
)


def _body(marker: str = "MIDDLE-EVIDENCE", size: int = 20_000) -> str:
    """A body whose middle is exactly what a truncating render would lose."""
    filler = "pytest collected a lot of output\n" * size
    half = len(filler) // 2
    return f"HEAD-EVIDENCE\n{filler[:half]}{marker}\n{filler[half:]}TAIL-EVIDENCE"


def _stale(path: Path, *, days: float) -> Path:
    """Backdate a capture past the age below which nothing is ever pruned."""
    old = time.time() - SPILL_MIN_AGE_SECONDS - days * 86_400
    os.utime(path, (old, old))
    return path


def _captures(spill_dir: Path, count: int, *, size: int = 1_000) -> list[Path]:
    """`count` abandoned bodies, oldest first, as a long-lived seat leaves them."""
    spill_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for index in range(count):
        path = spill_dir / f"old-{index:03d}.log"
        path.write_bytes(b"x" * size)
        written.append(_stale(path, days=count - index))
    return written


def test_helper_spills_an_oversized_body_and_returns_path_size_tail(
    tmp_path: Path,
) -> None:
    """AC-1: the body lands on disk whole; the render is path, size, tail."""
    body = _body()
    spill_dir = tmp_path / "spill"

    record = spill_large_output(body, spill_dir=spill_dir, limit=4_000, name="AC-1")

    assert record.spilled and record.path is not None
    assert record.path.parent == spill_dir
    assert record.path.read_text(encoding="utf-8") == body, "the body is kept whole"
    assert record.size_bytes == len(body.encode("utf-8"))

    rendered = record.render()
    assert str(record.path) in rendered
    assert f"{record.size_bytes} bytes" in rendered
    assert rendered.endswith("TAIL-EVIDENCE")
    assert "MIDDLE-EVIDENCE" not in rendered, "the reference is not the body"
    assert len(rendered) < len(body) // 10


def test_helper_files_a_large_capture_without_reading_it_whole(
    tmp_path: Path,
) -> None:
    """AC-1: a capture already on disk is copied, and only its ends decoded."""
    capture = tmp_path / "AC-2.log"
    body = _body()
    capture.write_bytes(body.encode("utf-8") + b"\xff")
    spill_dir = tmp_path / "spill"

    record = spill_output_file(capture, spill_dir=spill_dir, limit=500, name="AC-2")

    assert record.path is not None
    assert record.path.read_bytes() == capture.read_bytes(), "byte-for-byte copy"
    assert record.size_bytes == capture.stat().st_size
    assert record.note == "decoded with replacement characters"

    rendered = record.render()
    assert str(record.path) in rendered
    assert "TAIL-EVIDENCE" in rendered
    assert "MIDDLE-EVIDENCE" not in rendered
    assert "decoded with replacement characters" in rendered


def test_helper_inlines_a_small_capture_and_writes_no_file(tmp_path: Path) -> None:
    """AC-1: below the threshold nothing is filed and nothing is referenced."""
    capture = tmp_path / "AC-3.log"
    capture.write_text("3 passed in 0.4s\n", encoding="utf-8")
    spill_dir = tmp_path / "spill"

    record = spill_output_file(capture, spill_dir=spill_dir, limit=4_000)

    assert not record.spilled
    assert record.render() == "3 passed in 0.4s\n"
    assert not spill_dir.exists(), "a small output never creates a spill directory"


def test_helper_names_a_failed_spill_instead_of_cutting_silently(
    tmp_path: Path,
) -> None:
    """AC-1: an unusable spill directory is stated, not papered over."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    record = spill_large_output(_body(), spill_dir=blocked, limit=4_000)

    assert record.path is None and record.error
    rendered = record.render()
    assert rendered.startswith("[full output not retained:")
    assert str(blocked) in rendered
    assert TRUNCATION_MARKER.strip() in rendered, "the degraded render says so"
    assert rendered.startswith("[full output not retained:")
    assert "HEAD-EVIDENCE" in rendered and "TAIL-EVIDENCE" in rendered


def test_comment_references_oversized_check_output(tmp_path: Path) -> None:
    """AC-2: a failing criterion's transcript reaches the tracker by reference."""
    body = _body()
    record = checks.CriterionResult(
        criterion_id="AC-1",
        command="uv run pytest -q",
        exit_code=1,
        duration_seconds=12.5,
        output=body,
        verdict=checks.VERDICT_FAIL,
    )
    spill_dir = tmp_path / "spill"

    comment = render_tracker_comment(
        checks.CheckRunResult(ref="HEAD", results=(record,)),
        spill_dir=spill_dir,
    )

    assert "MIDDLE-EVIDENCE" not in comment
    assert "TAIL-EVIDENCE" in comment, "the end of the run still reads in place"
    assert "`uv run pytest -q`" in comment, "the command survives the reference"
    (spilled,) = sorted(spill_dir.iterdir())
    assert str(spilled) in comment
    assert f"{len(body.encode('utf-8'))} bytes" in comment
    assert spilled.read_text(encoding="utf-8") == body
    assert len(comment) < len(body) // 10


def test_comment_keeps_a_short_transcript_in_place(tmp_path: Path) -> None:
    """AC-2: the reference is for bodies that earn it, not for every failure."""
    record = checks.CriterionResult(
        criterion_id="AC-1",
        command="uv run pytest -q",
        exit_code=1,
        duration_seconds=0.9,
        output="E   assert 1 == 2\n1 failed in 0.3s\n",
        verdict=checks.VERDICT_FAIL,
    )

    comment = render_tracker_comment(
        checks.CheckRunResult(ref="HEAD", results=(record,)),
        spill_dir=tmp_path / "spill",
    )

    assert "E   assert 1 == 2" in comment
    assert "full output:" not in comment
    assert not (tmp_path / "spill").exists()


def test_prompt_injection_carries_a_reference_not_the_pasted_body(
    tmp_path: Path,
) -> None:
    """AC-3: a packet field somebody pasted a transcript into is not re-injected."""
    issue = {
        "id": "ortus-6gz3",
        "title": "Large output to file, never truncate",
        "description": _body(),
        "design": "Spill the body, inject the reference.",
    }
    spill_dir = tmp_path / "spill"

    details = format_issue_details(issue, spill_dir=spill_dir)

    assert "MIDDLE-EVIDENCE" not in details
    assert "Title: Large output to file, never truncate" in details
    assert "Spill the body, inject the reference." in details, "small fields inline"
    (spilled,) = sorted(spill_dir.iterdir())
    assert str(spilled) in details
    assert spilled.read_text(encoding="utf-8") == issue["description"]
    assert len(details) < len(issue["description"]) // 10


def test_prompt_injection_leaves_an_ordinary_packet_untouched(
    tmp_path: Path,
) -> None:
    """AC-3: an authored work spec is far below the ceiling and injects whole."""
    design = "Concrete locations\n" * 40
    issue = {"id": "ortus-6gz3", "title": "A packet", "design": design}

    details = format_issue_details(issue, spill_dir=tmp_path / "spill")

    assert len(design) < MAX_FIELD_CHARS
    assert design.strip() in details
    assert not (tmp_path / "spill").exists()


def test_small_and_redact_keeps_output_inline_with_credentials_masked(
    tmp_path: Path,
) -> None:
    """AC-4: below the threshold the body stays, and the secret does not."""
    leaked = "connecting\napi_key = sk-live-not-a-real-key\ndone\n"
    record = checks.CriterionResult(
        criterion_id="AC-1",
        command="uv run pytest -q",
        exit_code=1,
        duration_seconds=0.4,
        output=leaked,
        verdict=checks.VERDICT_FAIL,
    )

    comment = render_tracker_comment(
        checks.CheckRunResult(ref="HEAD", results=(record,)),
        spill_dir=tmp_path / "spill",
    )

    assert "connecting" in comment and "done" in comment
    assert "sk-live-not-a-real-key" not in comment
    assert "[REDACTED]" in comment


def test_small_and_redact_masks_a_spilled_tail_but_not_the_filed_body(
    tmp_path: Path,
) -> None:
    """AC-4: masking guards what travels; the filed artifact stays verbatim."""
    body = _body() + "\ntoken: sk-live-not-a-real-key\n"
    spill_dir = tmp_path / "spill"

    record = spill_large_output(body, spill_dir=spill_dir, limit=4_000)

    rendered = record.render()
    assert "[REDACTED]" in rendered
    assert "sk-live-not-a-real-key" not in rendered
    assert record.path is not None
    assert "sk-live-not-a-real-key" in record.path.read_text(encoding="utf-8")


def test_small_and_redact_leaves_authored_packet_prose_alone(
    tmp_path: Path,
) -> None:
    """AC-4: masking is for captured output, never for a work spec's own words."""
    issue = {
        "id": "ortus-6gz3",
        "title": "A packet",
        "acceptance_criteria": "- AC-1: `rg -n 'api_key = ' src/` finds the reader.",
    }

    details = format_issue_details(issue, spill_dir=tmp_path / "spill")

    assert "api_key = " in details
    assert "[REDACTED]" not in details


def test_small_and_redact_handles_an_empty_capture(tmp_path: Path) -> None:
    """AC-4: a command that printed nothing renders as nothing."""
    empty = tmp_path / "AC-1.log"
    empty.write_bytes(b"")

    record = spill_output_file(empty, spill_dir=tmp_path / "spill", limit=4_000)

    assert record == SpilledOutput(inline="", size_bytes=0)
    assert record.render() == ""


def test_prune_bounds_the_directory_and_keeps_this_run_own_capture(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-1: spilling into an over-full directory prunes it; the new body stays."""
    monkeypatch.setattr(spill, "SPILL_RETAINED_FILES", 5)
    spill_dir = tmp_path / "spill"
    abandoned = _captures(spill_dir, 12)

    record = spill_large_output(
        _body(), spill_dir=spill_dir, limit=4_000, name="AC-1"
    )

    assert record.path is not None and record.path.exists(), "the run keeps its own"
    survivors = sorted(spill_dir.iterdir())
    assert len(survivors) == 5, "the directory is back under the file bound"
    assert record.path in survivors
    assert not any(path.exists() for path in abandoned[:8]), "oldest go first"
    assert all(path.exists() for path in abandoned[8:]), "newest stale ones stay"


def test_prune_leaves_a_concurrent_seat_live_captures_alone(tmp_path: Path) -> None:
    """AC-1: over the bound but young is another seat's business, not ours."""
    spill_dir = tmp_path / "spill"
    live = _captures(spill_dir, 9)
    now = time.time()
    for path in live:
        os.utime(path, (now, now))

    removed = prune_spill_dir(spill_dir, max_files=2, max_bytes=1)

    assert removed == ()
    assert all(path.exists() for path in live)


def test_prune_enforces_the_byte_budget_not_only_the_file_count(
    tmp_path: Path,
) -> None:
    """AC-1: a few enormous bodies are pruned though the count is tiny."""
    spill_dir = tmp_path / "spill"
    heavy = _captures(spill_dir, 4, size=4_000)

    removed = prune_spill_dir(spill_dir, max_files=100, max_bytes=9_000)

    assert len(removed) == 2, "kept bodies fit the budget; the rest are dropped"
    assert set(removed) == set(heavy[:2]), "the oldest two are the ones dropped"
    assert sum(path.stat().st_size for path in spill_dir.iterdir()) <= 9_000


def test_prune_ignores_a_directory_it_cannot_read(tmp_path: Path) -> None:
    """AC-1: a spill directory that is not there yet prunes to nothing, quietly."""
    assert prune_spill_dir(tmp_path / "absent") == ()
    assert not (tmp_path / "absent").exists(), "pruning never creates the directory"
