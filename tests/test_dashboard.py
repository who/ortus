"""`ortus dashboard` shell: read-only posture, idle start, and visual contract.

Read-only is the load-bearing property, so it is asserted three ways rather
than documented once: the argument vector of every bd call, the fact that a
refresh cycle leaves the worktree byte-identical, and a syntax-tree walk that
fails if a `"bd"` or `"git"` literal ever appears outside the single gateway.
The last one is the guard that binds the panel leaves still to come — a
convention is exactly what a later panel would forget, and a write into a
worktree a worker owns silently becomes that worker's candidate.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from typer.models import OptionInfo

from ortus.commands import dashboard as dash
from ortus.commands import grind
from ortus.core.runstate import (
    LogEvent,
    RunSnapshot,
    RunWarning,
    classify_line,
    read_snapshot,
)
from ortus.core.verdict import Verdict, render_report

_MODULE_SOURCE = Path(dash.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _quiet_repo(tmp_path: Path) -> Path:
    """A repository with no bd workspace and no grind log."""

    repo = tmp_path / "quiet"
    repo.mkdir()
    return repo


def _ortus_line(stamp: str, message: str) -> str:
    return f"[{stamp}] {message}\n"


def _live_repo(tmp_path: Path) -> Path:
    """A repository mid-run: a grind log with a claim line and no journal file."""

    repo = tmp_path / "live"
    (repo / "logs").mkdir(parents=True)
    (repo / ".beads").mkdir()
    (repo / "logs" / "grind-20260808-221000.log").write_text(
        _ortus_line("2026-08-08 22:10:00", "iter 1: goal-prompt ready for ortus-0udo.2 (claude)")
        + _ortus_line("2026-08-08 22:10:00", "iter 1: worker started")
        + '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"reading the packet"}]}}\n',
        encoding="utf-8",
    )
    return repo


def _drive(
    app: dash.DashboardApp,
    steps: Callable[[Any], Awaitable[None]],
    *,
    size: tuple[int, int] = (100, 40),
) -> None:
    """Run `app` headless and hand the pilot to `steps`.

    Textual's harness is async and this suite is not, so each test hands in a
    coroutine rather than the whole file taking on an async plugin.
    """

    async def _main() -> None:
        async with app.run_test(size=size) as pilot:
            await steps(pilot)

    asyncio.run(_main())


def _screen_text(app: dash.DashboardApp) -> str:
    """Every character the dashboard actually paints.

    The composited screen is the only surface that proves what an operator
    sees. Textual exports a screenshot as SVG but has no plain-text export, so
    this reads the compositor that the screenshot itself renders from.
    """

    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


# Codepoints that render as a pictograph rather than as a glyph the layout can
# rely on. Deliberately wide: the point is that a single decorative character
# added later fails here rather than passing every other check.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1FAFF),  # emoticons, transport, symbols & pictographs
    (0x1F1E6, 0x1F1FF),  # regional indicators (flags)
    (0x2600, 0x27BF),  # miscellaneous symbols and dingbats
    (0x2B00, 0x2BFF),  # miscellaneous symbols and arrows
    (0xFE0F, 0xFE0F),  # emoji presentation selector
)
_EMOJI_SINGLES = frozenset(
    {0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
)


def _emoji_in(text: str) -> list[str]:
    found = []
    for char in text:
        point = ord(char)
        if point in _EMOJI_SINGLES or any(
            low <= point <= high for low, high in _EMOJI_RANGES
        ):
            found.append(f"U+{point:04X} {char!r}")
    return found


def _fingerprint(repo: Path) -> dict[str, str]:
    """Content hash of every path under `repo`, so a rewrite is not mistaken
    for a no-op the way an mtime comparison would be."""

    prints: dict[str, str] = {}
    for path in sorted(repo.rglob("*")):
        key = str(path.relative_to(repo))
        if path.is_dir():
            prints[key + "/"] = "dir"
        else:
            prints[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return prints


class _LiteralSites(ast.NodeVisitor):
    """Names the function enclosing every occurrence of one string literal."""

    def __init__(self, literal: str) -> None:
        self.literal = literal
        self._stack: list[str] = []
        self.sites: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value == self.literal:
            self.sites.append(self._stack[-1] if self._stack else "<module>")


def _literal_sites(literal: str) -> list[str]:
    visitor = _LiteralSites(literal)
    visitor.visit(ast.parse(_MODULE_SOURCE))
    return visitor.sites


# ---------------------------------------------------------------------------
# AC-2: every bd invocation is read-only
# ---------------------------------------------------------------------------


def test_dashboard_module_does_not_import_journal_types() -> None:
    """AC-2: the dashboard command module never names JournalStore or CandidateJournal."""

    assert "JournalStore" not in _MODULE_SOURCE
    assert "CandidateJournal" not in _MODULE_SOURCE
    assert "JOURNAL_RELATIVE_PATH" not in _MODULE_SOURCE


def test_readonly_bd_flags_on_every_bd_invocation(
    tmp_path: Path, monkeypatch
) -> None:
    """AC-2: the flags are pinned, and there is nowhere else to build a bd call."""

    assert dash.bd_argv() == ["bd", "--readonly", "--sandbox"]
    assert dash.bd_argv("show", "ortus-0udo.2", "--json") == [
        "bd",
        "--readonly",
        "--sandbox",
        "show",
        "ortus-0udo.2",
        "--json",
    ]

    calls: list[dict[str, Any]] = []

    def _record(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        calls.append({"argv": argv, "kwargs": kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(dash.subprocess, "run", _record)

    repo = _quiet_repo(tmp_path)
    assert dash.run_bd(repo, "show", "ortus-0udo.2", "--json") == "{}"
    assert dash.run_bd(repo, "memories", "--json") == "{}"

    assert calls, "run_bd did not reach subprocess"
    for call in calls:
        argv = call["argv"]
        assert argv[0] == "bd"
        assert argv[1:3] == list(dash.BD_READONLY_FLAGS), argv
        assert call["kwargs"]["cwd"] == str(repo)
        # A read-only query must never inherit a writable stdin pipe or be
        # allowed to run unbounded.
        assert call["kwargs"]["timeout"] > 0

    # The gateway is the only place a bd argv exists, so a panel leaf added
    # later cannot quietly assemble one without these flags. Nothing here may
    # shell out to git at all.
    assert _literal_sites("bd") == ["bd_argv"], _literal_sites("bd")
    assert _literal_sites("git") == []


def test_bd_query_failure_degrades_to_none(tmp_path: Path, monkeypatch) -> None:
    """bd being unavailable degrades the panel that asked, never the view."""

    repo = _quiet_repo(tmp_path)

    def _fails(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="nope")

    monkeypatch.setattr(dash.subprocess, "run", _fails)
    assert dash.run_bd(repo, "show", "x") is None

    def _missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise FileNotFoundError("bd")

    monkeypatch.setattr(dash.subprocess, "run", _missing)
    assert dash.run_bd(repo, "show", "x") is None


# ---------------------------------------------------------------------------
# AC-3: a refresh cycle writes nothing
# ---------------------------------------------------------------------------


def test_refresh_cycle_performs_no_writes_to_the_repository(tmp_path: Path) -> None:
    """AC-3: the worktree is byte-identical after a full refresh cycle."""

    repo = _live_repo(tmp_path)
    before = _fingerprint(repo)
    assert before, "fixture repository is empty"

    app = dash.DashboardApp(repo, refresh_seconds=3600)

    async def _steps(pilot: Any) -> None:
        for _ in range(3):
            app.refresh_run()
            await pilot.pause()

    _drive(app, _steps)

    after = _fingerprint(repo)
    assert after == before
    # The refresh really ran against this repository, so the comparison above
    # is not vacuously true of a view that never read anything.
    assert app.tick >= 4
    assert app.snapshot.issue_id == "ortus-0udo.2"


def test_refresh_cycle_performs_no_writes_when_the_repository_is_empty(
    tmp_path: Path,
) -> None:
    """AC-3: an idle repository is not seeded with a logs/ tree by being watched."""

    repo = _quiet_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    for _ in range(3):
        app.advance()
    assert _fingerprint(repo) == {}


# ---------------------------------------------------------------------------
# AC-4: starts and exits cleanly against a repository with no grind log
# ---------------------------------------------------------------------------


def test_app_starts_idle_and_exits_on_q(tmp_path: Path) -> None:
    """AC-4: no journal and no log is a valid state, and q ends the session."""

    repo = _quiet_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    seen: dict[str, Any] = {}

    async def _steps(pilot: Any) -> None:
        header = app.query_one("#header", dash.Region)
        seen["header"] = header.body
        seen["state"] = header.has_class("state-idle")
        seen["regions"] = [region.spec.key for region in app.query(dash.Region)]
        await pilot.press("q")
        await pilot.pause()
        seen["running"] = app.is_running

    _drive(app, _steps)

    assert "idle" in seen["header"]
    assert seen["state"] is True
    assert seen["regions"] == [spec.key for spec in dash.REGIONS]
    assert seen["running"] is False
    assert app.return_code == 0


def test_starts_idle_in_a_cramped_terminal(tmp_path: Path) -> None:
    """A terminal too small for the layout degrades rather than crashing."""

    repo = _quiet_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)

    painted: list[str] = []

    async def _steps(pilot: Any) -> None:
        app.refresh_run()
        await pilot.pause()
        painted.append(_screen_text(app))
        await pilot.press("q")
        await pilot.pause()

    _drive(app, _steps, size=(20, 6))
    assert painted and painted[0].strip(), "nothing rendered in a cramped terminal"
    assert app.return_code == 0


def test_shell_names_every_region_of_the_layout(tmp_path: Path) -> None:
    """Step 3: the five agreed regions exist, titled, for their leaves to fill."""

    assert [spec.key for spec in dash.REGIONS] == [
        "header",
        "current-action",
        "candidate",
        "verdict",
        "warnings",
    ]
    repo = _quiet_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    titles: dict[str, Any] = {}

    async def _steps(pilot: Any) -> None:
        for region in app.query(dash.Region):
            titles[region.spec.key] = (region.border_title, region.body)

    _drive(app, _steps)

    assert set(titles) == {spec.key for spec in dash.REGIONS}
    for spec in dash.REGIONS:
        assert titles[spec.key][0] == spec.title
        assert titles[spec.key][1]
    # Current action is filled by ortus-0udo.4, and a quiet repository has no
    # transaction in flight, so it says so rather than showing an old action.
    assert titles["current-action"][1] == dash.ACTION_IDLE
    # Candidate is filled by ortus-0udo.5, and a quiet repository has no journal,
    # so nothing was ever captured rather than a candidate holding nothing.
    assert titles["candidate"][1] == dash.CANDIDATE_IDLE
    # Warnings is filled by this leaf, so it states a finding rather than
    # holding space: a quiet region must read as "nothing fired".
    assert titles["warnings"][1] == dash.NO_WARNINGS
    # Verdict is filled by ortus-0udo.6, and a quiet repository has no run to
    # judge, which is a state rather than a pending panel.
    assert titles["verdict"][1] == dash.VERDICT_IDLE


def test_refresh_carries_the_offset_between_ticks(tmp_path: Path) -> None:
    """Step 4: a tick reads what the log grew by, not the whole log again."""

    repo = _live_repo(tmp_path)
    log = repo / "logs" / "grind-20260808-221000.log"
    app = dash.DashboardApp(repo, refresh_seconds=3600)

    app.advance()
    first_offset = app.snapshot.offset
    assert first_offset == log.stat().st_size

    app.advance()
    assert app.snapshot.offset == first_offset
    assert app.snapshot.events == (), "re-read a log that had not grown"

    with log.open("a", encoding="utf-8") as handle:
        handle.write("[2026-08-08 22:12:00] iter 1: verification\n")
    app.advance()
    assert app.snapshot.offset > first_offset
    assert len(app.snapshot.events) == 1


def test_a_run_that_starts_while_open_is_picked_up_on_the_next_tick(
    tmp_path: Path,
) -> None:
    """The dashboard may be opened before the grind it is there to watch."""

    repo = tmp_path / "later"
    repo.mkdir()
    app = dash.DashboardApp(repo, refresh_seconds=3600)

    assert app.advance().header == dash.HEADER_IDLE
    assert app.snapshot.idle

    (repo / "logs").mkdir()
    (repo / "logs" / "grind-20260808-221000.log").write_text(
        _ortus_line(
            "2026-08-08 22:10:00",
            "iter 1: goal-prompt ready for ortus-0udo.2 (claude)",
        )
        + _ortus_line("2026-08-08 22:10:00", "iter 1: spawning claude (single-issue worker)"),
        encoding="utf-8",
    )

    header = app.advance().header
    assert "ortus-0udo.2" in header
    assert "implementation" in header
    assert dash.region_state("header", app.snapshot) == "state-live"


def test_a_run_that_ends_while_open_shows_its_terminal_state(tmp_path: Path) -> None:
    """A finished run reports how it ended rather than freezing on the last frame."""

    repo = _live_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    app.advance()
    assert not app.snapshot.terminal
    assert dash.region_state("header", app.snapshot) == "state-live"

    log = next((repo / "logs").glob("grind-*.log"))
    with log.open("a", encoding="utf-8") as handle:
        handle.write(_ortus_line("2026-08-08 22:40:00", "iter 1: worker closed ortus-0udo.2"))

    app.advance()
    assert app.snapshot.terminal
    assert dash.region_state("header", app.snapshot) == "state-ended"
    assert "ended" in dash.header_line(app.snapshot)


# ---------------------------------------------------------------------------
# AC-5: the visual contract
# ---------------------------------------------------------------------------


def test_visual_contract_dark_no_emoji_and_moving(tmp_path: Path) -> None:
    """AC-5: no emoji anywhere, a dark default, and something that moves."""

    # The detector fires on a real pictograph, so "no emoji found" below is a
    # finding rather than a detector that never matches anything.
    assert _emoji_in("\N{ROCKET}\N{WHITE HEAVY CHECK MARK}")

    # No emoji in the source, so a decorative glyph added to a string a test
    # does not happen to render still fails.
    assert _emoji_in(_MODULE_SOURCE) == []
    # Box-drawing and block elements are the intended vocabulary and must not
    # trip the detector, or the contract would forbid the instrument panel.
    assert _emoji_in("─│╭╮╰╯█░") == []

    for repo in (_quiet_repo(tmp_path), _live_repo(tmp_path)):
        app = dash.DashboardApp(repo, refresh_seconds=3600)
        frames: list[str] = []
        seen: dict[str, Any] = {}

        async def _steps(pilot: Any, app: Any = app, frames: Any = frames) -> None:
            seen["theme"] = app.theme
            seen["dark"] = app.current_theme.dark
            frames.append(_screen_text(app))
            app.refresh_run()
            await pilot.pause()
            frames.append(_screen_text(app))

        _drive(app, _steps)

        # Dark by default: the palette is chosen for contrast against a dark
        # ground and a light terminal would wash the accents out.
        assert seen["theme"] == dash.DARK_THEME
        assert seen["dark"] is True

        for painted in frames:
            assert _emoji_in(painted) == [], painted

        # Something visibly moves on every refresh, so a healthy run looks
        # alive and a stalled one is obvious by its stillness.
        assert frames[0] != frames[1]
        # The way out is on screen rather than in the docstring, and it says so
        # in words because a pictograph is exactly what this contract forbids.
        assert any(dash.KEY_HINT in line for line in frames[1].splitlines())


def test_visual_contract_pulse_advances_every_tick() -> None:
    """AC-5: the moving element is a pure function of the tick, not the clock.

    Position from the tick rather than the wall clock is what makes stillness
    on screen mean "the dashboard stopped" rather than "the run went quiet."
    """

    marks = [dash.pulse(tick).index(dash._PULSE_MARK) for tick in range(6)]
    assert len(set(marks)) > 1
    for earlier, later in zip(marks, marks[1:]):
        assert earlier != later
    assert all(len(dash.pulse(tick)) == dash.PULSE_WIDTH for tick in range(50))

    snapshot = RunSnapshot()
    assert dash.pulse_line(snapshot, 1) != dash.pulse_line(snapshot, 2)


# ---------------------------------------------------------------------------
# ortus-0udo.7 AC-1/AC-2: warnings, with the ortus line as evidence
# ---------------------------------------------------------------------------

#: The line grind writes when the watchdog kills a worker mid-flight, copied
#: from its own `write_log` call so the fixture cannot drift from reality.
_WATCHDOG_LINE = "iter 1: worker TIMEOUT after 1800s, killed (rc=143)"


def _warned_repo(tmp_path: Path, name: str, body: str) -> Path:
    """A repository whose grind log is exactly `body`."""

    repo = tmp_path / name
    (repo / "logs").mkdir(parents=True)
    (repo / "logs" / "grind-20260808-221000.log").write_text(body, encoding="utf-8")
    return repo


def test_warning_real_timeout_shows_one_warning_with_its_ortus_line(
    tmp_path: Path,
) -> None:
    """AC-1: a real watchdog kill is one warning, carrying the line that said so."""

    repo = _warned_repo(
        tmp_path,
        "killed",
        "[2026-08-08 22:10:00] iter 1: worker started\n"
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"running the suite"}]}}\n'
        f"[2026-08-08 22:40:00] {_WATCHDOG_LINE}\n",
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    panel = app.advance().warnings

    assert app.snapshot.warning_counts == {"timeout": 1}
    # A count alone is what made the stopgap script untrustworthy, so the line
    # an operator would judge the claim by is on screen with it.
    assert "timeout 1" in panel
    assert _WATCHDOG_LINE in panel
    assert "22:40:00" in panel
    assert dash.region_state("warnings", app.snapshot) == "state-failed"


def test_warning_appears_live_rather_than_only_at_the_end_of_a_run(
    tmp_path: Path,
) -> None:
    """Step 2: the warning shows on the tick after it is written, run still live."""

    repo = _warned_repo(
        tmp_path, "live-warning", "[2026-08-08 22:10:00] iter 1: worker started\n"
    )
    log = repo / "logs" / "grind-20260808-221000.log"
    app = dash.DashboardApp(repo, refresh_seconds=3600)

    assert app.advance().warnings == dash.NO_WARNINGS
    assert dash.region_state("warnings", app.snapshot) == "state-idle"

    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"[2026-08-08 22:40:00] {_WATCHDOG_LINE}\n")

    panel = app.advance().warnings
    assert _WATCHDOG_LINE in panel
    # The run has not ended: the point is seeing the failure develop.
    assert not app.snapshot.terminal
    assert dash.region_state("warnings", app.snapshot) == "state-failed"


def test_warning_no_false_positive_when_agent_content_quotes_the_vocabulary(
    tmp_path: Path,
) -> None:
    """AC-2: a worker editing the recovery code produces no warnings at all.

    This is the exact failure the stopgap script had: it matched the transcript
    rather than the ortus lines and reported seven timeouts that never
    happened. The discrimination lives in the model; this asserts the panel
    inherits it rather than re-deriving it.
    """

    repo = _warned_repo(
        tmp_path,
        "quoting",
        "[2026-08-08 22:10:00] iter 1: worker started\n"
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"text","text":"editing recovery code: '
        f'{_WATCHDOG_LINE}"}}]}}}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","name":"Edit","input":'
        '{"file_path":"grind.py","description":"correction escalation, '
        'attempts exhausted, plan gap, HALT"}}]}}\n'
        '{"type":"user","message":{"role":"user","content":"rejected"}}\n',
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    frame = app.advance()

    assert app.snapshot.warnings == ()
    assert app.snapshot.warning_counts == {}
    assert frame.warnings == dash.NO_WARNINGS
    assert dash.region_state("warnings", app.snapshot) == "state-idle"


def test_warning_panel_counts_every_kind_and_elides_the_oldest_evidence() -> None:
    """Counts cover the run; the evidence window shows the newest and says so."""

    warnings = tuple(
        RunWarning(kind="timeout", text=f"iter {i}: worker TIMEOUT after 1800s")
        for i in range(dash.WARNING_EVIDENCE_LINES + 3)
    ) + (RunWarning(kind="escalation", text="correction escalation recorded"),)
    snapshot = RunSnapshot(journal_present=True, warnings=warnings)

    panel = dash.warnings_panel(snapshot)
    assert dash.warning_summary(snapshot) == (
        f"escalation 1   timeout {dash.WARNING_EVIDENCE_LINES + 3}"
    )
    elided = len(warnings) - dash.WARNING_EVIDENCE_LINES
    assert f"({elided} earlier not shown)" in panel
    # Nothing is dropped silently: the elided ones are still in the counts.
    assert len(dash.warning_evidence(snapshot)) == dash.WARNING_EVIDENCE_LINES
    assert "correction escalation recorded" in panel
    assert "iter 0:" not in panel

    long_line = "x" * (dash.WARNING_TEXT_CHARS * 2)
    clipped = dash.warnings_panel(
        RunSnapshot(warnings=(RunWarning(kind="halt", text=long_line),))
    )
    assert all(len(line) < dash.WARNING_TEXT_CHARS + 40 for line in clipped.splitlines())


# ---------------------------------------------------------------------------
# ortus-0udo.7 AC-3: replay of a finished run
# ---------------------------------------------------------------------------


def _finished_run(
    tmp_path: Path,
    name: str,
    *,
    phase: str = "finalized-verified",
    log_name: str = "grind-20260808-221000.log",
    body: str | None = None,
) -> tuple[Path, Path]:
    """A repository holding one finished run; returns the repo and its log."""

    repo = tmp_path / name
    (repo / "logs").mkdir(parents=True)
    log = repo / "logs" / log_name
    if body is None:
        body = (
            _ortus_line(
                "2026-08-08 22:10:00",
                "iter 1: goal-prompt ready for ortus-0udo.7 (claude)",
            )
            + _ortus_line("2026-08-08 22:10:00", "iter 1: worker started")
            + _ortus_line("2026-08-08 22:40:00", _WATCHDOG_LINE)
            + _ortus_line("2026-08-08 22:40:01", f"iter 1: step {phase}")
        )
    elif f"step {phase}" not in body and "goal-prompt ready" not in body:
        body = (
            _ortus_line(
                "2026-08-08 22:10:00",
                "iter 1: goal-prompt ready for ortus-0udo.7 (claude)",
            )
            + body
            + _ortus_line("2026-08-08 22:40:01", f"iter 1: step {phase}")
        )
    log.write_text(body, encoding="utf-8")
    return repo, log


def test_replay_finished_run_renders_the_same_panels_and_how_it_ended(
    tmp_path: Path,
) -> None:
    """AC-3: replay is the live view pointed at a finished run, plus its ending."""

    repo, log = _finished_run(tmp_path, "done", phase="corrections-exhausted")
    before = _fingerprint(repo)
    source = dash.resolve_replay(log)
    assert source.repo == repo

    app = dash.DashboardApp(source.repo, refresh_seconds=3600, replay=source)
    seen: dict[str, Any] = {}

    async def _steps(pilot: Any) -> None:
        app.refresh_run()
        await pilot.pause()
        seen["regions"] = {
            region.spec.key: (region.border_title, region.body)
            for region in app.query(dash.Region)
        }
        seen["screen"] = _screen_text(app)

    _drive(app, _steps)

    # The same panels, titled the same way: one renderer, two sources.
    assert set(seen["regions"]) == {spec.key for spec in dash.REGIONS}
    for spec in dash.REGIONS:
        assert seen["regions"][spec.key][0] == spec.title

    header = seen["regions"]["header"][1]
    assert "replay" in header
    assert "ortus-0udo.7" in header
    # Two runs share a log directory, so the filename is what names this one.
    assert log.name in header
    # How it ended, and at which phase.
    assert "correction attempts exhausted" in header
    assert "corrections-exhausted" in header

    assert _WATCHDOG_LINE in seen["regions"]["warnings"][1]
    assert app.snapshot.terminal
    assert dash.region_state("header", app.snapshot) == "state-ended"
    # A replayed run must never write to the repository, exactly as live does not.
    assert _fingerprint(repo) == before


def test_replay_of_a_clean_run_does_not_invent_a_terminal_failure(
    tmp_path: Path,
) -> None:
    """A run that ended cleanly says so rather than being given a failure."""

    repo, log = _finished_run(
        tmp_path,
        "clean",
        body="[2026-08-08 22:10:00] iter 1: worker started\n"
        "[2026-08-08 22:40:00] iter 1: verified\n",
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))
    frame = app.advance()

    assert dash.CLEAN_OUTCOME in frame.header
    assert "finalized-verified" in frame.header
    assert frame.warnings == dash.NO_WARNINGS
    for failed in dash.OUTCOMES.values():
        assert failed not in frame.header


def test_replay_renders_an_unknown_terminal_phase_verbatim(tmp_path: Path) -> None:
    """An older run whose vocabulary has since changed renders what it can."""

    repo, log = _finished_run(tmp_path, "old", phase="retired-phase-name")
    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))
    assert "retired-phase-name" in app.advance().header


def test_replay_of_a_run_whose_journal_is_gone_renders_from_the_log_alone(
    tmp_path: Path,
) -> None:
    """A leftover journal is ignored; replay still reads the grind log."""

    repo, log = _finished_run(tmp_path, "orphan-log")
    leftover = repo / "logs" / "grind-transaction.json"
    leftover.write_text('{"issue_id": "should-be-ignored"}', encoding="utf-8")

    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))
    frame = app.advance()

    assert not app.snapshot.idle
    assert leftover.is_file()
    assert "ortus-0udo.7" in frame.header
    assert log.name in frame.header
    # The warnings still come off the log, which is the point of replaying it.
    assert _WATCHDOG_LINE in frame.warnings


def test_replay_distinguishes_two_runs_sharing_one_log_directory(
    tmp_path: Path,
) -> None:
    """Filename is what tells two runs in one logs/ tree apart."""

    repo, first = _finished_run(tmp_path, "pair", log_name="grind-20260808-100000.log")
    second = repo / "logs" / "grind-20260808-220000.log"
    second.write_text(
        "[2026-08-08 22:00:00] iter 1: correction attempts exhausted\n",
        encoding="utf-8",
    )

    headers = {}
    panels = {}
    for log in (first, second):
        app = dash.DashboardApp(
            repo, refresh_seconds=3600, replay=dash.resolve_replay(log)
        )
        frame = app.advance()
        headers[log.name] = frame.header
        panels[log.name] = frame.warnings

    assert first.name in headers[first.name]
    assert second.name in headers[second.name]
    assert headers[first.name] != headers[second.name]
    assert "timeout" in panels[first.name]
    assert "exhausted" in panels[second.name]


def test_replay_follows_a_log_that_is_still_being_written(tmp_path: Path) -> None:
    """A run still in flight replays up to its current end and keeps up."""

    repo, log = _finished_run(
        tmp_path,
        "growing",
        phase="implementation",
        body="[2026-08-08 22:10:00] iter 1: worker started\n",
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))

    frame = app.advance()
    assert "no terminal state recorded" in frame.header
    assert frame.warnings == dash.NO_WARNINGS

    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"[2026-08-08 22:40:00] {_WATCHDOG_LINE}\n")
    assert _WATCHDOG_LINE in app.advance().warnings


def test_replay_resolves_the_repo_from_the_log_directory(tmp_path: Path) -> None:
    """Step 3: the repo is the log's own tree, not the caller's cwd."""

    repo, log = _finished_run(tmp_path, "resolve")
    assert dash.resolve_replay(log) == dash.ReplaySource(log_path=log, repo=repo)

    # A log copied somewhere with no logs/ parent still resolves to a directory
    # rather than raising; there is no claim line there so identity is empty.
    loose = tmp_path / "elsewhere" / "grind-copy.log"
    loose.parent.mkdir()
    loose.write_text("[2026-08-08 22:10:00] iter 1: worker started\n", encoding="utf-8")
    assert dash.resolve_replay(loose).repo == loose.parent

    app = dash.DashboardApp(
        loose.parent, refresh_seconds=3600, replay=dash.resolve_replay(loose)
    )
    header = app.advance().header
    assert loose.name in header
    assert "implementation" in header


# ---------------------------------------------------------------------------
# ortus-0udo.7 AC-4: live mode is unchanged by the presence of replay
# ---------------------------------------------------------------------------


def test_live_mode_is_unchanged_by_the_presence_of_the_replay_flag(
    tmp_path: Path,
) -> None:
    """AC-4: no replay means the newest log, an unadorned header, as before."""

    repo = _live_repo(tmp_path)
    # A second, older log proves live mode still chooses rather than being
    # pinned: replay is the only thing that pins a log.
    older = repo / "logs" / "grind-20260808-090000.log"
    older.write_text("[2026-08-08 09:00:00] iter 1: HALT stale\n", encoding="utf-8")
    os.utime(older, (0, 0))

    app = dash.DashboardApp(repo, refresh_seconds=3600)
    assert app.replay is None
    frame = app.advance()

    assert app.snapshot.log_path == repo / "logs" / "grind-20260808-221000.log"
    assert "replay" not in frame.header
    assert frame.header == dash.header_line(app.snapshot, identity=app.identity)
    # Unadorned means no replay adornment: the header leaf (ortus-0udo.3) fills
    # this region with several lines, and none of them names a log or an ending.
    assert app.snapshot.log_path is not None
    assert app.snapshot.log_path.name not in frame.header
    assert dash.NO_JOURNAL_OUTCOME not in frame.header
    assert frame.warnings == dash.NO_WARNINGS


def test_replay_option_is_registered_on_the_verb() -> None:
    """AC-4: replay is a flag on this verb rather than a verb of its own."""

    params = inspect.signature(dash.dashboard).parameters
    assert list(params) == ["repo", "replay"]
    default = params["replay"].default
    assert isinstance(default, OptionInfo)
    assert "--replay" in default.param_decls


# ---------------------------------------------------------------------------
# ortus-0udo.6: the verdict region, criteria as they are judged
# ---------------------------------------------------------------------------
#
# The fixtures below write their packet and their reports through the same
# store and renderer grind uses, so a format change breaks these tests rather
# than silently teaching the panel to parse something nothing writes.

_VERDICT_ISSUE = "ortus-0udo.6"
_VERDICT_LOG = "grind-20260808-231000.log"
_VERDICT_CANDIDATE = "deadbeefcafe"

_ACCEPTANCE = (
    "## Observable criteria\n"
    "- AC-1: criteria are listed before any verdict exists.\n"
    "- AC-2: statuses update from a verifier report artifact.\n"
    "- AC-3: the decision shows pending until an envelope is emitted.\n"
    "\n"
    "## Criterion checks\n"
    "- AC-1: Run `uv run pytest tests/test_dashboard.py -k one -q`.\n"
    "- AC-2: Run `uv run pytest tests/test_dashboard.py -k two -q`.\n"
    "- AC-3: Run `uv run pytest tests/test_dashboard.py -k three -q`.\n"
    "\n"
    "## Targeted tests\n"
    "Run `uv run pytest tests/test_dashboard.py -q`.\n"
)


def _packet(acceptance: str = _ACCEPTANCE) -> dict[str, Any]:
    return {
        "id": _VERDICT_ISSUE,
        "issue_type": "feature",
        "title": "Dashboard verdict panel",
        "description": "## Objective\nShow criteria as they are judged.\n",
        "design": "## Readiness schema\nv1\n",
        "acceptance_criteria": acceptance,
    }


def _judged_repo(
    tmp_path: Path,
    name: str,
    *,
    acceptance: str = _ACCEPTANCE,
    candidate_hash: str = _VERDICT_CANDIDATE,
) -> Path:
    """A repository mid-verification: a packet artifact and a grind log."""

    del candidate_hash
    repo = tmp_path / name
    (repo / "logs" / "grind-transactions").mkdir(parents=True)
    packet_path = (
        repo / "logs" / "grind-transactions" / f"{_VERDICT_ISSUE}-claimed.issue.json"
    )
    packet_path.write_text(json.dumps(_packet(acceptance)), encoding="utf-8")
    (repo / "logs" / _VERDICT_LOG).write_text(
        _ortus_line(
            "2026-08-08 23:10:00",
            f"iter 1: goal-prompt ready for {_VERDICT_ISSUE} (claude)",
        )
        + _ortus_line("2026-08-08 23:10:00", "iter 1: verification started"),
        encoding="utf-8",
    )
    return repo


def _write_report(
    repo: Path,
    criteria: tuple[tuple[str, str, str], ...],
    *,
    decision: str = "fail",
    candidate_hash: str = _VERDICT_CANDIDATE,
    attempt: int = 1,
) -> str:
    """Render a verifier report the way grind renders one, and journal it."""

    verdict = Verdict(
        candidate_hash=candidate_hash,
        decision=decision,
        criteria=tuple(
            {"id": name, "status": status, "evidence": evidence}
            for name, status, evidence in criteria
        ),
        commands=("uv run pytest tests/test_dashboard.py -q",),
        reviewed_files=("src/ortus/commands/dashboard.py",),
        reviewed_interfaces=("verdict_panel",),
        risks=("none",),
        findings=("none",),
        codegraph=("explored the verdict region",),
    )
    dest = (
        repo
        / "logs"
        / "grind-transactions"
        / f"{candidate_hash}.verifier-{attempt}.md"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        render_report(verdict, issue_id=_VERDICT_ISSUE, attempt=attempt),
        encoding="utf-8",
    )
    return str(dest.relative_to(repo))


def _log_envelope(
    repo: Path,
    *,
    decision: str,
    candidate_hash: str = _VERDICT_CANDIDATE,
    reason: str = "",
) -> None:
    """Append the verdict envelope event exactly as grind appends it."""

    with (repo / "logs" / _VERDICT_LOG).open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "ortus.verdict",
                    "schema": 1,
                    "decision": decision,
                    "candidate_hash": candidate_hash,
                    "reason": reason,
                },
                separators=(",", ":"),
            )
            + "\n"
        )


def test_verdict_not_yet_reached_before_any_verdict_exists(tmp_path: Path) -> None:
    """AC-1: the packet's criteria are listed, outstanding, before any verdict.

    An operator watching a seven-criterion issue needs to know four are still
    outstanding, which is only visible if the list comes from the packet rather
    than from what the verifier has already reported.
    """

    repo = _judged_repo(tmp_path, "outstanding")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    panel = app.advance().verdict

    assert app.verdict.criteria == ("AC-1", "AC-2", "AC-3")
    for name in app.verdict.criteria:
        assert f"{name:<8}{dash.NOT_REACHED}" in panel
    # Not-yet-reached is not failing: a run with no report yet has failed nothing.
    assert dash.STATUS_FAIL not in panel
    assert dash.NO_REPORT in panel
    assert dash.VERDICT_PENDING in panel
    assert dash.region_state("verdict", app.snapshot, app.verdict) == "state-idle"


def test_verdict_from_report_updates_each_criterion_status(tmp_path: Path) -> None:
    """AC-2: statuses come from the report artifact as it is written.

    The reasoning a verifier records is what went unread in a bd comment when a
    criterion failed on a wider sweep than its own prescribed check, so the
    evidence line is on the panel next to the status it explains.
    """

    repo = _judged_repo(tmp_path, "reported")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    assert dash.NOT_REACHED in app.advance().verdict

    ref = _write_report(
        repo,
        (
            ("AC-1", "pass", "listed all three before any verdict"),
            ("AC-2", "fail", "statuses did not update on the second tick"),
        ),
    )
    panel = app.advance().verdict

    assert (
        dash.criterion_line(
            dash.Criterion(
                id="AC-1",
                status=dash.STATUS_PASS,
                evidence="listed all three before any verdict",
            )
        )
        in panel
    )
    assert (
        dash.criterion_line(
            dash.Criterion(
                id="AC-2",
                status=dash.STATUS_FAIL,
                evidence="statuses did not update on the second tick",
            )
        )
        in panel
    )
    # A criterion the report did not cover is outstanding, never inferred.
    assert (
        dash.criterion_line(dash.Criterion(id="AC-3", status=dash.NOT_REACHED)) in panel
    )
    # The panel says which artifact it read, so the claim is checkable.
    assert ref in panel
    assert "attempt 1" in panel
    assert dash.region_state("verdict", app.snapshot, app.verdict) == "state-failed"


def test_verdict_pending_then_decided_when_an_envelope_is_emitted(
    tmp_path: Path,
) -> None:
    """AC-3: pending until an envelope exists, then the decision, and it stays.

    A verifier that passed every criterion and then ended without emitting an
    envelope had a sound candidate rejected, and the operator learned of it only
    from a terse error after the run had exited. Pending is that failure mode
    made visible while it is still happening.
    """

    repo = _judged_repo(tmp_path, "decided")
    app = dash.DashboardApp(repo, refresh_seconds=3600)

    _write_report(
        repo,
        (
            ("AC-1", "pass", "criteria listed"),
            ("AC-2", "pass", "statuses updated"),
            ("AC-3", "pass", "decision shown"),
        ),
        decision="pass",
    )
    panel = app.advance().verdict
    # Every criterion has passed and there is still no decision: exactly the
    # state that cost a run.
    assert dash.VERDICT_PENDING in panel
    assert dash.NOT_REACHED not in panel
    assert app.verdict.envelope is None

    _log_envelope(repo, decision="pass")
    panel = app.advance().verdict
    assert f"decision {dash.STATUS_PASS}" in panel
    assert dash.VERDICT_PENDING not in panel
    assert dash.region_state("verdict", app.snapshot, app.verdict) == "state-live"

    # The log tail is incremental, so a tick that reads no new bytes must not
    # forget the decision it has already seen.
    later = app.advance()
    assert app.snapshot.events == ()
    assert f"decision {dash.STATUS_PASS}" in later.verdict


def test_verdict_identifier_mismatch_renders_both_sets(tmp_path: Path) -> None:
    """AC-4: neither the packet's ids nor the verdict's are dropped.

    The verdict validator already treats a mismatch as a real condition — fatal
    on a pass, recorded on a fail — so the panel has to show it as one rather
    than render whichever set it happened to hold.
    """

    repo = _judged_repo(tmp_path, "mismatch")
    _write_report(
        repo,
        (
            ("AC-1", "pass", "packet criterion, reported"),
            ("AC-9", "fail", "identifier the packet never named"),
        ),
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    panel = app.advance().verdict

    # Both sets are on screen: the packet's outstanding ids and the stray one.
    for name in ("AC-2", "AC-3"):
        assert (
            dash.criterion_line(dash.Criterion(id=name, status=dash.NOT_REACHED))
            in panel
        )
    assert "AC-9" in panel
    assert "not in work spec" in panel
    assert "identifier the packet never named" in panel

    mismatch = dash.criteria_mismatch(app.verdict)
    assert "not in the work spec: AC-9" in mismatch
    assert "missing from the criterion results: AC-2, AC-3" in mismatch
    assert mismatch in panel


def test_verdict_shows_the_latest_report_rather_than_merging_a_correction_round(
    tmp_path: Path,
) -> None:
    """A correction round writes a second report; the panel shows that one."""

    repo = _judged_repo(tmp_path, "correction")
    first = _write_report(repo, (("AC-1", "fail", "first attempt was wrong"),))
    second = _write_report(
        repo,
        (
            ("AC-1", "pass", "second attempt corrected it"),
            ("AC-2", "pass", "and covered the next one"),
        ),
        decision="pass",
        attempt=2,
    )
    assert first != second

    app = dash.DashboardApp(repo, refresh_seconds=3600)
    panel = app.advance().verdict

    assert second in panel
    assert first not in panel
    assert "attempt 2" in panel
    assert "second attempt corrected it" in panel
    # Merged, the superseded failure would still be on screen.
    assert "first attempt was wrong" not in panel
    assert dash.STATUS_FAIL not in panel


def test_verdict_for_a_stale_candidate_is_shown_as_stale(tmp_path: Path) -> None:
    """A verdict against another candidate is stale rather than current."""

    repo = _judged_repo(tmp_path, "stale")
    _write_report(
        repo,
        (
            ("AC-1", "pass", "judged an earlier candidate"),
            ("AC-2", "pass", "judged an earlier candidate"),
            ("AC-3", "pass", "judged an earlier candidate"),
        ),
        decision="pass",
        candidate_hash="0123456789ab",
    )
    _log_envelope(repo, decision="pass", candidate_hash="0123456789ab")

    app = dash.DashboardApp(repo, refresh_seconds=3600)
    live = app.advance().verdict
    # Live-from-log has no current owned-path hash, so it does not invent one
    # just to paint stale. The envelope still names what was judged.
    assert "stale" not in live
    assert "0123456789ab" in live

    snapshot = replace(app.snapshot, candidate_hash=_VERDICT_CANDIDATE)
    state = replace(app.verdict, candidate_hash=_VERDICT_CANDIDATE)
    panel = dash.verdict_panel(snapshot, state)
    assert "stale" in panel
    assert "0123456789ab" in panel
    assert dash.short(_VERDICT_CANDIDATE) in panel
    # Stale is attention rather than the green of a current pass.
    assert dash.region_state("verdict", snapshot, state) == "state-ended"


def test_verdict_packet_without_criterion_identifiers_is_a_readiness_failure(
    tmp_path: Path,
) -> None:
    """An unidentified packet is a readiness failure, not an empty verdict."""

    repo = _judged_repo(
        tmp_path,
        "unidentified",
        acceptance=(
            "## Observable criteria\n"
            "- the first thing works.\n"
            "\n## Criterion checks\n"
            "- Run `uv run pytest -q`.\n"
            "\n## Targeted tests\n"
            "Run `uv run pytest tests/test_dashboard.py -q`.\n"
        ),
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    panel = app.advance().verdict

    assert app.verdict.criteria == ()
    assert "readiness failure" in panel
    assert dash.region_state("verdict", app.snapshot, app.verdict) == "state-failed"

    # A packet that is not on disk at all is named as absent rather than read
    # as an issue with nothing to satisfy.
    for artifact in (repo / "logs" / "grind-transactions").glob("*.issue.json"):
        artifact.unlink()
    assert dash.NO_PACKET in app.advance().verdict


def test_verdict_summarises_a_report_too_large_to_show_rather_than_clipping_it(
    tmp_path: Path,
) -> None:
    """Whole rows are dropped and counted; no criterion is cut in half.

    The truncation marker is written by hand here because it only appears in a
    report whose criterion matrix outgrew its own budget, which is a property of
    the artifact rather than of any one fixture.
    """

    parsed = dash.parse_report(
        "## Ortus verifier report (schema v1)\n"
        "\n"
        f"Issue: {_VERDICT_ISSUE}\n"
        f"Candidate: `{_VERDICT_CANDIDATE}`\n"
        "Decision: **FAIL**\n"
        "Verifier attempt: 3\n"
        "\n"
        "### Acceptance criteria\n"
        "- AC-1: pass — held\n"
        "- AC-2: fail — did not hold\n"
        "- [4 more entries truncated; see transaction artifacts]\n"
        "\n"
        "### Commands\n"
        "- uv run pytest -q\n",
        ref="logs/grind-transactions/deadbeefcafe.verifier-3.md",
    )
    assert parsed.decision == "fail"
    assert parsed.attempt == 3
    assert parsed.candidate_hash == _VERDICT_CANDIDATE
    assert [item.id for item in parsed.criteria] == ["AC-1", "AC-2"]
    assert parsed.criteria[1].evidence == "did not hold"
    # A section that is not the matrix is not read as one.
    assert all(item.id.startswith("AC-") for item in parsed.criteria)
    assert parsed.elided == 4

    state = dash.VerdictState(
        criteria=tuple(f"AC-{index}" for index in range(1, 31)),
        report=parsed,
        candidate_hash=_VERDICT_CANDIDATE,
    )
    panel = dash.verdict_panel(RunSnapshot(journal_present=True), state)
    rows = [line for line in panel.splitlines() if line.startswith("AC-")]

    assert len(rows) == dash.CRITERION_ROWS
    assert f"({30 - dash.CRITERION_ROWS} more criteria not shown)" in panel
    assert "4 criteria summarised by the report" in panel
    # Every rendered row is a whole criterion.
    for row in rows:
        assert row.split()[1] in (dash.STATUS_PASS, dash.STATUS_FAIL, "not")

    long_evidence = "x" * (dash.CRITERION_TEXT_CHARS * 3)
    clipped = dash.criterion_line(
        dash.Criterion(id="AC-1", status=dash.STATUS_PASS, evidence=long_evidence)
    )
    assert len(clipped) < dash.CRITERION_TEXT_CHARS + 40


def test_verdict_of_a_replayed_run_whose_journal_is_gone_still_names_the_decision(
    tmp_path: Path,
) -> None:
    """The decision lives in the grind log; a leftover journal is ignored."""

    repo = _judged_repo(tmp_path, "replayed")
    _log_envelope(repo, decision="fail", reason="AC-2 did not hold")
    leftover = repo / "logs" / "grind-transaction.json"
    leftover.write_text("{}", encoding="utf-8")

    log = repo / "logs" / _VERDICT_LOG
    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))
    panel = app.advance().verdict

    assert not app.snapshot.idle
    assert leftover.is_file()
    assert dash.VERDICT_IDLE not in panel
    assert f"decision {dash.STATUS_FAIL}" in panel
    assert "AC-2 did not hold" in panel
    assert app.verdict.criteria == ("AC-1", "AC-2", "AC-3")


def test_verdict_region_reads_the_packet_the_run_was_claimed_against(
    tmp_path: Path,
) -> None:
    """The log's issue id finds the packet artifact by prefix.

    The artifact is the packet the verifier is held to, so the panel and the
    verdict validator answer the same question rather than the panel answering
    for the issue as it happens to be now.
    """

    repo = _judged_repo(tmp_path, "packet")
    snapshot = dash.read_snapshot(repo)
    named = dash.packet_path(repo, snapshot)
    assert named is not None
    assert named.name.startswith(_VERDICT_ISSUE)

    # A snapshot that never recorded a packet ref still finds it, because
    # the artifact is prefixed with the issue that owns it.
    older = replace(snapshot, issue_packet_ref="")
    assert dash.packet_path(repo, older) == named

    packet = dash.read_packet(named)
    assert packet is not None
    assert dash.packet_criteria(packet) == ("AC-1", "AC-2", "AC-3")


def test_verdict_region_writes_nothing_while_it_reads_the_artifacts(
    tmp_path: Path,
) -> None:
    """AC-3 of the shell still holds once the region reads two more artifacts."""

    repo = _judged_repo(tmp_path, "readonly")
    _write_report(repo, (("AC-1", "pass", "held"),), decision="pass")
    _log_envelope(repo, decision="pass")

    app = dash.DashboardApp(repo, refresh_seconds=3600)
    app.advance()
    before = _fingerprint(repo)
    for _ in range(3):
        app.advance()

    assert _fingerprint(repo) == before
    assert f"decision {dash.STATUS_PASS}" in app.last_frame.verdict


def test_visual_contract_colour_carries_meaning() -> None:
    """AC-5: one accent for motion, one for attention, one for failure."""

    live = RunSnapshot(journal_present=True, phase="implementation")
    ended = RunSnapshot(journal_present=True, phase="corrections-exhausted")
    idle = RunSnapshot()

    assert dash.region_state("header", idle) == "state-idle"
    assert dash.region_state("header", live) == "state-live"
    assert dash.region_state("header", ended) == "state-ended"

    for state in dash.STATE_CLASSES:
        assert f"Region.{state}" in dash._CSS, state
    assert len({dash.ACCENT_MOTION, dash.ACCENT_ATTENTION, dash.ACCENT_FAILURE}) == 3


# ---------------------------------------------------------------------------
# ortus-0udo.5: the candidate region — owned, inherited and disowned paths
# ---------------------------------------------------------------------------
#
# Candidate composition caused several failures in one session and was invisible
# in all of them: a worker disowned seven inherited paths belonging to its own
# issue, which would have committed five and abandoned seven, and a verifier was
# rejected for drift because disowned paths were counted as unexplained. The
# facts were in the journal the whole time, so these tests are about what the
# panel makes visible rather than about how it is worded.

_CANDIDATE_ISSUE = "ortus-0udo.5"
_CANDIDATE_BASE = "9f1c4b7ad2e6c8b0"
_CANDIDATE_LOG = "grind-20260809-031500.log"


def _candidate_snapshot(
    *,
    candidate: tuple[str, ...] = (),
    handoff: tuple[str, ...] = (),
    unrelated: tuple[str, ...] = (),
    base_head: str = _CANDIDATE_BASE,
    phase: str = "implementation",
) -> RunSnapshot:
    """A constructed live snapshot with one candidate composition."""

    return RunSnapshot(
        issue_id=_CANDIDATE_ISSUE,
        base_head=base_head,
        candidate_paths=candidate,
        candidate_hash="deadbeefcafe",
        handoff_paths=handoff,
        unrelated_paths=unrelated,
        phase=phase,
        attempt=1,
        journal_present=True,
    )


def _candidate_repo(tmp_path: Path, name: str) -> Path:
    """A log-only repo: live-from-log, no invented owned paths."""

    repo = tmp_path / name
    (repo / "logs").mkdir(parents=True)
    (repo / "logs" / _CANDIDATE_LOG).write_text(
        _ortus_line(
            "2026-08-09 03:15:00",
            f"iter 1: goal-prompt ready for {_CANDIDATE_ISSUE} (claude)",
        )
        + _ortus_line("2026-08-09 03:15:00", "iter 1: worker started"),
        encoding="utf-8",
    )
    return repo


def test_candidate_owned_paths_and_their_count_are_shown(tmp_path: Path) -> None:
    """AC-1: what the run will commit, read off the journal.

    A candidate with no handoff and nothing disowned is the common case, so it
    renders as one labelled list rather than as three sections, two of them
    empty.
    """

    owned = ("src/ortus/commands/dashboard.py", "tests/test_dashboard.py")
    panel = dash.candidate_panel(_candidate_snapshot(candidate=owned))

    assert f"{dash.OWNED} {len(owned)}" in panel
    for path in owned:
        assert path in panel
    assert dash.INHERITED not in panel
    assert dash.DISOWNED not in panel
    assert "commits 2" in panel


def test_candidate_of_a_repository_with_no_journal_shows_no_candidate(
    tmp_path: Path,
) -> None:
    """Nothing was captured, which is not the same as a candidate holding nothing."""

    quiet = dash.DashboardApp(_quiet_repo(tmp_path), refresh_seconds=3600)
    assert quiet.advance().candidate == dash.CANDIDATE_IDLE

    empty = _candidate_repo(tmp_path, "empty")
    panel = dash.DashboardApp(empty, refresh_seconds=3600).advance().candidate
    assert panel != dash.CANDIDATE_IDLE
    assert dash.CANDIDATE_EMPTY in panel
    leftover = empty / "logs" / "grind-transaction.json"
    leftover.write_text("{}", encoding="utf-8")
    again = dash.DashboardApp(empty, refresh_seconds=3600).advance().candidate
    assert dash.CANDIDATE_EMPTY in again


def test_candidate_inherited_and_disowned_are_labelled_apart_from_owned(
    tmp_path: Path,
) -> None:
    """AC-2: three groups, each path in exactly one, disowned winning any overlap.

    The overlap is not hypothetical: grind hands a resumed worker its
    predecessor's candidate *plus* whatever was already disowned, so a disowned
    path is normally in the handoff list too and would otherwise be counted
    twice under two labels.
    """

    snapshot = _candidate_snapshot(
        candidate=("src/ortus/core/runstate.py", "src/ortus/commands/dashboard.py"),
        handoff=(
            "src/ortus/commands/dashboard.py",
            "docs/notes.md",
            "scratch/leftover.txt",
        ),
        unrelated=("scratch/leftover.txt", "README.md"),
    )
    panel = dash.candidate_panel(snapshot)
    groups = dash.candidate_groups(snapshot)
    by_label = {group.label: group.paths for group in groups}

    assert by_label[dash.OWNED] == ("src/ortus/core/runstate.py",)
    assert by_label[dash.INHERITED] == (
        "docs/notes.md",
        "src/ortus/commands/dashboard.py",
    )
    assert by_label[dash.DISOWNED] == ("README.md", "scratch/leftover.txt")
    # One path, one group: a disowned path is out of the commit whichever other
    # list also names it.
    seen = [path for group in groups for path in group.paths]
    assert len(seen) == len(set(seen))
    assert panel.count("scratch/leftover.txt") == 1

    for label, count in ((dash.OWNED, 1), (dash.INHERITED, 2), (dash.DISOWNED, 2)):
        assert f"{label} {count}" in panel
    # Disowning is the judgement that went wrong once, so the region claims a
    # colour for it rather than sitting quiet.
    assert dash.region_state("candidate", snapshot) == "state-ended"
    kept = _candidate_snapshot(candidate=("src/a.py",))
    assert dash.region_state("candidate", kept) == "state-idle"


def test_candidate_base_commit_is_shown(tmp_path: Path) -> None:
    """AC-3: the tree the candidate was captured on, which a moved base invalidates."""

    panel = dash.candidate_panel(_candidate_snapshot(candidate=("src/a.py",)))

    assert f"base {_CANDIDATE_BASE[:12]}" in panel

    # A record that never captured a base says so rather than showing a blank.
    unknown = _candidate_snapshot(candidate=("src/a.py",), base_head="")
    assert "base unknown" in dash.candidate_panel(unknown)


def test_candidate_truncates_a_long_list_with_a_remaining_count(
    tmp_path: Path,
) -> None:
    """AC-4: a candidate larger than the region is counted, not scrolled.

    The floor matters as much as the cap: a thousand owned paths must not push
    the disowned group down to a bare number, because a number with no paths
    under it is what an operator cannot act on.
    """

    owned = tuple(f"src/generated/module_{index:04d}.py" for index in range(1200))
    disowned = ("scratch/one.txt", "scratch/two.txt", "scratch/three.txt")
    panel = dash.candidate_panel(
        _candidate_snapshot(candidate=owned, handoff=disowned, unrelated=disowned)
    )
    lines = panel.splitlines()

    assert f"{dash.OWNED} {len(owned)}" in panel
    assert any("more not shown" in line for line in lines)
    # Bounded: the path rows plus the base line, one heading per group and one
    # elision line. Nothing here scrolls the layout.
    assert len(lines) <= dash.CANDIDATE_PATH_ROWS + 8
    for path in disowned:
        assert path in panel
    assert "commits 1200" in panel


def test_candidate_path_with_unusual_characters_does_not_break_the_layout(
    tmp_path: Path,
) -> None:
    """A filename is whatever the filesystem holds, and the screen must survive it.

    A newline would split one row into two, an escape sequence would repaint the
    terminal, and square brackets are markup to the widget these regions are
    built on, so a bracketed path would silently vanish from the screen.
    """

    odd = "src/[bracketed]/na\tme\x1b[31m.py"
    panel = dash.candidate_panel(
        _candidate_snapshot(candidate=(odd, "src/plain.py"))
    )

    assert "\x1b" not in panel and "\t" not in panel
    assert len(panel.splitlines()) == 4  # base, heading, two paths
    assert "[bracketed]" in panel
    assert dash.printable(odd) == "src/[bracketed]/na?me?[31m.py"


def test_candidate_of_a_finalized_run_shows_what_was_committed(
    tmp_path: Path,
) -> None:
    """A run that finished still explains its candidate rather than blanking."""

    snapshot = _candidate_snapshot(
        candidate=("src/ortus/commands/dashboard.py",),
        unrelated=("README.md",),
        phase="finalized-committed",
    )
    panel = dash.candidate_panel(snapshot)

    assert snapshot.terminal
    assert "src/ortus/commands/dashboard.py" in panel
    assert f"{dash.DISOWNED} 1" in panel and "README.md" in panel


def test_candidate_region_writes_nothing_while_it_reads_the_log(
    tmp_path: Path,
) -> None:
    """AC-3 of the shell still holds with the candidate region filled."""

    repo = _candidate_repo(tmp_path, "readonly")
    leftover = repo / "logs" / "grind-transaction.json"
    leftover.write_text('{"candidate_paths": ["must-not-be-read.py"]}', encoding="utf-8")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    app.advance()
    before = _fingerprint(repo)
    for _ in range(3):
        app.advance()

    assert _fingerprint(repo) == before
    assert dash.CANDIDATE_EMPTY in app.last_frame.candidate
    assert "must-not-be-read.py" not in app.last_frame.candidate


def test_candidate_row_budget_gives_every_group_a_floor() -> None:
    """The allocator itself: floors first, then the remainder in render order."""

    groups = (
        dash.PathGroup(dash.OWNED, tuple(f"a{i}" for i in range(50))),
        dash.PathGroup(dash.INHERITED, ()),
        dash.PathGroup(dash.DISOWNED, tuple(f"c{i}" for i in range(4))),
    )
    rows = dash.path_rows(groups, total=12, floor=3)

    assert rows[1] == 0
    assert rows[2] >= 3
    assert sum(rows) <= 12
    # A candidate that fits is not truncated at all.
    small = (dash.PathGroup(dash.OWNED, ("a", "b")), dash.PathGroup(dash.DISOWNED, ()))
    assert dash.path_rows(small, total=12, floor=3) == (2, 0)


# ---------------------------------------------------------------------------
# ortus-0udo.4: what the worker is doing, and how long it has been doing it
# ---------------------------------------------------------------------------
#
# The measured case: a worker issues one blocking tool call for a test suite and
# then writes nothing with content for twenty minutes. Every test here fixes the
# observation time rather than sleeping, because the property under test is the
# age of an action and a real clock would either make the suite slow or make the
# assertion approximate.


_STARTED = "[2026-08-08 22:10:00] iter 1: worker started\n"
_SUITE_CALL = (
    '{"type":"assistant","timestamp":"2026-08-08T22:11:00+00:00","message":'
    '{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":'
    '{"command":"uv run pytest tests/test_dashboard.py"}}]}}\n'
)
#: Events that carry no content: a tool result and a session heartbeat. The
#: model does not treat either as an action, and this region must not either.
_HEARTBEATS = (
    '{"type":"user","timestamp":"2026-08-08T22:15:00+00:00","message":{}}\n'
    '{"type":"system","subtype":"heartbeat","timestamp":"2026-08-08T22:16:00+00:00"}\n'
)
_SESSION_END = (
    '{"type":"result","subtype":"success","timestamp":"2026-08-08T22:20:00+00:00"}\n'
)


def _at(hour: int, minute: int, second: int = 0) -> _dt.datetime:
    """One instant on the day the sample run was measured, in UTC."""

    return _dt.datetime(2026, 8, 8, hour, minute, second, tzinfo=_dt.timezone.utc)


def _acting_repo(tmp_path: Path, name: str, body: str) -> Path:
    """A repository mid-run whose log holds `body`."""

    repo = tmp_path / name
    (repo / "logs").mkdir(parents=True)
    claim = _ortus_line(
        "2026-08-08 22:10:00",
        "iter 1: goal-prompt ready for ortus-0udo.4 (claude)",
    )
    if body and "goal-prompt ready" not in body and "ortus grind started" not in body:
        body = claim + body
    (repo / "logs" / "grind-20260808-221000.log").write_text(body, encoding="utf-8")
    return repo


def _pytest_root(tmp_path: Path, *, sessions: int = 1, workspaces: int = 0) -> Path:
    """A stand-in for the pytest temporary root, with `workspaces` under the
    newest session directory."""

    root = tmp_path / "tmp" / "pytest-of-someone"
    for index in range(sessions):
        session = root / f"pytest-{index}"
        session.mkdir(parents=True)
    newest = root / f"pytest-{sessions - 1}"
    for index in range(workspaces):
        (newest / f"test_case{index}").mkdir()
    return root


def test_action_latest_names_what_the_worker_is_doing_and_for_how_long(
    tmp_path: Path,
) -> None:
    """AC-1: the latest action and its age, for a live run."""

    repo = _acting_repo(tmp_path, "acting", _STARTED + _SUITE_CALL)
    snapshot = read_snapshot(repo, now=_at(22, 11, 30))
    panel = dash.action_panel(snapshot)

    # The action itself, named rather than summarised away.
    assert "Bash" in panel
    assert "uv run pytest tests/test_dashboard.py" in panel
    # Its age, and the state that age puts it in.
    assert dash.ACTION_THINKING in panel
    assert "30s" in panel
    assert dash.region_state("current-action", snapshot) == "state-live"


def test_action_latest_survives_a_heartbeat_that_carries_no_content(
    tmp_path: Path,
) -> None:
    """A log whose newest events are heartbeats still names the real action.

    This is the whole reason the age is worth showing: the log grows the entire
    time a suite runs, and none of that growth is something to display.
    """

    repo = _acting_repo(tmp_path, "beating", _STARTED + _SUITE_CALL + _HEARTBEATS)
    snapshot = read_snapshot(repo, now=_at(22, 16, 30))
    panel = dash.action_panel(snapshot)

    assert "uv run pytest tests/test_dashboard.py" in panel
    # The age is measured from the action, not from the last heartbeat, or a
    # stalled worker would look busy every time its log twitched.
    assert "5m 30s" in panel
    assert snapshot.blocked_seconds == 330.0


def test_action_text_cannot_repaint_the_terminal(tmp_path: Path) -> None:
    """An action is a shell command the agent wrote, and may carry anything."""

    hostile = (
        '{"type":"assistant","timestamp":"2026-08-08T22:11:00+00:00","message":'
        '{"role":"assistant","content":[{"type":"tool_use","name":"Bash","input":'
        '{"command":"echo \\u001b[2Jwiped"}}]}}\n'
    )
    repo = _acting_repo(tmp_path, "hostile", _STARTED + hostile)
    panel = dash.action_panel(read_snapshot(repo, now=_at(22, 11, 30)))

    assert "\x1b" not in panel
    assert "wiped" in panel
    assert len(panel.splitlines()) == 2


def test_action_blocked_threshold_is_visually_distinct_from_thinking(
    tmp_path: Path,
) -> None:
    """AC-2: past the threshold the same action reads as blocked, not thinking."""

    repo = _acting_repo(tmp_path, "blocking", _STARTED + _SUITE_CALL)
    thinking = read_snapshot(repo, now=_at(22, 11, 30))
    blocked = read_snapshot(repo, now=_at(22, 20, 0))

    assert thinking.blocked_seconds < dash.BLOCKED_SECONDS <= blocked.blocked_seconds
    assert dash.ACTION_THINKING in dash.action_panel(thinking)

    panel = dash.action_panel(blocked)
    assert dash.ACTION_BLOCKED in panel
    assert dash.ACTION_THINKING not in panel
    # Nine minutes on one tool call is the fact an operator acts on.
    assert "9m 00s" in panel

    # Distinct in words and in colour: a terminal that renders neither the
    # palette nor the other would still tell the two states apart.
    assert dash.region_state("current-action", thinking) == "state-live"
    assert dash.region_state("current-action", blocked) == "state-ended"


def test_action_blocked_threshold_clears_when_the_worker_finishes(
    tmp_path: Path,
) -> None:
    """A worker that finishes while the panel shows blocked clears next tick."""

    repo = _acting_repo(tmp_path, "finishing", _STARTED + _SUITE_CALL)
    log = repo / "logs" / "grind-20260808-221000.log"
    blocked = read_snapshot(repo, now=_at(22, 20, 0))
    assert dash.ACTION_BLOCKED in dash.action_panel(blocked)

    with log.open("a", encoding="utf-8") as handle:
        handle.write(_SESSION_END)
    finished = read_snapshot(repo, previous=blocked, now=_at(22, 20, 5))

    panel = dash.action_panel(finished)
    assert "worker session ended" in panel
    assert dash.ACTION_BLOCKED not in panel
    assert dash.region_state("current-action", finished) == "state-live"

    # And once the run itself reaches a terminal phase, the last action is over
    # rather than blocked: an age that keeps growing after a run has ended is
    # the stall this region exists to report, and it never happened.
    with log.open("a", encoding="utf-8") as handle:
        handle.write(_ortus_line("2026-08-08 22:20:06", "iter 1: worker closed ortus-0udo.4"))
    ended = dash.action_panel(read_snapshot(repo, previous=finished, now=_at(23, 0, 0)))

    assert dash.ACTION_ENDED in ended
    assert dash.ACTION_BLOCKED not in ended


def test_action_workspace_evidence_reports_the_pytest_workspace_count(
    tmp_path: Path,
) -> None:
    """AC-3: a running suite contributes a count, labelled as evidence."""

    root = _pytest_root(tmp_path, sessions=3, workspaces=7)
    assert dash.workspace_count(root) == 7

    repo = _acting_repo(tmp_path, "evidenced", _STARTED + _SUITE_CALL)
    app = dash.DashboardApp(repo, refresh_seconds=3600, workspace_root=root)
    panel = app.advance().current_action

    assert "7" in panel
    # Evidence, explicitly: the total is unknown, and a root shared by two
    # concurrent grinds counts both runs rather than this one.
    assert "evidence" in panel
    assert "not this run's progress" in panel
    # It moves while the suite runs, which is what makes it corroboration.
    (root / "pytest-2" / "test_case7").mkdir()
    assert dash.workspace_count(root) == 8


def test_action_workspace_evidence_is_absent_without_a_pytest_root(
    tmp_path: Path,
) -> None:
    """A missing root is normal and renders as absent rather than as zero."""

    missing = tmp_path / "tmp" / "pytest-of-nobody"
    assert dash.workspace_count(missing) is None

    repo = _acting_repo(tmp_path, "unevidenced", _STARTED + _SUITE_CALL)
    app = dash.DashboardApp(repo, refresh_seconds=3600, workspace_root=missing)
    panel = app.advance().current_action

    # Absent, not zero: a count of nothing would read as a suite that has
    # produced nothing rather than as a host that has run none.
    assert "evidence" not in panel
    assert dash.WORKSPACE_EVIDENCE.format(count=0) not in panel
    # The action and its age are unaffected by the absence.
    assert len(panel.splitlines()) == 2
    assert "uv run pytest" in panel


def test_action_idle_shows_no_action_rather_than_a_stale_one(
    tmp_path: Path,
) -> None:
    """AC-4: a repository whose run has finished shows no current action.

    The log outlives the transaction, so the last action of a finished run is
    still readable long after it stopped being current. Showing it is exactly
    the stale reading an operator would act on.
    """

    quiet = read_snapshot(_quiet_repo(tmp_path), now=_at(23, 30, 0))
    assert quiet.idle
    assert dash.action_panel(quiet) == dash.ACTION_IDLE
    assert dash.region_state("current-action", quiet) == "state-idle"

    repo = _acting_repo(
        tmp_path,
        "over",
        _STARTED
        + _SUITE_CALL
        + _ortus_line("2026-08-08 22:20:06", "iter 1: worker closed ortus-0udo.4")
        + _SESSION_END,
    )
    snapshot = read_snapshot(repo, now=_at(23, 30, 0))

    assert not snapshot.idle
    assert snapshot.terminal
    assert snapshot.latest_action, "the log still holds the run's last action"
    assert dash.ACTION_ENDED in dash.action_panel(snapshot)
    assert dash.region_state("current-action", snapshot) == "state-ended"

    # Replay is the one reader that pins a finished run's last action rather
    # than watching the newest log in the tree.
    source = dash.resolve_replay(repo / "logs" / "grind-20260808-221000.log")
    replayed = dash.action_panel(snapshot, replay=source)
    assert "worker session ended" in replayed
    assert dash.ACTION_ENDED in replayed
    assert dash.ACTION_BLOCKED not in replayed


def test_action_idle_run_in_flight_has_logged_nothing_yet(tmp_path: Path) -> None:
    """A claimed issue whose worker has not acted yet is not the same as idle."""

    repo = _acting_repo(tmp_path, "starting", "")
    snapshot = read_snapshot(repo, now=_at(22, 10, 0))

    assert not snapshot.idle
    assert dash.action_panel(snapshot) == dash.NO_ACTION
    assert dash.region_state("current-action", snapshot) == "state-idle"


def test_action_region_writes_nothing_while_it_counts_workspaces(
    tmp_path: Path,
) -> None:
    """AC-3 of the shell still holds with the current-action region filled."""

    root = _pytest_root(tmp_path, workspaces=2)
    repo = _acting_repo(tmp_path, "readonly-action", _STARTED + _SUITE_CALL)
    app = dash.DashboardApp(repo, refresh_seconds=3600, workspace_root=root)
    app.advance()
    before = (_fingerprint(repo), _fingerprint(root))
    for _ in range(3):
        app.advance()

    assert (_fingerprint(repo), _fingerprint(root)) == before
    assert "uv run pytest" in app.last_frame.current_action


def test_action_duration_reads_in_the_coarsest_useful_unit() -> None:
    """Seconds, then minutes, then hours, and never a negative age."""

    assert dash.duration(0) == "0s"
    assert dash.duration(59.9) == "59s"
    assert dash.duration(60) == "1m 00s"
    assert dash.duration(1_230) == "20m 30s"
    assert dash.duration(3_600) == "1h 00m"
    assert dash.duration(3_960) == "1h 06m"
    # A journal timestamp in local time against a UTC log clock is the case the
    # model clamps; a caller cannot render one either.
    assert dash.duration(-5) == "0s"


# ---------------------------------------------------------------------------
# ortus-27uu: a missing textual costs this one verb, not the whole CLI
# ---------------------------------------------------------------------------
#
# The property under test is "textual is never imported unless the dashboard
# runs", and asserting where the import statement sits would not test it — the
# statement can move back to module scope without such an assertion noticing.
# So each case drives the real CLI in a child interpreter with textual made
# unimportable, which is the stale environment the bug was reported from. A
# child is also what keeps the block from leaking: this test session has
# textual installed and every other test in this file depends on it.

_BLOCK_TEXTUAL = """
import sys


