"""A closed failure taxonomy for one worker window (ortus-jlxt).

`ortus grind` already knows how every worker window ended — an exit status,
or the exception that killed the process group — and already tees the
worker's JSONL stream and its own marker lines into `logs/grind-*.log`. What
it has never had is a stable name for *why* a window ended badly. Without
one, a Codex sandbox `EPERM`, a DNS `EAI_AGAIN`, a provider 429 and a
genuine Ortus defect all read alike downstream, and nothing can answer
whether last week's failures belonged to the environment or to the harness.

Two rules keep the rates honest.

Unknown is not a residual bucket to hide in. A window that failed and
matched no signal is `ortus_harness_bug`, not "other": if Ortus cannot say
why its own worker died, that silence is the defect, and a rate that climbs
is the signal to add the missing signature here rather than to widen a
catch-all.

Classification is Ortus-side, over what is already on disk. No worker is
ever asked to label its own failure, because the failures worth counting are
exactly the ones where the worker was in no position to report anything —
killed by a watchdog, refused by a sandbox before it ran, or cut off
mid-stream by a provider.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class WorkerFailure(str, Enum):
    """Why one worker window failed. Closed set; unrecognized is a harness bug."""

    INVALID_ARGS = "invalid_args"
    ENVIRONMENT = "environment"
    PROVIDER_API = "provider_api"
    TIMEOUT = "timeout"
    USER_ABORT = "user_abort"
    CLAIM_COLLISION = "claim_collision"
    ORTUS_HARNESS_BUG = "ortus_harness_bug"


#: A profile marker's placeholder for "no override; the CLI picks the model".
#: The failure marker reuses it so both lines read the same way in a log.
PROVIDER_DEFAULT = "provider-default"

#: How much of a worker's log slice is scanned. A failing window names itself
#: at the end of its stream, so an oversized slice is read from the tail
#: rather than sampled: the launch that never started is short anyway, and a
#: long window that died late is described by its last bytes, not its first.
SCAN_LIMIT = 256 * 1024

#: One detail string's budget in the marker line.
MAX_DETAIL_CHARS = 160

# Most specific first. The two leaders are decided outside the worker stream
# entirely — the harness killed the process, or the operator did — so nothing
# a dying stream happens to echo can outrank them. Launch-level faults come
# next because they explain whatever the stream logged afterwards: a worker
# whose spawns are refused will also fail to reach a provider. Queue state
# follows, and provider noise sits last: it is the most commonly echoed
# signal and the least specific about whose fault the window was.
PRECEDENCE: tuple[WorkerFailure, ...] = (
    WorkerFailure.TIMEOUT,
    WorkerFailure.USER_ABORT,
    WorkerFailure.ENVIRONMENT,
    WorkerFailure.INVALID_ARGS,
    WorkerFailure.CLAIM_COLLISION,
    WorkerFailure.PROVIDER_API,
    WorkerFailure.ORTUS_HARNESS_BUG,
)


def _signal(failure: WorkerFailure, label: str, pattern: str) -> tuple[WorkerFailure, str, re.Pattern[str]]:
    return failure, label, re.compile(pattern, re.IGNORECASE)


#: Substrings that already appear in grind logs, each naming one class. A
#: provider credential that is missing or rejected is counted as environment
#: rather than provider_api on purpose: the provider is answering correctly,
#: and what has to change is the operator's machine.
SIGNALS: tuple[tuple[WorkerFailure, str, re.Pattern[str]], ...] = (
    _signal(WorkerFailure.TIMEOUT, "watchdog killed the worker", r"TimeoutExpired|worker TIMEOUT after"),
    _signal(WorkerFailure.TIMEOUT, "operation timed out", r"timed out after|deadline exceeded|\bETIMEDOUT\b"),
    _signal(WorkerFailure.USER_ABORT, "operator interrupt", r"KeyboardInterrupt|\bSIGINT\b"),
    _signal(WorkerFailure.USER_ABORT, "aborted by the operator", r"(?:aborted|interrupted|cancell?ed) by (?:the )?user"),
    _signal(WorkerFailure.ENVIRONMENT, "sandbox denied a spawn", r"\bEPERM\b|spawnSync|operation not permitted"),
    _signal(WorkerFailure.ENVIRONMENT, "sandbox failed to initialize", r"sandbox is required but failed to initialize"),
    _signal(WorkerFailure.ENVIRONMENT, "name resolution failed", r"\bEAI_AGAIN\b|\bENOTFOUND\b|getaddrinfo|temporary failure in name resolution"),
    _signal(WorkerFailure.ENVIRONMENT, "a required file or binary is missing", r"\bENOENT\b|command not found|no such file or directory"),
    _signal(WorkerFailure.ENVIRONMENT, "access denied on this machine", r"\bEACCES\b|permission denied"),
    _signal(WorkerFailure.ENVIRONMENT, "host unreachable from this machine", r"\bECONNREFUSED\b|network is unreachable"),
    _signal(WorkerFailure.ENVIRONMENT, "no room left on the device", r"\bENOSPC\b|no space left on device"),
    _signal(WorkerFailure.ENVIRONMENT, "provider credential missing or rejected", r"authentication_error|invalid[_ ]api[_ ]key|\b401 unauthorized\b"),
    _signal(WorkerFailure.INVALID_ARGS, "the backend rejected the argv Ortus built", r"unrecognized arguments?|unknown (?:option|flag|argument)|no such option|unexpected argument|invalid value for"),
    _signal(WorkerFailure.CLAIM_COLLISION, "the claim was already taken", r"already in_progress|already claimed|claim collision|assigned to (?:another|a different)"),
    _signal(WorkerFailure.PROVIDER_API, "provider rate limit", r"rate[ _-]?limit|too many requests|insufficient_quota|quota exceeded"),
    _signal(WorkerFailure.PROVIDER_API, "provider server error", r"overloaded_error|api_error|server_error|service[ _]unavailable|bad gateway|internal server error|upstream connect error"),
    _signal(WorkerFailure.PROVIDER_API, "provider reported an error status", r"(?:status|code|http)\D{0,12}(?:429|5\d{2})\b"),
    _signal(WorkerFailure.PROVIDER_API, "provider is shedding load", r"experiencing high demand"),
)

#: Exception types the launch itself can raise, in isinstance order: the
#: narrower OSError subclasses have to be tested before anything broader.
EXCEPTIONS: tuple[tuple[type[BaseException], WorkerFailure, str], ...] = (
    (subprocess.TimeoutExpired, WorkerFailure.TIMEOUT, "watchdog killed the worker process group"),
    (KeyboardInterrupt, WorkerFailure.USER_ABORT, "operator interrupt"),
    (FileNotFoundError, WorkerFailure.ENVIRONMENT, "the backend binary is not on PATH"),
    (PermissionError, WorkerFailure.ENVIRONMENT, "the launch was denied by the operating system"),
)

# Exit statuses that name a class on their own. 137 and 143 are deliberately
# absent: grind's own reap kills a healthy worker's process group the same way
# the watchdog kills a hung one, so those codes cannot tell the two apart. A
# real timeout arrives here as `timed_out` or as TimeoutExpired instead.
EXIT_CODES: dict[int, tuple[WorkerFailure, str]] = {
    2: (WorkerFailure.INVALID_ARGS, "exit 2 (usage error)"),
    124: (WorkerFailure.TIMEOUT, "exit 124 (killed by a timeout wrapper)"),
    126: (WorkerFailure.ENVIRONMENT, "exit 126 (backend binary is not executable)"),
    127: (WorkerFailure.ENVIRONMENT, "exit 127 (backend binary not found)"),
    130: (WorkerFailure.USER_ABORT, "exit 130 (SIGINT)"),
}

#: The marker grind writes and `ortus.core.cost` reads back. Both halves live
#: here so the writer and the parser can never drift apart.
FAILURE_LINE = re.compile(
    r"^iter (?P<iteration>\d+): worker failure class=(?P<failure>[a-z_]+) "
    r"backend=(?P<backend>[A-Za-z0-9_-]+) model=(?P<model>\S+)"
)


@dataclass(frozen=True)
class WindowFailure:
    """The class one failed worker window is counted under."""

    failure: WorkerFailure
    #: The signal that named the class, for a human reading the log.
    detail: str = ""
    #: Every other class this window also showed evidence for.
    secondary: tuple[WorkerFailure, ...] = ()

    @property
    def unclassified(self) -> bool:
        """Whether Ortus failed to explain its own worker's death."""

        return self.failure is WorkerFailure.ORTUS_HARNESS_BUG


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_DETAIL_CHARS else flat[:MAX_DETAIL_CHARS] + "…"


