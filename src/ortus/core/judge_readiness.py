"""Advisory semantic readiness after the structural schema passes."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from ortus.core import output
from ortus.core.config import load_config
from ortus.core.judge import (
    GateAction, GateDecision, GateReason, JudgeConfig, JudgeMode, JudgePhase,
    JudgeRoute, JudgeState, WORKER_ROUTES, parse_judge_config,
)
from ortus.core.judge_log import DecisionEvent, elapsed_ms, write_decision
from ortus.core.judge_state import pack_state
from ortus.core.judge_typesafe import JudgeFailure, JudgeVerdict, TypeSafeJudge, route_options
from ortus.core.readiness import validate_issue


def readiness_questions(config: JudgeConfig, state: JudgeState) -> dict:
    """Use the existing typed vector with criteria about packet actionability."""
    overrides = config.semantic_readiness_criteria
    routes = {}
    for route in route_options(config, state):
        if route == JudgeRoute.HUMAN:
            what = "The objective, design or acceptance leaves an unresolved product decision."
        elif route == JudgeRoute.SKIP:
            what = "The packet describes no concrete change to implement."
        else:
            what = "A worker can implement the objective using the design and verify the acceptance criteria."
        routes[route.value] = {
            "what": what,
            "not_for": "Classifying syntax or heading presence; structural validation already passed.",
            "examples": ["Compare the objective with the design and the observable acceptance checks."],
        }
        routes[route.value].update(overrides.get("route", {}).get(route.value, {}))
    return {
        "route": {
            "type": "choice",
            "instructions": "Assess semantic readiness of this work spec, not backend preference.",
            "criteria": routes,
        },
        "needs_human": {
            "type": "noul",
            "instructions": "Does this packet leave a material decision unspecified or contradictory?",
            "criteria": deepcopy(overrides.get("needs_human", {
                "true": ["A worker must invent behavior or resolve conflicting requirements to implement it."],
                "false": ["The objective, design and acceptance agree and specify the required behavior."],
            })),
        },
        "action_risk": {
            "type": "score",
            "instructions": "How much execution risk comes from ambiguity or untestable acceptance in this packet?",
            "criteria": deepcopy(overrides.get("action_risk", [
                {"level": "routine", "what": "Concrete design and observable checks support the objective."},
                {"level": "elevated", "what": "Some requirements or checks need interpretation."},
                {"level": "dangerous", "what": "Vague, contradictory or untestable requirements make completion unverifiable."},
            ])),
        },
    }


def evaluate_readiness(
    repo: Path, issue: dict, config: JudgeConfig | None = None,
) -> dict | None:
    """Return screened advice; never change readiness, tracker state or routing."""
    report = validate_issue(issue)
    if not report.ready or report.exempt:
        return None
    try:
        config = config if config is not None else parse_judge_config(load_config(repo=repo))
        if not config.enabled:
            return None
        # Semantic advice is observational even when pre-turn routing enforces.
        config = replace(config, mode=JudgeMode.SHADOW)
        state = pack_state(issue, config, phase=JudgePhase.SEMANTIC_READINESS).state
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
            run_id=uuid4(), seat=state.seat, issue_id=state.issue_id, phase=state.phase,
            answers=verdict.answers, decision=GateDecision(GateAction.PROCEED, baseline, reason),
            model=config.model, criteria_version=state.criteria_version,
            criteria_hash=state.criteria_hash, latency_ms=elapsed_ms(started),
            failure=verdict.failure, usage=verdict.usage,
        ))
        return {
            "issue_id": state.issue_id,
            "status": verdict.failure.value if verdict.failure else "advisory",
            "answers": asdict(verdict.answers) if verdict.answers else None,
            "decision_id": str(decision_id),
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
