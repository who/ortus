"""Pin-don't-park: the harness reading of a parked environment skew (ortus-jvpn).

A worker that meets a declared value the installed tool cannot honour is
told by both bundled contracts to pin it, file a restore follow-up, and
carry on. A worker that parks it on the `human` label anyway stops the
queue, and the park reads exactly like a missing product decision. The
classifier here separates the two from the comment that flagged the claim,
and the post-window pass tags the pin-able ones so the park carries its own
diagnosis instead of joining generic human parking.

The bar the classifier is held to is asymmetric on purpose: a skew it fails
to recognise leaves today's PLAN-GAP untouched, while a planning gap it
mistakes for a pin tells an operator a decision nobody made was a version
number. Every case below drives the pure functions, and the last group
drives the tracker pass with a double so a tracker error is an annotation
that did not happen rather than a run that ended.
"""

from __future__ import annotations

from typing import Any

import pytest

from ortus.core.pin_skew import (
    PIN_SKEW_LABEL,
    classify_plan_gap,
    latest_plan_gap,
    pin_skew_note,
    tag_pin_skew_claims,
)

# The transcript shape: a declared compatibility date above the runtime the
# installed test runner bundles.
_SKEW = (
    "PLAN-GAP: `wrangler.toml` declares compatibility_date 2026-09-01, but the "
    "installed @cloudflare/vitest-pool-workers 0.22.0 bundles a runtime whose "
    "maximum compatibility date is 2026-08-22, so the suite refuses to start."
)

_TRUE_GAP = (
    "PLAN-GAP: the design requires the queue to be authoritative while "
    "`src/ortus/core/runstate.py` treats the journal as authoritative. Which "
    "one wins is a product decision nobody has made."
)


class _FakeBd:
    """Tracker double: labels and comments per issue, writes recorded only.

    ``fail`` names the call the real client would fail on, so a test can ask
    what the pass does with a tracker that answers one question and refuses
    the next.
    """

    def __init__(
        self,
        issues: dict[str, tuple[list[str], list[str]]],
        *,
        fail: str = "",
    ) -> None:
        self.issues = issues
        self.fail = fail
        self.labels: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []

    def show(self, issue_id: str) -> dict[str, Any]:
        if self.fail == "show":
            raise RuntimeError("tracker down")
        return {"id": issue_id, "labels": list(self.issues[issue_id][0])}

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        if self.fail == "comments":
            raise RuntimeError("tracker down")
        return [{"text": text} for text in self.issues[issue_id][1]]

    def add_label(self, issue_id: str, label: str) -> None:
        if self.fail == "label":
            raise RuntimeError("tracker down")
        self.labels.append((issue_id, label))

    def add_comment(self, issue_id: str, body: str) -> None:
        if self.fail == "comment":
            raise RuntimeError("tracker down")
        self.notes.append((issue_id, body))


def _tag(bd: _FakeBd, ids: list[str]) -> tuple[set[str], list[str]]:
    logged: list[str] = []
    tagged = tag_pin_skew_claims(
        bd, ids, window=3, write_log=logged.append  # type: ignore[arg-type]
    )
    return tagged, logged


# --- the classifier --------------------------------------------------------


def test_declared_value_past_the_installed_ceiling_is_pin_able() -> None:
    """AC-3: the skew the transcript carried classifies as pin-able, and the
    evidence names the literals and the tool rather than restating prose."""
    verdict = classify_plan_gap(_SKEW)
    assert verdict.pin_able
    assert "2026-09-01" in verdict.evidence
    assert any("vitest-pool-workers" in item for item in verdict.evidence)
    assert "maximum" in verdict.evidence


def test_a_missing_decision_is_not_pin_able() -> None:
    """AC-2: a gap that is only prose carries no version, date, or tool
    evidence, so it stays the planning gap the worker recorded."""
    assert not classify_plan_gap(_TRUE_GAP).pin_able
    assert classify_plan_gap(_TRUE_GAP).evidence == ()
    assert not classify_plan_gap("").pin_able


@pytest.mark.parametrize(
    "text",
    [
        # One literal: nothing says what ceiling it passed.
        "PLAN-GAP: `pyproject.toml` pins ruff 0.6.9 and the installed one is "
        "older, so the lint gate cannot run.",
        # Two literals, no ceiling named: a version disagreement can still be
        # a design question about which one the product wants.
        "PLAN-GAP: `pyproject.toml` asks for 3.12 while the design document "
        "describes 3.10 semantics; the intended target is undecided.",
        # A ceiling and two literals with no tool beside either: a schedule,
        # not an environment.
        "PLAN-GAP: the spec's 2026-09-01 milestone exceeds the 2026-08-22 "
        "freeze the operator asked for.",
    ],
)
def test_partial_evidence_is_not_enough(text: str) -> None:
    """AC-3: any one of the three evidence kinds missing leaves the comment a
    planning gap — the classifier never infers a pin from a bare number."""
    assert not classify_plan_gap(text).pin_able


