"""The frozen evaluation set, the report it emits, and the sweep that fills it.

The seats these tests build are the real shape: the marker lines are the ones
`ortus grind` writes and the worker streams are the golden backend fixtures
the cost tests already use, so the report is measured from logs rather than
from a hand-written metrics dict. Codex reports no dollars, which is why it
stands in for the missing-telemetry case — the null in the primary metric is
the provider's, not the test's.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.grind import _done_bar_met
from ortus.core.config import DEFAULTS
from ortus.core.harness_eval import (
    ADOPT,
    ARMS,
    CELL_FAILED,
    CELL_RAN,
    CELL_SKIPPED,
    CONTROL_ARM,
    FIXTURE_A,
    FIXTURE_B,
    FIXTURE_PACK,
    GUARDRAIL_METRICS,
    KEEP_CONTROL,
    PRIMARY_METRIC,
    REPORT_SCHEMA,
    TREATMENTS,
    UNMEASURED,
    arm_commands,
    build_report,
    matrix,
    render_matrix,
    render_report,
    run_matrix,
    seat_name,
    shell_executor,
)
from ortus.core import harness_eval
from ortus.core.git import GitClient
from ortus.core.judge_log import ROUTE_LOG_NAME

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()

#: The arm whose beads carry a tier, named through the treatment rather than
#: by position so a reordered pack cannot silently point these tests elsewhere.
ROUTER_ARM = next(
    item.key for item in TREATMENTS if item.config_key == "jev_model_router"
)


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


def _record(records, fixture_key: str, arm: str):
    for candidate in records:
        if candidate.fixture == fixture_key and candidate.arm == arm:
            return candidate
    raise AssertionError(f"no {fixture_key}/{arm} record in the sweep")


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

    # The control arm is the same recipe with every treatment pinned off.
    control = arm_commands(FIXTURE_A, CONTROL_ARM, root=Path("/seats"))
    assert any(".ortusrc" in command for command in control)
    for item in TREATMENTS:
        assert not any(item.enable_line in command for command in control)

    rendered = render_matrix(cells)
    for item in TREATMENTS:
        assert item.key in rendered
        assert item.enable_line in rendered
        # Each arm names the issue that spends its numbers, so a report
        # reader can get from a row to the decision it feeds.
        assert item.measured_by
        assert item.measured_by in rendered


def _appended_lines(fixture, arm: str) -> list[str]:
    """The `.ortusrc` lines one cell appends, in the order it writes them."""

    command = next(
        item
        for item in arm_commands(fixture, arm, root=Path("/seats"))
        if ".ortusrc" in item
    )
    body = command.split("printf '%s\\n' ", 1)[1].split(" >> ", 1)[0]
    return body.strip("'").split("' '")


def test_control_cell_pins_every_treatment_key_off() -> None:
    """AC-1: the control states its configuration instead of inheriting it."""

    lines = _appended_lines(FIXTURE_A, CONTROL_ARM)

    assert lines == [f"{item.config_key} = false" for item in TREATMENTS]
    for item in TREATMENTS:
        assert item.enable_line not in lines


def test_adopted_default_arm_is_not_a_second_control_arm() -> None:
    """AC-2: an arm whose key now ships on still differs from the control."""

    adopted = [item for item in TREATMENTS if DEFAULTS.get(item.config_key) is True]
    assert adopted, "the collapse this guards against needs an adopted default"

    control = _appended_lines(FIXTURE_A, CONTROL_ARM)
    for item in adopted:
        lines = _appended_lines(FIXTURE_A, item.key)

        assert lines != control
        assert item.enable_line in lines
        # The arm's key is the whole difference: it is the one pin the
        # control writes off that this cell does not.
        assert set(control) - set(lines) == {f"{item.config_key} = false"}

        pinned = []
        for line in lines:
            if line.startswith("["):
                break
            pinned.append(line.split("=", 1)[0].strip())
        # TOML rejects a repeated key outright, so a pin cannot be written
        # twice and left to the later line to win.
        assert len(pinned) == len(set(pinned))
        assert set(pinned) == {other.config_key for other in TREATMENTS}


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


def _recorder(failing: str | None = None, status: int = 2):
    """An executor that records its commands and can fail exactly one of them."""

    calls: list[tuple[str, float | None]] = []

    def execute(command: str, timeout: float | None = None) -> int:
        calls.append((command, timeout))
        return status if command == failing else 0

    return calls, execute


def _silent(message: str) -> None:
    """Swallow the sweep's narration in tests that assert on records instead."""


