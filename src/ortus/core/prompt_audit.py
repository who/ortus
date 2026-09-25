"""The audited worker-prompt variant and the flag that selects it.

`ortus grind` serves two worker-facing texts: the goal-prompt loop the worker
fetches with `ortus prompt show goal`, and the per-iteration work-issue
condition. Each ships twice — the legacy bundle, unchanged, and an audited
rewrite that states the same contract as definitions of the correct end state
instead of imperatives, and drops the rules Ortus already enforces in code.
This module resolves which variant a repository serves and records where the
dropped rules are enforced now.

The flag is `prompt_audit` in `.ortusrc`, default off, with the
`ORTUS_PROMPT_AUDIT` environment variable as a per-run override so an A/B run
needs no file edit. Both layers reach the worker: the key lives in the
repository the worker runs in, and the variable is inherited by the worker
subprocess, so the worker's own `ortus prompt show goal` resolves the same
variant as the harness that launched it.

Scope is those two texts. The `plan` and `interview` prompts are audited in
the issue record but ship once each: neither is composed into a grind
worker's prompt, so the flag has no A/B to serve for them.

Precedence is unchanged by the flag. A repo or user `.ortus/prompts/`
override still wins under either variant — the flag only selects which
bundled default it is compared against — and `ortus prompt eject` keeps
copying the legacy bundle, which is also the staleness baseline
`ortus check` reports against.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Mapping

from ortus.core.prompts import AUDITED_PROMPT_PACKAGE, bundled_prompt_text

#: `.ortusrc` key and environment variable that select the variant.
AUDIT_CONFIG_KEY = "prompt_audit"
AUDIT_ENV = "ORTUS_PROMPT_AUDIT"

#: The two worker-facing texts the flag gates.
GOAL_STEM = "goal-prompt"
WORK_ISSUE_FILE = "work-issue.txt"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off", ""})

#: Emphasis the audited text carries none of. Capitalised prose ("Never run
#: bd ready") is ordinary English; a shouted token is the emphasis the audit
#: converts into a definition, so only standalone upper-case forms count.
EMPHASIS_TOKENS: tuple[str, ...] = (
    "MUST",
    "NEVER",
    "ALWAYS",
    "IMPORTANT",
    "CRITICAL",
    "NOT",
    "ONLY",
    "ALREADY",
    "DO NOT",
)
_EMPHASIS_RE = re.compile(r"\b(" + "|".join(EMPHASIS_TOKENS) + r")\b")

#: Advice the audited text is forbidden to carry. A worker told to spend
#: fewer tokens spends them on deciding how, and the A/B this variant exists
#: for measures prompt size, so asking for brevity would confound it.
TOKEN_CONSERVATION_PHRASES: tuple[str, ...] = (
    "conserve tokens",
    "save tokens",
    "fewer tokens",
    "token budget",
    "minimize tokens",
    "minimise tokens",
    "be concise",
    "be brief",
    "keep it short",
    "as briefly as possible",
)


@dataclass(frozen=True)
class MovedRule:
    """One rule the audit moved out of prompt text into code.

    ``legacy_phrase`` is the wording the legacy bundle carries and the
    audited text does not; ``entry_point`` is the ``module:attribute`` that
    enforces the rule now, so a rule can never be dropped from a prompt
    without something still holding it.
    """

    rule: str
    legacy_phrase: str
    entry_point: str
    kind: str  # "hook" or "check"


#: Enforceable rules the audited text drops, each with the entry point that
#: holds it. Every phrase here is absent from the audited text and present in
#: the legacy bundle, and every entry point resolves — `check_worker_prompt`
#: and the flag's tests both hold that.
#: The wrapped-`bd` rule used to sit here, owned by
#: `ortus.core.judge_tools:inspect_tool`. That hook held it only as collateral
#: of refusing every compound shell line, and it no longer refuses those, so
#: the rule has no owner and the claim is gone rather than left standing false.
MOVED_RULES: tuple[MovedRule, ...] = (
    MovedRule(
        rule=(
            "a queue orchestrator started from inside a worker session cannot "
            "take the repository's grind lock"
        ),
        legacy_phrase=(
            "Do NOT invoke `ortus grind`, `goal.sh`, `ralph.sh`, or any other "
            "queue orchestrator from inside this session."
        ),
        entry_point="ortus.core.grind_logic:grind_flock",
        kind="check",
    ),
    MovedRule(
        rule=(
            "only the committed range is judged; the pipeline clones the "
            "branch and re-runs every criterion check on it"
        ),
        legacy_phrase=(
            "The machine verification pipeline judges your exact committed "
            "range next"
        ),
        entry_point="ortus.core.checks:run_checks",
        kind="check",
    ),
)


def _flag(value: object) -> bool | None:
    """A configured or exported flag value as a bool, or None when unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return None


