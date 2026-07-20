# Test gates

Ortus classifies every pytest item by dependency: `fast` (hermetic unit),
`integration` (hermetic subprocess/component), `network` (network or package
build), or `live_provider` (authenticated model API). `slow`, `smoke`, and
`regression` are orthogonal risk labels. Network and live-provider tests must
be marked explicitly; unclassified tests are collected as `fast`.

## Phase-aware commands

Implementation workers start with the smallest reliable changed surface:

```bash
# Standard inner loop: target under 60s on a typical development machine.
uv run pytest -m fast --test-timeout=30 --enforce-duration-budget

# A named changed surface (add each directly affected test module).
uv run pytest tests/test_init.py tests/test_init_render.py \
  --test-timeout=30 --enforce-duration-budget

# Hermetic subprocess integration for a risky command/orchestrator change.
uv run pytest -m integration --test-timeout=60 --enforce-duration-budget
```

Fresh verifiers expand from the changed paths and risk. They do not run
`network` or `live_provider` locally unless the issue explicitly requires it.

| Changed path | Implementation gate | Verifier expansion |
| --- | --- | --- |
| `src/ortus/commands/<verb>.py` | matching `tests/test_<verb>.py` | related command tests plus `-m integration` when subprocess behavior changed |
| `src/ortus/core/*.py` | matching `tests/test_core_*.py` | `-m "fast or integration"` |
| `src/ortus/prompts/**` | prompt-content tests | `-m "fast or integration"` |
| templates or init rendering | matching render/init tests | render/init tests plus relevant smoke tests using canned providers |
| test policy or CI | `tests/test_test_policy.py` | collect-only marker probes plus the fast gate |

The comprehensive main CI matrix runs `fast or integration` on Linux and
macOS across every supported Python version. It records JUnit XML, reports the
20 slowest tests, and rejects hermetic tests exceeding five seconds unless
they carry `slow`. Per-test timeouts print the running node id and allow pytest
to finish the report, preserving JUnit and timing evidence.

Only tagged release validation runs these external groups:

```bash
uv run pytest -m network --test-timeout=180
uv run pytest -m live_provider --test-timeout=900
```

Live-provider tests can spend API budget and require credentials. They are
never part of the worker, verifier, or main hermetic CI defaults.
