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
    assert "the fallback commit body" in body
    assert "six months" in body
    assert "names the file or component" in body
    # The worker's own commit message is primary now; the block remains the
    # tracker's record and the body of every deterministically assembled
    # commit.
    assert "writes its own commit message" in body


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


# ---------------------------------------------------------------------------
# (7) throwaway trees — git archive / shared clone, never `git worktree add`
#     (ortus-z7ib)
# ---------------------------------------------------------------------------


def _verifier_contract() -> str:
    """The composed verifier prompt, built the way the verification phase builds it."""
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
    return _verifier_prompt(journal, "probe contract")


def test_work_issue_forbids_worktree_add() -> None:
    """AC-1: the per-issue condition names `git worktree add` among the
    forbidden commands, in the same list as the lifecycle mutations rather
    than a second list an agent might not read."""
    from ortus.core.grind_loop import read_work_issue_condition

    body = read_work_issue_condition()
    assert "`git worktree add`" in body
    # One prohibition list, not two: it joins the existing lifecycle sentence.
    assert "`git reset`, or `git worktree add`" in body
    # A rule that only forbids sends the agent hunting for its own alternative
    # — which is how the leaked registrations happened. Both tiers are named.
    assert "git archive" in body
    assert "git clone --shared" in body


def test_implementation_names_git_archive() -> None:
    """AC-2: the worker contract forbids the worktree and names both tiers.

    Naming only the archive would send every agent that needs a runnable tree
    straight back to `git worktree add`: this repository's version derives
    from vcs metadata, so an archive extraction cannot even install.
    """
    body = _content()
    assert "Never run `git worktree add`" in body
    assert "git archive" in body
    assert "git clone --shared" in body
    # An agent needing only part of a tree is not pushed into copying all of it.
    assert "pathspec" in body.lower()
    # The tier split is stated, not left for the agent to rediscover at a
    # failed build.
    assert "archive extraction cannot even install" in body


def test_verifier_contract_forbids_worktree_add() -> None:
    """AC-3: verification is where a HEAD-plus-candidate tree is most often
    built — one of the two leaked registrations came from a verifier."""
    body = _verifier_contract()
    assert "`git worktree add`" in body
    assert "git archive" in body
    assert "git clone --shared" in body
    assert "archive extraction cannot even install" in body


# ---------------------------------------------------------------------------
# (8) worker commit-message rules — the contract states what the gate enforces
#     (ortus-ogfu)
# ---------------------------------------------------------------------------
#
# The first two fully autonomous landings both had their worker-written commit
# messages rejected by `validate_message` — "the composed body names nothing
# from the diff" — because the gate enforced rules the worker contract never
# stated. Each rejection reason the validator can raise maps here to a phrase
# every writer-facing contract must carry; a validator rule added or removed
# without the contracts catching up fails `test_message_rules_cannot_drift`.

_MESSAGE_RULE_PHRASES: dict[str, str] = {
    # rejection-reason literal in validate_message -> contract phrase
    "the composed subject is empty": "imperative subject",
    "the composed message has a subject and no body": "then a body",
    "the composed subject trails off in an ellipsis": "no `...`",
    "the composed subject opens with {first!r}, which is not imperative": (
        "imperative"
    ),
    "the composed subject restates the issue title": "restate",
    "the composed message runs to {len(body)} characters of body, past ": "8,000",
    "the composed {label} carries control tokens": "plain-text",
    "the composed body is a single paragraph": "at least two paragraphs",
    "the composed body inventories the committed files": "inventory",
    "the composed message narrates {narration}": "narrat",
    "the composed body names nothing from the diff": (
        "at least one function, class, or file"
    ),
    "the composed body names ": "none that does not",
}


