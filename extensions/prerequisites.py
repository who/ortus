"""Prerequisite checks extension for Copier templates.

Runs prerequisite checks during template generation and reports results.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from copier_templates_extensions import ContextHook

if TYPE_CHECKING:
    from jinja2 import Environment


# Track if checks have already run to avoid duplicate output
_checks_run = False


class PrerequisiteChecker(ContextHook):
    """Checks for required tools during template generation.

    Runs checks when the Jinja environment is initialized (before template
    rendering and prompts) so users see prerequisite status early.
    """

    # ANSI colors
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    NC = "\033[0m"  # No Color

    # Tools to check: (command, description, install_hint)
    TOOLS = [
        ("git", "Version control", "https://git-scm.com/downloads"),
        ("jq", "JSON processing", "brew install jq / apt install jq"),
        (
            "bd",
            "Beads issue tracking",
            "curl -sSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash",
        ),
        (
            "claude",
            "Claude CLI for automation",
            "npm install -g @anthropic-ai/claude-code",
        ),
        ("rg", "Fast search (ripgrep)", "brew install ripgrep / apt install ripgrep"),
        ("fd", "Fast file finder", "brew install fd / apt install fd-find"),
    ]

    def __init__(self, environment: Environment) -> None:
        """Initialize and run prerequisite checks immediately."""
        super().__init__(environment)
        global _checks_run
        if not _checks_run:
            self._run_checks()
            _checks_run = True

    def hook(self, context: dict) -> dict:
        """Return empty context (no variables added). Checks run in __init__."""
        return {}

    def _run_checks(self) -> None:
        """Run all prerequisite checks and print results."""
        print("\nChecking prerequisites...", flush=True)
        print(flush=True)

        missing_count = 0

        for tool, description, install_hint in self.TOOLS:
            if self._check_tool(tool):
                self._print_found(tool, description)
            else:
                self._print_missing(tool, description, install_hint)
                missing_count += 1

        print(flush=True)

        if missing_count == 0:
            print(f"{self.GREEN}All prerequisites installed!{self.NC}", flush=True)
        else:
            print(f"{self.YELLOW}Missing {missing_count} tool(s).{self.NC}", flush=True)
            print(
                "The project will work, but some automation features may not function until these are installed.",
                flush=True
            )
        print(flush=True)

    def _check_tool(self, tool: str) -> bool:
        """Check if a tool is available in PATH."""
        return shutil.which(tool) is not None

    def _print_found(self, tool: str, description: str) -> None:
        """Print a found tool message."""
        print(f"{self.GREEN}✓{self.NC} {tool:<10} {description}", flush=True)

    def _print_missing(self, tool: str, description: str, install_hint: str) -> None:
        """Print a missing tool message with install hint."""
        print(f"{self.RED}✗{self.NC} {tool:<10} {description}", flush=True)
        print(f"  {self.YELLOW}Install:{self.NC} {install_hint}", flush=True)
