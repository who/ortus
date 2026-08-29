"""Guard that github-bead ingest did not change readiness schema v1 rules."""

from __future__ import annotations

import pytest

from ortus.core.readiness import (
    _is_unbounded_suite,
    spec_markdown,
    targeted_test_command,
    validate_issue,
)
from tests.test_readiness import ready_issue


def test_hand_authored_packet_still_validates() -> None:
    report = validate_issue(ready_issue())
    assert report.ready
    assert report.failures == ()


UNBOUNDED_SUITE_COMMANDS = (
    "pytest",
    "pytest -q",
    "uv run pytest -q",
    "uv run pytest -n auto --test-timeout=30",
    "uv run --with pytest-xdist pytest -n auto",
    "python -m pytest",
    "python3 -m pytest -x",
    "PYTEST_ADDOPTS=-q uv run pytest",
    "pnpm test",
    "pnpm --filter @five-hundred/bots test",
    "pnpm --filter=@five-hundred/bots test --",
    "pnpm -r test",
    "pnpm run test",
    "npm test",
    "npm test -- --silent",
    "npm run test",
    "yarn test",
    "yarn workspace pkg test",
    "go test ./...",
    "go test -v -count=1 ./...",
    "cargo test",
    "cargo test --workspace -- --nocapture",
)

BOUNDED_COMMANDS = (
    "uv run pytest tests/test_demo.py -q",
    "uv run pytest tests/test_demo.py::test_x -q",
    "pytest tests/",
    "pytest -k name",
    "pytest -k 'a or b' -q",
    "uv run pytest -m fast -n auto --test-timeout=30",
    "uv run pytest -n auto tests/test_demo.py",
    "uv run pytest -x tests",
    "python -m pytest tests/test_demo.py",
    "pnpm --filter x test -- test/file.spec.ts",
    "pnpm test test/file.spec.ts",
    "npm test -- test/file.spec.ts",
    "npm test -- --testPathPattern=file",
    "yarn test src/file.test.ts",
    "go test ./pkg/...  -run TestX",
    "go test ./pkg",
    "go test",
    "cargo test my_filter",
    "cargo test -p crate_name",
    "cargo test --test integration",
    "cargo test -- name",
    "bash -c 'for i in 1 2 3; do pytest; done'",
    "make test",
    "npx vitest run",
    "rg -n uses: .github/workflows/test.yml",
    "true",
)


@pytest.mark.parametrize("command", UNBOUNDED_SUITE_COMMANDS)
def test_detector_flags_unbounded_suite(command: str) -> None:
    assert _is_unbounded_suite(command), command


@pytest.mark.parametrize("command", BOUNDED_COMMANDS)
def test_detector_accepts_bounded_or_opaque(command: str) -> None:
    assert not _is_unbounded_suite(command), command


def _issue_with_checks(ac1: str, ac2: str, targeted: str) -> dict:
    issue = ready_issue()
    issue["acceptance_criteria"] = f"""## Observable criteria
- AC-1: Preview performs no writes.
- AC-2: Normal execution is unchanged.

## Criterion checks
- AC-1: `{ac1}`
- AC-2: `{ac2}`

## Targeted tests
`{targeted}`
"""
    return issue


def test_unbounded_suite_command_fails_readiness() -> None:
    """AC-1: a bare full-suite criterion check is a readiness failure."""
    issue = _issue_with_checks(
        "pnpm --filter @five-hundred/bots test",
        "uv run pytest tests/test_demo.py::test_run -q",
        "uv run pytest tests/test_demo.py -q",
    )
    report = validate_issue(issue)
    assert not report.ready
    failures = {f.code: f.message for f in report.failures}
    assert failures == {
        "criterion_mapped_checks": (
            "AC-1: unbounded suite command; bound it to specific files/tests "
            "or move the full run to a human-run criterion"
        )
    }


def test_unbounded_suite_command_fails_on_observable_lines() -> None:
    """AC-1: the rule also covers commands carried on Observable lines."""
    issue = ready_issue()
    issue["acceptance_criteria"] = """## Observable criteria
- AC-1: Preview performs no writes. `uv run pytest -q`
- AC-2: Normal execution is unchanged. `uv run pytest tests/test_demo.py::test_run -q`
"""
    report = validate_issue(issue)
    assert not report.ready
    failures = {f.code: f.message for f in report.failures}
    assert set(failures) == {"observable_criteria"}
    assert failures["observable_criteria"].startswith("AC-1: unbounded suite command")


