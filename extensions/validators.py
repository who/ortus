"""Answer validators for Copier templates.

Copier's built-in `validator:` only runs during interactive prompting, so
values passed via `--data` bypass it. This module re-checks enum-shaped
answers via a context hook and raises a clear error before any files are
rendered.
"""

from __future__ import annotations

from copier.errors import UserMessageError
from copier_template_extensions import ContextHook


class AnswersValidator(ContextHook):
    """Validates copier answers, surfacing clear errors on invalid values."""

    VALID_LANGUAGE_PROFILES = ("python", "javascript", "go", "rust", "polyglot")

    # Must stay in lockstep with BACKEND_CHOICES in ortus/lib/backend.sh — a
    # value copier accepts here has to be one resolve_backend() will accept at
    # runtime, or generation succeeds and every loop invocation then fails.
    VALID_AGENT_CLIS = ("claude", "codex")

    def hook(self, context: dict) -> dict:
        profile = context.get("language_profile")
        if profile is not None and profile not in self.VALID_LANGUAGE_PROFILES:
            valid = ", ".join(self.VALID_LANGUAGE_PROFILES)
            raise UserMessageError(
                f"language_profile must be one of: {valid}. Got: {profile!r}"
            )

        agent_cli = context.get("agent_cli")
        if agent_cli is not None and agent_cli not in self.VALID_AGENT_CLIS:
            valid = ", ".join(self.VALID_AGENT_CLIS)
            raise UserMessageError(
                f"agent_cli must be one of: {valid}. Got: {agent_cli!r}"
            )
        return {}
