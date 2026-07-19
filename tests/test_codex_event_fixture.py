"""Guards the pinned Codex `--json` event schema (SPIKE Q2, PRD Open Questions).

tests/fixtures/codex-exec-events.jsonl is a real `codex exec --json` stream
captured from codex-cli 0.144.6 against a stub Responses endpoint — the upstream
model is faked, but every event is emitted by the genuine Codex event pipeline,
including real sandboxed command execution.

The fixture is the unit-test input for the tail.sh Codex decoder (ortus-5hae), so
these tests assert the exact field paths the decoder reads are present. If a
future Codex release renames a field, the fixture gets recaptured and these
assertions are what surface the break.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
HAPPY_PATH = FIXTURES / "codex-exec-events.jsonl"
FAILED_TURN = FIXTURES / "codex-exec-events-failed.jsonl"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def events() -> list[dict]:
    return load(HAPPY_PATH)


def items_of_type(events: list[dict], item_type: str) -> list[dict]:
    return [e["item"] for e in events if e.get("item", {}).get("type") == item_type]


def test_every_line_is_a_typed_event(events):
    """The decoder dispatches on top-level `type`; nothing may arrive untyped."""
    assert events, "fixture is empty"
    assert all("type" in e for e in events)


def test_envelope_types_present(events):
    seen = {e["type"] for e in events}
    assert {"thread.started", "turn.started", "turn.completed",
            "item.started", "item.completed"} <= seen


def test_thread_started_carries_thread_id(events):
    started = [e for e in events if e["type"] == "thread.started"]
    assert len(started) == 1
    assert started[0]["thread_id"]


def test_agent_message_text_path(events):
    """Assistant text lives at .item.text — a bare string, not a content array."""
    messages = items_of_type(events, "agent_message")
    assert messages, "fixture must exercise assistant text"
    assert all(isinstance(m["text"], str) and m["text"] for m in messages)


def test_reasoning_text_path(events):
    reasoning = items_of_type(events, "reasoning")
    assert reasoning
    assert all(isinstance(r["text"], str) for r in reasoning)


def test_command_execution_fields(events):
    """Command calls expose argv, merged output, exit code and status."""
    commands = items_of_type(events, "command_execution")
    assert commands, "fixture must exercise command execution"
    for cmd in commands:
        assert cmd["command"]
        assert "aggregated_output" in cmd
        assert "exit_code" in cmd
        assert cmd["status"] in {"in_progress", "completed", "failed"}


def test_command_execution_covers_success_and_failure(events):
    """A renderer must distinguish exit 0 from a non-zero exit."""
    terminal = [c for c in items_of_type(events, "command_execution")
                if c["status"] != "in_progress"]
    assert {c["status"] for c in terminal} == {"completed", "failed"}
    assert any(c["exit_code"] == 0 for c in terminal)
    assert any(c["exit_code"] not in (0, None) for c in terminal)


def test_in_progress_command_has_null_exit_code(events):
    running = [c for c in items_of_type(events, "command_execution")
               if c["status"] == "in_progress"]
    assert running
    assert all(c["exit_code"] is None for c in running)


def test_item_ids_are_reused_across_started_and_completed(events):
    """item.started and item.completed share .item.id, so renders update in place."""
    started = {e["item"]["id"] for e in events if e["type"] == "item.started"}
    completed = {e["item"]["id"] for e in events if e["type"] == "item.completed"}
    assert started & completed


def test_todo_list_fields(events):
    todos = items_of_type(events, "todo_list")
    assert todos
    for todo in todos:
        assert todo["items"]
        for entry in todo["items"]:
            assert entry["text"]
            assert isinstance(entry["completed"], bool)


def test_token_counts_on_turn_completed(events):
    """There is no standalone `usage` event — counts ride on turn.completed."""
    assert not any(e["type"] == "usage" for e in events)
    completed = [e for e in events if e["type"] == "turn.completed"]
    assert len(completed) == 1
    usage = completed[0]["usage"]
    for field in ("input_tokens", "cached_input_tokens",
                  "output_tokens", "reasoning_output_tokens"):
        assert isinstance(usage[field], int), field


def test_no_file_change_item_type(events):
    """Codex 0.144.6 has no apply_patch tool; edits arrive as command_execution.

    Documents the gap so the decoder is not written against an item type the CLI
    never emits. If a future Codex adds one, this fails and the decoder gains a
    branch deliberately rather than by accident.
    """
    assert not items_of_type(events, "file_change")


def test_failed_turn_fixture_exposes_error_message():
    events = load(FAILED_TURN)
    failed = [e for e in events if e["type"] == "turn.failed"]
    assert len(failed) == 1
    assert failed[0]["error"]["message"]
    assert any(e["type"] == "error" and e["message"] for e in events)