class _BlockTextual:
    \"\"\"Make textual unimportable, as a pre-dependency environment does.\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name == "textual" or name.startswith("textual."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, _BlockTextual())
for _stale in [m for m in sys.modules if m == "textual" or m.startswith("textual.")]:
    del sys.modules[_stale]
"""

# rich styles a phrase token by token, so a literal like `--replay` reaches the
# pipe as `ESC[1;36m-ESC[0mESC[1;36m-replayESC[0m` and no substring assertion on
# it can hold. `plain()` removes the styling so the assertions test the text.
_PLAIN_OUTPUT = r'''
import re as _re

_ANSI = _re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]")


def plain(text):
    """Child output with any ANSI escape sequences removed."""
    return _ANSI.sub("", text)
'''


def _run_without_textual(body: str) -> subprocess.CompletedProcess:
    """Run `body` in a child interpreter that cannot import textual."""

    env = dict(os.environ)
    # rich wraps stderr at the console width, and an assertion on a phrase in
    # the message must not fail because the phrase straddled a wrap.
    env["COLUMNS"] = "400"
    # rich turns color on when it detects a CI environment, which splits the
    # asserted phrases across escape sequences. Turning it off is the same
    # class of defense as the width above; `plain()` at the assertion sites
    # covers a renderer that ignores this. TERM is deliberately left alone —
    # `TERM=dumb` makes rich report a fixed 80-column console and would undo
    # COLUMNS above.
    env["NO_COLOR"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _BLOCK_TEXTUAL + _PLAIN_OUTPUT + body],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_cli_imports_without_textual(tmp_path: Path) -> None:
    """AC-1: the CLI imports and a non-dashboard verb runs with textual absent."""

    repo = tmp_path / "quiet-repo"
    (repo / ".beads").mkdir(parents=True)
    proc = _run_without_textual(
        f"""
