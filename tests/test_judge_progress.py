"""The looping-worker signal: bounded tails, a shadow record, then a reap.

A worker held by nothing but its own repetition used to run until
`--worker-timeout` killed it, and the run recorded a watchdog timeout for a
worker that had stopped getting anywhere twenty minutes earlier. The watch
reads that window's own log on an interval and asks Jev one question about it.

Everything here is hermetic: a fake clock drives the interval, a queued client
answers each request, and the records are read back off disk. The tests pin the
three things that could hurt a live run — shadow never touches the worker, a
Jev failure never reaps, and the tail that leaves the machine is bounded and
screened — and then the enforce path that the whole module exists for.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ortus.commands import grind as grind_mod
from ortus.core import judge_progress as progress_mod
from ortus.core.config import DEFAULTS, load_config
from ortus.core.judge import JudgeConfig
from ortus.core.judge_log import OutcomeStatus
from ortus.core.judge_progress import (
    DEFAULT_PROGRESS_INTERVAL_S,
    JEV_LOOPING_REASON,
    PROGRESS_LOG_NAME,
    REDACTED,
    TAIL_MAX_BYTES,
    TAIL_MAX_LINES,
    ProgressMode,
    ProgressSample,
    ProgressVerdict,
    ProgressWatch,
    WindowOutcome,
    WorkerProgress,
    bounded_tail,
    evaluate_progress,
    progress_interval,
    progress_mode,
    read_tail,
    should_reap,
)
from ortus.core.judge_state import StateError
from ortus.core.judge_typesafe import JudgeFailure
from ortus.core.profiles import ProfileError


ENV = {"TYPESAFE_API_KEY": "secret-test-credential"}
CONFIG = JudgeConfig(enabled=True, include_log_tail=True)
INTERVAL = 300.0


def noul(value: float) -> dict:
    """One well-formed answer to the looping question."""
    return {"model": "jev-1.13.0",
            "answers": {"looping": {"type": "noul", "noul": value}}}


def facts(elapsed: float = 12.0, advanced: bool = False) -> WorkerProgress:
    return WorkerProgress(elapsed, advanced, OutcomeStatus.IN_PROGRESS)


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Heads:
    """Successive branch tips; the last one answers every later poll."""

    def __init__(self, *oids: str) -> None:
        self.oids = list(oids) or [""]

    def __call__(self) -> str:
        return self.oids.pop(0) if len(self.oids) > 1 else self.oids[0]


def client(bodies: list, calls: list) -> SimpleNamespace:
    """A client that answers each request with the next queued body."""

    async def system_one(*args, **kwargs):
        calls.append((args, kwargs))
        body = bodies.pop(0)
        if isinstance(body, Exception):
            raise body
        return body

    return SimpleNamespace(system_one=system_one)


def watch(
    tmp_path: Path,
    mode: ProgressMode,
    bodies: list,
    *,
    clock: Clock,
    logged: list[str],
    calls: list,
    heads: tuple[str, ...] = ("head-0",),
    lines: tuple[str, ...] = ("worker: reading the same file again",),
    interval: float = INTERVAL,
) -> ProgressWatch:
    log = tmp_path / "worker.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ProgressWatch(
        repo=tmp_path,
        config=CONFIG,
        mode=mode,
        run_id=uuid4(),
        issue_id="ortus-loop",
        log_path=log,
        head_oid=Heads(*heads),
        bead_status=lambda: OutcomeStatus.IN_PROGRESS,
        write_log=logged.append,
        interval=interval,
        clock=clock,
        client_factory=lambda _: client(bodies, calls),
        environ=ENV,
    )


def records(tmp_path: Path, event: str = "progress") -> list[dict]:
    path = tmp_path / "logs" / PROGRESS_LOG_NAME
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return [row for row in rows if row["event"] == event]


# --- the rule, as pure arithmetic ------------------------------------------


@pytest.mark.parametrize("samples,expected", [
    ((), False),
    (((0.9, "h"),), False),
    (((0.9, "h"), (0.9, "h")), True),
    (((0.9, "h"), (0.9, "i")), False),
    (((0.9, "h"), (0.5, "h")), False),
    (((0.9, "h"), (None, "h")), False),
    (((0.9, ""), (0.9, "")), False),
    (((0.9, "h"), (0.9, "h"), (0.2, "h")), False),
])
def test_should_reap_needs_two_readings_and_a_still_head(samples, expected) -> None:
    """Two consecutive readings over the floor, on one unchanged commit."""
    history = [ProgressSample(value, head) for value, head in samples]
    assert should_reap(history) is expected


def test_shrunk_reading_lands_on_the_coin_flip_without_confidence() -> None:
    assert ProgressVerdict(0.9, 0.1, 0.0).shrunk_looping == 0.5
    assert ProgressVerdict(0.9, 0.1, 1.0).shrunk_looping == 0.9
    assert ProgressVerdict(failure=JudgeFailure.TIMEOUT).shrunk_looping is None


@pytest.mark.parametrize("kwargs", [
    {"p_looping": 1.2, "p_progressing": 0.0, "confidence": 1.0},
    {"p_looping": True, "p_progressing": 0.0, "confidence": 1.0},
    {"p_looping": None, "p_progressing": None, "confidence": None},
    {"failure": JudgeFailure.TIMEOUT, "p_looping": 0.9},
    {"failure": "timeout"},
])
def test_verdict_refuses_a_vector_it_cannot_score(kwargs) -> None:
    with pytest.raises(ValueError):
        ProgressVerdict(**kwargs)


# --- the tail that leaves the machine --------------------------------------


def test_bounded_tail_caps_its_lines_and_its_bytes() -> None:
    """Both bounds hold, and what survives them is the newest work."""
    lines = [f"worker: step {index} " + "detail " * 90 for index in range(500)]
    text = bounded_tail(lines, CONFIG, environ=ENV)
    assert len(text.splitlines()) <= TAIL_MAX_LINES
    assert len(text.encode("utf-8")) <= TAIL_MAX_BYTES
    assert "step 499" in text
    assert "step 0 " not in text


def test_bounded_tail_reads_one_window_and_screens_each_line(tmp_path: Path) -> None:
    """The offset excludes an earlier window; a credential line is dropped on
    its own, leaving its neighbours to travel."""
    turn = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash"},
        {"type": "text", "text": "running   the suite"},
    ]}}
    earlier = "worker: an earlier window\n"
    log = tmp_path / "worker.log"
    log.write_text(
        earlier + json.dumps(turn) + "\n"
        + f"export API_KEY={ENV['TYPESAFE_API_KEY']}\n"
        + "worker: still here\n",
        encoding="utf-8",
    )
    text = bounded_tail(
        read_tail(log, start_offset=len(earlier)), CONFIG, environ=ENV
    )
    assert "an earlier window" not in text
    assert "assistant: tool:Bash running the suite" in text
    assert ENV["TYPESAFE_API_KEY"] not in text
    assert REDACTED in text
    assert "worker: still here" in text


def test_bounded_tail_of_a_missing_log_is_empty(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "absent.log") == []
    assert bounded_tail([], CONFIG, environ=ENV) == ""


# --- the request ----------------------------------------------------------


def test_request_carries_the_facts_and_nothing_else() -> None:
    calls: list = []
    verdict = evaluate_progress(
        "worker: reading the same file again", facts(), CONFIG,
        environ=ENV, client_factory=lambda _: client([noul(0.4)], calls),
    )
    assert verdict == ProgressVerdict(0.4, 0.6, 0.6)
    args, kwargs = calls[0]
    assert args[0] == {"worker": facts().payload(),
                       "log_tail": "worker: reading the same file again"}
    assert set(args[1]) == {"looping"}
    assert args[1]["looping"]["type"] == "noul"
    assert kwargs["timeout"] == CONFIG.timeout_seconds


def test_request_refuses_a_tail_that_is_not_bounded() -> None:
    with pytest.raises(StateError):
        evaluate_progress("x" * (TAIL_MAX_BYTES + 1), facts(), CONFIG,
                          environ=ENV, client_factory=lambda _: None)


# --- shadow: read the vector, touch nothing -------------------------------


def test_shadow_records_the_reap_it_would_have_taken(tmp_path: Path) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.SHADOW, [noul(0.9), noul(0.95)],
                    clock=clock, logged=logged, calls=calls)
    assert running.reason() is None
    for _ in range(2):
        clock.advance(INTERVAL)
        assert running.reason() is None
    assert running.reaped is False
    assert running.would_reap == 1
    rows = records(tmp_path)
    assert [row["would_reap"] for row in rows] == [False, True]
    assert [row["reaped"] for row in rows] == [False, False]
    assert rows[-1]["mode"] == "shadow"
    assert rows[-1]["vector"] == pytest.approx(
        {"looping": 0.95, "progressing": 0.05}
    )
    assert rows[-1]["bead_status"] == "in_progress"
    assert len(calls) == 2


def test_shadow_window_outcome_joins_the_checks_by_run(tmp_path: Path) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.SHADOW, [noul(0.9)],
                    clock=clock, logged=logged, calls=calls)
    clock.advance(INTERVAL)
    running.reason()
    running.record_window(WindowOutcome.TIMEOUT)
    window = records(tmp_path, "progress_window")
    assert len(window) == 1
    assert window[0]["outcome"] == "timeout"
    assert window[0]["checks"] == 1
    assert window[0]["run_id"] == records(tmp_path)[0]["run_id"]


def test_shadow_window_outcome_is_silent_without_a_check(tmp_path: Path) -> None:
    """A window the interval never reached has no signal to score."""
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.SHADOW, [], clock=clock,
                    logged=logged, calls=calls)
    running.record_window(WindowOutcome.CLOSED)
    assert records(tmp_path, "progress_window") == []


# --- enforce: the decision reaches the poll -------------------------------


def test_enforce_reaps_the_second_looping_reading(tmp_path: Path) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9), noul(0.95)],
                    clock=clock, logged=logged, calls=calls)
    clock.advance(INTERVAL)
    assert running.reason() is None
    clock.advance(INTERVAL)
    reason = running.reason()
    assert reason is not None and reason.startswith(JEV_LOOPING_REASON)
    assert running.reaped is True
    assert records(tmp_path)[-1]["reaped"] is True
    clock.advance(INTERVAL)
    assert running.reason() is None
    assert len(calls) == 2


def test_enforce_reap_reason_reaches_the_grind_poll(tmp_path: Path) -> None:
    """`_reap_reason` answers with the looping reason once the rule fires, and
    with None on the readings before it."""
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9), noul(0.9)],
                    clock=clock, logged=logged, calls=calls)
    bd = SimpleNamespace(in_progress_ids=lambda **_: set())

    def poll() -> str | None:
        return grind_mod._reap_reason(
            bd, SimpleNamespace(), baseline_closed=None,
            flagged_at_start=frozenset(), integration_branch="main",
            progress=running,
        )

    clock.advance(INTERVAL)
    assert poll() is None
    clock.advance(INTERVAL)
    reason = poll()
    assert reason is not None and reason.startswith(JEV_LOOPING_REASON)


def test_enforce_does_not_reap_a_window_that_advanced_the_branch(
    tmp_path: Path,
) -> None:
    """Visible progress between the readings is the strongest evidence there
    is that this worker is working."""
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9), noul(0.95)],
                    clock=clock, logged=logged, calls=calls,
                    heads=("head-0", "head-1", "head-2"))
    for _ in range(2):
        clock.advance(INTERVAL)
        assert running.reason() is None
    assert running.reaped is False
    assert records(tmp_path)[-1]["head_advanced"] is True


def test_enforce_asks_at_most_once_per_interval(tmp_path: Path) -> None:
    """The poll runs every couple of seconds; the request does not. Two hundred
    polls span more than one interval and less than two, and buy one request."""
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9)],
                    clock=clock, logged=logged, calls=calls)
    for _ in range(200):
        clock.advance(2.0)
        assert running.reason() is None
    assert len(calls) == 1


def test_off_mode_asks_nothing_and_records_nothing(tmp_path: Path) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.OFF, [noul(0.9)],
                    clock=clock, logged=logged, calls=calls)
    for _ in range(3):
        clock.advance(INTERVAL)
        assert running.reason() is None
    assert calls == []
    assert records(tmp_path) == []


# --- fail-open ------------------------------------------------------------


@pytest.mark.parametrize("body,failure", [
    (RuntimeError("sensitive provider message"), "service_error"),
    ({}, "invalid_answer"),
    ({"model": "jev-1.13.0", "answers": {"looping": {"type": "score", "score": 1}}},
     "invalid_answer"),
])
def test_fail_open_records_the_failure_and_keeps_the_worker(
    tmp_path: Path, body, failure: str
) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [body, body],
                    clock=clock, logged=logged, calls=calls)
    for _ in range(2):
        clock.advance(INTERVAL)
        assert running.reason() is None
    assert running.reaped is False
    assert running.would_reap == 0
    row = records(tmp_path)[-1]
    assert row["failure"] == failure
    assert row["vector"] == {"looping": None, "progressing": None}
    assert any(failure in line for line in logged)


def test_fail_open_without_a_key_never_reaches_a_client(tmp_path: Path) -> None:
    calls: list = []
    verdict = evaluate_progress("worker: step", facts(), CONFIG, environ={},
                                client_factory=lambda _: client([], calls))
    assert verdict == ProgressVerdict(failure=JudgeFailure.KEY_MISSING)
    assert calls == []


def test_fail_open_failure_breaks_a_run_of_looping_readings(
    tmp_path: Path,
) -> None:
    """An outage resets the evidence rather than accumulating across it."""
    clock, logged, calls = Clock(), [], []
    running = watch(
        tmp_path, ProgressMode.ENFORCE,
        [noul(0.9), RuntimeError("provider down"), noul(0.9)],
        clock=clock, logged=logged, calls=calls,
    )
    for _ in range(3):
        clock.advance(INTERVAL)
        assert running.reason() is None
    assert running.reaped is False


def test_fail_open_when_the_check_itself_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll is not a place to raise: an unexpected failure costs a reading."""
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9)],
                    clock=clock, logged=logged, calls=calls)

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(progress_mod, "evaluate_progress", boom)
    clock.advance(INTERVAL)
    assert running.reason() is None
    assert any("check failed" in line for line in logged)


