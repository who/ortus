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
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

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


#: The id git writes for "no object" on a pre-push stdin line.
ZERO_SHA = "0" * 40

#: What a stealth push must never send to origin: the tracker's Dolt data,
#: which bd keeps under `refs/dolt/` on whatever git remote it syncs through.
_DOLT_REF_PREFIX = "refs/dolt/"
_DOLT_REMOTE_INFO_REF = "refs/heads/__dolt_remote_info__"

#: Agent attribution lines that name the machinery behind a commit.
_ATTRIBUTION_LINE = re.compile(
    r"^\s*(co-authored-by:\s*claude\b|executed-by:)", re.IGNORECASE | re.MULTILINE
)
_ISSUE_TRAILER_LINE = re.compile(r"^\s*Ortus-Issue:.*$", re.MULTILINE)

#: The marker every hook Ortus writes carries, so a re-install knows its own.
STEALTH_HOOK_MARKER = "# ortus stealth hook v1"
STEALTH_HOOKS: dict[str, str] = {
    "pre-push": 'ortus guard pre-push "$@"',
    "pre-commit": "ortus guard pre-commit",
}
ALLOW_GITIGNORE_ENV = "ORTUS_STEALTH_ALLOW_GITIGNORE"


def _stealth_patterns(facts: Mapping) -> tuple[str, ...]:
    extras = facts.get("stealth_extra_excludes") or []
    return STEALTH_EXCLUDE_PATTERNS + tuple(str(p) for p in extras)


def stealth_patterns(repo: Path) -> tuple[str, ...]:
    """The default stealth patterns plus the repository's recorded extras."""
    from ortus.core.config import read_recorded_facts

    try:
        return _stealth_patterns(read_recorded_facts(repo))
    except (OSError, ValueError):
        return STEALTH_EXCLUDE_PATTERNS


def tracked_excluded_paths(repo: Path, patterns: Sequence[str]) -> tuple[str, ...]:
    """Tracked files the exclude rules would hide, limited to Ortus patterns.

    `git ls-files -ci --exclude-standard` names every tracked path an ignore
    rule matches; filtering it to `patterns` keeps upstream's own
    force-tracked ignored files out of the answer, and leaves out a default
    pattern init dropped because upstream tracks that file.
    """
    proc = _git(repo, "ls-files", "-z", "-ci", "--exclude-standard")
    if proc.returncode != 0:
        return ()
    return tuple(
        path
        for path in proc.stdout.split("\0")
        if path and any(pattern_matches(path, p) for p in patterns)
    )


def remote_url(repo: Path, name: str) -> str:
    """The URL of git remote `name`, or "" when there is none."""
    proc = _git(repo, "remote", "get-url", name)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def same_remote_url(a: str, b: str) -> bool:
    """Whether two remote URLs name one repository.

    Ignores a trailing `/` or `.git` and Dolt's `git+` scheme prefix, so a
    tracker remote registered as `git+https://host/repo.git` matches the
    origin `https://host/repo`.
    """

    def norm(url: str) -> str:
        url = url.strip().removeprefix("git+").rstrip("/")
        return url[: -len(".git")] if url.endswith(".git") else url

    return bool(a and b) and norm(a) == norm(b)


def _targets_origin(repo: Path, remote: str, url: str | None) -> bool:
    """Whether a push to `remote` (a name or a URL) lands on origin."""
    origin = remote_url(repo, "origin")
    if not origin:
        return False
    if remote == "origin":
        return True
    candidate = url or remote_url(repo, remote) or remote
    return same_remote_url(candidate, origin)


def _pushed_messages(
    repo: Path, local_sha: str, remote_sha: str
) -> list[tuple[str, str]]:
    """(sha, message) for every commit a push of `local_sha` would send.

    A first push (or a remote tip this clone has never fetched) has no range
    to name, so it scans everything not already on some remote.
    """
    ranges: list[list[str]] = []
    if remote_sha != ZERO_SHA:
        ranges.append([f"{remote_sha}..{local_sha}"])
    ranges.append([local_sha, "--not", "--remotes"])
    for rev_args in ranges:
        proc = _git(repo, "log", "--format=%H%x00%B%x1e", *rev_args)
        if proc.returncode != 0:
            continue
        commits = []
        for record in proc.stdout.split("\x1e"):
            sha, sep, body = record.strip("\n").partition("\0")
            if sep:
                commits.append((sha, body))
        return commits
    return []


def _message_violations(
    sha: str, message: str, id_pattern: re.Pattern[str] | None, issue_ref: str
) -> list[str]:
    short = sha[:7]
    found: list[str] = []
    if id_pattern is not None:
        scanned = message
        if issue_ref == "trailer":
            scanned = _ISSUE_TRAILER_LINE.sub("", message)
        ids = sorted(set(m.group(0) for m in id_pattern.finditer(scanned)))
        if ids:
            found.append(f"commit {short} names bead id {', '.join(ids)}")
    for match in _ATTRIBUTION_LINE.finditer(message):
        found.append(f"commit {short} carries a {match.group(1).strip()!r} line")
    return found


