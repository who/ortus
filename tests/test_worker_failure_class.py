"""The closed worker-failure taxonomy and its rates (ortus-jlxt).

Every snippet below is the shape that actually reaches a grind log: Codex
item events carrying a failed command's output, error events carrying a
provider's own words, and the harness's own timestamped marker lines. The
point of the taxonomy is that those disparate shapes reduce to one small set
of names, so the tests assert the name rather than the pattern that found it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ortus.core.cost import failure_rates, parse_grind_log
from ortus.core.worker_failure import (
    WorkerFailure,
    classify_worker_failure,
    classify_worker_window,
    failure_log_line,
)

FIXTURES = Path(__file__).parent / "fixtures"

_EPERM = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "id": "item_4",
            "type": "command_execution",
            "command": "bash -lc 'uv run pytest -q'",
            "status": "failed",
            "aggregated_output": "Error: spawnSync /bin/bash EPERM",
        },
    }
)

_DNS = json.dumps(
    {
        "type": "error",
        "message": "stream error: getaddrinfo EAI_AGAIN api.example.invalid",
    }
)


def _classify(text: str, *, exit_code: int = 1):
    return classify_worker_failure(exit_code=exit_code, log_text=text)


# --- AC-1: the set itself ------------------------------------------------


def test_enum_is_the_closed_failure_set() -> None:
    assert {failure.value for failure in WorkerFailure} == {
        "invalid_args",
        "environment",
        "provider_api",
        "timeout",
        "user_abort",
        "claim_collision",
        "ortus_harness_bug",
    }


def test_enum_members_render_as_their_log_token() -> None:
    """The value is what lands in a log line and in a rollup key."""

    assert WorkerFailure.ORTUS_HARNESS_BUG.value == "ortus_harness_bug"
    assert f"{WorkerFailure.PROVIDER_API.value}" == "provider_api"


# --- AC-2: environment ---------------------------------------------------


def test_codex_sandbox_eperm_is_environment() -> None:
    classified = _classify(_EPERM)
    assert classified is not None
    assert classified.failure is WorkerFailure.ENVIRONMENT


def test_dns_failure_is_environment() -> None:
    classified = _classify(_DNS)
    assert classified is not None
    assert classified.failure is WorkerFailure.ENVIRONMENT


def test_missing_backend_binary_is_environment() -> None:
    classified = classify_worker_failure(
        exit_code=None, exception=FileNotFoundError("codex")
    )
    assert classified is not None
    assert classified.failure is WorkerFailure.ENVIRONMENT


def test_environment_outranks_the_provider_errors_it_causes() -> None:
    """A worker that lost DNS also logs provider noise; the cause wins."""

    classified = _classify(
        "\n".join([_DNS, json.dumps({"type": "error", "message": "429 Too Many Requests"})])
    )
    assert classified is not None
    assert classified.failure is WorkerFailure.ENVIRONMENT
    assert WorkerFailure.PROVIDER_API in classified.secondary


# --- AC-3: timeout -------------------------------------------------------


def test_watchdog_timeout_expired_is_timeout() -> None:
    classified = classify_worker_failure(
        exit_code=143,
        exception=subprocess.TimeoutExpired(cmd=["claude"], timeout=5400),
    )
    assert classified is not None
    assert classified.failure is WorkerFailure.TIMEOUT


def test_watchdog_timeout_flag_outranks_a_stream_signal() -> None:
    """grind converts TimeoutExpired into rc=143 plus a flag; the flag decides."""

    classified = classify_worker_failure(exit_code=143, timed_out=True, log_text=_EPERM)
    assert classified is not None
    assert classified.failure is WorkerFailure.TIMEOUT
    assert WorkerFailure.ENVIRONMENT in classified.secondary


def test_a_killed_worker_without_the_timeout_flag_is_not_a_timeout() -> None:
    """A reap kills the same way a watchdog does, so 143 alone proves nothing."""

    classified = classify_worker_failure(exit_code=143)
    assert classified is not None
    assert classified.failure is WorkerFailure.ORTUS_HARNESS_BUG


# --- AC-4: unknown is a harness bug --------------------------------------


def test_unrecognized_failure_is_ortus_harness_bug() -> None:
    classified = _classify(
        json.dumps({"type": "error", "message": "widget frobnicator disengaged"}),
        exit_code=9,
    )
    assert classified is not None
    assert classified.failure is WorkerFailure.ORTUS_HARNESS_BUG
    assert classified.unclassified
    assert "exit 9" in classified.detail


def test_a_window_that_did_not_fail_is_not_classified() -> None:
    classified = classify_worker_failure(exit_code=0, log_text=_EPERM)
    assert classified is None


# --- the remaining classes ----------------------------------------------


def test_provider_load_shedding_is_provider_api() -> None:
    """The recorded Codex failure stream, verbatim."""

    stream = (FIXTURES / "codex-exec-events-failed.jsonl").read_text(encoding="utf-8")
    classified = _classify(stream)
    assert classified is not None
    assert classified.failure is WorkerFailure.PROVIDER_API


def test_a_rejected_argv_is_invalid_args() -> None:
    classified = _classify("error: unexpected argument '--fastest' found", exit_code=2)
    assert classified is not None
    assert classified.failure is WorkerFailure.INVALID_ARGS


def test_an_already_claimed_issue_is_a_claim_collision() -> None:
    classified = _classify("bd update ortus-abcd: already in_progress")
    assert classified is not None
    assert classified.failure is WorkerFailure.CLAIM_COLLISION


def test_an_operator_interrupt_is_user_abort() -> None:
    classified = classify_worker_failure(exit_code=130)
    assert classified is not None
    assert classified.failure is WorkerFailure.USER_ABORT


def test_a_window_is_classified_from_its_own_slice_of_the_log(tmp_path: Path) -> None:
    """An earlier window's failure must not be charged to this one."""

    log = tmp_path / "grind-20260923-101500.log"
    log.write_text(_EPERM + "\n", encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "error", "message": "503 service unavailable"}) + "\n")

    classified = classify_worker_window(exit_code=1, log_path=log, start_offset=offset)
    assert classified is not None
    assert classified.failure is WorkerFailure.PROVIDER_API


