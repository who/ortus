"""ortus unlock <repo> — recover from a stuck grind flock.

The grind flock is a kernel advisory lock (portalocker/flock) — it cannot go
stale on disk: if acquisition fails, some LIVE process holds the fd. That is
either a grind you forgot about, or a detached worker subprocess that
inherited the fd when its parent died. This verb tells those cases apart:

  * nobody holds it      -> the file is inert; remove it and report
  * a process holds it   -> name the holder (pid + cmdline via /proc/locks)
                            and refuse, unless --force, which SIGTERMs (then
                            SIGKILLs) the holder before removing the file

--revert-claims additionally flips every in_progress issue back to open so a
killed grind's half-claimed work returns to the ready queue.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Optional

import typer

from ortus.core import output
from ortus.core.bd import BdClient
from ortus.core.grind_logic import FlockBusy, grind_flock
from ortus.core.grind_loop import EXCLUDED_LABELS
from ortus.core.repo import resolve_repo


def _make_bd(repo: Path) -> BdClient:
    """Indirection so tests can swap in a stub bd client."""
    return BdClient(repo=repo)


def _flock_holder_pids(lockfile: Path) -> list[int]:
    """Best-effort holder discovery via /proc/locks (Linux; empty elsewhere).

    flock rows look like `42: FLOCK ADVISORY WRITE 12345 08:20:9134679 0 EOF`
    — field 4 is the pid, field 5 is maj:min:inode. We match on inode.
    """
    try:
        inode = lockfile.stat().st_ino
    except OSError:
        return []
    try:
        rows = Path("/proc/locks").read_text(encoding="ascii").splitlines()
    except OSError:
        return []
    pids: list[int] = []
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
                pids.append(pid)
    return sorted(set(pids))


def _describe_pid(pid: int) -> str:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmd = cmdline.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        cmd = "(cmdline unavailable)"
    return f"pid {pid}: {cmd[:120]}"


def _kill_holders(pids: list[int]) -> list[int]:
    """SIGTERM the holders, escalate to SIGKILL after a grace period.

    Returns pids that could not be signalled at all (permission/vanished).
    """
    failed: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError:
            failed.append(pid)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if all(not _alive(pid) for pid in pids):
            return failed
        time.sleep(0.2)
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
    time.sleep(0.2)
    return failed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def unlock(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="If a live process holds the flock, kill it (SIGTERM, then "
        "SIGKILL) before removing the lock file.",
    ),
    revert_claims: bool = typer.Option(
        False,
        "--revert-claims",
        help="Also flip every in_progress issue back to open so a dead "
        "grind's half-claimed work returns to the ready queue.",
    ),
) -> None:
    """Clear a stuck grind flock; optionally revert in-progress claims."""
    target = resolve_repo(repo)
    lockfile = target / ".beads" / "ortus.flock"

    if not lockfile.exists():
        output.info(f"no flock file at {lockfile}; nothing to unlock")
    else:
        cleared = False
        try:
            with grind_flock(target):
                # Acquired instantly: nobody holds it — the file is inert.
                lockfile.unlink(missing_ok=True)
                cleared = True
        except FlockBusy:
            holders = _flock_holder_pids(lockfile)
            if not force:
                lines = [_describe_pid(p) for p in holders] or [
                    "holder pid not discoverable (no /proc/locks on this "
                    "platform, or the holder is in another namespace)"
                ]
                output.error(
                    "flock is actively held — a grind (or a worker that "
                    "inherited its fd) is still alive:\n  " + "\n  ".join(lines),
                    hint="stop that process (Ctrl-C / kill <pid>) or re-run "
                    "with --force to kill it and remove the lock",
                )
                raise typer.Exit(code=1)
            if holders:
                output.warn(
                    "killing flock holder(s): "
                    + "; ".join(_describe_pid(p) for p in holders)
                )
                unkillable = _kill_holders(holders)
                for pid in unkillable:
                    output.warn(f"could not signal pid {pid} (permission?)")
            else:
                output.warn(
                    "--force given but no holder pid discoverable; removing "
                    "the lock file anyway (if a hidden holder exists, its "
                    "lock dies with it — a fresh grind will use a new inode)"
                )
            try:
                with grind_flock(target):
                    lockfile.unlink(missing_ok=True)
                    cleared = True
            except FlockBusy:
                # Holder survived our signals (or is unkillable from here).
                # Removing the file still unblocks a fresh grind: the
                # survivor's lock rides the old inode, the new grind locks a
                # newly created file.
                lockfile.unlink(missing_ok=True)
                cleared = True
                output.warn(
                    "a holder is still alive; lock file removed anyway — "
                    "verify the old process is really dead before starting "
                    "a new grind, or two grinds may run at once"
                )
        if cleared:
            output.success(f"flock cleared: {lockfile}")

    if revert_claims:
        bd = _make_bd(target)
        claimed = sorted(bd.in_progress_ids(exclude_labels=EXCLUDED_LABELS))
        if not claimed:
            output.info("no in_progress issues to revert")
        for issue_id in claimed:
            try:
                bd.update_status(issue_id, "open")
                output.success(f"reverted claim: {issue_id} → open")
            except Exception as exc:  # bd hiccup: report, keep going
                output.warn(f"could not revert {issue_id}: {exc}")
