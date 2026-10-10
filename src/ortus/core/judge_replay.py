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
from ortus.core.judge_log import (
    MAX_COMMAND_CHARS, MAX_EVENT_BYTES, QUESTION_VERSION, ROUTER_TIERS, SOFT_BANDS, OutcomeStatus,
    _clean_metadata,
)
from ortus.core.judge_progress import _LOGGED_FACTS, PROGRESS_ACTIONS, WindowOutcome
from ortus.core.judge_state import TRUNCATABLE_FIELDS
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
#: The stuck-claim decision grind takes after a window ends without a close.
#: It carries no provider answer of its own — the vector is derived from the
#: post-turn record written moments earlier — so it shares none of the usage
#: or answer fields.
STUCK = COMMON | {
    "phase", "mode", "issue_id", "seat", "model", "windows", "branch_advanced",
    "fail_open", "vector", "action", "effective_action",
}
#: The route grind took for a bead that reached the human queue. Like the
#: stuck decision it carries no usage of its own: a regex hit and a disabled
#: seat both produce a vector without a request.
TRIAGE = COMMON | {
    "phase", "mode", "issue_id", "seat", "model", "vector", "regex_hit",
    "fail_open", "triage_failure", "triage_class", "effective_action",
}
#: One pre_tool outcome. Unlike the stuck and triage records it does carry
#: usage, because most of these rows are a real request; the local-policy rows
#: carry the same fields with nulls in them rather than a second shape.
TOOL = COMMON | USAGE | {
    "phase", "mode", "issue_id", "seat", "model", "tool", "reason", "vector",
    "action", "effective_action", "failure", "latency_ms",
}
STUCK_ACTIONS = {"continue", "replan", "escalate"}
TRIAGE_CLASSES = {"pin_skew", "planner_fix", "needs_human"}
TOOL_ACTIONS = {"allow", "deny_call", "park_bead"}
OUTCOMES = {"done", "plan_gap", "flake", "auth", "needs_human", "continue"}
#: The worker-failure class a post-turn record may also carry. Optional, and
#: read beside `reason` rather than folded into `OUTCOMES`: a log written
#: before the taxonomy existed is still a valid version-1 record, so the field
#: widens the accepted shape instead of bumping the schema out from under the
#: readers that already consume these files.
POST_SIDE = {"worker_failure", "narrow_answers", "soft_bands"}
#: The issue-text fields the packer shortened for this request. Optional for
#: the same reason: a decision written before truncation existed carries no
#: such list and is still a valid version-1 record.
DECISION_SIDE = {"truncated_fields", "narrow_answers", "soft_bands"}
#: What plain code did beside what Jev's answer argued for, and which wording
#: of the question it answered. Optional on every Jev record kind, so a log
#: written before these fields existed is still a valid version-1 record.
COMPARISON_SIDE = {"baseline_action", "jev_action", "question_version", "option_order"}
#: Request cost for the kinds whose required fields never carried it. Null
#: when the site made no request.
REQUEST_SIDE = {"latency_ms", "input_tokens", "output_tokens"}
#: The narrow answers a triage record's vector was read off, and their bands.
NARROW_SIDE = {"narrow_answers", "soft_bands"}
#: The bounded, redacted command a pre_tool record may carry.
TOOL_SIDE = {"command", "narrow_answers", "soft_bands"}
#: Each kind's action vocabulary for `baseline_action` and `jev_action`, and
#: the choice options its `option_order` may name.
POST_ACTIONS = {"human", "preserve"}
#: One bead's model-routing record, from `jev-model-routes.jsonl`.
ROUTE = COMMON | {
    "seat", "issue_id", "backend", "tier", "reason", "model", "reasoning_effort",
    "applied", "needs_frontier", "action_risk", "difficulty",
}
#: One progress check and the window it watched, from `jev-progress.jsonl`.
#: `window_facts` is optional because checks written before the computed
#: facts existed carry only the thin ones.
PROGRESS = COMMON | {
    "phase", "mode", "issue_id", "seat", "model", "elapsed_seconds", "head_advanced",
    "bead_status", "vector", "confidence", "shrunk_looping", "failure", "would_reap", "reaped",
}
PROGRESS_WINDOW = COMMON | {
    "phase", "mode", "issue_id", "seat", "outcome", "checks", "would_reap", "reaped",
}
#: The record kinds each log file holds, so one reader can take any of them.
SITE_KINDS = {"model_route", "progress", "progress_window"}
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


