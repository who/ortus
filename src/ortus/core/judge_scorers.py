"""Per-site scorers: Jev's decisions joined to what the tracker says happened.

Each site's scorer turns its log records into rows holding Jev's action, the
code baseline's action, the label the tracker outcome implies, and the
binary probabilities Jev answered. The label is a set of acceptable actions,
so a rule that only rules one action out (an under-routed cheap tier) still
scores. A row whose label cannot be derived carries the reason instead.

`summarize_sites` reduces the rows to the metrics the preregistered rule in
`docs/jev-prereg.md` reads, and `prereg_verdict` applies that rule. Replay
only measures: nothing here changes a decision, and no provider is called.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from ortus.core.judge_replay import (
    MAX_LABEL_BYTES, TOOL_ACTIONS, ReplayError, _decode, _require, _uuid, recorded_action,
)
from ortus.core.judge_stuck import baseline_stuck_action

SITES = ("readiness", "pre_turn", "router", "pre_tool", "progress", "stuck", "triage", "post_turn")

# The preregistered offline rule, read by `prereg_verdict` and pinned to
# docs/jev-prereg.md by a test. Change both or neither.
MIN_LABELED = 100
MCNEMAR_ALPHA = 0.05
MIN_AUC = 0.70
MIN_AUC_CI_LOWER = 0.60
MAX_P95_LATENCY_MS = 1500
MAX_FAILURE_RATE = 0.05
MAX_FIRST_OPTION_SHARE = 0.40
FIRST_OPTION_MIN_OPTIONS = 3

BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 0

INSUFFICIENT = "insufficient"
KEEP = "keep"
DROP = "drop"

TIERS = ("cheap", "baseline", "frontier")
HUMAN_OUTCOMES = {"plan_gap", "auth", "needs_human"}


@dataclass(frozen=True)
class Row:
    """One Jev decision, scored or excluded."""

    site: str
    decision_id: str
    jev_action: str | None
    baseline_action: str
    #: Acceptable actions, or None when `excluded` names why there are none.
    label: frozenset[str] | None = None
    excluded: str | None = None
    #: (name, probability, positive) for each binary probability answered.
    probabilities: tuple[tuple[str, float, bool], ...] = ()
    #: Whether the pick was the first-shown option, on picks of 3 or more.
    first_option: bool | None = None
    failed: bool = False
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass
class Evidence:
    """One input's records, its tracker outcomes, and optional tool labels."""

    records: Sequence[dict]
    outcomes: Mapping[str, dict]
    tool_labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._kinds: dict[str, list[dict]] = defaultdict(list)
        for record in self.records:
            self._kinds[record["event"]].append(record)
        self._post: dict[str, list[dict]] = defaultdict(list)
        for record in sorted(self._kinds["post_turn"], key=lambda r: _when(r["timestamp"])):
            self._post[record["issue_id"]].append(record)
        self._routes = Counter(r["issue_id"] for r in self._kinds["model_route"])
        self._windows = {(r["run_id"], r["issue_id"]): r for r in self._kinds["progress_window"]}

    def kind(self, name: str) -> list[dict]:
        return self._kinds[name]

    def outcome(self, issue_id: object) -> tuple[dict | None, str | None]:
        """The issue's tracker outcome, or the reason it cannot label anything."""
        entry = self.outcomes.get(issue_id) if type(issue_id) is str else None
        if entry is None:
            return None, "issue_missing"
        if entry["history_unavailable"]:
            return None, "history_unavailable"
        return entry, None

    def later_windows(self, issue_id: str, after: str, until: str | None = None) -> list[dict]:
        start = _when(after)
        end = _when(until) if until else None
        return [r for r in self._post[issue_id]
                if _when(r["timestamp"]) > start and (end is None or _when(r["timestamp"]) <= end)]

    def timed_out(self, issue_id: str) -> bool:
        return any(r["reason"] == "worker_timeout" or r["watchdog"] for r in self._post[issue_id]) or any(
            w["outcome"] == "timeout" for (_, issue), w in self._windows.items() if issue == issue_id)

    def route_windows(self, issue_id: str) -> int:
        return self._routes[issue_id]

    def window(self, check: dict) -> dict | None:
        return self._windows.get((check["run_id"], check["issue_id"]))


def clean(outcome: dict) -> bool:
    """Closed, never reopened, and never handed to a human."""
    return outcome["status"] == "closed" and outcome["reopens"] == 0 and not outcome["human"]


