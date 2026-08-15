"""Run-state model: log identity, incremental log tail, writer discrimination."""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path

from ortus.core.runstate import (
    PHASE_IDLE,
    LogEvent,
    Writer,
    classify_line,
    find_log,
    read_log_tail,
    read_snapshot,
)


def _stamp(moment: _dt.datetime) -> str:
    """Render `moment` the way grind's `_log_writer` does: local, no zone."""

    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _ortus(moment: _dt.datetime, message: str) -> str:
    return f"[{_stamp(moment)}] {message}\n"


def _agent(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _tool_use(name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": name, "input": tool_input}],
        },
    }


def _write_log(repo: Path, body: str, name: str = "grind-20260808-120000.log") -> Path:
    log = repo / "logs" / name
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(body, encoding="utf-8")
    return log


def test_snapshot_from_log_reports_identity(tmp_path: Path) -> None:
    """AC-1: issue, phase and attempt come off the grind log, not a journal."""

    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    _write_log(
        tmp_path,
        _ortus(moment, "iter 2: goal-prompt ready for ortus-0udo.1 (claude)")
        + _ortus(moment, "iter 2: verification started"),
    )
    leftover = tmp_path / "logs" / "grind-transaction.json"
    leftover.write_text('{"issue_id": "should-be-ignored", "phase": "verification"}')

    snapshot = read_snapshot(tmp_path)

    assert snapshot.issue_id == "ortus-0udo.1"
    assert snapshot.phase == "verification"
    assert snapshot.attempt == 2
    assert snapshot.backend == "claude"
    assert snapshot.candidate_paths == ()
    assert snapshot.journal_present is False
    assert leftover.is_file()
    assert snapshot.idle is False
    assert snapshot.terminal is False


def test_snapshot_from_log_reports_a_terminal_phase(tmp_path: Path) -> None:
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    _write_log(tmp_path, _ortus(moment, "iter 1: worker closed ortus-0udo.1"))

    assert read_snapshot(tmp_path).terminal is True

    _write_log(
        tmp_path,
        _ortus(moment, "iter 1: step corrections-exhausted"),
        name="grind-20260808-130000.log",
    )

    assert read_snapshot(tmp_path).terminal is True


def test_unknown_phase_renders_verbatim(tmp_path: Path) -> None:
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    _write_log(tmp_path, _ortus(moment, "iter 1: step some-future-phase"))

    snapshot = read_snapshot(tmp_path)

    assert snapshot.phase == "some-future-phase"
    assert snapshot.terminal is False


def test_leftover_journal_without_a_log_is_ignored(tmp_path: Path) -> None:
    leftover = tmp_path / "logs" / "grind-transaction.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(
        json.dumps(
            {
                "schema": 1,
                "issue_id": "ortus-legacy",
                "phase": "implementation-timeout",
                "invented_by_a_newer_writer": {"x": 1},
            }
        ),
        encoding="utf-8",
    )

    snapshot = read_snapshot(tmp_path)

    assert snapshot.issue_id == ""
    assert snapshot.phase == PHASE_IDLE
    assert snapshot.candidate_paths == ()
    assert snapshot.journal_present is False
    assert snapshot.idle is True
    assert leftover.is_file()


def test_incremental_offset_reads_only_appended_bytes(tmp_path: Path) -> None:
    """AC-2: a second call with the returned offset parses only the new bytes."""

    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    log.write_text(_ortus(moment, "iter 1: implementation worker started"), "utf-8")

    first = read_snapshot(tmp_path)

    assert first.log_path == log
    assert first.offset == log.stat().st_size
    assert [event.text for event in first.events] == [
        "iter 1: implementation worker started"
    ]

    with log.open("a", encoding="utf-8") as handle:
        handle.write(_agent(_tool_use("Bash", {"command": "uv run pytest -q"})))
        handle.write(_ortus(moment, "iter 1: candidate captured"))

    second = read_snapshot(tmp_path, offset=first.offset)

    assert second.offset == log.stat().st_size
    assert [event.text for event in second.events] == [
        "Bash uv run pytest -q",
        "iter 1: candidate captured",
    ]

    # Passing the prior snapshot back is the same contract without threading
    # the offset by hand, and a call with nothing appended reads no bytes.
    third = read_snapshot(tmp_path, previous=second)

    assert third.events == ()
    assert third.offset == second.offset


def test_incremental_offset_survives_a_new_run_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    old = logs / "grind-20260808-120000.log"
    old.write_text(_ortus(moment, "iter 1: done"), "utf-8")

    first = read_snapshot(tmp_path)

    fresh = logs / "grind-20260808-130000.log"
    fresh.write_text(_ortus(moment, "iter 1: fresh run started"), "utf-8")
    # The newest log is the live one; age the finished run explicitly so the
    # test does not depend on filesystem timestamp resolution.
    aged = fresh.stat().st_mtime - 3600
    os.utime(old, (aged, aged))

    second = read_snapshot(tmp_path, previous=first)

    assert second.log_path == fresh
    assert [event.text for event in second.events] == ["iter 1: fresh run started"]
    assert second.offset == fresh.stat().st_size