def test_unbounded_suite_command_fails_targeted_tests() -> None:
    """AC-1: an unbounded Targeted tests command fails that section."""
    issue = _issue_with_checks(
        "uv run pytest tests/test_demo.py::test_preview -q",
        "uv run pytest tests/test_demo.py::test_run -q",
        "uv run pytest -q",
    )
    report = validate_issue(issue)
    assert not report.ready
    failures = {f.code: f.message for f in report.failures}
    assert set(failures) == {"targeted_tests"}
    assert "unbounded suite command" in failures["targeted_tests"]
    assert "human-run criterion" in failures["targeted_tests"]


@pytest.mark.parametrize(
    ("ac1", "targeted"),
    [
        ("uv run pytest tests/test_demo.py::test_preview -q", "uv run pytest tests/test_demo.py -q"),
        ("pytest -k preview", "pytest -k demo -q"),
        ("uv run pytest -m fast -n auto --test-timeout=30", "uv run pytest -m fast"),
        ("pnpm --filter x test -- test/file.spec.ts", "pytest tests/test_demo.py"),
        ("go test -run TestPreview ./...", "python -m pytest tests/test_demo.py"),
        ("cargo test preview", "uv run pytest tests/"),
    ],
)
def test_bounded_commands_stay_ready(ac1: str, targeted: str) -> None:
    """AC-2: a path, a filter, or post-`--` file arguments keep the spec READY."""
    issue = _issue_with_checks(
        ac1, "uv run pytest tests/test_demo.py::test_run -q", targeted
    )
    report = validate_issue(issue)
    assert report.ready, [f.message for f in report.failures]


def test_spec_documents_bound_rule() -> None:
    """AC-3: `ortus spec` teaches the bound requirement it enforces."""
    spec = spec_markdown()
    assert "must be bounded" in spec
    assert "`uv run pytest -q`" in spec
    assert "`go test ./...`" in spec
    assert "human-run criterion" in spec


def test_vitest_command_satisfies_targeted_tests() -> None:
    """AC-1: a bounded pnpm/vitest command is a targeted test invocation."""
    issue = _issue_with_checks(
        "uv run pytest tests/test_demo.py::test_preview -q",
        "uv run pytest tests/test_demo.py::test_run -q",
        "pnpm --filter @five-hundred/bots test -- test/hardPlay.spec.ts",
    )
    report = validate_issue(issue)
    assert report.ready, [f.message for f in report.failures]


@pytest.mark.parametrize(
    "command",
    [
        "cargo test --test integration",
        "go test ./pkg -run TestX",
        "make test",
        "npm test -- test/file.spec.ts",
        "npm run test -- test/file.spec.ts",
        "yarn test src/file.test.ts",
        "yarn jest path/to.spec.ts",
        "pnpm test test/file.spec.ts",
        "pnpm --filter x test -- test/file.spec.ts",
    ],
)
def test_non_python_test_runners_accepted(command: str) -> None:
    """AC-2: cargo/go/make/npm/pnpm/yarn test forms are test invocations."""
    assert targeted_test_command(f"Run `{command}`.") == command
    assert targeted_test_command(command) == command


@pytest.mark.parametrize(
    "section",
    [
        "Run the unit tests for the parser.",
        "Verify test coverage stays above 90%.",
        "The targeted test is the vitest suite.",
        "Run `npm run build`.",
        "Run `pnpm --filter x build`.",
    ],
)
def test_prose_and_non_test_commands_are_not_test_invocations(section: str) -> None:
    """AC-4: prose without a runner token and build commands still fail."""
    assert targeted_test_command(section) is None


def test_runner_gate_still_applies_to_widened_forms() -> None:
    """AC-4: a test-looking command whose lead token is not a runner fails."""
    assert targeted_test_command("Run `nonexistent-tool test tests/x`.") is None
    issue = _issue_with_checks(
        "uv run pytest tests/test_demo.py::test_preview -q",
        "uv run pytest tests/test_demo.py::test_run -q",
        "nonexistent-tool test tests/x",
    )
    report = validate_issue(issue)
    assert {f.code for f in report.failures} == {"targeted_tests"}


@pytest.mark.xfail(
    reason="npx joins the runner allowlist in ortus-tnj5", strict=False
)
def test_npx_vitest_is_a_test_invocation_once_npx_is_a_runner() -> None:
    """Composes with ortus-tnj5: the runner gate and the test match both apply."""
    assert (
        targeted_test_command("Run `npx vitest run tests/x.test.ts`.")
        == "npx vitest run tests/x.test.ts"
    )


def test_spec_documents_bash_c_rule() -> None:
    """AC-3: `ortus spec` teaches the shell-construct escape it enforces."""
    spec = spec_markdown()
    assert "Shell constructs are not commands" in spec
    assert "`bash -c '…'`" in spec
    assert "`no runnable command`" in spec
    assert "cargo test --test demo" in spec
    assert "pnpm --filter pkg test -- test/demo.spec.ts" in spec