def _vocabularies(kind: str) -> tuple[set[str], set[str]]:
    """One kind's actions and the choice options its question presents."""
    if kind == "decision":
        return {a.value for a in GateAction}, {r.value for r in JudgeRoute} | {"none"}
    if kind == "post_turn":
        return POST_ACTIONS, OUTCOMES
    if kind == "stuck_decision":
        return STUCK_ACTIONS, STUCK_ACTIONS
    if kind == "triage":
        return TRIAGE_CLASSES, TRIAGE_CLASSES | {"none"}
    if kind == "model_route":
        return set(ROUTER_TIERS), set(ROUTER_TIERS)
    if kind in ("progress", "progress_window"):
        return set(PROGRESS_ACTIONS), set(PROGRESS_ACTIONS)
    return TOOL_ACTIONS, TOOL_ACTIONS


def _side(e: dict, kind: str) -> None:
    """Type-check whichever optional comparison and request fields are present."""
    actions, options = _vocabularies(kind)
    for key in ("baseline_action", "jev_action"):
        if key in e:
            _require(_choice(e[key], actions, nullable=True))
    if "question_version" in e:
        version = e["question_version"]
        _require(version is None or (type(version) is str
                 and QUESTION_VERSION.fullmatch(version) is not None))
    if "option_order" in e:
        order = e["option_order"]
        _require(order is None or (type(order) is list
                 and all(_choice(name, options) for name in order)
                 and len(set(order)) == len(order)))
    if kind in ("post_turn", "stuck_decision", "triage", *SITE_KINDS):
        if "latency_ms" in e:
            _require(e["latency_ms"] is None or _number(e["latency_ms"]))
        for key in ("input_tokens", "output_tokens"):
            if key in e:
                _require(e[key] is None or (type(e[key]) is int and 0 <= e[key] <= 2**63 - 1))
        _require(e.get("input_tokens") is None and e.get("output_tokens") is None
                 or ("input_tokens" in e and "output_tokens" in e
                     and e["input_tokens"] is not None and e["output_tokens"] is not None))
    if "command" in e:
        _require(type(e["command"]) is str and len(e["command"]) <= MAX_COMMAND_CHARS)
    if "narrow_answers" in e or "soft_bands" in e:
        narrow, bands = e.get("narrow_answers"), e.get("soft_bands")
        _require(type(narrow) is dict and type(bands) is dict and len(narrow) <= 16)
        for key, value in narrow.items():
            _require(re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key) is not None)
            _require(_choice(value, options) if type(value) is str else _number(value, 1))
        _require(set(bands) <= set(narrow) and all(_choice(v, SOFT_BANDS) for v in bands.values()))


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
              POST if kind == "post_turn" else
              STUCK if kind == "stuck_decision" else
              TRIAGE if kind == "triage" else
              TOOL if kind == "tool_decision" else set())
    optional = (POST_SIDE | COMPARISON_SIDE | REQUEST_SIDE if kind == "post_turn"
                else DECISION_SIDE | COMPARISON_SIDE if kind == "decision"
                else COMPARISON_SIDE | REQUEST_SIDE if kind == "stuck_decision"
                else COMPARISON_SIDE | REQUEST_SIDE | NARROW_SIDE if kind == "triage"
                else COMPARISON_SIDE | TOOL_SIDE if kind == "tool_decision" else set())
    _require(bool(fields) and fields <= set(e) <= fields | optional)
    _uuid(e["run_id"])
    _uuid(e["decision_id"])
    _require(type(e["timestamp"]) is str and len(e["timestamp"]) <= 40)
    _require(datetime.fromisoformat(e["timestamp"]).utcoffset() is not None)
    for name in ("seat", "issue_id", "criteria_version", "observed_issue_id", "actual_claimed_id"):
        if name in e:
            _metadata(e[name])
    if kind in ("decision", "post_turn", "stuck_decision", "triage", "tool_decision"):
        _require(e["model"] is None or (type(e["model"]) is str
                 and re.fullmatch(r"jev-\d+\.\d+\.\d+", e["model"]) is not None))
    if kind in ("decision", "post_turn", "tool_decision"):
        _require(_choice(e["failure"], JudgeFailure, nullable=True))
    if kind in ("decision", "outcome", "tool_decision"):
        for key in ("input_tokens", "output_tokens"):
            _require(e[key] is None or (type(e[key]) is int and 0 <= e[key] <= 2**63 - 1))
        _require((e["input_tokens"] is None) == (e["output_tokens"] is None))
        _require(e["measured_cost_usd"] is None or _number(e["measured_cost_usd"]))
    if kind != "outcome":
        _side(e, kind)
    if kind == "decision":
        _require(_choice(e["phase"], JudgePhase))
        _require(_choice(e["reason"], GateReason))
        _require(e["criteria_hash"] is None or (type(e["criteria_hash"]) is str
                 and re.fullmatch(r"[a-f0-9]{64}", e["criteria_hash"]) is not None))
        _require(_number(e["latency_ms"]))
        names = e.get("truncated_fields", [])
        # Every element is proved a known name before the duplicate check, so an
        # unhashable value is a rejected record rather than a reader crash.
        _require(type(names) is list)
        _require(all(_choice(name, TRUNCATABLE_FIELDS) for name in names))
        _require(len(set(names)) == len(names))
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
    elif kind == "stuck_decision":
        _require(e["phase"] == "stuck" and _choice(e["mode"], {"shadow", "enforce"}))
        _require(type(e["windows"]) is int and 0 <= e["windows"] <= 2**31 - 1)
        for key in ("branch_advanced", "fail_open"):
            _require(type(e[key]) is bool)
        for key in ("action", "effective_action"):
            _require(_choice(e[key], STUCK_ACTIONS))
        vector = e["vector"]
        _require(type(vector) is dict and set(vector) == STUCK_ACTIONS)
        for value in vector.values():
            _require(_number(value, 1))
        # Shadow records the read action and applies the fail-open baseline,
        # so the two differ there exactly when the judge argued for something
        # else; outside shadow the applied action is the decided one.
        _require(shadow or e["effective_action"] == e["action"])
    elif kind == "triage":
        _require(e["phase"] == "triage" and _choice(e["mode"], {"shadow", "enforce"}))
        for key in ("regex_hit", "fail_open"):
            _require(type(e[key]) is bool)
        _require(_choice(e["triage_failure"], JudgeFailure, nullable=True))
        for key in ("triage_class", "effective_action"):
            _require(_choice(e[key], TRIAGE_CLASSES))
        vector = e["vector"]
        _require(type(vector) is dict and set(vector) == TRIAGE_CLASSES)
        for value in vector.values():
            _require(_number(value, 1))
        # A route that could not complete falls back to the human queue, and
        # shadow records the read class while parking the bead. Those are the
        # only ways the applied route differs from the decided one: an
        # automatic route can never be applied over a different decision.
        _require(shadow or e["effective_action"] in (e["triage_class"], "needs_human"))
    elif kind == "tool_decision":
        _require(e["phase"] == "pre_tool" and _choice(e["mode"], {"shadow", "enforce"}))
        _require(type(e["tool"]) is str and type(e["reason"]) is str)
        _require(_number(e["latency_ms"]))
        for key in ("action", "effective_action"):
            _require(_choice(e[key], TOOL_ACTIONS))
        vector = e["vector"]
        _require(vector is None or (type(vector) is dict and set(vector) == TOOL_ACTIONS))
        for value in (vector or {}).values():
            _require(_number(value, 1))
        # Local policy answers without a vector and only ever denies the call;
        # shadow records what a judgment read and applies allow. Those are the
        # only two ways the applied action differs from the decided one.
        _require(vector is not None or e["action"] == "deny_call")
        _require(e["effective_action"] == ("allow" if shadow and vector is not None
                                           else e["action"]))
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
                 "service_failure", "low_confidence", "closure_disagreement", "plan_gap", "auth", "needs_human",
                 "no_basis"}))
        _require(_choice(e.get("worker_failure"), WORKER_FAILURES, nullable=True))
    return e