def _log_signals(text: str) -> list[tuple[WorkerFailure, str]]:
    """Every class the log slice shows evidence for, first evidence each."""

    found: list[tuple[WorkerFailure, str]] = []
    seen: set[WorkerFailure] = set()
    for line in text.splitlines():
        for failure, label, pattern in SIGNALS:
            if failure in seen or pattern.search(line) is None:
                continue
            seen.add(failure)
            found.append((failure, f"{label}: {_clip(line)}"))
    return found


def classify_worker_failure(
    *,
    exit_code: int | None = None,
    exception: BaseException | None = None,
    timed_out: bool = False,
    log_text: str = "",
) -> WindowFailure | None:
    """Name why this worker window failed, or None when it did not fail.

    Signals are gathered from all three sources Ortus observes — how the
    launch ended, what it raised, and what its stream said — and then ranked
    by `PRECEDENCE`. The winner is the window's class; every other class with
    evidence is kept as `secondary` so a window that both lost its network
    and then reported a provider error is not remembered as only one of them.
    """

    if not timed_out and exception is None and exit_code in (None, 0):
        return None

    signals: list[tuple[WorkerFailure, str]] = []
    if timed_out:
        signals.append((WorkerFailure.TIMEOUT, "the grind watchdog killed this window"))
    if exception is not None:
        for kind, failure, label in EXCEPTIONS:
            if isinstance(exception, kind):
                signals.append((failure, label))
                break
    signals.extend(_log_signals(log_text))
    if exit_code is not None and exit_code in EXIT_CODES:
        signals.append(EXIT_CODES[exit_code])

    if not signals:
        status = "no exit status" if exit_code is None else f"exit {exit_code}"
        return WindowFailure(
            failure=WorkerFailure.ORTUS_HARNESS_BUG,
            detail=f"no known failure signal in this window ({status})",
        )

    ranked = sorted(signals, key=lambda item: PRECEDENCE.index(item[0]))
    failure, detail = ranked[0]
    secondary = tuple(
        dict.fromkeys(other for other, _ in ranked[1:] if other is not failure)
    )
    return WindowFailure(failure=failure, detail=detail, secondary=secondary)


