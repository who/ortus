"""Is the running worker making progress, or repeating itself?

The reaper ends a worker window on two deterministic facts: the done bar and a
claim flagged for a human. A looping worker — re-reading the same file,
re-running the same failing command, restating the same plan — satisfies
neither, so it runs until ``--worker-timeout`` kills it and the run records a
watchdog timeout for a worker that was stuck from its third minute on.

This module supplies the missing signal. Once per interval the watch computes
the window's progress facts in code (:func:`progress_facts`: files written,
commits, check results and their trend, command repetition) and sends them
with the typed facts the harness already holds (elapsed seconds, whether the
integration branch advanced, the claim's tracker status) as a trusted block.
A bounded, sanitized slice of the worker's own log travels apart from them as
untrusted context. Jev answers two narrow questions and a basis question,
which become one probability: how likely it is that this worker is looping.
That probability is an opinion. The decision is taken in code, by
:func:`should_reap`, over the window's last samples and the same facts; there
is no confidence floor and no human parking
anywhere in this module, and a reaped worker takes the same post-window path
as any other no-close window.

Two opt-ins arm it, because this is the only part of Ortus that sends worker
transcript text to a provider: ``jev_progress_reaper`` must name a mode other
than off, and ``judge.include_log_tail`` must be true. Shadow is the default
mode and records the decision it would have taken without touching the worker;
only enforce lets a reap reason reach the poll. Every failure — no key, no
SDK, a missed deadline, a malformed answer — is recorded and skips the check,
so a Jev outage can never end a healthy worker early.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence
from uuid import UUID, uuid4

from ortus.core.judge import JudgeConfig
from ortus.core.judge_log import (
    OutcomeStatus, _append, _clean_metadata, _common, narrow_fields,
    question_version, request_fields, side_fields,
)
from ortus.core.judge_questions import (
    HAS_BASIS, basis_question, has_basis, noul_question, split_state,
)
from ortus.core.judge_state import StateError, environment_secrets, sanitize_field
from ortus.core.judge_typesafe import (
    API_KEY_ENV, JudgeFailure, JudgeUsage, _default_client, _Invalid, _mapping,
    _narrow, _usage,
)
from ortus.core.user_env import judge_environment

if TYPE_CHECKING:
    from ortus.core.config import Config

#: Progress records live beside the other gate logs but in their own file: the
#: replay reader rejects any record whose event name it does not know, so a new
#: event kind in the decision log would invalidate the log it validates.
PROGRESS_LOG_NAME = "jev-progress.jsonl"

#: `.ortusrc` keys and the environment variable that overrides the mode.
PROGRESS_CONFIG_KEY = "jev_progress_reaper"
PROGRESS_INTERVAL_KEY = "jev_progress_interval_s"
PROGRESS_ENV = "ORTUS_JEV_PROGRESS"

#: Seconds between checks. Long enough that a slow-but-working step is not read
#: as a loop, and short enough to save most of a 5400-second timeout.
DEFAULT_PROGRESS_INTERVAL_S = 300

#: The tail bound the request carries, as lines and as encoded bytes. This is
#: the module's own ceiling rather than `total_bytes_cap`, which budgets the
#: packed issue state that this request does not send.
TAIL_MAX_LINES = 200
TAIL_MAX_BYTES = 16 * 1024

#: Bytes read from the end of the log before any decoding. A stream-json line
#: can be tens of kilobytes, so the line bound alone is not a read bound.
_READ_BYTES = 1 << 20

#: Per-line cap for the summarized tail, and what an unscreenable line becomes.
_LINE_CAP = 240
REDACTED = "[redacted]"

#: The reap reason this module hands the poll.
JEV_LOOPING_REASON = "jev_looping"
#: The narrow questions (question set progress-v2), one worker window each.
SAME_FAILURE = "same_failure_repeating"
NO_NEW_ARTIFACTS = "no_new_artifacts_by_facts"

#: Consecutive looping samples that decide a reap, and the probability each
#: must exceed. Two because one reading of a transcript slice is a guess about
#: a moment and the pair has to span an interval with no new commits in it.
_REAP_SAMPLES = 2
_LOOPING_FLOOR = 0.5

#: Bounds on the code-computed window facts: paths named, checks in the trend,
#: and characters per path.
_MAX_FACT_PATHS = 20
_TREND_MAX = 10
_PATH_CAP = 160
#: Seconds one git query may take before the fact it answers is unknown.
_GIT_TIMEOUT_S = 10
#: Where the Jev logs live under a repository; never a worker artifact.
_HARNESS_LOG_PREFIX = "logs/jev-"
#: What `last_check` reads when the window ran no test or check command.
NONE_RUN = "none-run"
#: A shell command that runs tests, a linter or a type checker.
_CHECK_COMMAND = re.compile(
    r"\b(?:pytest|unittest|tox|nox|jest|vitest|mocha|ruff|mypy|pyright|eslint|tsc)\b"
    r"|\b(?:go|cargo|npm|pnpm|yarn|bun|deno|make|just|mvn|gradle|dotnet)\s+"
    r"(?:run\s+)?(?:test|check|lint)\b"
)


class ProgressMode(str, Enum):
    """How much the progress signal is allowed to do."""

    #: No checks, no records, no cost: today's reaper, byte for byte.
    OFF = "off"
    #: Read the vector and record what it would have done. Never reaps.
    SHADOW = "shadow"
    #: Let the decision reach the poll and end a looping worker.
    ENFORCE = "enforce"


class WindowOutcome(str, Enum):
    """How the worker window the checks watched actually ended."""

    CLOSED = "closed"
    TIMEOUT = "timeout"
    REAPED = "reaped"
    EXITED = "exited"


def progress_mode(
    config: Config | None = None, *, environ: Mapping[str, str] | None = None
) -> ProgressMode:
    """The mode this run watches workers in.

    The environment wins over `.ortusrc` so one run can be flipped without
    editing a tracked file. A value neither layer can parse never enables the
    reaper: an unreadable export falls back to the configured mode, and an
    unreadable configured mode falls back to shadow, which only records.
    """
    env = os.environ if environ is None else environ
    for value in (env.get(PROGRESS_ENV), _configured(config, PROGRESS_CONFIG_KEY)):
        if isinstance(value, str):
            try:
                return ProgressMode(value.strip().lower())
            except ValueError:
                continue
    return ProgressMode.SHADOW


def progress_interval(config: Config | None = None) -> float:
    """Seconds between checks, falling back to the default for any other value."""
    value = _configured(config, PROGRESS_INTERVAL_KEY)
    if type(value) in (int, float) and float(value) > 0:
        return float(value)
    return float(DEFAULT_PROGRESS_INTERVAL_S)


def _configured(config: Config | None, key: str) -> object:
    getter = getattr(config, "get", None)
    return getter(key) if callable(getter) else None


@dataclass(frozen=True)
class WorkerProgress:
    """The typed facts the harness already knows about this worker window."""

    elapsed_seconds: float
    head_advanced: bool
    bead_status: OutcomeStatus

    def __post_init__(self) -> None:
        if (type(self.elapsed_seconds) not in (int, float)
                or self.elapsed_seconds < 0
                or type(self.head_advanced) is not bool
                or not isinstance(self.bead_status, OutcomeStatus)):
            raise ValueError("invalid worker progress facts")

    def payload(self) -> dict:
        """The facts as they travel: no paths, no ids, no transcript text."""
        return {
            "elapsed_seconds": round(float(self.elapsed_seconds), 1),
            "head_advanced": self.head_advanced,
            "bead_status": self.bead_status.value,
        }


@dataclass(frozen=True)
class ProgressVerdict:
    """One reading of the window, or the typed reason there is none."""

    p_looping: float | None = None
    p_progressing: float | None = None
    confidence: float | None = None
    failure: JudgeFailure | None = None
    #: Request cost, for the log only, and left out of equality so two
    #: readings that say the same thing compare equal.
    latency_ms: float | None = field(default=None, compare=False)
    usage: JudgeUsage | None = field(default=None, compare=False)
    #: The narrow answers the vector was read off, keyed by question name.
    narrow: Mapping[str, float] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.failure is not None:
            if (not isinstance(self.failure, JudgeFailure)
                    or self.p_looping is not None
                    or self.p_progressing is not None
                    or self.confidence is not None):
                raise ValueError("invalid progress failure")
            return
        for value in (self.p_looping, self.p_progressing, self.confidence):
            if type(value) not in (int, float) or not 0.0 <= float(value) <= 1.0:
                raise ValueError("invalid progress vector")

    @property
    def shrunk_looping(self) -> float | None:
        """:attr:`p_looping` pulled toward the coin flip by its confidence."""
        if self.failure is not None:
            return None
        return _shrunk(float(self.p_looping or 0.0), float(self.confidence or 0.0))


def _shrunk(probability: float, confidence: float) -> float:
    """Pull a probability toward the coin flip by how unsure the judge was.

    A reading reported with full confidence passes through unchanged; one
    reported with no confidence carries no information and lands on 0.5, so an
    unsure judge cannot argue for ending a worker.

    The confidence is the answer set's own basis answer, or zero when it
    claims no basis, so a reading Jev cannot ground in the facts lands on the
    coin flip and argues for nothing.
    """
    p = min(max(probability, 0.0), 1.0)
    c = min(max(confidence, 0.0), 1.0)
    return 0.5 + (p - 0.5) * c


def _reading(narrow: Mapping[str, float]) -> ProgressVerdict:
    """A progress-v2 answer set as a looping vector.

    Looping needs both answers, a repeating failure and no new artifacts, so
    the weaker of the two is the probability. The basis answer is the
    confidence, and an answer set claiming no basis has none at all.
    """
    looping = min(narrow[SAME_FAILURE], narrow[NO_NEW_ARTIFACTS])
    return ProgressVerdict(
        p_looping=looping,
        p_progressing=1.0 - looping,
        confidence=narrow[HAS_BASIS] if has_basis(narrow) else 0.0,
        narrow=dict(narrow),
    )


@dataclass(frozen=True)
class ProgressSample:
    """What one interval read, and the commit the repository was on for it.

    `fact_progress` is the code-computed facts showing work this window (new
    or modified files, or a check that failed and then passed) and
    `start_known` is whether the window's start commit could be read on an
    attached HEAD. Both default to the values that leave the rule as it was.
    """

    shrunk_looping: float | None
    head_oid: str
    fact_progress: bool = False
    start_known: bool = True


def should_reap(history: Sequence[ProgressSample]) -> bool:
    """Whether the window's readings argue for ending this worker now.

    System Two, and the whole decision: the last two consecutive readings must
    both exceed the floor and the integration branch must not have advanced
    between them. A failed check has no reading and breaks the run, so a Jev
    outage resets the evidence instead of accumulating it, and a head that
    could not be read is not a head that stood still.

    The code facts have the last word over the readings: a window whose facts
    show new files or an improving check is not reaped however high the
    looping probability reads, and a window whose start commit is unknown
    argues for nothing. Neither is a threshold on Jev's confidence.
    """
    recent = list(history)[-_REAP_SAMPLES:]
    if len(recent) < _REAP_SAMPLES:
        return False
    if any(sample.fact_progress or not sample.start_known for sample in recent):
        return False
    if any(sample.shrunk_looping is None
           or sample.shrunk_looping <= _LOOPING_FLOOR for sample in recent):
        return False
    return bool(recent[0].head_oid) and len({s.head_oid for s in recent}) == 1


#: The progress question set's name in a record's `question_version`.
PROGRESS_QUESTIONS = "progress-v2"
#: What the progress site can do with a live worker.
PROGRESS_ACTIONS = frozenset({"continue", "reap"})


def build_progress_questions() -> dict:
    """Two narrow yes/nos about one worker window, plus their basis.

    The counting is done in code (see :func:`progress_facts`): the files, the
    commits, the check results and the command repetition reach Jev as
    trusted facts, and the log tail only as untrusted context.
    """
    return {
        SAME_FAILURE: noul_question(
            "Is this worker repeating the same failing step, with nothing new "
            "tried between the attempts?",
            ["The facts show the same failing command repeated and the log "
             "shows each attempt matching an earlier one.",
             "The log shows one error recurring with the same response each time."],
            ["Each attempt changes something: a file, a command, or the approach.",
             "No command failed more than once this window."],
        ),
        NO_NEW_ARTIFACTS: noul_question(
            "Do the trusted facts show this window producing nothing new: no new "
            "or modified files, no commits, and no improving check result?",
            ["The facts report zero new or modified files, zero commits, and no "
             "check that went from failing to passing."],
            ["The facts report new or modified files, a commit, or a check that "
             "now passes after failing."],
        ),
        HAS_BASIS: basis_question("these answers about this worker window"),
    }


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """One read-only git query: its exit status and output, or -1 when unrunnable."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout


