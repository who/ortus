"""Advisory outcome judgments. Tracker closure and mechanical failures win."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ortus.core import output
from ortus.core.judge import FailureMode, JudgeConfig, JudgeMode, JudgePhase
from ortus.core.judge_log import (
    OutcomeStatus, _append, _clean_metadata, _common,
)
from ortus.core.judge_state import StateError, pack_state
from ortus.core.judge_typesafe import (
    API_KEY_ENV, JudgeFailure, _answer, _default_client, _Invalid, _mapping, _number,
)


class Outcome(str, Enum):
    DONE = "done"
    PLAN_GAP = "plan_gap"
    FLAKE = "flake"
    AUTH = "auth"
    NEEDS_HUMAN = "needs_human"
    CONTINUE = "continue"


@dataclass(frozen=True)
class WorkerOutcome:
    exit_status: int
    watchdog: bool
    observed_status: OutcomeStatus
    branch_advanced: bool

    def __post_init__(self) -> None:
        if (type(self.exit_status) is not int
                or type(self.watchdog) is not bool
                or type(self.branch_advanced) is not bool
                or not isinstance(self.observed_status, OutcomeStatus)):
            raise ValueError("invalid worker outcome")

    def payload(self) -> dict:
        return {"exit_status": self.exit_status, "watchdog": self.watchdog,
                "observed_status": self.observed_status.value,
                "branch_advanced": self.branch_advanced}


@dataclass(frozen=True)
class OutcomeVerdict:
    outcome: Outcome | None = None
    confidence: float | None = None
    failure: JudgeFailure | None = None

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


def build_outcome_questions() -> dict:
    return {"outcome": {
        "type": "choice",
        "instructions": "Classify the observed worker outcome without granting completion.",
        "criteria": {
            "done": {"description": "The tracker records completed work."},
            "plan_gap": {"description": "The work specification needs an operator decision."},
            "flake": {"description": "A transient failure interrupted the worker."},
            "auth": {"description": "Authentication or access requires operator handling."},
            "needs_human": {"description": "The worker cannot safely proceed without a human."},
            "continue": {"description": "Incomplete work can continue in a fresh window."},
        },
    }}


def evaluate_outcome(
    issue: Mapping[str, object], observation: WorkerOutcome, config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
) -> OutcomeVerdict:
    """Send one bounded Choice request with sanitized state and typed facts only."""
    # Reserve space for the facts as well as the issue envelope.
    facts = {"worker": observation.payload()}
    overhead = len(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode())
    budget = config.total_bytes_cap - overhead
    if budget <= 0:
        raise StateError("judge outcome state budget is too small")
    packed = pack_state(issue, replace(config, total_bytes_cap=budget),
                        phase=JudgePhase.POST_TURN, environ=environ)
    payload = {**packed.to_payload(), **facts}
    if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) > config.total_bytes_cap:
        raise StateError("judge outcome state budget is too small")
    env = os.environ if environ is None else environ
    if not env.get(API_KEY_ENV, "").strip():
        return OutcomeVerdict(failure=JudgeFailure.KEY_MISSING)

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(payload, build_outcome_questions(),
                                           model=config.model, timeout=config.timeout_seconds)

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return OutcomeVerdict(failure=JudgeFailure.SDK_MISSING)
    except asyncio.TimeoutError:
        return OutcomeVerdict(failure=JudgeFailure.TIMEOUT)
    except Exception:  # noqa: BLE001 - provider prose never escapes
        return OutcomeVerdict(failure=JudgeFailure.SERVICE_ERROR)
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        answers = _mapping(body.get("answers"))
        if set(answers) != {"outcome"}:
            raise _Invalid
        answer = _answer(answers, "outcome", "choice")
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise _Invalid
        return OutcomeVerdict(Outcome(choice), _number(answer.get("confidence"), 1))
    except Exception:  # noqa: BLE001 - malformed response is a typed failure
        return OutcomeVerdict(failure=JudgeFailure.INVALID_ANSWER)


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
    elif verdict.confidence < 0.8:
        human = True
        reason = "low_confidence"
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
    return not shadow and (human or verdict.outcome in (Outcome.FLAKE, Outcome.CONTINUE))
