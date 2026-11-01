"""Dream's validation stage: code checks the orchestrator without redoing it.

The orchestrator clusters notes and drafts proposals; nothing it claims is
taken on trust. Every source it cites must be a label code wrote into its
input. Every quote must be a verbatim substring of a cached window belonging
to a bead the proposal names, and a reader note's evidence counts only when
its quote is verbatim in that note's own window. Prevalence is then recounted
from the verified quotes alone, and the code counts replace the model's
stats; a difference between the two is recorded rather than resolved.

Key, body, existence, readiness-key and suppression rules come next, and the
caps are applied last, in orchestrator order, so an item dropped for a rule
never takes a cap slot from one that passed.

Did-it-help is recounted the same way: for each accepted memory, the windows
whose cached text still contains one of its stored quotes, split at the
decision's close. The model's HELPED lines are only compared, never shown.

Everything here is a pure function of in-memory data, so two runs over the
same caches give the same result in the same order.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ortus.commands.grind import _LESSON_MAX_CHARS
from ortus.core.dream_decisions import Decision, accepted, fingerprint, suppressed
from ortus.core.dream_orchestrator import (
    Followup,
    OrchestratorResult,
    Proposal,
    notes_section,
)
from ortus.core.dream_transcripts import PreparedWindow, window_label
from ortus.core.readiness import READINESS_MEMORY_KEY

#: Kept items after every check, in orchestrator order.
MAX_PER_KIND = 5
MAX_PROPOSALS = 8
MAX_FOLLOWUPS = 3

#: Example quotes a kept proposal carries into the report.
EXAMPLE_QUOTES = 2

#: A memory key bd and the report can both carry safely.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DROP_UNKNOWN_LABEL = "unknown-label"
DROP_QUOTE_NOT_FOUND = "quote-not-found"
DROP_BELOW_MIN_BEADS = "below-min-beads"
DROP_BAD_KEY = "bad-key"
DROP_READINESS_KEY = "readiness-key"
DROP_BODY_TOO_LONG = "body-too-long"
DROP_KEY_EXISTS = "key-exists"
DROP_KEY_MISSING = "key-missing"
DROP_PREVIOUSLY_REJECTED = "previously-rejected"
DROP_CAP = "cap"

_STEM_RE = re.compile(r"^grind-(\d{8}-\d{6})")


@dataclass(frozen=True)
class KeptProposal:
    """A proposal that passed every check, with code's counts and examples."""

    proposal: Proposal
    fingerprint: str
    beads: tuple[str, ...]
    stats: Mapping[str, int]
    examples: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HelpedRow:
    """Hits for one accepted memory's quotes before and after its decision."""

    key: str
    closed_at: str
    before_windows: int
    before_hits: int
    before_beads: int
    after_windows: int
    after_hits: int
    after_beads: int

    @property
    def before_rate(self) -> float | None:
        return _rate(self.before_hits, self.before_windows)

    @property
    def after_rate(self) -> float | None:
        return _rate(self.after_hits, self.after_windows)


@dataclass(frozen=True)
class ValidationResult:
    proposals: tuple[KeptProposal, ...]
    followups: tuple[Followup, ...]
    helped_rows: tuple[HelpedRow, ...]
    drops: tuple[tuple[str, str], ...]
    stat_diffs: tuple[str, ...]


def _rate(hits: int, windows: int) -> float | None:
    return None if windows == 0 else 100.0 * hits / windows


