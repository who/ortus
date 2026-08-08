"""ortus dashboard — one read-only live view of a grind run (ortus-0udo.2).

The dashboard watches a repository while a worker owns it. A grind computes its
candidate as the dirty set minus the baseline captured when it claimed the
issue, so anything written into the tree after that claim silently becomes the
worker's work and is judged by its verifier. An observer that writes therefore
corrupts the thing it observes — during one session an operator assistant
edited two files while a worker held the tree and had to unpick its own hunks
an hour later. This module is built so that it cannot: run state comes from
`ortus.core.runstate`, which is a pure function of two files, and every bd
invocation goes through `bd_argv`, which pins `--readonly --sandbox` (the same
flags `ortus check` uses for its own memory query). `tests/test_dashboard.py`
asserts both properties rather than trusting the convention, because a
convention is exactly what a later panel would forget.

This leaf is the shell: the verb, the layout, and the refresh loop. The five
regions it names — header, current action, candidate, verdict, warnings — are
filled by their own leaves and carry placeholders here. What the shell does own
is the pulse: one element that advances on every tick, so a healthy run looks
alive and a stalled one is obvious by its stillness. An operator once watched a
silent tail for hours unable to tell progress from death.

Refresh is a timer over the incremental snapshot rather than a filesystem
watcher: a watcher on a repo under active test churn wakes constantly for paths
nothing here displays. Colour carries meaning rather than decoration — one
accent for healthy motion, one for attention, one for failure, dim for
everything static — against a dark ground, which is the default because the
palette is chosen for contrast and a light terminal would wash the accents out.
There is no emoji anywhere in the interface: glyph support is uneven across
terminal fonts, and a coloured rule or a filled bar reads faster than a
pictograph.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import typer
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from ortus.core import output
from ortus.core.runstate import RunSnapshot, read_snapshot

#: Seconds between refreshes. A tick reads only the bytes the log grew by, so
#: this is cheap even against the megabyte logs a long session produces.
REFRESH_SECONDS = 1.0

#: Flags every bd invocation carries: `--readonly` keeps bd off its write paths
#: and `--sandbox` off its auto-sync ones. `check.py` already queries bd this
#: way, so this is the established read-only shape rather than a new one.
BD_READONLY_FLAGS: tuple[str, ...] = ("--readonly", "--sandbox")
BD_TIMEOUT_SECONDS = 15.0

#: Width of the pulse bar, in cells.
PULSE_WIDTH = 24
_PULSE_MARK = "█"  # full block
_PULSE_RULE = "─"  # box-drawing horizontal

#: What a region shows before its own leaf fills it.
PLACEHOLDER = "panel pending"

KEY_HINT = "q  quit     read-only: never writes the repository, bd, or git"


@dataclass(frozen=True)
class RegionSpec:
    """One named region of the layout: its widget id and its border title."""

    key: str
    title: str


#: The agreed layout, in render order. Each region is filled by its own leaf.
REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec("header", "run"),
    RegionSpec("current-action", "current action"),
    RegionSpec("candidate", "candidate"),
    RegionSpec("verdict", "verdict"),
    RegionSpec("warnings", "warnings"),
)

# --- palette ---------------------------------------------------------------
#
# Dark ground; colour used to carry meaning. Hex literals rather than theme
# variables so the contract the tests assert is the contract that renders.

GROUND = "#0b0f14"
PANEL = "#111820"
TEXT_STATIC = "#9aa7b4"
TEXT_DIM = "#5c6773"
ACCENT_MOTION = "#3ddc97"
ACCENT_ATTENTION = "#e2b53d"
ACCENT_FAILURE = "#ef5f5f"

#: Themes ship with Textual; this one is dark, which is the default posture.
DARK_THEME = "textual-dark"

#: State classes a region may carry. Exactly one is applied at a time.
STATE_CLASSES = ("state-idle", "state-live", "state-ended", "state-failed")


class Region(Static):
    """One bordered region of the layout, filled by its own leaf."""

    def __init__(self, spec: RegionSpec) -> None:
        super().__init__("", id=spec.key)
        self.spec = spec
        #: The plain text currently displayed, kept so the shell (and its
        #: tests) can read back what was rendered.
        self.body = ""

    def on_mount(self) -> None:
        self.border_title = self.spec.title

    def set_body(self, text: str) -> None:
        self.body = text
        self.update(text)

    def set_state(self, state: str) -> None:
        self.remove_class(*STATE_CLASSES)
        self.add_class(state)


@dataclass(frozen=True)
class Frame:
    """Everything the shell renders at one tick."""

    header: str
    current_action: str
    candidate: str
    verdict: str
    warnings: str
    pulse: str
    #: Region key to state class, so colour tracks the run rather than layout.
    states: tuple[tuple[str, str], ...] = ()

    def bodies(self) -> dict[str, str]:
        """Region key to the text that region shows."""

        return {
            "header": self.header,
            "current-action": self.current_action,
            "candidate": self.candidate,
            "verdict": self.verdict,
            "warnings": self.warnings,
        }

    def texts(self) -> tuple[str, ...]:
        """Every string this frame puts on screen."""

        return (*self.bodies().values(), self.pulse)


def bd_argv(*args: str) -> list[str]:
    """The argument vector for a bd query, read-only flags pinned in front.

    Every bd call the dashboard makes is built here, so the read-only posture
    is one line a test can assert rather than a rule each panel remembers.
    """

    return ["bd", *BD_READONLY_FLAGS, *args]


def run_bd(repo: Path, *args: str, timeout: float = BD_TIMEOUT_SECONDS) -> str | None:
    """Run one read-only bd query in `repo`; None when it does not answer.

    A failed query degrades the panel that asked for it and never the view: bd
    being unavailable is not a reason to blank a screen whose other half is
    read straight off disk.
    """

    try:
        proc = subprocess.run(
            bd_argv(*args),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def pulse(tick: int, *, width: int = PULSE_WIDTH) -> str:
    """A scanner that advances one cell per tick.

    Position comes from the tick rather than from the clock, so the bar moves
    on every refresh even when the run itself is producing nothing. Stillness
    on screen then means the dashboard has stopped, which is a fact worth being
    able to see.
    """

    span = max(1, width)
    cycle = max(1, 2 * span - 2)
    step = tick % cycle
    index = step if step < span else cycle - step
    return "".join(_PULSE_MARK if i == index else _PULSE_RULE for i in range(span))


def pulse_line(snapshot: RunSnapshot, tick: int, *, width: int = PULSE_WIDTH) -> str:
    """The moving strip: the scanner, the refresh count, the observation time."""

    observed = snapshot.observed_at
    stamp = observed.strftime("%H:%M:%S") if observed is not None else "--:--:--"
    return f"{pulse(tick, width=width)}  refresh {tick}  {stamp}"


def header_line(snapshot: RunSnapshot) -> str:
    """The shell's own one-line run state, until the header leaf fills it.

    An absent journal is a valid state, not an error, so a repository with no
    run in flight opens idle. A run that ended while the view was open reports
    its terminal phase rather than freezing on the last live frame.
    """

    if snapshot.idle:
        return "idle - no transaction in flight"
    issue = snapshot.issue_id or "unknown issue"
    line = f"{issue}   phase {snapshot.phase}"
    if snapshot.terminal:
        line += "   ended"
    return line


def region_state(key: str, snapshot: RunSnapshot) -> str:
    """The state class for one region; colour carries meaning, not decoration."""

    if key == "header":
        if snapshot.idle:
            return "state-idle"
        return "state-ended" if snapshot.terminal else "state-live"
    if key == "warnings" and snapshot.warnings:
        return "state-failed"
    return "state-idle"


def frame(snapshot: RunSnapshot, tick: int, *, width: int = PULSE_WIDTH) -> Frame:
    """Build the frame for `snapshot` at `tick`. Pure: reads nothing, writes nothing."""

    return Frame(
        header=header_line(snapshot),
        current_action=PLACEHOLDER,
        candidate=PLACEHOLDER,
        verdict=PLACEHOLDER,
        warnings=PLACEHOLDER,
        pulse=pulse_line(snapshot, tick, width=width),
        states=tuple((spec.key, region_state(spec.key, snapshot)) for spec in REGIONS),
    )


_CSS = f"""
Screen {{
    background: {GROUND};
    color: {TEXT_STATIC};
}}

