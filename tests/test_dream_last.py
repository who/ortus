"""`ortus dream --last N`: the N newest grind runs, however old."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.commands.dream import LAST_AND_SINCE, NOTES_DIR, reader_key
from ortus.core.config import load_config
from ortus.core.profiles import Phase, resolve_dream_profile
from ortus.core.prompts import resolve_named_prompt

FIXTURE = Path(__file__).parent / "fixtures" / "dream" / "grind-20261011-000000.log"

#: Four runs, oldest first. Each gets its own pair of bead ids so the
#: selected runs can be read back from the candidate windows.
RUNS = (
    ("grind-20260101-000000.log", "ortus-r1a", "ortus-r1b"),
    ("grind-20260201-000000.log", "ortus-r2a", "ortus-r2b"),
    ("grind-20260301-000000.log", "ortus-r3a", "ortus-r3b"),
    ("grind-20260401-000000.log", "ortus-r4a", "ortus-r4b"),
)

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
    text = FIXTURE.read_text(encoding="utf-8")
    # Every log is far older than 30 days, and the mtimes run opposite to the
    # names, so ordering by mtime would pick the wrong two.
    for index, (name, first, second) in enumerate(RUNS):
        log = root / "logs" / name
        log.write_text(
            text.replace("ortus-aaa1", first).replace("ortus-bbb2", second),
            encoding="utf-8",
        )
        stamp = time.time() - (60 + index) * 86_400
        os.utime(log, (stamp, stamp))
    return root


def _invoke(repo: Path, *args: str):
    return runner.invoke(app, ["dream", str(repo), "--dry-run", *args])


def _dry(repo: Path, *args: str) -> dict:
    result = _invoke(repo, "--json", *args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])


def _window_beads(repo: Path) -> set[str]:
    windows = (repo / ".ortus" / "dream" / "windows").glob("*.txt")
    return {path.name.split(".")[1] for path in windows}


def test_last_selects_the_newest_runs_by_name(repo: Path) -> None:
    summary = _dry(repo, "--last", "2")

    assert summary["runs_selected"] == 2
    assert summary["last"] == 2
    assert summary["windows_total"] == 4
    assert summary["candidates"] == 4
    # Only the selected runs were prepared, so only their windows are cached.
    assert _window_beads(repo) == {"ortus-r3a", "ortus-r3b", "ortus-r4a", "ortus-r4b"}
    # Every log is past a 30-day window, which is what `--last` is for.
    assert _dry(repo, "--since", "30d")["candidates"] == 0


def test_last_larger_than_the_run_count_takes_every_newest_run(repo: Path) -> None:
    summary = _dry(repo, "--last", "10")

    assert summary["runs_selected"] == 4
    assert summary["candidates"] == 8


def test_last_unstamped_log_falls_back_to_mtime(repo: Path) -> None:
    # The fixture's mtimes run opposite to the names; a name with no run
    # stamp falls back to its mtime and sorts by that alone.
    renamed = repo / "logs" / "grind-manual.log"
    (repo / "logs" / RUNS[0][0]).rename(renamed)
    now = time.time()
    os.utime(renamed, (now, now))

    _dry(repo, "--last", "1")

    assert _window_beads(repo) == {"ortus-r1a", "ortus-r1b"}


def test_last_applies_within_given_logs(repo: Path) -> None:
    logs = repo / "logs"
    summary = _dry(
        repo,
        "--last",
        "1",
        "--logs",
        str(logs / RUNS[0][0]),
        "--logs",
        str(logs / RUNS[1][0]),
    )

    assert summary["runs_selected"] == 1
    assert _window_beads(repo) == {"ortus-r2a", "ortus-r2b"}


def test_last_filters_runs_before_beads(repo: Path) -> None:
    assert _dry(repo, "--last", "2", "--beads", "ortus-r4a")["candidates"] == 1
    assert _dry(repo, "--last", "2", "--beads", "ortus-r1a")["candidates"] == 0


def test_last_overrides_a_dream_table_since(repo: Path) -> None:
    (repo / ".ortusrc").write_text('[dream]\nsince = "7d"\n', encoding="utf-8")

    assert _dry(repo)["candidates"] == 0
    assert _dry(repo, "--last", "2")["candidates"] == 4


def test_last_with_since_both_refused(repo: Path) -> None:
    result = _invoke(repo, "--last", "2", "--since", "7d")

    assert result.exit_code == 2
    assert LAST_AND_SINCE in result.output


def test_last_zero_refused(repo: Path) -> None:
    assert _invoke(repo, "--last", "0").exit_code == 2
    assert _invoke(repo, "--last", "-1").exit_code == 2


def test_last_dry_run_reports_runs_selected(repo: Path) -> None:
    result = _invoke(repo, "--last", "2")

    assert result.exit_code == 0, result.output
    assert "runs selected" in result.stdout
    assert "2 (--last 2)" in result.stdout
    assert "runs selected" not in _invoke(repo).stdout


def test_last_with_no_logs_reports_zero(repo: Path) -> None:
    for log in (repo / "logs").iterdir():
        log.unlink()

    summary = _dry(repo, "--last", "2")

    assert summary["windows_total"] == 0
    assert summary["runs_selected"] == 0


def test_last_reuses_cached_notes_for_selected_runs(repo: Path) -> None:
    _dry(repo, "--last", "4")
    windows = (repo / ".ortus" / "dream" / "windows").glob("*.txt")
    profile = resolve_dream_profile(load_config(repo=repo), "claude", Phase.DREAM_READER)
    key = reader_key("claude", profile, resolve_named_prompt("dream-reader", repo=repo).text)
    notes = repo / NOTES_DIR
    notes.mkdir(parents=True)
    for window in windows:
        bead = window.name.split(".")[1]
        if bead in {"ortus-r4a", "ortus-r1a"}:
            stem = window.name[: -len(".txt")]
            (notes / f"{stem}.{key}.json").write_text("x" * 700, encoding="utf-8")

    summary = _dry(repo, "--last", "2")

    # The newest run's cached note is reused; the oldest run's is not counted.
    assert summary["reused"] == 1
    assert summary["candidates"] == 3
