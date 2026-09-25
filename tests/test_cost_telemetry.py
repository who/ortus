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
from ortus.commands.grind import _await_result_line
from ortus.core.cost import (
    COST_ESTIMATED,
    COST_PROVIDER,
    PRICE_TABLE_VERSION,
    UsageBuckets,
    estimate_cost,
    find_grind_logs,
    find_plan_logs,
    model_price,
    parse_grind_log,
    parse_tree,
)

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


def test_grok_usage_rolls_up_with_the_cached_read_outside_the_input(
    tmp_path: Path,
) -> None:
    """AC-1: the golden Grok stream fills the same buckets the others do."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260814-140840.log",
        [
            "[2026-08-14 14:08:38] === ortus grind started (subprocess-per-task "
            "shape; backend=grok; verification=full) ===",
            "[2026-08-14 14:08:39] iter 1: goal-prompt ready for ortus-grok (grok)",
            # The fixture opens with its own spawn marker.
            *_stream("grok-stream-events.jsonl"),
            "[2026-08-14 14:12:40] iter 1: worker closed ortus-grok "
            "(tasks_completed=1)",
        ],
    )

    run = parse_grind_log(log)
    assert run.backend == "grok"
    (session,) = run.sessions
    assert session.issue_id == "ortus-grok"
    usage = session.usage
    assert usage.is_empty is False
    assert usage.uncached_input_tokens == 17031
    assert usage.cached_input_tokens == 896
    assert usage.output_tokens == 620
    assert usage.input_tokens == 17031 + 896
    assert usage.cost_usd is None  # Grok reports no dollars of its own
    assert session.partial_usage is False
    assert session.turns == 1  # one per usage event


def test_repeated_grok_usage_events_sum_rather_than_overwrite(
    tmp_path: Path,
) -> None:
    """Two real consecutive turns, each billing its own slice of the session.

    Both are copied from a Grok run on disk, and both report fewer
    `input_tokens` than `cache_read_input_tokens` — the shape that settles the
    cached read as sitting outside the input count rather than inside it.
    """

    log = _write_log(
        tmp_path / "logs" / "grind-20260814-150000.log",
        [
            "[2026-08-14 15:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=grok; verification=full) ===",
            "[2026-08-14 15:00:01] iter 1: goal-prompt ready for ortus-grok (grok)",
            "[2026-08-14 15:00:02] iter 1: spawning grok (single-issue worker)",
            '{"type":"usage","usage":{"input_tokens":1056,"output_tokens":255,'
            '"cache_read_input_tokens":18432,"cache_creation_input_tokens":0,'
            '"reasoning_tokens":78}}',
            '{"type":"usage","usage":{"input_tokens":462,"output_tokens":170,'
            '"cache_read_input_tokens":27520,"cache_creation_input_tokens":0,'
            '"reasoning_tokens":167}}',
        ],
    )

    (session,) = parse_grind_log(log).sessions
    usage = session.usage
    assert usage.uncached_input_tokens == 1056 + 462
    assert usage.cached_input_tokens == 18432 + 27520
    assert usage.output_tokens == 255 + 170
    assert usage.reasoning_tokens == 78 + 167
    assert usage.cache_write_tokens == 0
    assert session.turns == 2
    assert session.partial_usage is False


def test_grok_usage_without_a_cache_field_leaves_that_bucket_unreported(
    tmp_path: Path,
) -> None:
    """The uncached count still stands; the cache split simply goes unsaid.

    Nothing is inferred either way: because Grok's input count excludes the
    cached read, an event that omits the cache field still states the uncached
    bucket honestly, and the missing split marks the record partial.
    """

    log = _write_log(
        tmp_path / "logs" / "grind-20260814-160000.log",
        [
            "[2026-08-14 16:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=grok; verification=full) ===",
            "[2026-08-14 16:00:01] iter 1: goal-prompt ready for ortus-grok (grok)",
            "[2026-08-14 16:00:02] iter 1: spawning grok (single-issue worker)",
            '{"type":"usage","usage":{"input_tokens":2048,"output_tokens":64}}',
        ],
    )

    (session,) = parse_grind_log(log).sessions
    usage = session.usage
    assert usage.uncached_input_tokens == 2048
    assert usage.output_tokens == 64
    assert usage.cached_input_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.cache_hit_rate is None
    assert session.partial_usage is True


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


def _reaped_log(tmp_path: Path) -> Path:
    """A window grind reaped at the done bar: usage per message, no result line.

    The markers are the ones grind writes when the done bar ends a window, in
    the order it writes them, so the fixture is the log a real reaped worker
    leaves behind rather than a stream with its last line deleted.
    """

    return _write_log(
        tmp_path / "logs" / "grind-20260924-090000.log",
        [
            "[2026-09-24 09:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-24 09:00:00] profile: claude/implement "
            "(model=provider-default, effort=provider-default)",
            "[2026-09-24 09:00:01] iter 1: goal-prompt ready for ortus-reap (claude)",
            "[2026-09-24 09:00:02] iter 1: spawning claude (single-issue worker)",
            *_stream("claude-stream-reaped.jsonl"),
            "[2026-09-24 09:12:00] iter 1: done bar met; result line not written "
            "within 5s",
            "[2026-09-24 09:12:00] iter 1: done bar met (ortus-reap closed, in "
            "sync); reaping worker",
            "[2026-09-24 09:12:01] iter 1: worker closed ortus-reap "
            "(tasks_completed=1)",
        ],
    )


def test_reaped_worker_window_is_billed_from_its_per_message_usage(
    tmp_path: Path,
) -> None:
    """AC-1: a window killed before its result line still reports what it cost.

    The buckets are the per-message readings summed once per message id, and
    the dollars are this module's price table applied to them, labelled as an
    estimate so nobody reads them as a bill.
    """

    (session,) = parse_grind_log(_reaped_log(tmp_path)).sessions

    usage = session.usage
    assert usage.uncached_input_tokens == 10 + 12 + 18
    assert usage.cache_write_tokens == 120000 + 50000 + 30000
    assert usage.cached_input_tokens == 1500000 + 2000000 + 2500000
    assert usage.output_tokens == 60 + 90 + 150
    assert usage.cache_hit_rate == pytest.approx(6000000 / (40 + 6000000 + 200000))

    # The init banner's model is priced through its context marker, and the
    # `<synthetic>` message neither prices the window nor adds to a bucket.
    assert session.model == "claude-opus-5[1m]"
    assert session.cost_source == COST_ESTIMATED
    assert usage.cost_usd == pytest.approx(
        (40 * 5.0 + 200000 * 10.0 + 6000000 * 0.5 + 300 * 25.0) / 1_000_000
    )
    assert session.closed is True
    assert session.partial_usage is False
    # Turn totals only ever come from the result event this window never wrote.
    assert session.turns is None


def test_repeated_usage_for_one_message_in_a_reaped_window_counts_once(
    tmp_path: Path,
) -> None:
    """The CLI repeats a message's usage per content block; a sum would multiply it.

    The fixture's first message reports the same block three times and its
    second reports a growing output count twice, so a summing parser would bill
    roughly twice the input the provider charged for.
    """

    (session,) = parse_grind_log(_reaped_log(tmp_path)).sessions

    naive_sum = 10 * 3 + 12 * 2 + 18
    assert session.usage.uncached_input_tokens == 40 < naive_sum
    # The largest reading of a streaming message is that message's own bill.
    assert session.usage.output_tokens == 60 + 90 + 150


def test_provider_result_totals_win_over_the_per_message_readings(
    tmp_path: Path,
) -> None:
    """AC-2: a window that reported totals keeps them, and is not double counted."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260924-100000.log",
        [
            "[2026-09-24 10:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-24 10:00:01] iter 1: goal-prompt ready for ortus-full (claude)",
            "[2026-09-24 10:00:02] iter 1: spawning claude (single-issue worker)",
            '{"type":"assistant","message":{"id":"msg_01Provider","role":'
            '"assistant","model":"claude-opus-5","content":[],"usage":'
            '{"input_tokens":5,"cache_creation_input_tokens":100,'
            '"cache_read_input_tokens":900,"output_tokens":7}}}',
            '{"type":"result","subtype":"success","usage":{"input_tokens":5,'
            '"cache_creation_input_tokens":100,"cache_read_input_tokens":900,'
            '"output_tokens":420},"total_cost_usd":0.25,"num_turns":3}',
        ],
    )

    (session,) = parse_grind_log(log).sessions

    usage = session.usage
    # Each bucket is the result event's own figure, not it plus the message's.
    assert usage.uncached_input_tokens == 5
    assert usage.cache_write_tokens == 100
    assert usage.cached_input_tokens == 900
    assert usage.output_tokens == 420
    assert usage.cost_usd == pytest.approx(0.25)
    assert session.cost_source == COST_PROVIDER
    assert session.turns == 3


