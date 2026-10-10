"""Advisory outcome judgments. Tracker closure and mechanical failures win."""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ortus.core import output
from ortus.core.user_env import judge_environment

from ortus.core.judge import FailureMode, JudgeConfig, JudgeMode, JudgePhase
from ortus.core.judge_log import (
    OutcomeStatus, _append, _clean_metadata, _common, narrow_fields,
    question_version, request_fields, side_fields,
)
from ortus.core.judge_questions import (
    HAS_BASIS, basis_question, has_basis, noul_question,
)
from ortus.core.judge_state import StateError, pack_state
from ortus.core.worker_failure import WorkerFailure
from ortus.core.judge_typesafe import (
    API_KEY_ENV, SPLIT_OVERHEAD, JudgeFailure, JudgeUsage, _default_client,
    _Invalid, _mapping, _narrow, _number, _usage, packed_split,
)


class Outcome(str, Enum):
    DONE = "done"
    PLAN_GAP = "plan_gap"
    FLAKE = "flake"
    AUTH = "auth"
    NEEDS_HUMAN = "needs_human"
    CONTINUE = "continue"


#: What the last test or check command of a window read; see
#: `judge_progress.last_check_result`.
CHECK_RESULTS = frozenset({"pass", "fail", "none-run"})


@dataclass(frozen=True)
class WorkerOutcome:
    exit_status: int
    watchdog: bool
    observed_status: OutcomeStatus
    branch_advanced: bool
    #: How the window died, as the taxonomy already named it, or None when it
    #: did not. It travels beside the outcome reason rather than inside it:
    #: the reason is what the judge decided about the work, the class is what
    #: killed the worker, and a replay that has to tell a sandbox refusal from
    #: a low-confidence verdict needs both.
    worker_failure: WorkerFailure | None = None
    #: The window's last test or check result, when the caller read the log.
    last_check: str | None = None

    def __post_init__(self) -> None:
        if (type(self.exit_status) is not int
                or type(self.watchdog) is not bool
                or type(self.branch_advanced) is not bool
                or not isinstance(self.observed_status, OutcomeStatus)
                or not (self.worker_failure is None
                        or isinstance(self.worker_failure, WorkerFailure))
                or not (self.last_check is None or self.last_check in CHECK_RESULTS)):
            raise ValueError("invalid worker outcome")

    def payload(self) -> dict:
        """The typed facts a post_turn record carries in its own fields."""
        return {"exit_status": self.exit_status, "watchdog": self.watchdog,
                "observed_status": self.observed_status.value,
                "branch_advanced": self.branch_advanced}

    def facts(self) -> dict:
        """The trusted facts the questions are asked over.

        The failure class travels as a fact: a narrow transient-failure
        question is answered better knowing the window died of a sandbox
        refusal than guessing it from the tracker status.
        """
        return {**self.payload(),
                "worker_failure": (self.worker_failure.value
                                   if self.worker_failure is not None else None),
                "last_check": self.last_check}


@dataclass(frozen=True)
class OutcomeVerdict:
    outcome: Outcome | None = None
    confidence: float | None = None
    failure: JudgeFailure | None = None
    #: Request cost, for the log only. Left out of equality so two verdicts
    #: that say the same thing compare equal however long each one took.
    latency_ms: float | None = field(default=None, compare=False)
    usage: JudgeUsage | None = field(default=None, compare=False)
    #: The narrow answers the outcome was derived from, keyed by question name.
    narrow: Mapping[str, float] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.failure is not None:
            if (not isinstance(self.failure, JudgeFailure)
                    or self.outcome is not None or self.confidence is not None):
                raise ValueError("invalid outcome failure")
        elif not isinstance(self.outcome, Outcome):
            raise ValueError("invalid outcome choice")
        else:
            try:
                _number(self.confidence, 1)
            except _Invalid:
                raise ValueError("invalid outcome confidence") from None


#: The post_turn question set's name in a record's `question_version`.
OUTCOME_QUESTIONS = "post-v2"
#: What the post_turn site does with a live window outside shadow.
POST_ACTIONS = frozenset({"human", "preserve"})

#: The narrow questions, one worker window each.
WORK_COMPLETE = "work_complete"
TRANSIENT_FAILURE = "transient_failure"
NEEDS_OPERATOR = "needs_operator_credential_or_decision"
SPEC_GAP = "spec_gap"

