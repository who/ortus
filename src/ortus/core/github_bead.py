"""Turn a labeled GitHub issue into a readiness-v1 bead.

The GitHub Action in ``.github/workflows/bead-from-issue.yml`` invokes this
module. Author allowlist, Grok drafting, ``validate_issue``, and ``bd create``
live here so a miswired workflow cannot bypass them. Git commit/push and the
GitHub comment/close are owned by the workflow, which reads the JSON result.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from ortus.core.bd import BdClient
from ortus.core.readiness import READINESS_SCHEMA_VERSION, validate_issue

ALLOWED_AUTHOR = "who"
FILED_PREFIX = "filed as "
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4"

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_GROK_SYSTEM = """You write a readiness schema v1 packet for the Ortus beads tracker.
Reply with a single JSON object and no other prose. Keys:
  title (string),
  issue_type (task|bug|feature|chore),
  priority (0-4 integer, 2 default),
  description (markdown),
  design (markdown),
  acceptance_criteria (markdown).

description MUST contain these ATX headings with real content (not TBD):
  ## Objective
  ## Behavioral context

design MUST contain these ATX headings with real content (not TBD):
  ## Readiness schema
  ## Scope
  ## Non-goals
  ## Concrete locations
  ## Resolved decisions
  ## Compatibility constraints
  ## Ordered steps
  ## Dependencies
  ## Edge cases
  ## Plan-gap guidance

Readiness schema body must be exactly v1.
Concrete locations must name at least one file path and one symbol or interface.
Ordered steps must include a numbered step (1. ...).

acceptance_criteria MUST contain:
  ## Observable criteria
  ## Criterion checks
  ## Targeted tests

Each observable criterion is one line `- AC-N (proves-new|guards-existing): ...`
and Criterion checks must list the same AC-N identifiers, each with exactly one
runnable command in backticks (prefer `uv run pytest tests/<module>.py -q`).
Do not invent downstream project names. Linux + macOS only; do not add Windows paths.
"""


class GrokDraftError(RuntimeError):
    """Grok could not produce a usable packet."""


class BeadStore(Protocol):
    def find_by_external_ref(self, ref: str) -> str | None:
        """Return the existing bead id for ``ref``, or None."""

    def create_packet(self, packet: dict[str, Any], *, external_ref: str) -> str:
        """Create a bead from a validated packet. Return the new id."""


Drafter = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class IngestResult:
    """Machine-readable outcome for the workflow."""

    status: str
    reason: str
    bead_id: str | None = None
    comment: str | None = None
    close_issue: bool = False
    created: bool = False


@dataclass
class MemoryBeadStore:
    """In-memory store used by tests. Production uses :class:`BdBeadStore`."""

    existing: dict[str, str] = field(default_factory=dict)
    created: list[dict[str, Any]] = field(default_factory=list)

    def find_by_external_ref(self, ref: str) -> str | None:
        return self.existing.get(ref)

    def create_packet(self, packet: dict[str, Any], *, external_ref: str) -> str:
        bead_id = f"ortus-mem{len(self.created) + 1}"
        self.created.append({"packet": packet, "external_ref": external_ref, "id": bead_id})
        self.existing[external_ref] = bead_id
        return bead_id


class BdBeadStore:
    """Beads store backed by :class:`BdClient`."""

    def __init__(self, client: BdClient) -> None:
        self.client = client

    def find_by_external_ref(self, ref: str) -> str | None:
        for issue in self.client.list_all():
            if _issue_external_ref(issue) == ref:
                return str(issue.get("id") or "") or None
        return None

    def create_packet(self, packet: dict[str, Any], *, external_ref: str) -> str:
        return self.client.create(
            title=str(packet.get("title") or "untitled"),
            issue_type=str(packet.get("issue_type") or "task"),
            priority=int(packet.get("priority") or 2),
            description=packet.get("description"),
            design=packet.get("design"),
            acceptance=packet.get("acceptance_criteria") or packet.get("acceptance"),
            external_ref=external_ref,
        )


def github_issue(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the issue object from a GitHub event or a bare issue payload."""

    issue = payload.get("issue")
    if isinstance(issue, dict):
        return issue
    return payload


def author_login(issue: dict[str, Any]) -> str:
    user = issue.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "").strip()
    return str(user or "").strip()


def issue_number(issue: dict[str, Any]) -> int:
    return int(issue["number"])


def external_ref_for(number: int) -> str:
    return f"gh-{number}"