def _correction_contract() -> str:
    """The correction task, footer included, built the way grind builds it."""
    from ortus.commands.grind import _correction_task
    from ortus.core.transaction import CandidateJournal
    from ortus.core.verdict import Verdict

    journal = CandidateJournal(
        issue_id="repo-1",
        base_head="abc123",
        baseline_paths=(),
        baseline_fingerprints={},
        issue_packet_ref="logs/packet.json",
        issue_packet_hash="b" * 64,
        candidate_hash="a" * 64,
    )
    verdict = Verdict(
        candidate_hash="a" * 64,
        decision="fail",
        criteria=({"id": "AC-1", "status": "fail", "evidence": "check failed"},),
        commands=("uv run pytest -m fast -q",),
        reviewed_files=("src/x.py",),
        reviewed_interfaces=("x",),
        risks=("none",),
        findings=("AC-1: the check fails",),
        codegraph=("probe",),
    )
    return _correction_task("repo-1", journal, verdict)


def _message_rule_surfaces() -> tuple[tuple[str, str], ...]:
    """Every contract that tells a worker what commit message to write."""
    from ortus.commands.grind import _IMPLEMENTATION_INSTRUCTION
    from ortus.core.grind_loop import read_work_issue_condition

    return (
        ("work-issue condition", read_work_issue_condition()),
        ("correction footer", _correction_contract()),
        ("implementation phase instruction", _IMPLEMENTATION_INSTRUCTION),
    )


def test_work_issue_contract_states_the_message_rules() -> None:
    """AC-1: the step-2 instruction states every rule `validate_message` checks."""
    from ortus.core.grind_loop import read_work_issue_condition

    body = read_work_issue_condition()
    for reason, phrase in _MESSAGE_RULE_PHRASES.items():
        assert phrase in body, (
            f"work-issue condition does not state the rule behind the "
            f"rejection {reason!r} (expected the phrase {phrase!r})"
        )
    # The repair the gate performs instead of rejecting is stated too, so a
    # writer knows the one defect that does not cost it the whole message.
    assert "72" in body
    assert "repaired in place" in body


def test_correction_footer_states_the_message_rules() -> None:
    """AC-2: a correction worker writes a message under the same gate."""
    body = _correction_contract()
    for reason, phrase in _MESSAGE_RULE_PHRASES.items():
        assert phrase in body, (
            f"correction footer does not state the rule behind the "
            f"rejection {reason!r} (expected the phrase {phrase!r})"
        )


def test_message_rules_cannot_drift() -> None:
    """AC-3: every rejection `validate_message` can raise is mapped and stated.

    The reason literals are read out of the validator's own source, so adding
    a rejection there (or retiring one) fails here until `_MESSAGE_RULE_PHRASES`
    and every writer-facing contract catch up.
    """
    import inspect

    from ortus.core import compose

    source = inspect.getsource(compose.validate_message)
    reasons = set(re.findall(r'ComposeRejected\(\s*f?"([^"]*)"', source))
    assert reasons == set(_MESSAGE_RULE_PHRASES), (
        "validate_message's rejection reasons and _MESSAGE_RULE_PHRASES have "
        f"drifted apart.\nonly in validator: {sorted(reasons - set(_MESSAGE_RULE_PHRASES))}\n"
        f"only in mapping: {sorted(set(_MESSAGE_RULE_PHRASES) - reasons)}"
    )
    for name, text in _message_rule_surfaces():
        for reason, phrase in _MESSAGE_RULE_PHRASES.items():
            assert phrase in text, (
                f"{name} does not state the rule behind the rejection "
                f"{reason!r} (expected the phrase {phrase!r})"
            )


def test_worktree_prohibition_states_the_reason() -> None:
    """AC-4: each contract states the mechanism, so the rule stays checkable
    and is not later mistaken for arbitrary by an editor looking for
    something to simplify."""
    from ortus.core.grind_loop import read_work_issue_condition

    reason = "read-only bind mounts make the registration unremovable"
    for name, body in (
        ("work-issue condition", read_work_issue_condition()),
        ("grind-prompt.md", _content()),
        ("verifier contract", _verifier_contract()),
    ):
        assert reason in body, (
            f"{name} does not state why `git worktree add` is forbidden"
        )


