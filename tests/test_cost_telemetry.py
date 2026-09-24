"""Per-bead cost telemetry rolled up from grind logs (ortus-7wnw).

The logs these tests build are the real thing: the harness marker lines are
copied from `logs/grind-*.log` verbatim and the worker streams are the golden
backend fixtures the tail tests already use, so a change in either shape fails
here rather than silently producing plausible-looking numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.cost import find_grind_logs, parse_grind_log

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _stream(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def _repo(tmp_path: Path) -> Path:
    """A minimal ortus repo: `resolve_repo` insists on a .beads/ workspace."""

    (tmp_path / ".beads").mkdir(exist_ok=True)
    return tmp_path


def _write_log(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _claude_log(tmp_path: Path) -> Path:
    return _write_log(
        tmp_path / "logs" / "grind-20260923-101500.log",
        [
            "[2026-09-23 10:15:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-23 10:15:00] profile: claude/implement "
            "(model=claude-opus-5[1m], effort=high)",
            "[2026-09-23 10:15:00] profile: claude/verify "
            "(model=provider-default, effort=provider-default)",
            "[2026-09-23 10:15:01] iter prep: worker will claim ortus-abcd "
            "via goal-prompt",
            "[2026-09-23 10:15:02] iter 1: goal-prompt ready for ortus-abcd (claude)",
            "[2026-09-23 10:15:03] iter 1: spawning claude (single-issue worker)",
            *_stream("claude-stream-events.jsonl"),
            "[2026-09-23 10:25:03] iter 1: worker closed ortus-abcd "
            "(tasks_completed=1)",
        ],
    )


def _codex_log(tmp_path: Path, stream: str = "codex-exec-events.jsonl") -> Path:
    return _write_log(
        tmp_path / "logs" / "grind-20260923-110000.log",
        [
            "[2026-09-23 11:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=codex; verification=full) ===",
            "[2026-09-23 11:00:00] profile: codex/implement "
            "(model=gpt-5-fake, effort=medium)",
            "[2026-09-23 11:00:01] iter 1: goal-prompt ready for ortus-wxyz (codex)",
            "[2026-09-23 11:00:02] iter 1: spawning codex (single-issue worker)",
            *_stream(stream),
            "[2026-09-23 11:04:02] iter 1: worker closed ortus-wxyz "
            "(tasks_completed=1)",
        ],
    )


def test_claude_log_rollup_reports_every_billing_bucket(tmp_path: Path) -> None:
    """AC-1: Claude's four usage fields become the normalized buckets."""

    run = parse_grind_log(_claude_log(tmp_path))

    assert run.backend == "claude"
    (session,) = run.sessions
    assert session.issue_id == "ortus-abcd"
    assert session.backend == "claude"
    # The fixture's init banner names no model, so the profile override stands
    # and survives verbatim -- a `[1m]` suffix is data here, not markup.
    assert session.model == "claude-opus-5[1m]"
    assert session.effort == "high"
    assert session.session_id == "a7f3c1e8-0b21-4d9a-9c44-2e6f5b8d1077"

    usage = session.usage
    # Claude keeps cache reads and cache writes out of input_tokens, so the
    # uncached bucket is that field unmodified.
    assert usage.uncached_input_tokens == 61
    assert usage.cached_input_tokens == 4893079
    assert usage.cache_write_tokens == 233123
    assert usage.output_tokens == 49017
    assert usage.input_tokens == 61 + 4893079 + 233123
    assert usage.cache_hit_rate == pytest.approx(4893079 / (61 + 4893079 + 233123))
    assert usage.cost_usd == pytest.approx(5.207076)

    assert session.turns == 8
    assert session.wall_seconds == 600.0
    assert session.errors == 1  # the fixture's failed `pytest` tool_result
    assert session.closed is True
    assert session.partial_usage is False
    assert session.incomplete is False


