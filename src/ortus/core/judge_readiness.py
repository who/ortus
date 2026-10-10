"""Advisory semantic readiness after the structural schema passes."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4

from ortus.core import output
from ortus.core.config import load_config
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeConfig, JudgeMode, JudgePhase,
    JudgeState, WORKER_ROUTES, parse_judge_config,
)
from ortus.core.judge_log import DecisionEvent, elapsed_ms, write_decision
from ortus.core.judge_state import OmissionReason, pack_state
from ortus.core.judge_questions import HAS_BASIS, basis_question, noul_question, soft_bands
from ortus.core.judge_typesafe import (
    ACTION_RISK, NEEDS_HUMAN, ROUTE, JudgeFailure, JudgeVerdict, TypeSafeJudge,
    report_ignored,
)
from ortus.core.readiness import validate_issue


#: The narrow readiness questions (question set readiness-v3).
ACCEPTANCE_OBSERVABLE = "acceptance_observable"
OPEN_PRODUCT_DECISION = "open_product_decision"
DESIGN_MATCHES_OBJECTIVE = "design_matches_objective"


def readiness_questions(config: JudgeConfig, state: JudgeState) -> dict:
    """Three narrow questions about one work spec, plus their basis.

    Section sizes, the criterion count and what Concrete locations names reach
    Jev as code-computed facts, so none of these asks it to count anything.
    Overrides written for the retired route/needs_human/action_risk rubric
    still load and are reported once as ignored.
    """
    report_ignored("semantic_readiness_criteria", config.semantic_readiness_criteria,
                   (ROUTE, NEEDS_HUMAN, ACTION_RISK))
    return {
        ACCEPTANCE_OBSERVABLE: noul_question(
            "Does every acceptance criterion name an observable result for the objective?",
            ["Each criterion names a check or an output a reader could observe pass or fail."],
            ["At least one criterion is a wish, a restatement, or has nothing to observe."],
        ),
        OPEN_PRODUCT_DECISION: noul_question(
            "Does the spec leave a product decision unresolved?",
            ["A worker would have to choose behavior the spec does not settle."],
            ["The objective, design and acceptance settle the behavior to build."],
        ),
        DESIGN_MATCHES_OBJECTIVE: noul_question(
            "Does the design implement the stated objective?",
            ["Carrying out the design produces the behavior the objective describes."],
            ["The design does something else, or leaves part of the objective out."],
        ),
        HAS_BASIS: basis_question("these readiness answers"),
    }


def evaluate_readiness(
    repo: Path, issue: dict, config: JudgeConfig | None = None,
    *, run_id: UUID | None = None,
) -> dict | None:
    """Return screened advice; never change readiness, tracker state or routing.

    A grind passes its own `run_id` so the shadow outcome it writes for this
    decision after the worker exits pairs with it in replay; other callers
    have no worker to observe and get a fresh id.
    """
    report = validate_issue(issue)
    if not report.ready or report.exempt:
        return None
    try:
        config = config if config is not None else parse_judge_config(load_config(repo=repo))
        if not config.enabled:
            return None
        # Semantic advice is observational even when pre-turn routing enforces.
        config = replace(config, mode=JudgeMode.SHADOW)
        packed = pack_state(issue, config, phase=JudgePhase.SEMANTIC_READINESS)
        state = packed.state
        # Length alone no longer silences the judge: an oversized field arrives
        # shortened. Empty, private and sensitive text still leave nothing to ask about.
        truncated = tuple(sorted(
            omission.field for omission in packed.omissions
            if omission.reason is OmissionReason.TRUNCATED
        ))
        if not all((state.title.strip(), state.objective.strip(), state.acceptance.strip(), state.design.strip())):
            output.progress("validate", "semantic readiness unavailable: issue text omitted; schema result unchanged")
            return {"status": "text_unavailable", "answers": None}
        started = time.monotonic()
        try:
            verdict = TypeSafeJudge(config).evaluate(state)
        except Exception:
            verdict = JudgeVerdict(failure=JudgeFailure.SERVICE_ERROR)
        baseline = next(route for route in config.routes if route in WORKER_ROUTES)
        reason = GateReason.SERVICE_FAILURE if verdict.failure else GateReason.ROUTED
        decision_id = write_decision(repo, config, DecisionEvent(
            run_id=run_id or uuid4(), seat=state.seat, issue_id=state.issue_id, phase=state.phase,
            answers=verdict.answers, decision=GateDecision(GateAction.PROCEED, baseline, reason),
            model=config.model, criteria_version=state.criteria_version,
            criteria_hash=state.criteria_hash, latency_ms=elapsed_ms(started),
            failure=verdict.failure, usage=verdict.usage, truncated_fields=truncated,
        ))
        return {
            "issue_id": state.issue_id,
            "status": verdict.failure.value if verdict.failure else "advisory",
            "answers": asdict(verdict.answers) if verdict.answers else None,
            # Labels only: readiness advice never changes the schema verdict.
            "soft_bands": soft_bands(verdict.answers.narrow) if verdict.answers else None,
            "decision_id": str(decision_id),
            "truncated_fields": list(truncated),
        }
    except Exception:
        # Configuration, packing and logging failures also preserve the schema
        # verdict. Exception bodies can contain private text; never print them.
        output.progress("validate", "semantic readiness unavailable; schema result unchanged")
        return {"status": "unavailable", "answers": None}


def readiness_context(advice: dict | None) -> str:
    """Small typed vector for System Two, with no provider prose or authority."""
    if advice is None:
        return ""
    return (
        "\n\nJev semantic readiness advice for this issue, not a stop or claim instruction. "
        "System Two should assess the work spec itself. "
        + json.dumps(advice, separators=(",", ":"))
    )