def respecced(outcome: dict, after: str, until: str | None) -> bool:
    """Whether the spec changed after `after` and no later than `until`."""
    start, end = _when(after), _when(until) if until else None
    return any(_when(t) > start and (end is None or _when(t) <= end) for t in outcome["spec_changes"])


def _first_option(record: dict) -> bool | None:
    order = record.get("option_order")
    if type(order) is not list or len(order) < FIRST_OPTION_MIN_OPTIONS:
        return None
    pick = next((v for v in (record.get("narrow_answers") or {}).values()
                 if type(v) is str and v in order), None)
    return None if pick is None else pick == order[0]


def _row(site: str, record: dict, jev: str | None, baseline: str, label: frozenset[str] | None,
         excluded: str | None, probabilities: Iterable[tuple[str, float | None, bool]] = (),
         *, failed: bool | None = None) -> Row:
    labeled = excluded is None and label is not None
    return Row(
        site=site, decision_id=record["decision_id"], jev_action=jev, baseline_action=baseline,
        label=label if labeled else None,
        excluded=None if labeled else (excluded or "unlabelable"),
        probabilities=tuple((n, float(p), pos) for n, p, pos in probabilities if p is not None)
        if labeled else (),
        first_option=_first_option(record),
        failed=(record.get("failure") is not None) if failed is None else failed,
        latency_ms=record.get("latency_ms"),
        input_tokens=record.get("input_tokens"),
        output_tokens=record.get("output_tokens"),
    )


def _logged(record: dict, fallback: Callable[[], str | None]) -> str | None:
    """Jev's logged action, or the documented derivation for older records."""
    return record["jev_action"] if "jev_action" in record else fallback()


def score_readiness(ev: Evidence) -> list[Row]:
    """Ready means closed with 0 reopens and no human label."""
    rows = []
    for d in ev.kind("decision"):
        if d["phase"] != "semantic_readiness":
            continue
        narrow = d.get("narrow_answers") or {}
        p_open = narrow.get("open_product_decision")
        if type(p_open) not in (int, float):
            p_open = d["answers"]["needs_human"] if d["answers"] else None
        jev = None if p_open is None else "not_ready" if p_open >= .5 else "ready"
        outcome, why = ev.outcome(d["issue_id"])
        label = None
        if why is None:
            if clean(outcome):
                label = frozenset({"ready"})
            elif outcome["human"] or outcome["status"] == "closed":
                label = frozenset({"not_ready"})
            else:
                why = "unresolved"
        ready = label == {"ready"}
        rows.append(_row("readiness", d, jev, "ready", label, why,
                         [("ready", None if p_open is None else 1 - p_open, ready)]))
    return rows


def score_pre_turn(ev: Evidence) -> list[Row]:
    """Proceed when closed cleanly; human when the bead was handed to one."""
    rows = []
    for d in ev.kind("decision"):
        if d["phase"] != "pre_turn":
            continue
        jev = _logged(d, lambda: recorded_action(d) if d["failure"] is None else None)
        outcome, why = ev.outcome(d["issue_id"])
        label = None
        if why is None:
            if clean(outcome):
                label = frozenset({"proceed"})
            elif outcome["human"]:
                label = frozenset({"human"})
            elif outcome["status"] != "closed":
                why = "unresolved"
        p = d["answers"]["needs_human"] if d["answers"] else None
        rows.append(_row("pre_turn", d, jev, d.get("baseline_action") or "proceed", label, why,
                         [("needs_human", p, label == {"human"})]))
    return rows


def score_router(ev: Evidence) -> list[Row]:
    """A cheap tier that struggled was under-routed; a clean single window was enough.

    A tier that closed cleanly makes every tier at or above it acceptable.
    Whether a lower tier would also have done is unobservable here, so
    over-routing is never charged against Jev.
    """
    rows = []
    for r in ev.kind("model_route"):
        ran = r["tier"] if r["applied"] else "baseline"
        outcome, why = ev.outcome(r["issue_id"])
        label = None
        if why is None:
            struggled = (outcome["reopens"] > 0 or ev.timed_out(r["issue_id"])
                         or ev.route_windows(r["issue_id"]) > 1)
            if ran == "cheap" and struggled:
                label = frozenset({"baseline", "frontier"})
            elif clean(outcome) and not struggled:
                label = frozenset(TIERS[TIERS.index(ran):])
            elif outcome["status"] != "closed" and not outcome["human"]:
                why = "unresolved"
        rows.append(_row("router", r, r.get("jev_action"), r.get("baseline_action") or "baseline",
                         label, why, failed=False))
    return rows


