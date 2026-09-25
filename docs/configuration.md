# Configuration

An optional `<repo>/.ortusrc` (TOML) overrides `~/.ortusrc`, and a command-line
flag overrides both. Every key has a working default, so a file that sets
nothing behaves exactly like no file at all.

## A full `.ortusrc`

```toml
prefix = "myproj"       # bd issue-id prefix
project_type = "python" # python | typescript | go | rust | polyglot
backend = "claude"      # claude | codex | grok | opencode (older name: local); always concrete, "all" is init-only and invalid here
codegraph = "required"  # off | auto | required (default: required)
codegraph_refresh_blocking = false
verification = "full"   # full | prototype (default: full)
integration_branch = "main"  # branch grind pins the tree to and re-asserts
reviewer = false        # add a read-only agent review after a green machine run
merge_gate = false      # wait for issue-branch checks before fast-forward
merge_gate_timeout = 1800  # seconds; timeout blocks, never lands

[profiles.claude.plan]
model = "opus"
reasoning_effort = "high"

[profiles.claude.implement]
model = "sonnet"

[profiles.claude.verify]
model = "opus"
reasoning_effort = "high"

[profiles.claude.finalize]
model = "haiku"

[profiles.codex.implement]
model = "gpt-5.2-codex"
reasoning_effort = "high"

[profiles.opencode.implement]
reasoning_effort = "medium"  # forwarded as `opencode run --variant`; the served model is [local].model, a model here overrides it

# Read under backend = "opencode" (or its older name local). Last in the file:
# TOML keeps every key below a [table] header inside that table.
[local]
base_url = "http://127.0.0.1:8080/v1"  # the server you run; default is llama-server on localhost
model = "unsloth/Qwen3.8-27B-GGUF:Q4_K_M"  # the id GET {base_url}/models reports (llama-server model path or --alias)
# api_key_env = "LLAMA_API_KEY"        # the NAME of a variable holding a bearer key, never the key itself
```

The `[judge]` table and its seats are documented separately in the
[judge pilot guide](judge.md), which also covers the privacy, failure and
rollback posture the gate is opt-in for. The optional pre-turn Jev gate is
disabled by default.

## Key reference

| Key | Default | What it decides |
|---|---|---|
| `prefix` | none | bd issue-id prefix `ortus init` writes |
| `owner` | none | bd owner new issues are filed against |
| `project_type` | inferred | which linter and syntax gate prototype verification runs |
| `backend` | `claude` | the run backend; see [agent backends](backends.md) |
| `codegraph` | `required` | CodeGraph policy; see the [CodeGraph lifecycle](codegraph.md) |
| `codegraph_refresh_blocking` | `false` | whether the post-edit index refresh blocks the phase |
| `verification` | `full` | the bar a worker clears before session-close |
| `integration_branch` | `main` | branch grind pins the working tree to each iteration |
| `reviewer` | `false` | add a read-only agent review after a green machine run |
| `merge_gate` | `false` | wait for the issue branch's forge checks before fast-forwarding |
| `merge_gate_timeout` | `1800` | seconds to wait for those checks; a timeout blocks, never passes |
| `condition` | none | legacy custom per-iteration `/goal` condition |

Keys whose names begin with `prompt_` or `jev_` gate in-flight harness
experiments. Each is off, or in shadow, until its comparison has a verdict, and
each reads an `ORTUS_*` environment variable so one run can flip it without
editing a tracked file.

## Profiles

Profiles are independent for `plan`, `implement`, `verify`, and `finalize`, and
are scoped to the selected backend. `finalize` is the one bounded, read-only
pass that writes the commit message from the verified diff; it is prose over
material it is handed rather than correctness reasoning, so Claude defaults it
to `haiku` and any failure falls back to the deterministic commit body.
Resolution is CLI phase override, then the matching project table, then the
matching user table, then the provider default. Nested tables merge field by
field, so a project can override only `model` while inheriting
`reasoning_effort` from `~/.ortusrc`. Omitted fields add no backend CLI flags.

Under `opencode` (or `local`) the served model comes from `[local].model`, a
profile `model` overrides it for that phase only, and `reasoning_effort` is
forwarded as `opencode run --variant`, a named variant of the model (`none`
through `xhigh`, plus `high` and `max`) that is a no-op for a name the served
model does not define.

`ortus plan` accepts `--model` and `--reasoning-effort`; `ortus grind` accepts
`--implement-model`, `--implement-reasoning-effort`, `--verify-model`, and
`--verify-reasoning-effort`. The compatibility `--fast` flag applies only to
Claude implementation workers and never to verification.

## Full versus prototype verification

`verification` is the bar a grind worker must clear before it session-closes
an issue. `full`, the default, runs the issue's criterion-check commands.
`prototype` runs only the project's linter (`ruff`, `eslint`, `golangci-lint`,
or `cargo clippy`) plus a syntax or compile gate chosen from `project_type`
(`python -m compileall`, `tsc --noEmit`, `go build ./...`, or `cargo check`; a
`polyglot` project gets the gate of every language whose marker file sits at
the repository root) and deliberately skips the issue's behavioral test
commands and the repo test suite. The acceptance criteria stay in the work
spec as the record; prototype mode does not run them. `ortus grind --prototype`
selects the mode for one run and wins over the `.ortusrc` pin, which wins over
the default. The grind start line records the resolved mode and where it came
from, and `ortus check` reports it before a run. Prototype mode is a lowered
bar for throwaway or exploratory work on high-velocity prototype projects, not
a faster full verification. An issue closes on a clean lint pass with its
behavior unproven.
