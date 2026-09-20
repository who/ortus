"""Soft threshold calibration: compare candidates, never apply or escalate them.

The diamond policy these tests pin is that System One's typed probabilities
travel into System Two as context. Calibration reads recorded answers and
reports bands; it cannot restore the scrapped hard escalate, and the pre-tool
floors are out of its reach entirely.
"""
import json

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.judge import (
    FailureMode, GateAction, GateReason, JudgeAnswers, JudgeConfig, JudgeRoute, JudgeState,
)
from ortus.core.judge_calibration import (
    MAX_CANDIDATES, CalibrationError, compare_candidates, fires, read_candidates,
)
from ortus.core.judge_policy import decide_pre_turn
from ortus.core.judge_replay import join_events, read_events, recorded_action, summarize
from ortus.core.judge_typesafe import JudgeVerdict

FIXTURE = "tests/fixtures/jev/labeled_replay.jsonl"
BANDS = """
[[candidate]]
name = "route-band"
action = "flag"
route_confidence_min = 0.8

[[candidate]]
name = "human-band"
action = "log"
needs_human_max = 0.8

[[candidate]]
name = "risk-band"
action = "rewrite"
risk_confidence_min = 0.8
action_risk_max = 1.0
"""


@pytest.fixture
def replay(request):
    """Pilot events plus the operator labels their issue ids stand for."""
    path = request.config.rootpath / FIXTURE
    data = join_events(read_events(path))
    labels = {}
    for decision, _ in data.pairs:
        human = decision["issue_id"].startswith("calib-human")
        labels[decision["decision_id"]] = {
            "expected_action": "human" if human else recorded_action(decision),
            "needs_human": human,
            "worker_cost_usd": 0.42,
        }
    return data, labels


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def report(tmp_path, replay, text=BANDS, **kwargs):
    data, labels = replay
    return compare_candidates(data, labels, read_candidates(write(tmp_path, "c.toml", text)), **kwargs)


def by_name(result):
    return {candidate["name"]: candidate for candidate in result["candidates"]}


def test_candidates_score_recorded_answers_without_changing_them(tmp_path, replay):
    result = report(tmp_path, replay)
    assert (result["labeled"], result["scored"], result["unanswered"]) == (8, 7, 1)
    assert (result["needs_human_labels"], result["proceed_labels"]) == (3, 4)
    assert result["recorded_actions"] == {"proceed": 6, "human": 0, "skip": 1}
    assert result["applies_settings"] is False
    candidates = by_name(result)
    assert candidates["route-band"]["over_caution"] == {"count": 1, "denominator": 4, "rate": 0.25}
    assert candidates["route-band"]["caught_needs_human"]["count"] == 0
    assert candidates["human-band"]["caught_needs_human"]["count"] == 1
    assert candidates["risk-band"]["caught_needs_human"]["count"] == 1
    # One labeled needs-human vector sits inside every band: soft comparison
    # surfaces what it misses instead of widening into a claim stop.
    assert all(c["missed_needs_human"]["count"] >= 1 for c in candidates.values())
    assert all(c["blocks_claims"] is False and c["action_changes"] == 0 for c in candidates.values())


def test_comparison_is_bounded_and_reproducible(tmp_path, replay):
    data, labels = replay
    many = "".join(
        f'[[candidate]]\nname = "band-{n}"\naction = "log"\nneeds_human_max = 0.{n:02d}\n'
        for n in range(1, MAX_CANDIDATES + 1)
    )
    assert len(read_candidates(write(tmp_path, "c.toml", many))) == MAX_CANDIDATES
    assert report(tmp_path, replay, many) == report(tmp_path, replay, many)
    over = many + '[[candidate]]\nname = "band-21"\naction = "log"\nneeds_human_max = 0.9\n'
    with pytest.raises(CalibrationError, match="at most 20"):
        read_candidates(write(tmp_path, "c.toml", over))
    with pytest.raises(CalibrationError, match="only"):
        read_candidates(write(tmp_path, "c.toml", "[other]\nname = 'x'\n"))
    with pytest.raises(CalibrationError, match="declares no"):
        read_candidates(write(tmp_path, "c.toml", "candidate = []\n"))


@pytest.mark.parametrize("table", [
    'name = "x"\naction = "human"\nneeds_human_max = 0.8',
    'name = "x"\naction = "escalate"\nneeds_human_max = 0.8',
    'name = "x"\naction = "log"\nneeds_human_max = 0.8\nescalate = true',
    'name = "x"\naction = "log"\nneeds_human_max = 0.8\nlow_confidence = "human"',
    'name = "x"\naction = "log"\nneeds_human_max = 0.8\neffective_action = "human"',
    'name = "x"\naction = "log"\nneeds_human_max = 0.8\nblocks_claims = true',
])
def test_hard_escalate_candidates_are_refused(tmp_path, table):
    """A candidate that names an enforcement outcome is a rejected document."""
    with pytest.raises(CalibrationError):
        read_candidates(write(tmp_path, "c.toml", f"[[candidate]]\n{table}\n"))