def test_run_executes_each_cell_in_matrix_order(tmp_path: Path) -> None:
    """AC-1: the sweep issues the recipe's own commands, cell by cell, in order."""

    root = tmp_path / "eval"
    cells = matrix(root=root)
    calls, execute = _recorder()
    notes: list[str] = []

    records = run_matrix(
        root, executor=execute, timeout=None, announce=notes.append
    )

    # The very strings `--matrix` prints, not a second argv built beside them.
    assert [command for command, _ in calls] == [
        command for cell in cells for command in cell.commands
    ]
    assert [(record.fixture, record.arm) for record in records] == [
        (cell.fixture, cell.arm) for cell in cells
    ]
    assert [record.status for record in records] == [CELL_RAN] * len(cells)
    assert all(record.exit_status == 0 for record in records)
    assert all(record.failed_command is None for record in records)

    # A long sweep has to look alive: every cell names itself on the way past.
    for cell in cells:
        assert any(f"{cell.fixture}/{cell.arm}" in note for note in notes)


def test_run_resumes_by_skipping_a_seat_that_already_has_a_log(
    tmp_path: Path,
) -> None:
    """AC-2: a finished arm is not paid for twice when a sweep is resumed."""

    root = tmp_path / "eval"
    _seat_log(root, FIXTURE_A, CONTROL_ARM, _claude_lines())
    calls, execute = _recorder()

    records = run_matrix(root, executor=execute, timeout=None, announce=_silent)

    resumed = _record(records, FIXTURE_A.key, CONTROL_ARM)
    assert resumed.status == CELL_SKIPPED
    assert resumed.exit_status is None
    assert resumed.ok

    seat = seat_name(FIXTURE_A, CONTROL_ARM)
    assert not any(seat in command for command, _ in calls)
    assert [command for command, _ in calls] == [
        command
        for cell in matrix(root=root)
        if cell.seat != seat
        for command in cell.commands
    ]
    assert [record.status for record in records].count(CELL_SKIPPED) == 1


def test_run_cell_failure_is_recorded_and_later_cells_still_run(
    tmp_path: Path,
) -> None:
    """AC-3: one broken cell costs its own remaining commands, not the sweep."""

    root = tmp_path / "eval"
    cells = matrix(root=root)
    doomed = cells[0]
    # The plan command, so the grind that follows it inside the same cell is
    # the thing that must not run.
    failing = doomed.commands[-2]
    calls, execute = _recorder(failing=failing, status=2)

    records = run_matrix(root, executor=execute, timeout=None, announce=_silent)

    failed = _record(records, doomed.fixture, doomed.arm)
    assert failed.status == CELL_FAILED
    assert failed.exit_status == 2
    assert failed.failed_command == failing
    assert not failed.ok

    issued = [command for command, _ in calls]
    assert doomed.commands[-1] not in issued
    assert [record.status for record in records[1:]] == [CELL_RAN] * (
        len(cells) - 1
    )
    for cell in cells[1:]:
        for command in cell.commands:
            assert command in issued


def test_run_emits_report_when_the_sweep_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the sweep ends in the unchanged report, with its records beside it."""

    root = tmp_path / "eval"

    def execute(command: str, timeout: float | None = None) -> int:
        # Stand in for a backend: the grind of each cell leaves the log the
        # report is measured from.
        if command.startswith("ortus grind "):
            seat = Path(command.split()[2])
            log = seat / "logs" / "grind-20260924-090000.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("\n".join(_claude_lines()) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("ortus.commands.eval._make_executor", lambda: execute)

    result = runner.invoke(app, ["eval", str(root), "--run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema"] == REPORT_SCHEMA
    assert payload["primary_metric"] == PRIMARY_METRIC
    assert payload["guardrails"] == list(GUARDRAIL_METRICS)
    assert len(payload["arms"]) == len(FIXTURE_PACK) * len(ARMS)
    assert payload["null_results"] == []
    for row in payload["arms"]:
        assert row[PRIMARY_METRIC] is not None

    assert len(payload["executions"]) == len(FIXTURE_PACK) * len(ARMS)
    assert {row["status"] for row in payload["executions"]} == {CELL_RAN}

    # Re-running the same root reports every cell as already run, and the
    # markdown rendering carries that verdict next to the metrics.
    again = runner.invoke(app, ["eval", str(root), "--run"])
    assert again.exit_code == 0, again.output
    assert "status" in again.stdout.splitlines()[5]
    assert again.stdout.count(CELL_SKIPPED) == len(FIXTURE_PACK) * len(ARMS)


def _routed_lines() -> list[str]:
    """One run over two beads: the first closes, the second never does."""

    return [
        "[2026-09-24 11:00:00] === ortus grind started (subprocess-per-task "
        "shape; backend=claude; verification=full) ===",
        "[2026-09-24 11:00:01] iter prep: worker will claim ortus-abcd "
        "via goal-prompt",
        "[2026-09-24 11:00:02] iter 1: spawning claude (single-issue worker)",
        "[2026-09-24 11:05:02] iter 1: worker closed ortus-abcd "
        "(tasks_completed=1)",
        "[2026-09-24 11:05:03] iter prep: worker will claim ortus-efgh "
        "via goal-prompt",
        "[2026-09-24 11:05:04] iter 2: spawning claude (single-issue worker)",
        "[2026-09-24 11:12:04] iter 2: worker TIMEOUT after 420s",
    ]


def _seat_routes(root: Path, fixture, arm: str, records: list[str]) -> Path:
    """Write one cell's routing log the way a worker launch appends to it."""

    path = root / seat_name(fixture, arm) / "logs" / ROUTE_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


