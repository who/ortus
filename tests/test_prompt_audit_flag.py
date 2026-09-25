"""The `prompt_audit` flag and the audited worker-prompt variant (ortus-xfw1).

Two worker-facing texts ship twice: the goal-prompt loop a worker fetches with
`ortus prompt show goal`, and the per-iteration work-issue condition. The flag
ships on, the arm its comparison adopted, and off still serves the legacy
bundles the comparison ran against; these tests hold the selection, the
audited text's content rules, and the enforcement behind every rule the audit
moved out of prompt text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ortus.cli import app
from ortus.core.config import load_config
from ortus.core.grind_loop import inject_issue, read_work_issue_condition
from ortus.core.profiles import ProfileError
from ortus.core.prompt_audit import (
    AUDIT_ENV,
    MOVED_RULES,
    audit_enabled,
    audit_note,
    audited_goal_text,
    emphasis_tokens_in,
    resolve_enforcement,
    token_conservation_phrases_in,
    unenforced_moved_rules,
)
from ortus.core.prompts import (
    PROMPT_REGISTRY,
    bundled_prompt_text,
    prompts_in_package,
    resolve_named_prompt,
)

runner = CliRunner()

#: The verdict recorded on this flag's measurement issue. `DEFAULTS` follows
#: this constant, never the other way round. It is True because the
#: hello-world comparison was run from a terminal and adopted: the audited arm
#: held the control's close rate of 1.0 and finished in 684 seconds against
#: 1030, which the operator took over the 2.0027 against 1.6794 it spent per
#: closed bead. Moving it again means running the arms again, because this is
#: a reading and not a preference.
MEASURED_VERDICT = True


@pytest.fixture(autouse=True)
def _no_exported_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own export must not decide which arm a test measures."""
    monkeypatch.delenv(AUDIT_ENV, raising=False)


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")