def test_an_unknown_model_leaves_dollars_null_and_flags_the_row_partial(
    tmp_path: Path,
) -> None:
    """AC-3: tokens without a price stay unpriced rather than becoming a guess."""

    log = _write_log(
        tmp_path / "logs" / "grind-20260924-110000.log",
        [
            "[2026-09-24 11:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-24 11:00:01] iter 1: goal-prompt ready for ortus-odd (claude)",
            "[2026-09-24 11:00:02] iter 1: spawning claude (single-issue worker)",
            '{"type":"system","subtype":"init","session_id":"odd-1","model":'
            '"some-unlisted-model-7"}',
            '{"type":"assistant","message":{"id":"msg_01Unlisted","role":'
            '"assistant","model":"some-unlisted-model-7","content":[],"usage":'
            '{"input_tokens":9,"cache_creation_input_tokens":0,'
            '"cache_read_input_tokens":4000,"output_tokens":11}}}',
        ],
    )

    (session,) = parse_grind_log(log).sessions

    assert session.model == "some-unlisted-model-7"
    assert session.usage.cached_input_tokens == 4000  # the tokens are known
    assert session.usage.cost_usd is None  # the price is not
    assert session.cost_source is None
    assert session.partial_usage is True
    assert model_price("some-unlisted-model-7") is None


