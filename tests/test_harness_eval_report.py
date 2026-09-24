"""The frozen evaluation set and the report it emits (ortus-4ji7).

The seats these tests build are the real shape: the marker lines are the ones
`ortus grind` writes and the worker streams are the golden backend fixtures
the cost tests already use, so the report is measured from logs rather than
from a hand-written metrics dict. Codex reports no dollars, which is why it
stands in for the missing-telemetry case — the null in the primary metric is
the provider's, not the test's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.harness_eval import (
    ARMS,
    CONTROL_ARM,
    FIXTURE_A,
    FIXTURE_B,
    FIXTURE_PACK,
    GUARDRAIL_METRICS,
    PRIMARY_METRIC,
    REPORT_SCHEMA,
    TREATMENTS,
    arm_commands,
    build_report,
    matrix,
    render_matrix,
    render_report,
    seat_name,
)

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _stream(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _seat_log(root: Path, fixture, arm: str, lines: list[str]) -> Path:
    path = root / seat_name(fixture, arm) / "logs" / "grind-20260924-090000.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _claude_lines() -> list[str]:
    return [
        "[2026-09-24 09:00:00] === ortus grind started (subprocess-per-task "
        "shape; backend=claude; verification=full) ===",
        "[2026-09-24 09:00:00] profile: claude/implement "
        "(model=claude-opus-5[1m], effort=high)",
        "[2026-09-24 09:00:01] iter prep: worker will claim ortus-abcd "
        "via goal-prompt",
        "[2026-09-24 09:00:02] iter 1: goal-prompt ready for ortus-abcd (claude)",
        "[2026-09-24 09:00:03] iter 1: spawning claude (single-issue worker)",
        *_stream("claude-stream-events.jsonl"),
        "[2026-09-24 09:10:03] iter 1: worker closed ortus-abcd (tasks_completed=1)",
    ]


def _codex_lines() -> list[str]:
    return [
        "[2026-09-24 09:20:00] === ortus grind started (subprocess-per-task "
        "shape; backend=codex; verification=full) ===",
        "[2026-09-24 09:20:00] profile: codex/implement "
        "(model=gpt-5-fake, effort=medium)",
        "[2026-09-24 09:20:01] iter 1: goal-prompt ready for ortus-wxyz (codex)",
        "[2026-09-24 09:20:02] iter 1: spawning codex (single-issue worker)",
        *_stream("codex-exec-events.jsonl"),
        "[2026-09-24 09:24:02] iter 1: worker closed ortus-wxyz (tasks_completed=1)",
    ]


def _seeded_root(tmp_path: Path) -> Path:
    """A seat root where the control arms ran and every treatment arm did not."""

    root = tmp_path / "eval"
    _seat_log(root, FIXTURE_A, CONTROL_ARM, _claude_lines())
    _seat_log(root, FIXTURE_B, CONTROL_ARM, _codex_lines())
    return root


def _arm(report, fixture_key: str, arm: str):
    for candidate in report.arms:
        if candidate.fixture == fixture_key and candidate.arm == arm:
            return candidate
    raise AssertionError(f"no {fixture_key}/{arm} row in the report")


def test_fixture_a_is_hello_world_and_ships_its_prd() -> None:
    """AC-1: fixture A is Hello World, and its PRD path resolves."""

    assert FIXTURE_PACK[0] is FIXTURE_A
    assert FIXTURE_A.key == "hello-world"
    assert FIXTURE_A.title == "Hello World"
    assert FIXTURE_A.prd_filename == "Hello_World_PRD.md"
    assert FIXTURE_A.prd_path.name == "Hello_World_PRD.md"
    assert FIXTURE_A.prd_path.is_file()

    text = FIXTURE_A.prd_text()
    assert "Hello, world!" in text
    # The floor fixture stays the floor: a plan that grows past three beads
    # means the PRD drifted, not that the harness got worse.
    assert (FIXTURE_A.min_beads, FIXTURE_A.max_beads) == (1, 3)

    # The recipe hands `ortus plan` the shipped path, not a copy someone made.
    commands = arm_commands(FIXTURE_A, CONTROL_ARM, root=Path("/seats"))
    assert any(str(FIXTURE_A.prd_path) in command for command in commands)


def test_fixture_b_is_a_five_to_ten_bead_prd_beside_hello_world() -> None:
    """AC-2: fixture B is the one-step-richer PRD, in band and in the pack."""

    assert FIXTURE_PACK == (FIXTURE_A, FIXTURE_B)
    assert FIXTURE_B.min_beads == 5
    assert FIXTURE_B.max_beads == 10
    assert FIXTURE_B.prd_path.is_file()
    assert FIXTURE_B.prd_filename != FIXTURE_A.prd_filename


def test_fixture_b_prd_asks_for_tests_and_one_inter_bead_dependency() -> None:
    """AC-2: the two properties fixture B exists to exercise are written down."""

    text = FIXTURE_B.prd_text()
    assert "Two test files" in text
    assert "node --test" in text
    # The dependency edge is the point of the fixture, so the PRD forbids
    # collapsing the three beads it spans.
    assert "must depend on both" in text
    assert "do not collapse" in text.lower()
    assert "Prefer 5 to 8 beads" in text


def test_report_schema_carries_the_primary_metric_and_every_guardrail(
    tmp_path: Path,
) -> None:
    """AC-3: one row per cell, primary metric named, guardrails beside it."""

    report = build_report(_seeded_root(tmp_path))
    payload = report.as_dict()

    assert payload["schema"] == REPORT_SCHEMA
    assert payload["primary_metric"] == PRIMARY_METRIC
    assert payload["guardrails"] == [
        "close_rate",
        "worker_errors",
        "turns",
        "wall_seconds",
        "cache_hit_rate",
    ]
    assert len(payload["arms"]) == len(FIXTURE_PACK) * len(ARMS)

    for row in payload["arms"]:
        assert PRIMARY_METRIC in row
        for guardrail in GUARDRAIL_METRICS:
            assert guardrail in row

    control = _arm(report, FIXTURE_A.key, CONTROL_ARM)
    assert control.closed_beads == 1
    # One closed bead billed at the provider's own number for the window.
    assert control.cost_per_closed_bead == pytest.approx(5.207076)
    assert control.close_rate == pytest.approx(1.0)
    assert control.worker_errors == 1
    assert control.turns == 8
    assert control.wall_seconds == pytest.approx(600.0)
    assert control.cache_hit_rate == pytest.approx(
        4893079 / (61 + 4893079 + 233123)
    )

    rendered = render_report(report)
    assert PRIMARY_METRIC in rendered
    assert FIXTURE_B.key in rendered


def test_report_schema_is_what_the_eval_verb_prints(tmp_path: Path) -> None:
    """AC-3: the CLI is the same report, not a second rendering of it."""

    result = runner.invoke(app, ["eval", str(_seeded_root(tmp_path)), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema"] == REPORT_SCHEMA
    assert payload["primary_metric"] == PRIMARY_METRIC
    assert len(payload["fixtures"]) == 2


def test_null_results_stay_null_rather_than_zero_filled(tmp_path: Path) -> None:
    """AC-4: unreported is a recorded outcome, never a zero."""

    report = build_report(_seeded_root(tmp_path))

    # Codex reports no dollars, so the primary metric is unknown for that
    # arm -- and unknown is not free.
    codex = _arm(report, FIXTURE_B.key, CONTROL_ARM)
    assert codex.closed_beads == 1
    assert codex.cost_per_closed_bead is None
    assert PRIMARY_METRIC in codex.null_metrics
    # Its guardrails did report, so the null is scoped to the missing field.
    assert codex.close_rate == pytest.approx(1.0)
    assert codex.turns == 1

    # An arm nobody ran keeps its row with every metric unset.
    unrun = _arm(report, FIXTURE_A.key, TREATMENTS[0].key)
    assert unrun.beads == 0
    assert unrun.cost_per_closed_bead is None
    assert unrun.close_rate is None
    assert unrun.worker_errors is None
    assert unrun.turns is None
    assert unrun.wall_seconds is None
    assert unrun.cache_hit_rate is None
    assert set(unrun.null_metrics) == {PRIMARY_METRIC, *GUARDRAIL_METRICS}

    payload = report.as_dict()
    roundtripped = json.loads(json.dumps(payload))
    for row in roundtripped["arms"]:
        for metric in row["null_metrics"]:
            assert row[metric] is None

    named = {
        (entry["fixture"], entry["arm"]) for entry in roundtripped["null_results"]
    }
    assert (FIXTURE_B.key, CONTROL_ARM) in named
    assert (FIXTURE_A.key, TREATMENTS[0].key) in named
    assert "null" in render_report(report)


def test_treatments_are_documented_with_one_recipe_each() -> None:
    """AC-5: every treatment arm is a copy-pasteable run, not a description."""

    assert [item.config_key for item in TREATMENTS] == [
        "prompt_audit",
        "stable_prompt_prefix",
        "jev_model_router",
    ]
    assert ARMS == (CONTROL_ARM, *(item.key for item in TREATMENTS))

    cells = matrix(root=Path("/seats"))
    assert len(cells) == len(FIXTURE_PACK) * len(ARMS)

    for item in TREATMENTS:
        for fixture in FIXTURE_PACK:
            commands = arm_commands(fixture, item.key, root=Path("/seats"))
            assert any(command.startswith("ortus init ") for command in commands)
            assert any(item.enable_line in command for command in commands)
            assert any(command.startswith("ortus plan ") for command in commands)
            assert any(command.startswith("ortus grind ") for command in commands)

    # The control arm is the same recipe minus the one config line.
    control = arm_commands(FIXTURE_A, CONTROL_ARM, root=Path("/seats"))
    assert not any(".ortusrc" in command for command in control)

    rendered = render_matrix(cells)
    for item in TREATMENTS:
        assert item.key in rendered
        assert item.enable_line in rendered
        # Each arm names the issue that spends its numbers, so a report
        # reader can get from a row to the decision it feeds.
        assert item.measured_by
        assert item.measured_by in rendered


def test_treatments_render_through_the_eval_verb() -> None:
    """AC-5: the recipe is one command away, with no repo to set up first."""

    result = runner.invoke(app, ["eval", "/seats", "--matrix"])

    assert result.exit_code == 0, result.output
    for item in TREATMENTS:
        assert item.enable_line in result.stdout


def test_pack_holds_no_large_app_fixture() -> None:
    """AC-6: the set stays two small PRDs; a big application is out of scope.

    The guard is structural rather than a name list: the pack is exactly the
    two declared fixtures, both inside the small bead band, and the shipped
    directory holds nothing else that a run could reach for.
    """

    assert len(FIXTURE_PACK) == 2
    for fixture in FIXTURE_PACK:
        assert fixture.max_beads <= 10
        assert fixture.min_beads >= 1
        text = fixture.prd_text()
        assert "## Non-goals" in text
        for excluded in ("database", "Docker", "auth"):
            assert excluded in text
        # A fixture whose PRD runs long has stopped being a small fixture.
        assert len(text) < 4000

    shipped = {path.name for path in FIXTURE_A.prd_path.parent.iterdir()}
    assert {
        fixture.prd_filename for fixture in FIXTURE_PACK
    } == {name for name in shipped if name.endswith(".md")}
