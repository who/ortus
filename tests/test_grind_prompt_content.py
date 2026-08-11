"""Structural regression guard for src/ortus/prompts/grind-prompt.md (ortus-ylv1).

The grind-prompt is loaded by `src/ortus/core/prompts.py` (FR-025) and
drives every `ortus grind` iteration. Its content was historically the
bash-era `ortus/prompts/ralph-prompt.md`, ported to Python with the
/goal evaluator adaptations (no `<promise>COMPLETE|EMPTY</promise>`
sentinels, queue exhaustion judged by outer `bd ready` poll).

This file enforces:

  1. The structural markers shared with ralph-prompt.md still exist in
     grind-prompt.md (no silent drift / accidental section deletion).
  2. The /goal adaptation invariant holds: grind-prompt.md must NOT
     instruct the model to emit `<promise>COMPLETE</promise>` or
     `<promise>EMPTY</promise>` (the legacy shell-parser sentinels).
     `<promise>BLOCKED</promise>` is allowed as a transcript marker
     (FR-017).
  3. ralph-prompt.md carries a "superseded by grind-prompt.md" preamble
     so future editors don't drift the two apart.

These markers are intentionally coarse — they check that sections still
exist, not their exact wording, so prose tweaks remain free.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GRIND_PROMPT = REPO_ROOT / "src" / "ortus" / "prompts" / "grind-prompt.md"
RALPH_PROMPT = REPO_ROOT / "ortus" / "prompts" / "ralph-prompt.md"


def _content() -> str:
    return GRIND_PROMPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (1) Structural markers — ported sections must still be present
# ---------------------------------------------------------------------------


REQUIRED_MARKERS = [
    # Per-task loop body steps
    "**Orient**",
    "**Select**",
    "**Claim**",
    "**Investigate**",
    "**Implement**",
    "**Verify**",
    # Subagent strategy + scheduler rules
    "Subagent Strategy",
    "Main context = scheduler only",
    "Subagents = disposable memory",
    # Issue Plan JSON schema
    "has_enough_info",
    "implementation_steps",
    "verification_steps",
    "closure_reason",
    # Completion comment format
    "**Changes**:",
    "**Verification**:",
    "**CodeGraph v1**",
    # CodeGraph integration
    "codegraph_available",
    # Important rules
    "One task per invocation",
    # Steering
    "Downstream",
    "tests/lints/builds",
    # BLOCKED transcript marker (kept even after the /goal adaptation)
    "<promise>BLOCKED</promise>",
]


@pytest.mark.parametrize("marker", REQUIRED_MARKERS)
def test_grind_prompt_contains_marker(marker: str) -> None:
    """Each ported section's structural marker must still be present.

    If you intentionally removed a section, delete its marker from this
    list AND update the bd issue history. Otherwise this is a regression.
    """
    body = _content()
    assert marker in body, (
        f"grind-prompt.md is missing structural marker {marker!r}; "
        f"either it was accidentally removed or the list is stale "
        f"(see ortus-ylv1)."
    )


# ---------------------------------------------------------------------------
# (2) /goal adaptation invariant — no shell-parser sentinels to emit
# ---------------------------------------------------------------------------


# Match the EMIT instruction shape, not bare occurrences inside a code fence
# describing what NOT to do. We allow the literal sentinel string to appear
# in prose ("do not output `<promise>COMPLETE</promise>`") but not in a
# context that instructs the model to emit it.
_FORBIDDEN_EMIT_RE = re.compile(
    r"(?:output|emit|print|return)\s+`?<promise>(COMPLETE|EMPTY)</promise>`?",
    re.IGNORECASE,
)


def test_grind_prompt_does_not_instruct_complete_or_empty_emit() -> None:
    """The /goal evaluator owns termination; do not re-introduce shell sentinels."""
    body = _content()
    matches = _FORBIDDEN_EMIT_RE.findall(body)
    assert not matches, (
        "grind-prompt.md re-introduces a shell-parser sentinel emit instruction; "
        f"saw {matches!r}. The outer `bd ready` poll handles queue exhaustion "
        "and the /goal evaluator handles per-task completion (ortus-ylv1)."
    )


def test_grind_prompt_keeps_blocked_as_transcript_marker() -> None:
    """`<promise>BLOCKED</promise>` is retained per FR-017 (claimed-but-stuck signal)."""
    body = _content()
    assert "<promise>BLOCKED</promise>" in body


# ---------------------------------------------------------------------------
# (3) verifier contract — the criterion-id rule must be stated, not implied
# ---------------------------------------------------------------------------


def test_verifier_states_criterion_ids_come_from_the_packet() -> None:
    """The rule `validate_verdict` enforces has to be in the prompt (ortus-wz3v).

    A verifier that was never told the ids are fixed will invent one to record
    something it could not run, and a pass shaped that way is rejected outright.
    """
    from ortus.commands.grind import _verifier_prompt
    from ortus.core.transaction import CandidateJournal

    journal = CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        issue_packet_ref="logs/packet.json",
        issue_packet_hash="b" * 64,
        candidate_hash="a" * 64,
    )
    body = _verifier_prompt(journal, "probe contract").lower()

    assert "ac-n" in body
    assert "issue packet" in body
    assert "exactly once" in body
    assert "invented" in body
    assert "evidence of the criterion" in body


def test_verification_flags_match_ci() -> None:
    """Verification guidance must name the CI gate's own duration/timeout flags.

    Verification used to run its expanded sweep with developer-loop flags, so a
    hermetic test over the budget was invisible until CI rejected it on main
    (ortus-q3lh). Both the verifier contract and the worker prompt now state
    the parity rule, and the expected flags are read out of the workflow so a
    change to the gate cannot silently desync them.
    """
    from tests.conftest import ci_gate_flags

    from ortus.commands.grind import _verifier_prompt
    from ortus.core.transaction import CandidateJournal

    journal = CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        issue_packet_ref="logs/packet.json",
        issue_packet_hash="b" * 64,
        candidate_hash="a" * 64,
    )
    verifier = _verifier_prompt(journal, "probe contract")
    worker = _content()

    for flag in ci_gate_flags():
        assert flag in verifier, (
            f"the verifier contract does not name the CI gate flag {flag!r}; "
            f"verification would judge durations by developer-machine rules."
        )
        assert flag in worker, (
            f"grind-prompt.md verification guidance does not name {flag!r}."
        )

    # The flags judge durations and timeouts; they must not be read as a
    # licence to change which tests are selected.
    assert "slow" in verifier
    assert "never narrow a marker expression" in verifier.lower()


def test_both_phases_instruct_a_parallel_sweep() -> None:
    """AC-2: worker and verifier guidance must both ask for `-n auto`.

    A grind turn spends the majority of its wall clock blocked on pytest, and
    almost all of that is subprocess wait rather than computation, so both
    phases distribute their sweep across cores (ortus-3ehq). The budget stays
    behind: it cannot be enforced while workers contend, so each phase is also
    told that CI — which runs serially — remains the authority on duration.
    """
    from ortus.commands.grind import _verifier_prompt
    from ortus.core.transaction import CandidateJournal

    journal = CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        issue_packet_ref="logs/packet.json",
        issue_packet_hash="b" * 64,
        candidate_hash="a" * 64,
    )
    verifier = _verifier_prompt(journal, "probe contract")
    worker = _content()

    for name, text in (("verifier contract", verifier), ("grind-prompt.md", worker)):
        assert "-n auto" in text, f"{name} does not instruct a parallel sweep"
        # Naming the budget is not enough; the guidance has to say it is CI's,
        # or a reader reproduces the CI command verbatim and gets contention
        # breaches instead of a verdict about the code.
        assert "--enforce-duration-budget" in text
        assert "single-threaded" in text, (
            f"{name} does not say CI runs the budget serially"
        )
        assert "pytest-xdist" in text, (
            f"{name} does not say what to do on a host without pytest-xdist"
        )

    # The worker's own bounded inner loop parallelises too, not just the
    # verifier's expansion.
    assert "uv run pytest -m fast -n auto --test-timeout=30" in worker


# ---------------------------------------------------------------------------
# (4) ralph-prompt superseded-by preamble
# ---------------------------------------------------------------------------


def test_ralph_prompt_marked_superseded() -> None:
    """The legacy bash prompt must carry a 'superseded by grind-prompt.md' note.

    Stops future editors from accidentally drifting ralph-prompt.md away
    from grind-prompt.md while both files coexist (until Phase 5 sunset
    deletes the bash sources).
    """
    if not RALPH_PROMPT.exists():
        pytest.skip("ralph-prompt.md already removed (Phase 5 sunset complete)")
    body = RALPH_PROMPT.read_text(encoding="utf-8")
    assert "SUPERSEDED" in body or "superseded" in body.lower(), body[:400]
    assert "grind-prompt.md" in body


def test_verifier_must_emit_before_optional_investigation() -> None:
    """ortus-pzfd.6: a verifier passed all seven criteria, then ended while
    waiting on a sweep it chose to start, emitting no verdict envelope. Grind
    rejected a candidate it had already approved."""
    from ortus.commands.grind import _verifier_prompt
    from ortus.core.transaction import CandidateJournal

    journal = CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        issue_packet_ref="logs/packet.json",
        issue_packet_hash="b" * 64,
        candidate_hash="a" * 64,
    )
    prompt = _verifier_prompt(journal, "probe contract")

    assert "EMIT THE VERDICT AS SOON AS EVERY CRITERION CHECK HAS RUN" in prompt
    assert "never a reason to withhold one" in prompt
    assert "do not restart the review" in prompt


# ---------------------------------------------------------------------------
# (5) completion-comment quality — the bullets are the commit body (ortus-q3je)
# ---------------------------------------------------------------------------


def test_grind_prompt_states_the_changes_bullets_become_the_commit_body() -> None:
    """AC-6: the format spec must hold the bullets to commit-worthy prose.

    Finalization commits the latest `**Changes**` block verbatim, so the
    quality ceiling of every commit message is set here. Guidance that only
    asked for concision produced bullets no reader could use six months later.
    """
    body = _content()
    assert "become the commit message body" in body
    assert "six months" in body
    assert "names the file or component" in body
    # Nothing else describes the change: the file list the fallback used to
    # print is gone, so a missing block leaves only the issue's own words.
    assert "only prose the commit can carry" in body


def test_grind_prompt_requires_a_refreshed_changes_block_after_a_correction() -> None:
    """AC-6: bullets authored before a review describe code that has changed."""
    body = _content()
    assert "refreshed `**Changes**` block" in body
    assert "final shipped state" in body


def test_grind_prompt_keeps_the_sdlc_out_of_source_comments() -> None:
    """AC-7: code comments explain the code, not the pipeline that produced it."""
    body = _content()
    assert "Comments explain code, not the Ortus SDLC" in body
    assert "belongs in beads" in body


# ---------------------------------------------------------------------------
# (6) bounded background waits — a wedged check costs one bounded wait, not
#     the whole worker timeout (ortus-xjdf)
# ---------------------------------------------------------------------------


def test_background_wait_is_bounded() -> None:
    """AC-1/AC-5: polling caps its attempts; no unbounded wait is described.

    A worker observed 2026-08-10 polled an empty output file for twenty
    minutes because its job's pipeline never closed; nothing bounded the wait
    except the ninety-minute worker timeout, which exists to protect finished
    work, not to diagnose wedged checks.
    """
    body = _content()
    assert "maximum number of polling attempts" in body
    assert "never wait in an unbounded loop" in body
    # The observed anti-pattern must not be demonstrated anywhere in the
    # prompt (AC-5's rg check, asserted here so it cannot quietly return).
    assert "until [ -s" not in body


def test_polled_output_is_redirected_not_piped() -> None:
    """AC-2: a pipeline through a filter cannot report progress."""
    body = _content()
    assert "Redirect output to a file" in body
    assert "never pipe through a filter" in body
    assert "emits nothing until its input reaches end of file" in body
    # Decision 5: the command's own `timeout` kills what it launched, but
    # surviving children can hold the pipe open — it does not end the wait.
    assert "Do not rely on the command's own `timeout`" in body


def test_silent_job_is_a_failure() -> None:
    """AC-3: no output at the bound is a failure to report, not more waiting."""
    body = _content()
    assert "failure to report, not a reason to keep waiting" in body
    # Launching a second job to wait on was part of the observed stall; the
    # bound survives across repeated jobs in one session.
    assert "never resets the bound" in body


def test_abandoned_wait_names_the_command() -> None:
    """AC-4: an abandoned wait must say what it was waiting for."""
    body = _content()
    assert "Name the command when you abandon a wait" in body
    assert "how many times you polled" in body


def test_unfinished_check_is_not_failed_work() -> None:
    """AC-6: a check that outlived its bound is unfinished, not wrong work."""
    body = _content()
    assert "Distinguish an unfinished check from wrong work" in body
    assert "report it as unfinished rather than as failed work" in body
    assert "normal completion, not a wedge" in body
    # Giving up on a check never means giving up the candidate.
    assert "leave the candidate edits intact" in body