from typer.testing import CliRunner

from ortus.cli import app

assert "textual" not in sys.modules, "importing the CLI pulled in textual"

# --codegraph off: this is about textual, not the CodeGraph prerequisite.
result = CliRunner().invoke(app, ["grind", {str(repo)!r}, "--dry-run", "--codegraph", "off"])
assert result.exit_code == 0, (result.exit_code, result.output, result.exception)
assert "/goal" in plain(result.stdout), result.stdout
assert "textual" not in sys.modules, "running grind pulled in textual"
print("VERB-OK")
"""
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "VERB-OK" in proc.stdout


def test_dashboard_reports_missing_textual(tmp_path: Path) -> None:
    """AC-2: the dashboard verb alone fails, with the install command, not a stack."""

    repo = tmp_path / "quiet-repo"
    repo.mkdir()
    proc = _run_without_textual(
        f"""
from typer.testing import CliRunner

from ortus.cli import app

runner = CliRunner()

# Argument parsing needs no framework, so --help still answers.
helped = runner.invoke(app, ["dashboard", "--help"])
assert helped.exit_code == 0, (helped.exit_code, helped.output)
assert "--replay" in plain(helped.stdout), helped.stdout

result = runner.invoke(app, ["dashboard", {str(repo)!r}])
assert result.exit_code == 1, (result.exit_code, result.output, result.exception)
assert not isinstance(result.exception, ImportError), result.exception
# Stripping can only remove text, so the negative assertions below stay at
# least as strict as they were: a real traceback still trips them.
message = plain(result.stderr)
assert "pip install textual" in message, message
assert "Traceback" not in message, message
assert "ModuleNotFoundError" not in message, message
print("MESSAGE-OK")
"""
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MESSAGE-OK" in proc.stdout
    # The message the child printed is this module's, so an edit that drops the
    # install command from it fails here too rather than only in the child.
    assert "pip install textual" in dash.MISSING_TEXTUAL


def test_partial_textual_install_reports_the_same_way() -> None:
    """Edge case: a package that imports but lacks a submodule is not special.

    It raises ImportError rather than ModuleNotFoundError, and to the operator
    it is the same broken environment, so it must produce the same sentence.
    """

    proc = _run_without_textual(
        """
