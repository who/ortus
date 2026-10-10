"""Fake System One answer documents in the narrow question shapes."""

from __future__ import annotations

PRE_TURN_NOULS = {
    "has_basis": 0.9,
    "needs_operator_input": 0.04,
    "destructive_scope": 0.1,
    "cross_module_reasoning": 0.3,
    "novel_algorithm": 0.2,
    "router_has_basis": 0.8,
}
READINESS_NOULS = {
    "acceptance_observable": 0.9,
    "open_product_decision": 0.1,
    "design_matches_objective": 0.85,
    "has_basis": 0.9,
}
TOOL_NOULS = {
    "destructive": 0.02,
    "credentials": 0.01,
    "out_of_scope": 0.05,
    "has_basis": 0.9,
}


def nouls(values: dict) -> dict:
    return {name: {"type": "noul", "noul": value} for name, value in values.items()}


def pre_turn_answers(route: str = "claude", confidence: float = 0.93, **values) -> dict:
    """The answers mapping for a pre-turn-v3 request."""
    return {
        "route": {"type": "choice", "choice": route, "confidence": confidence},
        **nouls({**PRE_TURN_NOULS, **values}),
    }


def pre_turn_response(model: str = "jev-1.13.0", route: str = "claude", **values) -> dict:
    return {"model": model, "answers": pre_turn_answers(route, **values)}


def readiness_response(model: str = "jev-1.13.0", **values) -> dict:
    return {"model": model, "answers": nouls({**READINESS_NOULS, **values})}


def tool_response(model: str = "jev-1.13.0", usage: dict | None = None, **values) -> dict:
    body = {"model": model, "answers": nouls({**TOOL_NOULS, **values})}
    if usage is not None:
        body["usage"] = usage
    return body
