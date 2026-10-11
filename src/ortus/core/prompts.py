"""Three-layer prompt resolution (FR-025).

Precedence (first existing file wins):
  1. <repo>/.ortus/prompts/<name>.md   per-repo override
  2. ~/.ortus/prompts/<name>.md        user-wide override
  3. bundled src/ortus/prompts/<name>.md  installed default (package data)

Bundled prompts are loaded via importlib.resources so they survive
wheel/sdist installs without filesystem assumptions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from string import Template

PROMPT_PACKAGE = "ortus.prompts"

# The audited rewrite of the worker-facing texts (goal prompt, work-issue
# condition) lives in a subpackage rather than beside the legacy bundles:
# `prompts_in_package()` enumerates only `*.md` directly under the package, so
# a variant cannot ship as an unnamed fourth registry entry. `ortus.core
# .prompt_audit` owns which variant a repository serves.
AUDITED_PROMPT_PACKAGE = "ortus.prompts.audited"

# The token the bundled plan prompt carries in place of the generated readiness
# contract. `ortus check` reads it to spot an override copied before it existed.
READINESS_SPEC_PLACEHOLDER = "$readiness_spec"


@dataclass(frozen=True)
class PromptInfo:
    """One bundled runtime prompt as the registry names it."""

    name: str  # short user-facing name, e.g. "goal"
    filename: str  # bundled stem without .md, e.g. "goal-prompt"
    phase: str  # workflow phase the prompt drives
    description: str  # one line for `ortus prompt list`


# The only enumerator of the bundled runtime prompts. Every `*.md` directly
# under src/ortus/prompts/ (not conditions/) has exactly one entry here;
# tests reconcile the two so a new file cannot ship unnamed.
PROMPT_REGISTRY: tuple[PromptInfo, ...] = (
    PromptInfo(
        name="goal",
        filename="goal-prompt",
        phase="implementation",
        description="One-issue worker loop grind points /goal workers at.",
    ),
    PromptInfo(
        name="interview",
        filename="interview-prompt",
        phase="interview",
        description="Interactive PRD-building interview script.",
    ),
    PromptInfo(
        name="plan",
        filename="plan-prompt",
        phase="planning",
        description="PRD decomposition into readiness-v1 bd issues.",
    ),
)


@dataclass(frozen=True)
class ResolvedPrompt:
    """A resolved prompt with its on-disk source and content."""

    name: str
    source: str  # "repo", "user", or "bundled"
    path: Path | None  # None for bundled (lives inside the package, may be a zip)
    text: str


class PromptNotFound(LookupError):
    """Raised when no prompt by that name exists in any layer."""


def registry_entry(name: str) -> PromptInfo:
    """The registry entry for a short prompt name, or PromptNotFound.

    The error names the valid choices so a CLI caller can print it verbatim.
    """
    for entry in PROMPT_REGISTRY:
        if entry.name == name:
            return entry
    valid = ", ".join(entry.name for entry in PROMPT_REGISTRY)
    raise PromptNotFound(f"unknown prompt {name!r}; valid names: {valid}")


def prompts_in_package() -> tuple[str, ...]:
    """Stems of every bundled `*.md` directly under the prompt package.

    Excludes conditions/ by construction (subdirectories are not files).
    Works on both unpacked and zip-backed installs via Traversable.
    """
    root = files(PROMPT_PACKAGE)
    return tuple(
        sorted(
            entry.name[: -len(".md")]
            for entry in root.iterdir()
            if entry.is_file() and entry.name.endswith(".md")
        )
    )


def resolve_named_prompt(
    name: str,
    *,
    repo: Path | None = None,
    home: Path | None = None,
    audited: bool = False,
) -> ResolvedPrompt:
    """Thin registry-name alias for resolve_prompt (`goal` -> `goal-prompt`)."""
    return resolve_prompt(
        registry_entry(name).filename, repo=repo, home=home, audited=audited
    )


def _repo_layer_path(repo: Path, name: str) -> Path:
    return repo / ".ortus" / "prompts" / f"{name}.md"


def _user_layer_path(home: Path, name: str) -> Path:
    return home / ".ortus" / "prompts" / f"{name}.md"


def resolve_prompt(
    name: str,
    *,
    repo: Path | None = None,
    home: Path | None = None,
    audited: bool = False,
) -> ResolvedPrompt:
    """Resolve <name>-prompt.md across the three layers.

    Args:
        name: prompt basename without the .md extension (e.g., "plan-prompt").
        repo: per-repo override root. When None, the repo layer is skipped.
        home: user-wide override root (defaults to Path.home()).
        audited: read the audited variant of the bundled layer. The override
            layers are unaffected — an operator's copy still wins, and the
            flag only selects which bundled default it replaces — and the
            resolved source reads "audited" so a caller can report which
            variant it served.

    Returns:
        ResolvedPrompt naming the layer that won and its content.

    Raises:
        PromptNotFound: if the bundled layer is missing too — indicates a
            broken install. Repo and user layers are optional by design.
    """
    if home is None:
        home = Path.home()

    if repo is not None:
        candidate = _repo_layer_path(repo, name)
        if candidate.is_file():
            return ResolvedPrompt(
                name=name, source="repo", path=candidate, text=candidate.read_text(encoding="utf-8")
            )

    candidate = _user_layer_path(home, name)
    if candidate.is_file():
        return ResolvedPrompt(
            name=name, source="user", path=candidate, text=candidate.read_text(encoding="utf-8")
        )

    package = AUDITED_PROMPT_PACKAGE if audited else PROMPT_PACKAGE
    bundled = files(package).joinpath(f"{name}.md")
    if not bundled.is_file():
        raise PromptNotFound(
            f"{name}.md is not in any of: "
            f"{_repo_layer_path(repo, name) if repo else '(no repo)'}, "
            f"{_user_layer_path(home, name)}, "
            f"bundled {package}"
        )
    # importlib.resources.Traversable: read_text() works on both
    # filesystem and zip-backed packages.
    bundled_text = bundled.read_text(encoding="utf-8")
    bundled_path: Path | None
    try:
        # When the package is unpacked on disk, Traversable resolves to a Path.
        bundled_path = Path(str(bundled))
        if not bundled_path.is_file():
            bundled_path = None
    except (TypeError, ValueError):
        bundled_path = None
    return ResolvedPrompt(
        name=name,
        source="audited" if audited else "bundled",
        path=bundled_path,
        text=bundled_text,
    )


def bundled_prompt_text(name: str, *, audited: bool = False) -> str:
    """The bundled text for a prompt stem, bypassing every override layer.

    Eject and staleness checks both need the installed default even when a
    repo or user override currently wins resolution (source purity). Those two
    callers stay on the legacy bundle — it is the provenance baseline a stamp
    records — and only a caller that asks for the audited variant gets it.
    """
    package = AUDITED_PROMPT_PACKAGE if audited else PROMPT_PACKAGE
    bundled = files(package).joinpath(f"{name}.md")
    if not bundled.is_file():
        raise PromptNotFound(f"{name}.md is not bundled in {package}")
    return bundled.read_text(encoding="utf-8")


def bundled_sha256(text: str) -> str:
    """Content hash the eject stamp records and the check recomputes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# First line of an ejected override. Hash is of the bundled text the copy was
