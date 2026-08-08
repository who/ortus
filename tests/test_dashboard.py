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
import hashlib
import inspect
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from typer.models import OptionInfo

from ortus.commands import dashboard as dash
from ortus.core.runstate import RunSnapshot, RunWarning
from ortus.core.transaction import CandidateJournal, JournalStore

_MODULE_SOURCE = Path(dash.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _quiet_repo(tmp_path: Path) -> Path:
    """A repository with no bd workspace, no logs directory and no journal."""

    repo = tmp_path / "quiet"
    repo.mkdir()
    return repo


def _live_repo(tmp_path: Path) -> Path:
    """A repository mid-run: a journal in flight plus a grind log with content."""

    repo = tmp_path / "live"
    (repo / "logs").mkdir(parents=True)
    (repo / ".beads").mkdir()
    JournalStore(repo).save(
        CandidateJournal(
            issue_id="ortus-0udo.2",
            base_head="abc1234def",
            baseline_paths=(),
            baseline_fingerprints={},
            candidate_paths=("src/ortus/commands/dashboard.py",),
            candidate_hash="deadbeefcafe",
            phase="implementation",
            attempt=1,
            corrections=0,
        )
    )
    (repo / "logs" / "grind-20260808-221000.log").write_text(
        "[2026-08-08 22:10:00] iter 1: worker started\n"
        '{"type":"assistant","message":{"role":"assistant","content":'
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
    # Panel content belongs to the panel leaves; the shell only reserves space.
    for key in ("current-action", "candidate", "verdict"):
        assert titles[key][1] == dash.PLACEHOLDER
    # Warnings is filled by this leaf, so it states a finding rather than
    # holding space: a quiet region must read as "nothing fired".
    assert titles["warnings"][1] == dash.NO_WARNINGS


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

    assert app.advance().header == "idle - no transaction in flight"
    assert app.snapshot.idle

    (repo / "logs").mkdir()
    JournalStore(repo).save(
        CandidateJournal(
            issue_id="ortus-0udo.2",
            base_head="abc1234def",
            baseline_paths=(),
            baseline_fingerprints={},
            phase="implementation",
        )
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

    store = JournalStore(repo)
    journal = store.load()
    assert journal is not None
    store.save(replace(journal, phase="finalized-verified"))

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
    JournalStore(repo).save(
        CandidateJournal(
            issue_id="ortus-0udo.7",
            base_head="abc1234def",
            baseline_paths=(),
            baseline_fingerprints={},
            phase="implementation",
            attempt=1,
        )
    )
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
    JournalStore(repo).save(
        CandidateJournal(
            issue_id="ortus-0udo.7",
            base_head="abc1234def",
            baseline_paths=(),
            baseline_fingerprints={},
            candidate_paths=("src/ortus/commands/dashboard.py",),
            candidate_hash="deadbeefcafe",
            phase=phase,
            attempt=1,
        )
    )
    log = repo / "logs" / log_name
    log.write_text(
        body
        if body is not None
        else (
            "[2026-08-08 22:10:00] iter 1: worker started\n"
            f"[2026-08-08 22:40:00] {_WATCHDOG_LINE}\n"
        ),
        encoding="utf-8",
    )
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
    """The log outlives the journal, and is still worth reading on its own."""

    repo, log = _finished_run(tmp_path, "orphan-log")
    JournalStore(repo).path.unlink()

    app = dash.DashboardApp(repo, refresh_seconds=3600, replay=dash.resolve_replay(log))
    frame = app.advance()

    assert app.snapshot.idle
    assert dash.NO_JOURNAL_OUTCOME in frame.header
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


def test_replay_resolves_the_journal_from_the_log_directory(tmp_path: Path) -> None:
    """Step 3: the journal comes from the log's own tree, not the caller's cwd."""

    repo, log = _finished_run(tmp_path, "resolve")
    assert dash.resolve_replay(log) == dash.ReplaySource(log_path=log, repo=repo)

    # A log copied somewhere with no logs/ parent still resolves to a directory
    # rather than raising; the journal is simply absent there.
    loose = tmp_path / "elsewhere" / "grind-copy.log"
    loose.parent.mkdir()
    loose.write_text("[2026-08-08 22:10:00] iter 1: worker started\n", encoding="utf-8")
    assert dash.resolve_replay(loose).repo == loose.parent

    app = dash.DashboardApp(
        loose.parent, refresh_seconds=3600, replay=dash.resolve_replay(loose)
    )
    assert app.advance().header.splitlines()[1] == dash.NO_JOURNAL_OUTCOME


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
    assert frame.header == dash.header_line(app.snapshot)
    assert "\n" not in frame.header
    assert frame.warnings == dash.NO_WARNINGS


def test_replay_option_is_registered_on_the_verb() -> None:
    """AC-4: replay is a flag on this verb rather than a verb of its own."""

    params = inspect.signature(dash.dashboard).parameters
    assert list(params) == ["repo", "replay"]
    default = params["replay"].default
    assert isinstance(default, OptionInfo)
    assert "--replay" in default.param_decls


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
