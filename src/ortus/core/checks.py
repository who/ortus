"""Deterministic acceptance-criteria runner (Phase L1 of the lean pipeline).

Executes a work spec's Criterion checks as subprocesses in a disposable shared
clone of a git ref and records every command, exit code, and bounded output —
the machine that replaces the verifier's mechanical half at zero model tokens.

The command grammar is exactly what readiness v1 validates: the identifier and
code-span regexes are imported from :mod:`ortus.core.readiness`, not restated,
so any work spec that passes readiness is runnable here. The tree the commands
run in is a ``git clone --shared`` — never a worktree (cleanup is
unrecoverable under the sandbox, ortus-z7ib) and never an archive (a hatch-vcs
build derives its version from git metadata an archive strips, so nothing in
an archive tree could run; measured both ways on 2026-08-11).

The runner judges nothing a command's exit code does not state: no model
calls, no heuristics about flaky output. The red–green proof stays inside
that rule: a criterion tagged `proves-new` on its Observable-criteria line
runs on a second clone at the merge base too, and its verdict is an
inequality between two exit codes — fail on the base, pass on the branch.
`guards-existing` must pass on both; an untagged criterion runs branch-only,
exactly as before kinds existed. Results are data first, rendering
second — :func:`render_tracker_comment` turns a run into durable tracker text.
Nothing in the live pipeline calls this yet; wiring is a separate,
human-landed task.
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

# The readiness grammar, shared by reference: an AC identifier, a backticked
# command, and a kind tag mean here exactly what the validator accepted at
# claim.
_CRITERION_ID = readiness._CRITERION_ID
_CODE_SPAN = readiness._CODE_SPAN
_CRITERION_KIND = readiness._CRITERION_KIND
_CHECKS_HEADING = readiness._section("criterion_mapped_checks").heading
_OBSERVABLE_HEADING = readiness._section("observable_criteria").heading

#: The red–green criterion kinds. `proves-new` must fail on the merge base
#: and pass on the branch — a test that also passes without the change proves
#: nothing and is rejected mechanically. `guards-existing` must pass on both.
#: An untagged criterion runs on the branch only, exactly as before kinds.
KIND_PROVES_NEW = readiness.CRITERION_KIND_PROVES_NEW
KIND_GUARDS_EXISTING = readiness.CRITERION_KIND_GUARDS_EXISTING

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_TIMEOUT = "timeout"
#: A `proves-new` criterion that passed on the merge base too: the test
#: proves nothing about the change. Distinct from fail because "your test
#: proves nothing" is more actionable than "failed" — and never a pass.
VERDICT_VACUOUS = "vacuous"
#: A `guards-existing` criterion that failed on the merge base, which usually
#: indicts the criterion or the base, not the change.
VERDICT_BROKEN_BASE = "broken-base"

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
    """One runnable check: a criterion identifier and its exact command.

    ``kind`` is the optional red–green tag from the Observable-criteria line
    (:data:`KIND_PROVES_NEW` or :data:`KIND_GUARDS_EXISTING`); ``None`` keeps
    the criterion branch-only. Never inferred — the tag is the work spec's word.
    """

    criterion_id: str
    command: str
    kind: str | None = None


@dataclass(frozen=True)
class PacketFailure:
    """A structural defect in the work spec's Criterion checks section.

    The runner never guesses a command: a criterion it cannot parse is
    reported as the work spec's failure, not skipped or improvised around.
    """

    criterion_id: str
    message: str


@dataclass(frozen=True)
class CriterionResult:
    """Outcome of one criterion command. ``exit_code`` is None on timeout.

    ``exit_code``, ``duration_seconds`` and ``output`` describe the branch
    run, exactly as before kinds existed. A tagged criterion additionally
    carries its merge-base run: ``base_exit_code`` is None when the base run
    timed out, and the folded ``verdict`` may then be
    :data:`VERDICT_VACUOUS` or :data:`VERDICT_BROKEN_BASE`.
    """

    criterion_id: str
    command: str
    exit_code: int | None
    duration_seconds: float
    output: str
    verdict: str
    kind: str | None = None
    base_exit_code: int | None = None
    base_output: str = ""


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
    Each check also carries the optional kind tag from its Observable-criteria
    line — the tag is data on the criterion, so a work spec claimed before kinds
    existed parses correctly, just untagged.
    """
    kinds = _criterion_kinds(acceptance_criteria)
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
        parsed.append(
            CriterionCheck(criterion_id, command, kinds.get(criterion_id))
        )
    return tuple(parsed), tuple(failures)