def _route(issue_id: str, tier: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "event": "model_route",
            "seat": "eval",
            "issue_id": issue_id,
            "backend": "claude",
            "tier": tier,
            "reason": "mapped",
            "model": "claude-opus-5[1m]",
            "reasoning_effort": "high",
        }
    )


def _routed_root(tmp_path: Path) -> Path:
    """A seat root whose router arm ran two beads on two different tiers."""

    root = _seeded_root(tmp_path)
    _seat_log(root, FIXTURE_A, ROUTER_ARM, _routed_lines())
    _seat_routes(
        root,
        FIXTURE_A,
        ROUTER_ARM,
        [
            _route("ortus-abcd", "cheap"),
            # The bead was re-routed on its second window; the tier that ran
            # it last owns its outcome.
            _route("ortus-efgh", "baseline"),
            _route("ortus-efgh", "frontier"),
            "{ this line was half-written when a worker was killed",
        ],
    )
    return root


def test_per_tier_close_rate_joins_the_routing_log_to_bead_outcomes(
    tmp_path: Path,
) -> None:
    """Which tier closed which bead, so a threshold is rewritten from outcomes."""

    report = build_report(_routed_root(tmp_path))
    routed = _arm(report, FIXTURE_A.key, ROUTER_ARM)

    # The cell's own numbers are unchanged by the join: two beads, one closed.
    assert (routed.beads, routed.closed_beads) == (2, 1)
    assert routed.close_rate == pytest.approx(0.5)

    # Rows come back weakest model first, and a tier nothing routed to has
    # no row at all.
    assert [row.tier for row in routed.tiers] == ["cheap", "frontier"]

    cheap, frontier = routed.tiers
    assert (cheap.beads, cheap.closed_beads) == (1, 1)
    assert cheap.close_rate == pytest.approx(1.0)
    # The tier that closed nothing is where the threshold is wrong, and a
    # zero close rate is a measurement rather than a missing one.
    assert (frontier.beads, frontier.closed_beads) == (1, 0)
    assert frontier.close_rate == pytest.approx(0.0)

    rendered = render_report(report)
    assert "## Per-tier close rate" in rendered
    assert f"{FIXTURE_A.key} / {ROUTER_ARM} / cheap: 1/1 closed" in rendered
    assert f"{FIXTURE_A.key} / {ROUTER_ARM} / frontier: 0/1 closed" in rendered

    payload = json.loads(json.dumps(report.as_dict()))
    rows = [
        row
        for row in payload["arms"]
        if row["fixture"] == FIXTURE_A.key and row["arm"] == ROUTER_ARM
    ]
    assert rows[0]["tiers"] == [
        {"tier": "cheap", "beads": 1, "closed_beads": 1, "close_rate": 1.0},
        {"tier": "frontier", "beads": 1, "closed_beads": 0, "close_rate": 0.0},
    ]


def test_a_cell_that_routed_nothing_reports_no_tier_rather_than_a_zero(
    tmp_path: Path,
) -> None:
    """An unrouted arm is not a tier that closed nothing."""

    report = build_report(_seeded_root(tmp_path))

    # The control arm ran a bead and closed it, and still has no tier: its
    # seat never recorded a routing decision.
    control = _arm(report, FIXTURE_A.key, CONTROL_ARM)
    assert control.closed_beads == 1
    assert control.tiers == ()
    assert render_report(report).count("No cell recorded a routing decision.") == 1

    payload = json.loads(json.dumps(report.as_dict()))
    assert all(row["tiers"] == [] for row in payload["arms"])


