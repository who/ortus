"""ortus plan <repo> [<PRD>] — decompose a PRD (or freeform idea) into bd issues.

Ports ortus/idea.sh's PRD-intake and idea-expansion flows. Explicit <repo>
arg eliminates the cd-to-PRD-dir bug from idea.sh (FR-014).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.agent import BackendError, make_runner, resolve_backend
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.codegraph import (
    CodeGraphAdapter,
    CodeGraphMode,
    CodeGraphPhase,
    CodeGraphUnavailable,
    CodeGraphCapability,
    append_normalized,
    parse_transcript,
    phase_contract,
    require_handshake,
)
from ortus.core.config import DEFAULT_CODEGRAPH_MODE, load_config
from ortus.core.profiles import AgentProfile, Phase, ProfileError
from ortus.core.prompts import resolve_prompt, substitute
from ortus.core.readiness import (
    ReadinessReport,
    failed_reports,
    spec_markdown,
    validate_issues,
)
from ortus.core.repo import resolve_repo


def _make_runner(backend: str = "claude", *, repo: Path | None = None) -> ClaudeRunner:
    """Indirection so tests can swap in a fake backend binary."""
    return make_runner(backend, repo=repo)  # type: ignore[arg-type]


def _make_codegraph() -> CodeGraphAdapter:
    """Indirection for end-to-end tests with a fake adapter."""
    return CodeGraphAdapter()


def _plan_prompt(repo: Path) -> str:
    """Resolve the plan prompt with the generated readiness contract filled in.

    The contract is rendered from readiness.py rather than restated here, so a
    schema change cannot leave the planner teaching sections the validator no
    longer enforces.
    """
    prompt = resolve_prompt("plan-prompt", repo=repo).text
    return substitute(prompt, readiness_spec=spec_markdown())


def _decompose_prd(
    repo: Path,
    prd: Path,
    *,
    log_path: Path,
    backend: str = "claude",
    profile: AgentProfile | None = None,
    contract: str = "",
    capability: CodeGraphCapability | None = None,
) -> int:
    """Run claude with the plan prompt, expanded to reference the PRD path."""
    prompt = _plan_prompt(repo)
    # The plan-prompt uses literal "$prd_path" as a placeholder for the absolute
    # PRD path; substitute it before handing to claude. Last, so a PRD path
    # containing a dollar sign is never itself read as a placeholder.
    expanded = prompt.replace("$prd_path", str(prd.resolve())) + contract
    runner = (
        _make_runner() if backend == "claude" else _make_runner(backend, repo=repo)
    )
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        configure(capability)
    return runner.run(expanded, repo=repo, log_path=log_path, profile=profile)


def _expand_idea(
    repo: Path,
    *,
    log_path: Path,
    backend: str = "claude",
    profile: AgentProfile | None = None,
    contract: str = "",
    capability: CodeGraphCapability | None = None,
) -> int:
    """Run the interactive idea-expansion flow (interview→PRD→tasks)."""
    # Use the grind prompt's interview entry-point indirectly; for now we
    # just hand claude a freeform "interview the user about their idea"
    # instruction. Phase 3 idzn.1 fleshes out the full interview prompt.
    runner = (
        _make_runner() if backend == "claude" else _make_runner(backend, repo=repo)
    )
    configure = getattr(runner, "configure_codegraph", None)
    if callable(configure):
        configure(capability)
    interview = (
        "The user invoked `ortus plan <repo>` without a PRD. Run an interactive "
        "idea-expansion interview: ask 3-7 questions to clarify the goal, then "
        "draft a brief PRD inline. Treat that inline PRD as the input to the "
        "planning instructions below."
    )
    plan_prompt = _plan_prompt(repo).replace(
        "$prd_path", "the PRD drafted in this conversation"
    )
    return runner.run(
        interview + "\n\n" + plan_prompt + contract,
        repo=repo,
        log_path=log_path,
        profile=profile,
    )


def _issue_reports(
    client: BdClient, issue_ids: list[str]
) -> tuple[ReadinessReport, ...]:
    """Load authoritative fields rather than relying on compact list output."""

    return validate_issues(client.show(issue_id) for issue_id in issue_ids)


def plan(
    repo: Optional[Path] = typer.Argument(
        None,
        help=(
            "Target repo directory. Defaults to $PWD; no walk-up. "
            "If a single positional file is given instead, it is treated as <PRD>."
        ),
    ),
    prd: Optional[Path] = typer.Argument(
        None, help="Optional PRD path. If omitted, runs the idea-interview flow."
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help=(
            "claude, codex, grok, local, or opencode; defaults from .ortusrc "
            "('all' is an init provisioning option, not a run backend)."
        ),
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Override the planning profile model for this run."
    ),
    reasoning_effort: Optional[str] = typer.Option(
        None,
        "--reasoning-effort",
        help="Override the planning profile reasoning effort for this run.",
    ),
    codegraph: Optional[CodeGraphMode] = typer.Option(
        None,
        "--codegraph",
        help="CodeGraph policy: off|auto|required (defaults from .ortusrc).",
        case_sensitive=False,
    ),
) -> None:
    """Decompose a PRD into bd issues, or interview-then-PRD-then-decompose."""
    # Disambiguate: if the operator passed a single positional that points at
    # a file (e.g. `ortus plan ~/prd.md` from inside a workspace), treat it
    # as the PRD and default the repo to $PWD. Mirrors `ortus check`'s
    # PWD-default convention while preserving `ortus plan <repo> <prd>`.
    if repo is not None and prd is None and repo.is_file():
        prd = repo
        repo = None

    target = resolve_repo(repo)
    try:
        resolved_backend = resolve_backend(backend, repo=target)
        config = load_config(repo=target)
        profile = config.resolve_profile(
            resolved_backend,
            Phase.PLAN,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    except (BackendError, ProfileError) as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    output.progress("plan", f"target: {target}")
    output.progress("plan", f"phase profile: {profile.display_name}")

    configured_mode = config.get("codegraph", DEFAULT_CODEGRAPH_MODE)
    try:
        mode = codegraph or CodeGraphMode(configured_mode)
    except ValueError:
        output.error(
            f"invalid CodeGraph mode {configured_mode!r}; expected off, auto, or required"
        )
        raise typer.Exit(code=1)
    adapter = _make_codegraph()
    output.progress("plan", f"CodeGraph probe (mode={mode.value})")
    try:
        probe = adapter.probe(target, mode, backend=resolved_backend)
    except CodeGraphUnavailable as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    if mode is CodeGraphMode.OFF:
        output.progress("plan", "CodeGraph disabled by policy")
    elif probe.available:
        output.progress(
            "plan", "CodeGraph child registration ready; awaiting handshake"
        )
    else:
        output.progress("plan", f"CodeGraph fallback: {probe.reason}")

    if prd is not None and not prd.is_file():
        output.error(f"PRD not found at {prd}")
        raise typer.Exit(code=1)

    log_dir = target / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"plan-{ts}.log"

    client = BdClient(target)
    before = {i["id"] for i in client.list_all()}

    if prd:
        output.progress("plan", f"reading PRD from {prd}")
        output.progress(
            "plan",
            f"decomposing PRD via {resolved_backend} (this typically takes 1-3 min)",
        )
        rc = _decompose_prd(
            target,
            prd,
            log_path=log_path,
            backend=resolved_backend,
            profile=profile,
            contract=phase_contract(CodeGraphPhase.PLANNING, probe),
            capability=probe.capability,
        )
    else:
        output.progress(
            "plan",
            f"no PRD given; running idea-expansion via {resolved_backend}",
        )
        rc = _expand_idea(
            target,
            log_path=log_path,
            backend=resolved_backend,
            profile=profile,
            contract=phase_contract(CodeGraphPhase.PLANNING, probe),
            capability=probe.capability,
        )
    if rc != 0:
        output.error(f"plan failed ({resolved_backend} exit {rc}); see {log_path}")
        raise typer.Exit(code=rc)

    summary = parse_transcript(log_path, phase=CodeGraphPhase.PLANNING, probe=probe)
    append_normalized(log_path, summary)
    try:
        require_handshake(summary)
    except CodeGraphUnavailable as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)
    output.progress(
        "plan",
        f"CodeGraph query summary: {len(summary.events)} queries, "
        f"{sum(not event.success for event in summary.events)} failures",
    )

    output.progress("plan", "scanning bd workspace for newly-created issues")
    after = client.list_all()
    new_ids = [i["id"] for i in after if i["id"] not in before]

    if not new_ids:
        # claude can exit 0 while its own tool calls failed (e.g. a sandbox
        # that cannot initialize, so every `bd create` in the generated script
        # errors out). Exiting 0 here made that look like a successful plan and
        # sent operators hunting a phantom decomposition regression — see
        # ortus-jke7. A plan that creates nothing is a failed plan.
        output.error(
            "plan produced no issues; the decomposition session exited 0 but "
            f"created nothing. Inspect {log_path} for failed tool calls."
        )
        raise typer.Exit(code=1)

    output.progress("plan", "validating readiness of newly-created issues")
    reports = _issue_reports(client, new_ids)
    defects = failed_reports(reports)
    if defects:
        for report in defects:
            output.error(f"readiness: {report.diagnostic()}")
        output.error(
            "plan left executable issues incomplete; no work was claimed"
        )
        output.error(
            "readiness: re-check after repair, without a run: ortus validate "
            f"{target} {' '.join(report.issue_id for report in defects)}"
        )
        raise typer.Exit(code=1)

    # Plan metadata travels with every implementation work spec. This remains
    # useful in auto/off mode because an explicit fallback is durable evidence.
    for issue_id in new_ids:
        client.add_comment(issue_id, summary.report())

    output.progress("plan", f"done ({len(new_ids)} new issue(s) created)")
    output.success(f"plan created {len(new_ids)} issue(s) in {target}/.beads/")
    output.table(
        ["id", "type", "priority", "title"],
        [
            (i["id"], i["issue_type"], f"P{i['priority']}", i["title"])
            for i in after
            if i["id"] in set(new_ids)
        ],
    )
    output.info(f"\nNext: [bold]ortus grind {target}[/bold]")