Region {{
    background: {PANEL};
    color: {TEXT_STATIC};
    border: round {TEXT_DIM};
    border-title-color: {TEXT_DIM};
    padding: 0 1;
    height: auto;
    min-height: 3;
}}

Region.state-idle {{ border: round {TEXT_DIM}; }}
Region.state-live {{ border: round {ACCENT_MOTION}; border-title-color: {ACCENT_MOTION}; }}
Region.state-ended {{ border: round {ACCENT_ATTENTION}; border-title-color: {ACCENT_ATTENTION}; }}
Region.state-failed {{ border: round {ACCENT_FAILURE}; border-title-color: {ACCENT_FAILURE}; }}

/* The body scrolls, so a terminal too small for the layout degrades to a
   shorter view rather than crashing. */
#body {{
    height: 1fr;
    background: {GROUND};
}}

#pulse {{
    height: 1;
    padding: 0 1;
    color: {ACCENT_MOTION};
    background: {GROUND};
}}

#hint {{
    height: 1;
    padding: 0 1;
    color: {TEXT_DIM};
    background: {GROUND};
}}
"""


class DashboardApp(App[None]):
    """The shell: the region layout, the dark palette, and the refresh timer."""

    TITLE = "ortus dashboard"
    CSS = _CSS
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", "quit", show=False),
    ]
    #: The dashboard takes no actions, so it offers none: the command palette
    #: is the one surface that would put a mutating verb on screen.
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, repo: Path, *, refresh_seconds: float = REFRESH_SECONDS) -> None:
        super().__init__()
        self.repo = Path(repo)
        self.refresh_seconds = refresh_seconds
        self.snapshot = RunSnapshot()
        self.tick = 0
        self.last_frame = frame(self.snapshot, self.tick)

    def compose(self) -> ComposeResult:
        yield Region(REGIONS[0])
        with VerticalScroll(id="body"):
            for spec in REGIONS[1:]:
                yield Region(spec)
        yield Static("", id="pulse")
        yield Static(KEY_HINT, id="hint")

    def on_mount(self) -> None:
        self.theme = DARK_THEME
        self.refresh_run()
        self.set_interval(self.refresh_seconds, self.refresh_run)

    def advance(self) -> Frame:
        """Read the next snapshot and build its frame. Renders nothing.

        Split from the paint so the refresh loop is testable headless, and so a
        repository that becomes unreadable mid-run keeps the last frame instead
        of taking the view down — the pulse still advances, which is how an
        operator can tell the dashboard is alive.
        """

        try:
            self.snapshot = read_snapshot(self.repo, previous=self.snapshot)
        except OSError:
            pass
        self.tick += 1
        self.last_frame = frame(self.snapshot, self.tick)
        return self.last_frame

    def refresh_run(self) -> None:
        """One tick: re-read the run, then repaint."""

        self.advance()
        self.paint()

    def paint(self) -> None:
        """Push the current frame onto the widgets."""

        bodies = self.last_frame.bodies()
        states = dict(self.last_frame.states)
        for region in self.query(Region):
            text = bodies.get(region.spec.key)
            if text is not None:
                region.set_body(text)
            state = states.get(region.spec.key)
            if state is not None:
                region.set_state(state)
        for bar in self.query("#pulse"):
            bar.update(self.last_frame.pulse)


def dashboard(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Watch one grind run live: phase, current action, candidate, verdict, warnings.

    Strictly read-only: never writes the repository, bd, or git, because a
    write into a worktree a worker owns becomes that worker's candidate. Needs
    no bd workspace and no run in flight — a quiet repository opens idle. Press
    q to exit.
    """

    target = (repo if repo is not None else Path.cwd()).resolve()
    if not target.is_dir():
        output.error(f"no such directory: {target}")
        raise typer.Exit(code=1)
    DashboardApp(target).run()
