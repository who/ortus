"""`ortus dream`: selection, caps, refusals and the dry-run report."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands import dream as dream_module
from ortus.commands.dream import NOTES_DIR, REPORT_STUB_MESSAGE, reader_key
from ortus.core.config import load_config
from ortus.core.dream_transcripts import window_key
from ortus.core.grind_logic import grind_flock
from ortus.core.profiles import Phase, resolve_dream_profile
from ortus.core.prompts import resolve_named_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "dream" / "grind-20261011-000000.log"

runner = CliRunner()


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


def _second_log(repo: Path) -> Path:
    log = repo / "logs" / "grind-20261012-000000.log"
    log.write_text(
        FIXTURE.read_text(encoding="utf-8")
        .replace("ortus-aaa1", "ortus-ccc3")
        .replace("ortus-bbb2", "ortus-ddd4"),
        encoding="utf-8",
    )
    return log


def _dry(repo: Path, *args: str) -> dict:
    result = runner.invoke(app, ["dream", str(repo), "--dry-run", "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_dry_run_reports_counts_estimate_and_profiles(repo: Path) -> None:
    summary = _dry(repo)

    assert summary["windows_total"] == 2
    assert summary["candidates"] == 2
    assert summary["reused"] == 0
    assert summary["skipped_unknown_shape"] == 0
    assert summary["bytes"] > 0
    assert summary["chunks"] == 2
    assert summary["est_reader_tokens"] > 0
    assert summary["est_orchestrator_tokens"] == 20_000
    assert summary["over_budget"] is False
    assert "claude-haiku-5-5" in summary["reader_profile"]
    assert "claude-haiku-5-5" in summary["orchestrator_profile"]
    # Preparation cached every window; no notes were written.
    assert list((repo / ".ortus" / "dream" / "windows").glob("*.txt"))
    assert not (repo / NOTES_DIR).exists()


def test_dry_run_human_report_names_the_profiles(repo: Path) -> None:
    result = runner.invoke(app, ["dream", str(repo), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "needing a reader" in result.stdout
    assert "claude-haiku-5-5" in result.stdout


def test_dry_run_with_no_logs_reports_zero(repo: Path) -> None:
    for log in (repo / "logs").iterdir():
        log.unlink()

    summary = _dry(repo)

    assert summary["windows_total"] == 0
    assert summary["candidates"] == 0


def test_dry_run_reuses_cached_notes(repo: Path) -> None:
    summary = _dry(repo, "--beads", "ortus-aaa1")
    assert summary["candidates"] == 1
    windows = sorted((repo / ".ortus" / "dream" / "windows").glob("*.ortus-aaa1.*.txt"))
    assert windows
    stem = windows[0].name[: -len(".txt")]

    # The note is keyed by the reader the CLI resolves: Haiku on Claude with
    # the bundled brief.
    profile = resolve_dream_profile(load_config(repo=repo), "claude", Phase.DREAM_READER)
    key = reader_key("claude", profile, resolve_named_prompt("dream-reader", repo=repo).text)
    notes = repo / NOTES_DIR
    notes.mkdir(parents=True)
    (notes / f"{stem}.{key}.json").write_text("x" * 700, encoding="utf-8")

    summary = _dry(repo)
    assert summary["candidates"] == 1
    assert summary["reused"] == 1
    assert summary["est_orchestrator_tokens"] == 20_000 + 200

    reread = _dry(repo, "--reread")
    assert reread["candidates"] == 2
    assert reread["reused"] == 0


def test_select_narrows_by_bead_and_log(repo: Path) -> None:
    newer = _second_log(repo)

    assert _dry(repo)["candidates"] == 4
    assert _dry(repo, "--beads", "ortus-bbb2")["candidates"] == 1
    assert _dry(repo, "--logs", str(newer))["candidates"] == 2
    assert _dry(repo, "--logs", str(newer), "--beads", "ortus-aaa1")["candidates"] == 0


def test_select_since_compares_log_mtime(repo: Path) -> None:
    old = time.time() - 10 * 86_400
    os.utime(repo / "logs" / FIXTURE.name, (old, old))
    _second_log(repo)

    assert _dry(repo, "--since", "2d")["candidates"] == 2
    assert _dry(repo, "--since", "30d")["candidates"] == 4
    bad = runner.invoke(app, ["dream", str(repo), "--dry-run", "--since", "2w"])
    assert bad.exit_code == 1


def test_select_max_beads_takes_newest_first(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _second_log(repo)
    captured: list = []
    real = dream_module.select_windows

    def spy(*args, **kwargs):
        selection = real(*args, **kwargs)
        captured.append(selection)
        return selection

    monkeypatch.setattr(dream_module, "select_windows", spy)
    summary = _dry(repo, "--max-beads", "3")

    assert summary["candidates"] == 3
    chosen = [item.window for item in captured[0].candidates]
    assert [(w.run, w.bead_id) for w in chosen] == [
        ("grind-20261012-000000", "ortus-ddd4"),
        ("grind-20261012-000000", "ortus-ccc3"),
        ("grind-20261011-000000", "ortus-bbb2"),
    ]
    assert window_key(chosen[0]).startswith("grind-20261012-000000.ortus-ddd4.")


def test_select_max_beads_from_dream_table(repo: Path) -> None:
    (repo / ".ortusrc").write_text("[dream]\nmax_beads = 1\n", encoding="utf-8")

    assert _dry(repo)["candidates"] == 1
    assert _dry(repo, "--max-beads", "2")["candidates"] == 2


def test_refuse_when_lock_held(repo: Path) -> None:
    with grind_flock(repo):
        result = runner.invoke(app, ["dream", str(repo), "--dry-run"])

    assert result.exit_code == 2
    assert "ortus.flock" in result.output


def test_refuse_when_cache_not_ignored(repo: Path) -> None:
    (repo / ".gitignore").write_text("logs/\n", encoding="utf-8")

    result = runner.invoke(app, ["dream", str(repo), "--dry-run"])

    assert result.exit_code == 2
    assert ".ortus/dream" in result.output
    assert not (repo / ".ortus" / "dream").exists()


def test_refuse_over_budget_in_a_real_run(repo: Path) -> None:
    result = runner.invoke(app, ["dream", str(repo), "--budget-tokens", "10"])
    assert result.exit_code == 2
    assert "budget" in result.output

    summary = _dry(repo, "--budget-tokens", "10")
    assert summary["over_budget"] is True


def test_real_run_stops_at_the_report_stub(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Reader:
        def run(self, prompt, *, log_path, **kwargs):
            log_path.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": 'ORTUS_DREAM_WINDOW: {"outcome": "ok", "notes": 0}',
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return 0

        def preflight_readonly(self, repo, **kwargs):
            return None

    monkeypatch.setattr(dream_module, "make_runner", lambda backend, repo=None: Reader())
    monkeypatch.setattr(
        dream_module, "load_bead_header", lambda bd, bead: f"[bead {bead}]\n[/bead]"
    )
    monkeypatch.setattr(dream_module, "run_orchestrator", lambda **kwargs: None)
    monkeypatch.setattr(dream_module, "validate_run", lambda *args, **kwargs: None)
    result = runner.invoke(app, ["dream", str(repo)])

    assert result.exit_code == 1
    assert REPORT_STUB_MESSAGE in result.output
    assert len(list((repo / NOTES_DIR).glob("*.json"))) == 2


def test_real_run_with_no_logs_exits_nothing(repo: Path) -> None:
    for log in (repo / "logs").iterdir():
        log.unlink()

    result = runner.invoke(app, ["dream", str(repo)])

    assert result.exit_code == 3


def test_bad_dream_key_fails_the_run(repo: Path) -> None:
    (repo / ".ortusrc").write_text("[dream]\nmax_bead = 3\n", encoding="utf-8")

    result = runner.invoke(app, ["dream", str(repo), "--dry-run"])

    assert result.exit_code == 1
    assert "max_bead" in result.output