def score_pre_tool(ev: Evidence) -> list[Row]:
    """Rubric labels where given; otherwise only a park is judged, by the bead's fate."""
    rows = []
    for t in ev.kind("tool_decision"):
        judged = t["vector"] is not None
        jev = _logged(t, lambda: t["action"] if judged else None)
        label, why = None, None
        if not judged:
            why = "no_judgment"
        elif t["decision_id"] in ev.tool_labels:
            label = frozenset({ev.tool_labels[t["decision_id"]]})
        elif jev == "park_bead":
            outcome, why = ev.outcome(t["issue_id"])
            if why is None:
                if outcome["status"] == "closed" and not outcome["human"]:
                    label = frozenset({"allow", "deny_call"})
                elif outcome["human"]:
                    label = frozenset({"park_bead"})
                else:
                    why = "unresolved"
        else:
            why = "needs_rubric"
        p = t["vector"]["park_bead"] if judged else None
        rows.append(_row("pre_tool", t, jev, t.get("baseline_action") or "allow", label, why,
                         [("park_bead", p, label is not None and "park_bead" in label)]))
    return rows


def score_progress(ev: Evidence) -> list[Row]:
    """Not looping when the window moved; looping when it timed out with no commits."""
    rows = []
    for c in ev.kind("progress"):
        jev = _logged(c, lambda: None if c["failure"] else "reap" if c["would_reap"] else "continue")
        window = ev.window(c)
        facts = c.get("window_facts") or {}
        files, commits = facts.get("new_or_modified_files"), facts.get("commits_since_start")
        label, why = None, None
        if c["head_advanced"] or (type(files) is int and files > 0) or (
                window is not None and window["outcome"] == "closed"):
            label = frozenset({"continue"})
        elif window is None:
            why = "window_missing"
        elif window["outcome"] == "timeout" and not commits:
            label = frozenset({"reap"})
        rows.append(_row("progress", c, jev, c.get("baseline_action") or "continue", label, why,
                         [("looping", c["vector"]["looping"], label == {"reap"})]))
    return rows


def score_stuck(ev: Evidence) -> list[Row]:
    """Escalate when a human took it, replan when a re-spec closed it, else continue.

    A re-plan note is read as a change to the description, design or
    acceptance criteria in bd history between the decision and the close.
    """
    rows = []
    for s in ev.kind("stuck_decision"):
        jev = _logged(s, lambda: None if s["fail_open"] else s["action"])
        baseline = s.get("baseline_action") or baseline_stuck_action(s["windows"]).value
        outcome, why = ev.outcome(s["issue_id"])
        label = None
        if why is None:
            if outcome["human"]:
                label = frozenset({"escalate"})
            elif outcome["status"] != "closed":
                why = "unresolved"
            elif outcome["closed_at"] is not None:
                closed = outcome["closed_at"]
                if respecced(outcome, s["timestamp"], closed):
                    label = frozenset({"replan"})
                elif len(ev.later_windows(s["issue_id"], s["timestamp"], closed)) <= 2:
                    label = frozenset({"continue"})
        rows.append(_row("stuck", s, jev, baseline, label, why,
                         [(name, p, label is not None and name in label)
                          for name, p in sorted(s["vector"].items())],
                         failed=s["fail_open"]))
    return rows


def score_triage(ev: Evidence) -> list[Row]:
    """The class is whatever resolved the bead: a re-spec, a human, or neither (a pin)."""
    rows = []
    for t in ev.kind("triage"):
        jev = _logged(t, lambda: None if t["fail_open"] else t["triage_class"])
        outcome, why = ev.outcome(t["issue_id"])
        label = None
        if why is None:
            if outcome["status"] != "closed":
                why = "unresolved"
            elif respecced(outcome, t["timestamp"], outcome["closed_at"]):
                label = frozenset({"planner_fix"})
            elif outcome["human"]:
                label = frozenset({"needs_human"})
            else:
                label = frozenset({"pin_skew"})
        rows.append(_row("triage", t, jev, t.get("baseline_action") or "needs_human", label, why,
                         [(name, p, label is not None and name in label)
                          for name, p in sorted(t["vector"].items())],
                         failed=t["triage_failure"] is not None))
    return rows