def _fact(value: object) -> bool:
    """One logged window fact: a count, a flag, a short result, or a trend of them."""
    if value is None or type(value) is bool or _number(value):
        return True
    if type(value) is str:
        return len(value) <= 32 and re.fullmatch(r"[a-z0-9_.-]*", value) is not None
    return type(value) is list and len(value) <= 16 and all(
        type(item) is str and _fact(item) for item in value)


def validate_site_record(value: object) -> dict:
    """Validate a routing or progress record against its writer's allowlist."""
    _require(type(value) is dict)
    e = value
    _require(type(e.get("schema_version")) is int and e["schema_version"] == 1)
    kind = e.get("event")
    fields = (ROUTE if kind == "model_route" else PROGRESS if kind == "progress"
              else PROGRESS_WINDOW if kind == "progress_window" else set())
    optional = COMPARISON_SIDE | REQUEST_SIDE | (
        {"window_facts"} | NARROW_SIDE if kind == "progress" else set())
    _require(bool(fields) and fields <= set(e) <= fields | optional)
    _uuid(e["run_id"])
    _uuid(e["decision_id"])
    _require(type(e["timestamp"]) is str and len(e["timestamp"]) <= 40)
    _require(datetime.fromisoformat(e["timestamp"]).utcoffset() is not None)
    for name in ("seat", "issue_id", "backend", "reason", "model", "reasoning_effort"):
        if name in e and not (kind == "progress" and name == "model"):
            _metadata(e[name])
    _side(e, kind)
    if kind == "model_route":
        _require(_choice(e["tier"], ROUTER_TIERS) and type(e["applied"]) is bool)
        for key in ("needs_frontier", "action_risk", "difficulty"):
            _require(e[key] is None or _number(e[key], 1))
        return e
    _require(e["phase"] == "progress" and _choice(e["mode"], {"shadow", "enforce"}))
    _require(type(e["reaped"]) is bool)
    if kind == "progress_window":
        _require(_choice(e["outcome"], {o.value for o in WindowOutcome}))
        for key in ("checks", "would_reap"):
            _require(type(e[key]) is int and 0 <= e[key] <= 2**31 - 1)
        return e
    _require(e["model"] is None or (type(e["model"]) is str
             and re.fullmatch(r"jev-\d+\.\d+\.\d+", e["model"]) is not None))
    _require(_choice(e["failure"], JudgeFailure, nullable=True))
    _require(_number(e["elapsed_seconds"]) and type(e["head_advanced"]) is bool)
    _require(_choice(e["bead_status"], OutcomeStatus))
    _require(type(e["would_reap"]) is bool)
    _require(e["shrunk_looping"] is None or _number(e["shrunk_looping"], 1))
    vector = e["vector"]
    _require(type(vector) is dict and set(vector) == {"looping", "progressing"})
    for key in ("looping", "progressing", "confidence"):
        number = vector.get(key) if key != "confidence" else e["confidence"]
        _require(number is None or _number(number, 1))
    facts = e.get("window_facts")
    _require(facts is None or (type(facts) is dict and set(facts) <= set(_LOGGED_FACTS)
                               and all(_fact(v) for v in facts.values())))
    return e


def validate_record(value: object) -> dict:
    """Validate a record from any of the three Jev logs."""
    if type(value) is dict and value.get("event") in SITE_KINDS:
        return validate_site_record(value)
    return validate_event(value)


def read_records(path: Path, *, warn: Callable[[str], None] = lambda _: None) -> Iterable[dict]:
    """`read_events` over any of the three Jev logs: decisions, routes and progress."""
    return read_events(path, warn=warn, validate=validate_record)


def read_events(
    path: Path, *, warn: Callable[[str], None] = lambda _: None,
    validate: Callable[[object], dict] = validate_event,
) -> Iterable[dict]:
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
                    yield validate(value)
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
