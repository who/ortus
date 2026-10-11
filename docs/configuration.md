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

### Removed: Jev

Jev, the TypeSafe judge, is no longer part of Ortus. A `[judge]` table or a
`jev_*` key left in a user or project `.ortusrc` still loads: it is dropped
before validation, and one deprecation warning on stderr names the file and
the keys it ignored. `ORTUS_JUDGE_*` and `ORTUS_JEV_*` environment variables
are ignored the same way and named in that same warning.

## Stealth mode (forks)

Use stealth when Ortus runs in a fork of someone else's project that has to
stay cleanly mergeable with upstream: no Ortus, beads, or agent file may ever
reach git, and no commit may name the tracker behind it. Run it once in an
existing checkout that already has history:

```bash
ortus init --stealth            # --local-only is the same flag
```

What init does: it runs `bd init --stealth` (bd makes no commit, installs no
hooks, and leaves `.gitignore` alone) and sets bd's `no-git-ops` to `true`. It
renders `.ortusrc`, the agent files, and `.claude/settings.local.json` with
co-author and attribution lines turned off. It writes one fenced block into
`.git/info/exclude`, sets `git config --local ortus.stealth true`, installs the
stealth guard hooks, and ends with a self-check that fails when git can still
see any Ortus path. What init skips: the init commit, `bd hooks install`, any
`core.hooksPath` (a stale one pointing at `.beads/hooks` is unset), and every
`.gitignore` edit. A tracked upstream file is never overwritten. When upstream
tracks `AGENTS.md` or `CLAUDE.md`, the managed block goes to
`AGENTS.override.md` or `CLAUDE.local.md` instead. When upstream tracks
`.claude/`, only `.claude/settings.local.json` is written.

The exclude block lives between `# BEGIN ortus stealth` and
`# END ortus stealth` markers, and a re-run rewrites only that block, leaving
bd's own stealth lines and anything you wrote untouched. To keep more of your
own paths out of git, list them in `.ortusrc` and re-run init:

```toml
stealth = true
stealth_extra_excludes = ["/notes/"]
commit_issue_ref = "none"            # none | trailer | subject
# tracker_remote = "file:///home/me/tracker"
```

Stealth is on when `.ortusrc` records `stealth = true` or the local git config
carries `ortus.stealth = true`. Either one is enough, so losing `.ortusrc`
does not quietly turn stealth off.

`commit_issue_ref` decides how a grind commit names its bd issue. `none`, the
stealth default, names no issue. `trailer` adds an `Ortus-Issue:` trailer, and
`subject` keeps the `<issue-id>: ` subject prefix every non-stealth repository
uses. `ortus init --commit-issue-ref` records it.

The tracker stays on this machine unless you give it a private Dolt remote:
`ortus init --stealth --tracker-remote <url>` registers it as `ortus-private`,
and the stealth session-close then runs `bd dolt push --remote ortus-private`.
The URL may not be origin or upstream. Without one, the bare `bd dolt push` is
dropped from session-close entirely. Grind sets `BD_NO_REMOTE_ADOPT=1` for its
workers so bd never adopts origin as a Dolt remote. Set it yourself in a shell
where you run bd by hand.

Two guards run on every push. Init installs `pre-push` and `pre-commit` hooks
that run `ortus guard`, and grind runs the same check in process before it
pushes, so a repository whose `core.hooksPath` belongs to something else is
still covered. A push is refused when it would carry a tracked Ortus path (for
example a `git add -f .beads/...`), a commit message naming a bead id or an
agent attribution line, or a `refs/dolt/*` or `__dolt_remote_info__` ref to
origin. Ortus never rewrites history to fix one. Untrack the path or reword the
commit by hand. The hooks fail closed when `ortus` is not on PATH.

The `.gitignore` rule: the fork's `.gitignore` files belong to upstream.
`pre-commit` refuses a commit that stages one, and grind halts the issue and
flags it for a human. When you do mean an upstream `.gitignore` change, set
`ORTUS_STEALTH_ALLOW_GITIGNORE=1` for that one commit.

Lint scope: `logs/` stays in the working tree but excluded, so a linter that
walks the tree without reading `.git/info/exclude` will see it. Tell that
linter to ignore `logs/`, or use one that honors git's exclude file. Grind's
tool caches (uv, pip, npm and the rest) move to `$TMPDIR/ortus-cache/` instead
of `.cache/` in the tree.

A fresh clone loses the setup, because `.git/info/exclude`, the hooks, and the
git config never travel with the repository. Re-run
`ortus init --stealth --force` in each clone. The same command converts a fork
you set up by hand: it keeps the existing `.beads/` workspace, turns on
`no-git-ops`, and installs the exclude block and hooks. A `--force` re-run is
idempotent.

Init will not clean up a remote that bd already pushed tracker data to. To check
origin and remove the refs yourself (`--no-verify` because the pre-push guard
refuses any `refs/dolt/*` update to origin, deletions included):

```bash
git ls-remote origin 'refs/dolt/*' refs/heads/__dolt_remote_info__
git push --no-verify origin --delete refs/dolt/data refs/heads/__dolt_remote_info__
```

Before you open a pull request upstream, squash the branch to commits written
for upstream's reviewers. Grind's per-issue history makes sense to you but
means nothing to them.

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

Keys whose names begin with `prompt_` gate in-flight harness
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