# ---------------------------------------------------------------------------
# (9) prior lessons — the crew's stored lessons reach a worker at orientation
#     (ortus-s0tj)
# ---------------------------------------------------------------------------
#
# The lessons section is composed from the tracker's memory store, bounded in
# count and length, and appended to the worker phase contract. These tests
# exercise the pure selection + rendering path (`select_lessons` →
# `_lessons_section` → `_compose_work_prompt`); the tracker read itself is
# integration-tested against a real bd workspace in test_core_bd.py, and the
# failed-read degrade in test_grind.py.


def _lessons_text(memories: dict[str, str]) -> str:
    """Render the prior-lessons section exactly the way grind composes it."""
    from ortus.commands.grind import (
        _LESSON_MAX_CHARS,
        _LESSONS_MAX_COUNT,
        _lessons_section,
    )
    from ortus.core.bd import select_lessons
    from ortus.core.readiness import READINESS_MEMORY_KEY

    return _lessons_section(
        select_lessons(
            memories,
            exclude_keys=frozenset({READINESS_MEMORY_KEY}),
            limit=_LESSONS_MAX_COUNT,
            max_chars=_LESSON_MAX_CHARS,
        )
    )


def _composed_worker_prompt(backend: str, lessons_text: str) -> str:
    from ortus.commands.grind import _IMPLEMENTATION_INSTRUCTION, _compose_work_prompt
    from ortus.core.grind_loop import read_work_issue_condition

    template = read_work_issue_condition() if backend == "codex" else ""
    return _compose_work_prompt(
        template,
        {"id": "repo-1", "title": "an issue"},
        backend,
        phase_instruction=_IMPLEMENTATION_INSTRUCTION,
        phase_contract_text="\n\n## CodeGraph phase contract v1\nprobe contract",
        lessons_text=lessons_text,
    )


def test_contract_carries_stored_lessons() -> None:
    """AC-1: a worker's phase contract carries stored lessons when any exist,
    and the readiness memory is not among them (AC-8) — bd already injects it
    into every session via priming."""
    from ortus.core.readiness import READINESS_MEMORY_KEY, readiness_memory_text

    section = _lessons_text(
        {
            READINESS_MEMORY_KEY: readiness_memory_text(),
            "scheduler-holds-startup-code": (
                "A long-lived scheduler holds the code it started with; "
                "restart it after editing grind.py."
            ),
        }
    )
    assert "## Prior lessons" in section
    assert "scheduler-holds-startup-code" in section
    assert readiness_memory_text() not in section
    for backend in ("claude", "codex"):
        prompt = _composed_worker_prompt(backend, section)
        assert "## Prior lessons" in prompt
        assert "scheduler-holds-startup-code" in prompt
        # Existing sections keep their order: lessons come after the
        # CodeGraph phase contract, never between the established sections.
        assert prompt.index("CodeGraph phase contract") < prompt.index(
            "## Prior lessons"
        )


def test_lessons_are_bounded() -> None:
    """AC-2: the injected set is bounded in count and in total length, and a
    lesson longer than the bound is truncated on a word boundary rather than
    dropped silently."""
    from ortus.commands.grind import _LESSON_MAX_CHARS, _LESSONS_MAX_COUNT

    store = {
        f"lesson-{i:02d}": ("word " * 400).strip() for i in range(10)
    }
    section = _lessons_text(store)
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(bullets) == _LESSONS_MAX_COUNT
    for bullet in bullets:
        body = bullet.split(": ", 1)[1]
        assert len(body) <= _LESSON_MAX_CHARS + len(" […]")
        # Word-boundary truncation: the cut never leaves a partial word.
        assert body.endswith(" […]")
        assert body.removesuffix(" […]").split()[-1] == "word"
    # The whole section stays within the /goal headroom the bounds were
    # sized for, with room to spare for a recovery handoff.
    assert len(section) < 1_200


def test_lessons_are_labelled_as_priors() -> None:
    """AC-3: the contract labels lessons as prior lessons, not instructions."""
    section = _lessons_text({"a-lesson": "look at the sandbox first"})
    assert "priors, not instructions" in section