# --- AC-5: rates per backend and model -----------------------------------


def _log(tmp_path: Path, backend: str, model: str, windows: list[tuple[int, str]]) -> Path:
    """A grind log whose windows end in the given failure marker lines."""

    lines = [
        f"[2026-09-23 10:15:00] === ortus grind started (subprocess-per-task "
        f"shape; backend={backend}; verification=full) ===",
        f"[2026-09-23 10:15:00] profile: {backend}/implement "
        f"(model={model}, effort=high)",
    ]
    for iteration, marker in windows:
        lines.append(
            f"[2026-09-23 10:15:0{iteration}] iter prep: worker will claim "
            f"ortus-ab{iteration}d via goal-prompt"
        )
        lines.append(
            f"[2026-09-23 10:15:0{iteration}] iter {iteration}: spawning {backend} "
            "(single-issue worker)"
        )
        if marker:
            lines.append(f"[2026-09-23 10:16:0{iteration}] {marker}")
    path = tmp_path / "logs" / "grind-20260923-101500.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_grind_failure_marker_feeds_the_rates_rollup(tmp_path: Path) -> None:
    """The line grind writes is the line the rollup reads back."""

    eperm = classify_worker_failure(exit_code=1, log_text=_EPERM)
    assert eperm is not None
    marker = failure_log_line(
        eperm, iteration=1, backend="codex", model="gpt-5-codex"
    )
    run = parse_grind_log(_log(tmp_path, "codex", "gpt-5-codex", [(1, marker), (2, "")]))

    assert [session.failure_class for session in run.sessions] == ["environment", None]

    rates = failure_rates(run.sessions)
    assert len(rates) == 1
    assert rates[0].backend == "codex"
    assert rates[0].model == "gpt-5-codex"
    assert rates[0].sessions == 2
    assert rates[0].failures == 1
    assert rates[0].counts["environment"] == 1
    assert rates[0].counts["provider_api"] == 0
    assert rates[0].failure_rate == 0.5


def test_failure_rates_split_by_backend_and_model(tmp_path: Path) -> None:
    timeout = classify_worker_failure(exit_code=143, timed_out=True)
    assert timeout is not None
    claude = parse_grind_log(
        _log(
            tmp_path / "a",
            "claude",
            "claude-opus-5[1m]",
            [(1, failure_log_line(timeout, iteration=1, backend="claude", model="claude-opus-5[1m]"))],
        )
    )
    codex = parse_grind_log(_log(tmp_path / "b", "codex", "gpt-5-codex", [(1, "")]))

    rates = failure_rates(claude.sessions + codex.sessions)
    assert [(rate.backend, rate.model) for rate in rates] == [
        ("claude", "claude-opus-5[1m]"),
        ("codex", "gpt-5-codex"),
    ]
    assert rates[0].counts["timeout"] == 1
    assert rates[1].failures == 0
    assert rates[1].failure_rate == 0.0


def test_failure_rates_count_every_class_in_the_closed_set(tmp_path: Path) -> None:
    """Zero-filled buckets: an empty class has to be visibly empty."""

    run = parse_grind_log(_log(tmp_path, "claude", "claude-opus-5[1m]", [(1, "")]))
    rates = failure_rates(run.sessions)
    assert set(rates[0].counts) == {failure.value for failure in WorkerFailure}
    assert rates[0].as_dict()["counts"]["ortus_harness_bug"] == 0


def test_failure_rates_bucket_an_unknown_class_as_a_harness_bug(tmp_path: Path) -> None:
    """A marker written by a newer Ortus must not invent a rollup key."""

    run = parse_grind_log(
        _log(
            tmp_path,
            "claude",
            "claude-opus-5[1m]",
            [(1, "iter 1: worker failure class=quantum_flutter backend=claude "
                 "model=claude-opus-5[1m] detail=from the future")],
        )
    )
    rates = failure_rates(run.sessions)
    assert rates[0].failures == 1
    assert rates[0].counts["ortus_harness_bug"] == 1