import types

from ortus.commands import dashboard as dash

# textual itself imports; textual.widgets does not.
sys.modules["textual"] = types.ModuleType("textual")
sys.modules["textual.app"] = types.ModuleType("textual.app")

try:
    dash._shell()
except dash.TextualUnavailable as exc:
    assert "pip install textual" in str(exc), str(exc)
    print("PARTIAL-OK")
else:
    raise AssertionError("a partial textual install was accepted")
"""
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARTIAL-OK" in proc.stdout


# ---------------------------------------------------------------------------
# ortus-0udo.3: the header region — issue, phase, iteration, budget, watchdog
# ---------------------------------------------------------------------------
#
# Two failures in one session were invisible until they were fatal. A worker was
# killed at the watchdog cap while holding finished work, stranding a candidate
# a human then recovered by hand, and nothing on screen counted down to it.
# Correction attempts were consumed silently, so escalation to a human label
# arrived as a surprise after the budget was already spent. Both facts were on
# disk the whole time, so these tests are about what the header makes visible.

_HEADER_ISSUE = "ortus-0udo.3"
_HEADER_TITLE = "Dashboard header: phase, iteration, correction budget and watchdog"
_HEADER_NOW = _dt.datetime(2026, 8, 9, 5, 0, 0, tzinfo=_dt.timezone.utc)


def _stamp(minutes_ago: float) -> str:
    return (_HEADER_NOW - _dt.timedelta(minutes=minutes_ago)).isoformat()


def _header_snapshot(**fields: Any) -> RunSnapshot:
    """A live run thirty minutes in, on its first pass, with a worker running."""

    base: dict[str, Any] = {
        "issue_id": _HEADER_ISSUE,
        "phase": "implementation",
        "attempt": 1,
        "journal_present": True,
        "observed_at": _HEADER_NOW,
        "created_at": _HEADER_NOW - _dt.timedelta(minutes=30),
        "attempts": (
            {"number": 1, "phase": "implementation", "started_at": _stamp(30)},
        ),
    }
    base.update(fields)
    return RunSnapshot(**base)


def _header_repo(tmp_path: Path, name: str, **journal: Any) -> Path:
    """A repository mid-run whose grind log carries real phase timestamps.

    Its stamps hang off the wall clock rather than off `_HEADER_NOW`, because
    the app reads the run through `read_snapshot`, which observes at the real
    now; a pinned stamp would be compared against it and read as however long
    ago the fixture was written.
    """

    del journal
    repo = tmp_path / name
    (repo / "logs").mkdir(parents=True)
    started = _dt.datetime.now().astimezone() - _dt.timedelta(minutes=30)
    stamp = started.strftime("%Y-%m-%d %H:%M:%S")
    (repo / "logs" / "grind-20260809-043000.log").write_text(
        _ortus_line(
            stamp,
            f"iter 1: goal-prompt ready for {_HEADER_ISSUE} (claude)",
        )
        + _ortus_line(stamp, "iter 1: spawning claude (single-issue worker)"),
        encoding="utf-8",
    )
    return repo


def _no_bd(monkeypatch: Any) -> list[list[str]]:
    """Make every bd query fail, and record what was asked."""

    asked: list[list[str]] = []

    def _fake(argv: list[str], **kwargs: Any) -> Any:
        asked.append(list(argv))
        raise OSError("bd is not installed")

    monkeypatch.setattr(subprocess, "run", _fake)
    return asked


def test_header_live_run_shows_issue_phase_iteration_and_elapsed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """AC-1: the four facts that say which work is in flight, and for how long."""

    _no_bd(monkeypatch)
    repo = _header_repo(tmp_path, "live-header")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    header = app.advance().header

    assert _HEADER_ISSUE in header
    assert "step implementation" in header
    assert "iteration 1" in header
    assert "elapsed 30m" in header
    assert dash.region_state("header", app.snapshot) == "state-live"
    # Step and budget lead their own lines, so a narrow terminal clips the
    # title before it clips either of the two fields an operator scans for.
    lines = header.splitlines()
    assert lines[1].startswith("step ")
    assert lines[2].startswith("corrections ")


def test_header_idle_repository_shows_an_idle_header(tmp_path: Path) -> None:
    """Compatibility: no grind log is a state, not a blank panel."""

    app = dash.DashboardApp(_quiet_repo(tmp_path), refresh_seconds=3600)
    frame = app.advance()

    assert frame.header == dash.HEADER_IDLE
    assert dash.region_state("header", app.snapshot) == "state-idle"
    # Nothing is fabricated: no phase, no iteration, no countdown.
    for invented in ("step ", "iteration ", "watchdog", "corrections"):
        assert invented not in frame.header


def test_header_correction_budget_is_shown_against_its_cap() -> None:
    """Corrections are gone: the header says retries are disabled.

    `--max-corrections` no longer exists on grind, so the dashboard must not
    invent a cap by reading a missing declaration.
    """

    assert "max_corrections" not in inspect.signature(grind.grind).parameters
    assert dash.declared_cap(dash.MAX_CORRECTIONS_OPTION) is None

    snapshot = _header_snapshot(corrections=1, attempt=2, phase="correction")
    assert "retries disabled" in dash.header_line(snapshot)
    assert "corrections 1/0" in dash.header_line(snapshot)

    # An explicit historical cap still renders spent-against-limit.
    assert "corrections 1/2" in dash.header_line(snapshot, correction_cap=2)
    assert "retries disabled" in dash.header_line(snapshot, correction_cap=0)


def test_header_watchdog_clamped_at_zero_when_a_worker_is_overdue() -> None:
    """AC-3: the countdown drains, clamps at zero, and never goes negative."""

    cap = dash.declared_cap(dash.WORKER_TIMEOUT_OPTION)
    assert cap is not None
    assert (
        cap
        == inspect.signature(grind.grind).parameters["worker_timeout"].default.default
    )

    live = _header_snapshot()
    assert dash.watchdog_headroom(live, cap=3600) == 1800.0
    assert "watchdog 30m 00s left of 1h 00m" in dash.header_line(live, worker_cap=3600)

    # Draining: the same worker, ten minutes later, has ten minutes less.
    later = _header_snapshot(observed_at=_HEADER_NOW + _dt.timedelta(minutes=10))
    assert dash.watchdog_headroom(later, cap=3600) == 1200.0

    # Past the cap: clamped at zero and rendered as overdue, which is the fact
    # an operator acts on. A negative countdown would read as a bug. Inspect
    # watchdog_line alone — header_line appends it to the corrections clause,
    # and "corrections 0/0 - retries disabled" contains a hyphen that is not
    # a minus sign on the countdown.
    overdue = _header_snapshot(observed_at=_HEADER_NOW + _dt.timedelta(minutes=45))
    assert dash.watchdog_headroom(overdue, cap=3600) == 0.0
    watchdog = dash.watchdog_line(overdue, cap=3600)
    assert "overdue by 15m 00s" in watchdog
    assert "past 1h 00m" in watchdog
    assert not watchdog.lstrip().startswith("-")
    assert " -" not in watchdog.split("overdue by", 1)[-1]

    # A cap of zero disables the watchdog: no limit, not an expired one.
    assert dash.WATCHDOG_OFF in dash.header_line(live, worker_cap=0)
    assert "overdue" not in dash.header_line(live, worker_cap=0)

    # A resumed run: the transaction opened hours ago, but the worker being
    # watched started at the newest attempt boundary, and the countdown follows
    # the worker rather than the transaction.
    resumed = _header_snapshot(
        phase="verification",
        attempt=2,
        created_at=_HEADER_NOW - _dt.timedelta(hours=4),
        implementation_started_at=_HEADER_NOW - _dt.timedelta(hours=4),
        attempts=(
            {"number": 1, "phase": "implementation", "started_at": _stamp(240)},
            {"number": 2, "phase": "verification", "started_at": _stamp(5)},
        ),
    )
    assert dash.watchdog_headroom(resumed, cap=3600) == 3300.0
    assert dash.worker_started_at(resumed) == _HEADER_NOW - _dt.timedelta(minutes=5)

    # A journal that records no start at all shows no countdown rather than one
    # taken from a time it does not have.
    blind = _header_snapshot(attempts=())
    assert dash.watchdog_headroom(blind, cap=3600) is None
    assert dash.WATCHDOG_NO_START in dash.header_line(blind, worker_cap=3600)

    # A finished run is not being watched by anything, so it carries no
    # countdown that would tick forever after the worker exited.
    ended = _header_snapshot(phase="corrections-exhausted")
    assert "watchdog" not in dash.header_line(ended, worker_cap=3600)
    assert dash.HEADER_ENDED in dash.header_line(ended, worker_cap=3600)


def test_header_degrades_without_bd_to_the_issue_id_alone(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """AC-4: a failed bd query costs the title, never the view."""

    asked = _no_bd(monkeypatch)
    repo = _header_repo(tmp_path, "no-bd")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    frame = app.advance()

    assert asked and asked[0][:3] == ["bd", "--readonly", "--sandbox"]
    assert app.identity == dash.IssueIdentity(issue_id=_HEADER_ISSUE, queried=True)
    # Everything read off disk is still on screen; only the enrichment is gone.
    assert _HEADER_ISSUE in frame.header
    assert "step implementation" in frame.header
    assert "corrections 0/" in frame.header

    # The question is asked once per issue rather than once per refresh: bd
    # costs a subprocess and the loop ticks once a second.
    for _ in range(3):
        app.advance()
    assert len(asked) == 1


def test_header_enrichment_names_the_issue_when_bd_answers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Step 4: title and priority come from one read-only bd query."""

    payload = json.dumps(
        [{"id": _HEADER_ISSUE, "title": _HEADER_TITLE, "priority": 2, "status": "open"}]
    )
    asked: list[list[str]] = []

    def _fake(argv: list[str], **kwargs: Any) -> Any:
        asked.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, payload, "")

    monkeypatch.setattr(subprocess, "run", _fake)

    repo = _header_repo(tmp_path, "with-bd")
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    header = app.advance().header

    assert asked[0] == [
        "bd",
        "--readonly",
        "--sandbox",
        "show",
        _HEADER_ISSUE,
        "--json",
    ]
    assert app.identity.priority == 2
    assert _HEADER_ISSUE in header
    assert "P2" in header
    assert _HEADER_TITLE[:40] in header

    # An answer that names a different issue is not this run's, and is refused
    # rather than shown against the id the journal recorded.
    other = json.dumps([{"id": "ortus-other", "title": "not this run", "priority": 0}])
    assert dash.parse_identity(other, _HEADER_ISSUE) == dash.IssueIdentity(
        issue_id=_HEADER_ISSUE, queried=True
    )
    assert dash.parse_identity("{not json", _HEADER_ISSUE).title == ""


