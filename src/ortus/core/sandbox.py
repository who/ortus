"""Sandbox precondition checks (ported from ortus/lib/sandbox.sh).

Tier 1 — native OS sandbox (bwrap on Linux/WSL2, sandbox-exec on macOS).
Tier 2 — `docker sandbox` subcommand (--docker mode).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


class SandboxUnavailable(RuntimeError):
    """A required sandbox prerequisite is missing."""


@dataclass(frozen=True)
class SandboxInfo:
    platform: str
    binary: str


def smoke_test() -> SandboxInfo:
    """Verify the native sandbox prerequisite is present.

    Returns the resolved SandboxInfo on success; raises SandboxUnavailable
    otherwise. Intentionally not skippable — by design the orchestrator
    must refuse to launch on hosts that lack OS-level isolation.
    """
    system = platform.system()
    if system == "Linux":
        if shutil.which("bwrap") is None:
            raise SandboxUnavailable(
                "Sandbox prerequisite missing: bubblewrap (bwrap)\n"
                "  Install on Debian/Ubuntu/WSL2: sudo apt-get install bubblewrap socat\n"
                "  Note: WSL1 is unsupported (requires WSL2's Linux kernel)"
            )
        return SandboxInfo(platform=system, binary="bwrap")
    if system == "Darwin":
        if shutil.which("sandbox-exec") is None:
            raise SandboxUnavailable(
                "Sandbox prerequisite missing: Seatbelt (sandbox-exec)\n"
                "  Seatbelt is built into macOS; absence indicates a system-level issue"
            )
        return SandboxInfo(platform=system, binary="sandbox-exec")
    raise SandboxUnavailable(
        f"Unsupported platform '{system}' for native sandbox\n"
        f"  Supported: Linux/WSL2 (bubblewrap+socat), macOS (Seatbelt built-in)"
    )


def docker_precondition_check() -> SandboxInfo:
    """Verify `docker` + `docker sandbox` subcommand for --docker mode."""
    if shutil.which("docker") is None:
        system = platform.system()
        if system == "Darwin":
            hint = (
                "  Install Docker Desktop: https://www.docker.com/products/docker-desktop/\n"
                "  Or via Homebrew: brew install --cask docker"
            )
        elif system == "Linux":
            hint = "  Install Docker Engine: https://docs.docker.com/engine/install/"
        else:
            hint = "  Install Docker for your platform: https://docs.docker.com/get-docker/"
        raise SandboxUnavailable(
            "--docker requires Docker, but 'docker' was not found on PATH\n" + hint
        )
    result = subprocess.run(
        ["docker", "sandbox", "--help"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SandboxUnavailable(
            "--docker requires the bundled-image 'docker sandbox' subcommand, "
            "which is unavailable\n"
            "  Update Docker Desktop to a version with the bundled-image rollout\n"
            "  See: https://docs.docker.com/desktop/release-notes/"
        )
    return SandboxInfo(platform=platform.system(), binary="docker")