def test_the_tier_breakdown_reaches_the_eval_verb(tmp_path: Path) -> None:
    """The operator reads the breakdown from the CLI, not from a library call."""

    result = runner.invoke(app, ["eval", str(_routed_root(tmp_path)), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    tiers = {
        (row["fixture"], row["arm"]): row["tiers"] for row in payload["arms"]
    }
    assert [row["tier"] for row in tiers[(FIXTURE_A.key, ROUTER_ARM)]] == [
        "cheap",
        "frontier",
    ]


def _router_seat(root: Path, lines: list[str]) -> None:
    """Give fixture A's router arm a cell of its own, with its tier recorded."""

    _seat_log(root, FIXTURE_A, ROUTER_ARM, lines)
    _seat_routes(root, FIXTURE_A, ROUTER_ARM, [_route("ortus-abcd", "cheap")])


def test_model_router_is_read_against_the_control_of_its_own_fixture(
    tmp_path: Path,
) -> None:
    """The comparison a default gets flipped on, computed rather than eyeballed."""

    root = _seeded_root(tmp_path)
    # Same beads, same spend, half the wall clock: the router arm wins on the
    # one guardrail it moved and matched the control on the rest.
    lines = _claude_lines()
    lines[-1] = lines[-1].replace("09:10:03", "09:05:03")
    _router_seat(root, lines)

    report = build_report(root)
    control = _arm(report, FIXTURE_A.key, CONTROL_ARM)
    routed = _arm(report, FIXTURE_A.key, ROUTER_ARM)
    comparison = _record(report.comparisons, FIXTURE_A.key, ROUTER_ARM)

    assert comparison.control_cost_per_closed_bead == control.cost_per_closed_bead
    assert comparison.arm_cost_per_closed_bead == routed.cost_per_closed_bead
    assert comparison.control_close_rate == comparison.arm_close_rate == 1.0
    assert comparison.arm_wall_seconds < comparison.control_wall_seconds
    assert comparison.faster and not comparison.cheaper
    assert comparison.verdict == ADOPT

    rendered = render_report(report)
    assert "## Control versus treatment" in rendered
    assert f"{FIXTURE_A.key} / {ROUTER_ARM}: {PRIMARY_METRIC} " in rendered
    assert f"{ROUTER_ARM}: {PRIMARY_METRIC} " in rendered
    assert f"— {ADOPT}" in rendered


def test_model_router_that_closed_less_loses_however_cheap_it_was(
    tmp_path: Path,
) -> None:
    """Close rate is the guardrail: a cheaper arm that closes fewer beads lost."""

    root = _seeded_root(tmp_path)
    _router_seat(root, _routed_lines())

    comparison = _record(
        build_report(root).comparisons, FIXTURE_A.key, ROUTER_ARM
    )

    assert comparison.arm_close_rate == pytest.approx(0.5)
    assert comparison.control_close_rate == pytest.approx(1.0)
    assert comparison.verdict == KEEP_CONTROL


def test_model_router_without_a_cell_reports_unmeasured_not_a_loss(
    tmp_path: Path,
) -> None:
    """An arm that never ran has no verdict; a missing number is not a zero."""

    comparison = _record(
        build_report(_seeded_root(tmp_path)).comparisons, FIXTURE_A.key, ROUTER_ARM
    )

    assert comparison.arm_close_rate is None
    assert comparison.verdict == UNMEASURED
    assert not comparison.cheaper and not comparison.faster


def test_model_router_comparison_reaches_the_eval_verb(tmp_path: Path) -> None:
    """The operator reads the verdict from the CLI, not from a library call."""

    root = _seeded_root(tmp_path)
    _router_seat(root, _routed_lines())

    result = runner.invoke(app, ["eval", str(root), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    rows = {(row["fixture"], row["arm"]): row for row in payload["comparisons"]}
    assert rows[(FIXTURE_A.key, ROUTER_ARM)]["verdict"] == KEEP_CONTROL
    assert set(rows) == {
        (fixture.key, item.key) for fixture in FIXTURE_PACK for item in TREATMENTS
    }


def test_model_router_arm_enables_the_gate_it_is_routed_from(tmp_path: Path) -> None:
    """The flag alone leaves the arm running the control's configuration."""

    item = next(t for t in TREATMENTS if t.key == ROUTER_ARM)
    commands = arm_commands(FIXTURE_A, ROUTER_ARM, root=Path("/seats"))
    appended = next(command for command in commands if ".ortusrc" in command)

    for line in ("jev_model_router = true", "[judge]", "enabled = true"):
        assert line in appended
    # The flag is a top-level key, so it has to be written above the table
    # header or TOML swallows it into `[judge]`.
    assert appended.index("jev_model_router") < appended.index("[judge]")
    assert item.config_lines[0] == item.enable_line

    rendered = render_matrix(matrix(root=Path("/seats")))
    assert "jev_model_router = true" in rendered


def _origin_setup(commands: tuple[str, ...]) -> list[str]:
    """The commands between the `.ortusrc` pins and `ortus plan`."""

    pins = next(i for i, c in enumerate(commands) if ".ortusrc" in c)
    plan = next(i for i, c in enumerate(commands) if c.startswith("ortus plan "))
    return list(commands[pins + 1 : plan])


def test_arm_commands_push_a_baseline_to_a_seat_origin_before_plan() -> None:
    """AC-1: every arm commits, gets a bare origin, and pushes before planning."""

    for fixture in FIXTURE_PACK:
        for arm in ARMS:
            commands = arm_commands(fixture, arm, root=Path("/seats"))
            seat = Path("/seats") / seat_name(fixture, arm)
            origin = f"{seat}.origin.git"
            setup = _origin_setup(commands)

            assert setup == [
                f"git -C {seat} add -A",
                f"git -C {seat} commit -q -m 'ortus eval: seat baseline'",
                f"test ! -e {origin} || {{ echo 'stale eval origin {origin}: "
                "remove it before re-running this cell' >&2; exit 1; }",
                f"git init -q --bare {origin}",
                f"git -C {seat} remote add origin {origin}",
                f"git -C {seat} push -q -u origin main",
            ]
            # Nothing in the setup hard-codes who authored the baseline.
            assert not any("--author" in c or "user.name" in c for c in setup)


class _ClosedOneBd:
    def count_by_status(self, status: str) -> int:
        assert status == "closed"
        return 1


def _init_like_seat(seat: Path) -> None:
    """A committed repo with the dirty paths `ortus init` leaves behind."""

    seat.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seat)], check=True)
    (seat / "AGENTS.md").write_text("tracker\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seat), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(seat), "commit", "-q", "-m", "bd init"], check=True
    )
    (seat / "AGENTS.md").write_text("tracker\nortus block\n", encoding="utf-8")
    (seat / ".gitignore").write_text("logs/\n", encoding="utf-8")


def test_eval_seat_done_bar_fires_once_the_recipe_built_its_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a recipe-built seat is in sync and clean, so the bar can fire."""

    for key, value in {
        "GIT_AUTHOR_NAME": "eval",
        "GIT_AUTHOR_EMAIL": "eval@example.invalid",
        "GIT_COMMITTER_NAME": "eval",
        "GIT_COMMITTER_EMAIL": "eval@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "seats"
    seat = root / seat_name(FIXTURE_A, CONTROL_ARM)
    _init_like_seat(seat)
    commands = arm_commands(FIXTURE_A, CONTROL_ARM, root=root)

    for command in commands:
        if command.startswith("ortus "):
            continue
        assert shell_executor(command) == 0, command

    git = GitClient(seat)
    assert git.remote_tip("main")
    assert git.dirty_paths() == frozenset()
    assert _done_bar_met(_ClosedOneBd(), git, 0, "main") == "closed 0->1"

    # A second build over the same seat root refuses the stale origin.
    stale = next(c for c in commands if c.startswith("test ! -e "))
    assert shell_executor(stale) == 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        return stat.read_text().split(") ", 1)[1][:1] != "Z"
    except (OSError, IndexError):
        return True


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("ignores_term", [False, True])
def test_shell_executor_timeout_kills_the_whole_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignores_term: bool
) -> None:
    """AC-3: a timed-out cell leaves no child of its command alive."""

    monkeypatch.setattr(harness_eval, "KILL_GRACE_SECONDS", 0.3)
    pidfile = tmp_path / "child.pid"
    trap = "trap '' TERM; " if ignores_term else ""
    command = f"sh -c \"{trap}sleep 60\" & echo $! > {pidfile}; wait"

    status = shell_executor(command, timeout=0.5)

    assert status == harness_eval.TIMEOUT_STATUS
    child = int(pidfile.read_text().strip())
    assert _wait_dead(child), f"child {child} survived the cell timeout"


def test_shell_executor_keeps_a_failing_commands_exit_status() -> None:
    """A non-timeout failure still reports the command's own status."""

    assert shell_executor("exit 3", timeout=10) == 3
    assert shell_executor("true") == 0
