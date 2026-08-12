#!/usr/bin/env python3
"""Refuse git-mutating Bash commands while an Ortus grind run holds the flock.

Wired as a Claude Code ``PreToolUse`` hook on the Bash tool. While ``ortus
grind`` is in flight the shared checkout sits on a worker's issue branch, so
an interactive ``git commit`` (or push/checkout/switch/reset/rebase/merge)
would land changes inside the live candidate. This guard blocks the call —
exit 2 with the refusal on stderr, per the Claude Code hooks contract — and
names the in-flight run's claimed issue plus the safe alternatives.

Decision table, first match wins:

1. ``ORTUS_WORKER=1`` in the environment  -> allow (pipeline session).
2. Tool is not Bash / no command string   -> allow.
3. No git-mutating subcommand in command  -> allow (read-only git, non-git).
4. Grind flock not held                   -> allow (no run in flight).
5. Session descends from the flock holder -> allow (worker of a grind that
   predates the ORTUS_WORKER marker; Linux /proc best-effort).
6. Otherwise                              -> block with an instructive message.

The flock (``.beads/ortus.flock``) is a kernel advisory lock: held always
means a live grind process (see ``ortus unlock``), so this guard can never
wedge intake after a crash. The guard degrades OPEN — any failure of its own
(unreadable stdin, missing lock machinery, no bd) exits 0 so it never bricks
unrelated Bash calls. Stdlib only: template repositories run it with whatever
``python`` uv provides.

Interim guard: retired (or narrowed to integration-branch pushes) by the
workspace-isolation leaf of ortus-u4zv.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

#: Subcommands that rewrite git state in ways that can foul a live candidate.
#: Deliberately conservative and word-based (design decision 2): false
#: negatives are acceptable — the pipeline's own guards remain the backstop —
#: false positives on read-only work are not.
MUTATING_SUBCOMMANDS = frozenset(
    {"commit", "push", "checkout", "switch", "reset", "rebase", "merge"}
)

#: Global git options that consume the following token before the subcommand.
_OPTIONS_WITH_ARG = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)

_SHELL_SEPARATORS = re.compile(r"[;&|\n]+")


def mutating_subcommand(command: str) -> str | None:
    """Return the git-mutating subcommand in ``command``, or None.

    Splits on shell separators so compound commands (``cd x && git commit``)
    match, then reads the first non-option token after each ``git`` word.
    Only that subcommand position is consulted — ``git log --grep merge``
    stays read-only no matter what its arguments mention.
    """
    for segment in _SHELL_SEPARATORS.split(command):
        tokens = segment.split()
        if tokens and tokens[0] == "bd":
            # bd commands are never blocked (intake is the carve-out this
            # guard exists to keep safe), even when their arguments quote
            # git-mutating words.
            continue
        for index, token in enumerate(tokens):
            if token != "git":
                continue
            cursor = index + 1
            while cursor < len(tokens):
                candidate = tokens[cursor]
                if candidate in _OPTIONS_WITH_ARG:
                    cursor += 2
                    continue
                if candidate.startswith("-"):
                    cursor += 1
                    continue
                if candidate in MUTATING_SUBCOMMANDS:
                    return candidate
                break
    return None


def find_lockfile(start: Path) -> Path | None:
    """Walk up from ``start`` to the workspace's ``.beads/ortus.flock``."""
    for candidate in (start, *start.parents):
        lockfile = candidate / ".beads" / "ortus.flock"
        if lockfile.is_file():
            return lockfile
    return None


def flock_held(lockfile: Path) -> bool:
    """Probe the grind flock without blocking; True means a run is in flight.

    Mirrors the holder in ``ortus.core.grind_logic.grind_flock`` (fcntl on
    POSIX, msvcrt on Windows) but stays stdlib-only. Any probe failure other
    than lock contention reads as "not held" — degrade open.
    """
    try:
        fd = os.open(str(lockfile), os.O_RDWR)
    except OSError:
        return False
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _flock_holder_pids(lockfile: Path) -> set[int]:
    """Best-effort holder discovery via /proc/locks (Linux; empty elsewhere).

    Same technique as ``ortus unlock``: flock rows look like
    ``42: FLOCK ADVISORY WRITE 12345 08:20:9134679 0 EOF`` — field 4 is the
    pid, field 5 is maj:min:inode; match on inode.
    """
    try:
        inode = lockfile.stat().st_ino
        rows = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except OSError:
        return set()
    pids: set[int] = set()
    for row in rows:
        parts = row.split()
        if len(parts) < 6:
            continue
        dev_inode = parts[5].split(":")
        if len(dev_inode) == 3 and dev_inode[2] == str(inode):
            try:
                pid = int(parts[4])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
    return pids


def _ancestor_pids() -> set[int]:
    """This process's ancestor pids via /proc/<pid>/stat (Linux; empty elsewhere)."""
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(64):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def grind_is_ancestor(lockfile: Path) -> bool:
    """True when this session was spawned by the flock-holding grind itself.

    Belt-and-braces companion to the ORTUS_WORKER marker: a grind process
    started before the marker existed must not have its own workers' commits
    refused mid-run. Interactive sessions are never descendants of grind, so
    this never widens the guard's blind spot. Best-effort and Linux-only; on
    failure the env marker remains the only exemption.
    """
    holders = _flock_holder_pids(lockfile)
    return bool(holders) and bool(holders & _ancestor_pids())


def claimed_issues(cwd: Path) -> str:
    """Best-effort name for the run's claimed issue(s) via bd."""
    bd = shutil.which("bd")
    if bd is None:
        return "unknown — bd unavailable"
    try:
        proc = subprocess.run(
            [bd, "list", "--status=in_progress", "--json"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        issues = json.loads(proc.stdout)
        names = [
            f"{issue['id']} ({issue.get('title', '').strip()})"
            for issue in issues
            if isinstance(issue, dict) and issue.get("id")
        ]
        if names:
            return ", ".join(names[:3])
    except Exception:
        pass
    return "unknown — bd lookup failed"


def refusal(subcommand: str, lockfile: Path, claimed: str) -> str:
    return (
        f"grind commit guard: refusing `git {subcommand}` — an ortus grind run "
        f"is in flight (it holds {lockfile}).\n"
        f"The shared checkout is on the worker's issue branch (claimed: "
        f"{claimed}); mutating git now would land changes inside the live "
        "candidate.\n"
        "Safe right now: bd commands (intake and tracker writes) and "
        "read-only git (status, log, diff, show). Retry this git command "
        "after the run releases the flock."
    )


def main() -> int:
    if os.environ.get("ORTUS_WORKER") == "1":
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    subcommand = mutating_subcommand(command)
    if subcommand is None:
        return 0
    start = Path(data.get("cwd") or os.getcwd())
    lockfile = find_lockfile(start)
    if lockfile is None or not flock_held(lockfile):
        return 0
    if grind_is_ancestor(lockfile):
        return 0
    sys.stderr.write(refusal(subcommand, lockfile, claimed_issues(start)) + "\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Degrade open: a broken guard must never brick every Bash call.
        sys.exit(0)
