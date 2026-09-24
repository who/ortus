"""The frozen evaluation set: two fixtures, four arms, one report.

A harness change is only worth keeping if it pays for itself, and the only
way to know is to run the same work twice and compare. This module freezes
what "the same work" means so two comparisons a month apart are still
comparable: exactly two PRDs — Hello World and one converter CLI a single
step richer — and one arm per harness treatment beside the control.

The primary metric is cost per closed bead. Everything else is a guardrail:
a treatment that halves the bill by closing half as many beads, burning twice
the turns, or failing twice as often has not paid for itself, and the report
puts those numbers beside the primary one rather than leaving them to memory.

A treatment that picks a different model per bead is measured one level
deeper: the tier each bead was routed to is read from the seat's own routing
log and joined to that bead's outcome, so a tier that closes fewer beads than
the others is visible as the place a threshold is wrong.

Null is a result. A provider that never reports dollars (Codex) leaves the
primary metric unset for its arm, and an arm whose seat was never run leaves
every metric unset. Neither is zero-filled: a zero cost and an unreported
cost answer the operator's question in opposite directions, so the report
carries `None` through to JSON `null` and names every null metric explicitly
under `null_results`.

Measuring is hermetic and offline: the numbers come from the grind logs
already on disk via `ortus.core.cost`, so a report can be rebuilt long after
the runs themselves finished. Producing those logs is the sweep's job, and it
issues the very command strings the recipe prints, through an injected
executor — a cell someone ran by hand and a cell the sweep ran are the same
run, because there is only one construction path for the commands.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ortus.core import output
from ortus.core.cost import (
    BeadCost,
    RunCost,
    SessionCost,
    UsageBuckets,
    find_grind_logs,
    parse_grind_log,
    rollup_beads,
)
from ortus.core.judge_log import ROUTE_LOG_NAME
from ortus.core.judge_routing import RouterTier

#: Package holding the fixture PRDs. Installed with the wheel, so the paths
#: the recipe hands to `ortus plan` resolve outside a checkout too.
EVALPACK_PACKAGE = "ortus.evalpack"

#: Bumped whenever a metric is added, renamed, or changes meaning. A reader
#: comparing two reports checks this before comparing anything else.
REPORT_SCHEMA = "harness-eval-report/v2"

#: The one number the evaluation set exists to move.
PRIMARY_METRIC = "cost_per_closed_bead"

#: The numbers that decide whether a cheaper run was actually a better run.
GUARDRAIL_METRICS: tuple[str, ...] = (
    "close_rate",
    "worker_errors",
    "turns",
    "wall_seconds",
    "cache_hit_rate",
)

#: The arm every treatment is measured against: stock configuration.
CONTROL_ARM = "control"


def _sum_optional(values: Sequence[float | None]) -> float | None:
    """Sum the reported values, or None when nothing was reported.

    Unreported is not zero. A single reported value among nulls still yields
    a total, because the nulls are missing measurements rather than zeroes.
    """

    reported = [value for value in values if value is not None]
    if not reported:
        return None
    return sum(reported)


@dataclass(frozen=True)
class EvalFixture:
    """One frozen fixture: a PRD plus the bead band a correct plan lands in."""

    key: str
    title: str
    prd_filename: str
    min_beads: int
    max_beads: int
    summary: str

    @property
    def prd_path(self) -> Path:
        """Where the PRD lives in this installation."""

        return Path(str(files(EVALPACK_PACKAGE).joinpath(self.prd_filename)))

    def prd_text(self) -> str:
        return (
            files(EVALPACK_PACKAGE)
            .joinpath(self.prd_filename)
            .read_text(encoding="utf-8")
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "prd_filename": self.prd_filename,
            "prd_path": str(self.prd_path),
            "min_beads": self.min_beads,
            "max_beads": self.max_beads,
            "summary": self.summary,
        }


FIXTURE_A = EvalFixture(
    key="hello-world",
    title="Hello World",
    prd_filename="Hello_World_PRD.md",
    min_beads=1,
    max_beads=3,
    summary=(
        "The floor fixture: one Node CLI that prints one line. Its outcome is "
        "never in doubt, so a difference between arms is a harness difference."
    ),
)

FIXTURE_B = EvalFixture(
    key="temperature-converter",
    title="Temperature Converter CLI",
    prd_filename="Temperature_Converter_PRD.md",
    min_beads=5,
    max_beads=10,
    summary=(
        "One step richer: a small Node CLI with two test files and a bead that "
        "depends on two others, so planning and ordering are exercised too."
    ),
)

#: The whole evaluation set. Exactly two fixtures, both small on purpose: a
#: large application fixture would make a run cost hours and hide the harness
#: effect inside its own variance.
FIXTURE_PACK: tuple[EvalFixture, ...] = (FIXTURE_A, FIXTURE_B)


@dataclass(frozen=True)
class Treatment:
    """One harness change under test, and the config key that switches it on."""

    key: str
    config_key: str
    summary: str
    #: The bd issue that consumes this arm's numbers, when one is filed.
    measured_by: str | None = None

    @property
    def enable_line(self) -> str:
        """The `.ortusrc` line that turns this treatment on."""

        return f"{self.config_key} = true"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "config_key": self.config_key,
            "enable_line": self.enable_line,
            "summary": self.summary,
            "measured_by": self.measured_by,
        }


TREATMENTS: tuple[Treatment, ...] = (
    Treatment(
        key="prompt-audit",
        config_key="prompt_audit",
        summary="Serve the audited prompt variants to every phase.",
        measured_by="ortus-2637",
    ),
    Treatment(
        key="stable-prefix",
        config_key="stable_prompt_prefix",
        summary=(
            "Order the composed prompt so the per-bead segments come last and "
            "the prefix stays byte-identical across beads."
        ),
        measured_by="ortus-nudo",
    ),
    Treatment(
        key="model-router",
        config_key="jev_model_router",
        summary="Let the judge pick the per-bead worker model tier.",
        measured_by="ortus-p810",
    ),
)

#: Control first, then one arm per treatment, in declaration order.
ARMS: tuple[str, ...] = (CONTROL_ARM,) + tuple(t.key for t in TREATMENTS)


def treatment(key: str) -> Treatment | None:
    """The treatment an arm name selects, or None for the control arm."""

    for candidate in TREATMENTS:
        if candidate.key == key:
            return candidate
    return None


def seat_name(fixture: EvalFixture, arm: str) -> str:
    """The directory one cell of the matrix runs in, under the seat root."""

    return f"{fixture.key}-{arm}"


def arm_commands(
    fixture: EvalFixture,
    arm: str,
    *,
    root: Path,
    backend: str = "claude",
) -> tuple[str, ...]:
    """The commands that run one cell, in order.

    Deterministic and copy-pasteable: the same cell run next month issues the
    same commands against the same PRD, which is the whole point of freezing
    the set. The treatment is applied by appending its key to the seat's
    `.ortusrc` — every treatment here is a config key rather than a grind
    flag, so one appended line is the entire difference between arms.
    """

    seat = root / seat_name(fixture, arm)
    commands = [f"ortus init {seat} --backend {backend}"]
    applied = treatment(arm)
    if applied is not None:
        commands.append(f"printf '%s\\n' '{applied.enable_line}' >> {seat}/.ortusrc")
    commands.append(f"ortus plan {seat} {fixture.prd_path}")
    commands.append(f"ortus grind {seat} --tasks 0")
    return tuple(commands)


@dataclass(frozen=True)
class EvalCell:
    """One fixture under one arm: where it runs and what runs it."""

    fixture: str
    arm: str
    seat: str
    commands: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "arm": self.arm,
            "seat": self.seat,
            "commands": list(self.commands),
        }


def matrix(*, root: Path, backend: str = "claude") -> tuple[EvalCell, ...]:
    """Every fixture under every arm — the before/after runner's whole recipe."""

    cells: list[EvalCell] = []
    for fixture in FIXTURE_PACK:
        for arm in ARMS:
            cells.append(
                EvalCell(
                    fixture=fixture.key,
                    arm=arm,
                    seat=seat_name(fixture, arm),
                    commands=arm_commands(
                        fixture, arm, root=root, backend=backend
                    ),
                )
            )
    return tuple(cells)