def _criterion_kinds(acceptance_criteria: object) -> dict[str, str]:
    """Kind tags by criterion identifier, from the Observable-criteria lines.

    Only the two known kinds match; any other parenthesised text is section
    prose, so a pre-kind work spec's asides stay asides and its criteria stay
    branch-only. A kind is never inferred for an untagged criterion.
    """
    body = readiness.section_text(acceptance_criteria, _OBSERVABLE_HEADING)
    return {
        criterion_id.upper(): kind.lower()
        for criterion_id, kind in _CRITERION_KIND.findall(body)
    }


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
    """Run one work-spec command through the shell; file-captured, bounded read.

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


def _fold_verdicts(kind: str, base: _Execution, branch: _Execution) -> str:
    """Fold a tagged criterion's two runs into one verdict.

    A timeout on either tree is a timeout, never anything else: a wedged
    command is not evidence, so it can never stand in for the base failure
    `proves-new` requires. `proves-new` passes only as fail-on-base AND
    pass-on-branch; pass-on-both is vacuous. `guards-existing` passes only
    as pass-on-both; fail-on-base is broken-base.
    """
    if base.timed_out or branch.timed_out:
        return VERDICT_TIMEOUT
    if kind == KIND_PROVES_NEW:
        if branch.exit_code != 0:
            return VERDICT_FAIL
        return VERDICT_PASS if base.exit_code != 0 else VERDICT_VACUOUS
    if base.exit_code != 0:
        return VERDICT_BROKEN_BASE
    return VERDICT_PASS if branch.exit_code == 0 else VERDICT_FAIL


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


def _materialize_tree(
    git: GitClient,
    ref: str,
    target: Path,
    log_path: Path,
    *,
    sync_command: str | None,
    timeout_seconds: float,
    output_limit: int,
) -> EnvironmentFailure | None:
    """Clone `ref` at `target` and prepare its environment; None on success.

    Both trees of a red–green run are built exactly this way — disposable
    shared clones with prepared environments, never archives, which cannot
    build here (the version derives from vcs metadata an archive strips).
    """
    reason = git.clone_shared(ref, target)
    if reason:
        return EnvironmentFailure(
            command=f"git clone --shared @ {ref}",
            exit_code=None,
            output=reason,
        )
    prepare = _sync_command_for(target, sync_command)
    if prepare is None:
        return None
    prepared = _execute(
        prepare,
        target,
        log_path,
        timeout_seconds=timeout_seconds,
        output_limit=output_limit,
    )
    if prepared.timed_out or prepared.exit_code != 0:
        return EnvironmentFailure(
            command=prepare,
            exit_code=prepared.exit_code,
            output=prepared.output,
            timed_out=prepared.timed_out,
        )
    return None


def run_checks(
    repo: Path,
    acceptance_criteria: object,
    ref: str,
    *,
    base_ref: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    sync_command: str | None = None,
    scratch_root: Path | None = None,
) -> CheckRunResult:
    """Execute a work spec's Criterion checks against `ref`, one record per AC-N.

    Materializes `ref` as a disposable shared clone under a scratch
    directory, prepares its environment, runs each parsed command there with
    a per-command timeout, and removes the scratch tree afterwards. The clone
    lives outside the repository, so accidental writes stay out of the source
    tree; one clone serves every command of the run.

    When the work spec tags criteria with kinds, `base_ref` names the other end
    of the ref pair: a second shared clone is checked out once per run at the
    merge base of `base_ref` and `ref`, tagged commands run on both trees,
    and the two exit codes fold into one verdict. Tagged criteria with no
    establishable merge base are an environment failure — reported, never
    improvised around by running branch-only.
    """
    parsed, packet_failures = parse_criterion_checks(acceptance_criteria)
    if not parsed:
        return CheckRunResult(ref=ref, packet_failures=packet_failures)
    git = GitClient(repo)
    scratch = Path(tempfile.mkdtemp(prefix="ortus-checks-", dir=scratch_root))
    try:
        clone = scratch / "tree"
        environment = _materialize_tree(
            git,
            ref,
            clone,
            scratch / "environment.log",
            sync_command=sync_command,
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )
        if environment is not None:
            return CheckRunResult(
                ref=ref, packet_failures=packet_failures, environment=environment
            )
        base_clone: Path | None = None
        if any(check.kind is not None for check in parsed):
            if base_ref is None:
                return CheckRunResult(
                    ref=ref,
                    packet_failures=packet_failures,
                    environment=EnvironmentFailure(
                        command="git merge-base",
                        exit_code=None,
                        output=(
                            "the work spec tags criteria with kinds, but no "
                            "base ref was supplied to take a merge base with"
                        ),
                    ),
                )
            base_oid = git.merge_base(base_ref, ref)
            if not base_oid:
                return CheckRunResult(
                    ref=ref,
                    packet_failures=packet_failures,
                    environment=EnvironmentFailure(
                        command=f"git merge-base {base_ref} {ref}",
                        exit_code=None,
                        output=(
                            f"no merge base between {base_ref!r} and {ref!r}"
                        ),
                    ),
                )
            base_clone = scratch / "base"
            environment = _materialize_tree(
                git,
                base_oid,
                base_clone,
                scratch / "environment.base.log",
                sync_command=sync_command,
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
            )
            if environment is not None:
                return CheckRunResult(
                    ref=ref,
                    packet_failures=packet_failures,
                    environment=environment,
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
            if check.kind is None or base_clone is None:
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
                continue
            base_outcome = _execute(
                check.command,
                base_clone,
                scratch / f"{check.criterion_id}.base.log",
                timeout_seconds=timeout_seconds,
                output_limit=output_limit,
            )
            records.append(
                CriterionResult(
                    criterion_id=check.criterion_id,
                    command=check.command,
                    exit_code=outcome.exit_code,
                    duration_seconds=outcome.duration_seconds,
                    output=outcome.output,
                    verdict=_fold_verdicts(check.kind, base_outcome, outcome),
                    kind=check.kind,
                    base_exit_code=base_outcome.exit_code,
                    base_output=base_outcome.output,
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
        f"- work-spec failure: {failure.message}" for failure in result.packet_failures
    )
    for record in result.results:
        exit_text = (
            "no exit code" if record.exit_code is None else f"exit {record.exit_code}"
        )
        if record.kind is None:
            lines.append(
                f"- {record.criterion_id}: {record.verdict} — {exit_text} "
                f"in {record.duration_seconds:.1f}s — `{record.command}`"
            )
            if record.verdict != VERDICT_PASS and record.output.strip():
                lines.extend(["", "```", record.output.strip(), "```", ""])
            continue
        base_text = (
            "no exit code"
            if record.base_exit_code is None
            else f"exit {record.base_exit_code}"
        )
        lines.append(
            f"- {record.criterion_id} ({record.kind}): {record.verdict} — "
            f"base {base_text}, branch {exit_text} "
            f"in {record.duration_seconds:.1f}s — `{record.command}`"
        )
        if record.verdict != VERDICT_PASS:
            # Quote the tree that indicts: the base run for a vacuous,
            # broken-base, or base-timeout verdict, the branch run otherwise.
            blames_base = record.verdict in (
                VERDICT_VACUOUS,
                VERDICT_BROKEN_BASE,
            ) or (
                record.verdict == VERDICT_TIMEOUT and record.exit_code is not None
            )
            output = record.base_output if blames_base else record.output
            if output.strip():
                lines.extend(["", "```", output.strip(), "```", ""])
    return "\n".join(lines).rstrip() + "\n"
