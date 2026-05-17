"""disableAllHooks precheck (ported from ortus/goal.sh:118-160).

`/goal` is implemented as a managed Stop hook. If any Claude Code settings
layer sets `disableAllHooks=true`, the /goal directive silently degrades
into a hookless `claude -p` run with no termination contract. Detect and
refuse to launch.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path


class HookConflictError(RuntimeError):
    """disableAllHooks=true is set in a settings layer that would suppress /goal."""

    def __init__(self, settings_path: Path):
        self.settings_path = settings_path
        super().__init__(
            f"disableAllHooks=true in {settings_path}\n"
            f"  /goal is implemented as a managed Stop hook and requires hooks to be enabled.\n"
            f"  With hooks disabled, /goal silently does nothing — the session would run as a\n"
            f"  normal claude -p invocation with no termination contract.\n"
            f"\n"
            f"  To fix: remove or set disableAllHooks=false in {settings_path}.\n"
            f"\n"
            f"  Docs: https://code.claude.com/docs/en/goal"
        )


@dataclass(frozen=True)
class HookCheckResult:
    """Outcome of check_hooks_enabled(). `checked_paths` is the audit trail."""

    checked_paths: tuple[Path, ...]


def _managed_settings_path() -> Path | None:
    system = platform.system()
    if system == "Linux":
        return Path("/etc/claude/managed-settings.json")
    if system == "Darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return None


def _candidate_layers(repo: Path, home: Path) -> list[Path]:
    layers = [
        home / ".claude" / "settings.json",
        repo / ".claude" / "settings.json",
    ]
    managed = _managed_settings_path()
    if managed is not None:
        layers.append(managed)
    return layers


def check_hooks_enabled(repo: Path, *, home: Path | None = None) -> HookCheckResult:
    """Raise HookConflictError if any layer sets disableAllHooks=true."""
    if home is None:
        home = Path.home()
    checked: list[Path] = []
    for layer in _candidate_layers(repo, home):
        if not layer.is_file():
            continue
        checked.append(layer)
        try:
            data = json.loads(layer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Mirrors goal.sh's silent-on-parse-fail behavior; we can't
            # block grind on a layer we can't parse.
            continue
        if data.get("disableAllHooks") is True:
            raise HookConflictError(layer)
    return HookCheckResult(checked_paths=tuple(checked))
