"""Dream's validation stage: citations, quotes, prevalence, rules, caps, Did-it-help."""

from __future__ import annotations

from pathlib import Path

from ortus.core.dream_decisions import (
    ACCEPTED,
    REJECTED,
    DecidedProposal,
    Decision,
    fingerprint,
)
from ortus.core.dream_orchestrator import (
    Followup,
    Helped,
    OrchestratorResult,
    Proposal,
    notes_section,
)
from ortus.core.dream_transcripts import BeadWindow, PreparedWindow, window_label
from ortus.core.dream_validate import (
    DROP_BAD_KEY,
    DROP_BELOW_MIN_BEADS,
    DROP_BODY_TOO_LONG,
    DROP_CAP,
    DROP_KEY_EXISTS,
    DROP_KEY_MISSING,
    DROP_PREVIOUSLY_REJECTED,
    DROP_QUOTE_NOT_FOUND,
    DROP_READINESS_KEY,
    DROP_UNKNOWN_LABEL,
    MAX_FOLLOWUPS,
    validate,
)
from ortus.core.readiness import READINESS_MEMORY_KEY

QUOTE = "ModuleNotFoundError: No module named 'typer'"


def _window(bead: str, run: str, body: str, start: int = 1) -> PreparedWindow:
    lines = body.count("\n") + 1
    return PreparedWindow(
        window=BeadWindow(
            log_path=Path(f"logs/{run}.log"),
            run=run,
            bead_id=bead,
            start_line=start,
            end_line=start + lines,
            backend="claude",
            lines=(),
        ),
        body=body,
        cuts=0,
    )


def _record(item: PreparedWindow, *quotes: str, turns: int = 2) -> dict:
    window = item.window
    return {
        "bead": window.bead_id,
        "run": window.run,
        "lines": f"{window.start_line}-{window.end_line}",
        "notes": [
            {
                "issue": "bare pytest",
                "evidence": [{"label": window_label(window), "quote": q} for q in quotes],
                "cost": {"turns": turns, "tokens": None},
                "memory_would_help": True,
                "kind_hint": "memory",
            }
        ],
    }


def _label(record: dict) -> str:
    return notes_section(record).label


def _proposal(**overrides) -> Proposal:
    fields = {
        "kind": "add",
        "key": "pytest-uv-run",
        "body": "Run tests with uv run pytest, never bare pytest.",
        "pattern": "bare pytest picks the wrong interpreter",
        "why": "two beads lost turns to it",
        "beads": ("ortus-a", "ortus-b"),
        "sources": (),
        "quotes": (QUOTE,),
        "stats": {"beads": 2, "windows": 2, "turns_wasted": 4},
    }
    fields.update(overrides)
    return Proposal(**fields)


def _result(proposals=(), followups=(), helped=(), labels=()) -> OrchestratorResult:
    return OrchestratorResult(
        proposals=tuple(proposals),
        followups=tuple(followups),
        helped=tuple(helped),
        invalid_lines=0,
        input_labels=frozenset(labels),
        dropped_notes=0,
        usage=None,
        cost_usd=None,
        needs_answered=0,
    )


def _two_beads():
    a = _window("ortus-a", "grind-20261001-100000", f"step\n{QUOTE}\nend")
    b = _window("ortus-b", "grind-20261002-100000", f"x\n{QUOTE}")
    records = [_record(a, QUOTE), _record(b, QUOTE)]
    return [a, b], records


def _run(proposals, *, windows, records, memories=None, decisions=(), min_beads=2,
         followups=(), helped=(), extra_labels=()):
    labels = {_label(record) for record in records} | set(extra_labels)
    sources = tuple(sorted(_label(record) for record in records))
    proposals = [
        p if p.sources else Proposal(**{**p.__dict__, "sources": sources}) for p in proposals
    ]
    return validate(
        _result(proposals, followups, helped, labels),
        windows=windows,
        records=records,
        memories=memories or {},
        decisions=decisions,
        min_beads=min_beads,
    )


# --- citations ---------------------------------------------------------------


def test_citation_kept_proposal_carries_code_stats_and_examples() -> None:
    windows, records = _two_beads()

    result = _run([_proposal()], windows=windows, records=records)

    assert result.drops == ()
    kept = result.proposals[0]
    assert kept.beads == ("ortus-a", "ortus-b")
    assert kept.stats == {"beads": 2, "windows": 2, "turns_wasted": 4}
    assert kept.examples == ((QUOTE, window_label(windows[0].window)),)
    assert kept.fingerprint == fingerprint("add", "pytest-uv-run", kept.proposal.pattern)


def test_citation_unknown_label_drops_the_proposal() -> None:
    windows, records = _two_beads()
    forged = _proposal(sources=("[reader-notes bead=ortus-z run=grind-x lines=1-2]",))

    result = _run([forged], windows=windows, records=records)

    assert result.proposals == ()
    assert result.drops == (("add pytest-uv-run", DROP_UNKNOWN_LABEL),)


