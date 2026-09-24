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

Null is a result. A provider that never reports dollars (Codex) leaves the
primary metric unset for its arm, and an arm whose seat was never run leaves
every metric unset. Neither is zero-filled: a zero cost and an unreported
cost answer the operator's question in opposite directions, so the report
carries `None` through to JSON `null` and names every null metric explicitly
under `null_results`.

Nothing here runs an agent. The measurements come from the grind logs already
on disk via `ortus.core.cost`, and the arm recipes are emitted as commands for
the operator (or a shell loop) to run, which keeps this module hermetic and
re-runnable long after the runs themselves finished.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Sequence

from ortus.core.cost import (
    BeadCost,
    RunCost,
    SessionCost,
    UsageBuckets,
    find_grind_logs,
    parse_grind_log,
    rollup_beads,
)

#: Package holding the fixture PRDs. Installed with the wheel, so the paths
#: the recipe hands to `ortus plan` resolve outside a checkout too.
EVALPACK_PACKAGE = "ortus.evalpack"

#: Bumped whenever a metric is added, renamed, or changes meaning. A reader
#: comparing two reports checks this before comparing anything else.
REPORT_SCHEMA = "harness-eval-report/v1"

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
            "null_metrics": list(self.null_metrics),
        }


def measure_arm(
    fixture: EvalFixture, arm: str, runs: Sequence[RunCost]
) -> ArmMetrics:
    """Roll one cell's grind logs into the primary metric and its guardrails."""

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
    )


def collect_runs(root: Path, fixture: EvalFixture, arm: str) -> tuple[RunCost, ...]:
    """Every grind log one cell's seat wrote, oldest run last."""

    seat = root / seat_name(fixture, arm)
    return tuple(parse_grind_log(path) for path in find_grind_logs(seat, newest=0))


@dataclass(frozen=True)
class EvalReport:
    """The before/after comparison, one row per cell."""

    root: Path
    arms: tuple[ArmMetrics, ...]

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
            "null_results": [dict(entry) for entry in self.null_results],
        }


def build_report(root: Path) -> EvalReport:
    """Measure every cell under `root`, including the ones that never ran.

    A cell with no seat still gets a row. Dropping it would turn "we never
    ran this arm" into "this arm has nothing to say", and those are different
    answers to the same comparison.
    """

    arms = tuple(
        measure_arm(fixture, arm, collect_runs(root, fixture, arm))
        for fixture in FIXTURE_PACK
        for arm in ARMS
    )
    return EvalReport(root=root, arms=arms)


def _cell(value: Any) -> str:
    """A metric as a table cell, with unreported spelled out rather than blank."""

    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_report(report: EvalReport) -> str:
    """The report as markdown: one table, plus the nulls named underneath."""

    header = (
        "| fixture | arm | beads | closed | "
        + " | ".join((PRIMARY_METRIC,) + GUARDRAIL_METRICS)
        + " |"
    )
    rule = "| --- " * (4 + 1 + len(GUARDRAIL_METRICS)) + "|"
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
        lines.append("| " + " | ".join(cells) + " |")

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