# taken from, before the stamp itself, so the check can tell "the bundled
# default moved since this eject" apart from "the user edited their copy"
# (edits are expected and not reported).
_STAMP_TEMPLATE = "<!-- ejected-from: ortus/{version} bundled-sha256={digest} -->"
_STAMP_RE = re.compile(
    r"<!--\s*ejected-from:\s*ortus/(?P<version>\S+)\s+"
    r"bundled-sha256=(?P<digest>[0-9a-f]{64})\s*-->"
)


def eject_stamp(version: str, bundled_text: str) -> str:
    """The provenance header `ortus prompt eject` writes above the copy."""
    return _STAMP_TEMPLATE.format(
        version=version, digest=bundled_sha256(bundled_text)
    )


def parse_eject_stamp(text: str) -> tuple[str, str] | None:
    """(version, digest) from an override's first line, or None if unstamped.

    Only the first line counts: that is where eject puts the stamp, and a
    hand-created override with prose above a pasted stamp is treated as
    unstamped rather than guessing at its provenance.
    """
    first_line = text.split("\n", 1)[0]
    match = _STAMP_RE.search(first_line)
    if match is None:
        return None
    return match.group("version"), match.group("digest")


def substitute(text: str, /, **values: str) -> str:
    """Fill `$name` placeholders in a resolved prompt, tolerantly.

    Safe-substitution semantics on purpose. Prompts are shell-heavy — `$(...)`,
    `$ID_FEATURE_A`, `--arg t "$title"` — and a user's private override may
    predate any placeholder we add later. Silently degrading to an older
    contract is bad, but crashing every run that loads such an override is
    worse, so unknown and malformed tokens pass through untouched and
    `ortus check` reports the staleness instead.
    """

    return Template(text).safe_substitute(**values)


#: The session-close sequence the bundled goal prompts carry in step 4. It is
#: the anchor the stealth rewrite replaces, so a bundle that loses it is
#: caught by `session_close_steps(False, None)` no longer matching the text.
_SESSION_CLOSE_ANCHOR = (
    "completion comment, commit, `bd close`, `git pull --rebase --autostash`, "
    "`bd dolt push`, `git push`"
)


