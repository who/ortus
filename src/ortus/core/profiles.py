"""Typed, backend-specific agent profiles for Ortus execution phases."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ortus.core.config import Config

BackendName = Literal["claude", "codex", "grok", "local", "opencode"]

#: The backend names as every "expected ..." message spells them.
BACKEND_NAMES_PROSE = "claude, codex, grok, local, or opencode"


class Phase(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    #: Writes the commit message from the verified diff. Bounded prose over
    #: material it is handed, so it is the one phase a cheap model suits.
    FINALIZE = "finalize"
    #: The two `ortus dream` roles: one reader per bead window of grind logs,
    #: and the orchestrator that drafts memory proposals from their notes.
    DREAM_READER = "dream_reader"
    DREAM_ORCHESTRATOR = "dream_orchestrator"


#: The Dream phases, which alone carry a built-in model default.
DREAM_PHASES: frozenset[Phase] = frozenset(
    {Phase.DREAM_READER, Phase.DREAM_ORCHESTRATOR}
)

#: The model a Dream phase runs on when no CLI flag or profile table names
#: one. Dream reads many transcripts out of band, so a cheap model is the
#: right default; a backend absent here keeps its provider default.
DREAM_DEFAULT_MODELS: dict[str, str] = {"claude": "claude-haiku-5-5"}


# Forwarded verbatim as `opencode run --variant`, which selects a named
# variant of the model. opencode 1.18.27 names the variants it derives from a
# provider catalog after the reasoning-effort values that catalog lists (none
# through xhigh) and thinking-budget variants high and max, so this is the
# union of every name it can define. A name the served model does not define
# is a no-op there, so this set only catches typos.
_OPENCODE_EFFORTS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)

SUPPORTED_EFFORTS: dict[str, frozenset[str]] = {
    "claude": frozenset({"low", "medium", "high", "max"}),
    "codex": frozenset({"low", "medium", "high", "xhigh"}),
    "grok": frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
    # `local` is opencode under its older name, so a `[profiles.local.*]`
    # table validates against the same variant names.
    "local": _OPENCODE_EFFORTS,
    "opencode": _OPENCODE_EFFORTS,
}


class ProfileError(ValueError):
    """Raised when an agent profile cannot be safely resolved."""


@dataclass(frozen=True)
class AgentProfile:
    """Immutable model selection for one backend and one execution phase."""

    backend: BackendName
    phase: Phase
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def display_name(self) -> str:
        """A stable, credential-free description suitable for operator logs."""
        model = self.model or "provider-default"
        effort = self.reasoning_effort or "provider-default"
        return f"{self.backend}/{self.phase.value} (model={model}, effort={effort})"


def validate_profile_values(
    backend: str,
    phase: Phase,
    *,
    model: object = None,
    reasoning_effort: object = None,
) -> AgentProfile:
    """Validate untyped configuration and return an immutable profile."""
    if backend not in SUPPORTED_EFFORTS:
        raise ProfileError(
            f"invalid profile backend {backend!r}; expected {BACKEND_NAMES_PROSE}"
        )
    if model is not None and (
        not isinstance(model, str)
        or not model.strip()
        or any(c.isspace() for c in model)
    ):
        raise ProfileError(
            f"invalid model for profiles.{backend}.{phase.value}: expected a "
            "non-empty model name without whitespace"
        )
    if reasoning_effort is not None:
        allowed = SUPPORTED_EFFORTS[backend]
        if not isinstance(reasoning_effort, str) or reasoning_effort not in allowed:
            raise ProfileError(
                f"invalid reasoning_effort for profiles.{backend}.{phase.value}: "
                f"expected one of {', '.join(sorted(allowed))}"
            )
    return AgentProfile(
        backend=backend,  # type: ignore[arg-type]
        phase=phase,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def resolve_dream_profile(
    config: "Config",
    backend: str,
    phase: Phase,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> AgentProfile:
    """Resolve a Dream phase profile, filling in the backend's default model.

    The usual CLI, project, user chain runs first, so a configured model
    always wins; only a profile that resolved no model takes the entry in
    `DREAM_DEFAULT_MODELS`, keeping whatever reasoning effort was configured.
    """
    if phase not in DREAM_PHASES:
        raise ProfileError(
            f"{phase.value!r} is not a Dream phase; expected "
            f"{Phase.DREAM_READER.value} or {Phase.DREAM_ORCHESTRATOR.value}"
        )
    profile = config.resolve_profile(
        backend, phase, model=model, reasoning_effort=reasoning_effort
    )
    if profile.model is not None:
        return profile
    return replace(profile, model=DREAM_DEFAULT_MODELS.get(backend))