def test_fail_open_when_the_window_has_no_log_yet(tmp_path: Path) -> None:
    clock, logged, calls = Clock(), [], []
    running = watch(tmp_path, ProgressMode.ENFORCE, [noul(0.9)],
                    clock=clock, logged=logged, calls=calls)
    running.log_path.unlink()
    clock.advance(INTERVAL)
    assert running.reason() is None
    assert calls == []
    assert records(tmp_path) == []
    assert any("check skipped" in line for line in logged)


# --- how a run resolves the mode -----------------------------------------


def test_progress_mode_prefers_the_export_then_the_file(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert progress_mode(cfg, environ={}) is ProgressMode.SHADOW
    assert progress_mode(cfg, environ={"ORTUS_JEV_PROGRESS": "enforce"}) is (
        ProgressMode.ENFORCE
    )
    assert progress_mode(None, environ={"ORTUS_JEV_PROGRESS": "off"}) is (
        ProgressMode.OFF
    )
    # An export nobody can read is not an opt-in to anything.
    assert progress_mode(None, environ={"ORTUS_JEV_PROGRESS": "sometimes"}) is (
        ProgressMode.SHADOW
    )


def test_progress_defaults_match_the_module_that_owns_them(tmp_path: Path) -> None:
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert DEFAULTS["jev_progress_reaper"] == ProgressMode.SHADOW.value
    assert DEFAULTS["jev_progress_interval_s"] == DEFAULT_PROGRESS_INTERVAL_S
    assert cfg.get("jev_progress_reaper") == ProgressMode.SHADOW.value
    assert progress_interval(cfg) == float(DEFAULT_PROGRESS_INTERVAL_S)


@pytest.mark.parametrize("line", [
    'jev_progress_reaper = "sometimes"',
    "jev_progress_reaper = true",
    "jev_progress_interval_s = 0",
    "jev_progress_interval_s = -1",
    'jev_progress_interval_s = "300"',
])
def test_an_unhonorable_progress_setting_fails_the_load(
    tmp_path: Path, line: str
) -> None:
    (tmp_path / ".ortusrc").write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ProfileError):
        load_config(repo=tmp_path, home=tmp_path / "home")


def test_an_enforcing_project_loads_its_cadence(tmp_path: Path) -> None:
    (tmp_path / ".ortusrc").write_text(
        'jev_progress_reaper = "enforce"\njev_progress_interval_s = 45\n',
        encoding="utf-8",
    )
    cfg = load_config(repo=tmp_path, home=tmp_path / "home")
    assert progress_mode(cfg, environ={}) is ProgressMode.ENFORCE
    assert progress_interval(cfg) == 45.0


@pytest.mark.parametrize("status,timed_out,reaped,expected", [
    ("closed", True, True, WindowOutcome.CLOSED),
    ("in_progress", True, False, WindowOutcome.TIMEOUT),
    ("in_progress", False, True, WindowOutcome.REAPED),
    ("open", False, False, WindowOutcome.EXITED),
])
def test_window_outcome_reads_a_close_before_a_manner_of_death(
    status: str, timed_out: bool, reaped: bool, expected: WindowOutcome
) -> None:
    assert grind_mod._window_outcome(
        status, timed_out=timed_out, reaped=reaped
    ) is expected