def test_codex_log_rollup_splits_cached_out_of_total_input(tmp_path: Path) -> None:
    """AC-2: the same buckets, from Codex's different inclusion rule."""

    run = parse_grind_log(_codex_log(tmp_path))

    assert run.backend == "codex"
    (session,) = run.sessions
    assert session.issue_id == "ortus-wxyz"
    assert session.backend == "codex"
    assert session.model == "gpt-5-fake"
    assert session.effort == "medium"
    assert session.session_id == "019f7787-14a4-7151-9123-fe6c13abf620"

    usage = session.usage
    # Codex folds the cached tokens into input_tokens, so uncached is the
    # difference -- the number Claude would have reported directly.
    assert usage.uncached_input_tokens == 5800 - 1280
    assert usage.cached_input_tokens == 1280
    assert usage.output_tokens == 230
    assert usage.reasoning_tokens == 80
    assert usage.input_tokens == 5800
    assert usage.cache_hit_rate == pytest.approx(1280 / 5800)
    # Codex reports no price, and nothing here invents one.
    assert usage.cost_usd is None

    assert session.turns == 1
    assert session.errors == 2
    assert session.wall_seconds == 240.0
    assert session.closed is True
    assert session.partial_usage is False


def test_codex_turn_failure_flags_the_session_incomplete(tmp_path: Path) -> None:
    run = parse_grind_log(
        _codex_log(tmp_path, stream="codex-exec-events-failed.jsonl")
    )

    (session,) = run.sessions
    assert session.incomplete is True
    assert session.errors == 8
    assert session.usage.is_empty


def test_opencode_step_finish_rolls_up_the_cache_read_bucket(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path / "logs" / "grind-20260923-120000.log",
        [
            "[2026-09-23 12:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=opencode; verification=full) ===",
            "[2026-09-23 12:00:01] iter 1: goal-prompt ready for ortus-open (opencode)",
            "[2026-09-23 12:00:02] iter 1: spawning opencode (single-issue worker)",
            *_stream("opencode-run-events.jsonl"),
        ],
    )

    (session,) = parse_grind_log(log).sessions
    usage = session.usage
    assert usage.uncached_input_tokens == 19709
    assert usage.cached_input_tokens == 48374
    assert usage.cache_write_tokens == 0
    assert usage.output_tokens == 641
    assert usage.cost_usd == pytest.approx(0.0)
    assert session.turns == 7  # one per step_finish


def test_attribution_splits_one_run_across_the_beads_it_worked(
    tmp_path: Path,
) -> None:
    """AC-3: usage lands on the bead the iteration markers name."""

    first = (
        '{"type":"result","subtype":"success","usage":{"input_tokens":10,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":90,'
        '"output_tokens":5},"num_turns":3}'
    )
    second = (
        '{"type":"result","subtype":"success","usage":{"input_tokens":20,'
        '"cache_creation_input_tokens":0,"cache_read_input_tokens":180,'
        '"output_tokens":7},"num_turns":4}'
    )
    log = _write_log(
        tmp_path / "logs" / "grind-20260923-130000.log",
        [
            "[2026-09-23 13:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-23 13:00:01] iter 1: goal-prompt ready for ortus-one (claude)",
            "[2026-09-23 13:00:02] iter 1: spawning claude (single-issue worker)",
            first,
            "[2026-09-23 13:05:02] iter 1: worker closed ortus-one "
            "(tasks_completed=1)",
            "[2026-09-23 13:05:10] iter 2: goal-prompt ready for ortus-two (claude)",
            "[2026-09-23 13:05:11] iter 2: spawning claude (single-issue worker)",
            second,
            "[2026-09-23 13:09:11] iter 2: worker TIMEOUT after 5400s, killed (rc=143)",
        ],
    )

    run = parse_grind_log(log)
    one, two = run.beads
    assert (one.issue_id, two.issue_id) == ("ortus-one", "ortus-two")
    assert one.usage.output_tokens == 5
    assert one.closed is True and one.incomplete is False
    assert two.usage.output_tokens == 7
    assert two.usage.cached_input_tokens == 180
    # The watchdog killed the second worker, so its usage covers part of a bead.
    assert two.closed is False and two.incomplete is True
    assert two.turns == 4