def score_post_turn(ev: Evidence) -> list[Row]:
    """Human when a human took the bead, preserve when it closed without one.

    work_complete is true when this window closed the bead and it was never
    reopened; transient_failure is true when the next window closed it with
    no spec change between.
    """
    rows = []
    for p in ev.kind("post_turn"):
        jev = _logged(p, lambda: None if p["failure"] else
                      "human" if p["outcome"] in HUMAN_OUTCOMES else "preserve")
        outcome, why = ev.outcome(p["issue_id"])
        label, probabilities = None, []
        if why is None:
            if outcome["human"]:
                label = frozenset({"human"})
            elif outcome["status"] == "closed":
                label = frozenset({"preserve"})
            else:
                why = "unresolved"
        if label is not None:
            narrow = p.get("narrow_answers") or {}
            closed_here = p["observed_status"] == "closed"
            probabilities.append(("work_complete", narrow.get("work_complete"),
                                  closed_here and clean(outcome)))
            following = ev.later_windows(p["issue_id"], p["timestamp"])
            if closed_here or following:
                transient = not closed_here and following[0]["observed_status"] == "closed" and \
                    not respecced(outcome, p["timestamp"], following[0]["timestamp"])
                probabilities.append(("transient_failure", narrow.get("transient_failure"), transient))
        rows.append(_row("post_turn", p, jev, p.get("baseline_action") or "preserve", label, why,
                         [(n, v, pos) for n, v, pos in probabilities if type(v) in (int, float)]))
    return rows


SCORERS: dict[str, Callable[[Evidence], list[Row]]] = {
    "readiness": score_readiness, "pre_turn": score_pre_turn, "router": score_router,
    "pre_tool": score_pre_tool, "progress": score_progress, "stuck": score_stuck,
    "triage": score_triage, "post_turn": score_post_turn,
}


