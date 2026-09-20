"""Offline comparison of soft threshold candidates against labeled replay.

Diamond policy, locked 2026-09-20: System One emits typed probability vectors
and those vectors travel into System Two as context. A candidate here may only
describe a soft band — something to log, to flag for operator review, or to
rewrite a prompt around. No candidate withholds a claim, changes a recorded
action, or writes configuration; the hard pre-turn escalate is scrapped and
this module cannot re-arm it.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):  # pragma: no cover - interpreter branch
    import tomllib
else:  # pragma: no cover - interpreter branch
    import tomli as tomllib

from ortus.core.judge import GateAction, JudgeConfig
from ortus.core.judge_replay import ReplayData, ReplayError, eligible_decisions, recorded_action

DIAMOND_POLICY = (
    "probability vectors into System Two; soft candidates only, no hard escalate"
)
MAX_CANDIDATES = 20
MAX_DOCUMENT_BYTES = 64 * 1024
# What a candidate is allowed to ask for once it fires. Every one of these is a
# report or a prompt change; none of them stops a worker.
SOFT_ACTIONS = ("log", "flag", "rewrite")
# A band bound reads one recorded probability and nothing else. `_min` fires
# below its value, `_max` fires above it, and the ceiling matches the logged
# answer's own range.
BOUNDS = {
    "route_confidence_min": ("route_confidence", 1.0, False),
    "noul_confidence_min": ("noul_confidence", 1.0, False),
    "risk_confidence_min": ("risk_confidence", 1.0, False),
    "needs_human_max": ("needs_human", 1.0, True),
    "action_risk_max": ("action_risk", 2.0, True),
}
# The pre-diamond setting names keep parsing, coerced onto the band that reads
# the same answer. An operator's older candidate file stays usable without
# becoming a claim gate again.
LEGACY_BOUNDS = {
    "route_confidence": "route_confidence_min",
    "noul_confidence": "noul_confidence_min",
    "risk_confidence": "risk_confidence_min",
    "human_threshold": "needs_human_max",
    "risk_threshold": "action_risk_max",
}
# Keys that would name an enforcement outcome rather than a band. These are the
# shape a re-armed hard escalate would arrive in, so they are refused by name
# instead of being silently downgraded to a soft action.
HARD_KEYS = (
    "block", "blocks_claims", "deny", "effective_action", "enforce", "escalate",
    "gate", "intended_action", "low_confidence", "mode", "stop",
)
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class CalibrationError(ReplayError):
    """A diagnostic naming a table position, never a candidate value or path."""


@dataclass(frozen=True)
class Candidate:
    """One named soft band: what it reads, and what it asks for when it fires."""

    name: str
    action: str
    bounds: tuple[tuple[str, float], ...]


def _fail(message: str) -> None:
    raise CalibrationError(message)


def _bound_value(value: object, ceiling: float, position: int) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(f"candidate {position}: bound must be a finite number")
    if not 0 <= value <= ceiling:
        _fail(f"candidate {position}: bound is outside the answer's range")
    return float(value)


def _candidate(table: object, position: int, seen: set[str]) -> Candidate:
    if type(table) is not dict:
        _fail(f"candidate {position}: must be a table")
    hard = sorted(set(table) & set(HARD_KEYS))
    if hard:
        _fail(
            f"candidate {position}: {hard[0]} names an enforcement outcome; "
            "diamond policy allows soft bands only (log, flag, rewrite)"
        )
    fields = {LEGACY_BOUNDS.get(key, key): value for key, value in table.items()}
    if len(fields) != len(table):
        _fail(f"candidate {position}: bound is set twice under two names")
    unknown = sorted(set(fields) - set(BOUNDS) - {"name", "action", "note"})
    if unknown:
        _fail(f"candidate {position}: unknown key {unknown[0]}")
    name = fields.get("name")
    if type(name) is not str or NAME_PATTERN.fullmatch(name) is None:
        _fail(f"candidate {position}: name must be lowercase, short and unreserved")
    if name in seen:
        _fail(f"candidate {position}: duplicate name")
    action = fields.get("action")
    if action not in SOFT_ACTIONS:
        _fail(
            f"candidate {position}: action must be one of "
            f"{', '.join(SOFT_ACTIONS)}; a hard escalate cannot be calibrated"
        )
    if "note" in fields and type(fields["note"]) is not str:
        _fail(f"candidate {position}: note must be text")
    bounds = tuple(
        (key, _bound_value(fields[key], BOUNDS[key][1], position))
        for key in sorted(BOUNDS)
        if key in fields
    )
    if not bounds:
        _fail(f"candidate {position}: needs at least one bound to compare")
    return Candidate(name=name, action=action, bounds=bounds)


def read_candidates(path: Path) -> tuple[Candidate, ...]:
    """Parse a bounded candidate document in the order the operator wrote it."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError:
        raise CalibrationError("cannot read threshold candidates") from None
    if len(data) > MAX_DOCUMENT_BYTES:
        raise CalibrationError("threshold candidates document is too large")
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:  # TOMLDecodeError subclasses ValueError
        raise CalibrationError("threshold candidates are not valid TOML") from exc
    if set(document) - {"candidate"}:
        raise CalibrationError("only [[candidate]] tables belong in this document")
    tables = document.get("candidate")
    if type(tables) is not list or not tables:
        raise CalibrationError("document declares no [[candidate]] table")
    if len(tables) > MAX_CANDIDATES:
        raise CalibrationError(f"at most {MAX_CANDIDATES} candidates may be compared")
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for position, table in enumerate(tables, start=1):
        candidate = _candidate(table, position, seen)
        seen.add(candidate.name)
        candidates.append(candidate)
    return tuple(candidates)