def test_warning_writer_discrimination_ignores_agent_content(tmp_path: Path) -> None:
    """AC-3: only ortus lines produce warnings, whatever the agent quotes."""

    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    quoted = (
        # A worker editing the recovery code writes the vocabulary verbatim.
        _agent(
            _tool_use(
                "Edit",
                {
                    "file_path": "src/ortus/commands/grind.py",
                    "new_string": 'write_log(f"iter {n}: worker TIMEOUT after {t}s, killed")',
                },
            )
        )
        + _agent(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "## Ortus correction escalation — bounded "
                            "correction attempts exhausted (2/2); HALT",
                        }
                    ],
                },
            }
        )
        + _agent(
            {
                "type": "ortus.verdict",
                "schema": 1,
                "decision": "fail",
                "candidate_hash": "deadbeef",
                "reason": "verifier rejected: plan gap in findings",
            }
        )
    )
    log.write_text(quoted, "utf-8")

    clean = read_snapshot(tmp_path)

    assert clean.warnings == ()
    assert clean.warning_counts == {}

    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            _ortus(moment, "iter 3: worker TIMEOUT after 1800s, killed (rc=143)")
        )

    warned = read_snapshot(tmp_path, previous=clean)

    assert [warning.kind for warning in warned.warnings] == ["timeout"]
    assert warned.warning_counts == {"timeout": 1}
    assert "TIMEOUT after 1800s" in warned.warnings[0].text
    assert warned.warnings[0].at == moment


def test_warning_writer_discrimination_classifies_each_writer(tmp_path: Path) -> None:
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()

    plain = classify_line(_ortus(moment, "iter 1: verifier rejected: drift").strip())
    structured = classify_line(
        json.dumps({"type": "ortus.verdict", "decision": "pass"})
    )
    agent = classify_line(json.dumps(_tool_use("Bash", {"command": "ls"})))

    assert plain is not None and plain.writer is Writer.ORTUS and plain.kind == ""
    assert structured is not None and structured.writer is Writer.ORTUS
    assert structured.kind == "ortus.verdict"
    assert agent is not None and agent.writer is Writer.AGENT
    assert classify_line("   ") is None


def test_warnings_accumulate_across_refreshes(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    log.write_text(_ortus(moment, "preflight: HALT — git status failed"), "utf-8")

    first = read_snapshot(tmp_path)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            _ortus(moment, "iter 2: bounded correction attempts exhausted (2/2)")
        )
    second = read_snapshot(tmp_path, previous=first)

    assert [warning.kind for warning in second.warnings] == ["halt", "exhausted"]


def test_latest_action_and_blocked_duration(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    started = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    log.write_text(
        _ortus(started, "iter 1: implementation worker started")
        + _agent(_tool_use("Bash", {"command": "uv run pytest -q"})),
        "utf-8",
    )

    snapshot = read_snapshot(tmp_path, now=started + _dt.timedelta(seconds=90))

    assert snapshot.latest_action == "Bash uv run pytest -q"
    # The agent event carries no time of its own, so it inherits the log clock
    # from the ortus line that precedes it.
    assert snapshot.latest_action_at == started
    assert snapshot.blocked_seconds == 90.0


def test_heartbeat_events_do_not_displace_the_latest_action(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    started = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    log.write_text(
        _agent(_tool_use("Bash", {"command": "uv run pytest -q"})), "utf-8"
    )

    first = read_snapshot(tmp_path, now=started)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            _agent(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "thinking", "thinking": "still waiting"}]
                    },
                }
            )
        )
        handle.write(_agent({"type": "system", "subtype": "init", "session_id": "s1"}))
    second = read_snapshot(
        tmp_path, previous=first, now=started + _dt.timedelta(seconds=600)
    )

    assert second.latest_action == "Bash uv run pytest -q"
    assert second.blocked_seconds == 600.0


