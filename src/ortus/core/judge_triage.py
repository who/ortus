"""Route a parked bead instead of leaving every park to an operator.

Three different things reach the human queue by the same door. A declared
version the installed toolchain cannot honour is a pin the run still owes; a
work spec whose sections are missing or malformed is a planning job nobody
needs a human for; a credential nobody supplied and a product decision nobody
has made are the parks that genuinely belong to an operator. Before this
module every one of them stopped the queue until someone read the comment.

The reading happens in two passes, cheapest first. `classify_plan_gap` is the
conservative regex pass and still runs before anything is asked of a model: a
comment it recognises is pin-able skew, and no request is made. What it does
not recognise gets one bounded Jev choice request whose answer becomes a
probability vector, and :func:`route_triage` — pure System Two — takes the
argmax. There is no confidence floor anywhere in the path: a hedged answer is
still an answer about the park, and the human label is what the needs-human
class winning *means*, never what low confidence produces.

The deterministic readiness rule is untouched by all of this. The planner-fix
route may propose a repaired packet, but only `validate_issue` decides whether
it re-enters the queue, and one attempt per bead is all a park ever buys.
Every route fails toward today's behaviour: a tracker error, a missing
re-spec runner, a Jev timeout and a disabled seat all leave the bead exactly
as parked as it was, which is why a repository with Jev off sees no change.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from ortus.core.judge import JudgeConfig, JudgeMode, JudgePhase
from ortus.core.judge_log import _append, _clean_metadata, _common
from ortus.core.judge_state import StateError, pack_state, sanitize_field
from ortus.core.judge_typesafe import (
    API_KEY_ENV, JudgeFailure, _answer, _default_client, _Invalid, _mapping,
)
from ortus.core.pin_skew import PIN_SKEW_LABEL, classify_plan_gap
from ortus.core.readiness import validate_issue
from ortus.core.user_env import judge_environment

#: The label every park carries today, and the one the two automatic routes
#: remove. Spelled once here so a route and its log line cannot drift.
HUMAN_LABEL = "human"

#: Marker comment recording that this bead has already had its one automatic
#: re-spec. Grind windows are separate processes, so a bd comment is the only
#: cross-process store; the prefix must stay stable across versions because
#: older markers are re-read by newer grinds.
RESPEC_MARKER = "ortus-grind: triage re-spec attempted"

#: Heading of the section the work prompt carries for a bead the triage
#: released as pin-able skew.
PIN_DIRECTIVE_HEADER = "\n\n## Pin directive\n"

_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE
)


class TriageClass(str, Enum):
    PIN_SKEW = "pin_skew"
    PLANNER_FIX = "planner_fix"
    NEEDS_HUMAN = "needs_human"


#: Argmax order, conservative first. Ties resolve to the earliest entry, so a
#: flat vector keeps the bead where it already is: no evidence for unparking
#: is not evidence for unparking.
_CLASSES: tuple[TriageClass, ...] = (
    TriageClass.NEEDS_HUMAN, TriageClass.PIN_SKEW, TriageClass.PLANNER_FIX,
)


class _Tracker(Protocol):
    """The tracker surface the routes use, so tests can supply a double."""

    def show(self, issue_id: str) -> dict[str, Any]: ...

    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...

    def add_label(self, issue_id: str, label: str) -> None: ...

    def remove_label(self, issue_id: str, label: str) -> None: ...

    def add_comment(self, issue_id: str, body: str) -> None: ...


@dataclass(frozen=True)
class TriageVector:
    """What the park reads as, and what produced that reading.

    `regex_hit` records that the conservative first pass answered and no
    request was made; `fail_open` records that nothing answered at all and the
    vector is the baseline park rather than a judgment.
    """

    p_pin_skew: float = 0.0
    p_planner_fix: float = 0.0
    p_needs_human: float = 1.0
    fail_open: bool = False
    regex_hit: bool = False
    failure: JudgeFailure | None = None

    def vector(self) -> dict[str, float]:
        """The probability vector keyed by class value, log-ready."""
        return {
            TriageClass.PIN_SKEW.value: self.p_pin_skew,
            TriageClass.PLANNER_FIX.value: self.p_planner_fix,
            TriageClass.NEEDS_HUMAN.value: self.p_needs_human,
        }


def _certain(triage_class: TriageClass, **kwargs: Any) -> TriageVector:
    """The vector that puts all of its mass on one class."""
    weights = {entry: 1.0 if entry is triage_class else 0.0 for entry in _CLASSES}
    return TriageVector(
        p_pin_skew=weights[TriageClass.PIN_SKEW],
        p_planner_fix=weights[TriageClass.PLANNER_FIX],
        p_needs_human=weights[TriageClass.NEEDS_HUMAN],
        **kwargs,
    )


def route_triage(vector: TriageVector) -> TriageClass:
    """The class the vector argues for: argmax, no confidence floor.

    System Two in full. The vector is advice — the routing rule lives here,
    in code an operator can read without replaying a request.
    """
    weights = vector.vector()
    return max(
        _CLASSES,
        key=lambda entry: (weights[entry.value], -_CLASSES.index(entry)),
    )


def build_triage_questions() -> dict:
    """The single choice question a parked bead is classified by."""
    return {"triage": {
        "type": "choice",
        "instructions": (
            "Classify why this issue is parked. Do not grant that the work is "
            "ready or that it should be worked next."
        ),
        "criteria": {
            TriageClass.PIN_SKEW.value: {"description": (
                "A declared version, date, or toolchain value exceeds what the "
                "installed tool supports; lowering the declared value and "
                "filing a restore issue resolves it."
            )},
            TriageClass.PLANNER_FIX.value: {"description": (
                "The work specification itself is incomplete or malformed, and "
                "re-writing its sections resolves it without any decision only "
                "an operator can make."
            )},
            TriageClass.NEEDS_HUMAN.value: {"description": (
                "An operator must supply a credential, an access grant, or a "
                "product decision before any worker can proceed."
            )},
        },
    }}


def _clean_evidence(
    text: str, config: JudgeConfig, environ: Mapping[str, str] | None,
) -> str:
    """Bound and screen the park's own words before they leave the process.

    Same screening `pack_state` applies to packet fields, at the acceptance
    cap rather than the metadata cap: a PLAN-GAP comment is prose, and a
    160-character slice of one classifies nothing.
    """
    env = os.environ if environ is None else environ
    secrets = tuple(v for k, v in env.items() if v and _SECRET_NAME.search(k))
    try:
        return sanitize_field(
            text, cap=config.acceptance_cap, secret_values=secrets,
            sensitive_paths=config.sensitive_paths,
        ).value or ""
    except StateError:
        return ""


def triage_parked(
    issue: Mapping[str, object], evidence: str, config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> TriageVector:
    """Send one bounded Choice request with sanitized state and the park's evidence.

    Every failure — no key, no SDK, a timeout, a malformed answer — returns
    the fail-open vector, which argues for the human queue the bead is
    already in. The caller never has to distinguish "the judge said park it"
    from "nothing answered": both keep today's behaviour, and the record says
    which one happened.
    """
    environ = judge_environment(environ)
    facts = {"park": {"evidence": _clean_evidence(evidence, config, environ)}}
    overhead = len(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode())
    budget = config.total_bytes_cap - overhead
    if budget <= 0:
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.INVALID_ANSWER)
    try:
        packed = pack_state(issue, replace(config, total_bytes_cap=budget),
                            phase=JudgePhase.POST_TURN, environ=environ)
    except StateError:
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.INVALID_ANSWER)
    payload = {**packed.to_payload(), **facts}
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > config.total_bytes_cap:
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.INVALID_ANSWER)
    env = os.environ if environ is None else environ
    if not env.get(API_KEY_ENV, "").strip():
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.KEY_MISSING)

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(payload, build_triage_questions(),
                                           model=config.model,
                                           timeout=config.timeout_seconds)

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.SDK_MISSING)
    except asyncio.TimeoutError:
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.TIMEOUT)
    except Exception:  # noqa: BLE001 - provider prose never escapes
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.SERVICE_ERROR)
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        answers = _mapping(body.get("answers"))
        if set(answers) != {"triage"}:
            raise _Invalid
        answer = _answer(answers, "triage", "choice")
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise _Invalid
        chosen = TriageClass(choice)
        confidence = answer.get("confidence")
        if type(confidence) not in (int, float) or not 0.0 <= float(confidence) <= 1.0:
            raise _Invalid
    except Exception:  # noqa: BLE001 - a malformed answer is a park, not a route
        return _certain(TriageClass.NEEDS_HUMAN, fail_open=True,
                        failure=JudgeFailure.INVALID_ANSWER)
    # The answer's confidence lands on the class it argues for and the rest
    # spreads uniformly, so a zero-confidence answer is the uniform vector —
    # which the tie rule reads as the park it already was.
    spread = (1.0 - float(confidence)) / len(_CLASSES)
    weights = {
        entry: spread + (float(confidence) if entry is chosen else 0.0)
        for entry in _CLASSES
    }
    return TriageVector(
        p_pin_skew=weights[TriageClass.PIN_SKEW],
        p_planner_fix=weights[TriageClass.PLANNER_FIX],
        p_needs_human=weights[TriageClass.NEEDS_HUMAN],
    )


def pin_directive_section(evidence: str) -> str:
    """The prompt section a released pin-skew bead carries into its window."""
    detail = f" The park named: {evidence}." if evidence else ""
    return (
        f"{PIN_DIRECTIVE_HEADER}"
        "This issue was parked on a declared version, date, or toolchain "
        "value the installed tool cannot honour, and the harness released it "
        "as a pin rather than a planning gap." + detail + " Lower the declared "
        "value to what the installed tool supports, file a follow-up bead "
        "recording the value you lowered and the condition for restoring it, "
        "and carry on with the claim. Do not park this issue again for the "
        "same mismatch."
    )


def pin_release_note(issue_id: str, evidence: tuple[str, ...] | None = None) -> str:
    """The comment that records the pin-skew route and the release it made."""
    detail = f" (evidence: {', '.join(evidence)})" if evidence else ""
    return (
        f"triage: {issue_id} reads as an environment or version skew a pin "
        f"resolves{detail}, not as a gap an operator has to answer. The "
        f"{HUMAN_LABEL} label is removed and the {PIN_SKEW_LABEL} label "
        "records the reading; the next worker is told to lower the declared "
        "value to what the installed tool supports and to file a follow-up "
        "bead for restoring it."
    )


def respec_release_note(issue_id: str) -> str:
    """The comment that records an automatic re-spec the schema accepted."""
    return (
        f"triage: {issue_id} was re-specified by one bounded planner turn and "
        "the repaired packet passes readiness schema v1, so the "
        f"{HUMAN_LABEL} label is removed and the issue returns to the queue. "
        "The deterministic check is what released it; a bead gets one "
        "automatic re-spec and no more."
    )


def respec_failed_note(diagnostic: str) -> str:
    """The comment that records a re-spec the schema still rejects."""
    return (
        "triage: one bounded planner turn re-specified this issue and the "
        "repaired packet still fails readiness schema v1, so it stays the "
        f"operator's. No second automatic attempt will be made.\n\n{diagnostic}"
    )


def needs_human_note(vector: TriageVector) -> str:
    """The comment that records the park, with the vector as advice."""
    reading = ", ".join(
        f"{name}={value:.3f}" for name, value in vector.vector().items()
    )
    how = (
        "no triage answer was available, so this is the baseline park"
        if vector.fail_open else
        "the triage read it as work only an operator can unblock"
    )
    return (
        f"triage: this issue stays labeled {HUMAN_LABEL} — {how} ({reading}). "
        "The probabilities are advice for whoever picks it up, not a gate: "
        "the label came from the needs-human class winning, never from a "
        "confidence threshold."
    )


def log_triage(
    repo: Path, config: JudgeConfig, run_id: UUID, issue_id: str,
    vector: TriageVector, decided: TriageClass, applied: TriageClass,
) -> None:
    """Append one triage record beside the phase records.

    `applied` is the route that actually ran, which differs from the decided
    class in shadow mode and whenever a route could not complete and fell back
    to the human queue.
    """
    payload = _common("triage", run_id, uuid4())
    payload.update({
        "phase": "triage", "mode": config.mode.value,
        "issue_id": _clean_metadata(issue_id, config),
        "seat": _clean_metadata(config.seat, config), "model": config.model,
        "vector": vector.vector(),
        "regex_hit": vector.regex_hit,
        "fail_open": vector.fail_open,
        # Named apart from the gate decision's `failure`: a triage that fails
        # open is not a gate that fell back, and the replay summary counts the
        # two separately rather than reading one as the other.
        "triage_failure": vector.failure.value if vector.failure is not None else None,
        "triage_class": decided.value,
        "effective_action": applied.value,
    })
    _append(repo, payload)


def _labels(issue: Mapping[str, Any]) -> set[str]:
    return {str(label) for label in (issue.get("labels") or ())}


def _release(bd: _Tracker, issue_id: str, issue: Mapping[str, Any], note: str) -> None:
    """Comment first, then drop the label. Order is the safety property.

    A tracker that dies between the two leaves a parked bead carrying an
    explanation, which is recoverable. The other order leaves an unparked
    bead with nothing on it saying why.
    """
    bd.add_comment(issue_id, note)
    if HUMAN_LABEL in _labels(issue):
        bd.remove_label(issue_id, HUMAN_LABEL)


def _route_pin_skew(
    bd: _Tracker, issue_id: str, issue: Mapping[str, Any],
    evidence: tuple[str, ...], write_log: Callable[[str], None],
) -> TriageClass:
    """Tag the reading and release the bead for the pin its next window owes."""
    try:
        if PIN_SKEW_LABEL not in _labels(issue):
            bd.add_label(issue_id, PIN_SKEW_LABEL)
        _release(bd, issue_id, issue, pin_release_note(issue_id, evidence))
    except Exception as exc:  # noqa: BLE001 - a tracker hiccup never ends the run
        write_log(f"triage: could not release {issue_id} as pin-skew ({exc})")
        return TriageClass.NEEDS_HUMAN
    write_log(f"triage: released {issue_id} as pin-able skew; {HUMAN_LABEL} label removed")
    return TriageClass.PIN_SKEW


def _respec_spent(bd: _Tracker, issue_id: str) -> bool:
    """Whether this bead has already had its one automatic re-spec.

    An unreadable comment list reads as spent: a bead whose history cannot be
    checked must not buy an unbounded number of planner turns.
    """
    try:
        existing = bd.comments(issue_id)
    except Exception:  # noqa: BLE001
        return True
    return any(
        RESPEC_MARKER in str(comment.get("text") or "") for comment in existing
    )


def _route_planner_fix(
    bd: _Tracker, issue_id: str, issue: Mapping[str, Any],
    respec: Callable[[str], bool] | None, write_log: Callable[[str], None],
) -> TriageClass:
    """Re-spec the packet once, then let the schema decide whether it re-queues.

    Returns the route that actually ran: a missing runner, a spent attempt, a
    turn that failed, and a repaired packet the validator still rejects all
    fall back to the human queue, where the caller's needs-human route
    records what happened.
    """
    if respec is None:
        write_log(
            f"triage: {issue_id} reads as a planner fix, but this run has no "
            "re-spec runner; it stays parked"
        )
        return TriageClass.NEEDS_HUMAN
    if _respec_spent(bd, issue_id):
        write_log(
            f"triage: {issue_id} has already spent its one automatic re-spec; "
            "it stays parked"
        )
        return TriageClass.NEEDS_HUMAN
    try:
        # The marker is written before the turn, not after it: a crash
        # mid-re-spec must not buy the bead a second one.
        bd.add_comment(issue_id, RESPEC_MARKER)
    except Exception as exc:  # noqa: BLE001
        write_log(f"triage: could not record the re-spec marker on {issue_id} ({exc})")
        return TriageClass.NEEDS_HUMAN
    try:
        ran = bool(respec(issue_id))
    except Exception as exc:  # noqa: BLE001 - a failed planner turn is a park
        write_log(f"triage: the re-spec turn for {issue_id} failed ({exc})")
        ran = False
    if not ran:
        write_log(f"triage: the re-spec turn for {issue_id} produced no repair")
        return TriageClass.NEEDS_HUMAN
    try:
        repaired = bd.show(issue_id)
    except Exception as exc:  # noqa: BLE001
        write_log(f"triage: could not re-read {issue_id} after its re-spec ({exc})")
        return TriageClass.NEEDS_HUMAN
    report = validate_issue(repaired)
    if not report.ready:
        try:
            bd.add_comment(issue_id, respec_failed_note(report.diagnostic()))
        except Exception as exc:  # noqa: BLE001
            write_log(f"triage: could not comment on {issue_id} ({exc})")
        write_log(
            f"triage: the re-spec of {issue_id} still fails readiness schema "
            "v1; it stays parked"
        )
        return TriageClass.NEEDS_HUMAN
    try:
        _release(bd, issue_id, repaired, respec_release_note(issue_id))
    except Exception as exc:  # noqa: BLE001
        write_log(f"triage: could not release {issue_id} after its re-spec ({exc})")
        return TriageClass.NEEDS_HUMAN
    write_log(
        f"triage: {issue_id} passes readiness schema v1 after one re-spec; "
        f"{HUMAN_LABEL} label removed"
    )
    return TriageClass.PLANNER_FIX


def _route_needs_human(
    bd: _Tracker, issue_id: str, issue: Mapping[str, Any], vector: TriageVector,
    write_log: Callable[[str], None],
) -> None:
    """Today's park, plus the probabilities as advice for whoever reads it."""
    try:
        if HUMAN_LABEL not in _labels(issue):
            bd.add_label(issue_id, HUMAN_LABEL)
        bd.add_comment(issue_id, needs_human_note(vector))
    except Exception as exc:  # noqa: BLE001
        write_log(f"triage: could not record the park on {issue_id} ({exc})")


