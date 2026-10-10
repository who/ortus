"""Collection mode: ask Jev at every site, apply only the code baseline.

`[judge] collect = true` (or `ORTUS_JEV_COLLECT=1`) exists to gather labeled
decisions from every site in one grind. Each site already has a shadow path
that records Jev's would-be action beside the baseline it applies, so
collection is nothing more than turning every one of those paths on at once
and forcing the seat into shadow. It can only make Jev less authoritative: a
seat with `mode = "enforce"` is demoted for the run, and no site gains a way
to write the tracker, reap a worker or deny a tool beyond local policy.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ortus.core.agent import BackendError
from ortus.core.judge import JudgeConfig, JudgeMode
from ortus.core.judge_hooks import check_pre_tool

#: Every site collection arms, in the order a grind iteration reaches them.
COLLECT_SITES: tuple[str, ...] = (
    "readiness", "pre_turn", "router", "pre_tool", "progress", "stuck",
    "triage", "post_turn",
)


def collection_arming(
    repo: Path, judge_config: JudgeConfig, backend: str, *, docker: bool = False,
) -> dict[str, bool]:
    """Which sites this run arms; empty when collection is off.

    Only pre_tool can be unavailable. Its hook is Claude-only and needs the
    run-scoped hook settings to be honored, and a collection run reports that
    rather than halting: the other sites still have decisions to record. A
    seat that set `pre_tool` itself keeps the refusal it always had.
    """
    if not judge_config.collect:
        return {}
    arming = dict.fromkeys(COLLECT_SITES, True)
    if not judge_config.pre_tool:
        try:
            check_pre_tool(repo, backend, docker=docker)
        except BackendError:
            arming["pre_tool"] = False
    return arming


def collecting_config(judge_config: JudgeConfig, arming: dict[str, bool]) -> JudgeConfig:
    """The seat as a collection run sees it: enabled, shadow, every phase on.

    The bounded log tail goes to the progress check because the operator
    opted in by turning collection on; sensitive paths and secret screening
    still apply to it as they do everywhere else.
    """
    if not arming:
        return judge_config
    return replace(
        judge_config,
        enabled=True,
        mode=JudgeMode.SHADOW,
        pre_tool=arming["pre_tool"],
        post_turn=True,
        include_log_tail=True,
    )


def collection_note(judge_config: JudgeConfig, arming: dict[str, bool]) -> str:
    """The one startup line naming what collection armed and what it overrode."""
    armed = [site for site in COLLECT_SITES if arming.get(site)]
    note = "judge collect: shadow on " + ", ".join(armed)
    if not arming.get("pre_tool"):
        note += " (pre_tool unavailable on this backend)"
    if judge_config.mode == JudgeMode.ENFORCE and judge_config.enabled:
        note += " (mode=enforce overridden)"
    return note
