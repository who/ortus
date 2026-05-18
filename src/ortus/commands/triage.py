"""ortus triage <repo> — operator-driven triage with claude-assisted context.

Two-phase flow:

  Phase 1 (claude): a non-interactive `claude -p` session reads the bd
  human-flagged queue, gathers context per issue, and appends one
  JSON envelope per issue to `logs/triage-envelopes.jsonl`.

  Phase 2 (operator): this module reads the envelopes file, prompts
  the operator via typer for each disposition, and applies the
  decisions via direct `bd` subprocess calls.

The split exists because `AskUserQuestion` is an interactive-only
surface and is unreachable under `claude -p` — the agent would silently
no-op the prompt. The Python wrapper owns operator I/O so stdin is
real, and the agent stays scoped to context gathering.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient
from ortus.core.claude import ClaudeRunner
from ortus.core.prompts import resolve_prompt
from ortus.core.repo import resolve_repo


ENVELOPES_FILE = "logs/triage-envelopes.jsonl"

BASE_CHOICES: dict[str, str] = {
    "defer": "Defer        — push to a future date",
    "close": "Close        — resolve and remove from queue",
    "ac": "Revise AC    — rewrite acceptance criteria for a loop to pick up",
    "dismiss": "Dismiss      — remove human label, release back to loops",
    "skip": "Skip         — leave untouched; next",
}

CLOSE_REASONS: dict[str, str] = {
    "wont_fix": "Won't fix",
    "resolved": "Already resolved",
    "superseded": "Superseded",
    "custom": "Custom reason (free-form)",
}


def _make_runner() -> ClaudeRunner:
    return ClaudeRunner()


def _read_envelopes(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    envelopes: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            envelopes.append(json.loads(line))
        except json.JSONDecodeError:
            output.warn(f"skipping malformed envelope line: {line[:80]}…")
    return envelopes


def _print_card(env: dict, idx: int, total: int) -> None:
    iid = env.get("issue_id", "?")
    title = env.get("title", "")
    priority = env.get("priority", "?")
    status = env.get("status", "?")
    summary = (env.get("context_summary") or "").strip()
    rec = env.get("recommended_disposition") or ""
    rationale = (env.get("rationale") or "").strip()
    output.info("")
    output.info(f"── Issue {idx} of {total}: {iid} — {title}  [P{priority}, {status}]")
    if summary:
        output.info(f"Context: {summary}")
    if rec:
        output.info(f"Claude recommends: {rec}")
        if rationale:
            output.info(f"  why: {rationale}")


def _prompt_choice(
    prompt_text: str, choices: dict[str, str], default_key: str
) -> str:
    """Print numbered choices, prompt for index, return the matching key.

    Operator types a number (1..N). Empty input accepts the default.
    """
    keys = list(choices.keys())
    if default_key not in keys:
        default_key = keys[-1]
    default_idx = keys.index(default_key) + 1
    output.info(prompt_text)
    for i, key in enumerate(keys, start=1):
        marker = "*" if key == default_key else " "
        output.info(f"  {marker}{i}) {choices[key]}")
    while True:
        raw = typer.prompt(
            f"Choose [1-{len(keys)}]", default=str(default_idx), show_default=True
        ).strip()
        try:
            idx = int(raw)
        except ValueError:
            output.warn(f"not a number: {raw!r}")
            continue
        if 1 <= idx <= len(keys):
            return keys[idx - 1]
        output.warn(f"out of range: {idx}")


def _bd_write(repo: Path, *args: str) -> bool:
    """Run a bd write subcommand; return True on success, print stderr on failure."""
    proc = subprocess.run(
        ["bd", *args], cwd=str(repo), capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        output.error(f"bd {' '.join(args)} failed: {proc.stderr.strip()}")
        return False
    output.success("applied")
    return True


def _apply_defer(repo: Path, iid: str, today: _dt.date) -> bool:
    presets = {
        "1w": (today + _dt.timedelta(days=7)).isoformat(),
        "1m": (today + _dt.timedelta(days=30)).isoformat(),
        "3m": (today + _dt.timedelta(days=90)).isoformat(),
    }
    labels = {
        "1w": f"1 week    ({presets['1w']})",
        "1m": f"1 month   ({presets['1m']})",
        "3m": f"3 months  ({presets['3m']})",
        "custom": "Custom    (YYYY-MM-DD)",
    }
    pick = _prompt_choice(f"\nDefer {iid}: until when?", labels, default_key="1m")
    if pick == "custom":
        date = typer.prompt("Date (YYYY-MM-DD)").strip()
    else:
        date = presets[pick]
    output.info(f"  $ bd defer {iid} --until={date}")
    return _bd_write(repo, "defer", iid, f"--until={date}")


def _apply_close(repo: Path, iid: str) -> bool:
    pick = _prompt_choice(
        f"\nClose {iid}: reason?", CLOSE_REASONS, default_key="resolved"
    )
    if pick == "custom":
        reason = typer.prompt("Custom reason").strip() or "no reason given"
    else:
        reason = CLOSE_REASONS[pick]
    output.info(f"  $ bd close {iid} --reason={reason!r}")
    return _bd_write(repo, "close", iid, "--reason", reason)


def _apply_ac(repo: Path, iid: str) -> bool:
    output.info(
        f"\nRevise AC for {iid}. Enter new acceptance criteria (single line OK; "
        "use \\n for line breaks). Empty input cancels."
    )
    ac = typer.prompt("AC").strip()
    if not ac:
        output.warn(f"empty AC; skipping {iid}")
        return False
    ac = ac.replace("\\n", "\n")
    output.info(f"  $ bd update {iid} --acceptance <…{len(ac)} chars>")
    return _bd_write(repo, "update", iid, "--acceptance", ac)


def _apply_dismiss(repo: Path, iid: str) -> bool:
    output.info(f"  $ bd update {iid} --remove-label=human --status=open")
    return _bd_write(repo, "update", iid, "--remove-label", "human", "--status", "open")


_APPLY: dict[str, Callable[..., bool]] = {
    "defer": _apply_defer,
    "close": _apply_close,
    "ac": _apply_ac,
    "dismiss": _apply_dismiss,
}


def _drive_operator_loop(
    repo: Path, envelopes: list[dict], today: _dt.date
) -> dict[str, int]:
    counts = {k: 0 for k in BASE_CHOICES}
    counts["fail"] = 0
    for idx, env in enumerate(envelopes, start=1):
        _print_card(env, idx, len(envelopes))
        iid = env.get("issue_id", "?")
        default = env.get("recommended_disposition") or "skip"
        if default not in BASE_CHOICES:
            default = "skip"
        try:
            pick = _prompt_choice(
                f"\nDisposition for {iid}?", BASE_CHOICES, default_key=default
            )
        except (KeyboardInterrupt, EOFError, typer.Abort):
            output.warn("operator interrupted; stopping triage")
            break
        if pick == "skip":
            counts["skip"] += 1
            continue
        applier = _APPLY[pick]
        try:
            ok = applier(repo, iid, today) if pick == "defer" else applier(repo, iid)
        except (KeyboardInterrupt, EOFError, typer.Abort):
            output.warn("operator interrupted; stopping triage")
            break
        counts[pick if ok else "fail"] += 1
    return counts


def triage(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
) -> None:
    """Triage bd `human`-flagged issues: claude gathers context, you decide."""
    target = resolve_repo(repo)
    client = BdClient(target)
    flagged = client.list_human()
    if not flagged:
        output.info("no human-queue items — nothing to triage")
        return

    prompt = resolve_prompt("triage-prompt", repo=target).text

    log_path = target / "logs" / "triage.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    envelopes_path = target / ENVELOPES_FILE
    if envelopes_path.exists():
        envelopes_path.unlink()

    output.info(
        f"triage starting; {len(flagged)} human-flagged issue(s); "
        f"claude log → {log_path.relative_to(target)}"
    )
    rc = _make_runner().run(prompt, repo=target, log_path=log_path)
    if rc != 0:
        output.error(f"claude (context phase) exited {rc}; see {log_path}")
        raise typer.Exit(code=rc)

    envelopes = _read_envelopes(envelopes_path)
    if not envelopes:
        output.error(
            "claude did not write any envelopes",
            hint=(
                f"expected at least one JSON line in {ENVELOPES_FILE}; "
                f"see {log_path.relative_to(target)} for the claude transcript."
            ),
        )
        raise typer.Exit(code=2)

    counts = _drive_operator_loop(target, envelopes, _dt.date.today())
    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    output.success(f"triage complete: {summary or 'no dispositions applied'}")