def _ttl_log(tmp_path: Path) -> Path:
    """A reaped window whose two messages wrote cache under different TTLs."""

    return _write_log(
        tmp_path / "logs" / "grind-20260924-130000.log",
        [
            "[2026-09-24 13:00:00] === ortus grind started (subprocess-per-task "
            "shape; backend=claude; verification=full) ===",
            "[2026-09-24 13:00:00] profile: claude/implement "
            "(model=claude-opus-5, effort=high)",
            "[2026-09-24 13:00:01] iter 1: goal-prompt ready for ortus-ttl (claude)",
            "[2026-09-24 13:00:02] iter 1: spawning claude (single-issue worker)",
            '{"type":"system","subtype":"init","session_id":"ttl-1","model":'
            '"claude-opus-5"}',
            '{"type":"assistant","message":{"id":"msg_01FiveMinuteWrite","role":'
            '"assistant","model":"claude-opus-5","content":[],"usage":'
            '{"input_tokens":10,"cache_creation_input_tokens":100000,'
            '"cache_read_input_tokens":0,"output_tokens":20,"cache_creation":'
            '{"ephemeral_5m_input_tokens":100000,'
            '"ephemeral_1h_input_tokens":0}}}}',
            '{"type":"assistant","message":{"id":"msg_01OneHourWrite","role":'
            '"assistant","model":"claude-opus-5","content":[],"usage":'
            '{"input_tokens":10,"cache_creation_input_tokens":100000,'
            '"cache_read_input_tokens":0,"output_tokens":20,"cache_creation":'
            '{"ephemeral_5m_input_tokens":0,'
            '"ephemeral_1h_input_tokens":100000}}}}',
            "[2026-09-24 13:12:00] iter 1: worker closed ortus-ttl "
            "(tasks_completed=1)",
        ],
    )