def test_blocked_duration_never_goes_negative(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    future = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    log.write_text(_ortus(future, "iter 1: worker started"), "utf-8")

    snapshot = read_snapshot(tmp_path, now=future - _dt.timedelta(hours=5))

    assert snapshot.blocked_seconds == 0.0


def test_degraded_inputs_survive_truncation_rotation_and_absence(
    tmp_path: Path,
) -> None:
    """AC-4: a half-written line, a rotated log and an absent grind log."""

    # No logs directory at all: idle, not an error.
    empty = read_snapshot(tmp_path)
    assert empty.log_path is None
    assert empty.offset == 0
    assert empty.events == ()
    assert empty.phase == PHASE_IDLE
    assert empty.idle is True
    assert find_log(tmp_path) is None

    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    moment = _dt.datetime(2026, 8, 8, 12, 0, 0).astimezone()
    complete = _ortus(moment, "iter 1: implementation worker started")
    partial = '{"type":"assistant","message":{"cont'  # cut mid-key, as a live log is
    log.write_text(complete + partial, "utf-8")

    # A log with no leftover journal is valid, and the half-written final line
    # is skipped rather than raising or being parsed as an ortus line.
    first = read_snapshot(tmp_path)
    assert first.journal_present is False
    assert first.phase == "implementation"
    assert first.idle is False
    assert first.offset == len(complete.encode("utf-8"))
    assert [event.text for event in first.events] == [
        "iter 1: implementation worker started"
    ]

    # Completing the line makes it readable on the next call, exactly once.
    rest = 'ent":[{"type":"text","text":"done"}]}}\n'
    with log.open("a", encoding="utf-8") as handle:
        handle.write(rest)
    second = read_snapshot(tmp_path, previous=first)
    assert [event.text for event in second.events] == ["done"]
    assert second.offset == log.stat().st_size

    # Rotation: the log shrinks, so the offset resets instead of seeking past
    # the end, and accumulated warnings are dropped with the run they came from.
    log.write_text(_ortus(moment, "iter 1: rotated run started"), "utf-8")
    third = read_snapshot(tmp_path, previous=second)
    assert [event.text for event in third.events] == ["iter 1: rotated run started"]
    assert third.offset == log.stat().st_size
    assert third.warnings == ()

    # A leftover journal that is not valid JSON is ignored, not a crash.
    leftover = tmp_path / "logs" / "grind-transaction.json"
    leftover.write_text("{not json", encoding="utf-8")
    fourth = read_snapshot(tmp_path, previous=third)
    assert fourth.journal_present is False
    assert fourth.idle is False
    assert leftover.is_file()


def test_degraded_inputs_tolerate_a_leftover_journal_with_no_log(
    tmp_path: Path,
) -> None:
    leftover = tmp_path / "logs" / "grind-transaction.json"
    leftover.parent.mkdir(parents=True)
    leftover.write_text(
        json.dumps({"issue_id": "ortus-0udo.1", "phase": "verification"}),
        encoding="utf-8",
    )

    snapshot = read_snapshot(tmp_path)

    assert snapshot.issue_id == ""
    assert snapshot.log_path is None
    assert snapshot.idle is True
    assert snapshot.latest_action == ""
    assert snapshot.blocked_seconds == 0.0


def test_degraded_inputs_tolerate_a_vanished_replay_log(tmp_path: Path) -> None:
    missing = tmp_path / "logs" / "grind-gone.log"

    tail = read_log_tail(missing, 512)
    snapshot = read_snapshot(tmp_path, log_path=missing)

    assert tail.events == ()
    assert tail.truncated is True
    assert snapshot.events == ()
    assert snapshot.log_path == missing


def test_read_log_tail_reports_truncation(tmp_path: Path) -> None:
    log = tmp_path / "grind.log"
    log.write_text("[2026-08-08 12:00:00] one\n[2026-08-08 12:00:01] two\n", "utf-8")

    first = read_log_tail(log)
    log.write_text("[2026-08-08 12:00:02] three\n", "utf-8")
    second = read_log_tail(log, first.offset)

    assert first.truncated is False
    assert second.truncated is True
    assert [event.text for event in second.events] == ["three"]


def test_codex_items_describe_the_current_action(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        _agent(
            {
                "type": "item.started",
                "item": {
                    "id": "i1",
                    "type": "command_execution",
                    "command": "uv run pytest -q",
                },
            }
        ),
        "utf-8",
    )

    snapshot = read_snapshot(tmp_path)

    assert snapshot.latest_action == "command: uv run pytest -q"


def test_module_stays_headless() -> None:
    """The model must be usable without Textual, so it may not import it."""

    module = Path(__file__).parent.parent / "src" / "ortus" / "core" / "runstate.py"
    source = module.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.MULTILINE)

    assert not [name for name in imports if name.split(".")[0] == "textual"]
    assert "subprocess" not in imports


def test_events_carry_their_payload_for_downstream_panels(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "grind-20260808-120000.log"
    log.parent.mkdir(parents=True)
    envelope = {
        "type": "ortus.verdict",
        "schema": 1,
        "decision": "pass",
        "candidate_hash": "deadbeef",
        "reason": "",
    }
    log.write_text(_agent(envelope), "utf-8")

    snapshot = read_snapshot(tmp_path)
    verdicts = [event for event in snapshot.events if event.kind == "ortus.verdict"]

    assert isinstance(verdicts[0], LogEvent)
    assert verdicts[0].payload == envelope
    assert verdicts[0].text == "verdict pass"
