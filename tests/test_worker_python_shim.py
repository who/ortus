"""The worker PATH gains a `python` shim only when the host has none."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ortus.core import worker_path


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repo, a shim root outside it, and a PATH holding only `python3`."""

    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    _exe(bindir / "python3")
    return repo, tmp_path / "shim-root", str(bindir)


def _first_python(path: str) -> Path:
    first = Path(path.split(os.pathsep)[0])
    return (first / "python").resolve()


def test_venv_python_wins_when_python_is_missing(host) -> None:
    repo, shim_root, path = host
    venv = _exe(repo / ".venv" / "bin" / "python")

    shimmed = worker_path.with_python_shim(repo, path, shim_root)

    assert shimmed.endswith(os.pathsep + path)
    assert _first_python(shimmed) == venv.resolve()


def test_dangling_venv_falls_back_to_python3(host) -> None:
    repo, shim_root, path = host
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(repo / "missing")

    shimmed = worker_path.with_python_shim(repo, path, shim_root)

    assert _first_python(shimmed) == (Path(path) / "python3").resolve()


def test_python3_is_the_target_without_a_venv(host) -> None:
    repo, shim_root, path = host

    shimmed = worker_path.with_python_shim(repo, path, shim_root)

    assert _first_python(shimmed) == (Path(path) / "python3").resolve()


def test_existing_python_leaves_path_unchanged(host) -> None:
    repo, shim_root, path = host
    _exe(Path(path) / "python")
    _exe(repo / ".venv" / "bin" / "python")

    assert worker_path.with_python_shim(repo, path, shim_root) == path
    assert not shim_root.exists()


def test_no_python3_and_no_venv_changes_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()

    assert worker_path.with_python_shim(repo, str(empty), tmp_path / "root") == str(
        empty
    )
    assert not (tmp_path / "root").exists()


def test_shim_is_written_outside_repo(host) -> None:
    repo, shim_root, path = host
    _exe(repo / ".venv" / "bin" / "python")
    before = sorted(p.relative_to(repo) for p in repo.rglob("*"))

    shimmed = worker_path.with_python_shim(repo, path, shim_root)

    shim_dir = Path(shimmed.split(os.pathsep)[0])
    assert shim_dir.is_relative_to(shim_root)
    assert not shim_dir.is_relative_to(repo)
    assert sorted(p.relative_to(repo) for p in repo.rglob("*")) == before


def test_grind_skips_shim_when_python_present(tmp_path: Path) -> None:
    from ortus.commands.grind import _install_python_shim

    bindir = tmp_path / "bin"
    _exe(bindir / "python")
    env = {"PATH": str(bindir)}
    lines: list[str] = []

    _install_python_shim(env, tmp_path, lines.append)

    assert env == {"PATH": str(bindir)}
    assert lines == []


@pytest.mark.integration
def test_grind_hands_workers_a_shimmed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_stealth_grind import _stealth_grind

    real_which = worker_path._which
    monkeypatch.setattr(
        worker_path,
        "_which",
        lambda name, path: None if name == "python" else real_which(name, path),
    )
    if real_which("python3", os.environ.get("PATH", "")) is None:
        pytest.skip("python3 not on PATH")

    _, _, worker, result = _stealth_grind(
        tmp_path, monkeypatch, scope="Edit `src/a.py`.", touch=False
    )

    assert worker.envs, result.output
    first = Path(worker.envs[0]["PATH"].split(os.pathsep)[0])
    assert first.parent.name.startswith("ortus-pyshim-")
    assert (first / "python").is_symlink()