def test_header_renders_an_unrecognised_phase_verbatim() -> None:
    """Edge case: a phase this model does not know is shown, not called unknown."""

    snapshot = _header_snapshot(phase="some-future-phase")
    assert "step some-future-phase" in dash.header_line(snapshot)
    assert "unknown" not in dash.header_line(snapshot)


# ---------------------------------------------------------------------------
# ortus-dp36: Grok crumb NOC
# ---------------------------------------------------------------------------


def _event(payload: dict[str, Any]) -> LogEvent:
    event = classify_line(json.dumps(payload, separators=(",", ":")))
    assert event is not None
    return event


def _grok_body(*lines: str) -> str:
    return "[2026-08-08 22:10:00] iter 1: worker started\n" + "".join(
        line if line.endswith("\n") else line + "\n" for line in lines
    )


_GROK_THOUGHT = '{"type":"thought","data":"The user wants a plan."}'
_GROK_TEXT = '{"type":"text","data":"I will inspect the leftover state."}'
_GROK_TOOL = (
    '{"type":"tool_call","toolCallId":"c1","toolName":"read_file",'
    '"rawInput":{"target_file":"src/ortus/commands/dashboard.py"}}'
)
_GROK_DONE = '{"type":"tool_call_update","toolCallId":"c1","status":"completed"}'
_GROK_FAIL_TOOL = (
    '{"type":"tool_call","toolCallId":"c2","toolName":"search_tool",'
    '"rawInput":{"query":"codegraph explore"}}'
)
_GROK_FAIL = '{"type":"tool_call_update","toolCallId":"c2","status":"failed"}'
_GROK_OPEN_TOOL = (
    '{"type":"tool_call","toolCallId":"c3","toolName":"run_terminal_command",'
    '"rawInput":{"command":"bd prime"}}'
)
_CLAUDE_TURN = (
    '{"type":"assistant","message":{"role":"assistant","content":'
    '[{"type":"text","text":"reading the packet about thought and tool_call"}]}}'
)
_CODEX_ITEM = (
    '{"type":"item.started","item":{"type":"command_execution","command":"ls"}}'
)


