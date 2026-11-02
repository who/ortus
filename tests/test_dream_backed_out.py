"""Dream reads only work that reached the default branch and still exists at HEAD."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ortus.commands.dream as dream_module
from ortus.cli import app
from ortus.core.config import load_config
from ortus.core.dream_orchestrator import (
    Followup,
    OrchestratorResult,
    Proposal,
    notes_section,
)
from ortus.core.dream_report import render_report
from ortus.core.dream_transcripts import BeadWindow, PreparedWindow, window_label
from ortus.core.dream_validate import DROP_STALE_REF, validate

FIXTURE = Path(__file__).parent / "fixtures" / "dream" / "grind-20261011-000000.log"
MERGED = "ortus-aaa1"
BACKED_OUT = "ortus-bbb2"
QUOTE = "ModuleNotFoundError: No module named 'typer'"

runner = CliRunner()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Bead A merged on main; bead B committed on a branch that was deleted."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("ORTUS_BACKEND", raising=False)
    root = tmp_path / "repo"
    (root / ".beads").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "src").mkdir()
    (root / ".gitignore").write_text(".ortus/dream/\nlogs/\n.beads/\n", encoding="utf-8")
    shutil.copy(FIXTURE, root / "logs" / FIXTURE.name)

    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "Initial commit")
    (root / "src" / "thing.py").write_text("def present_symbol():\n    return 1\n")
    _git(root, "add", "src/thing.py")
    _git(root, "commit", "-q", "-m", f"{MERGED}: Add the thing", "-m", f"Ortus-Issue: {MERGED}")
    _git(root, "checkout", "-q", "-b", "spike")
    (root / "src" / "spike.py").write_text("def spike_symbol():\n    return 2\n")
    _git(root, "add", "src/spike.py")
    _git(root, "commit", "-q", "-m", f"{BACKED_OUT}: Try a spike")
    _git(root, "checkout", "-q", "main")
    _git(root, "branch", "-q", "-D", "spike")
    return root


def _selection(repo: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    captured: list = []
    real = dream_module.select_windows

    def spy(*a, **kw):
        selection = real(*a, **kw)
        captured.append(selection)
        return selection

    monkeypatch.setattr(dream_module, "select_windows", spy)
    result = runner.invoke(app, ["dream", str(repo), "--dry-run", "--json", *args])
    assert result.exit_code == 0, result.output
    return captured[0], json.loads(result.stdout.strip().splitlines()[-1])


# --- window selection --------------------------------------------------------


def test_only_main_windows_are_selected(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection, summary = _selection(repo, monkeypatch)

    assert [item.window.bead_id for item in selection.candidates] == [MERGED]
    assert summary["windows_total"] == 2
    assert summary["skipped_by_reason"] == {"not-on-main": 1}
    assert summary["not_on_main"] == [BACKED_OUT]
    assert summary["reachability"] == "main"


def test_only_main_dry_run_row_names_the_skip(repo: Path) -> None:
    result = runner.invoke(app, ["dream", str(repo), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "skipped (not on main)" in result.stdout
    assert "reachability" in result.stdout


def test_include_unmerged_selects_both_windows(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection, summary = _selection(repo, monkeypatch, "--include-unmerged")

    assert sorted(item.window.bead_id for item in selection.candidates) == [
        MERGED,
        BACKED_OUT,
    ]
    assert summary["not_on_main"] == []
    assert summary["skipped_by_reason"] == {}
    assert summary["reachability"] == dream_module.REACH_BYPASSED


def test_only_main_kept_under_reread(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection, _ = _selection(repo, monkeypatch, "--reread")

    assert [item.window.bead_id for item in selection.candidates] == [MERGED]


# --- HEAD references ---------------------------------------------------------


def _window(bead: str, run: str, body: str) -> PreparedWindow:
    return PreparedWindow(
        window=BeadWindow(
            log_path=Path(f"logs/{run}.log"),
            run=run,
            bead_id=bead,
            start_line=1,
            end_line=1 + body.count("\n") + 1,
            backend="claude",
            lines=(),
        ),
        body=body,
        cuts=0,
    )


def _record(item: PreparedWindow) -> dict:
    window = item.window
    return {
        "bead": window.bead_id,
        "run": window.run,
        "lines": f"{window.start_line}-{window.end_line}",
        "notes": [
            {
                "issue": "bare pytest",
                "evidence": [{"label": window_label(window), "quote": QUOTE}],
                "cost": {"turns": 2, "tokens": None},
                "memory_would_help": True,
                "kind_hint": "memory",
            }
        ],
    }


def _proposal(key: str, body: str, sources: tuple[str, ...]) -> Proposal:
    return Proposal(
        kind="add",
        key=key,
        body=body,
        pattern="the same mistake twice",
        why="two beads lost turns to it",
        beads=("ortus-a", "ortus-b"),
        sources=sources,
        quotes=(QUOTE,),
        stats={"beads": 2, "windows": 2, "turns_wasted": 4},
    )


def _validate(repo: Path, proposals=(), followups=()):
    windows = [
        _window("ortus-a", "grind-20261001-100000", f"step\n{QUOTE}"),
        _window("ortus-b", "grind-20261002-100000", f"x\n{QUOTE}"),
    ]
    records = [_record(item) for item in windows]
    labels = tuple(sorted(notes_section(record).label for record in records))
    result = OrchestratorResult(
        proposals=tuple(_proposal(*p, labels) for p in proposals),
        followups=tuple(
            Followup(title=t, why=w, beads=("ortus-a",), sources=labels)
            for t, w in followups
        ),
        helped=(),
        invalid_lines=0,
        input_labels=frozenset(labels),
        dropped_notes=0,
        usage=None,
        cost_usd=None,
        needs_answered=0,
    )
    return validate(
        result,
        windows=windows,
        records=records,
        memories={},
        decisions=(),
        min_beads=2,
        refs=dream_module.head_refs(repo),
    )


STALE = ("gone-path", "Edit `src/gone.py` before running the suite.")
LIVE = ("live-path", "Call `present_symbol` in `src/thing.py`, never `bd remember x`.")


def test_stale_proposal_dropped_and_live_one_kept(repo: Path) -> None:
    validation = _validate(repo, proposals=[STALE, LIVE])

    assert [kept.proposal.key for kept in validation.proposals] == ["live-path"]
    assert ("add gone-path", DROP_STALE_REF) in validation.drops
    assert validation.stale_refs == (("add gone-path", ("src/gone.py",)),)


def test_stale_proposal_backed_out_symbol_dropped(repo: Path) -> None:
    validation = _validate(
        repo, proposals=[("spike-sym", "Reuse `spike_symbol` for the spike.")]
    )

    assert validation.proposals == ()
    assert validation.stale_refs == (("add spike-sym", ("spike_symbol",)),)


def test_stale_followup_kept_and_marked(repo: Path) -> None:
    validation = _validate(
        repo,
        followups=[
            ("Fix `vanished_symbol`", "it misreads `src/thing.py`"),
            ("Keep `present_symbol`", "fine"),
        ],
    )

    assert [f.title for f in validation.followups] == [
        "Fix `vanished_symbol`",
        "Keep `present_symbol`",
    ]
    assert validation.followups[0].why.endswith("(names missing: vanished_symbol)")
    assert validation.followups[1].why == "fine"
    assert validation.drops == ()


class _Client:
    def memories(self) -> dict[str, str]:
        return {}

    def prime(self) -> str:
        return "prime"


def test_run_facts_list_not_on_main_and_stale_refs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection, _ = _selection(repo, monkeypatch)
    validation = _validate(repo, proposals=[STALE, LIVE])
    profile = dream_module.resolve_dream_profile(
        load_config(repo=repo), "claude", dream_module.Phase.DREAM_READER
    )
    facts = dream_module.run_facts(
        _Client(),
        backend="claude",
        reader=profile,
        orchestrator=profile,
        selection=selection,
        readers=[],
        windows_fed=1,
        result=OrchestratorResult(
            proposals=(),
            followups=(),
            helped=(),
            invalid_lines=0,
            input_labels=frozenset(),
            dropped_notes=0,
            usage=None,
            cost_usd=None,
            needs_answered=0,
        ),
    )

    report = render_report(validation, facts, "ortus-dream1")
    section = report.split("## Run facts", 1)[1].split("## How to respond", 1)[0]
    assert "- reachability: main" in section
    assert f"- skipped not-on-main: {BACKED_OUT}" in section
    assert "- stale-ref drop: add gone-path (missing: src/gone.py)" in section
    assert "stale-ref=1" in section


def test_run_facts_without_git_read_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert dream_module.merged_beads(tmp_path) == (dream_module.REACH_UNAVAILABLE, None)
    assert dream_module.head_refs(tmp_path) is None