def test_the_flagging_comment_is_the_last_plan_gap() -> None:
    """A thread carrying an answered gap and then a fresh one classifies the
    fresh one; a thread with no gap at all classifies nothing."""
    thread = [
        {"text": _TRUE_GAP},
        {"text": "operator: answered, the queue wins."},
        {"text": _SKEW},
    ]
    assert latest_plan_gap(thread) == _SKEW
    assert classify_plan_gap(latest_plan_gap(thread)).pin_able
    assert latest_plan_gap([{"text": "just a progress note"}]) == ""
    assert latest_plan_gap([]) == ""


# --- the post-window pass --------------------------------------------------


def test_pin_able_park_is_tagged_and_logged() -> None:
    """AC-3: a pin-able park is labelled and annotated, and the log names the
    evidence so the run record reads as a redirect, not a park."""
    bd = _FakeBd({"ortus-a": (["human"], [_SKEW])})
    tagged, logged = _tag(bd, ["ortus-a"])
    assert tagged == {"ortus-a"}
    assert bd.labels == [("ortus-a", PIN_SKEW_LABEL)]
    assert bd.notes[0][0] == "ortus-a"
    assert "pin-don't-park" in bd.notes[0][1]
    assert "file a follow-up bead" in bd.notes[0][1]
    assert any("pin-skew: ortus-a" in line and "2026-09-01" in line for line in logged)
    # The park itself is untouched: the human label is the operator's to drop.
    assert "human" not in [label for _issue, label in bd.labels]
    assert "bd label remove ortus-a human" in bd.notes[0][1]


def test_generic_park_is_left_alone() -> None:
    """AC-2: a true planning gap is neither labelled nor commented, so human
    parking still reads the way it did before this pass existed."""
    bd = _FakeBd({"ortus-b": (["human"], [_TRUE_GAP])})
    tagged, logged = _tag(bd, ["ortus-b"])
    assert tagged == set()
    assert bd.labels == [] and bd.notes == []
    assert logged == []


def test_an_already_tagged_park_is_not_annotated_twice() -> None:
    """A claim resumed and parked again collects the note once: the label is
    read before the thread, so a repeat window adds nothing."""
    bd = _FakeBd({"ortus-c": (["human", PIN_SKEW_LABEL], [_SKEW])})
    tagged, _ = _tag(bd, ["ortus-c"])
    assert tagged == set()
    assert bd.labels == [] and bd.notes == []


def test_several_parks_are_classified_independently() -> None:
    """Two parks in one window: the pin-able one is tagged and the planning
    gap beside it is not."""
    bd = _FakeBd(
        {
            "ortus-d": (["human"], [_SKEW]),
            "ortus-e": (["human"], [_TRUE_GAP]),
        }
    )
    tagged, _ = _tag(bd, ["ortus-e", "ortus-d"])
    assert tagged == {"ortus-d"}
    assert bd.labels == [("ortus-d", PIN_SKEW_LABEL)]


@pytest.mark.parametrize("fail", ["show", "comments", "label", "comment"])
def test_a_tracker_error_is_logged_not_raised(fail: str) -> None:
    """Every tracker call in the pass can fail: the annotation is skipped and
    logged against its issue, and the window ends the way it would have."""
    bd = _FakeBd({"ortus-f": (["human"], [_SKEW])}, fail=fail)
    tagged, logged = _tag(bd, ["ortus-f"])
    assert tagged == set()
    assert any("pin-skew" in line and "ortus-f" in line for line in logged)


def test_the_note_names_the_evidence_and_the_release_command() -> None:
    """The comment an operator reads carries the evidence and the one command
    that releases the park; it never claims to have released it."""
    note = pin_skew_note("ortus-g", ("2026-08-22", "wrangler.toml", "maximum"))
    assert "2026-08-22" in note and "wrangler.toml" in note
    assert "bd label remove ortus-g human" in note
    assert PIN_SKEW_LABEL in note


def test_grind_runs_the_pass_after_a_flagged_claim() -> None:
    """The post-window flagged-claim path is where the pass runs: grind binds
    the same function rather than a copy of the rule."""
    from ortus.commands import grind as grind_mod

    assert grind_mod.tag_pin_skew_claims is tag_pin_skew_claims
