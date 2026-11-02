"""Dream's last stage: render the validated result and file one decision bead.

The report is the whole output of a Dream run. It is written to
`logs/dream-<ts>.md` and, unless the operator asked for `--no-file` or there
is nothing to say, filed as one bead of type decision labeled `human` and
`dream`, so `ortus human` lists it and the next run reads it back through
`load_decisions()`.

Every `bd remember` line the report carries ends in a `[src:<bead-id>]` tail
naming the decision that proposed it. The id does not exist until the bead
does, so the bead is created with a placeholder and its description is then
rewritten with the real id. Dream never runs those lines itself: pasting them
is Andy's answer, and `bd human respond` records which ones he accepted.
"""

from __future__ import annotations

import datetime as _dt
import json
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from ortus.core.bd import BdError
from ortus.core.dream_decisions import DECISION_TYPE, DREAM_LABEL, render_fingerprints_block
from ortus.core.dream_transcripts import scrub
from ortus.core.dream_validate import HelpedRow, ValidationResult

#: Stands in for the decision bead's id until `bd create` has minted it.
PLACEHOLDER = "DREAM_BEAD"

#: The audit-trail actor every Dream write is recorded under.
DREAM_ACTOR = "ortus-dream"

#: Shown wherever the bead id would be when no bead was filed.
NOT_FILED = "not filed"

STATE_FILE = Path(".ortus") / "dream" / "state.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING = 3


@dataclass(frozen=True)
class RunFacts:
    """What the run did and cost, for the report's `## Run facts` section."""

    backend: str
    reader: str
    orchestrator: str
    windows_read: int
    windows_reused: int
    windows_fed: int
    chunked_windows: int
    usage: Mapping[str, Any] | None
    cost_usd: float | None
    memory_chars: int
    memory_keys: int
    prime_chars: int | None
    #: The branch windows were checked against, "unavailable" with no git or
    #: default branch, or None when the run did not report it.
    reachability: str | None = None
    #: Bead ids whose windows were skipped for having no commit on that branch.
    not_on_main: tuple[str, ...] = ()


def title(result: ValidationResult, windows: int, today: _dt.date) -> str:
    """`Dream <date>: <n> memory proposals from <w> windows[, <f> follow-ups]`."""

    text = (
        f"Dream {today.isoformat()}: {len(result.proposals)} memory proposals "
        f"from {windows} windows"
    )
    if result.followups:
        text += f", {len(result.followups)} follow-ups"
    return text


def command(kind: str, key: str, body: str, bead_id: str) -> str:
    """The paste-ready bd line for one proposal."""

    if kind == "prune":
        return f"bd forget {shlex.quote(key)}"
    return f"bd remember {shlex.quote(f'{body} [src:{bead_id}]')} --key {shlex.quote(key)}"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _helped_row(row: HelpedRow) -> str:
    return (
        f"| {row.key} | {_rate(row.before_rate)} | {_rate(row.after_rate)} "
        f"| {row.before_beads}/{row.after_beads} |"
    )


def _proposals(result: ValidationResult, bead_id: str) -> list[str]:
    lines = ["## Proposals", ""]
    if not result.proposals:
        lines.extend(["None.", ""])
    for index, kept in enumerate(result.proposals, start=1):
        proposal = kept.proposal
        stats = kept.stats
        lines.extend(
            [
                f"### P{index} {proposal.kind} {proposal.key}",
                "",
                f"Pattern: {_one_line(proposal.pattern)}",
                "",
                f"Why: {_one_line(proposal.why)}",
                "",
                f"beads={stats.get('beads', 0)} windows={stats.get('windows', 0)} "
                f"turns_wasted={stats.get('turns_wasted', 0)}",
                "",
            ]
        )
        for quote, label in kept.examples:
            lines.extend([f"{label}", f"> {_one_line(quote)}", ""])
        lines.extend(
            [
                "```bash",
                command(proposal.kind, proposal.key, proposal.body, bead_id),
                "```",
                "",
            ]
        )
    return lines


def _followups(result: ValidationResult) -> list[str]:
    lines = ["## Follow-ups (not memories)", ""]
    if not result.followups:
        lines.extend(["None.", ""])
    for index, followup in enumerate(result.followups, start=1):
        lines.extend(
            [
                f"### F{index} {_one_line(followup.title)}",
                "",
                f"Why: {_one_line(followup.why)}",
                "",
                f"Sources: {', '.join(followup.sources) or 'none'}",
                "",
            ]
        )
    return lines