def read_log_slice(
    log_path: Path, *, start_offset: int = 0, limit: int = SCAN_LIMIT
) -> str:
    """This window's slice of the run log, bounded to its last `limit` bytes.

    A log that cannot be read yields no text rather than an exception: a
    classification is telemetry, and telemetry must never be the thing that
    ends a run.
    """

    try:
        with log_path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(max(start_offset, end - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def classify_worker_window(
    *,
    exit_code: int | None,
    timed_out: bool = False,
    log_path: Path | None = None,
    start_offset: int = 0,
) -> WindowFailure | None:
    """`classify_worker_failure` against a window's own slice of the run log."""

    text = (
        read_log_slice(log_path, start_offset=start_offset)
        if log_path is not None
        else ""
    )
    return classify_worker_failure(
        exit_code=exit_code, timed_out=timed_out, log_text=text
    )


def failure_log_line(
    failure: WindowFailure,
    *,
    iteration: int,
    backend: str,
    model: str | None = None,
) -> str:
    """The marker line grind writes for a failed window.

    Backend and model ride in the line itself so the rollup can report rates
    per backend and model without re-deriving which profile served which
    window, and the detail trails so a longer signal can never push a parsed
    field off the end.
    """

    parts = [
        f"iter {iteration}: worker failure class={failure.failure.value}",
        f"backend={backend}",
        f"model={model or PROVIDER_DEFAULT}",
    ]
    if failure.secondary:
        parts.append(
            "also=" + ",".join(other.value for other in failure.secondary)
        )
    parts.append(f"detail={_clip(failure.detail) or 'none'}")
    return " ".join(parts)
