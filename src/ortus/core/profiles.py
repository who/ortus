"""Typed, backend-specific agent profiles for Ortus execution phases."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

BackendName = Literal["claude", "codex"]


class Phase(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"


SUPPORTED_EFFORTS: dict[str, frozenset[str]] = {
    "claude": frozenset({"low", "medium", "high", "max"}),
    "codex": frozenset({"low", "medium", "high", "xhigh"}),
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
            f"invalid profile backend {backend!r}; expected claude or codex"
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