def _unique(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def window_time(run: str) -> _dt.datetime | None:
    """The local time a `grind-YYYYMMDD-HHMMSS` log stem names, or None."""

    match = _STEM_RE.match(run)
    if match is None:
        return None
    try:
        return _dt.datetime.strptime(match.group(1), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _closed_time(closed_at: str) -> _dt.datetime | None:
    """`closed_at` as naive local time, comparable with a log stem."""

    try:
        parsed = _dt.datetime.fromisoformat(closed_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


class _Windows:
    """The cached windows, indexed by bead and by where a notes record came from."""

    def __init__(self, windows: Iterable[PreparedWindow]) -> None:
        by_label: dict[str, PreparedWindow] = {}
        for item in windows:
            if item.skipped is None:
                by_label.setdefault(window_label(item.window), item)
        self.ordered = tuple(
            sorted(
                by_label.values(),
                key=lambda item: (item.window.run, item.window.start_line, item.window.bead_id),
            )
        )
        self.by_bead: dict[str, list[PreparedWindow]] = {}
        self.by_origin: dict[tuple[str, str, str], PreparedWindow] = {}
        for item in self.ordered:
            window = item.window
            self.by_bead.setdefault(window.bead_id, []).append(item)
            lines = f"{window.start_line}-{window.end_line}"
            self.by_origin[(window.run, window.bead_id, lines)] = item

    def own(self, record: Mapping[str, Any]) -> PreparedWindow | None:
        origin = (
            str(record.get("run") or ""),
            str(record.get("bead") or ""),
            str(record.get("lines") or ""),
        )
        return self.by_origin.get(origin)


def _verified_notes(
    record: Mapping[str, Any], own: PreparedWindow | None
) -> list[tuple[dict[str, Any], tuple[str, ...]]]:
    """Each note whose evidence has a quote verbatim in its own window."""

    if own is None:
        return []
    verified: list[tuple[dict[str, Any], tuple[str, ...]]] = []
    for note in record.get("notes") or ():
        if not isinstance(note, dict):
            continue
        quotes = tuple(
            str(item.get("quote"))
            for item in note.get("evidence") or ()
            if isinstance(item, dict)
            and isinstance(item.get("quote"), str)
            and item["quote"]
            and item["quote"] in own.body
        )
        if quotes:
            verified.append((note, quotes))
    return verified


def _turns(note: Mapping[str, Any]) -> int:
    cost = note.get("cost")
    turns = cost.get("turns") if isinstance(cost, dict) else None
    return turns if isinstance(turns, int) and not isinstance(turns, bool) else 0


def _key_rule(proposal: Proposal, memories: Mapping[str, str]) -> str | None:
    if not KEY_RE.match(proposal.key):
        return DROP_BAD_KEY
    if proposal.key == READINESS_MEMORY_KEY:
        return DROP_READINESS_KEY
    if proposal.kind != "prune" and len(proposal.body) > _LESSON_MAX_CHARS:
        return DROP_BODY_TOO_LONG
    if proposal.kind == "add" and proposal.key in memories:
        return DROP_KEY_EXISTS
    if proposal.kind in ("update", "prune") and proposal.key not in memories:
        return DROP_KEY_MISSING
    return None


def _stat_diffs(name: str, model: Mapping[str, Any], code: Mapping[str, int]) -> list[str]:
    return [
        f"{name}: {stat} model={model.get(stat)} code={value}"
        for stat, value in code.items()
        if model.get(stat) != value
    ]


def _check_proposal(
    proposal: Proposal,
    *,
    labels: frozenset[str],
    windows: _Windows,
    records: Mapping[str, Mapping[str, Any]],
    min_beads: int,
) -> tuple[str | None, KeptProposal | None]:
    """The drop reason, or the proposal with its code-counted stats."""

    if any(source not in labels for source in proposal.sources):
        return DROP_UNKNOWN_LABEL, None

    beads = _unique(proposal.beads)
    hit_windows: dict[str, PreparedWindow] = {}
    examples: list[tuple[str, str]] = []
    verified_quotes = 0
    for quote in _unique(proposal.quotes):
        found = [
            item
            for bead in beads
            for item in windows.by_bead.get(bead, ())
            if quote and quote in item.body
        ]
        if not found:
            continue
        verified_quotes += 1
        for item in found:
            hit_windows.setdefault(window_label(item.window), item)
        if len(examples) < EXAMPLE_QUOTES:
            examples.append((quote, window_label(found[0].window)))
    if verified_quotes == 0:
        return DROP_QUOTE_NOT_FOUND, None

    turns = 0
    for source in _unique(proposal.sources):
        record = records.get(source)
        if record is None:
            continue
        own = windows.own(record)
        verified = _verified_notes(record, own)
        if not verified or own is None:
            continue
        hit_windows.setdefault(window_label(own.window), own)
        turns += sum(_turns(note) for note, _ in verified)

    counted = _unique(item.window.bead_id for item in hit_windows.values())
    stats = {"beads": len(counted), "windows": len(hit_windows), "turns_wasted": turns}
    if len(counted) < min_beads:
        return DROP_BELOW_MIN_BEADS, None
    return None, KeptProposal(
        proposal=proposal,
        fingerprint=fingerprint(proposal.kind, proposal.key, proposal.pattern),
        beads=tuple(sorted(counted)),
        stats=stats,
        examples=tuple(examples),
    )


def helped_rows(
    accepted_rows: Iterable[tuple[str, tuple[str, ...], str]],
    windows: Iterable[PreparedWindow],
) -> tuple[HelpedRow, ...]:
    """Recount each accepted memory's quotes in windows before and after its close.

    A window whose log stem names no time, or a decision with no parseable
    `closed_at`, cannot be placed on either side and is left out.
    """

    index = _Windows(windows)
    timed = [
        (when, item)
        for item in index.ordered
        if (when := window_time(item.window.run)) is not None
    ]
    rows: list[HelpedRow] = []
    for key, quotes, closed_at in accepted_rows:
        cut = _closed_time(closed_at)
        if cut is None:
            continue
        sides: dict[bool, list[PreparedWindow]] = {False: [], True: []}
        for when, item in timed:
            sides[when >= cut].append(item)
        counts: dict[bool, tuple[int, int, int]] = {}
        for after, items in sides.items():
            hits = [item for item in items if any(q and q in item.body for q in quotes)]
            beads = {item.window.bead_id for item in hits}
            counts[after] = (len(items), len(hits), len(beads))
        rows.append(
            HelpedRow(
                key=key,
                closed_at=closed_at,
                before_windows=counts[False][0],
                before_hits=counts[False][1],
                before_beads=counts[False][2],
                after_windows=counts[True][0],
                after_hits=counts[True][1],
                after_beads=counts[True][2],
            )
        )
    return tuple(rows)


def validate(
    result: OrchestratorResult,
    *,
    windows: Iterable[PreparedWindow],
    records: Iterable[Mapping[str, Any]],
    memories: Mapping[str, str],
    decisions: Iterable[Decision],
    min_beads: int,
) -> ValidationResult:
    """Check every proposal and follow-up, recount stats and Did-it-help.

    `windows` are the prepared (cached) windows, `records` the cached notes
    records the orchestrator input was composed from.
    """

    windows = tuple(windows)
    decisions = tuple(decisions)
    index = _Windows(windows)
    by_label: dict[str, Mapping[str, Any]] = {}
    for record in records:
        by_label.setdefault(notes_section(record).label, record)
    keys_out, prints_out = suppressed(decisions)

    drops: list[tuple[str, str]] = []
    diffs: list[str] = []
    passed: list[KeptProposal] = []
    for proposal in result.proposals:
        name = f"{proposal.kind} {proposal.key}"
        reason, kept = _check_proposal(
            proposal,
            labels=result.input_labels,
            windows=index,
            records=by_label,
            min_beads=min_beads,
        )
        if reason is None:
            reason = _key_rule(proposal, memories)
        if reason is None and (
            proposal.key in keys_out or kept.fingerprint in prints_out
        ):
            reason = DROP_PREVIOUSLY_REJECTED
        if reason is not None:
            drops.append((name, reason))
            continue
        diffs.extend(_stat_diffs(name, proposal.stats, kept.stats))
        passed.append(kept)

    kept_proposals: list[KeptProposal] = []
    per_kind: dict[str, int] = {}
    for item in passed:
        kind = item.proposal.kind
        if len(kept_proposals) >= MAX_PROPOSALS or per_kind.get(kind, 0) >= MAX_PER_KIND:
            drops.append((f"{kind} {item.proposal.key}", DROP_CAP))
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        kept_proposals.append(item)

    kept_followups: list[Followup] = []
    for followup in result.followups:
        name = f"followup {followup.title}"
        if any(source not in result.input_labels for source in followup.sources):
            drops.append((name, DROP_UNKNOWN_LABEL))
        elif len(kept_followups) >= MAX_FOLLOWUPS:
            drops.append((name, DROP_CAP))
        else:
            kept_followups.append(followup)

    rows = helped_rows(accepted(decisions), windows)
    by_key = {row.key: row for row in rows}
    for model in result.helped:
        row = by_key.get(model.key)
        if row is None:
            diffs.append(f"helped {model.key}: not an accepted memory")
            continue
        diffs.extend(
            _stat_diffs(
                f"helped {model.key} before",
                model.before,
                {"beads": row.before_beads, "windows": row.before_hits},
            )
        )
        diffs.extend(
            _stat_diffs(
                f"helped {model.key} after",
                model.after,
                {"beads": row.after_beads, "windows": row.after_hits},
            )
        )

    return ValidationResult(
        proposals=tuple(kept_proposals),
        followups=tuple(kept_followups),
        helped_rows=rows,
        drops=tuple(drops),
        stat_diffs=tuple(diffs),
    )
