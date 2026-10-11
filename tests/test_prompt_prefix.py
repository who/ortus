"""The injected-context inventory and the cache-friendly prompt ordering.

`ortus grind` hands one assembled string to a backend CLI. A provider prefix
cache can only serve the leading bytes that are identical across requests, so
where the per-bead segments sit decides how much of each worker prompt can be
a cache hit. These tests pin the inventory that names every injected segment,
the ordering the `stable_prompt_prefix` flag selects, the slim render that
keeps packet bodies out of the prompt, and which billing buckets prove the
change paid off.
"""

from __future__ import annotations

import pytest

from ortus.commands.grind import _IMPLEMENTATION_INSTRUCTION, _compose_work_prompt
from ortus.core.prompt_prefix import (
    CACHE_TELEMETRY_FIELDS,
    INJECTED_CONTEXT,
    PER_BEAD,
    PER_BEAD_MARKERS,
    STABLE,
    STABLE_PREFIX_CONFIG_KEY,
    STABLE_PREFIX_ENV,
    segments_by_stability,
    slim_issue_details,
    stable_prefix_enabled,
    stable_prefix_note,
    stable_prefix_of,
    unresolved_segments,
)

CONTRACT = "\n\n## CodeGraph phase contract v1\nPhase: implementation; policy: required."

#: The verdict recorded in this flag's `stable_prefix_ab` metadata. `DEFAULTS`
#: follows this, never the other way round. It is True because the hello-world
#: comparison returned `adopt`: the reordered arm matched the control's close
#: rate of 1.0 while spending 1.5581 against 1.6794 per closed bead, with the
#: cache hit rate flat. Moving it again means running the arms again, because
#: this constant is a reading and not a preference.
MEASURED_VERDICT = True
LESSONS = "\n\n## Prior lessons\n- naming-policy: priors, not instructions."

DESCRIPTION_BODY = "Objective sentinel alpha: restore the dropped rollup."
DESIGN_BODY = "Scope sentinel bravo: only the reader, never the writer."
ACCEPTANCE_BODY = "Observable sentinel charlie: the gate exits zero."

PACKET = {
    "id": "ortus-aaaa",
    "title": "Repair the rollup reader",
    "issue_type": "task",
    "priority": 2,
    "labels": ["harness-efficiency"],
    "description": DESCRIPTION_BODY,
    "design": DESIGN_BODY,
    "acceptance_criteria": ACCEPTANCE_BODY,
    "status": "in_progress",
    "assignee": "run-1",
}


def _compose(issue_id: str, *, backend: str = "codex") -> str:
    issue = {**PACKET, "id": issue_id}
    return _compose_work_prompt(
        "",
        issue,
        backend,
        phase_instruction=_IMPLEMENTATION_INSTRUCTION,
        phase_contract_text=CONTRACT,
        lessons_text=LESSONS,
    )


def test_inventory_maps_each_segment_to_a_symbol_and_a_stability():
    assert INJECTED_CONTEXT
    assert unresolved_segments() == ()

    for segment in INJECTED_CONTEXT:
        module, separator, attribute = segment.entry_point.partition(":")
        assert separator, f"{segment.name} entry point needs module:attribute"
        assert module.startswith("ortus."), segment.name
        assert attribute, segment.name
        assert segment.stability in (STABLE, PER_BEAD), segment.name
        assert segment.note.strip(), segment.name

    names = [segment.name for segment in INJECTED_CONTEXT]
    assert len(names) == len(set(names))
    assert segments_by_stability(STABLE)
    assert len(segments_by_stability(STABLE)) + len(
        segments_by_stability(PER_BEAD)
    ) == len(INJECTED_CONTEXT)


def test_inventory_names_no_judge_segment():
    entry_points = {segment.entry_point for segment in INJECTED_CONTEXT}
    assert not any(point.startswith("ortus.core.judge") for point in entry_points)
    assert PER_BEAD_MARKERS == ()

    per_bead = {segment.entry_point for segment in segments_by_stability(PER_BEAD)}
    assert "ortus.core.grind_loop:format_issue_details" in per_bead


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_composed_prompt_is_identical_across_two_issue_ids(backend):
    first = _compose("ortus-aaaa", backend=backend)
    second = _compose("ortus-zzzz", backend=backend)

    assert first == second
    assert stable_prefix_of(first) == first
    assert _IMPLEMENTATION_INSTRUCTION in first
    assert "## CodeGraph phase contract v1" in first
    assert "## Prior lessons" in first


def test_pin_directive_is_reserved_under_the_claude_cap():
    from ortus.core.pin_skew import PIN_DIRECTIVE_HEADER, pin_directive_section

    prompt = _compose_work_prompt(
        "",
        PACKET,
        "claude",
        phase_instruction=_IMPLEMENTATION_INSTRUCTION,
        phase_contract_text=CONTRACT,
        lessons_text="x" * 4000,
        pin_text=pin_directive_section(""),
    )

    assert PIN_DIRECTIVE_HEADER.strip() in prompt
    assert "x" * 4000 not in prompt
    assert len(prompt.removeprefix("/goal ")) <= 4000