def _helped(result: ValidationResult) -> list[str]:
    lines = ["## Did it help?", ""]
    if not result.helped_rows:
        return [*lines, "No accepted Dream memories yet.", ""]
    return [
        *lines,
        "| key | before hits/100 | after hits/100 | beads before/after |",
        "| --- | --- | --- | --- |",
        *(_helped_row(row) for row in result.helped_rows),
        "",
    ]


def _usage_line(usage: Mapping[str, Any] | None) -> str:
    if not usage:
        return "unavailable"
    parts = [
        f"{name}={value}"
        for name, value in usage.items()
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return " ".join(parts) or "unavailable"


def _reach_lines(result: ValidationResult, facts: RunFacts) -> list[str]:
    lines: list[str] = []
    if facts.reachability is not None:
        lines.append(f"- reachability: {facts.reachability}")
    if facts.not_on_main:
        lines.append(f"- skipped not-on-main: {', '.join(facts.not_on_main)}")
    lines.extend(
        f"- stale-ref drop: {_one_line(name)} (missing: {', '.join(names)})"
        for name, names in result.stale_refs
    )
    return lines


def _facts(result: ValidationResult, facts: RunFacts) -> list[str]:
    drops = Counter(reason for _, reason in result.drops)
    dropped = ", ".join(f"{reason}={count}" for reason, count in sorted(drops.items()))
    cost = "unavailable" if facts.cost_usd is None else f"${facts.cost_usd:.4f}"
    prime = "unavailable" if facts.prime_chars is None else str(facts.prime_chars)
    return [
        "## Run facts",
        "",
        f"- backend: {facts.backend}",
        f"- reader: {facts.reader}",
        f"- orchestrator: {facts.orchestrator}",
        f"- windows: {facts.windows_read} read, {facts.windows_reused} reused, "
        f"{facts.windows_fed} fed to the orchestrator",
        f"- chunked windows: {facts.chunked_windows}",
        f"- tokens: {_usage_line(facts.usage)}",
        f"- cost: {cost}",
        f"- drops: {dropped or 'none'}",
        *_reach_lines(result, facts),
        f"- memory store: {facts.memory_chars} chars, {facts.memory_keys} keys",
        f"- bd prime: {prime} chars",
        "",
    ]


def render_report(result: ValidationResult, run_facts: RunFacts, bead_id: str) -> str:
    """The decision bead's description, which is also the report file's text."""

    respond = PLACEHOLDER if bead_id == NOT_FILED else bead_id
    block = render_fingerprints_block(
        [
            {
                "kind": kept.proposal.kind,
                "key": kept.proposal.key,
                "fingerprint": kept.fingerprint,
                "quotes": [quote for quote, _ in kept.examples],
            }
            for kept in result.proposals
        ],
        [{"title": followup.title} for followup in result.followups],
    )
    lines = [
        *_proposals(result, respond),
        *_followups(result),
        *_helped(result),
        *_facts(result, run_facts),
        *_how_to_respond(result, bead_id, respond),
        block,
        "",
    ]
    return "\n".join(lines)


def example_response(result: ValidationResult) -> str:
    """A `bd human respond` answer built from the ids this report really lists.

    A fixed example once named a P3 the run did not have. With two or more
    proposals the last is shown rejected so both verdict words appear; a
    follow-up is shown as filed. With no proposals the follow-up itself is
    accepted, and with nothing at all the answer names no id, so every form
    here parses through `parse_response` to ids the report carries.
    """

    ids = [f"P{index}" for index in range(1, len(result.proposals) + 1)]
    if len(ids) >= 2:
        text = f"accepted {' '.join(ids[:-1])}; rejected {ids[-1]} (reason)"
    elif ids:
        text = "accepted P1"
    elif result.followups:
        return "accepted F1 (filed)"
    else:
        return "accepted none"
    if result.followups:
        text += "; F1 filed"
    return text


def _how_to_respond(result: ValidationResult, bead_id: str, respond: str) -> list[str]:
    """Steps an agent can follow from this section alone, without `ortus --help`."""

    return [
        "## How to respond",
        "",
        f"Decision bead: {bead_id}",
        "",
        "Steps for you or your agent, in order:",
        "",
        "1. For each proposal you accept, run the `bd remember` (or `bd forget`) "
        "command shown under it in Proposals. Dream never runs them itself.",
        f"2. Record the decision on the bead with `bd human respond {respond} "
        '"<response>"`:',
        "",
        "```bash",
        f'bd human respond {respond} "{example_response(result)}"',
        "```",
        "",
        "   The response is `accepted <ids>` and `rejected <ids> (reason)` groups "
        "separated by `;`. Ids are `P<n>` for proposals and `F<n>` for follow-ups, "
        "as numbered above; an id the response leaves out stays undecided.",
        "3. Dream never files follow-ups. File each one you want as a bead with "
        "`ortus ingest --packet <dir>` (a directory holding description.md, "
        "design.md and acceptance.md) or `ortus ingest --stdin` (one JSON packet).",
        "",
    ]


class _Filer(Protocol):
    def create(self, **kwargs: Any) -> str: ...

    def update_description(
        self, issue_id: str, text: str, *, actor: str | None = None
    ) -> None: ...


def _append_state(repo: Path, entry: dict[str, Any]) -> None:
    path = repo / STATE_FILE
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    history.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Delivery:
    exit_code: int
    bead_id: str | None
    report_path: Path
    error: str = ""


def deliver(
    result: ValidationResult,
    run_facts: RunFacts,
    *,
    repo: Path,
    client: _Filer,
    stamp: str,
    windows: list[str],
    reader_key: str,
    orchestrator_profile: str,
    no_file: bool,
    today: _dt.date | None = None,
) -> Delivery:
    """Write the report, file the decision bead when there is one to file.

    Exit 3 when the result holds nothing to answer; `--no-file` writes the
    report only. A failed create leaves a `not filed` report; a failed id
    substitution leaves the bead with its placeholder and names it.
    """

    report_path = repo / "logs" / f"dream-{stamp}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    empty = not result.proposals and not result.followups
    bead_id: str | None = None
    exit_code = EXIT_NOTHING if empty else EXIT_OK
    error = ""

    if not empty and not no_file:
        draft = scrub(render_report(result, run_facts, PLACEHOLDER), repo)
        try:
            bead_id = client.create(
                title=scrub(title(result, run_facts.windows_fed, today or _dt.date.today()), repo),
                issue_type=DECISION_TYPE,
                priority=2,
                description=draft,
                labels=["human", DREAM_LABEL],
                actor=DREAM_ACTOR,
            )
        except BdError as exc:
            exit_code, error = EXIT_ERROR, f"could not file the decision bead: {exc}"
        if bead_id:
            final = draft.replace(PLACEHOLDER, bead_id)
            try:
                client.update_description(bead_id, final, actor=DREAM_ACTOR)
            except BdError as exc:
                exit_code = EXIT_ERROR
                error = (
                    f"filed {bead_id} but could not replace its {PLACEHOLDER} "
                    f"placeholders: {exc}"
                )
        elif not error:
            exit_code, error = EXIT_ERROR, "bd create returned no issue id"

    report_path.write_text(
        scrub(render_report(result, run_facts, bead_id or NOT_FILED), repo),
        encoding="utf-8",
    )
    _append_state(
        repo,
        {
            "ts": stamp,
            "bead": bead_id,
            "windows": windows,
            "reader_key": reader_key,
            "orchestrator_profile": orchestrator_profile,
        },
    )
    return Delivery(exit_code=exit_code, bead_id=bead_id, report_path=report_path, error=error)


def next_steps(result: ValidationResult, delivery: Delivery) -> str:
    """What a finished run tells the operator to do next, for stderr.

    stdout stays the bead id alone so `id=$(ortus dream ...)` keeps working.
    With no bead filed there is nothing for an agent to walk through, so only
    the report is named.
    """

    report = f"Report: {delivery.report_path}"
    if not delivery.bead_id:
        return f"Dream filed no decision bead.\n{report}"
    return "\n".join(
        [
            f"Dream filed decision {delivery.bead_id}: "
            f"{len(result.proposals)} proposals, {len(result.followups)} follow-ups.",
            report,
            f"Ask your agent: Walk me through Dream decision {delivery.bead_id} "
            "and record what I accept.",
        ]
    )