def test_cache_write_ttl_is_priced_at_the_rate_it_was_billed(
    tmp_path: Path,
) -> None:
    """AC-1: a five-minute write costs 1.25x input, a one-hour write 2x.

    Both messages wrote the same number of tokens, so the dollars are the only
    thing that can tell the two TTLs apart. Weighting both at the one-hour
    rate, as this table did before the split, overstates the cheaper write by
    60% and says nothing about having done so.
    """

    (session,) = parse_grind_log(_ttl_log(tmp_path)).sessions

    usage = session.usage
    assert usage.cache_write_5m_tokens == 100000
    assert usage.cache_write_1h_tokens == 100000
    # AC-2: the flat bucket every other reader asks for is still the total.
    assert usage.cache_write_tokens == 200000
    assert session.cost_source == COST_ESTIMATED
    assert usage.cost_usd == pytest.approx(
        (20 * 5.0 + 100000 * 6.25 + 100000 * 10.0 + 40 * 25.0) / 1_000_000
    )


def test_cache_write_ttl_left_unstated_keeps_the_one_hour_rate() -> None:
    """A write no `cache_creation` object splits is priced as it always was."""

    unstated = UsageBuckets(cache_write_tokens=1_000_000)
    assert estimate_cost(unstated, "claude-opus-5") == pytest.approx(10.0)

    split = UsageBuckets(
        cache_write_tokens=1_000_000, cache_write_5m_tokens=1_000_000
    )
    assert estimate_cost(split, "claude-opus-5") == pytest.approx(6.25)


def test_the_price_table_reads_a_context_marker_as_the_same_family() -> None:
    """`claude-opus-5[1m]` is one model id for one priced family, not two."""

    assert model_price("claude-opus-5[1m]") == model_price("claude-opus-5")
    assert model_price("<synthetic>") is None
    assert model_price(None) is None
    # An unreported bucket contributes nothing, so an estimate is a floor.
    assert estimate_cost(
        UsageBuckets(output_tokens=1_000_000), "claude-opus-5"
    ) == pytest.approx(25.0)


def _plan_log(tmp_path: Path, *, cost: float = 1.5) -> Path:
    """One planning session: an agent stream with no harness markers at all."""

    return _write_log(
        tmp_path / "logs" / "plan-20260924-080000.log",
        [
            '{"type":"system","subtype":"init","session_id":"plan-1","model":'
            '"claude-opus-5"}',
            '{"type":"result","subtype":"success","usage":{"input_tokens":30,'
            '"cache_creation_input_tokens":200,"cache_read_input_tokens":5000,'
            f'"output_tokens":900}},"total_cost_usd":{cost},"num_turns":2}}',
        ],
    )


def test_tree_rollup_reports_planner_and_worker_cost_per_closed_bead(
    tmp_path: Path,
) -> None:
    """AC-4: the planner's spend is part of what a closed bead cost."""

    _plan_log(tmp_path)
    _claude_log(tmp_path)

    tree = parse_tree(tmp_path, runs=0)

    (planner,) = tree.planner
    assert planner.issue_id is None  # a planning session owns no bead of its own
    assert tree.planner_usd == pytest.approx(1.5)
    assert tree.worker_usd == pytest.approx(5.207076)
    assert tree.total_usd == pytest.approx(1.5 + 5.207076)
    assert tree.closed_beads == 1
    assert tree.usd_per_closed_bead == pytest.approx(1.5 + 5.207076)
    assert tree.source_counts() == {"provider": 2, "estimated": 0, "unpriced": 0}
    assert find_plan_logs(tmp_path) == (tmp_path / "logs" / "plan-20260924-080000.log",)