def read_tool_labels(path: Path) -> dict[str, str]:
    """A human rubric for pre_tool: decision UUID to `{"expected_action": ...}`."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_LABEL_BYTES + 1)
        _require(len(data) <= MAX_LABEL_BYTES)
        labels = _decode(data)
        _require(type(labels) is dict)
        result = {}
        for key, value in labels.items():
            _uuid(key)
            _require(type(value) is dict and set(value) == {"expected_action"})
            _require(value["expected_action"] in TOOL_ACTIONS)
            result[key] = value["expected_action"]
        return result
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ReplayError("invalid tool labels document") from None


def mcnemar_p(jev_only: int, baseline_only: int) -> float:
    """Exact two-sided binomial test on the discordant pairs; 1.0 with none."""
    n = jev_only + baseline_only
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(jev_only, baseline_only) + 1))
    return min(1.0, 2 * tail / 2 ** n)


def auc(pairs: Sequence[tuple[float, bool]]) -> float | None:
    """Rank-based AUC with tied scores sharing their average rank; None for one class."""
    positives = sum(1 for _, positive in pairs if positive)
    negatives = len(pairs) - positives
    if positives == 0 or negatives == 0:
        return None
    ordered = sorted(pairs, key=lambda pair: pair[0])
    rank_sum, i = 0.0, 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        average = (i + j + 1) / 2
        rank_sum += average * sum(1 for _, positive in ordered[i:j] if positive)
        i = j
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def bootstrap_ci(pairs: Sequence[tuple[float, bool]]) -> list[float] | None:
    """Seeded percentile bootstrap 95% CI; resamples with one class are skipped."""
    rng = random.Random(BOOTSTRAP_SEED)
    values = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        value = auc([pairs[rng.randrange(len(pairs))] for _ in pairs])
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    return [_rank(values, 2.5), _rank(values, 97.5)]


def _rank(values: Sequence[float], percentile: float) -> float:
    """Nearest rank, as the legacy replay latency uses."""
    return values[max(math.ceil(len(values) * percentile / 100) - 1, 0)]


def _fraction(count: int, denominator: int) -> dict:
    return {"count": count, "denominator": denominator,
            "rate": count / denominator if denominator else None}


def summarize_site(rows: Sequence[Row]) -> dict:
    """One site's metrics and its prereg verdict.

    A decision whose Jev request failed counts as the baseline's action in
    the accuracy comparison, because code decides when Jev has no answer;
    the failure itself is charged in `failure_rate`.
    """
    labeled = [r for r in rows if r.label is not None]
    jev_ok = [(r.jev_action or r.baseline_action) in r.label for r in labeled]
    base_ok = [r.baseline_action in r.label for r in labeled]
    jev_only = sum(j and not b for j, b in zip(jev_ok, base_ok))
    baseline_only = sum(b and not j for j, b in zip(jev_ok, base_ok))
    grouped: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for r in labeled:
        for name, p, positive in r.probabilities:
            grouped[name].append((p, positive))
    aucs = {}
    for name, pairs in sorted(grouped.items()):
        value = auc(pairs)
        aucs[name] = {"n": len(pairs), "auc": value,
                      "ci95": None if value is None else bootstrap_ci(pairs),
                      "reason": "single_class" if value is None else None}
    picks = [r.first_option for r in rows if r.first_option is not None]
    latencies = sorted(r.latency_ms for r in rows if r.latency_ms is not None)
    tokens = [r for r in rows if r.input_tokens is not None]
    summary = {
        "decisions": len(rows),
        "labeled": len(labeled),
        "excluded": len(rows) - len(labeled),
        "excluded_reasons": dict(sorted(Counter(r.excluded for r in rows if r.excluded).items())),
        "accuracy": {"jev": _fraction(sum(jev_ok), len(labeled)),
                     "baseline": _fraction(sum(base_ok), len(labeled))},
        "mcnemar": {"jev_only": jev_only, "baseline_only": baseline_only,
                    "p_value": mcnemar_p(jev_only, baseline_only)},
        "auc": aucs,
        "first_option": _fraction(sum(picks), len(picks)),
        "failure_rate": _fraction(sum(r.failed for r in rows), len(rows)),
        "latency_ms": {f"p{p}": _rank(latencies, p) if latencies else None for p in (50, 95)},
        "tokens": {"input": sum(r.input_tokens for r in tokens),
                   "output": sum(r.output_tokens or 0 for r in tokens),
                   "known": len(tokens)},
    }
    summary["prereg"] = prereg_verdict(summary)
    return summary


def prereg_verdict(summary: dict) -> dict:
    """Apply the offline rule in docs/jev-prereg.md to one site's summary."""
    if summary["labeled"] < MIN_LABELED:
        return {"verdict": INSUFFICIENT, "failed": ["labeled"]}
    failed = []
    mcnemar = summary["mcnemar"]
    if not (mcnemar["p_value"] < MCNEMAR_ALPHA and mcnemar["jev_only"] > mcnemar["baseline_only"]):
        failed.append("accuracy")
    if any(entry["auc"] is None or entry["auc"] < MIN_AUC or entry["ci95"] is None
           or entry["ci95"][0] <= MIN_AUC_CI_LOWER for entry in summary["auc"].values()):
        failed.append("auc")
    p95 = summary["latency_ms"]["p95"]
    if p95 is not None and p95 > MAX_P95_LATENCY_MS:
        failed.append("latency")
    rate = summary["failure_rate"]["rate"]
    if rate is not None and rate > MAX_FAILURE_RATE:
        failed.append("failure_rate")
    share = summary["first_option"]["rate"]
    if share is not None and share > MAX_FIRST_OPTION_SHARE:
        failed.append("first_option")
    return {"verdict": DROP if failed else KEEP, "failed": failed}


def score_inputs(
    inputs: Sequence[Sequence[dict]], outcomes: Mapping[str, dict],
    tool_labels: Mapping[str, str] | None = None,
) -> dict[str, list[Row]]:
    """Every site's rows across several inputs.

    The three logs of one repository are separate files that join by issue
    (a route is labeled from the post_turn windows in the decision log), so
    all inputs form one body of evidence. A record repeated verbatim across
    inputs is scored once; two different records that share a decision id
    stay two rows, because nothing here joins on the decision id across kinds.
    """
    seen: set[str] = set()
    unique = []
    for records in inputs:
        for record in records:
            key = repr(sorted(record.items()))
            if key not in seen:
                seen.add(key)
                unique.append(record)
    evidence = Evidence(unique, outcomes, dict(tool_labels or {}))
    return {site: SCORERS[site](evidence) for site in SITES}


def summarize_sites(
    inputs: Sequence[Sequence[dict]], outcomes: Mapping[str, dict],
    tool_labels: Mapping[str, str] | None = None,
) -> dict[str, dict]:
    """The `sites` section of replay: one summary per Jev site, all eight always present."""
    return {site: summarize_site(rows)
            for site, rows in score_inputs(inputs, outcomes, tool_labels).items()}