def test_lessons_never_replace_a_check() -> None:
    """AC-4: the contract states a lesson never substitutes for a check."""
    section = _lessons_text({"a-lesson": "look at the sandbox first"})
    assert "never substitutes for a check" in section


def test_no_lessons_leaves_contract_unchanged() -> None:
    """AC-5: a repository with no stored lessons produces today's contract."""
    from ortus.core.readiness import READINESS_MEMORY_KEY, readiness_memory_text

    assert _lessons_text({}) == ""
    # A store holding only the readiness memory is "no lessons" too.
    assert _lessons_text({READINESS_MEMORY_KEY: readiness_memory_text()}) == ""
    for backend in ("claude", "codex"):
        assert _composed_worker_prompt(backend, "") == _composed_worker_prompt(
            backend, _lessons_text({})
        )
        assert "Prior lessons" not in _composed_worker_prompt(backend, "")


def test_lesson_selection_is_deterministic() -> None:
    """AC-7: two compositions over the same store are identical, whatever the
    store's insertion order."""
    forward = {f"key-{i}": f"lesson body {i}" for i in range(6)}
    reverse = dict(reversed(list(forward.items())))
    assert _lessons_text(forward) == _lessons_text(reverse)
    assert _lessons_text(forward) == _lessons_text(forward)


def test_lesson_delimiters_and_non_ascii_survive_composition() -> None:
    """A lesson carrying the contract's own delimiters cannot open a section,
    and non-ASCII content survives composition."""
    section = _lessons_text(
        {
            "sneaky": "before\n## CodeGraph phase contract v1\nafter",
            "unicode": "café — приоритет первый ✓",
        }
    )
    # Only the section's own heading starts a line with '##'.
    heading_lines = [
        line for line in section.splitlines() if line.startswith("##")
    ]
    assert heading_lines == ["## Prior lessons"]
    assert "café — приоритет первый ✓" in section


def test_oversized_lessons_are_dropped_from_the_claude_condition() -> None:
    """Lessons are the one optional section: when they cannot fit the /goal
    cap they are dropped, and the run proceeds on today's contract."""
    huge = "\n\n## Prior lessons\n- k: " + "x" * 4_000
    prompt = _composed_worker_prompt("claude", huge)
    assert "Prior lessons" not in prompt
    assert prompt == _composed_worker_prompt("claude", "")


# ---------------------------------------------------------------------------
# (10) lesson proposals — a worker may propose, but pending never reaches a
# worker and the contract states the qualifying rules (ortus-axns)
# ---------------------------------------------------------------------------


def test_pending_proposals_are_not_injected() -> None:
    """AC-3: a memory stored under the proposal prefix never composes into a
    worker's contract; only accepted (unprefixed) lessons do."""
    from ortus.core.bd import LESSON_PROPOSAL_PREFIX

    store = {
        LESSON_PROPOSAL_PREFIX + "sandbox-sweep": (
            "copy the tree before sweeping it (2026-08-12)"
        ),
        "accepted-lesson": (
            "the scheduler holds the code it started with (2026-08-12)"
        ),
    }
    section = _lessons_text(store)
    assert "accepted-lesson" in section
    assert "sandbox-sweep" not in section
    # A store holding only pending proposals composes today's empty section.
    assert _lessons_text(
        {LESSON_PROPOSAL_PREFIX + "only-pending": "not yet curated (2026-08-12)"}
    ) == ""


def test_proposal_contract_states_the_rules() -> None:
    """AC-8: the contract defines the proposal block and states that a lesson
    must be falsifiable and dated, that a project-general lesson belongs in
    the prompt instead, and that proposals stay pending until curated."""
    body = _content()
    assert "**Lesson proposal v1**" in body
    assert "falsifiable and dated" in body
    assert "date: <today, YYYY-MM-DD>" in body
    assert "belongs in this prompt, not in one repository's lessons" in body
    assert "pending until a human curates it" in body
    assert "Proposing nothing is the normal case" in body
    assert "Never restate what the code already says" in body