def _painted_repo(tmp_path: Path, name: str, body: str, *, backend: str = "") -> Path:
    if backend:
        body = (
            _ortus_line(
                "2026-08-08 22:09:59",
                f"=== ortus grind started (subprocess-per-task shape; backend={backend}) ===",
            )
            + _ortus_line(
                "2026-08-08 22:10:00",
                f"iter 1: goal-prompt ready for ortus-0udo.4 ({backend})",
            )
            + body
        )
    return _acting_repo(tmp_path, name, body)


def test_grok_crumb_snapshot_paints_feed_and_rate_sensitive_pulse(
    tmp_path: Path,
) -> None:
    """AC-1: Grok crumbs paint a feed; more crumbs change the pulse."""

    repo = _painted_repo(
        tmp_path,
        "grok-live",
        _grok_body(_GROK_THOUGHT, _GROK_TEXT, _GROK_TOOL, _GROK_DONE),
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    first = app.advance()

    assert app.grok is True
    assert first.crumbs
    assert "think  The user wants a plan." in first.crumbs
    assert "text  I will inspect the leftover state." in first.crumbs
    assert "tool  read_file" in first.crumbs
    assert "done  tool" in first.crumbs
    assert first.crumbs in first.current_action
    assert dash.CRUMB_THINK in first.current_action

    extra = (
        '{"type":"thought","data":"another crumb arrives"}\n'
        '{"type":"thought","data":"and another"}\n'
        '{"type":"thought","data":"and a third"}\n'
        '{"type":"thought","data":"and a fourth"}\n'
        '{"type":"thought","data":"and a fifth"}\n'
        '{"type":"thought","data":"and a sixth"}\n'
    )
    log = repo / "logs" / "grind-20260808-221000.log"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(extra)
    second = app.advance()

    assert "think  another crumb arrives" in second.crumbs
    assert second.pulse != first.pulse
    # Same tick index is not the point: crumb rate and crumb count move the
    # Grok pulse independently of the 1 Hz scanner.
    sparse = dash.frame(app.snapshot, second_tick := app.tick)
    grok_same_tick = dash.frame(
        app.snapshot,
        second_tick,
        grok=True,
        crumbs=app.crumbs,
        tools=app.tools,
        crumb_rate=app.crumb_rate,
    )
    assert sparse.pulse != grok_same_tick.pulse
    assert grok_same_tick.crumbs == second.crumbs


def test_claude_snapshot_same_panels_has_no_crumb_feed(tmp_path: Path) -> None:
    """AC-1: Claude of the same header/action/candidate/verdict/warnings is sparse."""

    claude_repo = _painted_repo(tmp_path, "claude-same", _grok_body(_CLAUDE_TURN))
    grok_events = tuple(
        event
        for event in (
            _event({"type": "thought", "data": "The user wants a plan."}),
            _event({"type": "text", "data": "I will inspect the leftover state."}),
            _event(
                {
                    "type": "tool_call",
                    "toolCallId": "c1",
                    "toolName": "read_file",
                    "rawInput": {"target_file": "src/ortus/commands/dashboard.py"},
                }
            ),
            _event({"type": "tool_call_update", "toolCallId": "c1", "status": "completed"}),
        )
    )
    crumbs, tools, arrived = dash.ingest_crumbs(grok_events)
    assert arrived == 4

    app = dash.DashboardApp(claude_repo, refresh_seconds=3600)
    claude = app.advance()
    grok = dash.frame(
        app.snapshot,
        app.tick,
        grok=True,
        crumbs=crumbs,
        tools=tools,
        crumb_rate=arrived,
    )

    assert claude.header == dash.frame(app.snapshot, app.tick).header
    assert claude.current_action == dash.action_panel(
        app.snapshot, workspaces=app.workspaces
    )
    assert claude.candidate == grok.candidate
    assert claude.verdict == grok.verdict
    assert claude.warnings == grok.warnings
    assert claude.crumbs == ""
    assert dash.CRUMB_THINK not in claude.current_action
    assert "tool  read_file" not in claude.current_action
    assert grok.crumbs
    assert grok.current_action != claude.current_action
    assert claude.pulse == dash.pulse_line(app.snapshot, app.tick)
    assert grok.pulse != claude.pulse
    assert app.grok is False


def test_grok_feed_is_bounded_to_newest_n() -> None:
    """AC-2: more paragraphs than the region keeps only the newest N."""

    # A newline ends the paragraph so forty thought events stay forty rows,
    # not one coalesced line. Bound is on paragraphs, not tokens.
    burst = tuple(
        _event({"type": "thought", "data": f"crumb {index:04d}\n"}) for index in range(40)
    )
    feed, tools, arrived = dash.ingest_crumbs(burst)
    assert arrived == 40
    assert len(feed) == dash.CRUMB_FEED_LINES
    assert "crumb 0000" not in feed[0].text
    assert f"crumb {39:04d}" in feed[-1].text
    panel = dash.crumb_panel(feed, tools)
    assert panel.count("\n") + 1 <= dash.CRUMB_FEED_LINES + 1
    assert "crumb 0000" not in panel
    assert "crumb 0039" in panel

    more = tuple(
        _event({"type": "thought", "data": f"later {index:04d}\n"}) for index in range(15)
    )
    feed, _, _ = dash.ingest_crumbs(more, feed)
    assert len(feed) == dash.CRUMB_FEED_LINES
    grown = dash.crumb_panel(feed)
    assert len(grown) <= len(panel) + 80
    assert "later 0014" in grown
    assert "crumb 0000" not in grown


def test_grok_token_stream_thoughts_share_one_think_row() -> None:
    """AC-1: word-at-a-time thought events paint one think row, not one per word."""

    words = ("Now", "I", "need", "to", "run", "git", "push")
    events = tuple(_event({"type": "thought", "data": word}) for word in words)
    first, rest = events[:3], events[3:]
    feed, _, arrived = dash.ingest_crumbs(first)
    assert arrived == 3
    assert len(feed) == 1
    feed, _, arrived = dash.ingest_crumbs(rest, feed)
    assert arrived == 4
    assert len(feed) == 1
    assert feed[0].kind == "thought"
    assert feed[0].text == f"{dash.CRUMB_THINK}  {' '.join(words)}"
    panel = dash.crumb_panel(feed)
    assert panel == feed[0].text
    assert "\n" not in panel

    broken = (
        _event({"type": "thought", "data": "Hello"}),
        _event({"type": "thought", "data": "world\n"}),
        _event({"type": "thought", "data": "Next"}),
        _event({"type": "thought", "data": "paragraph"}),
        _event(
            {
                "type": "tool_call",
                "toolCallId": "c1",
                "toolName": "read_file",
                "rawInput": {"target_file": "src/ortus/commands/dashboard.py"},
            }
        ),
        _event({"type": "thought", "data": "after"}),
        _event({"type": "thought", "data": "tool"}),
        _event({"type": "text", "data": "plain"}),
        _event({"type": "text", "data": "words"}),
    )
    feed, _, arrived = dash.ingest_crumbs(broken)
    assert arrived == 9
    thinks = [crumb for crumb in feed if crumb.kind == "thought"]
    texts = [crumb for crumb in feed if crumb.kind == "text"]
    assert len(thinks) == 3
    assert "Hello world" in thinks[0].text
    assert "Next paragraph" in thinks[1].text
    assert thinks[2].text == f"{dash.CRUMB_THINK}  after tool"
    assert len(texts) == 1
    assert texts[0].text == f"{dash.CRUMB_TEXT}  plain words"


def test_only_grok_mode_shortens_refresh(tmp_path: Path) -> None:
    """AC-3: Claude and Codex stay at 1 Hz; only Grok drops the interval."""

    assert dash.refresh_interval() == dash.REFRESH_SECONDS == 1.0
    assert dash.refresh_interval(grok=False) == 1.0
    assert dash.refresh_interval(grok=True) == dash.GROK_REFRESH_SECONDS
    assert dash.GROK_REFRESH_SECONDS < dash.REFRESH_SECONDS

    claude = _painted_repo(tmp_path, "hz-claude", _grok_body(_CLAUDE_TURN))
    codex = _painted_repo(tmp_path, "hz-codex", _grok_body(_CODEX_ITEM))
    grok = _painted_repo(
        tmp_path, "hz-grok", _grok_body(_GROK_THOUGHT, _GROK_TEXT, _GROK_TOOL)
    )

    claude_app = dash.DashboardApp(claude)
    codex_app = dash.DashboardApp(codex)
    grok_app = dash.DashboardApp(grok)
    claude_app.advance()
    codex_app.advance()
    grok_app.advance()

    assert claude_app.grok is False
    assert codex_app.grok is False
    assert grok_app.grok is True
    assert claude_app.refresh_seconds == dash.REFRESH_SECONDS
    assert codex_app.refresh_seconds == dash.REFRESH_SECONDS
    assert grok_app.refresh_seconds == dash.GROK_REFRESH_SECONDS
    assert dash.pulse_line(claude_app.snapshot, 1) == (
        f"{dash.pulse(1)}  refresh 1  {claude_app.snapshot.observed_at.strftime('%H:%M:%S')}"
        if claude_app.snapshot.observed_at is not None
        else f"{dash.pulse(1)}  refresh 1  --:--:--"
    )


def test_grok_mode_keeps_readonly_and_has_no_emoji(tmp_path: Path) -> None:
    """AC-4: Grok strings stay pictograph-free; every bd vector stays sandboxed."""

    repo = _painted_repo(
        tmp_path,
        "grok-contract",
        _grok_body(
            _GROK_THOUGHT,
            _GROK_TEXT,
            _GROK_TOOL,
            _GROK_DONE,
            _GROK_FAIL_TOOL,
            _GROK_FAIL,
            _GROK_OPEN_TOOL,
        ),
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    painted = app.advance()

    for text in painted.texts():
        assert _emoji_in(text) == [], text
    assert _emoji_in(painted.crumbs) == []
    assert _emoji_in(dash.tool_row(app.tools)) == []
    assert dash.bd_argv("show", "ortus-dp36", "--json")[:3] == [
        "bd",
        "--readonly",
        "--sandbox",
    ]
    assert _literal_sites("bd") == ["bd_argv"]
    assert _literal_sites("git") == []


def test_claude_thought_substring_does_not_trip_grok_mode(tmp_path: Path) -> None:
    """Detection is the parsed JSON type, not the word thought in a Claude log."""

    repo = _painted_repo(tmp_path, "false-thought", _grok_body(_CLAUDE_TURN))
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    frame = app.advance()

    assert "thought" in frame.current_action or "thought" in app.snapshot.latest_action
    assert app.grok is False
    assert frame.crumbs == ""
    assert dash.is_grok_mode(app.snapshot) is False


def test_idle_repository_does_not_enter_grok_mode(tmp_path: Path) -> None:
    """An idle repo stays sparse even when the project default might be grok."""

    repo = _quiet_repo(tmp_path)
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    frame = app.advance()
    assert app.grok is False
    assert frame.crumbs == ""
    assert dash.is_grok_mode(app.snapshot, journal="grok") is False


def test_journal_backend_grok_without_crumbs_stays_sparse(tmp_path: Path) -> None:
    """Scheduler-only grok journal: Grok refresh, no empty crumb feed."""

    repo = _painted_repo(tmp_path, "journal-grok", _STARTED, backend="grok")
    app = dash.DashboardApp(repo)
    frame = app.advance()
    assert dash.journal_backend(repo) == "grok"
    assert app.grok is True
    assert frame.crumbs == ""
    assert dash.CRUMB_THINK not in frame.current_action
    assert app.refresh_seconds == dash.GROK_REFRESH_SECONDS


def test_tool_row_tracks_inflight_done_and_fail() -> None:
    """tool_call lights the row; tool_call_update settles done or fail."""

    events = (
        _event(
            {
                "type": "tool_call",
                "toolCallId": "c1",
                "toolName": "read_file",
                "rawInput": {"target_file": "a.py"},
            }
        ),
        _event({"type": "tool_call_update", "toolCallId": "c1", "status": "completed"}),
        _event(
            {
                "type": "tool_call",
                "toolCallId": "c2",
                "toolName": "search_tool",
                "rawInput": {"query": "x"},
            }
        ),
        _event({"type": "tool_call_update", "toolCallId": "c2", "status": "failed"}),
        _event(
            {
                "type": "tool_call",
                "toolCallId": "c3",
                "toolName": "run_terminal_command",
                "rawInput": {"command": "bd prime"},
            }
        ),
        _event({"type": "tool_call_update", "toolCallId": "c3", "status": None}),
        _event({"type": "usage", "usage": {"input_tokens": 1}}),
        _event({"type": "available_commands", "tools": ["read_file"]}),
    )
    feed, tools, arrived = dash.ingest_crumbs(events)
    assert arrived == 5
    row = dash.tool_row(tools)
    assert "done read_file" in row
    assert "fail search_tool" in row
    assert "in-flight run_terminal_command" in row
    assert all(crumb.kind != "usage" for crumb in feed)
    assert dash.crumb_panel((), tools).endswith(row)


def test_backend_conflict_is_recorded_not_silenced(tmp_path: Path) -> None:
    """Journal vs log disagreement is named; log crumbs still paint."""

    repo = _painted_repo(
        tmp_path,
        "conflict",
        _grok_body(_GROK_THOUGHT),
        backend="claude",
    )
    app = dash.DashboardApp(repo, refresh_seconds=3600)
    frame = app.advance()
    assert "PLAN-GAP" in app.conflict
    assert "named backend=claude" in app.conflict
    assert "event backend=grok" in app.conflict
    assert app.conflict in frame.warnings
    assert app.grok is True
    assert "think" in frame.crumbs
