"""Per-site scorers joined to read-only tracker outcomes, and the prereg verdict."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core import judge_outcomes, judge_scorers
from ortus.core.bd import BdError
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeAnswers, JudgeConfig, JudgeMode, JudgePhase,
    JudgeRoute,
)
from ortus.core.judge_log import (
    LOG_NAME, ROUTE_LOG_NAME, DecisionEvent, ModelRouteEvent, OutcomeStatus, write_decision,
    write_model_route, write_tool_decision,
)
from ortus.core.judge_outcomes import read_outcomes, snapshot_outcomes
from ortus.core.judge_post import Outcome, OutcomeVerdict, WorkerOutcome, apply_outcome
from ortus.core.judge_progress import (
    PROGRESS_LOG_NAME, ProgressCheck, ProgressMode, ProgressVerdict, ProgressWindow,
    WindowOutcome, WorkerProgress, log_progress_check, log_progress_window,
)
from ortus.core.judge_replay import ReplayError, read_records, validate_record
from ortus.core.judge_scorers import (
    INSUFFICIENT, KEEP, DROP, SITES, Row, auc, mcnemar_p, prereg_verdict, summarize_site,
    summarize_sites,
)
from ortus.core.judge_stuck import decide_stuck_claim, log_stuck_decision
from ortus.core.judge_triage import TriageClass, TriageVector, log_triage, route_triage
from ortus.core.judge_typesafe import JudgeUsage

CONFIG = JudgeConfig(enabled=True)
DOC = Path(__file__).resolve().parents[1] / "docs" / "jev-prereg.md"
T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def at(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


class Tracker:
    def show(self, _):
        return {"status": "in_progress", "labels": []}


@pytest.fixture(scope="module")
def templates(tmp_path_factory) -> dict[str, dict]:
    """One record of every kind, as the real writers produce it."""
    repo = tmp_path_factory.mktemp("templates")
    gate = DecisionEvent(
        run_id=uuid4(), seat="default", issue_id="t-1", phase=JudgePhase.PRE_TURN,
        answers=JudgeAnswers(JudgeRoute.CODEX, .9, .2, .9, 0, .9),
        decision=GateDecision(GateAction.PROCEED, JudgeRoute.CODEX, GateReason.ROUTED),
        model=CONFIG.model, criteria_version="pre-turn-v2", criteria_hash="ab" * 32,
        latency_ms=120, usage=JudgeUsage(300, 20),
    )
    write_decision(repo, CONFIG, gate)
    write_decision(repo, replace(CONFIG, mode=JudgeMode.SHADOW),
                   replace(gate, decision_id=uuid4(), phase=JudgePhase.SEMANTIC_READINESS))
    verdict = OutcomeVerdict(Outcome.FLAKE, .8, latency_ms=250.0, usage=JudgeUsage(400, 12))
    apply_outcome(Tracker(), repo, "t-1", WorkerOutcome(0, False, OutcomeStatus.IN_PROGRESS, False),
                  verdict, replace(CONFIG, mode=JudgeMode.SHADOW), uuid4())
    decision = decide_stuck_claim(verdict, 1, False)
    log_stuck_decision(repo, CONFIG, uuid4(), "t-1", decision, decision.action)
    vector = TriageVector(0.1, 0.7, 0.2, latency_ms=90.0, usage=JudgeUsage(200, 5))
    log_triage(repo, CONFIG, uuid4(), "t-1", vector, route_triage(vector), TriageClass.PLANNER_FIX)
    write_tool_decision(repo, CONFIG, uuid4(), "t-1", {
        "reason": "judged", "tool": "Bash", "action": "park_bead", "effective_action": "park_bead",
        "vector": {"allow": 0.1, "deny_call": 0.2, "park_bead": 0.7}, "failure": None,
        "latency_ms": 41.5, "input_tokens": 120, "output_tokens": 9, "command": "rm -rf build",
        "baseline_action": "allow", "jev_action": "park_bead",
        "question_version": "pre-tool-v1:" + "0" * 16, "option_order": None,
    })
    run = uuid4()
    log_progress_check(repo, CONFIG, ProgressCheck(
        run, uuid4(), "t-1", ProgressMode.SHADOW,
        WorkerProgress(30.0, False, OutcomeStatus.IN_PROGRESS),
        ProgressVerdict(0.9, 0.1, 0.9, latency_ms=70.0, usage=JudgeUsage(50, 2)), True, False,
    ))
    log_progress_window(repo, CONFIG, ProgressWindow(
        run, uuid4(), "t-1", ProgressMode.SHADOW, WindowOutcome.TIMEOUT, 1, 1, False))
    write_model_route(repo, CONFIG, ModelRouteEvent(
        uuid4(), uuid4(), "default", "t-1", "claude", "cheap", "mapped", jev_tier="cheap"))
    found = {}
    for name in (LOG_NAME, ROUTE_LOG_NAME, PROGRESS_LOG_NAME):
        for record in read_records(repo / "logs" / name):
            key = record["event"] if record["event"] != "decision" else record["phase"]
            found[key] = record
    assert set(found) == {"pre_turn", "semantic_readiness", "post_turn", "stuck_decision",
                          "triage", "tool_decision", "progress", "progress_window", "model_route"}
    return found


def make(templates, kind: str, issue: str, minutes: float = 0, **changes) -> dict:
    record = {**templates[kind], "decision_id": str(uuid4()), "issue_id": issue,
              "timestamp": at(minutes), **changes}
    if "observed_issue_id" in record:
        record["observed_issue_id"] = issue
    return validate_record(record)


def outcome(status="closed", *, reopens=0, human=False, closed=60, spec=(), history=True) -> dict:
    return {
        "status": status, "labels": ["human"] if human else [], "human": human,
        "closed_at": at(closed) if status == "closed" else None,
        "reopens": reopens if history else None,
        "spec_changes": [at(m) for m in spec] if history else None,
        "history_unavailable": not history,
    }


def rows(templates, records, outcomes, tool_labels=None):
    return judge_scorers.score_inputs([records], outcomes, tool_labels)


# --- AC-1: the read-only outcomes snapshot ------------------------------------------


def history(*states):
    """bd history entries newest first, from (minutes, status, design) oldest first."""
    return [{"CommitHash": "x", "CommitDate": at(m).replace("+00:00", "Z"),
             "Issue": {"id": "a", "status": s, "description": "d", "design": design,
                       "acceptance_criteria": "c"}}
            for m, s, design in reversed(states)]


def fake_bd(calls):
    listing = [
        {"id": "a", "status": "closed", "labels": [], "closed_at": "2026-10-10T13:00:00Z"},
        {"id": "b", "status": "open", "labels": ["human"], "closed_at": None},
        {"id": "c", "status": "closed", "closed_at": "2026-10-10T13:00:00.5-07:00"},
        {"id": "unreferenced", "status": "open"},
    ]
    histories = {
        "a": history((0, "open", "v1"), (10, "closed", "v1"), (20, "open", "v2"),
                     (30, "closed", "v2")),
        "b": history((0, "open", "v1")),
    }

    def run(args):
        calls.append(tuple(args))
        if args[0] == "list":
            return listing
        if args[1] in histories:
            return histories[args[1]]
        raise BdError(["bd", *args], 1, "boom")
    return run


def test_outcomes_snapshot_reads_status_labels_reopens_and_human(tmp_path):
    calls = []
    snapshot = snapshot_outcomes(tmp_path, ["a", "b", "c", "gone"], run=fake_bd(calls))
    issues = snapshot["issues"]
    assert set(issues) == {"a", "b", "c"}
    assert issues["a"]["reopens"] == 1 and issues["a"]["spec_changes"] == [at(20)]
    assert issues["a"]["closed_at"] == "2026-10-10T13:00:00+00:00"
    assert issues["b"] == {"status": "open", "labels": ["human"], "human": True,
                           "closed_at": None, "reopens": 0, "spec_changes": [],
                           "history_unavailable": False}
    assert issues["c"]["history_unavailable"] and issues["c"]["reopens"] is None
    assert issues["c"]["closed_at"] == "2026-10-10T20:00:00.500000+00:00"
    assert {call[0] for call in calls} == {"list", "history"}
    path = tmp_path / "outcomes.json"
    path.write_text(judge_outcomes.dumps(snapshot))
    assert read_outcomes(path) == issues


def test_outcomes_runner_is_readonly_and_sandboxed(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(judge_outcomes.BeadsTracker, "run",
                        lambda self, *args, parse_json=False: (seen.append(args), ("", []))[1])
    snapshot_outcomes(tmp_path, [])
    assert seen == [("--readonly", "--sandbox", "list", "--all", "--json", "--limit", "0")]


@pytest.mark.parametrize("body", [
    "[]", '{"schema_version": 2, "issues": {}}',
    '{"schema_version": 1, "issues": {"a": {"status": "closed"}}}',
    json.dumps({"schema_version": 1, "issues": {"a": {**outcome(), "reopens": None}}}),
])
def test_outcomes_document_is_validated(tmp_path, body):
    path = tmp_path / "outcomes.json"
    path.write_text(body)
    with pytest.raises(ReplayError, match="invalid outcomes document"):
        read_outcomes(path)


def test_outcomes_cli_snapshots_issues_the_logs_reference(tmp_path, monkeypatch, templates):
    calls = []
    monkeypatch.setattr(judge_outcomes, "bd_runner", lambda repo: fake_bd(calls))
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / LOG_NAME).write_text(json.dumps(make(templates, "pre_turn", "a")) + "\n")
    (logs / ROUTE_LOG_NAME).write_text(json.dumps(make(templates, "model_route", "b")) + "\n")
    target = tmp_path / "out.json"
    result = CliRunner().invoke(app, ["judge", "outcomes", str(tmp_path), "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert set(read_outcomes(target)) == {"a", "b"}
    assert "done (2 issues)" in result.stderr
    assert sorted(call[1] for call in calls if call[0] == "history") == ["a", "b"]


# --- AC-2: a sites entry for every site ---------------------------------------------


def corpus(templates):
    """At least one labeled decision per site, against these outcomes."""
    outcomes = {"clean": outcome(), "human": outcome("open", human=True),
                "respec": outcome(spec=[30]), "open": outcome("open"),
                "reopened": outcome(reopens=1), "nohist": outcome(history=False)}
    window_run = str(uuid4())
    records = [
        make(templates, "semantic_readiness", "clean",
             narrow_answers={"open_product_decision": 0.1}, soft_bands={}),
        make(templates, "semantic_readiness", "human",
             narrow_answers={"open_product_decision": 0.8}, soft_bands={}),
        make(templates, "pre_turn", "clean"),
        make(templates, "pre_turn", "human"),
        make(templates, "pre_turn", "open"),
        make(templates, "model_route", "clean", tier="baseline", jev_action="cheap"),
        make(templates, "model_route", "reopened", tier="cheap", jev_action="frontier"),
        make(templates, "tool_decision", "clean"),
        make(templates, "tool_decision", "human"),
        make(templates, "tool_decision", "clean", jev_action="allow"),
        make(templates, "progress", "clean", 5, run_id=window_run, head_advanced=True),
        make(templates, "progress", "open", 5),
        make(templates, "progress_window", "open", 9),
        make(templates, "stuck_decision", "human", 10),
        make(templates, "stuck_decision", "respec", 10),
        make(templates, "stuck_decision", "clean", 10),
        make(templates, "triage", "respec", 10),
        make(templates, "triage", "clean", 10),
        make(templates, "triage", "nohist", 10),
        make(templates, "post_turn", "clean", 5, observed_status="closed",
             narrow_answers={"work_complete": 0.9, "transient_failure": 0.1}, soft_bands={}),
        make(templates, "post_turn", "human", 5),
        make(templates, "post_turn", "missing", 5),
    ]
    # The progress window ended in a timeout for the second check's run.
    records[12]["run_id"] = records[11]["run_id"]
    return records, outcomes


def test_sites_entry_for_every_site_with_every_metric(templates):
    records, outcomes = corpus(templates)
    sites = summarize_sites([records], outcomes)
    assert tuple(sites) == SITES
    for site, summary in sites.items():
        assert summary["labeled"] >= 1, site
        assert {"decisions", "labeled", "excluded", "excluded_reasons", "accuracy", "mcnemar",
                "auc", "first_option", "failure_rate", "latency_ms", "tokens", "prereg"} <= set(summary)
        assert set(summary["accuracy"]) == {"jev", "baseline"}
        assert {"jev_only", "baseline_only", "p_value"} == set(summary["mcnemar"])
        assert summary["prereg"]["verdict"] == INSUFFICIENT
    assert sites["readiness"]["accuracy"]["jev"]["rate"] == 1.0
    assert sites["readiness"]["accuracy"]["baseline"]["rate"] == 0.5
    assert sites["readiness"]["auc"]["ready"]["auc"] == 1.0
    assert sites["pre_turn"]["excluded_reasons"] == {"unresolved": 1}
    assert sites["pre_turn"]["latency_ms"] == {"p50": 120, "p95": 120}
    assert sites["pre_turn"]["tokens"] == {"input": 900, "output": 60, "known": 3}
    assert sites["triage"]["excluded_reasons"] == {"history_unavailable": 1}
    assert sites["post_turn"]["excluded_reasons"] == {"issue_missing": 1}
    assert sites["pre_tool"]["excluded_reasons"] == {"needs_rubric": 1}


def test_sites_label_rules(templates):
    records, outcomes = corpus(templates)
    scored = rows(templates, records, outcomes)
    labels = {site: [r.label for r in found] for site, found in scored.items()}
    assert labels["readiness"] == [{"ready"}, {"not_ready"}]
    assert labels["pre_turn"] == [{"proceed"}, {"human"}, None]
    assert labels["router"] == [{"baseline", "frontier"}, {"baseline", "frontier"}]
    assert labels["pre_tool"] == [{"allow", "deny_call"}, {"park_bead"}, None]
    assert labels["progress"] == [{"continue"}, {"reap"}]
    assert labels["stuck"] == [{"escalate"}, {"replan"}, {"continue"}]
    assert labels["triage"] == [{"planner_fix"}, {"pin_skew"}, None]
    assert labels["post_turn"] == [{"preserve"}, {"human"}, None]
    assert scored["post_turn"][0].probabilities == (
        ("work_complete", 0.9, True), ("transient_failure", 0.1, False))


def test_sites_tool_rubric_and_older_records_use_documented_baselines(templates):
    tool = make(templates, "tool_decision", "clean", jev_action="allow")
    stuck = {k: v for k, v in make(templates, "stuck_decision", "clean", windows=3).items()
             if k not in ("baseline_action", "jev_action")}
    scored = rows(templates, [tool, validate_record(stuck)], {"clean": outcome()},
                  {tool["decision_id"]: "deny_call"})
    assert scored["pre_tool"][0].label == {"deny_call"}
    assert scored["stuck"][0].baseline_action == "escalate"
    assert scored["stuck"][0].jev_action == stuck["action"]


def test_sites_replay_cli_with_outcomes_and_no_labels(tmp_path, templates):
    records, outcomes = corpus(templates)
    decisions, routes, progress_log = (tmp_path / n for n in (LOG_NAME, ROUTE_LOG_NAME, PROGRESS_LOG_NAME))
    for path, kinds in ((decisions, {"decision", "post_turn", "stuck_decision", "triage", "tool_decision"}),
                        (routes, {"model_route"}), (progress_log, {"progress", "progress_window"})):
        path.write_text("".join(json.dumps(r) + "\n" for r in records if r["event"] in kinds))
    snapshot = tmp_path / "outcomes.json"
    snapshot.write_text(json.dumps({"schema_version": 1, "issues": outcomes}))
    metrics = tmp_path / "metrics.json"
    result = CliRunner().invoke(app, ["judge", "replay", str(decisions), str(routes), str(progress_log),
                                      "--outcomes", str(snapshot), "--output", str(metrics)])
    assert result.exit_code == 0, result.output
    report = json.loads(metrics.read_text())
    assert set(report["sites"]) == set(SITES)
    assert report["sites"] == json.loads(json.dumps(summarize_sites([records], outcomes)))
    assert report["labeled"] == 0 and report["decisions"] == 5
    missing = CliRunner().invoke(app, ["judge", "replay", str(decisions), "--output", str(metrics),
                                       "--force"])
    assert missing.exit_code == 1 and "pass --labels, --outcomes, or both" in missing.stderr


def test_sites_metrics_mcnemar_auc_and_edges():
    assert mcnemar_p(0, 0) == 1.0
    assert mcnemar_p(10, 0) == pytest.approx(2 / 2 ** 10)
    assert mcnemar_p(3, 3) == 1.0
    assert auc([(0.1, False), (0.9, True)]) == 1.0
    assert auc([(0.5, False), (0.5, True)]) == 0.5
    assert auc([(0.2, True), (0.9, True)]) is None
    single = summarize_site([Row("readiness", "x", "ready", "ready", frozenset({"ready"}),
                                 probabilities=(("ready", .9, True),))])
    assert single["auc"]["ready"] == {"n": 1, "auc": None, "ci95": None, "reason": "single_class"}
    first = summarize_site([Row("triage", str(i), "pin_skew", "needs_human", frozenset({"pin_skew"}),
                                first_option=i < 3) for i in range(4)])
    assert first["first_option"] == {"count": 3, "denominator": 4, "rate": 0.75}


# --- AC-3: the prereg verdict -------------------------------------------------------


def labeled_rows(n, *, jev_right=True, latency=100.0, failed=0, auc_good=True):
    found = []
    for i in range(n):
        positive = i % 2 == 0
        p = (0.9 if positive else 0.1) if auc_good else 0.5
        found.append(Row("pre_turn", str(i), "proceed" if jev_right else "human", "human",
                         frozenset({"proceed"}), probabilities=(("needs_human", p, positive),),
                         failed=i < failed, latency_ms=latency))
    return found


def test_prereg_insufficient_below_100_labeled():
    assert summarize_site(labeled_rows(99))["prereg"] == {"verdict": INSUFFICIENT, "failed": ["labeled"]}
    excluded = [Row("pre_turn", str(i), "proceed", "proceed", None, "unresolved") for i in range(200)]
    assert summarize_site(excluded)["prereg"]["verdict"] == INSUFFICIENT


def test_prereg_keep_and_each_drop_reason():
    assert summarize_site(labeled_rows(100))["prereg"] == {"verdict": KEEP, "failed": []}
    assert summarize_site(labeled_rows(100, jev_right=False))["prereg"]["failed"] == ["accuracy"]
    assert summarize_site(labeled_rows(100, auc_good=False))["prereg"]["failed"] == ["auc"]
    assert summarize_site(labeled_rows(100, latency=1501))["prereg"]["failed"] == ["latency"]
    assert summarize_site(labeled_rows(100, failed=6))["prereg"]["failed"] == ["failure_rate"]
    summary = summarize_site(labeled_rows(100))
    summary["first_option"]["rate"] = 0.41
    assert prereg_verdict(summary) == {"verdict": DROP, "failed": ["first_option"]}


def test_prereg_thresholds_match_the_doc():
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    s = judge_scorers
    assert f"{s.MIN_LABELED} or more labeled decisions" in text
    assert f"fewer than {s.MIN_LABELED} labeled decisions is \"insufficient\"" in text
    assert f"p < {s.MCNEMAR_ALPHA:.2f}" in text
    assert f"AUC ≥ {s.MIN_AUC:.2f}" in text
    assert f"lower bound > {s.MIN_AUC_CI_LOWER:.2f}" in text
    assert f"p95 latency ≤ {s.MAX_P95_LATENCY_MS / 1000:g} s" in text
    assert f"≤ {s.MAX_FAILURE_RATE:.0%}" in text
    assert f"First-shown-option share ≤ {s.MAX_FIRST_OPTION_SHARE:.0%}" in text
    assert f"{s.FIRST_OPTION_MIN_OPTIONS} or more options" in text
    assert re.search(r"\[the preregistered rule\]\(jev-prereg\.md\)",
                     (DOC.parent / "judge.md").read_text(encoding="utf-8"))
