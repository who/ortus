"""Resolve literal criteria packs for one explicitly named seat."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from typing import TYPE_CHECKING, Mapping

from ortus.core.judge import JudgeConfig, JudgeRoute, _parse_judge_values
from ortus.core.profiles import ProfileError

if TYPE_CHECKING:
    from ortus.core.config import Config

CRITERIA_VERSION = "pre-turn-v2"
_SAFE_OVERRIDES = {
    "route_confidence", "noul_confidence", "risk_confidence", "human_threshold",
    "risk_threshold", "routes", "question_criteria", "include_issue_text",
    "sensitive_paths",
}


def _alias(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        raise ProfileError("invalid judge alias: expected a named alias of at most 64 characters")
    return value


def validate_criteria(value: object) -> dict:
    """Accept only literal descriptions in the existing three question schemas."""
    def text(item: object) -> bool:
        return isinstance(item, str) and bool(item.strip()) and len(item) <= 2048 and not any(
            ord(c) < 32 and c not in "\n\t" or 0xD800 <= ord(c) <= 0xDFFF for c in item
        )

    def texts(item: object) -> bool:
        return isinstance(item, list) and 0 < len(item) <= 16 and all(map(text, item))

    valid = isinstance(value, dict) and not set(value) - {"route", "needs_human", "action_risk"}
    if valid and "route" in value:
        routes = value["route"]
        valid = isinstance(routes, dict) and bool(routes) and not set(routes) - set(JudgeRoute)
        if valid:
            valid = all(
                isinstance(row, dict) and set(row) == {"what", "not_for", "examples"}
                and text(row["what"]) and text(row["not_for"]) and texts(row["examples"])
                for row in routes.values()
            )
    if valid and "needs_human" in value:
        row = value["needs_human"]
        valid = isinstance(row, dict) and set(row) == {"true", "false"} and all(map(texts, row.values()))
    if valid and "action_risk" in value:
        rows = value["action_risk"]
        valid = isinstance(rows, list) and len(rows) == 3 and all(
            isinstance(row, dict) and set(row) == {"level", "what"}
            and row["level"] == level and text(row["what"])
            for row, level in zip(rows, ("routine", "elevated", "dangerous"))
        )
    if not valid:
        raise ProfileError("invalid judge.question_criteria: expected literal atomic criteria")
    return deepcopy(value)


def _overlay(base: dict, overlay: dict) -> dict:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _overlay(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def resolve_pack(
    cfg: Config, *, judge: bool | None = None, judge_seat: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> JudgeConfig:
    """Validate all definitions, then resolve only the selected seat without I/O."""
    from ortus.core.config import Config

    table = cfg.get("judge", {})
    if not isinstance(table, dict):
        raise ProfileError("invalid judge configuration: expected a TOML table")
    base = {k: v for k, v in table.items() if k not in {"packs", "seats"}}
    packs, seats = table.get("packs", {}), table.get("seats", {})
    if not isinstance(packs, dict) or not isinstance(seats, dict):
        raise ProfileError("judge.packs and judge.seats must be TOML tables")
    _parse_judge_values(Config(values={"judge": base}), environ={})
    for name, pack in packs.items():
        _alias(name)
        if not isinstance(pack, dict) or set(pack) - _SAFE_OVERRIDES:
            raise ProfileError("invalid judge pack override")
        _parse_judge_values(Config(values={"judge": _overlay(base, pack)}), environ={})
    for name, seat in seats.items():
        _alias(name)
        if not isinstance(seat, dict) or set(seat) - (_SAFE_OVERRIDES | {"enabled", "pack"}):
            raise ProfileError("invalid judge seat override")
        pack = seat.get("pack")
        if pack is not None and (_alias(pack) not in packs):
            raise ProfileError("judge seat references a missing pack")
        resolved = _overlay(_overlay(base, packs.get(pack, {})), {k: v for k, v in seat.items() if k != "pack"})
        _parse_judge_values(Config(values={"judge": resolved}), environ={})
    env = os.environ if environ is None else environ
    selected = _alias(judge_seat if judge_seat is not None else env.get("ORTUS_JUDGE_SEAT", base.get("seat", "default")))
    # Legacy single-seat tables remain valid. Once a seat registry is declared,
    # every selected name must resolve in that registry, including "default".
    if ("seats" in table or judge_seat is not None or "ORTUS_JUDGE_SEAT" in env) and selected not in seats:
        raise ProfileError("judge selected seat is missing")
    seat = seats.get(selected, {})
    resolved = _overlay(_overlay(base, packs.get(seat.get("pack"), {})), {k: v for k, v in seat.items() if k != "pack"})
    resolved["seat"] = selected
    return _parse_judge_values(Config(values={"judge": resolved}), judge=judge, environ=env)


def criteria_hash(config: JudgeConfig, questions: dict) -> str:
    """Hash resolved questions and policy without storing literal text in events."""
    body = {key: getattr(config, key) for key in sorted(_SAFE_OVERRIDES - {"question_criteria"})}
    body.update(version=CRITERIA_VERSION, questions=questions)
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
