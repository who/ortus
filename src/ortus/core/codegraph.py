"""Typed CodeGraph policy, probing, transcript normalization, and reporting.

CodeGraph is optional in ``auto`` mode, explicit in ``off`` mode, and a hard
prerequisite in ``required`` mode.  The parent process can observe the local
index and CLI; MCP registration is reconciled from each agent phase's JSONL.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
MAX_EVENTS = 50
MAX_LABEL = 120
MAX_SYMBOLS = 20


class CodeGraphMode(str, Enum):
    OFF = "off"
    AUTO = "auto"
    REQUIRED = "required"


class CodeGraphPhase(str, Enum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"


class CodeGraphUnavailable(RuntimeError):
    """Required-mode prerequisites or capability handshake were absent."""


@dataclass(frozen=True)
class CodeGraphProbe:
    mode: CodeGraphMode
    index_present: bool
    cli_present: bool
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class CodeGraphEvent:
    phase: str
    tool: str
    query: str
    success: bool
    hit: bool | None
    duration_ms: int | None = None
    fallback_reason: str | None = None
    truncated: bool = False


@dataclass
class CodeGraphSummary:
    phase: str
    probe: CodeGraphProbe
    events: list[CodeGraphEvent] = field(default_factory=list)
    freshness: str = "not-refreshed"
    sync_duration_ms: int | None = None
    fallbacks: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    impacted_callers: list[str] = field(default_factory=list)
    out_of_scope_callers: list[str] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)

    @property
    def capability_observed(self) -> bool:
        return bool(self.events)

    def report(self) -> str:
        tools = sorted({event.tool for event in self.events})
        misses = sum(event.hit is False for event in self.events)
        failures = sum(not event.success for event in self.events)
        return (
            f"**CodeGraph engagement v{SCHEMA_VERSION}**\n"
            f"- phase: {self.phase}\n"
            f"- mode: {self.probe.mode.value}\n"
            f"- availability: {'available' if self.probe.available else 'unavailable'}"
            f" (index={str(self.probe.index_present).lower()}, "
            f"cli={str(self.probe.cli_present).lower()})\n"
            f"- freshness: {self.freshness}; sync_duration_ms: "
            f"{self.sync_duration_ms if self.sync_duration_ms is not None else 'n/a'}\n"
            f"- tools: {', '.join(tools) if tools else 'none'}; "
            f"queries: {len(self.events)}; failures: {failures}; misses: {misses}\n"
            f"- symbols_reviewed: {_bounded_join(self.symbols)}\n"
            f"- impacted_callers: {_bounded_join(self.impacted_callers)}\n"
            f"- out_of_scope_callers: {_bounded_join(self.out_of_scope_callers)}\n"
            f"- unresolved_references: {_bounded_join(self.unresolved_references)}\n"
            f"- fallbacks: {_bounded_join(self.fallbacks)}\n"
            f"- caps: events={MAX_EVENTS}, symbols={MAX_SYMBOLS}, label_chars={MAX_LABEL}"
        )


def _bounded_join(values: Iterable[str]) -> str:
    bounded = [str(value)[:MAX_LABEL] for value in list(values)[:MAX_SYMBOLS]]
    return ", ".join(bounded) if bounded else "none"


class CodeGraphAdapter:
    """Outer-process adapter; replaceable with a fake in command tests."""

    def probe(self, repo: Path, mode: CodeGraphMode) -> CodeGraphProbe:
        if mode is CodeGraphMode.OFF:
            return CodeGraphProbe(mode, False, False, False, "disabled by policy")
        index = (repo / ".codegraph").is_dir()
        cli = shutil.which("codegraph") is not None
        available = index and cli
        missing = []
        if not index:
            missing.append("project index .codegraph/ is missing")
        if not cli:
            missing.append("codegraph CLI is not on PATH")
        probe = CodeGraphProbe(mode, index, cli, available, "; ".join(missing) or None)
        if mode is CodeGraphMode.REQUIRED and not available:
            raise CodeGraphUnavailable(
                f"CodeGraph required but unavailable: {probe.reason}. "
                "Run `codegraph init`/`codegraph sync`, install the CLI, and configure "
                "the agent's CodeGraph MCP server."
            )
        return probe

    def refresh(self, repo: Path, probe: CodeGraphProbe) -> tuple[str, int | None]:
        if not probe.available or probe.mode is CodeGraphMode.OFF:
            return "not-supported", None
        started = time.monotonic()
        proc = subprocess.run(
            ["codegraph", "sync"], cwd=repo, capture_output=True, text=True, check=False
        )
        duration = int((time.monotonic() - started) * 1000)
        return ("fresh" if proc.returncode == 0 else "sync-failed"), duration


def phase_contract(phase: CodeGraphPhase, probe: CodeGraphProbe) -> str:
    """Schema-backed instructions injected into every active agent phase."""
    if probe.mode is CodeGraphMode.OFF:
        return (
            "\n\n## CodeGraph phase contract\nCodeGraph policy is off. Do not call any "
            "CodeGraph tool; use repository Read/grep facilities."
        )
    policy = "required" if probe.mode is CodeGraphMode.REQUIRED else "auto"
    fallback = (
        "Failure or missing MCP capability is fatal; stop before repository work."
        if probe.mode is CodeGraphMode.REQUIRED
        else "If MCP is unavailable or a query fails, use grep/Read and state the fallback reason."
    )
    duties = {
        CodeGraphPhase.PLANNING: (
            "Orient to the repository; validate every file and symbol reference; trace "
            "dependencies and callers. Put concrete files, symbols, dependencies, callers, "
            "and unresolved references in each implementation-ready leaf issue."
        ),
        CodeGraphPhase.IMPLEMENTATION: (
            "Confirm the issue packet against repository reality and run an impact query "
            "before editing. Do not close the issue; leave candidate edits for verification."
        ),
        CodeGraphPhase.VERIFICATION: (
            "Independently query changed symbols, callers, callees, and impact radius. Compare "
            "the actual blast radius with the issue packet and diff, report out-of-scope callers, "
            "then add the verification comment and close only if all criteria pass."
        ),
    }[phase]
    return (
        f"\n\n## CodeGraph phase contract v{SCHEMA_VERSION}\n"
        f"Phase: {phase.value}; policy: {policy}; outer index+CLI probe: available. "
        "Before other work, test the registered CodeGraph MCP capability with one bounded "
        "repository-orientation query. This tool event is the capability handshake. "
        f"{fallback} {duties} Keep each query label under {MAX_LABEL} characters and never "
        "copy full source payloads into progress or bd comments."
    )


def parse_transcript(
    path: Path,
    *,
    phase: CodeGraphPhase,
    probe: CodeGraphProbe,
    start_offset: int = 0,
) -> CodeGraphSummary:
    """Normalize Claude/Codex JSONL CodeGraph calls without retaining payloads."""
    summary = CodeGraphSummary(phase.value, probe)
    if probe.mode is CodeGraphMode.OFF:
        summary.fallbacks.append("disabled by policy")
        return summary
    if not probe.available:
        summary.fallbacks.append(probe.reason or "outer prerequisites unavailable")
        return summary
    if not path.exists():
        summary.fallbacks.append("agent transcript missing")
        return summary
    pending: dict[str, tuple[str, str, bool]] = {}
    with path.open("rb") as fh:
        fh.seek(start_offset)
        for raw in fh:
            try:
                obj = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            for tool_id, name, arguments, result, error in _tool_records(obj):
                if tool_id in pending and not name:
                    name, prior_query, prior_truncated = pending[tool_id]
                    if arguments is None:
                        query, truncated = prior_query, prior_truncated
                    else:
                        query, truncated = _query_label(arguments)
                else:
                    query, truncated = _query_label(arguments)
                if "codegraph" not in name.lower():
                    continue
                if result is None and error is None:
                    pending[tool_id] = (name, query, truncated)
                    continue
                if tool_id in pending:
                    name, query, truncated = pending.pop(tool_id)
                success = not bool(error)
                summary.events.append(
                    CodeGraphEvent(
                        phase.value,
                        name[:MAX_LABEL],
                        query,
                        success,
                        _hit(result) if success else None,
                        fallback_reason="tool error" if error else None,
                        truncated=truncated,
                    )
                )
                if query != "query" and query not in summary.symbols:
                    summary.symbols.append(query)
                if ("caller" in name.lower() or "impact" in name.lower()) and success:
                    summary.impacted_callers.append(query)
                if not success:
                    summary.fallbacks.append(f"{name[:MAX_LABEL]}: tool error")
                if len(summary.events) >= MAX_EVENTS:
                    break
            if len(summary.events) >= MAX_EVENTS:
                break
    if not summary.events:
        summary.fallbacks.append("agent MCP capability handshake not observed")
    return summary


def append_normalized(log_path: Path, summary: CodeGraphSummary) -> None:
    """Append bounded lifecycle records to the command transaction journal."""
    records: list[dict[str, Any]] = [
        {
            "type": "ortus.codegraph",
            "schema": SCHEMA_VERSION,
            "kind": "phase_summary",
            "phase": summary.phase,
            "available": summary.probe.available,
            "freshness": summary.freshness,
            "query_count": len(summary.events),
            "fallbacks": summary.fallbacks[:5],
        }
    ]
    records.extend(
        {"type": "ortus.codegraph", "schema": SCHEMA_VERSION, "kind": "query", **asdict(e)}
        for e in summary.events
    )
    with log_path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def require_handshake(summary: CodeGraphSummary) -> None:
    if summary.probe.mode is CodeGraphMode.REQUIRED and not summary.capability_observed:
        raise CodeGraphUnavailable(
            f"CodeGraph required but the {summary.phase} agent reported no CodeGraph MCP "
            "tool capability. Configure the MCP server for this backend and retry."
        )


def _query_label(arguments: object) -> tuple[str, bool]:
    if isinstance(arguments, dict):
        for key in ("query", "symbol", "name", "path"):
            if key in arguments:
                value = str(arguments[key])
                return value[:MAX_LABEL], len(value) > MAX_LABEL
        value = ",".join(sorted(str(key) for key in arguments)) or "query"
    elif arguments is None:
        value = "query"
    else:
        value = str(arguments)
    return value[:MAX_LABEL], len(value) > MAX_LABEL


def _hit(result: object) -> bool:
    if result is None or result == "" or result == [] or result == {}:
        return False
    if isinstance(result, str):
        try:
            return _hit(json.loads(result))
        except (json.JSONDecodeError, ValueError):
            pass
    text = json.dumps(result, ensure_ascii=False).lower()
    return text not in ("[]", "{}", '""') and '"results":[]' not in text.replace(" ", "")


def _tool_records(obj: object) -> Iterable[tuple[str, str, object, object, object]]:
    """Yield id/name/arguments/result/error across Claude and Codex schemas."""
    if not isinstance(obj, dict):
        return
    kind = obj.get("type")
    if kind == "assistant":
        content = (obj.get("message") or {}).get("content", [])
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                yield str(part.get("id", "")), str(part.get("name", "")), part.get("input"), None, None
    elif kind == "user":
        content = (obj.get("message") or {}).get("content", [])
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                yield str(part.get("tool_use_id", "")), "", None, part.get("content"), part.get("is_error")
    elif kind in ("item.started", "item.completed"):
        item = obj.get("item") or {}
        if isinstance(item, dict) and item.get("type") in ("mcp_tool_call", "tool_call"):
            name = str(item.get("tool", item.get("name", "")))
            server = str(item.get("server", ""))
            full_name = f"{server}.{name}" if server else name
            completed = kind == "item.completed"
            yield (
                str(item.get("id", "")),
                full_name,
                item.get("arguments", item.get("input")),
                item.get("result") if completed else None,
                item.get("error") if completed else None,
            )
