"""Stealth mode: Ortus in a repository without any Ortus trace in git.

The case is a fork of an open-source project that has to stay cleanly
mergeable with upstream. Every file Ortus, bd and the agent backends write is
kept out of git through one fenced block in `.git/info/exclude`, which git
reads and never commits, instead of through the tracked `.gitignore`.

Stealth is on when the project `.ortusrc` records `stealth = true` or the
repository's local git config carries `ortus.stealth = true`. Either one is
enough, so a clobbered `.ortusrc` does not quietly turn it off.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

from ortus import __version__ as ORTUS_VERSION
from ortus.core.agent_files import BlockOutcome, apply_hash_block

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

STEALTH_BLOCK = "stealth"
STEALTH_SCHEMA = 1
STEALTH_GIT_CONFIG_KEY = "ortus.stealth"

#: What an Ortus repository writes that git must never see. Root-anchored so
#: a same-named file deeper in the fork's tree stays tracked, except the three
#: globs that name generated scratch wherever it lands.
STEALTH_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "/.ortusrc",
    "/.beads/",
    "/.beads.gate.lock",
    "/.beads-credential-key",
    "/.dolt/",
    "*.gate.lock*",
    "/.agents/",
    "/.claude/",
    "/.codex/",
    "/.cursor/",
    "/.grok/",
    "/.codegraph/",
    "/.bv/",
    "/opencode.json",
    "/AGENTS.md",
    "/AGENTS.override.md",
    "/CLAUDE.md",
    "/CLAUDE.local.md",
    "/HUMAN-TODO.md",
    "/PRD.md",
    "PRD*.md",
    "*.bak.*",
    "/logs/",
    "/.cache/",
)

#: Where the managed agent block goes when upstream already tracks the file.
#: Each target is a file its agent loads alongside the tracked one.
AGENT_FILE_REDIRECTS: dict[str, str] = {
    "AGENTS.md": "AGENTS.override.md",
    "CLAUDE.md": "CLAUDE.local.md",
}

#: The one Claude settings file a fork that tracks `.claude/` still lets
#: Ortus write, and the exclude line that replaces `/.claude/` there.
CLAUDE_LOCAL_SETTINGS = ".claude/settings.local.json"

#: The ignore files that still count against a managed agent file in stealth:
#: only the tracked one, since `.git/info/exclude` is where stealth puts it.
TRACKED_IGNORE_SOURCES: tuple[Path, ...] = (Path(".gitignore"),)

#: The name a stealth tracker's private Dolt remote is registered under.
TRACKER_REMOTE_NAME = "ortus-private"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def local_git_config(repo: Path, key: str) -> str:
    """The repository-local value of `key`, or "" when unset or unreadable."""
    if not repo.is_dir():
        return ""
    try:
        proc = _git(repo, "config", "--local", "--get", key)
    except OSError:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _recorded_stealth(repo: Path) -> bool:
    path = repo / ".ortusrc"
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh).get("stealth") is True
    except (OSError, ValueError):
        return False


def is_stealth(repo: Path) -> bool:
    """True when `.ortusrc` or the local git config turns stealth on."""
    if _recorded_stealth(repo):
        return True
    return local_git_config(repo, STEALTH_GIT_CONFIG_KEY).lower() == "true"


def render_exclude_block(
    patterns: Iterable[str], ortus_version: str = ORTUS_VERSION
) -> str:
    """The Ortus `.git/info/exclude` block, markers included.

    The markers carry the same `block=` and `schema=` attributes as the
    `.gitignore` section, so the shared hash-fence parser rewrites only this
    block and treats a newer schema as one to leave alone.
    """
    begin = (
        f"# BEGIN ortus stealth v{STEALTH_SCHEMA} block={STEALTH_BLOCK} "
        f"schema={STEALTH_SCHEMA} generated-by=ortus@{ortus_version}"
    )
    end = f"# END ortus stealth block={STEALTH_BLOCK}"
    body = "\n".join(patterns)
    return f"{begin}\n{body}\n{end}"


def exclude_path(repo: Path) -> Path:
    """`info/exclude` under the common git directory.

    A linked worktree shares the main checkout's exclude file, so the block
    is written where every worktree of the repository reads it.
    """
    proc = _git(repo, "rev-parse", "--git-common-dir")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"{repo} is not a git checkout: {proc.stderr.strip()}")
    common = Path(proc.stdout.strip())
    if not common.is_absolute():
        common = repo / common
    return common / "info" / "exclude"


def merge_exclude(repo: Path, patterns: Sequence[str]) -> BlockOutcome:
    """Write the Ortus block into the repository's exclude file.

    Only the fenced block is ever rewritten; bd's own stealth lines and
    anything the operator wrote survive byte for byte. Malformed Ortus markers
    raise `AgentFileError`, as they do for `.gitignore`.
    """
    return apply_hash_block(
        exclude_path(repo),
        STEALTH_BLOCK,
        render_exclude_block(patterns),
        schema=STEALTH_SCHEMA,
    )


def tracked_ortus_paths(
    repo: Path, patterns: Sequence[str] = STEALTH_EXCLUDE_PATTERNS
) -> tuple[str, ...]:
    """Tracked files that match any of `patterns`, by gitignore rules.

    `git ls-files --cached --ignored` applies only the patterns given here,
    never the repository's own ignore files, so the answer is exactly what
    the stealth block would hide if git were not already tracking it.
    """
    if not patterns:
        return ()
    args = ["ls-files", "-z", "--cached", "--ignored"]
    args += [f"--exclude={pattern}" for pattern in patterns]
    proc = _git(repo, *args)
    if proc.returncode != 0:
        return ()
    return tuple(path for path in proc.stdout.split("\0") if path)


def bd_hooks_path(repo: Path) -> str:
    """The local `core.hooksPath` when it points at bd's `.beads/hooks`, else "".

    A plain `bd init` sets it, absolute or relative; a stealth repository must
    not run hooks out of an excluded directory. A hooks path that belongs to
    something else (husky and the like) is not bd's and answers "".
    """
    value = local_git_config(repo, "core.hooksPath")
    if not value:
        return ""
    parts = Path(value.rstrip("/")).parts
    return value if parts[-2:] == (".beads", "hooks") else ""


def pattern_matches(path: str, pattern: str) -> bool:
    """A small gitignore matcher over repository-relative paths.

    Covers the shapes the default pattern list uses: a root-anchored name or
    directory and an unanchored basename glob.
    """
    body = pattern.strip("/")
    if pattern.startswith("/"):
        return path == body or path.startswith(body + "/")
    return any(fnmatch.fnmatch(part, body) for part in path.split("/"))


def status_ortus_paths(
    repo: Path, patterns: Sequence[str] = STEALTH_EXCLUDE_PATTERNS
) -> tuple[str, ...]:
    """Paths `git status --porcelain` reports that match a stealth pattern."""
    proc = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        return ()
    paths: list[str] = []
    entries = proc.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # the rename source follows as its own entry
        if any(pattern_matches(path, pattern) for pattern in patterns):
            paths.append(path)
    return tuple(paths)