def _touched(path: Path, start_time: float) -> bool:
    try:
        return path.is_file() and path.stat().st_mtime >= start_time
    except OSError:
        return False


def _harness_file(name: str, log_path: Path, repo: Path) -> bool:
    """Whether a path is one the harness itself writes during the window."""
    if name.startswith(_HARNESS_LOG_PREFIX):
        return True
    try:
        return (repo / name).resolve() == log_path.resolve()
    except OSError:
        return False


def _window_files(
    repo: Path, start_head: str, start_time: float, log_path: Path,
) -> list[str] | None:
    """Files present now and written since the window began, or None when unknown.

    Tracked changes come from the diff against the start commit, untracked
    files from git's own listing; either is kept only when its mtime falls
    inside the window, so edits a previous window left behind do not count.
    The worker log and the Jev logs are the harness's own writes, not the
    worker's artifacts, and are left out. Only names are read, never contents.
    """
    if not start_head:
        return None
    status, diff = _git(repo, "diff", "--name-only", "--no-renames", start_head, "--")
    listed, untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if status != 0 or listed != 0:
        return None
    names = [*diff.splitlines(), *untracked.split("\0")]
    return sorted({
        name for name in names
        if name and not _harness_file(name, log_path, repo)
        and _touched(repo / name, start_time)
    })


