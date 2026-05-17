"""Unit tests for core/grind_logic.py — pure condition + flock logic."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from ortus.core.grind_logic import (
    BuiltCondition,
    CONDITION_CEILING,
    ConditionTooLong,
    FlockBusy,
    build_condition,
    grind_flock,
)


# --- build_condition --------------------------------------------------------


def test_custom_condition_passes_through_verbatim() -> None:
    custom = "queue empty AND tests green"
    c = build_condition(custom)
    assert c.text == custom


def test_default_drops_the_early_stop_block() -> None:
    c = build_condition()
    assert "Drive the bd queue to zero" in c.text
    assert "You may stop early" not in c.text
    assert "(a) you have closed" not in c.text
    assert "(b) you have used" not in c.text


def test_max_tasks_keeps_a_drops_b() -> None:
    c = build_condition(max_tasks=5)
    assert "(a) you have closed 5 issues" in c.text
    assert "(b) you have used" not in c.text
    # And the trailing ", OR" on (a) should be rewritten to ".".
    assert ", OR\n" not in c.text


def test_max_iters_keeps_b_drops_a() -> None:
    c = build_condition(max_iters=10)
    assert "(b) you have used 10 turns" in c.text
    assert "(a) you have closed" not in c.text


def test_both_flags_keep_block_intact() -> None:
    c = build_condition(max_tasks=3, max_iters=10)
    assert "(a) you have closed 3 issues" in c.text
    assert "(b) you have used 10 turns" in c.text
    assert "You may stop early if EITHER:" in c.text


def test_condition_too_long_raises() -> None:
    huge = "x" * (CONDITION_CEILING + 1)
    with pytest.raises(ConditionTooLong):
        build_condition(huge)


def test_built_condition_under_ceiling() -> None:
    c = build_condition()
    assert len(c.text) <= CONDITION_CEILING


# --- flock ------------------------------------------------------------------


def test_flock_acquired_and_released(tmp_path: Path) -> None:
    (tmp_path / ".beads").mkdir()
    with grind_flock(tmp_path) as lockfile:
        assert lockfile.is_file()
    # After release, a second acquire should succeed.
    with grind_flock(tmp_path):
        pass


def _hold_flock(repo_str: str, hold_seconds: float, acquired_event) -> None:
    """Run in a subprocess to hold the flock for `hold_seconds` then release.

    Signals `acquired_event` once the lock is held so the parent can race
    in deterministically — without a handshake the parent may try to
    acquire before the child reaches the lock call, especially on Windows
    where multiprocessing uses spawn() (re-imports the module + interprets
    Python startup overhead before the lock body runs).
    """
    from ortus.core.grind_logic import grind_flock as _gf

    with _gf(Path(repo_str)):
        acquired_event.set()
        time.sleep(hold_seconds)


def test_second_concurrent_grind_raises_flockbusy_under_500ms(
    tmp_path: Path,
) -> None:
    """Acceptance #2: second grind exits non-zero in ≤500ms (we measure FlockBusy)."""
    (tmp_path / ".beads").mkdir()
    # Use the spawn context explicitly so behavior is identical on POSIX and
    # Windows — fork() vs spawn() change how the child inherits the lock,
    # and we want the same shape of test running everywhere.
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    proc = ctx.Process(target=_hold_flock, args=(str(tmp_path), 3.0, acquired))
    proc.start()
    try:
        # Wait for the child to actually hold the lock before the parent tries.
        # Spawn-start can take >1s on Windows runners; 5s budget here is for
        # process startup, not for the lock acquire path under test.
        assert acquired.wait(timeout=5.0), (
            "child grind_flock holder did not signal lock acquisition within 5s"
        )
        t0 = time.monotonic()
        with pytest.raises(FlockBusy):
            with grind_flock(tmp_path):
                pass
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"FlockBusy took {elapsed*1000:.0f}ms (budget: 500ms)"
    finally:
        proc.terminate()
        proc.join(timeout=2)
