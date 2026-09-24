"""Offline readers and metrics for the logger's version-1 public fields.

Accuracy scores the intended gate action in shadow mode and the effective gate
otherwise. Labels describe the expected gate action and whether human handling
was needed. Unattributed shadow outcomes never enter accuracy denominators.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from uuid import UUID

from ortus.core.judge import WORKER_ROUTES, GateAction, GateReason, JudgeConfig, JudgePhase, JudgeRoute
from ortus.core.judge_log import MAX_EVENT_BYTES, OutcomeStatus, _clean_metadata
from ortus.core.judge_typesafe import JudgeFailure
from ortus.core.worker_failure import WorkerFailure

COMMON = {"schema_version", "event", "timestamp", "run_id", "decision_id"}
USAGE = {"input_tokens", "output_tokens", "measured_cost_usd"}
DECISION = COMMON | USAGE | {
    "seat", "issue_id", "phase", "answers", "intended_action", "effective_action",
    "backend", "reason", "model", "criteria_version", "criteria_hash", "latency_ms", "failure",
}
OUTCOME = COMMON | USAGE | {"observed_status", "elapsed_worker_ms"}
POST = COMMON | {
    "phase", "mode", "issue_id", "seat", "model", "exit_status", "watchdog",
    "observed_status", "branch_advanced", "outcome", "confidence", "failure",
    "intended_outcome", "effective_action", "reason", "disagreement",
}
SHADOW_OUTCOME = {
    "mode", "observed_issue_id", "actual_claimed_id", "attribution_mismatch", "accuracy_eligible",
}
OUTCOMES = {"done", "plan_gap", "flake", "auth", "needs_human", "continue"}
#: The worker-failure class a post-turn record may also carry. Optional, and
#: read beside `reason` rather than folded into `OUTCOMES`: a log written
#: before the taxonomy existed is still a valid version-1 record, so the field
#: widens the accepted shape instead of bumping the schema out from under the
#: readers that already consume these files.
POST_SIDE = {"worker_failure"}
WORKER_FAILURES = {failure.value for failure in WorkerFailure}
MAX_LABEL_BYTES = 16 * 1024 * 1024


class ReplayError(ValueError):
    """A diagnostic containing no input values or paths."""


def _require(ok: bool) -> None:
    if not ok:
        raise ReplayError("invalid record")


def _number(value: object, maximum: float | None = None) -> bool:
    try:
        return (type(value) in (int, float) and math.isfinite(value) and value >= 0
                and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _choice(value: object, choices: Iterable[str], *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (type(value) is str and any(value == c for c in choices))


def _uuid(value: object) -> None:
    _require(type(value) is str and str(UUID(value)) == value)


def _metadata(value: object) -> None:
    _require(value is None or (type(value) is str and len(value) <= 160
                              and _clean_metadata(value, JudgeConfig()) == value))


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _decode(data: bytes) -> object:
    return json.loads(data, object_pairs_hook=_pairs,
                      parse_constant=lambda _: _require(False))


def validate_event(value: object) -> dict:
    """Reject unknown fields and invalid values rather than exporting raw prose."""
    _require(type(value) is dict)
    e = value
    _require(type(e.get("schema_version")) is int and e["schema_version"] == 1)
    kind = e.get("event")
    shadow = e.get("mode") == "shadow"
    fields = (DECISION | ({"mode", "observed_issue_id"} if shadow else set())
              if kind == "decision" else
              OUTCOME | (SHADOW_OUTCOME if shadow else set()) if kind == "outcome" else
              POST if kind == "post_turn" else set())
    optional = POST_SIDE if kind == "post_turn" else set()
    _require(bool(fields) and fields <= set(e) <= fields | optional)
    _uuid(e["run_id"])
    _uuid(e["decision_id"])
    _require(type(e["timestamp"]) is str and len(e["timestamp"]) <= 40)
    _require(datetime.fromisoformat(e["timestamp"]).utcoffset() is not None)
    for name in ("seat", "issue_id", "criteria_version", "observed_issue_id", "actual_claimed_id"):
        if name in e:
            _metadata(e[name])
    if kind in ("decision", "post_turn"):
        _require(e["model"] is None or (type(e["model"]) is str
                 and re.fullmatch(r"jev-\d+\.\d+\.\d+", e["model"]) is not None))
        _require(_choice(e["failure"], JudgeFailure, nullable=True))
    if kind in ("decision", "outcome"):
        for key in ("input_tokens", "output_tokens"):
            _require(e[key] is None or (type(e[key]) is int and 0 <= e[key] <= 2**63 - 1))
        _require((e["input_tokens"] is None) == (e["output_tokens"] is None))
        _require(e["measured_cost_usd"] is None or _number(e["measured_cost_usd"]))
    if kind == "decision":
        _require(_choice(e["phase"], JudgePhase))
        _require(_choice(e["reason"], GateReason))
        _require(e["criteria_hash"] is None or (type(e["criteria_hash"]) is str
                 and re.fullmatch(r"[a-f0-9]{64}", e["criteria_hash"]) is not None))
        _require(_number(e["latency_ms"]))
        _require(_choice(e["backend"], WORKER_ROUTES, nullable=True))
        answers = e["answers"]
        _require((answers is None) != (e["failure"] is None))
        if answers is not None:
            _require(type(answers) is dict and set(answers) == {
                "route", "route_confidence", "needs_human", "noul_confidence",
                "action_risk", "risk_confidence",
            })
            _require(_choice(answers["route"], JudgeRoute))
            for key in set(answers) - {"route"}:
                _require(_number(answers[key], 2 if key == "action_risk" else 1))
        action = e["intended_action"] if shadow else e["effective_action"]
        _require(_choice(action, GateAction))
        _require((action == "proceed") == (e["backend"] is not None))
        if shadow:
            _require(e["effective_action"] == "baseline")
            _require(e["observed_issue_id"] == e["issue_id"])
        else:
            _require(e["intended_action"] == (answers["route"] if answers else None))
    elif kind == "outcome":
        _require(_number(e["elapsed_worker_ms"]))
        _require(_choice(e["observed_status"], OutcomeStatus, nullable=shadow))
        if shadow:
            _require(type(e["attribution_mismatch"]) is bool and type(e["accuracy_eligible"]) is bool)
            matched = e["actual_claimed_id"] is not None and e["actual_claimed_id"] == e["observed_issue_id"]
            _require(e["attribution_mismatch"] == (e["actual_claimed_id"] is not None and not matched))
            _require(e["accuracy_eligible"] == (matched and e["observed_status"] not in (None, "unknown")))
            _require(matched == (e["observed_status"] is not None))
    else:
        _require(e["phase"] == "post_turn" and _choice(e["mode"], {"shadow", "enforce"}))
        _require(type(e["exit_status"]) is int)
        for key in ("watchdog", "branch_advanced", "disagreement"):
            _require(type(e[key]) is bool)
        _require(_choice(e["observed_status"], OutcomeStatus))
        _require(_choice(e["outcome"], OUTCOMES, nullable=True))
        _require(_choice(e["intended_outcome"], OUTCOMES, nullable=True))
        _require((e["outcome"] is None) == (e["failure"] is not None))
        _require(e["confidence"] is None if e["failure"] else _number(e["confidence"], 1))
        _require(_choice(e["effective_action"], {"baseline"} if shadow else {"human", "preserve"}))
        _require(_choice(e["reason"], {"classified", "tracker_closed", "worker_timeout",
                 "service_failure", "low_confidence", "closure_disagreement", "plan_gap", "auth", "needs_human"}))
        _require(_choice(e.get("worker_failure"), WORKER_FAILURES, nullable=True))
    return e


def read_events(path: Path, *, warn: Callable[[str], None] = lambda _: None) -> Iterable[dict]:
    """Read one bounded line at a time; only a malformed final fragment is skipped."""
    try:
        with path.open("rb") as stream:
            line_number = 0
            while data := stream.readline(MAX_EVENT_BYTES + 1):
                line_number += 1
                if len(data) > MAX_EVENT_BYTES:
                    raise ReplayError(f"event line {line_number}: record too large")
                try:
                    value = _decode(data)
                except (ValueError, UnicodeError, RecursionError):
                    if not data.endswith(b"\n"):
                        warn(f"event line {line_number}: ignored incomplete final record")
                        return
                    raise ReplayError(f"event line {line_number}: malformed JSON") from None
                try:
                    yield validate_event(value)
                except (ValueError, TypeError, AttributeError, KeyError, RuntimeError, RecursionError):
                    raise ReplayError(f"event line {line_number}: invalid version or event shape") from None
    except OSError:
        raise ReplayError("cannot read events") from None


def read_labels(path: Path) -> dict:
    """Read a bounded JSON object mapping decision UUIDs to operator labels."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_LABEL_BYTES + 1)
        _require(len(data) <= MAX_LABEL_BYTES)
        labels = _decode(data)
        _require(type(labels) is dict)
        for key, value in labels.items():
            _uuid(key)
            _require(type(value) is dict and {"expected_action", "needs_human"} <= set(value)
                     <= {"expected_action", "needs_human", "worker_cost_usd"})
            _require(_choice(value["expected_action"], GateAction))
            _require(type(value["needs_human"]) is bool)
            if "worker_cost_usd" in value:
                _require(_number(value["worker_cost_usd"]))
        return labels
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ReplayError("invalid labels document") from None