def _commits(repo: Path, start_head: str) -> tuple[int | None, bool]:
    """Commits on HEAD since the start commit, and whether HEAD is detached.

    The count is None for a missing start head, a detached HEAD, or a
    directory git cannot read.
    """
    attached, _ = _git(repo, "symbolic-ref", "-q", "HEAD")
    detached = attached == 1
    if not start_head or attached != 0:
        return None, detached
    status, out = _git(repo, "rev-list", "--count", f"{start_head}..HEAD")
    try:
        return (int(out.strip()) if status == 0 else None), detached
    except ValueError:
        return None, detached


def shell_runs(lines: Sequence[str]) -> tuple[list[tuple[str, bool | None]], bool]:
    """The window's shell commands in order, each with whether it passed.

    Reads the Claude stream-json shape (a Bash `tool_use` joined to its
    `tool_result` by id) and the Codex shape (a completed
    `command_execution` item with its exit code). A command whose result is
    not in the window reads as None. The second value says whether any line
    parsed at all, so a malformed log is reported rather than read as idle.
    """
    runs: list[list[Any]] = []
    pending: dict[str, int] = {}
    parsed = False
    for line in lines:
        obj = _json_object(line.strip()) if line.strip() else None
        if obj is None:
            continue
        parsed = True
        item = obj.get("item")
        if isinstance(item, Mapping) and item.get("type") == "command_execution":
            command, code = item.get("command"), item.get("exit_code")
            if obj.get("type") == "item.completed" and isinstance(command, str):
                runs.append([command, code == 0 if type(code) is int else None])
            continue
        message = obj.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        for part in content if isinstance(content, list) else ():
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "tool_use" and part.get("name") == "Bash":
                arguments = part.get("input")
                command = arguments.get("command") if isinstance(arguments, Mapping) else None
                if isinstance(command, str):
                    runs.append([command, None])
                    if isinstance(part.get("id"), str):
                        pending[part["id"]] = len(runs) - 1
            elif part.get("type") == "tool_result":
                ref = part.get("tool_use_id")
                index = pending.pop(ref, None) if isinstance(ref, str) else None
                if index is not None:
                    runs[index][1] = part.get("is_error") is not True
    return [(" ".join(command.split()), ok) for command, ok in runs], parsed


