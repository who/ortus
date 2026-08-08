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
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from ortus.commands import dashboard as dash
from ortus.core.runstate import RunSnapshot
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
    for key in ("current-action", "candidate", "verdict", "warnings"):
        assert titles[key][1] == dash.PLACEHOLDER


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