#: How one recipe command gets run: the command string and the seconds left in
#: its cell's budget, in; the command's exit status, out.
CommandExecutor = Callable[[str, "float | None"], int]

#: Recorded against a command its cell's wall clock cut short. 124 is the
#: shell's own status for a timed-out command, so the record needs no private
#: code for the one outcome an operator most wants to tell apart.
TIMEOUT_STATUS = 124

#: One cell plans a small PRD and grinds it to zero, so an hour is generous
#: for the fixtures in the pack and still bounds a wedged backend.
DEFAULT_CELL_TIMEOUT_SECONDS = 3600.0

CELL_RAN = "ran"
CELL_SKIPPED = "skipped"
CELL_FAILED = "failed"


def shell_executor(command: str, timeout: float | None = None) -> int:
    """Run one recipe command through a shell and return its exit status.

    A shell rather than an argv list, because the printed recipe is the
    contract: a treatment arm applies itself with a `printf ... >> .ortusrc`
    redirect, and re-splitting these strings would build the second
    construction path the frozen recipe exists to prevent. The commands run in
    the caller's working directory, which is where an operator copying the
    same lines would have run them.
    """

    try:
        completed = subprocess.run(command, shell=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMEOUT_STATUS
    return completed.returncode


def cell_has_run(root: Path, cell: EvalCell) -> bool:
    """Whether this cell's seat already holds a grind log.

    The resume test: a seat with a log has an arm's numbers in it already, and
    re-running it would spend real model budget to overwrite a measurement.
    """

    return bool(find_grind_logs(root / cell.seat, newest=1))


@dataclass(frozen=True)
class CellExecution:
    """What the sweep did with one cell, and where it stopped if it stopped."""

    fixture: str
    arm: str
    seat: str
    status: str
    exit_status: int | None = None
    failed_command: str | None = None

    @property
    def ok(self) -> bool:
        """A skipped cell is not a failure: its seat already has its run."""

        return self.status != CELL_FAILED

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "arm": self.arm,
            "seat": self.seat,
            "status": self.status,
            "exit_status": self.exit_status,
            "failed_command": self.failed_command,
        }


