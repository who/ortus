"""Pure-logic helpers for `ortus grind` — condition builder + flock holder.

The IO orchestration lives in commands/grind.py; this module is what
the unit tests pin behavior on.
"""

from __future__ import annotations

import contextlib
import fcntl
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Iterator, Optional


CONDITION_CEILING = 4000
CONDITIONS_PACKAGE = "ortus.prompts.conditions"


class ConditionTooLong(ValueError):
    """The built condition exceeds the FR-004 4000-char ceiling."""


class CanonicalConditionMissing(FileNotFoundError):
    """The canonical condition file is absent or still a TODO placeholder."""


def _read_canonical() -> str:
    res = files(CONDITIONS_PACKAGE).joinpath("queue-zero.txt")
    if not res.is_file():
        raise CanonicalConditionMissing(
            f"canonical condition file missing in {CONDITIONS_PACKAGE}"
        )
    text = res.read_text()
    if text.lstrip().startswith("TODO PLACEHOLDER"):
        raise CanonicalConditionMissing("canonical condition is still a TODO placeholder")
    return text


@dataclass(frozen=True)
class BuiltCondition:
    text: str

    def __post_init__(self) -> None:
        if len(self.text) > CONDITION_CEILING:
            raise ConditionTooLong(
                f"built condition is {len(self.text)} chars; "
                f"FR-004 ceiling is {CONDITION_CEILING}"
            )


def build_condition(
    custom: Optional[str] = None,
    *,
    max_tasks: int = 0,
    max_iters: int = 0,
) -> BuiltCondition:
    """Compose the /goal condition.

    Mirrors goal.sh build_condition(): custom verbatim if supplied;
    otherwise canonical text with early-stop clauses trimmed per the
    presence/absence of max_tasks/max_iters and the <NTASKS>/<NITERS>
    placeholders substituted.
    """
    if custom:
        return BuiltCondition(custom)

    body = _read_canonical()
    has_tasks = max_tasks > 0
    has_iters = max_iters > 0

    if not has_tasks and not has_iters:
        # Drop the whole "You may stop early if EITHER" block and collapse
        # the now-adjacent blank lines.
        body = _drop_block(body)
    elif has_tasks and not has_iters:
        body = _drop_b_line(body)
    elif not has_tasks and has_iters:
        body = _drop_a_line(body)
    # Both: leave intact.

    body = body.replace("<NTASKS>", str(max_tasks))
    body = body.replace("<NITERS>", str(max_iters))
    return BuiltCondition(body)


_BLOCK_START = "You may stop early if EITHER:"
_BLOCK_END_PREFIX = "(b) you have used"


def _drop_block(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if not inside and line.strip() == _BLOCK_START:
            inside = True
            continue
        if inside:
            if line.startswith(_BLOCK_END_PREFIX):
                inside = False  # line itself dropped
                continue
            continue
        out.append(line)
    # Collapse runs of blank lines.
    collapsed: list[str] = []
    prev_blank = False
    for line in out:
        if not line.strip():
            if prev_blank:
                continue
            prev_blank = True
        else:
            prev_blank = False
        collapsed.append(line)
    return "\n".join(collapsed).rstrip("\n") + "\n"


def _drop_b_line(text: str) -> str:
    """Remove the (b) line; rewrite (a)'s trailing ', OR' → '.'."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith(_BLOCK_END_PREFIX):
            continue
        if "issues in this session" in line and line.rstrip().endswith(", OR"):
            line = line[: line.rfind(", OR")] + "."
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _drop_a_line(text: str) -> str:
    """Remove the (a) line; (b) is already terminated with a period."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("(a) you have closed") and "OR" in line:
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# --- flock --------------------------------------------------------------


class FlockBusy(RuntimeError):
    """Another grind holds the flock on this repo's .beads/ortus.flock."""


@contextlib.contextmanager
def grind_flock(repo: Path) -> Iterator[Path]:
    """Acquire an exclusive non-blocking flock at <repo>/.beads/ortus.flock.

    Raises FlockBusy immediately if another process holds it (mirrors
    goal.sh's --nb behavior). Releases on context exit.
    """
    lockfile = repo / ".beads" / "ortus.flock"
    lockfile.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lockfile, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise FlockBusy(f"another grind holds {lockfile}") from exc
    try:
        yield lockfile
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