def test_tree_with_nothing_closed_reports_no_price_per_bead(tmp_path: Path) -> None:
    """AC-4: no closed bead is a missing denominator, never a zero price."""

    _plan_log(tmp_path)
    _reaped_log(tmp_path)
    # The reaped fixture closes its bead; strip that marker to leave nothing closed.
    log = tmp_path / "logs" / "grind-20260924-090000.log"
    log.write_text(
        "\n".join(
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "worker closed" not in line
        )
        + "\n",
        encoding="utf-8",
    )

    tree = parse_tree(tmp_path, runs=0)

    assert tree.closed_beads == 0
    assert tree.total_usd is not None
    assert tree.usd_per_closed_bead is None
    assert tree.source_counts()["estimated"] == 1
    assert tree.as_dict()["price_table_version"] == PRICE_TABLE_VERSION


def test_cost_cli_tree_json_carries_the_whole_tree_rollup(tmp_path: Path) -> None:
    """AC-4: the same numbers, for a program rather than a reader."""

    repo = _repo(tmp_path)
    _plan_log(repo)
    _claude_log(repo)

    result = runner.invoke(app, ["cost", str(repo), "--tree", "--runs", "0", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["closed_beads"] == 1
    assert payload["planner_usd"] == pytest.approx(1.5)
    assert payload["usd_per_closed_bead"] == pytest.approx(1.5 + 5.207076)
    assert payload["cost_sources"]["provider"] == 2
    assert len(payload["planner_sessions"]) == 1


def test_cost_cli_tree_block_prints_an_estimated_basis(tmp_path: Path) -> None:
    """A reader is told which dollars are a bill and which are this table's work."""

    repo = _repo(tmp_path)
    _reaped_log(repo)

    result = runner.invoke(app, ["cost", str(repo), "--tree"])

    assert result.exit_code == 0, result.output
    assert "1 estimated" in result.stdout
    assert PRICE_TABLE_VERSION in result.stdout


def test_cost_cli_tree_refuses_a_single_log_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = _claude_log(repo)

    result = runner.invoke(app, ["cost", str(repo), "--tree", "--log", str(log)])

    assert result.exit_code == 1
    assert "--tree" in result.stderr


def test_grace_returns_as_soon_as_the_result_line_lands(tmp_path: Path) -> None:
    """AC-5: the wait before a done-bar reap ends the moment the line arrives."""

    log = tmp_path / "worker.log"
    log.write_text('{"type":"assistant","message":{"id":"m","usage":{}}}\n', "utf-8")
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write('{"type":"result","subtype":"success","usage":{}}\n')

    assert _await_result_line(
        log,
        start_offset=0,
        grace=5.0,
        poll=0.5,
        clock=lambda: next(clock),
        sleep=_sleep,
    )
    assert slept == [0.5]  # one poll, then the line was there


def test_grace_expires_on_its_constant_when_no_result_line_comes(
    tmp_path: Path,
) -> None:
    """AC-5: the /goal Stop hook usually holds the line; the reap still follows."""

    log = tmp_path / "worker.log"
    log.write_text('{"type":"assistant","message":{"id":"m","usage":{}}}\n', "utf-8")
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    slept: list[float] = []

    assert not _await_result_line(
        log,
        start_offset=0,
        grace=2.0,
        poll=1.0,
        clock=lambda: next(ticks),
        sleep=slept.append,
    )
    # Bounded: the wait cannot outlast the grace it was given.
    assert sum(slept) <= 2.0


def test_grace_ignores_a_result_line_from_an_earlier_window(tmp_path: Path) -> None:
    """AC-5: the offset is the window's own start, so a previous reap cannot count."""

    log = tmp_path / "worker.log"
    earlier = '{"type":"result","subtype":"success","usage":{}}\n'
    log.write_text(earlier, encoding="utf-8")
    ticks = iter([0.0, 1.0, 2.0])

    assert not _await_result_line(
        log,
        start_offset=len(earlier.encode("utf-8")),
        grace=1.0,
        poll=1.0,
        clock=lambda: next(ticks),
        sleep=lambda _seconds: None,
    )
