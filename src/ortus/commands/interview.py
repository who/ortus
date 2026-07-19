"""ortus interview <repo> [<feature-id>] — interactive PRD-building interview (idzn.1).

Launches the configured agent with the bundled interview prompt. If a feature
id is supplied, the prompt jumps directly to that feature; otherwise the
verb picks the first open feature (or errors if none exist).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.agent import BackendError, make_runner, resolve_backend
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.prompts import resolve_prompt
from ortus.core.repo import resolve_repo


def _make_runner(backend: str = "claude") -> ClaudeRunner:
    return make_runner(backend)  # type: ignore[arg-type]


def _pick_feature(client: BdClient) -> Optional[str]:
    """Return the oldest (by created_at) open feature, or None.

    bd stores created_at at second resolution, so two features created in
    the same second tie. `bd list --json` does not promise a stable order,
    so the id is a secondary key: ties then resolve the same way on every
    call instead of tracking whatever order bd happened to return.
    """
    features = [i for i in client.list_open() if i.get("issue_type") == "feature"]
    if not features:
        return None
    features.sort(key=lambda i: (i.get("created_at") or "", i["id"]))
    return features[0]["id"]


def interview(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    feature_id: Optional[str] = typer.Argument(
        None, help="Optional feature bd id. Defaults to the first open feature."
    ),
    backend: Optional[str] = typer.Option(
        None,
        "--backend",
        help="Agent backend (claude|codex); defaults from .ortusrc.",
    ),
) -> None:
    """Run an interactive interview to draft a PRD for an open feature."""
    target = resolve_repo(repo)
    try:
        resolved_backend = resolve_backend(backend, repo=target)
    except BackendError as exc:
        output.error(str(exc))
        raise typer.Exit(code=1)

    client = BdClient(target)
    chosen = feature_id or _pick_feature(client)
    if chosen is None:
        output.error(
            "no open features in this workspace",
            hint="create one first (e.g., `ortus plan` or `bd create --type feature ...`)",
        )
        raise typer.Exit(code=1)

    # Substantively verify the chosen id exists. show() raises if not.
    try:
        feature = client.show(chosen)
    except Exception as exc:
        output.error(f"could not load {chosen}: {exc}")
        raise typer.Exit(code=1)

    if feature.get("issue_type") != "feature":
        output.warn(f"{chosen} is type={feature.get('issue_type')!r}, not 'feature'")

    prompt_text = resolve_prompt("interview-prompt", repo=target).text
    expanded = prompt_text.replace("$feature_id", chosen)

    log_path = target / "logs" / "interview.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    runner = _make_runner() if resolved_backend == "claude" else _make_runner("codex")
    output.info(
        f"interview starting for {chosen} via {resolved_backend}; "
        f"log → {log_path.relative_to(target)}"
    )
    rc = runner.run(expanded, repo=target, log_path=log_path)
    if rc != 0:
        output.error(f"interview exited {rc}; see {log_path}")
        # Surface the last few log lines inline so CI failures don't require
        # downloading workspace artifacts to diagnose. The log lives in a
        # tmp dir that pytest may have already torn down by the time the
        # operator reads the failure message.
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        except OSError:
            tail = []
        if tail:
            output.error("interview.log tail:\n  " + "\n  ".join(tail))
        raise typer.Exit(code=rc)
    output.success(f"interview complete for {chosen}")