def push_violations(
    repo: Path,
    remote: str,
    refspecs: Sequence[tuple[str, str, str, str]],
    *,
    url: str | None = None,
) -> list[str]:
    """Every reason a stealth push of `refspecs` to `remote` must not happen.

    `refspecs` are `(local_ref, local_sha, remote_ref, remote_sha)`, the four
    fields git hands a pre-push hook on each stdin line. Three things leak a
    stealth repository: a tracked Ortus path (a `git add -f` past the exclude
    block), a commit message naming a bead id or the agent behind it, and the
    tracker's Dolt refs landing on origin. An empty list means the push is
    clean. Nothing here rewrites history or touches a remote.
    """
    from ortus.core.config import read_recorded_facts, resolve_commit_issue_ref

    try:
        facts = read_recorded_facts(repo)
    except (OSError, ValueError):
        facts = {}
    violations: list[str] = []

    for path in tracked_excluded_paths(repo, _stealth_patterns(facts)):
        violations.append(
            f"tracked ortus path {path} (untrack it: git rm --cached {path})"
        )

    try:
        issue_ref = resolve_commit_issue_ref(facts, stealth=True)
    except ValueError:
        issue_ref = "none"
    prefix = facts.get("prefix")
    id_pattern = (
        re.compile(rf"\b{re.escape(str(prefix))}-[a-z0-9]+(\.[0-9]+)*\b")
        if prefix and issue_ref != "subject"
        else None
    )
    seen: set[str] = set()
    for _local_ref, local_sha, _remote_ref, remote_sha in refspecs:
        if local_sha == ZERO_SHA:
            continue  # a deletion sends no commits
        for sha, message in _pushed_messages(repo, local_sha, remote_sha):
            if sha in seen:
                continue
            seen.add(sha)
            violations.extend(_message_violations(sha, message, id_pattern, issue_ref))

    if _targets_origin(repo, remote, url):
        for _local_ref, _local_sha, remote_ref, _remote_sha in refspecs:
            if remote_ref.startswith(_DOLT_REF_PREFIX) or remote_ref == _DOLT_REMOTE_INFO_REF:
                violations.append(f"{remote_ref} is tracker data and must not go to origin")
    return violations


def staged_gitignore_violation(repo: Path, env: Mapping[str, str]) -> str | None:
    """Why a stealth commit staging a `.gitignore` is refused, or None.

    The fork's `.gitignore` files are upstream's; stealth keeps its excludes
    in `.git/info/exclude`. An operator who means the edit sets
    `ORTUS_STEALTH_ALLOW_GITIGNORE=1` for that one commit.
    """
    if env.get(ALLOW_GITIGNORE_ENV) == "1":
        return None
    proc = _git(repo, "diff", "--cached", "--name-only", "-z")
    if proc.returncode != 0:
        return None
    staged = [p for p in proc.stdout.split("\0") if p and p.split("/")[-1] == ".gitignore"]
    if not staged:
        return None
    return (
        f"staged {', '.join(staged)}: stealth keeps excludes in .git/info/exclude; "
        f"set {ALLOW_GITIGNORE_ENV}=1 to commit an upstream .gitignore change"
    )


def _hook_script(command: str) -> str:
    return (
        "#!/bin/sh\n"
        f"{STEALTH_HOOK_MARKER}\n"
        "if ! command -v ortus >/dev/null 2>&1; then\n"
        '    echo "ortus stealth guard: ortus is not on PATH; refusing (fail closed)" >&2\n'
        "    exit 1\n"
        "fi\n"
        f"exec {command}\n"
    )


def install_stealth_hooks(repo: Path) -> list[str]:
    """Write the stealth `pre-push` and `pre-commit` hooks; return warnings.

    Only into git's own hooks directory, and only when `core.hooksPath` is
    unset: a hooks path belongs to whatever set it, and grind's in-process
    guard covers its pushes either way. A hook Ortus did not write is never
    overwritten.
    """
    configured = _git(repo, "config", "--get", "core.hooksPath")
    if configured.returncode == 0 and configured.stdout.strip():
        return [
            f"core.hooksPath is {configured.stdout.strip()}; stealth hooks not "
            "installed (grind's in-process push guard still runs)"
        ]
    proc = _git(repo, "rev-parse", "--git-path", "hooks")
    if proc.returncode != 0 or not proc.stdout.strip():
        return ["git resolved no hooks directory; stealth hooks not installed"]
    hooks = Path(proc.stdout.strip())
    if not hooks.is_absolute():
        hooks = repo / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    for name, command in STEALTH_HOOKS.items():
        hook = hooks / name
        if hook.exists():
            text = hook.read_text(encoding="utf-8", errors="replace")
            if STEALTH_HOOK_MARKER not in text:
                warnings.append(f"{hook} is not an ortus hook; left untouched")
                continue
        hook.write_text(_hook_script(command), encoding="utf-8")
        hook.chmod(0o755)
    return warnings
