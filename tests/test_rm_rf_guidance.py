"""The goal prompt explains the `rm -rf` deny whenever the template carries it.

Claude Code checks every subcommand of a compound Bash call against the
deny list, so a staging chain with one `rm -rf` in it is refused whole. The
settings template rendered by `ortus init` keeps that deny for safety; the
worker prompt has to say so up front, or each worker rediscovers it by
losing turns to denied chains.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ortus.core.prompt_audit import audited_goal_text
from ortus.core.prompts import bundled_prompt_text

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "ortus"
    / "templates"
    / ".claude"
    / "settings.json.jinja"
)
_RM_RF_DENY = "Bash(rm -rf *)"


def _deny_rules() -> list[str]:
    text = _TEMPLATE.read_text(encoding="utf-8")
    try:
        return list(json.loads(text)["permissions"]["deny"])
    except (ValueError, KeyError):
        # A template with Jinja logic in it is not plain JSON; fall back to
        # a literal scan of the quoted rule.
        return [_RM_RF_DENY] if f'"{_RM_RF_DENY}"' in text else []


def test_settings_template_still_denies_rm_rf() -> None:
    assert _RM_RF_DENY in _deny_rules()


@pytest.mark.parametrize(
    ("arm", "text"),
    [
        ("legacy", bundled_prompt_text("goal-prompt")),
        ("audited", audited_goal_text()),
    ],
)
def test_goal_prompt_carries_rm_rf_guidance_while_denied(arm: str, text: str) -> None:
    if _RM_RF_DENY not in _deny_rules():
        pytest.skip("template no longer denies rm -rf; guidance not required")
    assert "`rm -rf` is denied" in text, arm
    assert "`&&`" in text and "`;` chain" in text, arm
    assert "mktemp -d" in text, arm
    assert "`mv`" in text, arm