def test_citation_forged_quote_drops_the_proposal() -> None:
    windows, records = _two_beads()
    forged = _proposal(quotes=("a sentence no transcript contains",))

    result = _run([forged], windows=windows, records=records)

    assert result.drops == (("add pytest-uv-run", DROP_QUOTE_NOT_FOUND),)


def test_citation_quote_must_come_from_a_named_bead() -> None:
    windows, records = _two_beads()
    other = _window("ortus-c", "grind-20261003-100000", "only here: zebra")
    elsewhere = _proposal(quotes=("only here: zebra",))

    result = _run([elsewhere], windows=[*windows, other], records=records)

    assert result.drops == (("add pytest-uv-run", DROP_QUOTE_NOT_FOUND),)


def test_citation_quote_across_a_cut_marker_fails() -> None:
    a = _window("ortus-a", "grind-20261001-100000", "head of a long read [cut 12 KB]")
    b = _window("ortus-b", "grind-20261002-100000", "head of a long read [cut 12 KB]")
    records = [_record(a, "head of a long read"), _record(b, "head of a long read")]
    spanning = _proposal(quotes=("head of a long read and the tail it lost",))

    result = _run([spanning], windows=[a, b], records=records)

    assert result.drops == (("add pytest-uv-run", DROP_QUOTE_NOT_FOUND),)


def test_citation_followup_with_unknown_label_drops() -> None:
    windows, records = _two_beads()
    good = Followup(title="Pin uv", why="env", beads=("ortus-a",), sources=(_label(records[0]),))
    bad = Followup(title="Forged", why="?", beads=("ortus-a",), sources=("[memory nope]",))

    result = _run([], windows=windows, records=records, followups=[good, bad])

    assert result.followups == (good,)
    assert result.drops == (("followup Forged", DROP_UNKNOWN_LABEL),)


# --- prevalence --------------------------------------------------------------


def test_prevalence_single_bead_pattern_drops() -> None:
    a = _window("ortus-a", "grind-20261001-100000", QUOTE)
    a2 = _window("ortus-a", "grind-20261002-100000", QUOTE)
    records = [_record(a, QUOTE), _record(a2, QUOTE)]
    single = _proposal(beads=("ortus-a", "ortus-a"))

    result = _run([single], windows=[a, a2], records=records)

    assert result.drops == (("add pytest-uv-run", DROP_BELOW_MIN_BEADS),)


def test_prevalence_claimed_bead_without_evidence_is_not_counted() -> None:
    windows, records = _two_beads()
    empty = _window("ortus-c", "grind-20261003-100000", "nothing relevant")
    claims_three = _proposal(
        beads=("ortus-a", "ortus-b", "ortus-c"),
        stats={"beads": 3, "windows": 3, "turns_wasted": 9},
    )

    result = _run([claims_three], windows=[*windows, empty], records=records, min_beads=3)

    assert result.drops == (("add pytest-uv-run", DROP_BELOW_MIN_BEADS),)


def test_prevalence_counts_verified_reader_evidence_and_records_diffs() -> None:
    a = _window("ortus-a", "grind-20261001-100000", QUOTE)
    b = _window("ortus-b", "grind-20261002-100000", "uv: command not found")
    forged = _window("ortus-c", "grind-20261003-100000", "nothing here")
    records = [
        _record(a, QUOTE, turns=3),
        _record(b, "uv: command not found", turns=5),
        _record(forged, "a quote the window never had", turns=40),
    ]
    proposal = _proposal(
        beads=("ortus-a",),
        stats={"beads": 3, "windows": 3, "turns_wasted": 48},
    )

    result = _run([proposal], windows=[a, b, forged], records=records)

    kept = result.proposals[0]
    assert kept.beads == ("ortus-a", "ortus-b")
    assert kept.stats == {"beads": 2, "windows": 2, "turns_wasted": 8}
    assert result.stat_diffs == (
        "add pytest-uv-run: beads model=3 code=2",
        "add pytest-uv-run: windows model=3 code=2",
        "add pytest-uv-run: turns_wasted model=48 code=8",
    )


# --- key rules, suppression and caps ----------------------------------------


def test_rules_key_body_existence_and_readiness() -> None:
    windows, records = _two_beads()
    memories = {"existing-key": "old body", READINESS_MEMORY_KEY: "contract"}
    proposals = [
        _proposal(key="Bad_Key"),
        _proposal(key="existing-key"),
        _proposal(kind="update", key="no-such-key"),
        _proposal(kind="prune", key=READINESS_MEMORY_KEY, body=""),
        _proposal(key="too-long", body="x" * 221),
        _proposal(kind="prune", key="existing-key", body=""),
        _proposal(kind="update", key="existing-key", body="y" * 220),
    ]

    result = _run(proposals, windows=windows, records=records, memories=memories)

    assert result.drops == (
        ("add Bad_Key", DROP_BAD_KEY),
        ("add existing-key", DROP_KEY_EXISTS),
        ("update no-such-key", DROP_KEY_MISSING),
        (f"prune {READINESS_MEMORY_KEY}", DROP_READINESS_KEY),
        ("add too-long", DROP_BODY_TOO_LONG),
    )
    assert [(p.proposal.kind, p.proposal.key) for p in result.proposals] == [
        ("prune", "existing-key"),
        ("update", "existing-key"),
    ]