@dataclass(frozen=True)
class ReplayData:
    events: tuple[dict, ...]
    pairs: tuple[tuple[dict, dict | None], ...]
    orphan_outcomes: int


def join_events(events: Iterable[dict]) -> ReplayData:
    """Deduplicate identical events and join outcomes by decision and run identity."""
    unique: dict[tuple[str, str], dict] = {}
    identities: dict[str, str] = {}
    for event in events:
        key = (event["decision_id"], event["event"])
        if key in unique:
            _require(unique[key] == event)
            continue
        if event["event"] != "outcome":
            _require(event["decision_id"] not in identities)
            identities[event["decision_id"]] = event["event"]
        unique[key] = event
    pairs = []
    paired = set()
    for key, decision in sorted(unique.items()):
        if decision["event"] != "decision":
            continue
        outcome = unique.get((key[0], "outcome"))
        if outcome is not None:
            _require(outcome["run_id"] == decision["run_id"])
            _require((outcome.get("mode") == "shadow") == (decision.get("mode") == "shadow"))
            if decision.get("mode") == "shadow":
                _require(outcome["observed_issue_id"] == decision["observed_issue_id"])
            paired.add(key[0])
        pairs.append((decision, outcome))
    return ReplayData(tuple(unique[key] for key in sorted(unique)), tuple(pairs),
                      sum(e["event"] == "outcome" and e["decision_id"] not in paired for e in unique.values()))


