"""Git config context extension for Copier templates.

Provides default values for author_name and author_email from git config.
"""

import subprocess

from copier_templates_extensions import ContextHook


class GitConfigContext(ContextHook):
    """Adds git config values to template context for use as defaults."""

    def hook(self, context: dict) -> dict:
        """Fetch git config values and add them to context."""
        return {
            "git_user_name": self._get_git_config("user.name") or "Developer",
            "git_user_email": self._get_git_config("user.email") or "dev@example.com",
        }

    def _get_git_config(self, key: str) -> str | None:
        """Get a value from git config, returning None if not set."""
        try:
            result = subprocess.run(
                ["git", "config", "--global", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return None
