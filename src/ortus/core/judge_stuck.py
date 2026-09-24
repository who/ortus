"""What grind does with a claim whose window ended without a close.

The post-turn verdict arrives here as an opinion, never as an instruction: it
is turned into a probability vector over the three things grind can do with a
live claim — resume it, resume it with a re-plan directive, or hand it to the
human queue — and System Two takes the argmax in code. Confidence only shrinks
that vector toward uniform, so an unsure judge moves a bead toward the middle
action instead of parking it; there is no confidence floor anywhere in this
module.

When there is no usable verdict the module fails open to the rule that
predates it: escalate at :data:`WEDGED_WINDOW_THRESHOLD` consecutive no-close
windows, resume below it. A seat with Jev off therefore sees exactly the
behavior it saw before.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

from ortus.core.judge import JudgeConfig
from ortus.core.judge_log import _append, _clean_metadata, _common
from ortus.core.judge_post import Outcome, OutcomeVerdict


class StuckAction(str, Enum):
    CONTINUE = "continue"
    REPLAN = "replan"
    ESCALATE = "escalate"


#: Consecutive resumed windows a leftover claim may end still in_progress with
#: no new commits on the integration branch before the fail-open rule hands it
#: to the human queue. The field case (fh-aqi) burned exactly two workers
#: before a human stepped in. It is the baseline only: with a verdict in hand
#: the decision is made after the first such window, not the second.
WEDGED_WINDOW_THRESHOLD = 2

#: Least drastic first. Ties resolve to the earliest entry, so a flat vector
#: resumes the claim rather than spending an operator's attention on it.
_ACTIONS: tuple[StuckAction, ...] = (
    StuckAction.CONTINUE, StuckAction.REPLAN, StuckAction.ESCALATE,
)

#: The class each outcome argues for. `flake` is a transient death and `done`
#: is a claim the tracker disagrees with — both are answered by giving the
#: work another window, not by rewriting the spec or fetching a human.
_OUTCOME_ACTIONS: dict[Outcome, StuckAction] = {
    Outcome.CONTINUE: StuckAction.CONTINUE,
    Outcome.FLAKE: StuckAction.CONTINUE,
    Outcome.DONE: StuckAction.CONTINUE,
    Outcome.PLAN_GAP: StuckAction.REPLAN,
    Outcome.AUTH: StuckAction.ESCALATE,
    Outcome.NEEDS_HUMAN: StuckAction.ESCALATE,
}

#: Fraction of the continue mass the window prior moves per burned window, and
#: the ceiling it moves in total. A claim that keeps failing to close loses
#: continue mass whatever the judge said about it.
_WINDOW_STEP = 0.25
_WINDOW_CAP = 0.75
#: Share of that moved mass which lands on escalate rather than replan, per
#: window. By the third no-close window a re-plan has had its chance.
_ESCALATE_SHARE_STEP = 0.34
#: Fraction of the non-continue mass returned to continue when the window did
#: advance the integration branch. Visible progress is the strongest evidence
#: there is that another window is worth spending.
_ADVANCED_PULL = 0.5
#: Fraction of the replan mass pushed to escalate per re-plan already spent on
#: this claim. A directive that did not work the first time is weak evidence
#: for a second one.
_REPLAN_STEP = 0.6


@dataclass(frozen=True)
class StuckDecision:
    """One decision plus the vector it was read off, for the log and replay."""

    action: StuckAction
    p_continue: float
    p_replan: float
    p_escalate: float
    windows: int
    branch_advanced: bool
    fail_open: bool

    def vector(self) -> dict[str, float]:
        """The probability vector keyed by action value, log-ready."""
        return {
            StuckAction.CONTINUE.value: self.p_continue,
            StuckAction.REPLAN.value: self.p_replan,
            StuckAction.ESCALATE.value: self.p_escalate,
        }


def baseline_stuck_action(windows: int) -> StuckAction:
    """The pre-Jev rule: escalate at the threshold, otherwise resume."""
    return (StuckAction.ESCALATE if windows >= WEDGED_WINDOW_THRESHOLD
            else StuckAction.CONTINUE)


def _decided(action: StuckAction, weights: dict[StuckAction, float], windows: int,
             advanced: bool, fail_open: bool) -> StuckDecision:
    return StuckDecision(
        action, weights[StuckAction.CONTINUE], weights[StuckAction.REPLAN],
        weights[StuckAction.ESCALATE], windows, advanced, fail_open,
    )


def _certain(action: StuckAction) -> dict[StuckAction, float]:
    return {entry: 1.0 if entry is action else 0.0 for entry in _ACTIONS}


def decide_stuck_claim(
    verdict: OutcomeVerdict | None, windows: int, branch_advanced: bool,
    *, replans: int = 0,
) -> StuckDecision:
    """Decide continue / re-plan / escalate for a claim that did not close.

    `windows` is the claim's consecutive no-close window count and `replans`
    how many re-plan directives it has already been given. A missing verdict,
    a typed judge failure and an answerless verdict all fail open to
    :func:`baseline_stuck_action`, which is why a disabled Jev changes
    nothing. Otherwise the verdict's confidence lands on the class its choice
    argues for and the rest spreads uniformly, so zero confidence is the
    uniform vector rather than a park; the window count then moves mass off
    continue, a spent re-plan moves mass off replan, and an advanced
    integration branch pulls mass back to continue.
    """
    windows = max(int(windows), 0)
    replans = max(int(replans), 0)
    advanced = bool(branch_advanced)
    if verdict is None or verdict.failure is not None or verdict.outcome is None:
        action = baseline_stuck_action(windows)
        return _decided(action, _certain(action), windows, advanced, True)
    if windows < 1:
        # The claim has not yet burned a window; there is nothing to decide
        # and the caller simply resumes it.
        return _decided(
            StuckAction.CONTINUE, _certain(StuckAction.CONTINUE), windows,
            advanced, False,
        )

    confidence = min(max(float(verdict.confidence or 0.0), 0.0), 1.0)
    argued = _OUTCOME_ACTIONS.get(verdict.outcome, StuckAction.CONTINUE)
    spread = (1.0 - confidence) / len(_ACTIONS)
    weights = {
        entry: spread + (confidence if entry is argued else 0.0)
        for entry in _ACTIONS
    }

    moved = weights[StuckAction.CONTINUE] * min(_WINDOW_STEP * windows, _WINDOW_CAP)
    weights[StuckAction.CONTINUE] -= moved
    escalate_share = min(_ESCALATE_SHARE_STEP * windows, 1.0)
    weights[StuckAction.ESCALATE] += moved * escalate_share
    weights[StuckAction.REPLAN] += moved * (1.0 - escalate_share)

    if replans:
        spent = weights[StuckAction.REPLAN] * min(_REPLAN_STEP * replans, 1.0)
        weights[StuckAction.REPLAN] -= spent
        weights[StuckAction.ESCALATE] += spent

    if advanced:
        for entry in (StuckAction.REPLAN, StuckAction.ESCALATE):
            returned = weights[entry] * _ADVANCED_PULL
            weights[entry] -= returned
            weights[StuckAction.CONTINUE] += returned

    total = sum(weights.values())
    if total <= 0:
        return _decided(
            StuckAction.CONTINUE, _certain(StuckAction.CONTINUE), windows,
            advanced, False,
        )
    weights = {entry: weights[entry] / total for entry in _ACTIONS}
    action = max(_ACTIONS, key=lambda entry: (weights[entry], -_ACTIONS.index(entry)))
    return _decided(action, weights, windows, advanced, False)


def log_stuck_decision(
    repo: Path, config: JudgeConfig, run_id: UUID, issue_id: str,
    decision: StuckDecision, applied: StuckAction,
) -> None:
    """Append one stuck-decision record beside the phase records.

    `applied` is what grind actually did, which differs from the decision's
    own action in shadow mode: there the vector is recorded and the fail-open
    baseline is what runs.
    """
    payload = _common("stuck_decision", run_id, uuid4())
    payload.update({
        "phase": "stuck", "mode": config.mode.value,
        "issue_id": _clean_metadata(issue_id, config),
        "seat": _clean_metadata(config.seat, config), "model": config.model,
        "windows": decision.windows,
        "branch_advanced": decision.branch_advanced,
        "fail_open": decision.fail_open,
        "vector": decision.vector(),
        "action": decision.action.value,
        "effective_action": applied.value,
    })
    _append(repo, payload)