def test_stable_prefix_flag_ships_the_adopted_arm_and_reads_the_environment():
    """The adopted verdict is the shipped default; an export still wins over it."""
    from ortus.core.config import DEFAULTS

    assert DEFAULTS[STABLE_PREFIX_CONFIG_KEY] is MEASURED_VERDICT
    # A caller holding no resolved configuration at all still composes the
    # legacy ordering: no config is not the same fact as a config carrying the
    # adopted default.
    assert stable_prefix_enabled(None, environ={}) is False
    assert stable_prefix_enabled(None, environ={STABLE_PREFIX_ENV: "1"}) is True
    assert stable_prefix_enabled(None, environ={STABLE_PREFIX_ENV: "off"}) is False
    assert stable_prefix_note(None, environ={}) == "legacy"
    assert stable_prefix_note(None, environ={STABLE_PREFIX_ENV: "1"}) == (
        f"stable from {STABLE_PREFIX_ENV}"
    )


def test_an_untouched_ortusrc_resolves_to_the_adopted_ordering(tmp_path):
    """The default reaches a run through a plain config load, named as itself."""
    from ortus.core.config import load_config

    config = load_config(repo=tmp_path, home=tmp_path / "home")

    assert config.get(STABLE_PREFIX_CONFIG_KEY) is MEASURED_VERDICT
    assert stable_prefix_enabled(config, environ={}) is MEASURED_VERDICT
    # The arm alone, because no `.ortusrc` layer carries the key. A log line
    # crediting a pin that does not exist misreports which arm a run was.
    assert stable_prefix_note(config, environ={}) == "stable"
    assert stable_prefix_note(config, environ={STABLE_PREFIX_ENV: "0"}) == (
        f"legacy from {STABLE_PREFIX_ENV}"
    )


def test_a_pinned_ordering_is_credited_to_the_project_layer(tmp_path):
    """A project file that pins the key is named, and the export still beats it."""
    from ortus.core.config import load_config

    (tmp_path / ".ortusrc").write_text(
        f"{STABLE_PREFIX_CONFIG_KEY} = false\n", encoding="utf-8"
    )
    config = load_config(repo=tmp_path, home=tmp_path / "home")

    assert stable_prefix_enabled(config, environ={}) is False
    assert stable_prefix_note(config, environ={}) == "legacy from .ortusrc"
    assert stable_prefix_note(config, environ={STABLE_PREFIX_ENV: "1"}) == (
        f"stable from {STABLE_PREFIX_ENV}, .ortusrc pins legacy"
    )


def test_slim_render_names_the_packet_fields_without_inlining_them():
    slim = slim_issue_details(PACKET)

    assert "Id: ortus-aaaa" in slim
    assert "Title: Repair the rollup reader" in slim
    assert "Labels: harness-efficiency" in slim
    for body in (DESCRIPTION_BODY, DESIGN_BODY, ACCEPTANCE_BODY):
        assert body not in slim
    for heading in ("Description", "Design", "Acceptance criteria"):
        assert heading in slim
    assert "bd show" in slim
    assert "Notes" not in slim


def test_slim_injection_keeps_the_id_when_the_packet_is_large():
    from ortus.core.grind_loop import inject_issue, read_work_issue_condition

    huge = {**PACKET, "description": DESCRIPTION_BODY + " padding " * 5000}
    template = read_work_issue_condition()

    full = inject_issue(template, huge)
    slim = inject_issue(template, huge, slim=True)

    assert huge["id"] in full
    assert huge["id"] in slim
    assert DESIGN_BODY in full
    assert DESIGN_BODY not in slim
    assert len(slim) < len(full)


def test_composed_worker_prompt_is_slim():
    prompt = _compose("ortus-aaaa")

    for body in (DESCRIPTION_BODY, DESIGN_BODY, ACCEPTANCE_BODY):
        assert body not in prompt
    assert "Repair the rollup reader" not in prompt
    assert "ortus-aaaa" not in prompt


def test_telemetry_contract_names_the_cache_fields_that_prove_the_win():
    from ortus.core.cost import UsageBuckets

    assert CACHE_TELEMETRY_FIELDS
    buckets = UsageBuckets(
        uncached_input_tokens=250,
        cached_input_tokens=750,
        cache_write_tokens=0,
    )
    reported = buckets.as_dict()

    for field in CACHE_TELEMETRY_FIELDS:
        assert hasattr(buckets, field), field
        assert field in reported, field

    assert buckets.input_tokens == 1000
    assert buckets.cache_hit_rate == 0.75
