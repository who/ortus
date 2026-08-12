"""Deterministic acceptance-criteria runner (Phase L1 of the lean pipeline).

Executes a packet's Criterion checks as subprocesses in a disposable shared
clone of a git ref and records every command, exit code, and bounded output —
the machine that replaces the verifier's mechanical half at zero model tokens.

The command grammar is exactly what readiness v1 validates: the identifier and
code-span regexes are imported from :mod:`ortus.core.readiness`, not restated,
so any packet that passes readiness is runnable here. The tree the commands
run in is a ``git clone --shared`` — never a worktree (cleanup is
unrecoverable under the sandbox, ortus-z7ib) and never an archive (a hatch-vcs
build derives its version from git metadata an archive strips, so nothing in
an archive tree could run; measured both ways on 2026-08-11).

The runner judges nothing a command's exit code does not state: no model
calls, no heuristics about flaky output. Results are data first, rendering
second — :func:`render_tracker_comment` turns a run into durable tracker text.
Nothing in the live pipeline calls this yet; wiring is a separate,
human-landed leaf.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ortus.core import readiness
from ortus.core.git import GitClient

# The readiness grammar, shared by reference: an AC identifier and a
# backticked command mean here exactly what the validator enforced at claim.
_CRITERION_ID = readiness._CRITERION_ID
_CODE_SPAN = readiness._CODE_SPAN
_CHECKS_HEADING = readiness._section("criterion_mapped_checks").heading

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_TIMEOUT = "timeout"

#: Generous by default: a wedged check is reported as timed out rather than
#: waited on forever (the ortus-xjdf lesson applied mechanically).
DEFAULT_TIMEOUT_SECONDS = 600.0
#: Bound applied when a command's captured output is read back for the record.
DEFAULT_OUTPUT_LIMIT = 4_000
#: Sync convention for a uv-managed tree. A fresh clone has no venv, and
#: `uv run` alone installs the project but not the test extras, so `pytest`
#: fails to spawn without this.
DEFAULT_SYNC_COMMAND = "uv sync --all-extras"

_TRUNCATION_MARKER = "\n[... output truncated ...]\n"


@dataclass(frozen=True)
class CriterionCheck:
    """One runnable check: a criterion identifier and its exact command."""

    criterion_id: str
    command: str


@dataclass(frozen=True)
class PacketFailure:
    """A structural defect in the packet's Criterion checks section.

    The runner never guesses a command: a criterion it cannot parse is
    reported as the packet's failure, not skipped or improvised around.
    """

    criterion_id: str
    message: str


@dataclass(frozen=True)
class CriterionResult:
    """Outcome of one criterion command. ``exit_code`` is None on timeout."""

    criterion_id: str
    command: str
    exit_code: int | None
    duration_seconds: float
    output: str
    verdict: str


@dataclass(frozen=True)
class EnvironmentFailure:
    """The run could not prepare a tree for the first criterion.

    Distinct from any criterion's verdict: a clone that cannot materialize or
    a sync command that fails indicts the environment, not the change.
    """

    command: str
    exit_code: int | None
    output: str
    timed_out: bool = False

    @property
    def reason(self) -> str:
        if self.timed_out:
            return f"environment preparation timed out: `{self.command}`"
        if self.exit_code is None:
            return f"environment preparation failed: `{self.command}`"
        return (
            f"environment preparation failed: `{self.command}` "
            f"exited {self.exit_code}"
        )


@dataclass(frozen=True)
class CheckRunResult:
    """Structured result of one AC run against one ref."""

    ref: str
    results: tuple[CriterionResult, ...] = ()
    packet_failures: tuple[PacketFailure, ...] = ()
    environment: EnvironmentFailure | None = None

    @property
    def ok(self) -> bool:
        """True only for a non-empty run in which every criterion passed.

        An empty acceptance_criteria field yields an empty run, and an empty
        run is not a success — nothing was verified.
        """
        return (
            self.environment is None
            and not self.packet_failures
            and bool(self.results)
            and all(record.verdict == VERDICT_PASS for record in self.results)
        )


def parse_criterion_checks(
    acceptance_criteria: object,
) -> tuple[tuple[CriterionCheck, ...], tuple[PacketFailure, ...]]:
    """Extract per-criterion commands from an acceptance_criteria field.

    Reads the same ``## Criterion checks`` section readiness v1 validates,
    with the same identifier and code-span grammar. Lines without an ``AC-N``
    are section prose and skipped; a line with an identifier must carry
    exactly one backticked command, and each identifier may appear only once.
    """
    body = readiness.section_text(acceptance_criteria, _CHECKS_HEADING)
    parsed: list[CriterionCheck] = []
    failures: list[PacketFailure] = []
    seen: set[str] = set()
    for line in body.splitlines():
        ids = [item.upper() for item in _CRITERION_ID.findall(line)]
        if not ids:
            continue
        if len(set(ids)) > 1:
            failures.append(
                PacketFailure(
                    ids[0],
                    f"one check line names several criteria: {line.strip()!r}",
                )
            )
            continue
        criterion_id = ids[0]
        if criterion_id in seen:
            failures.append(
                PacketFailure(criterion_id, f"{criterion_id} appears more than once")
            )
            continue
        seen.add(criterion_id)
        spans = _CODE_SPAN.findall(line)
        if len(spans) != 1:
            failures.append(
                PacketFailure(
                    criterion_id,
                    f"{criterion_id}: expected exactly one backticked command, "
                    f"found {len(spans)}",
                )
            )
            continue
        command = spans[0][1:-1].strip()
        if not command:
            failures.append(
                PacketFailure(criterion_id, f"{criterion_id}: empty command")
            )
            continue
        parsed.append(CriterionCheck(criterion_id, command))
    return tuple(parsed), tuple(failures)


@dataclass(frozen=True)
class _Execution:
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    output: str


def _execute(
    command: str,
    cwd: Path,
    output_path: Path,
    *,
    timeout_seconds: float,
    output_limit: int,
) -> _Execution:
    """Run one packet command through the shell; file-captured, bounded read.

    The command runs exactly as written — readiness validated it, and its
    shell metacharacters are part of its meaning. Output goes to a file and
    is bounded on read, never piped through a filter (the
    pipeline-through-tail pathology must not be rebuilt here). On timeout the
    whole process group is killed, so parallel test workers cannot outlive
    the check that spawned them.
    """
    started = time.monotonic()
    with output_path.open("wb") as sink:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            exit_code: int | None = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            try:
                # start_new_session made the child its own group leader, so
                # its pid is the pgid and the kill reaches every descendant.
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
    return _Execution(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started,
        output=_bounded_read(output_path, output_limit),
    )


def _bounded_read(path: Path, limit: int) -> str:
    """Read captured output back, bounded, keeping the head and the tail.

    Failures announce themselves at both ends — a build error up front, a
    pytest summary at the bottom — so the bound keeps both rather than
    truncating blindly at one.
    """
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    half = max(limit // 2, 1)
    return text[:half] + _TRUNCATION_MARKER + text[-half:]


def _sync_command_for(tree: Path, configured: str | None) -> str | None:
    """Resolve the environment-preparation command for a materialized tree.

    ``None`` means the repository's convention: :data:`DEFAULT_SYNC_COMMAND`
    where uv manages the project (a ``uv.lock`` in the tree), nothing
    otherwise. An explicit command always runs; an explicit ``""`` skips
    preparation entirely.
    """
    if configured is None:
        return DEFAULT_SYNC_COMMAND if (tree / "uv.lock").is_file() else None
    return configured or None


def run_checks(
    repo: Path,
    acceptance_criteria: object,
    ref: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    sync_command: str | None = None,
    scratch_root: Path | None = None,
) -> CheckRunResult:
    """Execute a packet's Criterion checks against `ref`, one record per AC-N.

    Materializes `ref` as a disposable shared clone under a scratch
    directory, prepares its environment, runs each parsed command there with
    a per-command timeout, and removes the scratch tree afterwards. The clone
    lives outside the repository, so accidental writes stay out of the source
    tree; one clone serves every command of the run.
    """
    parsed, packet_failures = parse_criterion_checks(acceptance_criteria)
    if not parsed:
        return CheckRunResult(ref=ref, packet_failures=packet_failures)
    scratch = Path(tempfile.mkdtemp(prefix="ortus-checks-", dir=scratch_root))
    try:
        clone = scratch / "tree"
        reason = GitClient(repo).clone_shared(ref, clone)
        if reason:
            return CheckRunResult(
                ref=ref,
                packet_failures=packet_failures,
                environment=EnvironmentFailure(
                    command=f"git clone --shared @ {ref}",
                    exit_code=None,
                    output=reason,
                ),
            )
        prepare = _sync_command_for(clone, sync_command)
        if prepare is not None:
            prepared = _execute(
                prepare,
                clone,
                scratch / "environment.log",
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
            )
            if prepared.timed_out or prepared.exit_code != 0:
                return CheckRunResult(
                    ref=ref,
                    packet_failures=packet_failures,
                    environment=EnvironmentFailure(
                        command=prepare,
                        exit_code=prepared.exit_code,
                        output=prepared.output,
                        timed_out=prepared.timed_out,
                    ),
                )
        records: list[CriterionResult] = []
        for check in parsed:
            outcome = _execute(
                check.command,
                clone,
                scratch / f"{check.criterion_id}.log",
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
            )
            if outcome.timed_out:
                verdict = VERDICT_TIMEOUT
            elif outcome.exit_code == 0:
                verdict = VERDICT_PASS
            else:
                verdict = VERDICT_FAIL
            records.append(
                CriterionResult(
                    criterion_id=check.criterion_id,
                    command=check.command,
                    exit_code=outcome.exit_code,
                    duration_seconds=outcome.duration_seconds,
                    output=outcome.output,
                    verdict=verdict,
                )
            )
        return CheckRunResult(
            ref=ref, results=tuple(records), packet_failures=packet_failures
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def render_tracker_comment(result: CheckRunResult) -> str:
    """Render a run as durable tracker-comment text: data in, prose out.

    Every command, verdict, and exit code appears; non-passing output is
    quoted (already bounded at capture). An empty run is stated as
    unverified, never as a success.
    """
    if (
        result.environment is None
        and not result.packet_failures
        and not result.results
    ):
        return (
            f"Deterministic AC run @ {result.ref}: no criteria parsed — "
            "nothing verified."
        )
    passed = sum(1 for r in result.results if r.verdict == VERDICT_PASS)
    lines = [
        f"Deterministic AC run @ {result.ref}: "
        f"{'pass' if result.ok else 'fail'} "
        f"({passed}/{len(result.results)} criteria passed)"
    ]
    if result.environment is not None:
        lines.append(f"- environment: {result.environment.reason}")
        if result.environment.output.strip():
            lines.extend(["", "```", result.environment.output.strip(), "```", ""])
    lines.extend(
        f"- packet failure: {failure.message}" for failure in result.packet_failures
    )
    for record in result.results:
        exit_text = (
            "no exit code" if record.exit_code is None else f"exit {record.exit_code}"
        )
        lines.append(
            f"- {record.criterion_id}: {record.verdict} — {exit_text} "
            f"in {record.duration_seconds:.1f}s — `{record.command}`"
        )
        if record.verdict != VERDICT_PASS and record.output.strip():
            lines.extend(["", "```", record.output.strip(), "```", ""])
    return "\n".join(lines).rstrip() + "\n"