def triage_parked_bead(
    bd: _Tracker,
    issue_id: str,
    *,
    evidence: str,
    repo: Path,
    config: JudgeConfig,
    run_id: UUID,
    write_log: Callable[[str], None],
    respec: Callable[[str], bool] | None = None,
    evaluate: Callable[..., TriageVector] = triage_parked,
) -> TriageClass | None:
    """Classify one parked bead and apply the route its vector argues for.

    `evidence` is the park's own words — the readiness diagnostic for a leaf
    the schema rejected, the latest PLAN-GAP comment for one a worker or the
    post-turn judge parked. Returns the route that ran, or None for a bead
    that is not triaged at all (an epic, a bead closed since the park, a bead
    the tracker cannot read).

    Shadow mode reads the class and applies the park, so a seat can watch the
    routes it would have taken for a whole run before any of them moves a
    bead. A disabled seat never calls the model and takes the same path.
    """
    try:
        issue = bd.show(issue_id)
    except Exception as exc:  # noqa: BLE001
        write_log(f"triage: could not read {issue_id} ({exc}); it stays as it is")
        return None
    if str(issue.get("status") or "") == "closed":
        return None
    if str(issue.get("issue_type") or issue.get("type") or "") == "epic":
        return None

    regex = classify_plan_gap(evidence)
    if regex.pin_able:
        vector = _certain(TriageClass.PIN_SKEW, regex_hit=True)
    elif not config.enabled:
        vector = _certain(TriageClass.NEEDS_HUMAN, fail_open=True)
    else:
        vector = evaluate(issue, evidence, config)
    decided = route_triage(vector)
    applied = (
        TriageClass.NEEDS_HUMAN if config.mode == JudgeMode.SHADOW else decided
    )

    if applied is TriageClass.PIN_SKEW:
        applied = _route_pin_skew(bd, issue_id, issue, regex.evidence, write_log)
    elif applied is TriageClass.PLANNER_FIX:
        applied = _route_planner_fix(bd, issue_id, issue, respec, write_log)
    if applied is TriageClass.NEEDS_HUMAN:
        _route_needs_human(bd, issue_id, issue, vector, write_log)

    try:
        log_triage(repo, config, run_id, issue_id, vector, decided, applied)
    except Exception as exc:  # noqa: BLE001 - the route stands without its record
        write_log(
            f"triage: could not log the decision for {issue_id} ({exc}); the "
            "route still stands"
        )
    write_log(
        f"triage for {issue_id}: {applied.value} (decided={decided.value}, "
        f"regex_hit={vector.regex_hit}, fail_open={vector.fail_open}, "
        + ", ".join(f"{name}={value:.3f}" for name, value in vector.vector().items())
        + ")"
    )
    return applied
