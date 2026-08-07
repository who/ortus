"""Bounded readiness repair pass shared by the plan and grind verbs.

`ortus plan` owned this pass privately, which meant the only way to repair an
unready packet was to run the plan verb. It lives in core so grind can reuse
the identical update-in-place repair without importing a command module.

The pass is deliberately narrow: it may only fill the description, design, and
acceptance-criteria fields of issues it was named. Creating a replacement issue
is the failure mode that silently grows the queue, so the guard that detects it
travels with the pass rather than with any one caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Protocol

from ortus.core.agent import make_runner
from ortus.core.codegraph import CodeGraphCapability
from ortus.core.profiles import AgentProfile
from ortus.core.prompts import resolve_prompt
from ortus.core.readiness import ReadinessReport


class _Runner(Protocol):
    """The subset of the backend runners this pass depends on."""

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        profile: AgentProfile | None,
    ) -> int: ...


RunnerFactory = Callable[..., _Runner]


class RepairCreatedReplacements(RuntimeError):
    """A repair pass created issues instead of updating the named ones."""

    def __init__(self, issue_ids: Iterable[str]) -> None:
        self.issue_ids = tuple(issue_ids)
        super().__init__(
            "readiness repair created replacement issue(s), which is forbidden: "
            + ", ".join(self.issue_ids)
        )


def _default_runner_factory(backend: str = "claude") -> _Runner:
    """Indirection so callers (and tests) can swap in a fake backend binary."""

    return make_runner(backend)  # type: ignore[arg-type]


def readiness_repair_prompt(reports: tuple[ReadinessReport, ...]) -> str:
    """Build a bounded repair request that can only update named issues."""

    diagnostics = "\n".join(f"- {report.diagnostic()}" for report in reports)
    ids = ", ".join(report.issue_id for report in reports)
    return f"""READINESS REPAIR PASS (one pass only).

The planning run created executable issues that fail readiness schema v1.
Repair ONLY these existing issue IDs: {ids}

Exact failures:
{diagnostics}

Use `bd show <id> --json` and `bd update <id>` to fill the existing
description, design, and acceptance-criteria fields. Do not run `bd create`,
do not close, replace, supersede, or rename an issue, and do not change issue
dependencies. Preserve all sound detail already present. Every repaired leaf
must use readiness schema v1 with these exact field headings:

- description: `## Objective`, `## Behavioral context`
- design: `## Readiness schema` (body `v1`), `## Scope`, `## Non-goals`,
  `## Concrete locations`, `## Resolved decisions`,
  `## Compatibility constraints`, `## Ordered steps` (numbered),
  `## Dependencies`, `## Edge cases`, `## Plan-gap guidance`
- acceptance criteria: `## Observable criteria` with unique AC-N identifiers,
  `## Criterion checks` mapping every AC-N exactly once to an exact command or
  deterministic check, and `## Targeted tests` with exact bounded test commands

End immediately after updating the named IDs.
"""


def replacement_ids(
    before: Iterable[str], after: Iterable[str]
) -> tuple[str, ...]:
    """Issue ids that appeared while the repair pass was running."""

    return tuple(sorted(set(after) - set(before)))


def guard_no_replacements(before: Iterable[str], after: Iterable[str]) -> None:
    """Reject a repair pass that added issue ids instead of updating them."""

    unexpected = replacement_ids(before, after)
    if unexpected:
        raise RepairCreatedReplacements(unexpected)


def repair_readiness(
    repo: Path,
    reports: tuple[ReadinessReport, ...],
    *,
    log_path: Path,
    backend: str,
    profile: AgentProfile,
    contract: str,
    capability: CodeGraphCapability | None,
    context: str = "",
    timeout: float | None = None,
    runner_factory: RunnerFactory = _default_runner_factory,
) -> int:
    """Run one fresh planning-profile subprocess to repair existing packets.

    `context` is extra grounding appended after the repair request (grind has
    no PRD, so it names each issue's parent epic instead). `timeout` bounds the
    subprocess for callers that already run under a watchdog; it is forwarded
    only when set so runners without the kwarg keep working.
    """

    if not reports:
        return 0
    runner = runner_factory() if backend == "claude" else runner_factory("codex")
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        configure(capability)
    prompt = resolve_prompt("plan-prompt", repo=repo).text
    prompt += "\n\n" + readiness_repair_prompt(reports)
    if context:
        prompt += "\n" + context
    prompt += contract
    if timeout is not None:
        return runner.run(
            prompt,
            repo=repo,
            log_path=log_path,
            profile=profile,
            timeout=timeout,  # type: ignore[call-arg]
        )
    return runner.run(prompt, repo=repo, log_path=log_path, profile=profile)