def _repo(tmp_path: Path, ortusrc: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    if ortusrc:
        (repo / ".ortusrc").write_text(ortusrc, encoding="utf-8")
    return repo


def _audited_texts() -> tuple[tuple[str, str], ...]:
    """Both audited worker-facing texts, named for an assertion message."""
    return (
        ("audited goal prompt", audited_goal_text()),
        ("audited work-issue condition", read_work_issue_condition(audited=True)),
    )


# --- selection ---------------------------------------------------------------


def test_the_shipped_default_is_the_arm_the_comparison_adopted(
    tmp_path: Path,
) -> None:
    """AC-2: an untouched repository resolves the recorded verdict, named as itself."""
    from ortus.core.config import DEFAULTS

    config = load_config(repo=_repo(tmp_path), home=tmp_path / "home")
    assert DEFAULTS["prompt_audit"] is MEASURED_VERDICT
    assert config.get("prompt_audit") is MEASURED_VERDICT
    assert audit_enabled(config, environ={}) is MEASURED_VERDICT
    # The arm alone, because no `.ortusrc` layer carries the key. Crediting a
    # pin that does not exist misreports where a run's arm came from.
    assert audit_note(config, environ={}) == "audited"
    # A caller holding no resolved configuration still serves the legacy text:
    # no config is not the same fact as a config carrying the adopted default.
    assert audit_enabled(None, environ={}) is False


def test_pinning_the_key_off_restores_the_legacy_bundles(tmp_path: Path) -> None:
    """Off is the kill switch and the control arm at once, credited to its layer."""
    repo = _repo(tmp_path, "prompt_audit = false\n")
    config = load_config(repo=repo, home=tmp_path / "home")
    assert audit_enabled(config, environ={}) is False
    assert audit_note(config, environ={}) == "legacy from .ortusrc"
    assert read_work_issue_condition() != read_work_issue_condition(audited=True)
    resolved = resolve_named_prompt("goal", repo=repo, home=tmp_path / "home")
    assert resolved.source == "bundled"
    assert resolved.text == bundled_prompt_text("goal-prompt")


def test_ortusrc_key_selects_the_audited_variant(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "prompt_audit = true\n")
    config = load_config(repo=repo, home=tmp_path / "home")
    assert audit_enabled(config, environ={}) is True
    assert audit_note(config, environ={}) == "audited from .ortusrc"
    resolved = resolve_named_prompt(
        "goal", repo=repo, home=tmp_path / "home", audited=True
    )
    assert resolved.source == "audited"
    assert resolved.text == audited_goal_text()
    assert resolved.text != bundled_prompt_text("goal-prompt")


def test_exported_flag_overrides_the_pinned_key(tmp_path: Path) -> None:
    """One A/B run flips the arm without editing a tracked file."""
    pinned = load_config(repo=_repo(tmp_path, "prompt_audit = true\n"), home=tmp_path)
    assert audit_enabled(pinned, environ={AUDIT_ENV: "0"}) is False
    assert "legacy from ORTUS_PROMPT_AUDIT" in audit_note(
        pinned, environ={AUDIT_ENV: "0"}
    )
    assert ".ortusrc pins audited" in audit_note(pinned, environ={AUDIT_ENV: "0"})
    unpinned = load_config(repo=_repo(tmp_path / "other"), home=tmp_path)
    assert audit_enabled(unpinned, environ={AUDIT_ENV: "1"}) is True
    assert audit_note(unpinned, environ={AUDIT_ENV: "1"}) == (
        f"audited from {AUDIT_ENV}"
    )


def test_an_unparsable_export_resolves_the_layer_below_it(tmp_path: Path) -> None:
    """A value that parses as neither arm is no instruction, not a vote for one."""
    default = load_config(repo=_repo(tmp_path), home=tmp_path)
    assert audit_enabled(default, environ={AUDIT_ENV: "maybe"}) is MEASURED_VERDICT
    pinned = load_config(
        repo=_repo(tmp_path / "off", "prompt_audit = false\n"), home=tmp_path
    )
    assert audit_enabled(pinned, environ={AUDIT_ENV: "maybe"}) is False


def test_a_non_boolean_key_fails_config_load(tmp_path: Path) -> None:
    repo = _repo(tmp_path, 'prompt_audit = "yes"\n')
    with pytest.raises(ProfileError, match="prompt_audit"):
        load_config(repo=repo, home=tmp_path / "home")


def test_an_override_layer_still_wins_over_the_audited_bundle(
    tmp_path: Path,
) -> None:
    """The flag selects a bundled default; it never shadows an operator copy."""
    repo = _repo(tmp_path)
    override = repo / ".ortus" / "prompts" / "goal-prompt.md"
    override.parent.mkdir(parents=True)
    override.write_text("OPERATOR-OWNED LOOP", encoding="utf-8")
    resolved = resolve_named_prompt(
        "goal", repo=repo, home=tmp_path / "home", audited=True
    )
    assert resolved.source == "repo"
    assert resolved.text == "OPERATOR-OWNED LOOP"


# --- what the flag serves to a worker ---------------------------------------


def test_prompt_show_serves_the_variant_the_repo_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker fetches its loop with this command, so it decides the arm."""
    _isolate_home(monkeypatch, tmp_path)
    repo = _repo(tmp_path, "prompt_audit = true\n")
    shown = runner.invoke(app, ["prompt", "show", "goal", str(repo)])
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    assert shown.stdout == audited_goal_text()
    assert "bundled (audited)" in shown.stderr
    legacy_repo = _repo(tmp_path / "legacy", "prompt_audit = false\n")
    legacy = runner.invoke(app, ["prompt", "show", "goal", str(legacy_repo)])
    assert legacy.exit_code == 0, legacy.stdout + legacy.stderr
    assert legacy.stdout == bundled_prompt_text("goal-prompt")
    assert "bundled (default)" in legacy.stderr


def test_prompt_list_names_every_registered_prompt_under_either_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1: the audit changes text, not the registry `ortus prompt list` reads."""
    _isolate_home(monkeypatch, tmp_path)
    arms = (
        ("legacy", "prompt_audit = false\n", "bundled (default)"),
        ("audited", "prompt_audit = true\n", None),
    )
    for arm, ortusrc, label in arms:
        repo = _repo(tmp_path / f"repo-{arm}", ortusrc)
        result = runner.invoke(app, ["prompt", "list", str(repo)])
        assert result.exit_code == 0, result.stdout + result.stderr
        for entry in PROMPT_REGISTRY:
            line = next(
                row
                for row in result.stdout.splitlines()
                if row.split()[0] == entry.name
            )
            assert entry.phase in line
            if label is not None:
                assert label in line
        # Only the worker-facing goal prompt has an audited variant; the two
        # planner-facing prompts keep resolving to their single bundle.
        if arm == "audited":
            goal_line = next(
                row for row in result.stdout.splitlines() if row.split()[0] == "goal"
            )
            assert "bundled (audited)" in goal_line
            plan_line = next(
                row for row in result.stdout.splitlines() if row.split()[0] == "plan"
            )
            assert "bundled (default)" in plan_line


def test_the_audited_variant_is_not_a_registry_entry() -> None:
    """The variant lives in a subpackage, so no prompt ships unnamed."""
    assert sorted(entry.filename for entry in PROMPT_REGISTRY) == sorted(
        prompts_in_package()
    )


def test_grind_still_injects_the_issue_id_into_the_audited_condition() -> None:
    """Injection is the wedge against a hallucinated id; both arms carry it."""
    issue = {
        "id": "ortus-abcd",
        "title": "Audited injection",
        "issue_type": "task",
        "description": "body text",
    }
    template = read_work_issue_condition(audited=True)
    assert "<ISSUE_ID>" in template
    assert "<ISSUE_DETAILS>" in template
    injected = inject_issue(template, issue)
    assert "<ISSUE_ID>" not in injected
    assert "<ISSUE_DETAILS>" not in injected
    assert injected.count("ortus-abcd") >= 3
    assert "Title: Audited injection" in injected
    assert "body text" in injected


def test_the_audited_goal_prompt_supports_the_bound_issue_contract() -> None:
    """Judge binding validates the served goal text before it claims."""
    from ortus.commands.grind import _IMPLEMENTATION_INSTRUCTION, _compose_work_prompt
    from ortus.core.judge_claim import validate_bound_goal

    validate_bound_goal(audited_goal_text())
    prompt = _compose_work_prompt(
        "",
        {
            "id": "ortus-abcd",
            "status": "in_progress",
            "assignee": "run-1",
            "labels": [],
        },
        "claude",
        bound_issue_id="ortus-abcd",
        goal_template=audited_goal_text(),
        phase_instruction=_IMPLEMENTATION_INSTRUCTION,
    )
    assert "Never run bd ready or claim another id" in prompt


def test_grind_dry_run_names_the_arm_it_would_serve(tmp_path: Path) -> None:
    """A run log or preview that cannot say which arm it is has no A/B value."""
    from tests.test_grind import _fixture_repo, _plain

    repo = _fixture_repo(tmp_path)
    shipped = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert shipped.exit_code == 0, shipped.stdout
    assert "prompt text:    audited\n" in _plain(shipped.stdout)
    (repo / ".ortusrc").write_text("prompt_audit = false\n", encoding="utf-8")
    legacy = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert legacy.exit_code == 0, legacy.stdout
    assert "prompt text:    legacy from .ortusrc" in _plain(legacy.stdout)
    (repo / ".ortusrc").write_text("prompt_audit = true\n", encoding="utf-8")
    audited = runner.invoke(app, ["grind", str(repo), "--dry-run"])
    assert audited.exit_code == 0, audited.stdout
    assert "prompt text:    audited from .ortusrc" in _plain(audited.stdout)


# --- audited content --------------------------------------------------------


def test_audited_content_carries_no_shouted_emphasis() -> None:
    """The audit's premise: a capable model reads definitions, not shouting."""
    for name, text in _audited_texts():
        assert emphasis_tokens_in(text) == (), (
            f"{name} still shouts: {emphasis_tokens_in(text)}"
        )
    # The contrast is real: the legacy condition this replaced shouts.
    assert emphasis_tokens_in(read_work_issue_condition())


def test_audited_content_has_no_token_conservation_advice() -> None:
    """Asking for brevity would confound the size A/B this variant exists for."""
    for name, text in _audited_texts():
        assert token_conservation_phrases_in(text) == (), (
            f"{name} asks the worker to spend fewer tokens: "
            f"{token_conservation_phrases_in(text)}"
        )


def test_audited_content_keeps_the_commit_message_rules() -> None:
    """The gate still enforces them, so the contract still states them."""
    from tests.test_grind_prompt_content import _MESSAGE_RULE_PHRASES

    body = read_work_issue_condition(audited=True)
    for reason, phrase in _MESSAGE_RULE_PHRASES.items():
        assert phrase in body, (
            f"audited work-issue condition does not state the rule behind the "
            f"rejection {reason!r} (expected the phrase {phrase!r})"
        )
    assert "72" in body
    assert "repaired in place" in body


def test_audited_content_keeps_the_definitions_behind_known_quirks() -> None:
    """Deletion stops where a line fixes an observed failure.

    Each phrase here stands for a failure the legacy text was written after:
    leaked worktree registrations, a packet edited mid-claim, a claim reported
    without measurements, an unrunnable criterion check improvised around, and
    a window that answered while a check was still running.
    """
    condition = read_work_issue_condition(audited=True)
    for phrase in (
        "`git worktree add`",
        "git archive",
        "git clone --shared",
        "**Claims v1**",
        "--acceptance",
        "PLAN-GAP",
        "bd human <ISSUE_ID>",
    ):
        assert phrase in condition, f"audited condition dropped {phrase!r}"
    goal = audited_goal_text()
    for phrase in (
        "labeled `human`",
        "PLAN-GAP",
        "pin-able",
        "finishes",
        "`bd memories <keyword>`",
        "session-close",
    ):
        assert phrase in goal, f"audited goal prompt dropped {phrase!r}"


def test_audited_content_states_the_loop_as_end_states() -> None:
    """Every step of the legacy loop survives the rewrite."""
    goal = audited_goal_text()
    for step in ("1. **Orient.**", "2. **Continue or select.**",
                 "3. **Investigate and implement**", "4. **Session-close**",
                 "5. **Exit.**"):
        assert step in goal, f"audited goal prompt lost {step!r}"
    assert "HEAD is in sync with origin" in goal


# --- moved enforcement ------------------------------------------------------


def test_moved_enforcement_entry_points_resolve() -> None:
    """AC-5: a rule dropped from the prompt is held by something that runs."""
    assert MOVED_RULES
    assert unenforced_moved_rules() == ()
    for rule in MOVED_RULES:
        target = resolve_enforcement(rule)
        assert callable(target), f"{rule.entry_point} is not callable"
        assert rule.kind in {"hook", "check"}


def test_moved_enforcement_rules_left_the_audited_text() -> None:
    """Each moved rule's wording is in the legacy bundle and gone from the audit."""
    legacy = bundled_prompt_text("goal-prompt") + read_work_issue_condition()
    audited = audited_goal_text() + read_work_issue_condition(audited=True)
    for rule in MOVED_RULES:
        assert rule.legacy_phrase in legacy, (
            f"{rule.entry_point} claims to replace wording the legacy text "
            f"does not carry: {rule.legacy_phrase!r}"
        )
        assert rule.legacy_phrase not in audited, (
            f"audited text still carries the moved rule {rule.legacy_phrase!r}"
        )


def test_moved_enforcement_missing_entry_point_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished enforcement owner surfaces instead of passing silently."""
    import ortus.core.prompt_audit as prompt_audit

    broken = prompt_audit.MovedRule(
        rule="gone",
        legacy_phrase="gone",
        entry_point="ortus.core.checks:no_such_function",
        kind="check",
    )
    monkeypatch.setattr(prompt_audit, "MOVED_RULES", (broken,))
    assert prompt_audit.unenforced_moved_rules() == (broken.entry_point,)
    with pytest.raises(LookupError):
        prompt_audit.resolve_enforcement(broken)


def test_moved_enforcement_row_reports_variant_and_owners(tmp_path: Path) -> None:
    """`ortus check` states the arm and the entry points holding the audit."""
    from ortus.commands.check import check_worker_prompt

    repo = _repo(tmp_path, "prompt_audit = true\n")
    row = check_worker_prompt(repo)
    assert row.ok
    assert "audited from .ortusrc" in row.message
    for rule in MOVED_RULES:
        assert rule.entry_point in row.message


def test_moved_enforcement_row_fails_when_enforcement_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ortus.commands.check as check_mod

    monkeypatch.setattr(
        check_mod, "unenforced_moved_rules", lambda: ("ortus.core.checks:gone",)
    )
    row = check_mod.check_worker_prompt(_repo(tmp_path))
    assert not row.ok
    assert "no enforcement" in row.message