def test_rules_rejected_key_or_fingerprint_is_suppressed() -> None:
    windows, records = _two_beads()
    by_print = _proposal(key="fresh-key")
    decision = Decision(
        bead_id="ortus-dec1",
        status="closed",
        closed_at="2026-10-01T00:00:00Z",
        proposals=(
            DecidedProposal("P1", "add", "rejected-key", "0" * 16, (), REJECTED),
            DecidedProposal(
                "P2", "add", "other",
                fingerprint("add", "fresh-key", by_print.pattern), (), REJECTED,
            ),
        ),
        followups=(),
        response="rejected P1 P2",
    )

    result = _run(
        [_proposal(key="rejected-key"), by_print, _proposal(key="kept-key")],
        windows=windows,
        records=records,
        decisions=[decision],
    )

    assert result.drops == (
        ("add rejected-key", DROP_PREVIOUSLY_REJECTED),
        ("add fresh-key", DROP_PREVIOUSLY_REJECTED),
    )
    assert [p.proposal.key for p in result.proposals] == ["kept-key"]


def test_rules_caps_apply_in_order_after_every_check() -> None:
    windows, records = _two_beads()
    memories = {f"mem-{n}": "body" for n in range(6)}
    proposals = [_proposal(key="Invalid")]
    proposals += [_proposal(key=f"add-{n}") for n in range(6)]
    proposals += [_proposal(kind="prune", key=f"mem-{n}", body="") for n in range(4)]
    followups = [
        Followup(title=f"F{n}", why="w", beads=("ortus-a",), sources=(_label(records[0]),))
        for n in range(MAX_FOLLOWUPS + 1)
    ]

    result = _run(
        proposals, windows=windows, records=records, memories=memories, followups=followups
    )

    assert [p.proposal.key for p in result.proposals] == [
        "add-0", "add-1", "add-2", "add-3", "add-4", "mem-0", "mem-1", "mem-2",
    ]
    assert result.drops == (
        ("add Invalid", DROP_BAD_KEY),
        ("add add-5", DROP_CAP),
        ("prune mem-3", DROP_CAP),
        (f"followup F{MAX_FOLLOWUPS}", DROP_CAP),
    )
    assert len(result.followups) == MAX_FOLLOWUPS


def test_rules_two_runs_give_identical_results() -> None:
    windows, records = _two_beads()
    proposals = [_proposal(), _proposal(key="bad key")]

    first = _run(proposals, windows=windows, records=records)
    second = _run(proposals, windows=list(reversed(windows)), records=records)

    assert first == second


# --- Did-it-help -------------------------------------------------------------


def _accepted(closed_at: str) -> Decision:
    return Decision(
        bead_id="ortus-dec1",
        status="closed",
        closed_at=closed_at,
        proposals=(
            DecidedProposal("P1", "add", "pytest-uv-run", "f" * 16, (QUOTE,), ACCEPTED),
        ),
        followups=(),
        response="accepted P1",
    )


def test_helped_counts_hits_before_and_after_the_accept_date() -> None:
    before = [
        _window("ortus-a", "grind-20260901-100000", QUOTE),
        _window("ortus-b", "grind-20260901-100000", f"again {QUOTE}", start=50),
        _window("ortus-c", "grind-20260901-100000", "clean", start=90),
    ]
    after = [
        _window("ortus-d", "grind-20261101-100000", "clean"),
        _window("ortus-e", "grind-20261101-100000", QUOTE, start=50),
        _window("ortus-f", "grind-20261101-100000", "clean", start=90),
        _window("ortus-g", "grind-20261101-100000", "clean", start=130),
    ]
    model = Helped(
        key="pytest-uv-run",
        before={"beads": 2, "windows": 2},
        after={"beads": 0, "windows": 0},
    )

    result = _run(
        [],
        windows=[*before, *after],
        records=[],
        decisions=[_accepted("2026-10-15T00:00:00Z")],
        helped=[model],
    )

    (row,) = result.helped_rows
    assert row.key == "pytest-uv-run"
    assert (row.before_windows, row.before_hits, row.before_beads) == (3, 2, 2)
    assert (row.after_windows, row.after_hits, row.after_beads) == (4, 1, 1)
    assert round(row.before_rate, 2) == 66.67
    assert row.after_rate == 25.0
    assert result.stat_diffs == (
        "helped pytest-uv-run after: beads model=0 code=1",
        "helped pytest-uv-run after: windows model=0 code=1",
    )


def test_helped_skips_unplaceable_windows_and_decisions() -> None:
    odd = _window("ortus-a", "manual-run", QUOTE)
    dated = _window("ortus-b", "grind-20260901-100000", QUOTE)

    result = _run(
        [],
        windows=[odd, dated],
        records=[],
        decisions=[_accepted("2026-10-15T00:00:00Z"), _accepted("")],
    )

    (row,) = result.helped_rows
    assert (row.before_windows, row.before_hits, row.after_windows) == (1, 1, 0)
    assert row.after_rate is None