def _checks(runs: Sequence[tuple[str, bool | None]]) -> list[bool]:
    """The results of the test and check commands among the runs, in order."""
    return [ok for command, ok in runs if ok is not None and _CHECK_COMMAND.search(command)]


def _result(ok: bool) -> str:
    return "pass" if ok else "fail"


def progress_facts(
    repo: Path, start_head: str, start_time: float, log_path: Path,
    start_offset: int = 0,
) -> dict[str, Any]:
    """What code can say about this worker window, for the trusted block.

    Computed from git and the worker's own log, never asked of Jev: files
    written since `start_time` (wall clock), commits since `start_head`, the
    last test or check command's result and the pass/fail trend, distinct
    against total shell commands and the longest run of one identical
    command, and whether one failing command repeated. Only file names and
    counts leave this function; command text and file contents do not.
    """
    runs, parsed = shell_runs(
        read_tail(log_path, start_offset=start_offset, max_lines=None)
    )
    commands = [command for command, _ in runs]
    checks = _checks(runs)
    failures = Counter(command for command, ok in runs if ok is False)
    longest = streak = 0
    previous: str | None = None
    for command in commands:
        streak = streak + 1 if command == previous else 1
        previous = command
        longest = max(longest, streak)
    files = _window_files(repo, start_head, start_time, log_path)
    commits, detached = _commits(repo, start_head)
    return {
        "log_readable": parsed,
        "new_or_modified_files": None if files is None else len(files),
        "file_paths": None if files is None else [
            name[:_PATH_CAP] for name in files[:_MAX_FACT_PATHS]
        ],
        "commits_since_start": commits,
        "detached_head": detached,
        "last_check": _result(checks[-1]) if checks else NONE_RUN,
        "check_trend": [_result(ok) for ok in checks[-_TREND_MAX:]],
        "check_improving": bool(checks) and checks[-1] and False in checks,
        "shell_commands": len(commands),
        "distinct_shell_commands": len(set(commands)),
        "longest_identical_run": longest,
        "same_failing_command_repeated": any(count >= 2 for count in failures.values()),
    }