def test_legacy_threshold_names_become_soft_bands(tmp_path, replay):
    legacy = (
        '[[candidate]]\nname = "legacy"\naction = "flag"\n'
        "human_threshold = 0.8\nrisk_threshold = 1.0\nroute_confidence = 0.8\n"
    )
    candidate = read_candidates(write(tmp_path, "c.toml", legacy))[0]
    assert dict(candidate.bounds) == {
        "action_risk_max": 1.0, "needs_human_max": 0.8, "route_confidence_min": 0.8,
    }
    assert candidate.action == "flag"
    result = by_name(report(tmp_path, replay, legacy))["legacy"]
    assert result["blocks_claims"] is False and result["fired"]["count"] == 3


def test_low_risk_confidence_vector_still_proceeds(tmp_path, replay):
    """AC-2: an unsure risk answer is logged, not turned into effective_action human."""
    answers = JudgeAnswers(JudgeRoute.CODEX, 0.97, 0.05, 0.95, 2, 0.3)
    state = JudgeState(issue_id="calib-1", backends_available=(JudgeRoute.CLAUDE, JudgeRoute.CODEX))
    for failure_mode in (FailureMode.OPEN, FailureMode.CLOSED):
        decision = decide_pre_turn(
            JudgeConfig(enabled=True, failure_mode=failure_mode), state,
            JudgeVerdict(answers=answers), baseline_backend=JudgeRoute.CLAUDE,
        )
        assert decision.action is GateAction.PROCEED
        assert decision.reason is GateReason.ROUTED
    band = '[[candidate]]\nname = "unsure-risk"\naction = "flag"\nrisk_confidence_min = 0.8\n'
    result = report(tmp_path, replay, band)
    assert by_name(result)["unsure-risk"]["fired"]["count"] == 1
    # The action distribution is read from the log, so supplying a band that
    # fires on the same vector leaves it exactly where the no-band report had it.
    assert result["recorded_actions"] == report(tmp_path, replay)["recorded_actions"]
    assert fires(read_candidates(write(tmp_path, "c.toml", band))[0], {
        "route_confidence": 0.97, "needs_human": 0.05, "noul_confidence": 0.95,
        "action_risk": 2, "risk_confidence": 0.3,
    })


def test_unlabeled_replay_fails_closed(replay, tmp_path):
    data, _ = replay
    candidates = read_candidates(write(tmp_path, "c.toml", BANDS))
    with pytest.raises(CalibrationError, match="no labeled decision"):
        compare_candidates(data, {}, candidates)


def test_production_soft_settings_travel_with_the_report(tmp_path, replay):
    settings = report(tmp_path, replay, config=JudgeConfig(risk_confidence=0.9))
    assert settings["production_soft_settings"]["risk_confidence"] == 0.9
    assert settings["production_soft_settings"]["enforced_pre_turn"] is False
    assert settings["production_soft_settings"]["low_confidence"] == "human"


def test_pilot_latency_bars_are_measurable_from_replay(replay):
    """The runbook's p50 and p95 bars read from the same offline metrics."""
    data, labels = replay
    metrics = summarize(data, labels)
    assert metrics["latency_ms"]["p50"] < 500 and metrics["latency_ms"]["p95"] < 1500
    assert metrics["measured_cost_usd"]["worker"] is not None
    assert metrics["measured_cost_usd"]["judge"] is None


def test_cli_reports_candidates_and_writes_nothing_else(tmp_path, request):
    events = (request.config.rootpath / FIXTURE).read_text(encoding="utf-8")
    source = write(tmp_path, "events.jsonl", events)
    labels = {}
    for line in events.splitlines():
        event = json.loads(line)
        if event["event"] == "decision":
            labels[event["decision_id"]] = {
                "expected_action": "proceed", "needs_human": "human" in event["issue_id"],
            }
    write(tmp_path, "labels.json", json.dumps(labels))
    write(tmp_path, "candidates.toml", BANDS)
    config = write(tmp_path, ".ortusrc", '[judge]\nenabled = true\nrisk_confidence = 0.8\n')
    before = config.read_bytes()
    result = CliRunner().invoke(app, [
        "judge", "replay", str(source), "--labels", str(tmp_path / "labels.json"),
        "--output", str(tmp_path / "metrics.json"), "--thresholds", str(tmp_path / "candidates.toml"),
    ])
    assert result.exit_code == 0, result.output
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["calibration"]["policy"].endswith("no hard escalate")
    assert [c["name"] for c in metrics["calibration"]["candidates"]] == [
        "route-band", "human-band", "risk-band",
    ]
    assert config.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".ortusrc", "candidates.toml", "events.jsonl", "labels.json", "metrics.json",
    ]


def test_cli_rejects_a_hard_escalate_document_before_writing(tmp_path, request):
    source = write(tmp_path, "events.jsonl",
                   (request.config.rootpath / FIXTURE).read_text(encoding="utf-8"))
    write(tmp_path, "labels.json", "{}")
    write(tmp_path, "candidates.toml",
          '[[candidate]]\nname = "hard"\naction = "human"\nneeds_human_max = 0.8\n')
    result = CliRunner().invoke(app, [
        "judge", "replay", str(source), "--labels", str(tmp_path / "labels.json"),
        "--output", str(tmp_path / "metrics.json"), "--thresholds", str(tmp_path / "candidates.toml"),
    ])
    assert result.exit_code == 1
    assert not (tmp_path / "metrics.json").exists()