def comment_bodies(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        bodies: list[str] = []
        for item in raw:
            if isinstance(item, str):
                bodies.append(item)
            elif isinstance(item, dict):
                bodies.append(str(item.get("body") or item.get("text") or ""))
        return bodies
    return []


def already_filed(comments: list[str]) -> bool:
    return any(FILED_PREFIX in body for body in comments)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, allowing fences or surrounding prose."""

    stripped = text.strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    fence = _FENCE.search(stripped)
    if fence:
        value = json.loads(fence.group(1))
        if isinstance(value, dict):
            return value
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(stripped[start : end + 1])
        if isinstance(value, dict):
            return value
    raise GrokDraftError("Grok output did not contain a JSON object")


def _join_sections(pairs: list[tuple[str, Any]]) -> str:
    parts: list[str] = []
    for heading, body in pairs:
        if body is None:
            continue
        text = str(body).strip()
        if not text:
            continue
        parts.append(f"## {heading}\n{text}")
    return "\n\n".join(parts)


def assemble_issue(
    draft: dict[str, Any], *, title_fallback: str, draft_id: str
) -> dict[str, Any]:
    """Map a Grok draft onto the fields ``validate_issue`` reads."""

    description = draft.get("description")
    if not description:
        description = _join_sections(
            [
                ("Objective", draft.get("objective")),
                ("Behavioral context", draft.get("behavioral_context")),
            ]
        )
    design = draft.get("design")
    if not design:
        design = _join_sections(
            [
                ("Readiness schema", draft.get("readiness_schema") or READINESS_SCHEMA_VERSION),
                ("Scope", draft.get("scope")),
                ("Non-goals", draft.get("non_goals") or draft.get("nongoals")),
                ("Concrete locations", draft.get("concrete_locations")),
                ("Resolved decisions", draft.get("resolved_decisions")),
                ("Compatibility constraints", draft.get("compatibility_constraints")),
                ("Ordered steps", draft.get("ordered_steps")),
                ("Dependencies", draft.get("dependencies")),
                ("Edge cases", draft.get("edge_cases")),
                ("Plan-gap guidance", draft.get("plan_gap_guidance")),
            ]
        )
    acceptance = draft.get("acceptance_criteria") or draft.get("acceptance")
    if not acceptance:
        acceptance = _join_sections(
            [
                ("Observable criteria", draft.get("observable_criteria")),
                ("Criterion checks", draft.get("criterion_checks")),
                ("Targeted tests", draft.get("targeted_tests")),
            ]
        )
    try:
        priority = int(draft.get("priority") or 2)
    except (TypeError, ValueError):
        priority = 2
    return {
        "id": draft_id,
        "title": str(draft.get("title") or title_fallback).strip() or title_fallback,
        "issue_type": str(draft.get("issue_type") or draft.get("type") or "task"),
        "priority": priority,
        "description": description or "",
        "design": design or "",
        "acceptance_criteria": acceptance or "",
    }


def grok_user_prompt(*, title: str, body: str, number: int) -> str:
    return (
        f"GitHub issue #{number}\n"
        f"Title: {title}\n\n"
        f"Body:\n{body or '(empty)'}\n"
    )


def draft_packet_via_grok(
    *,
    title: str,
    body: str,
    number: int,
    api_key: str | None = None,
    model: str | None = None,
    url: str = XAI_CHAT_URL,
) -> dict[str, Any]:
    """Call xAI chat completions and parse the JSON packet."""

    key = (os.environ.get("XAI_API_KEY", "") if api_key is None else api_key).strip()
    if not key:
        raise GrokDraftError("XAI_API_KEY is not set")
    payload = {
        "model": model or os.environ.get("XAI_MODEL", DEFAULT_MODEL),
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _GROK_SYSTEM},
            {
                "role": "user",
                "content": grok_user_prompt(title=title, body=body, number=number),
            },
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GrokDraftError(f"Grok HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise GrokDraftError(f"Grok request failed: {exc}") from exc
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GrokDraftError("Grok response missing choices[0].message.content") from exc
    return parse_json_object(str(text))


def ingest_github_issue(
    payload: dict[str, Any],
    *,
    comments: list[str] | None = None,
    store: BeadStore | None = None,
    drafter: Drafter | None = None,
    repo: Path | None = None,
) -> IngestResult:
    """Author-gate, draft, validate, and create. Never creates on failure."""

    issue = github_issue(payload)
    if author_login(issue) != ALLOWED_AUTHOR:
        return IngestResult(
            status="skipped_author",
            reason="author is not who",
        )
    try:
        number = issue_number(issue)
    except (KeyError, TypeError, ValueError):
        return IngestResult(status="validate_failed", reason="issue payload has no number")
    ref = external_ref_for(number)
    bodies = comments if comments is not None else comment_bodies(payload.get("comments"))
    if already_filed(bodies):
        return IngestResult(
            status="skipped_idempotent",
            reason="existing filed-as comment",
        )
    if store is None:
        store = BdBeadStore(BdClient(repo or Path.cwd()))
    existing = store.find_by_external_ref(ref)
    if existing:
        return IngestResult(
            status="skipped_idempotent",
            reason=f"{ref} already exists as {existing}",
            bead_id=existing,
        )
    title = str(issue.get("title") or "").strip()
    body = str(issue.get("body") or "")
    if drafter is None:
        drafter = draft_packet_via_grok
    try:
        draft = drafter(title=title, body=body, number=number)
    except GrokDraftError as exc:
        diagnostic = str(exc)
        return IngestResult(
            status="validate_failed",
            reason=diagnostic,
            comment=f"PLAN-GAP: could not draft readiness packet: {diagnostic}",
        )
    packet = assemble_issue(draft, title_fallback=title, draft_id=f"{ref}-draft")
    report = validate_issue(packet)
    if not report.ready:
        return IngestResult(
            status="validate_failed",
            reason=report.diagnostic(),
            comment=f"PLAN-GAP: {report.diagnostic()}",
        )
    bead_id = store.create_packet(packet, external_ref=ref)
    return IngestResult(
        status="created",
        reason="created",
        bead_id=bead_id,
        comment=f"{FILED_PREFIX}{bead_id}",
        close_issue=True,
        created=True,
    )


def _issue_external_ref(issue: dict[str, Any]) -> str:
    for key in ("external_ref", "external-ref", "externalRef"):
        value = issue.get(key)
        if value:
            return str(value)
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ortus.core.github_bead")
    parser.add_argument("--event", required=True, help="GitHub event JSON path")
    parser.add_argument(
        "--comments",
        help="JSON file of issue comments (array of strings or {body} objects)",
    )
    parser.add_argument("--repo", default=".", help="Repository root for bd")
    args = parser.parse_args(argv)
    payload = json.loads(Path(args.event).read_text(encoding="utf-8"))
    comments = None
    if args.comments:
        comments = comment_bodies(json.loads(Path(args.comments).read_text(encoding="utf-8")))
    result = ingest_github_issue(payload, comments=comments, repo=Path(args.repo))
    sys.stdout.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
