"""Tests for core/sandbox.py — smoke_test + docker check (q075.3 acceptance #5)."""

from __future__ import annotations

import platform as _platform
import shutil

import pytest

from ortus.core import sandbox
from ortus.core.sandbox import SandboxUnavailable


def test_smoke_test_skips_on_runners_without_bwrap() -> None:
    """Per acceptance: skips via pytest.skip on a runner without the native binary."""
    system = _platform.system()
    if system == "Linux" and shutil.which("bwrap") is None:
        pytest.skip("bwrap not available on this runner; native sandbox absent")
    if system == "Darwin" and shutil.which("sandbox-exec") is None:
        pytest.skip("sandbox-exec not available on this runner")
    if system not in ("Linux", "Darwin"):
        pytest.skip(f"native sandbox unsupported on {system}")
    info = sandbox.smoke_test()
    assert info.platform == system
    assert info.binary in {"bwrap", "sandbox-exec"}


def test_smoke_test_raises_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Windows")
    with pytest.raises(SandboxUnavailable, match="Unsupported platform"):
        sandbox.smoke_test()


def test_smoke_test_linux_missing_bwrap_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: None)
    with pytest.raises(SandboxUnavailable, match="bubblewrap"):
        sandbox.smoke_test()


def test_smoke_test_darwin_missing_sandbox_exec_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: None)
    with pytest.raises(SandboxUnavailable, match="Seatbelt"):
        sandbox.smoke_test()


def test_smoke_test_darwin_success_returns_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: "/usr/bin/sandbox-exec")
    info = sandbox.smoke_test()
    assert info.platform == "Darwin"
    assert info.binary == "sandbox-exec"


def test_smoke_test_linux_success_returns_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: "/usr/bin/bwrap")
    info = sandbox.smoke_test()
    assert info.platform == "Linux"
    assert info.binary == "bwrap"


def test_docker_check_missing_docker_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: None)
    with pytest.raises(SandboxUnavailable, match="Docker"):
        sandbox.docker_precondition_check()


def test_docker_check_missing_docker_hint_varies_by_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: None)
    for system, needle in [
        ("Darwin", "Docker Desktop"),
        ("Linux", "Docker Engine"),
        ("FreeBSD", "your platform"),
    ]:
        monkeypatch.setattr(sandbox.platform, "system", lambda s=system: s)
        with pytest.raises(SandboxUnavailable, match=needle):
            sandbox.docker_precondition_check()


def test_docker_check_missing_sandbox_subcommand_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: "/fake/docker")

    class _FakeCP:
        returncode = 1

    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: _FakeCP())
    with pytest.raises(SandboxUnavailable, match="bundled-image"):
        sandbox.docker_precondition_check()


def test_docker_check_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda binary: "/fake/docker")

    class _FakeCP:
        returncode = 0

    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: _FakeCP())
    info = sandbox.docker_precondition_check()
    assert info.binary == "docker"