def fact_progress(facts: Mapping[str, Any]) -> bool:
    """Whether the computed facts show work: new files or an improving check."""
    files = facts.get("new_or_modified_files")
    return (type(files) is int and files > 0) or facts.get("check_improving") is True


def last_check_result(log_path: Path, start_offset: int = 0) -> str:
    """The last test or check command's result this window: pass, fail or none-run."""
    runs, _ = shell_runs(read_tail(log_path, start_offset=start_offset, max_lines=None))
    checks = _checks(runs)
    return _result(checks[-1]) if checks else NONE_RUN


def read_tail(
    log_path: Path, *, start_offset: int = 0, max_lines: int | None = TAIL_MAX_LINES,
) -> list[str]:
    """This window's own log lines, newest last, bounded before any decoding.

    The read starts at the window's offset into a log earlier windows also
    wrote, and never more than :data:`_READ_BYTES` from the end. A line the
    byte bound cut in half is dropped rather than parsed. ``max_lines=None``
    keeps every line inside the byte bound, for counting.
    """
    try:
        with open(log_path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            begin = max(int(start_offset), size - _READ_BYTES, 0)
            if begin >= size:
                return []
            stream.seek(begin)
            raw = stream.read(size - begin)
    except OSError:
        return []
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if begin > int(start_offset) and lines:
        lines = lines[1:]
    return lines if max_lines is None else lines[-max_lines:]


def _json_object(line: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _summarize(line: str) -> str:
    """One log line as the short signature a loop would repeat.

    A stream-json line carries a whole assistant turn and its usage counts; a
    plain harness line carries its own text. Both collapse to a kind, the tool
    names in the turn and its prose, whitespace-flattened and capped, which is
    what makes repetition legible in the space a request has for it. A format
    this cannot read falls through to its raw text.
    """
    text = line.strip()
    if not text:
        return ""
    obj = _json_object(text)
    if obj is None:
        return text[:_LINE_CAP]
    kind = obj.get("type") or obj.get("event") or obj.get("role") or "event"
    body = obj.get("message") if isinstance(obj.get("message"), Mapping) else obj
    parts: list[str] = []
    content = body.get("content")
    for item in content if isinstance(content, list) else ():
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            parts.append(f"tool:{name.strip()}")
        for key in ("text", "thinking", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    for value in (content if isinstance(content, str) else None, obj.get("text")):
        if isinstance(value, str) and value.strip():
            parts.append(value)
    joined = " ".join(" ".join(part.split()) for part in parts)
    kind = " ".join(str(kind).split())
    return (f"{kind}: {joined}" if joined else kind)[:_LINE_CAP]


def _screen(text: str, config: JudgeConfig, secrets: tuple[str, ...]) -> str:
    """One summarized line, or the placeholder when it cannot travel."""
    try:
        screened = sanitize_field(
            text, cap=_LINE_CAP, secret_values=secrets,
            sensitive_paths=config.sensitive_paths,
        )
    except StateError:
        return REDACTED
    return REDACTED if screened.value is None else screened.value


def bounded_tail(
    lines: Sequence[str], config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
) -> str:
    """The lines as one readable block, screened line by line and bounded.

    The whole-field sanitizer omits any value carrying a credential, a key
    file or an address, which for a transcript would omit the transcript. Per
    line it keeps the signal instead: an offending line becomes the
    placeholder and its neighbours still travel. The newest lines are the ones
    kept when the byte bound bites, because the loop to detect is the one
    happening now.
    """
    secrets = environment_secrets(environ)
    rendered = [
        _screen(summary, config, secrets)
        for summary in (_summarize(line) for line in lines[-TAIL_MAX_LINES:])
        if summary
    ]
    kept: list[str] = []
    budget = TAIL_MAX_BYTES
    for line in reversed(rendered):
        cost = len(line.encode("utf-8")) + 1
        if cost > budget:
            break
        budget -= cost
        kept.append(line)
    return "\n".join(reversed(kept))


def _trusted(
    facts: WorkerProgress, window: Mapping[str, Any] | None, config: JudgeConfig,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    """The typed facts plus the window facts, with each file name screened."""
    trusted: dict[str, Any] = {**facts.payload(), **dict(window or {})}
    paths = trusted.get("file_paths")
    if isinstance(paths, list):
        secrets = environment_secrets(environ)
        trusted["file_paths"] = [
            _screen(str(path), config, secrets) for path in paths[:_MAX_FACT_PATHS]
        ]
    return trusted


def evaluate_progress(
    tail: str, facts: WorkerProgress, config: JudgeConfig,
    *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[[JudgeConfig], Any] = _default_client,
    window: Mapping[str, Any] | None = None,
) -> ProgressVerdict:
    """Ask the narrow progress questions about one window, or name the failure.

    `window` is :func:`progress_facts` for this window. It travels with the
    typed facts in the trusted block; the tail travels alone in the untrusted
    one. The caller bounds and screens the tail; this refuses one that is not
    bounded rather than sending it. The request carries no issue packet, so no
    issue prose travels with it.
    """
    if not isinstance(tail, str) or not isinstance(facts, WorkerProgress):
        raise StateError("invalid progress request")
    if len(tail.encode("utf-8")) > TAIL_MAX_BYTES:
        raise StateError("progress tail exceeds the bound")
    environ = judge_environment(environ)
    questions = build_progress_questions()
    payload = split_state(
        _trusted(facts, window, config, environ), {"log_tail": tail},
    )
    env = os.environ if environ is None else environ
    if not str(env.get(API_KEY_ENV, "")).strip():
        return ProgressVerdict(failure=JudgeFailure.KEY_MISSING)
    started = time.monotonic()

    def timed(verdict: ProgressVerdict, usage: JudgeUsage | None = None) -> ProgressVerdict:
        return replace(verdict, usage=usage,
                       latency_ms=max((time.monotonic() - started) * 1000.0, 0.0))

    async def ask() -> object:
        async with AsyncExitStack() as stack:
            client = client_factory(config)
            if hasattr(client, "__aenter__"):
                client = await stack.enter_async_context(client)
            elif hasattr(client, "aclose"):
                stack.push_async_callback(client.aclose)
            return await client.system_one(
                payload, questions,
                model=config.model, timeout=config.timeout_seconds,
            )

    async def bounded() -> object:
        return await asyncio.wait_for(ask(), config.timeout_seconds)

    try:
        response = asyncio.run(bounded())
    except ImportError:
        return timed(ProgressVerdict(failure=JudgeFailure.SDK_MISSING))
    except asyncio.TimeoutError:
        return timed(ProgressVerdict(failure=JudgeFailure.TIMEOUT))
    except Exception:  # noqa: BLE001 - provider prose never escapes
        return timed(ProgressVerdict(failure=JudgeFailure.SERVICE_ERROR))
    try:
        dump = getattr(response, "model_dump", None)
        body = _mapping(dump(mode="json") if callable(dump) else response)
        if body.get("model") != config.model:
            raise _Invalid
        narrow = _narrow(_mapping(body.get("answers")), questions)
        return timed(_reading(narrow), _usage(body.get("usage")))
    except Exception:  # noqa: BLE001 - a malformed answer is a typed failure
        return timed(ProgressVerdict(failure=JudgeFailure.INVALID_ANSWER))


@dataclass(frozen=True)
class ProgressCheck:
    """One interval's record: the vector, the facts, and what was done."""

    run_id: UUID
    decision_id: UUID
    issue_id: str
    mode: ProgressMode
    facts: WorkerProgress
    verdict: ProgressVerdict
    would_reap: bool
    reaped: bool
    #: The code-computed window facts the request carried, if any.
    window: Mapping[str, Any] | None = None


#: The window facts a progress record keeps: counts and results, no names.
_LOGGED_FACTS = (
    "log_readable", "new_or_modified_files", "commits_since_start",
    "detached_head", "last_check", "check_trend", "check_improving",
    "shell_commands", "distinct_shell_commands", "longest_identical_run",
    "same_failing_command_repeated",
)


@dataclass(frozen=True)
class ProgressWindow:
    """How the watched window ended, joined to its checks by ``run_id``."""

    run_id: UUID
    decision_id: UUID
    issue_id: str
    mode: ProgressMode
    outcome: WindowOutcome
    checks: int
    would_reap: int
    reaped: bool


def log_progress_check(
    repo: Path, config: JudgeConfig, event: ProgressCheck,
    *, environ: Mapping[str, str] | None = None,
) -> None:
    """Append one check beside the phase records, vector and failure alike.

    A shadow record carries the decision that was not taken, which is the
    whole point of the mode: ``would_reap`` is what the rule said and
    ``reaped`` is what happened to the worker.
    """
    payload = _common("progress", event.run_id, event.decision_id)
    verdict = event.verdict
    payload.update({
        "phase": "progress", "mode": event.mode.value,
        "issue_id": _clean_metadata(event.issue_id, config, environ),
        "seat": _clean_metadata(config.seat, config, environ),
        "model": config.model,
        **event.facts.payload(),
        "vector": {
            "looping": verdict.p_looping, "progressing": verdict.p_progressing,
        },
        "confidence": verdict.confidence,
        "shrunk_looping": verdict.shrunk_looping,
        "failure": verdict.failure.value if verdict.failure is not None else None,
        "would_reap": event.would_reap,
        "reaped": event.reaped,
        "window_facts": (None if event.window is None else {
            key: event.window.get(key) for key in _LOGGED_FACTS
        }),
        # Without Jev nothing reaps a worker; `should_reap` is the rule that
        # reads Jev's readings, so it names Jev's action.
        **side_fields(
            PROGRESS_ACTIONS, baseline_action="continue",
            jev_action=(None if verdict.failure is not None
                        else "reap" if event.would_reap else "continue"),
            question_version=question_version(
                PROGRESS_QUESTIONS, build_progress_questions(),
            ),
        ),
        **narrow_fields(verdict.narrow),
        **request_fields(verdict.latency_ms, verdict.usage),
    })
    _append(repo, payload, name=PROGRESS_LOG_NAME)


def log_progress_window(
    repo: Path, config: JudgeConfig, event: ProgressWindow,
    *, environ: Mapping[str, str] | None = None,
) -> None:
    """Append the window's fate, so a shadow reading can be scored against it."""
    payload = _common("progress_window", event.run_id, event.decision_id)
    payload.update({
        "phase": "progress", "mode": event.mode.value,
        "issue_id": _clean_metadata(event.issue_id, config, environ),
        "seat": _clean_metadata(config.seat, config, environ),
        "outcome": event.outcome.value,
        "checks": event.checks,
        "would_reap": event.would_reap,
        "reaped": event.reaped,
        # The window summarizes its checks and asks nothing itself.
        **side_fields(
            PROGRESS_ACTIONS, baseline_action="continue",
            jev_action="reap" if event.would_reap else "continue",
        ),
        **request_fields(None, None),
    })
    _append(repo, payload, name=PROGRESS_LOG_NAME)


@dataclass
class ProgressWatch:
    """One worker window's progress signal: the interval, the samples, the log.

    Built by the caller that owns the reap poll and asked on every poll, so the
    interval gate is a clock read and the Jev request happens at most once per
    interval, bounded by ``judge.timeout_seconds``. The tracker and repository
    are reached through callables that answer with a default rather than
    raising: an unanswered fact must never end a live worker.
    """

    repo: Path
    config: JudgeConfig
    mode: ProgressMode
    run_id: UUID
    issue_id: str
    log_path: Path
    head_oid: Callable[[], str]
    bead_status: Callable[[], OutcomeStatus]
    write_log: Callable[[str], None]
    interval: float = DEFAULT_PROGRESS_INTERVAL_S
    start_offset: int = 0
    clock: Callable[[], float] = time.monotonic
    client_factory: Callable[[JudgeConfig], Any] = _default_client
    environ: Mapping[str, str] | None = None
    decision_id: UUID = field(default_factory=uuid4)
    samples: list[ProgressSample] = field(default_factory=list)
    checks: int = 0
    would_reap: int = 0
    reaped: bool = False
    started: float = field(init=False, default=0.0)
    last_check: float = field(init=False, default=0.0)
    start_head: str = field(init=False, default="")
    #: Wall-clock start, which file mtimes are compared against.
    wall_start: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.started = self.clock()
        self.last_check = self.started
        self.wall_start = time.time()
        self.start_head = self._head()

    def reason(self) -> str | None:
        """Why this worker should be reaped for looping now, or None.

        Shadow records and returns None however the vector reads, and a window
        that has already produced a reap reason is not asked again.
        """
        if self.reaped or self.mode is ProgressMode.OFF:
            return None
        now = self.clock()
        if now - self.last_check < self.interval:
            return None
        self.last_check = now
        try:
            return self._check(now)
        except Exception as exc:  # noqa: BLE001 - a poll never ends a worker
            self.write_log(
                f"jev progress: check failed ({exc}); the worker keeps its window"
            )
            return None

    def _check(self, now: float) -> str | None:
        lines = read_tail(self.log_path, start_offset=self.start_offset)
        tail = bounded_tail(lines, self.config, environ=self.environ) if lines else ""
        if not tail:
            self.write_log("jev progress: no readable worker log yet; check skipped")
            return None
        head = self._head()
        facts = WorkerProgress(
            elapsed_seconds=max(now - self.started, 0.0),
            head_advanced=bool(head and self.start_head and head != self.start_head),
            bead_status=self._status(),
        )
        window = progress_facts(
            self.repo, self.start_head, self.wall_start, self.log_path,
            self.start_offset,
        )
        verdict = evaluate_progress(
            tail, facts, self.config,
            environ=self.environ, client_factory=self.client_factory,
            window=window,
        )
        self.checks += 1
        self.samples.append(ProgressSample(
            verdict.shrunk_looping, head,
            fact_progress=fact_progress(window),
            start_known=bool(self.start_head) and not window["detached_head"],
        ))
        argued = should_reap(self.samples)
        self.would_reap += int(argued)
        self.reaped = argued and self.mode is ProgressMode.ENFORCE
        self._record(facts, verdict, argued, window)
        if verdict.failure is not None:
            self.write_log(
                f"jev progress: {verdict.failure.value}; check skipped "
                "and the worker keeps its window"
            )
            return None
        self.write_log(
            f"jev progress check {self.checks} on {self.issue_id}: "
            f"looping={verdict.shrunk_looping:.3f} "
            f"advanced={facts.head_advanced} would_reap={argued} "
            f"mode={self.mode.value}"
        )
        if not self.reaped:
            return None
        return (
            f"{JEV_LOOPING_REASON} (looping={verdict.shrunk_looping:.2f} over "
            f"{_REAP_SAMPLES} checks with no new commits, files or passing checks)"
        )

    def record_window(self, outcome: WindowOutcome) -> None:
        """Append how the window ended, once, for the checks it actually made."""
        if not self.checks:
            return
        try:
            log_progress_window(self.repo, self.config, ProgressWindow(
                run_id=self.run_id, decision_id=self.decision_id,
                issue_id=self.issue_id, mode=self.mode, outcome=outcome,
                checks=self.checks, would_reap=self.would_reap,
                reaped=self.reaped,
            ), environ=self.environ)
        except Exception as exc:  # noqa: BLE001 - a log is not worth a run
            self.write_log(
                f"jev progress: could not record the window outcome ({exc})"
            )

    def _record(
        self, facts: WorkerProgress, verdict: ProgressVerdict, argued: bool,
        window: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            log_progress_check(self.repo, self.config, ProgressCheck(
                run_id=self.run_id, decision_id=self.decision_id,
                issue_id=self.issue_id, mode=self.mode, facts=facts,
                verdict=verdict, would_reap=argued, reaped=self.reaped,
                window=window,
            ), environ=self.environ)
        except Exception as exc:  # noqa: BLE001 - the decision still stands
            self.write_log(f"jev progress: could not record the check ({exc})")

    def _head(self) -> str:
        try:
            value = self.head_oid()
        except Exception:  # noqa: BLE001 - an unread head is not an advance
            return ""
        return value if isinstance(value, str) else ""

    def _status(self) -> OutcomeStatus:
        try:
            status = self.bead_status()
        except Exception:  # noqa: BLE001 - an unread status is unknown
            return OutcomeStatus.UNKNOWN
        return status if isinstance(status, OutcomeStatus) else OutcomeStatus.UNKNOWN
