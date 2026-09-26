"""Which programs an issue's criterion checks start, and which PATH lacks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ortus.core.checks import check_programs, missing_check_programs

pytestmark = pytest.mark.fast


def _bin_dir(tmp_path: Path, *programs: str) -> str:
    """A directory holding one executable stub per named program."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for program in programs:
        stub = bin_dir / program
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    return str(bin_dir)


def _criteria(*commands: str) -> str:
    lines = "\n".join(
        f"- AC-{index}: Run `{command}`."
        for index, command in enumerate(commands, start=1)
    )
    return f"## Criterion checks\n{lines}\n"


@pytest.mark.parametrize(
    ("command", "programs"),
    [
        ("FOO=1 node x.js", ["node"]),
        ("A=1 B=2 make build", ["make"]),
        ("cd sub && npm test", ["npm"]),
        ('node -e "a; b" | tee out', ["node", "tee"]),
        ("make lint; go test ./... || cargo test", ["make", "go", "cargo"]),
        ("./gradlew test", []),
        ("bin/tconv.js --help", []),
        ("/usr/bin/env true", []),
        ("test -f out && echo ok", []),
        ('echo "unbalanced', []),
    ],
)
def test_check_programs_edge_cases(command: str, programs: list[str]) -> None:
    assert check_programs(command) == programs


def test_missing_check_programs_names_only_unresolved(tmp_path: Path) -> None:
    path = _bin_dir(tmp_path, "uv")
    criteria = _criteria(
        "uv run pytest tests/test_a.py -q",
        "node bin/tconv.js 32 && FOO=1 npm test",
        "./gradlew test",
    )

    assert missing_check_programs(criteria, path=path) == ["node", "npm"]


def test_missing_check_programs_all_resolved(tmp_path: Path) -> None:
    path = _bin_dir(tmp_path, "uv", "node", "npm")
    criteria = _criteria("uv run pytest -q", "node x.js | npm test")

    assert missing_check_programs(criteria, path=path) == []


def test_missing_check_programs_empty_path_reports_every_program() -> None:
    criteria = _criteria("uv run pytest -q", "npm run build && cd sub && make")

    assert missing_check_programs(criteria, path="") == ["make", "npm", "uv"]


def test_missing_check_programs_skips_unparseable_criteria() -> None:
    assert missing_check_programs("", path="") == []
    assert missing_check_programs(
        "## Observable criteria\n- AC-1: a human reads the page.\n", path=""
    ) == []
    assert missing_check_programs(
        _criteria('uv run pytest "unbalanced'), path=""
    ) == []


def test_missing_check_programs_uses_given_path_not_process_path(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert os.environ.get("PATH")

    assert missing_check_programs(
        _criteria("uv run pytest -q"), path=str(empty)
    ) == ["uv"]
