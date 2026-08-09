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
uv run pytest -m fast -n auto --test-timeout=30

# A named changed surface (add each directly affected test module).
uv run pytest tests/test_init.py tests/test_init_render.py \
  -n auto --test-timeout=30

# Hermetic subprocess integration for a risky command/orchestrator change.
uv run pytest -m integration -n auto --test-timeout=60

# Fresh verification: the expanded sweep, same selection rules.
uv run pytest <expansion> -n auto --test-timeout=180
```

Fresh verifiers expand from the changed paths and risk. They do not run
`network` or `live_provider` locally unless the issue explicitly requires it.

**Workers parallelise; CI enforces the budget serially.** Most of this suite's
wall clock is subprocess wait rather than computation — a grind-family test
drives eight to twelve `bd` invocations at roughly a second each — so `-n auto`
(pytest-xdist) returns the same answer several times sooner. It is not in
`addopts`, because parallelism and the duration budget are mutually exclusive:
contending workers make each individual test slower even as wall clock falls,
so the same `fast or integration` gate run at `-n 16` reported 15 duration
breaches that a serial run did not. The budget is a claim about how fast a test
is on a quiet machine, and only CI is quiet. So CI keeps running the gate
single-threaded with `--test-timeout=180 --enforce-duration-budget` and stays
the authority on duration, while workers and verifiers run parallel without it
and answer only whether the code is correct. `auto` rather than a fixed count,
so a small host is not oversubscribed and two grinds can share a machine. On a
host without pytest-xdist, drop `-n auto`; the identical selection still runs
serially.

**Verification-to-CI flag parity.** The flags above change how durations and
timeouts are judged and how many processes run them, and
never which tests are selected. Narrowing the marker expression to get past
them is never the fix; record a plan gap instead. `slow`-marked tests stay
exempt from the CI budget as before. A test that passes serially but fails
under `-n auto` is a real finding about a shared resource that test depends
on: fix or mark it rather than dropping the flag.

## bd workspaces: copy a template, never `bd init`

Every `bd` call is a fresh Go process opening a Dolt database, so `bd init`
costs about 1.6s and `bd create` about 1.2s where git doing comparable
filesystem work costs 2-3ms. A test that runs `bd init` therefore starts with a
multi-second setup floor before its first assertion. Instead `tests/conftest.py`
runs `bd init` **once per session** into a template workspace, and each test
copies the template it needs — measured at about 25ms.

```python
from tests.conftest import copy_bd_workspace

def test_something(tmp_path):
    workspace = copy_bd_workspace(tmp_path / "repo", "leaf")
    repo, issue_id = workspace.path, workspace.issues[0]
```

Three kinds. `bare` is the one that runs `bd init`; the seeded kinds are built
by copying it, so the session still pays exactly one init however many it uses:

| kind | contents |
| --- | --- |
| `bare` | `main` branch, fixture git identity, `.claude/settings.json` with bd excluded from the sandbox, no issues |
| `leaf` | `bare` plus one ready, executable leaf — `workspace.issues[0]` |
| `epic` | `bare` plus 1 epic and 2 children, one ready and one blocked |

`copy_bd_workspace(dest, kind)` returns a `BdWorkspace` carrying the copy's
`path` and the ids baked into it, so a test never re-derives an id it was
handed. The `bd_workspace` fixture is the same thing rooted at the test's own
`tmp_path` — `bd_workspace("repo", "leaf")` — which is also what keeps two
xdist workers off one destination.

Templates are read-only masters: **never write into one.** Take a copy and
mutate that. The copy step re-roots git's `core.hooksPath` (the one absolute
path a bd workspace carries) onto the copy and then fails loudly if any other
reference to the template survived, so a workspace that is not genuinely
independent is caught at the copy rather than as a puzzling failure later. If a
copied workspace is ever unusable on a supported platform, record a plan gap:
falling back to a per-test `bd init` silently restores the cost this removed.

Two kinds of test should still run `bd init` themselves — anything exercising
`bd init` behaviour, and anything asserting on a specific issue prefix, since
every template shares one prefix.

Tests must also hold without an ambient global git identity, since a developer
machine has one and a runner does not. `tests/conftest.py` points
`GIT_CONFIG_GLOBAL` at an empty file for every test, so a fixture that shells
out to `git commit` has to configure its own `user.name` and `user.email`.

Every selection below runs with `-n auto` in both phases; only CI runs them
serially with the duration budget.

| Changed path | Implementation gate | Verifier expansion |
| --- | --- | --- |
| `src/ortus/commands/<verb>.py` | matching `tests/test_<verb>.py` | related command tests plus `-m integration` when subprocess behavior changed |
| `src/ortus/core/*.py` | matching `tests/test_core_*.py` | `-m "fast or integration"` |
| `src/ortus/prompts/**` | prompt-content tests | `-m "fast or integration"` |
| templates or init rendering | matching render/init tests | render/init tests plus relevant smoke tests using canned providers |
| test policy or CI | `tests/test_test_policy.py` | collect-only marker probes plus the fast gate |

The comprehensive main CI matrix runs `fast or integration` on Linux and
macOS across every supported Python version, single-threaded and with the
budget enforced. It records JUnit XML, reports the 20 slowest tests, and
rejects hermetic tests exceeding five seconds unless they carry `slow`. Per-test timeouts print the running node id and allow pytest
to finish the report, preserving JUnit and timing evidence.

Only tagged release validation runs these external groups:

```bash
uv run pytest -m network --test-timeout=180
uv run pytest -m live_provider --test-timeout=900
```

Live-provider tests can spend API budget and require credentials. They are
never part of the worker, verifier, or main hermetic CI defaults.