def _announce(message: str) -> None:
    """Default sweep narration: one progress line per cell, on stderr."""

    output.progress("eval", message)


def _run_cell(
    cell: EvalCell,
    *,
    executor: CommandExecutor,
    timeout: float | None,
) -> CellExecution:
    """Issue one cell's commands in order, stopping at the first that fails.

    The budget is the cell's, not the command's: `ortus grind` inherits
    whatever the seat's build and plan left of it, so a slow init cannot buy
    the run an extra hour.
    """

    deadline = None if timeout is None else time.monotonic() + timeout
    for command in cell.commands:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return CellExecution(
                fixture=cell.fixture,
                arm=cell.arm,
                seat=cell.seat,
                status=CELL_FAILED,
                exit_status=TIMEOUT_STATUS,
                failed_command=command,
            )
        status = executor(command, remaining)
        if status != 0:
            return CellExecution(
                fixture=cell.fixture,
                arm=cell.arm,
                seat=cell.seat,
                status=CELL_FAILED,
                exit_status=status,
                failed_command=command,
            )
    return CellExecution(
        fixture=cell.fixture,
        arm=cell.arm,
        seat=cell.seat,
        status=CELL_RAN,
        exit_status=0,
    )


def run_matrix(
    root: Path,
    *,
    backend: str = "claude",
    executor: CommandExecutor = shell_executor,
    timeout: float | None = DEFAULT_CELL_TIMEOUT_SECONDS,
    announce: Callable[[str], None] = _announce,
) -> tuple[CellExecution, ...]:
    """Execute every cell of the matrix in order and record what each did.

    One cell's failure is that cell's: the sweep keeps going, because the arms
    that did run are still comparable and re-running them to reach the ones
    that did not would cost the run twice.
    """

    cells = matrix(root=root, backend=backend)
    records: list[CellExecution] = []
    for index, cell in enumerate(cells, start=1):
        label = f"cell {index}/{len(cells)} {cell.fixture}/{cell.arm}"
        if cell_has_run(root, cell):
            announce(f"{label}: skipped, {cell.seat} already holds a grind log")
            records.append(
                CellExecution(
                    fixture=cell.fixture,
                    arm=cell.arm,
                    seat=cell.seat,
                    status=CELL_SKIPPED,
                )
            )
            continue
        announce(
            f"{label}: running {len(cell.commands)} commands in {cell.seat} "
            "(a cell typically takes 10-40 min)"
        )
        record = _run_cell(cell, executor=executor, timeout=timeout)
        if record.ok:
            announce(f"{label}: done (ran)")
        else:
            announce(
                f"{label}: failed (exit {record.exit_status}) on "
                f"{record.failed_command}"
            )
        records.append(record)
    return tuple(records)