def fires(candidate: Candidate, answers: dict) -> bool:
    """Whether the recorded vector falls outside this candidate's band."""
    for key, value in candidate.bounds:
        answer, _, above = BOUNDS[key]
        recorded = answers[answer]
        if (recorded > value) if above else (recorded < value):
            return True
    return False


def _fraction(count: int, denominator: int) -> dict:
    return {"count": count, "denominator": denominator,
            "rate": count / denominator if denominator else None}


def _soft_settings(config: JudgeConfig) -> dict:
    """The production values a candidate is read against, all of them inert."""
    return {
        "route_confidence": config.route_confidence,
        "noul_confidence": config.noul_confidence,
        "risk_confidence": config.risk_confidence,
        "human_threshold": config.human_threshold,
        "risk_threshold": config.risk_threshold,
        "low_confidence": config.low_confidence.value,
        "enforced_pre_turn": False,
    }


def compare_candidates(
    data: ReplayData,
    labels: dict,
    candidates: tuple[Candidate, ...],
    *,
    config: JudgeConfig = JudgeConfig(),
) -> dict:
    """Score each candidate against recorded answers without applying any of it.

    Only labeled decisions that carry answers are scored; a service failure has
    no vector to band. The recorded action distribution is reported alongside
    the candidates as the evidence that nothing here reroutes or withholds: it
    is read from the log and is identical whatever candidates are supplied.
    """
    eligible, _ = eligible_decisions(data)
    labeled = [d for d in eligible if d["decision_id"] in labels]
    if not labeled:
        raise CalibrationError("no labeled decision to calibrate against")
    scored = [d for d in labeled if d["answers"] is not None]
    positives = [d for d in scored if labels[d["decision_id"]]["needs_human"]]
    negatives = [d for d in scored if not labels[d["decision_id"]]["needs_human"]]
    reports = []
    for candidate in candidates:
        firing = {d["decision_id"] for d in scored if fires(candidate, d["answers"])}
        caught = sum(d["decision_id"] in firing for d in positives)
        reports.append({
            "name": candidate.name,
            "action": candidate.action,
            "blocks_claims": False,
            "bounds": dict(candidate.bounds),
            "fired": _fraction(len(firing), len(scored)),
            "caught_needs_human": _fraction(caught, len(positives)),
            "missed_needs_human": _fraction(len(positives) - caught, len(positives)),
            "over_caution": _fraction(
                sum(d["decision_id"] in firing for d in negatives), len(negatives)
            ),
            "action_changes": 0,
        })
    return {
        "schema_version": 1,
        "policy": DIAMOND_POLICY,
        "applies_settings": False,
        "labeled": len(labeled),
        "scored": len(scored),
        "unanswered": len(labeled) - len(scored),
        "needs_human_labels": len(positives),
        "proceed_labels": len(negatives),
        "recorded_actions": {
            action.value: sum(recorded_action(d) == action.value for d in scored)
            for action in GateAction
        },
        "production_soft_settings": _soft_settings(config),
        "candidates": reports,
    }