def test_attribution_is_explicitly_null_without_an_iteration_marker(
    tmp_path: Path,
) -> None:
    """AC-3: the legacy self-selecting worker leaves the bead unknown, not guessed."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260923-140000.log",
        [
            "[2026-09-23 14:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            '{"type":"result","subtype":"success","usage":{"input_tokens":1,'
            '"cache_creation_input_tokens":0,"cache_read_input_tokens":2,'
            '"output_tokens":3},"num_turns":1}',
        ],
    )

    run = parse_grind_log(log)
    (session,) = run.sessions
    assert session.issue_id is None
    assert session.backend == "claude"
    (bead,) = run.beads
    assert bead.issue_id is None
    assert bead.as_dict()["issue_id"] is None


def test_null_usage_fields_stay_null_and_flag_partial(tmp_path: Path) -> None:
    """AC-4: an omitted provider field is unreported, never zero."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260923-150000.log",
        [
            "[2026-09-23 15:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-23 15:00:01] iter 1: goal-prompt ready for ortus-thin (claude)",
            "[2026-09-23 15:00:02] iter 1: spawning claude (single-issue worker)",
            '{"type":"result","subtype":"success","usage":{"input_tokens":12}}',
        ],
    )

    (session,) = parse_grind_log(log).sessions
    usage = session.usage
    assert usage.uncached_input_tokens == 12
    assert usage.output_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.cost_usd is None
    assert usage.cache_hit_rate is None
    assert session.partial_usage is True


def test_null_usage_leaves_an_unsplittable_codex_turn_unreported(
    tmp_path: Path,
) -> None:
    """AC-4: without the cached figure Codex's total cannot be split honestly."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260923-160000.log",
        [
            "[2026-09-23 16:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=codex; verification=full) ===",
            "[2026-09-23 16:00:01] iter 1: goal-prompt ready for ortus-thin (codex)",
            "[2026-09-23 16:00:02] iter 1: spawning codex (single-issue worker)",
            '{"type":"turn.completed","usage":{"input_tokens":900,'
            '"output_tokens":40}}',
        ],
    )

    (session,) = parse_grind_log(log).sessions
    usage = session.usage
    assert usage.output_tokens == 40
    assert usage.uncached_input_tokens is None
    assert usage.cached_input_tokens is None
    assert usage.input_tokens is None
    assert session.partial_usage is True
    assert session.turns == 1


def test_find_grind_logs_returns_the_newest_first(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("grind-20260101-000000.log", "grind-20260202-000000.log"):
        (logs / name).write_text("", encoding="utf-8")
    (logs / "ralph-20260303-000000.log").write_text("", encoding="utf-8")

    assert [p.name for p in find_grind_logs(tmp_path, newest=0)] == [
        "grind-20260202-000000.log",
        "grind-20260101-000000.log",
    ]
    assert len(find_grind_logs(tmp_path)) == 1


def test_cost_cli_emits_the_claude_rollup_as_json(tmp_path: Path) -> None:
    _claude_log(_repo(tmp_path))

    result = runner.invoke(app, ["cost", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    (bead,) = payload["beads"]
    assert bead["issue_id"] == "ortus-abcd"
    assert bead["usage"]["output_tokens"] == 49017
    assert bead["usage"]["cached_input_tokens"] == 4893079
    assert bead["usage"]["cost_usd"] == pytest.approx(5.207076)
    assert bead["backends"] == ["claude"]
    assert bead["models"] == ["claude-opus-5[1m]"]
    assert bead["efforts"] == ["high"]


def test_cost_cli_prints_a_readable_block_per_bead(tmp_path: Path) -> None:
    _claude_log(_repo(tmp_path))

    result = runner.invoke(app, ["cost", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "ortus-abcd" in result.stdout
    assert "49,017" in result.stdout
    # Rich would eat a bare `[1m]`; the report prints model ids literally.
    assert "claude-opus-5[1m]" in result.stdout
    assert "served from cache" in result.stdout


def test_cost_cli_reports_no_logs_instead_of_an_empty_table(tmp_path: Path) -> None:
    result = runner.invoke(app, ["cost", str(_repo(tmp_path))])

    assert result.exit_code == 1
    assert "no grind logs" in result.stderr


def test_cost_verb_is_listed_in_ortus_help() -> None:
    """AC-5: the query surface is discoverable from the top-level help."""

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "cost" in result.stdout