def _pinned(config: Any | None) -> bool:
    """True when a `.ortusrc` layer sets the key, rather than the default."""
    return any(
        AUDIT_CONFIG_KEY in layer.data
        for layer in getattr(config, "layers", ())
        if layer.source != "defaults"
    )


def audit_enabled(
    config: Any | None = None, *, environ: Mapping[str, str] | None = None
) -> bool:
    """True when this repository serves the audited worker prompts.

    The environment wins over `.ortusrc` so one A/B run can flip the variant
    without touching a tracked file, and an unparsable value in either layer
    falls back to the legacy bundles rather than guessing at an opt-in.
    """
    env = os.environ if environ is None else environ
    exported = _flag(env.get(AUDIT_ENV))
    if exported is not None:
        return exported
    if config is None:
        return False
    return bool(_flag(config.get(AUDIT_CONFIG_KEY, False)))


def audit_note(
    config: Any | None = None, *, environ: Mapping[str, str] | None = None
) -> str:
    """The active variant with its provenance, for a log line or check row.

    Names the layer whenever it is not the default, so a run log reads which
    prompt text a worker was served and why — the A/B is worthless if the
    transcript cannot say which arm it belongs to.
    """
    env = os.environ if environ is None else environ
    exported = _flag(env.get(AUDIT_ENV))
    configured = bool(_flag(config.get(AUDIT_CONFIG_KEY, False))) if config else False
    if exported is None:
        return "audited from .ortusrc" if configured else "legacy"
    variant = "audited" if exported else "legacy"
    if exported == configured or not _pinned(config):
        return f"{variant} from {AUDIT_ENV}"
    pinned = "audited" if configured else "legacy"
    return f"{variant} from {AUDIT_ENV}, .ortusrc pins {pinned}"


def audited_goal_text() -> str:
    """The audited goal-prompt bundle, bypassing every override layer."""
    return bundled_prompt_text(GOAL_STEM, audited=True)


def resolve_enforcement(rule: MovedRule) -> object:
    """The live object a moved rule's entry point names.

    Raises ``LookupError`` when the module or attribute is gone: that is a
    moved rule enforced nowhere, which is worse than the prompt line the
    audit deleted.
    """
    module_name, _, attribute = rule.entry_point.partition(":")
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise LookupError(f"{rule.entry_point} is not importable: {exc}") from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise LookupError(f"{rule.entry_point} no longer exists") from exc


def unenforced_moved_rules() -> tuple[str, ...]:
    """Entry points of moved rules whose enforcement no longer resolves."""
    missing: list[str] = []
    for rule in MOVED_RULES:
        try:
            resolve_enforcement(rule)
        except LookupError:
            missing.append(rule.entry_point)
    return tuple(missing)


def emphasis_tokens_in(text: str) -> tuple[str, ...]:
    """Shouted emphasis tokens a text carries, in order of appearance."""
    return tuple(match.group(1) for match in _EMPHASIS_RE.finditer(text))


def token_conservation_phrases_in(text: str) -> tuple[str, ...]:
    """Token-conservation advice a text carries, if any."""
    lowered = text.lower()
    return tuple(
        phrase for phrase in TOKEN_CONSERVATION_PHRASES if phrase in lowered
    )


__all__ = [
    "AUDITED_PROMPT_PACKAGE",
    "AUDIT_CONFIG_KEY",
    "AUDIT_ENV",
    "EMPHASIS_TOKENS",
    "GOAL_STEM",
    "MOVED_RULES",
    "MovedRule",
    "TOKEN_CONSERVATION_PHRASES",
    "WORK_ISSUE_FILE",
    "audit_enabled",
    "audit_note",
    "audited_goal_text",
    "emphasis_tokens_in",
    "resolve_enforcement",
    "token_conservation_phrases_in",
    "unenforced_moved_rules",
]