def eligible_decisions(data: ReplayData) -> tuple[list[dict], int]:
    """Decisions that may enter a denominator, plus how many were excluded.

    A shadow row counts only when its outcome was attributed to the worker's
    actual issue. Summaries and offline calibration share this definition so a
    candidate is never scored against a denominator the metrics never used.
    """
    eligible = []
    excluded = 0
    for decision, outcome in data.pairs:
        if decision.get("mode") == "shadow" and (
            outcome is None or not outcome["accuracy_eligible"] or decision["issue_id"] is None
        ):
            excluded += 1
            continue
        eligible.append(decision)
    return eligible, excluded


def recorded_action(decision: dict) -> str:
    """The action the log attributes to a decision: intended under shadow."""
    return decision["intended_action"] if decision.get("mode") == "shadow" else decision["effective_action"]


def summarize(data: ReplayData, labels: dict) -> dict:
    """Compute nearest-rank latency and explicit denominators, never inferred cost.

    Cost totals cover only records carrying measured cost, with separate counts
    showing how much is unknown. Worker cost labels override outcome cost for
    the same decision, avoiding double counting.
    """
    decisions = [d for d, _ in data.pairs]
    latencies = sorted(d["latency_ms"] for d in decisions)
    eligible, excluded = eligible_decisions(data)
    labeled = [d for d in eligible if d["decision_id"] in labels]
    action = recorded_action
    positives = [d for d in labeled if labels[d["decision_id"]]["needs_human"]]
    negatives = [d for d in labeled if not labels[d["decision_id"]]["needs_human"]]
    def fraction(count: int, denominator: int) -> dict:
        return {"count": count, "denominator": denominator,
                "rate": count / denominator if denominator else None}
    gate_costs = [d["measured_cost_usd"] for d in decisions if d["measured_cost_usd"] is not None]
    worker_costs = []
    for decision, outcome in data.pairs:
        # A worker label or outcome cost must refer to this decision's worker.
        if decision.get("mode") == "shadow" and (outcome is None or not outcome["accuracy_eligible"]):
            continue
        cost = labels.get(decision["decision_id"], {}).get("worker_cost_usd")
        if cost is None and outcome is not None:
            cost = outcome["measured_cost_usd"]
        if cost is not None:
            worker_costs.append(cost)
    return {
        "schema_version": 1,
        "decisions": len(decisions),
        "post_turn_events": sum(e["event"] == "post_turn" for e in data.events),
        "failures": sum(e.get("failure") is not None for e in data.events),
        "fallbacks": sum(d["failure"] is not None and action(d) == "proceed" for d in decisions),
        "escalations": sum(action(d) == "human" for d in decisions),
        "paired_outcomes": sum(o is not None for _, o in data.pairs),
        "orphan_outcomes": data.orphan_outcomes,
        "accuracy_eligible": len(eligible),
        "excluded_shadow": excluded,
        "labeled": len(labeled),
        "unlabeled": len(eligible) - len(labeled),
        "unused_labels": len(set(labels) - {d["decision_id"] for d in labeled}),
        "coverage": len(labeled) / len(eligible) if eligible else None,
        "accuracy": fraction(sum(action(d) == labels[d["decision_id"]]["expected_action"] for d in labeled), len(labeled)),
        "false_escalate": fraction(sum(action(d) == "human" for d in negatives), len(negatives)),
        "false_proceed": fraction(sum(action(d) == "proceed" for d in positives), len(positives)),
        "latency_ms": {f"p{p}": latencies[math.ceil(len(latencies) * p / 100) - 1] if latencies else None for p in (50, 95)},
        "measured_cost_usd": {
            "judge": sum(gate_costs) if gate_costs else None,
            "judge_known": len(gate_costs), "judge_unknown": len(decisions) - len(gate_costs),
            "worker": sum(worker_costs) if worker_costs else None,
            "worker_known": len(worker_costs), "worker_unknown": len(decisions) - len(worker_costs),
        },
    }