#: Legacy outcome precedence, most consequential first. The first question
#: answered yes names the outcome; none answered yes is `continue`.
_PRECEDENCE: tuple[tuple[str, Outcome], ...] = (
    (NEEDS_OPERATOR, Outcome.NEEDS_HUMAN),
    (SPEC_GAP, Outcome.PLAN_GAP),
    (TRANSIENT_FAILURE, Outcome.FLAKE),
    (WORK_COMPLETE, Outcome.DONE),
)


def build_outcome_questions() -> dict:
    """Four narrow yes/nos about one ended worker window, plus their basis.

    Each replaces one branch of the old six-way pick, so a window can read as
    neither complete nor failed without being forced into a class.
    """
    return {
        WORK_COMPLETE: noul_question(
            "Do the facts show this issue's work complete?",
            ["The tracker status is closed, or the branch advanced and the last "
             "check passed with nothing left unfinished in the issue text."],
            ["The tracker status is open or in progress and the facts show "
             "unfinished work or a failing check."],
        ),
        TRANSIENT_FAILURE: noul_question(
            "Did a transient failure, not the work itself, end this window?",
            ["The worker failure class or exit status names a timeout, a "
             "provider error, or a killed process."],
            ["The window ended normally, or the failure would recur on a retry."],
        ),
        NEEDS_OPERATOR: noul_question(
            "Does the next step need an operator to supply a credential, an "
            "access grant, or a product decision?",
            ["The worker failure class names authentication, or the issue text "
             "asks for a decision no worker is allowed to make."],
            ["Another worker window could proceed with what it already has."],
        ),
        SPEC_GAP: noul_question(
            "Is the work specification itself missing or contradicting what "
            "the worker needs?",
            ["A required section is absent, or two sections contradict each other."],
            ["The specification is complete enough to keep working from."],
        ),
        HAS_BASIS: basis_question("these answers about this worker window"),
    }


def derive_outcome(narrow: Mapping[str, float]) -> Outcome:
    """The legacy outcome the narrow answers argue for, by fixed precedence."""
    return next((outcome for name, outcome in _PRECEDENCE if narrow[name] >= 0.5),
                Outcome.CONTINUE)


def _verdict(narrow: Mapping[str, float]) -> OutcomeVerdict:
    """The verdict one answer set reads as: its derived outcome at its basis."""
    return OutcomeVerdict(derive_outcome(narrow), float(narrow[HAS_BASIS]),
                          narrow=dict(narrow))


def _no_basis(verdict: OutcomeVerdict) -> bool:
    """Whether an answered verdict claims no real basis for its outcome."""
    return verdict.narrow is not None and not has_basis(verdict.narrow)