#: Tier rows print in the order the router declares them, weakest model
#: first, so two arms' tables line up row for row.
_TIER_ORDER: tuple[str, ...] = tuple(tier.value for tier in RouterTier)


@dataclass(frozen=True)
class TierMetrics:
    """How one router tier fared in a cell: beads routed to it, beads closed."""

    tier: str
    beads: int = 0
    closed_beads: int = 0
    close_rate: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "beads": self.beads,
            "closed_beads": self.closed_beads,
            "close_rate": self.close_rate,
        }


def _tier_rows(
    beads: Sequence[BeadCost], routes: Mapping[str, str]
) -> tuple[TierMetrics, ...]:
    """Close rate per tier, for the beads the routing log actually names.

    A bead with no routing record belongs to no tier. The cell's own close
    rate already counts it, and filing it under `baseline` would credit a
    tier with a bead the router never sent there. A tier the log never names
    gets no row either: an empty table says this arm routed nothing, which is
    a different answer from every tier closing nothing.
    """

    grouped: dict[str, list[BeadCost]] = {}
    for bead in beads:
        tier = routes.get(bead.issue_id or "")
        if tier is not None:
            grouped.setdefault(tier, []).append(bead)

    ordered = [tier for tier in _TIER_ORDER if tier in grouped]
    ordered.extend(sorted(tier for tier in grouped if tier not in _TIER_ORDER))
    rows: list[TierMetrics] = []
    for tier in ordered:
        group = grouped[tier]
        closed = sum(1 for bead in group if bead.closed)
        rows.append(
            TierMetrics(
                tier=tier,
                beads=len(group),
                closed_beads=closed,
                close_rate=closed / len(group),
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class ArmMetrics:
    """What one cell cost and how well it went. None means never reported."""

    fixture: str
    arm: str
    runs: tuple[str, ...] = ()
    beads: int = 0
    closed_beads: int = 0
    cost_per_closed_bead: float | None = None
    close_rate: float | None = None
    worker_errors: int | None = None
    turns: int | None = None
    wall_seconds: float | None = None
    cache_hit_rate: float | None = None
    #: Empty when this cell's seat recorded no routing decisions.
    tiers: tuple[TierMetrics, ...] = ()

    @property
    def null_metrics(self) -> tuple[str, ...]:
        """Every metric this cell could not report, primary metric first."""

        names = (PRIMARY_METRIC,) + GUARDRAIL_METRICS
        return tuple(name for name in names if getattr(self, name) is None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "arm": self.arm,
            "runs": list(self.runs),
            "beads": self.beads,
            "closed_beads": self.closed_beads,
            PRIMARY_METRIC: self.cost_per_closed_bead,
            "close_rate": self.close_rate,
            "worker_errors": self.worker_errors,
            "turns": self.turns,
            "wall_seconds": self.wall_seconds,
            "cache_hit_rate": self.cache_hit_rate,
            "tiers": [row.as_dict() for row in self.tiers],
            "null_metrics": list(self.null_metrics),
        }


def measure_arm(
    fixture: EvalFixture,
    arm: str,
    runs: Sequence[RunCost],
    routes: Mapping[str, str] | None = None,
) -> ArmMetrics:
    """Roll one cell's grind logs into the primary metric and its guardrails.

    `routes` is that cell's bead-to-tier map. It is optional because a cell
    whose arm never routed has none, and the metrics that do not depend on
    routing are measured the same either way.
    """

    sessions: tuple[SessionCost, ...] = tuple(
        session for run in runs for session in run.sessions
    )
    beads: tuple[BeadCost, ...] = rollup_beads(sessions)
    closed = tuple(bead for bead in beads if bead.closed)

    usage = UsageBuckets()
    for bead in beads:
        usage = usage.merged(bead.usage)

    billed = _sum_optional([bead.usage.cost_usd for bead in closed])
    cost_per_closed = None if billed is None or not closed else billed / len(closed)

    turns = _sum_optional([bead.turns for bead in beads])
    wall = _sum_optional([bead.wall_seconds for bead in beads])

    return ArmMetrics(
        fixture=fixture.key,
        arm=arm,
        runs=tuple(run.run_id for run in runs),
        beads=len(beads),
        closed_beads=len(closed),
        cost_per_closed_bead=cost_per_closed,
        close_rate=len(closed) / len(beads) if beads else None,
        # Zero errors is a measurement; no windows at all is not.
        worker_errors=sum(s.errors for s in sessions) if sessions else None,
        turns=int(turns) if turns is not None else None,
        wall_seconds=wall,
        cache_hit_rate=usage.cache_hit_rate,
        tiers=_tier_rows(beads, routes or {}),
    )


def collect_runs(root: Path, fixture: EvalFixture, arm: str) -> tuple[RunCost, ...]:
    """Every grind log one cell's seat wrote, oldest run last."""

    seat = root / seat_name(fixture, arm)
    return tuple(parse_grind_log(path) for path in find_grind_logs(seat, newest=0))


def collect_routes(root: Path, fixture: EvalFixture, arm: str) -> dict[str, str]:
    """Each bead's tier from one cell's routing log, the newest record winning.

    A bead can be routed more than once, because every resumed window writes
    its own record, and the tier that last ran it is the one its outcome
    belongs to. The log is read defensively: it is appended to by workers a
    timeout may kill mid-line, so an undecodable line is skipped rather than
    failing the whole report, and a seat without the log simply routes nothing.
    """

    path = root / seat_name(fixture, arm) / "logs" / ROUTE_LOG_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    routes: dict[str, str] = {}
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        issue_id = record.get("issue_id")
        tier = record.get("tier")
        if isinstance(issue_id, str) and issue_id and isinstance(tier, str) and tier:
            routes[issue_id] = tier
    return routes


@dataclass(frozen=True)
class EvalReport:
    """The before/after comparison, one row per cell."""

    root: Path
    arms: tuple[ArmMetrics, ...]
    #: Empty when the report was rebuilt from logs rather than swept.
    executions: tuple[CellExecution, ...] = ()

    def execution(self, fixture: str, arm: str) -> CellExecution | None:
        """The sweep record for one cell, or None when nothing swept it."""

        for record in self.executions:
            if record.fixture == fixture and record.arm == arm:
                return record
        return None

    @property
    def null_results(self) -> tuple[dict[str, Any], ...]:
        """Every cell that left a metric unreported, and which ones."""

        return tuple(
            {"fixture": arm.fixture, "arm": arm.arm, "metrics": list(arm.null_metrics)}
            for arm in self.arms
            if arm.null_metrics
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "root": str(self.root),
            "primary_metric": PRIMARY_METRIC,
            "guardrails": list(GUARDRAIL_METRICS),
            "fixtures": [fixture.as_dict() for fixture in FIXTURE_PACK],
            "treatments": [item.as_dict() for item in TREATMENTS],
            "arms": [arm.as_dict() for arm in self.arms],
            "executions": [record.as_dict() for record in self.executions],
            "null_results": [dict(entry) for entry in self.null_results],
        }


def build_report(
    root: Path, *, executions: Sequence[CellExecution] = ()
) -> EvalReport:
    """Measure every cell under `root`, including the ones that never ran.

    A cell with no seat still gets a row. Dropping it would turn "we never
    ran this arm" into "this arm has nothing to say", and those are different
    answers to the same comparison. A sweep passes its own records in, so a
    row whose metrics are null can be read against whether its cell failed,
    was skipped, or ran and reported nothing.
    """

    arms = tuple(
        measure_arm(
            fixture,
            arm,
            collect_runs(root, fixture, arm),
            routes=collect_routes(root, fixture, arm),
        )
        for fixture in FIXTURE_PACK
        for arm in ARMS
    )
    return EvalReport(root=root, arms=arms, executions=tuple(executions))


def _cell(value: Any) -> str:
    """A metric as a table cell, with unreported spelled out rather than blank."""

    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _status_cell(record: CellExecution | None) -> str:
    """One cell's sweep outcome, with a failure's exit status kept with it."""

    if record is None:
        return "not swept"
    if record.status == CELL_FAILED:
        return f"failed (exit {record.exit_status})"
    return record.status


def render_report(report: EvalReport) -> str:
    """The report as markdown: one table, the tiers, then the nulls.

    A swept report gains one trailing status column. A report rebuilt from
    logs has no records to show and keeps the table it always had. The
    per-tier section is where a threshold gets rewritten from outcomes rather
    than from taste, so it names the arms that routed and says so plainly
    when none did.
    """

    columns = ["fixture", "arm", "beads", "closed", PRIMARY_METRIC]
    columns.extend(GUARDRAIL_METRICS)
    if report.executions:
        columns.append("status")
    header = "| " + " | ".join(columns) + " |"
    rule = "| --- " * len(columns) + "|"
    lines = [
        f"# Harness evaluation report ({REPORT_SCHEMA})",
        "",
        f"Seat root: {report.root}",
        f"Primary metric: {PRIMARY_METRIC}",
        "",
        header,
        rule,
    ]
    for arm in report.arms:
        cells = [arm.fixture, arm.arm, str(arm.beads), str(arm.closed_beads)]
        cells.append(_cell(arm.cost_per_closed_bead))
        cells.extend(_cell(getattr(arm, name)) for name in GUARDRAIL_METRICS)
        if report.executions:
            cells.append(_status_cell(report.execution(arm.fixture, arm.arm)))
        lines.append("| " + " | ".join(cells) + " |")

    lines.extend(["", "## Per-tier close rate", ""])
    routed = tuple(arm for arm in report.arms if arm.tiers)
    if not routed:
        lines.append("No cell recorded a routing decision.")
    else:
        for arm in routed:
            for row in arm.tiers:
                lines.append(
                    f"- {arm.fixture} / {arm.arm} / {row.tier}: "
                    f"{row.closed_beads}/{row.beads} closed "
                    f"({_cell(row.close_rate)})"
                )

    lines.extend(["", "## Null results", ""])
    if not report.null_results:
        lines.append("Every cell reported every metric.")
    else:
        for entry in report.null_results:
            metrics = ", ".join(entry["metrics"])
            lines.append(f"- {entry['fixture']} / {entry['arm']}: {metrics}")
    return "\n".join(lines) + "\n"


def render_matrix(cells: Sequence[EvalCell]) -> str:
    """The recipe as markdown: one command block per cell, in run order."""

    lines = [
        "# Harness evaluation matrix",
        "",
        f"Fixtures: {', '.join(fixture.title for fixture in FIXTURE_PACK)}",
        f"Arms: {', '.join(ARMS)}",
        "",
        "## Treatments",
        "",
    ]
    for item in TREATMENTS:
        measured = item.measured_by or "unfiled"
        lines.append(f"- `{item.key}` — {item.summary}")
        lines.append(f"  - `.ortusrc`: `{item.enable_line}` (measured by {measured})")
    lines.extend(["", "## Cells", ""])
    for cell in cells:
        lines.append(f"### {cell.fixture} / {cell.arm}")
        lines.append("")
        lines.append("```bash")
        lines.extend(cell.commands)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