def session_close_steps(stealth: bool, tracker_remote: str | None) -> str:
    """The session-close sequence a goal prompt tells the worker to run.

    Outside stealth this is the bundled text byte for byte. A stealth
    repository has no Dolt remote on origin, and a bare `bd dolt push` would
    derive one from origin and land `refs/dolt/data` on the fork, so the step
    is dropped, or pointed at the private tracker remote when one is set.
    """
    if not stealth:
        return _SESSION_CLOSE_ANCHOR
    from ortus.core.stealth import TRACKER_REMOTE_NAME

    steps = [
        "completion comment",
        "commit",
        "`bd close`",
        "`git pull --rebase --autostash`",
    ]
    if tracker_remote:
        steps.append(f"`bd dolt push --remote {TRACKER_REMOTE_NAME}`")
    steps.append("`git push`")
    return ", ".join(steps)


def apply_session_close(
    resolved: ResolvedPrompt, *, stealth: bool, tracker_remote: str | None
) -> tuple[ResolvedPrompt, str | None]:
    """The resolved prompt with its session-close step fitted to stealth.

    Only a bundled text is rewritten. An override is the operator's own
    contract and is served untouched; in stealth one that still carries the
    bundled anchor gets a warning back, because its `bd dolt push` would
    publish the tracker to origin.
    """
    if not stealth:
        return resolved, None
    if resolved.source not in ("bundled", "audited"):
        if _SESSION_CLOSE_ANCHOR in resolved.text:
            return resolved, (
                f"stealth: {resolved.source} override {resolved.path} keeps "
                "`bd dolt push` in its session-close step; served unchanged"
            )
        return resolved, None
    text = resolved.text.replace(
        _SESSION_CLOSE_ANCHOR, session_close_steps(stealth, tracker_remote)
    )
    return ResolvedPrompt(
        name=resolved.name, source=resolved.source, path=resolved.path, text=text
    ), None


#: The subject clause each commit issue-reference style states. `subject` is
#: the clause grind has always written; the other two keep the issue id out
#: of the subject an upstream maintainer reads.
_SUBJECT_CLAUSES: dict[str, str] = {
    "subject": (
        "an imperative subject of at most 72 characters counted with the "
        "`{id}: ` prefix"
    ),
    "none": (
        "a plain imperative subject of at most 72 characters with no issue id "
        "anywhere in the message"
    ),
    "trailer": (
        "a plain imperative subject of at most 72 characters, with the issue "
        "id nowhere in the message but a final git trailer line "
        "`Ortus-Issue: {id}`"
    ),
}

#: The subject clause as the work-issue conditions phrase it today. Only this
#: clause is replaced when a repository names its issue another way.
_CONDITION_SUBJECT_CLAUSE = (
    "an imperative subject of at most 72 characters counted over the whole "
    "line including the `<ISSUE_ID>: ` prefix"
)


def subject_clause(issue_ref: str, id_token: str = "<issue-id>") -> str:
    """How the commit subject names the issue, for one `commit_issue_ref`."""
    try:
        return _SUBJECT_CLAUSES[issue_ref].format(id=id_token)
    except KeyError:
        raise ValueError(f"unknown commit_issue_ref {issue_ref!r}") from None


def message_rules(issue_ref: str = "subject") -> str:
    """The commit-message rule set every writer-facing contract carries.

    `subject` is the exact text tests/test_grind_prompt_content.py pins.
    """
    return (
        "Commit-message rules (a message that breaks one is replaced by a weaker "
        "deterministic assembly; only an over-long subject is repaired in place): "
        + subject_clause(issue_ref)
        + " — describe the change, do not restate the issue "
        "title, no trailing period, no `...` — then a body of at least two "
        "paragraphs of plain-text prose, under 8,000 characters in all, naming in "
        "backticks at least one function, class, or file that actually appears in "
        "your diff and none that does not (a diff with no nameable symbol still "
        "names one of its files), never an inventory of the files touched, and "
        "never narration of how the commit was produced (attempt counts, "
        "criterion results, step names, owned-path hashes)."
    )


def condition_message_rules(text: str, issue_ref: str) -> str:
    """A work-issue condition with its subject clause fitted to `issue_ref`.

    `subject` returns the condition unchanged, so the bundled text is what a
    non-stealth worker reads.
    """
    if issue_ref == "subject":
        return text
    return text.replace(
        _CONDITION_SUBJECT_CLAUSE, subject_clause(issue_ref, "<ISSUE_ID>")
    )