def evaluate_outcome(
    issue: Mapping[str, object], observation: WorkerOutcome, config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> OutcomeVerdict:
    """Send the narrow questions with typed facts trusted and issue text not."""
    environ = judge_environment(environ)
    # Reserve space for the facts and the block wrapper as well as the issue.
    facts = {"worker": observation.facts()}
    overhead = len(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode())
    budget = config.total_bytes_cap - overhead - SPLIT_OVERHEAD
    if budget <= 0:
        raise StateError("judge outcome state budget is too small")
    packed = pack_state(issue, replace(config, total_bytes_cap=budget),
                        phase=JudgePhase.POST_TURN, environ=environ)
    payload = packed_split(packed.to_payload(), facts, {})
    questions = build_outcome_questions()
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > config.total_bytes_cap:
        raise StateError("judge outcome state budget is too small")
    env = os.environ if environ is None else environ
    if not env.get(API_KEY_ENV, "").strip():
        return OutcomeVerdict(failure=JudgeFailure.KEY_MISSING)
    started = time.monotonic()

    def timed(verdict: OutcomeVerdict, usage: JudgeUsage | None = None) -> OutcomeVerdict:
        return replace(verdict, usage=usage,
                       latency_ms=max((time.monotonic() - started) * 1000.0, 0.0))

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(payload, questions,
                                           model=config.model, timeout=config.timeout_seconds)

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return timed(OutcomeVerdict(failure=JudgeFailure.SDK_MISSING))
    except asyncio.TimeoutError:
        return timed(OutcomeVerdict(failure=JudgeFailure.TIMEOUT))
    except Exception:  # noqa: BLE001 - provider prose never escapes
        return timed(OutcomeVerdict(failure=JudgeFailure.SERVICE_ERROR))
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        narrow = _narrow(_mapping(body.get("answers")), questions)
        return timed(_verdict(narrow), _usage(body.get("usage")))
    except Exception:  # noqa: BLE001 - malformed response is a typed failure
        return timed(OutcomeVerdict(failure=JudgeFailure.INVALID_ANSWER))


def apply_outcome(
    bd: Any, repo: Path, issue_id: str, observation: WorkerOutcome,
    verdict: OutcomeVerdict, config: JudgeConfig, run_id: UUID,
) -> bool:
    """Log before annotating; return whether to end this worker window.

    Never close, release, reset a counter, or modify candidate files. Re-read
    the tracker after evaluation so a concurrent close cannot be reopened.
    """
    current = bd.show(issue_id)
    status = OutcomeStatus(current.get("status", "unknown"))
    human = False
    reason = "classified"
    intended = verdict.outcome
    if status == OutcomeStatus.CLOSED or observation.observed_status == OutcomeStatus.CLOSED:
        intended = Outcome.DONE
        reason = "tracker_closed"
    elif observation.watchdog:
        reason = "worker_timeout"
    elif verdict.failure is not None:
        human = config.failure_mode == FailureMode.CLOSED
        reason = "service_failure"
    elif _no_basis(verdict):
        # An answer that claims no basis falls to the code baseline: keep the
        # tracker's result and let the stuck rule read the near-uniform vector.
        reason = "no_basis"
    # No confidence floor. An unsure answer is still an answer about the work,
    # and parking a bead on it spent an operator every time the model hedged.
    # The confidence now travels to `decide_stuck_claim`, where it shrinks the
    # continue/replan/escalate vector toward uniform instead of picking a
    # branch on its own.
    elif verdict.outcome == Outcome.DONE:
        reason = "closure_disagreement"
    elif verdict.outcome in (Outcome.PLAN_GAP, Outcome.AUTH, Outcome.NEEDS_HUMAN):
        human = True
        reason = verdict.outcome.value
    shadow = config.mode == JudgeMode.SHADOW
    payload = _common("post_turn", run_id, uuid4())
    payload.update({
        "phase": "post_turn", "mode": config.mode.value,
        "issue_id": _clean_metadata(issue_id, config),
        "seat": _clean_metadata(config.seat, config), "model": config.model,
        **observation.payload(), "observed_status": status.value,
        "outcome": verdict.outcome.value if verdict.outcome is not None else None,
        "confidence": verdict.confidence,
        "failure": verdict.failure.value if verdict.failure is not None else None,
        "intended_outcome": intended.value if intended is not None else None,
        "effective_action": "baseline" if shadow else "human" if human else "preserve",
        "reason": reason,
        "disagreement": verdict.outcome == Outcome.DONE and status != OutcomeStatus.CLOSED,
        "worker_failure": (observation.worker_failure.value
                           if observation.worker_failure is not None else None),
        # The baseline keeps the tracker's result. Jev's action is the one
        # this function takes on its answer outside shadow; a failure argues
        # for nothing, whatever the failure mode then applies.
        **side_fields(
            POST_ACTIONS, baseline_action="preserve",
            jev_action=None if verdict.failure is not None
            else "human" if human else "preserve",
            question_version=question_version(
                OUTCOME_QUESTIONS, build_outcome_questions(),
            ),
        ),
        **narrow_fields(verdict.narrow),
        **request_fields(verdict.latency_ms, verdict.usage),
    })
    _append(repo, payload)
    if human and not shadow:
        # Check once more after logging; annotations must not target a closed bead.
        current = bd.show(issue_id)
        if current.get("status") != "closed":
            if "human" not in current.get("labels", []):
                bd.add_label(issue_id, "human")
            prefix = "PLAN-GAP: " if reason == "plan_gap" else ""
            bd.add_comment(issue_id, f"{prefix}judge post_turn requires human handling: {reason}")
    output.progress("grind", f"judge post_turn {reason}")
    return not shadow and (human or (
        not _no_basis(verdict) and verdict.outcome in (Outcome.FLAKE, Outcome.CONTINUE)
    ))
