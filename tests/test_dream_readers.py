"""Dream's reader stage: parsing, the notes cache, chunking and input files."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import dream as dream_module
from ortus.commands.dream import NOTES_DIR, REPORT_STUB_MESSAGE
from ortus.core.dream_readers import (
    INPUTS_DIR,
    DreamReaderError,
    check_context,
    parse_answer,
    run_readers,
    split_chunks,
)
from ortus.core.dream_transcripts import (
    BeadWindow,
    PreparedWindow,
    load_or_prepare,
    split_windows,
    window_key,
)
from ortus.core.profiles import AgentProfile, Phase

FIXTURE = Path(__file__).parent / "fixtures" / "dream" / "grind-20261011-000000.log"

cli = CliRunner()

GOOD_NOTE = {
    "issue": "pytest ran under the wrong interpreter",
    "evidence": [{"label": "[grind-log bead=ortus-aaa1]", "quote": "1 failed"}],
    "cost": {"turns": 2, "tokens": None},
    "memory_would_help": True,
    "kind_hint": "memory",
}
WINDOW_LINE = 'ORTUS_DREAM_WINDOW: {"outcome": "closed", "notes": 1}'


def _assistant(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def _answer(*lines: str, cost: float = 0.01) -> str:
    result = {
        "type": "result",
        "usage": {"input_tokens": 100, "output_tokens": 10},
        "total_cost_usd": cost,
    }
    return "\n".join((_assistant("\n".join(lines)), json.dumps(result))) + "\n"


def _note(note: dict) -> str:
    return f"ORTUS_DREAM_NOTE: {json.dumps(note)}"


class FakeRunner:
    """Writes a canned stream-json answer to log_path, records each call."""

    def __init__(self, answer: str | None = None, code: int = 0) -> None:
        self.answer = answer if answer is not None else _answer(_note(GOOD_NOTE), WINDOW_LINE)
        self.code = code
        self.calls: list[dict] = []
        self.preflights = 0

    def run(self, prompt, *, repo, log_path, profile=None, timeout=None, readonly=False):
        self.calls.append(
            {"prompt": prompt, "log_path": log_path, "readonly": readonly, "timeout": timeout}
        )
        log_path.write_text(self.answer, encoding="utf-8")
        return self.code

    def preflight_readonly(self, repo, *, timeout=60.0):
        self.preflights += 1


PROFILE = AgentProfile(
    backend="claude", phase=Phase.DREAM_READER, model="claude-haiku-5-5"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)
    root = tmp_path / "repo"
    (root / ".beads").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / ".gitignore").write_text(".ortus/dream/\n", encoding="utf-8")
    shutil.copy(FIXTURE, root / "logs" / FIXTURE.name)
    return root


def _prepared(repo: Path) -> list[PreparedWindow]:
    return [
        load_or_prepare(window, repo=repo)
        for window in split_windows(repo / "logs" / FIXTURE.name)
    ]


def _run(repo: Path, runner: FakeRunner, items, *, chunk_tokens: int = 100_000):
    return run_readers(
        items,
        runner=runner,
        repo=repo,
        header_for=lambda bead: f"[bead {bead}]\ntitle:   t\n[/bead]",
        brief="BRIEF",
        key="k1",
        backend="claude",
        profile=PROFILE,
        chunk_tokens=chunk_tokens,
        cut_bytes=8192,
        notes_path_for=lambda item: repo / NOTES_DIR / f"{window_key(item.window)}.k1.json",
        stamp="20261031-000000",
    )


# --- parse -----------------------------------------------------------------


def test_parse_keeps_valid_notes_and_counts_invalid(tmp_path: Path) -> None:
    bad_kind = {**GOOD_NOTE, "kind_hint": "maybe"}
    no_evidence = {**GOOD_NOTE, "evidence": []}
    empty_issue = {**GOOD_NOTE, "issue": " "}
    log = tmp_path / "call.jsonl"
    log.write_text(
        _answer(
            _note(GOOD_NOTE),
            _note(bad_kind),
            _note(no_evidence),
            _note(empty_issue),
            "ORTUS_DREAM_NOTE: {not json",
            "  " + WINDOW_LINE,
        ),
        encoding="utf-8",
    )

    answer = parse_answer(log)

    assert answer.error is None
    assert answer.notes == (GOOD_NOTE,)
    assert answer.invalid_notes == 4
    assert answer.window == {"outcome": "closed", "notes": 1}
    assert answer.usage == {"input_tokens": 100, "output_tokens": 10}
    assert answer.cost_usd == 0.01


def test_parse_missing_or_duplicate_window_line_is_an_error(tmp_path: Path) -> None:
    log = tmp_path / "call.jsonl"
    log.write_text(_answer(_note(GOOD_NOTE)), encoding="utf-8")
    assert parse_answer(log).error == "no ORTUS_DREAM_WINDOW line"

    log.write_text(_answer(WINDOW_LINE, WINDOW_LINE), encoding="utf-8")
    assert parse_answer(log).error == "2 ORTUS_DREAM_WINDOW lines"


def test_parse_zero_notes_with_window_line_is_valid(repo: Path) -> None:
    runner = FakeRunner(_answer('ORTUS_DREAM_WINDOW: {"outcome": "ok", "notes": 0}'))
    results = _run(repo, runner, _prepared(repo)[:1])

    assert results[0].error is None
    record = json.loads(results[0].notes_file.read_text(encoding="utf-8"))
    assert record["notes"] == []
    assert record["window"] == {"outcome": "ok", "notes": 0}


# --- cache -----------------------------------------------------------------


def test_cache_writes_notes_file_with_record_fields(repo: Path) -> None:
    runner = FakeRunner()
    results = _run(repo, runner, _prepared(repo))

    assert runner.preflights == 1
    assert all(call["readonly"] for call in runner.calls)
    assert all(call["timeout"] == 900 for call in runner.calls)
    record = json.loads(results[0].notes_file.read_text(encoding="utf-8"))
    assert record["notes"] == [GOOD_NOTE]
    assert record["reader_key"] == "k1"
    assert record["model"] == "claude-haiku-5-5"
    assert record["chunks"] == 1
    assert record["cost_usd"] == 0.01
    assert record["label"].startswith("[grind-log bead=")
    assert (repo / "logs" / "dream-20261031-000000.log").is_file()


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(code=1),
        FakeRunner(_answer(_note(GOOD_NOTE))),
    ],
    ids=["nonzero-exit", "no-window-line"],
)
def test_cache_skips_failed_window(repo: Path, runner: FakeRunner) -> None:
    results = _run(repo, runner, _prepared(repo))

    assert all(result.error for result in results)
    assert all(result.notes_file is None for result in results)
    assert not (repo / NOTES_DIR).exists()


def test_cache_skips_timed_out_window(repo: Path) -> None:
    class Slow(FakeRunner):
        def run(self, prompt, **kwargs):
            raise subprocess.TimeoutExpired("claude", 900)

    results = _run(repo, Slow(), _prepared(repo)[:1])

    assert "timed out" in results[0].error
    assert not (repo / NOTES_DIR).exists()


def _cli_runner(monkeypatch: pytest.MonkeyPatch, fake: FakeRunner) -> None:
    monkeypatch.setattr(dream_module, "make_runner", lambda backend, repo=None: fake)
    monkeypatch.setattr(
        dream_module, "load_bead_header", lambda bd, bead: f"[bead {bead}]\n[/bead]"
    )
    monkeypatch.setattr(dream_module, "run_orchestrator", lambda **kwargs: None)
    monkeypatch.setattr(dream_module, "validate_run", lambda *args, **kwargs: None)


def test_cache_reused_on_rerun_unless_reread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    _cli_runner(monkeypatch, fake)

    first = cli.invoke(app, ["dream", str(repo)])
    assert first.exit_code == 1
    assert REPORT_STUB_MESSAGE in first.output
    assert len(fake.calls) == 2
    assert len(list((repo / NOTES_DIR).glob("*.json"))) == 2

    second = cli.invoke(app, ["dream", str(repo)])
    assert REPORT_STUB_MESSAGE in second.output
    assert len(fake.calls) == 2

    third = cli.invoke(app, ["dream", str(repo), "--reread"])
    assert REPORT_STUB_MESSAGE in third.output
    assert len(fake.calls) == 4


def test_cache_failed_window_is_retried_next_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(code=1)
    _cli_runner(monkeypatch, fake)
    cli.invoke(app, ["dream", str(repo)])
    assert len(fake.calls) == 2

    fake.code = 0
    cli.invoke(app, ["dream", str(repo)])
    assert len(fake.calls) == 4


# --- chunk -----------------------------------------------------------------


def test_chunk_split_respects_budget_and_isolates_huge_lines() -> None:
    lines = [(10, "a" * 40), (11, "b" * 40), (12, "c" * 500), (13, "d" * 10)]

    chunks = split_chunks(lines, budget_bytes=100)

    assert [(c.start_line, c.end_line) for c in chunks] == [(10, 11), (12, 12), (13, 13)]
    assert chunks[0].body == "a" * 40 + "\n" + "b" * 40


def _big_window(repo: Path) -> PreparedWindow:
    lines = tuple(_assistant(f"step {n} " + "x" * 300) for n in range(20))
    window = BeadWindow(
        log_path=repo / "logs" / "grind-20261020-000000.log",
        run="grind-20261020-000000",
        bead_id="ortus-big1",
        start_line=101,
        end_line=120,
        backend="claude",
        lines=lines,
    )
    return PreparedWindow(window=window, body="\n".join(lines), cuts=0)


def test_chunk_large_window_carries_previous_notes(repo: Path) -> None:
    first_note = {**GOOD_NOTE, "issue": "first chunk friction"}
    answers = iter(
        [
            _answer(_note(first_note), WINDOW_LINE),
            _answer(_note(first_note), _note(GOOD_NOTE), WINDOW_LINE),
        ]
    )

    class Chunked(FakeRunner):
        def run(self, prompt, *, log_path, **kwargs):
            self.calls.append({"prompt": prompt, "log_path": log_path})
            try:
                log_path.write_text(next(answers), encoding="utf-8")
            except StopIteration:
                log_path.write_text(_answer(_note(GOOD_NOTE), WINDOW_LINE), encoding="utf-8")
            return 0

    runner = Chunked()
    results = _run(repo, runner, [_big_window(repo)], chunk_tokens=1000)

    result = results[0]
    assert result.chunks >= 2
    inputs = sorted((repo / INPUTS_DIR).glob("*.chunk*.md"))
    assert len(inputs) == result.chunks
    first = (repo / INPUTS_DIR / f"{window_key(_big_window(repo).window)}.k1.chunk1.md")
    second = first.with_name(first.name.replace("chunk1", "chunk2"))
    first_text = first.read_text(encoding="utf-8")
    second_text = second.read_text(encoding="utf-8")
    assert "[grind-log bead=ortus-big1 run=grind-20261020-000000 lines=101-" in first_text
    assert "[reader-notes" not in first_text
    assert (
        "[reader-notes bead=ortus-big1 run=grind-20261020-000000 chunk=1]" in second_text
    )
    assert "first chunk friction" in second_text
    assert second_text.count("[bead ortus-big1]") == 1
    # Only the last chunk's notes are cached.
    record = json.loads(result.notes_file.read_text(encoding="utf-8"))
    assert record["chunks"] == result.chunks
    assert record["lines"] == "101-120"
    assert record["usage"]["input_tokens"] == 100 * result.chunks


def test_chunk_context_guard_refuses_and_warns() -> None:
    with pytest.raises(DreamReaderError, match="600000.*1000000"):
        check_context("claude-haiku-5-5", 600_000)
    with pytest.raises(DreamReaderError, match="200000"):
        check_context("claude-haiku-4-5-20251001", 150_000)
    assert check_context("claude-haiku-5-5", 100_000) is None
    assert "no known context window" in check_context("mystery-model", 100_000)


# --- input_file ------------------------------------------------------------


def test_input_file_carries_window_not_argv(repo: Path) -> None:
    runner = FakeRunner()
    items = _prepared(repo)
    _run(repo, runner, items)

    for call, item in zip(runner.calls, items):
        prompt = call["prompt"]
        path = (repo / INPUTS_DIR / f"{window_key(item.window)}.k1.chunk1.md").resolve()
        assert prompt.startswith("BRIEF")
        assert f"Your input is the file {path}." in prompt
        assert len(prompt.encode("utf-8")) < 32 * 1024
        text = path.read_text(encoding="utf-8")
        assert f"[grind-log bead={item.window.bead_id}" in text
        assert "[/grind-log]" in text
        assert item.window.bead_id in text
        # The transcript itself is not in argv.
        assert "[grind-log" not in prompt


def test_input_file_prompt_stays_small_for_huge_window(repo: Path) -> None:
    runner = FakeRunner()
    big = _big_window(repo)
    huge = PreparedWindow(window=big.window, body="y" * 300_000, cuts=0)
    _run(repo, runner, [huge], chunk_tokens=200_000)

    assert len(runner.calls) == 1
    assert len(runner.calls[0]["prompt"].encode("utf-8")) < 32 * 1024
