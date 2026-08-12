"""Central wrapper for subprocess.run(['claude', '-p', ...]).

Standard flag set (ortus-6q8v non-regression):
  --dangerously-skip-permissions
  --output-format stream-json
  --verbose
  --fast                       (only when fast=True)

stdout/stderr are tee'd to log_path; the launching terminal sees NOTHING.
Signals to the parent (SIGINT/SIGTERM) kill the child process group so no
descendant claude PIDs leak.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from ortus.core.profiles import AgentProfile

# Windows lacks setsid(), getpgid(), killpg(), and SIGKILL. The process-group
# reap path collapses to proc.terminate()/.kill() on the parent PID — Windows
# has no first-class process-group abstraction the way POSIX does, so we trust
# Popen.terminate() to issue TerminateProcess on the child.
_IS_WINDOWS = sys.platform == "win32"


STANDARD_FLAGS = (
    "--dangerously-skip-permissions",
    "--output-format",
    "stream-json",
    "--verbose",
)


class ReadOnlyExecutionBlocked(RuntimeError):
    """The read-only posture cannot execute a trivial command.

    Raised by the verification preflight. A verifier launched into this
    condition cannot run a single check, so every criterion it reports comes
    back blocked — a verdict shaped like a judgement of the code that is
    nothing of the kind (ortus-dyio).
    """


@dataclass
class ClaudeRunner:
    """Runs `claude -p <prompt>` with the standard flag set, tee'd to log_path."""

    claude_binary: str = "claude"
    extra_env: dict[str, str] = field(default_factory=dict)

    def build_argv(
        self,
        prompt: str,
        *,
        fast: bool = False,
        profile: AgentProfile | None = None,
        readonly: bool = False,
    ) -> list[str]:
        argv: list[str] = [self.claude_binary, "-p", prompt]
        argv.extend(STANDARD_FLAGS)
        if profile is not None and profile.model is not None:
            argv.extend(["--model", profile.model])
        if profile is not None and profile.reasoning_effort is not None:
            argv.extend(["--effort", profile.reasoning_effort])
        if fast:
            argv.append("--fast")
        if readonly:
            # Defense in depth: the OS sandbox in ``run`` is authoritative;
            # these provider controls also keep write tools out of the model's
            # advertised surface.
            argv.extend(
                [
                    "--permission-mode",
                    "dontAsk",
                    "--disallowedTools",
                    "Write,Edit,NotebookEdit",
                ]
            )
        return argv

    def run(
        self,
        prompt: str,
        *,
        repo: Path,
        log_path: Path,
        fast: bool = False,
        profile: AgentProfile | None = None,
        timeout: float | None = None,
        readonly: bool = False,
    ) -> int:
        """Spawn claude, tee output to log_path (NOT stdout), return exit code.

        Raises subprocess.TimeoutExpired if timeout is exceeded; the child
        and its process group are SIGKILL'd before the exception propagates.
        """
        argv = self.build_argv(prompt, fast=fast, profile=profile, readonly=readonly)
        if readonly:
            argv = self._readonly_argv(argv, repo)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # ORTUS_WORKER marks every harness-spawned agent session so the grind
        # commit guard hook (scripts/grind_commit_guard.py) exempts pipeline
        # sessions; operator-supplied extra_env stays authoritative.
        env = {**os.environ, "ORTUS_WORKER": "1", **self.extra_env}
        if readonly:
            env.update(
                {
                    "XDG_CACHE_HOME": "/tmp/ortus-verifier-cache",
                    "UV_CACHE_DIR": "/tmp/ortus-verifier-cache/uv",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )

        # Open log_path in line-buffered append mode. Both stdout and stderr
        # go straight to the file; the parent's terminal sees nothing.
        with open(log_path, "ab", buffering=0) as log_fh:
            popen_kwargs: dict = dict(
                cwd=str(repo),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
            )
            if not _IS_WINDOWS:
                # POSIX: detach into a new session so SIGINT propagates to the
                # process group, not just the parent. Windows has no setsid()
                # equivalent; we fall back to per-PID termination in _kill_group.
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **popen_kwargs)
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                raise
            except KeyboardInterrupt:
                _kill_group(proc)
                raise
            finally:
                # If the child somehow survived a normal-path exit (shouldn't,
                # since wait() blocks), reap its process group to mirror
                # goal.sh's cleanup_children trap.
                if proc.poll() is None:
                    _kill_group(proc)

    def _readonly_argv(self, argv: list[str], repo: Path) -> list[str]:
        """Apply the backend's OS-level read-only launch posture."""

        return _readonly_wrapper(argv, repo)

    def preflight_readonly(self, repo: Path, *, timeout: float = 60.0) -> None:
        """Assert the read-only posture can still execute a trivial command.

        Runs the probe through the *same* wrapper the verifier will get, so a
        posture that cannot execute is caught before a worker is dispatched
        rather than after it has produced an all-blocked report. Raises
        `ReadOnlyExecutionBlocked` naming the probe, its error, and the agent
        session-env path; returns None when the posture is healthy.
        """

        home = Path.home()
        script = _preflight_script(home, repo)
        argv = self._readonly_argv(["/bin/sh", "-c", script], repo)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(repo) if repo.is_dir() else None,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"{type(exc).__name__}: {exc}"
        else:
            # The marker, not the exit status, is the assertion: a command that
            # legitimately exits nonzero is not the failure being guarded
            # against, and a posture that cannot execute never prints it.
            if PREFLIGHT_MARKER in proc.stdout:
                return
            stderr = (proc.stderr or "").strip()
            if _SANDBOX_UNSUPPORTED.search(stderr):
                # A host that cannot create the sandbox at all is a different
                # condition from one whose posture is unwritable, and not the
                # one this guard exists to catch. It is pre-existing, it is
                # `sandbox.smoke_test()`'s to police, and the verifier launch
                # goes through this same wrapper — so it still fails loudly a
                # moment later as a nonzero verifier exit. Blocking here
                # instead took down every grind run on a host without
                # unprivileged user namespaces, CI runners included.
                return
            detail = (
                stderr.splitlines()[-1]
                if stderr
                else f"exited {proc.returncode} without printing the probe marker"
            )
        raise ReadOnlyExecutionBlocked(
            f"{PREFLIGHT_PROBE} failed: {detail}\n"
            f"  probe: /bin/sh -c {script!r}\n"
            f"  agent session-env: {home / '.claude' / 'session-env'}\n"
            "  The verification sandbox cannot execute commands, so no verdict "
            "it produced would be a judgement of the code."
        )


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the child (and on POSIX, its process group)."""
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        # Windows has no killpg; rely on TerminateProcess via Popen helpers.
        # If the child spawned its own children (e.g. cmd.exe wrapper),
        # those are orphaned — there is no portable Job Object plumbing here.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ProcessLookupError):
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# Directories the agent CLI writes under $HOME on every run: a per-session env
# dir, shell snapshots, session transcripts, per-project state. `--ro-bind / /`
# makes all of them read-only, and the CLI then fails to start a shell at all
# ("EROFS: read-only file system, mkdir .../session-env/<uuid>"), so every
# verification returns each criterion BLOCKED instead of judged (ortus-dyio).
# Each gets an empty tmpfs: writable, discarded with the sandbox, and carrying
# nothing from the host session into the verifier.
_AGENT_SCRATCH_DIRS: tuple[str, ...] = (
    ".claude/session-env",
    ".claude/shell-snapshots",
    ".claude/sessions",
    ".claude/projects",
    ".claude/file-history",
    ".claude/paste-cache",
)


PREFLIGHT_PROBE = "read-only verifier execution probe"
PREFLIGHT_MARKER = "ortus-readonly-probe-ok"
# bwrap failing to build the namespace itself — no unprivileged user namespaces
# (GitHub runners, hardened kernels, some containers). Distinct from a sandbox
# that starts and then cannot write, which is what this guard is for.
_SANDBOX_UNSUPPORTED = re.compile(
    r"(setting up uid map|setting up gid map|Creating new namespace|"
    r"clone failed|Operation not permitted.*namespace|unshare)",
    re.IGNORECASE,
)
_PREFLIGHT_SCRATCH = "ortus-preflight"


def _preflight_targets(home: Path, repo: Path) -> list[Path]:
    """Every directory the agent CLI must be able to write to start.

    Exactly the set `_readonly_wrapper` opens up, and for the same reasons.
    Probing more than that would fail a healthy posture; probing less is how
    this guard grew a blind spot — its first version covered only $HOME and
    reported a clean posture to a verifier that could not run a single command
    because `<repo>/.claude` was unwritable (ortus-dyio).
    """

    targets = [home / relative for relative in _AGENT_SCRATCH_DIRS]
    targets = [target for target in targets if target.is_dir()]
    if platform.system() == "Linux":
        # Only the Linux posture nests a bwrap sandbox that materialises
        # bind-mount placeholders under the repo's .claude/. Seatbelt denies
        # writes there deliberately and the CLI does not need them, so probing
        # it on macOS would report every healthy posture as blocked.
        targets.append(repo.resolve() / ".claude")
    return targets


def _preflight_script(home: Path, repo: Path) -> str:
    """The trivial command the verification preflight runs in the sandbox.

    `echo ok` alone would not have caught the failure this guards: the sandbox
    ran `echo` fine and the agent CLI still could not open a shell, because the
    read-only root left its per-session dirs unwritable. So the probe first
    writes what the CLI writes to start, then prints its marker.
    """

    steps = []
    for target in _preflight_targets(home, repo):
        # Created and removed in the same breath: on Linux this lands in a
        # discarded tmpfs, but the macOS posture writes the real directory, and
        # a probe has no business leaving anything behind. `mkdir -p` is what
        # makes a missing parent count as a failure rather than a skip.
        probe_dir = shlex.quote(str(target / _PREFLIGHT_SCRATCH))
        steps.append(f"mkdir -p {probe_dir} && rmdir {probe_dir}")
    return " && ".join([*steps, f"echo {PREFLIGHT_MARKER}"])


def _agent_scratch_tmpfs(home: Path) -> list[str]:
    """`--tmpfs` args for agent scratch dirs that exist on this host.

    A tmpfs needs its mountpoint to already exist: the root is bound read-only,
    so bwrap cannot create a missing one. Skipping absent paths keeps the
    wrapper working across CLI versions that add or drop a directory.
    """

    args: list[str] = []
    for relative in _AGENT_SCRATCH_DIRS:
        candidate = home / relative
        if candidate.is_dir():
            args.extend(["--tmpfs", str(candidate)])
    return args


# Config the agent CLI reads out of the repo's .claude/ and must keep seeing
# once that directory is made writable below.
_REPO_AGENT_CONFIG: tuple[str, ...] = ("settings.json", "settings.local.json")


def _repo_agent_dir_tmpfs(repo: Path) -> list[str]:
    """Make the repo's `.claude/` writable without losing its config.

    The agent CLI runs its own inner sandbox, which materialises bind-mount
    placeholders under `<repo>/.claude` for every deny-within-allow rule it
    carries (`hooks`, `skills`, `.cc-writes`, ...). Those paths need not exist
    on disk, and `--ro-bind / /` leaves the repo tree read-only, so the inner
    sandbox cannot create them and refuses to start:

        bwrap: Can't create file at <repo>/.claude/hooks: Read-only file system

    An empty tmpfs over the directory lets the inner sandbox create whatever it
    needs, and the config files are re-bound read-only on top so the CLI still
    reads the project's real settings. Pre-creating individual placeholders was
    the alternative and loses to this: the deny list is a moving target owned by
    the CLI, not by ortus. The source tree outside `.claude/` stays read-only,
    so the candidate is as protected as before (ortus-dyio).

    A repo with no `.claude/` at all gets no mount: bwrap cannot create a tmpfs
    mountpoint under a read-only root, and ortus will not write into a candidate
    to make one. That posture genuinely cannot start the inner sandbox, and
    `_preflight_targets` includes the path so the run aborts naming the probe
    rather than handing back a report whose every criterion is blocked.
    """

    claude_dir = repo / ".claude"
    if not claude_dir.is_dir():
        return []
    args = ["--tmpfs", str(claude_dir)]
    for name in _REPO_AGENT_CONFIG:
        config = claude_dir / name
        if config.is_file():
            # Target sits inside the tmpfs, so bwrap creates the mountpoint.
            args.extend(["--ro-bind", str(config), str(config)])
    return args


# Repo-level state that belongs to the tools, not to the candidate: git's own
# metadata and lock files, the agent CLI's project config and deny-rule
# placeholders, and the bd export. None of it is code under test, and all of it
# gets written during an ordinary read-only review.
REPO_TOOL_STATE: frozenset[str] = frozenset(
    {".git", ".gitconfig", ".claude", ".codex", ".beads", ".ortus"}
)


def _repo_source_readonly(repo: Path) -> list[str]:
    """Bind the repo writable, then re-bind its source entries read-only.

    Inverts the posture (ortus-v8fn). `--ro-bind / /` alone forbids every write
    under the repo, and the tools a verifier runs need several just to start —
    each fix revealed the next path:

        ~/.claude/session-env/<uuid>            <repo>/.git/config.lock
        <repo>/.claude/hooks                    <repo>/.gitconfig
        <repo>/.git/worktrees/*/config.worktree

    That set cannot be enumerated: it belongs to git and to the agent CLI, it
    grows with their versions, and a lock file specifically *must not* exist
    for git to take it. So enumerate ours instead. The repo goes writable, then
    every top-level entry that is not tool state is re-bound read-only — bwrap
    applies mounts in order, so the later read-only binds win.

    The result states the property directly: the verifier cannot modify the code
    under test. It is also not the only guard. `_verify_candidate` re-hashes the
    candidate diff, the candidate path set and the issue packet after the verdict
    and discards any verdict where a byte moved, so mutation is caught even where
    the mount is not.
    """

    if not repo.is_dir():
        # Nothing to bind and nothing to protect. bwrap would refuse to launch
        # on a missing source, so returning empty keeps the wrapper usable for
        # a caller that has not created the repo yet.
        return []
    entries = sorted(
        entry for entry in repo.iterdir() if entry.name not in REPO_TOOL_STATE
    )
    args = ["--bind", str(repo), str(repo)]
    for entry in entries:
        args.extend(["--ro-bind", str(entry), str(entry)])
    return args


def _seatbelt_literal(path: Path) -> str:
    """Escape a path for a Seatbelt string literal."""

    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _readonly_wrapper(argv: list[str], repo: Path) -> list[str]:
    """Apply a source-read-only OS posture while leaving /tmp writable."""

    system = platform.system()
    if system == "Linux":
        wrapper = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            *_agent_scratch_tmpfs(Path.home()),
        ]
        resolved_repo = repo.resolve()
        try:
            relative_to_tmp = resolved_repo.relative_to("/tmp")
        except ValueError:
            pass
        else:
            current = Path("/tmp")
            for part in relative_to_tmp.parts[:-1]:
                current /= part
                wrapper.extend(["--dir", str(current)])
            wrapper.extend(["--ro-bind", str(resolved_repo), str(resolved_repo)])
        # Last, so it lands on top: bwrap applies mounts in order, and a repo
        # under /tmp is restored by the ro-bind above after `--tmpfs /tmp` wiped
        # it. Staging the repo mounts before that ro-bind lets the ro-bind mask
        # them, which puts the repo back to read-only — the very failure they
        # exist to prevent (ortus-dyio).
        wrapper.extend(_repo_source_readonly(resolved_repo))
        return [*wrapper, "--chdir", str(resolved_repo), "--", *argv]
    if system == "Darwin":
        repo_literal = _seatbelt_literal(repo)
        # Seatbelt is last-match-wins, so the agent scratch dirs are allowed
        # before the candidate is denied: the CLI gets the per-session dirs it
        # needs to start (the same ones Linux hands a tmpfs) and the candidate
        # stays unwritable regardless of where $HOME sits.
        scratch = " ".join(
            f'(subpath "{_seatbelt_literal(Path.home() / relative)}")'
            for relative in _AGENT_SCRATCH_DIRS
        )
        profile = (
            "(version 1) (allow default) (deny file-write*) "
            '(allow file-write* (subpath "/tmp") (subpath "/private/tmp") '
            f'(subpath "/dev") {scratch}) '
            f'(deny file-write* (subpath "{repo_literal}"))'
        )
        return ["sandbox-exec", "-p", profile, *argv]
    raise RuntimeError(f"read-only verifier unsupported on {system}")
