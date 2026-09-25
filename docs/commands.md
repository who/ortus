# Command reference

Every verb Ortus registers, with its key flags. Run `ortus <verb> --help` for
the authoritative flag list, and `ortus --version` for the installed version.
A `<repo>` argument defaults to `$PWD` with no walk-up.

## Bootstrapping

### `ortus init <repo>`

Bootstrap a fresh repo for Claude, Codex, Grok, or a local model. Writes
`.beads/`, `.ortusrc`, `.gitignore`, the managed blocks in `AGENTS.md` and
`CLAUDE.md`, and the provisioned backends' config directories.

`--backend` (`all|claude|codex|grok|local|opencode`; the default `all` is
provisioning-only and pins a concrete run backend), `--codegraph`, `--prefix`,
`--project-type`, `--package-manager`, `--framework`, `--linter`,
`--local-model`, `--local-base-url`, `--force`.

### `ortus check <repo>`

Verify bd, the run backend, sandbox, backend config, and managed agent files.
WARN rows cover provisioned siblings. Strictly read-only. `--backend`.

## Authoring work

### `ortus plan <repo> [<PRD>]`

Decompose a PRD into bd issues, or run the interview-then-PRD-then-decompose
flow when no PRD path is given. `--backend`, `--model`, `--reasoning-effort`,
`--codegraph`.

### `ortus interview <repo> [<feature-id>]`

Interactive PRD-building interview for an open feature. `--backend`.

### `ortus ingest <repo>`

File one readiness schema v1 bead from a packet directory or stdin JSON — the
filing path for agents, in place of a multiline `bd create`. `--packet`,
`--stdin`, `--title`, `--type`, `--priority`.

### `ortus validate <repo> [<id>...]`

Report whether bd issues satisfy readiness schema v1 before grinding. No id
sweeps every open issue; exit 1 when any is unready. `--json`.

### `ortus spec`

Print the readiness schema issue-authoring contract.

## Running work

### `ortus grind <repo>`

Drive the bd queue via backend-neutral subprocess-per-task workers.
`--tasks`, `--iterations`, `--condition`, `--orphan-policy`, `--idle-sleep`,
`--worker-timeout`, `--integration-branch`, `--fast`, `--implement-model`,
`--implement-reasoning-effort`, `--verify-model`, `--verify-reasoning-effort`,
`--docker`, `--dry-run`, `--judge`, `--judge-seat`, `--backend`, `--codegraph`,
`--prototype`.

### `ortus unlock <repo>`

Clear a stuck grind flock; optionally revert in-progress claims. `--force`,
`--revert-claims`.

### `ortus export [<repo>]`

Refresh `.beads/issues.jsonl` from the tracker, atomically, with every term in
the clone's local `.beads/protected-terms.txt` removed before the new bytes
become the tracked file. Run it at session close instead of a bare `bd export`
over the tracked path, so the export a commit carries is both current and free
of the strings this machine refuses to publish.

## Watching a run

### `ortus tail <repo>`

Tail the newest orchestrator log (use `--all` for every matching file).
`--raw`, `--tools`/`-t`, `--system`/`-s`, `--verbose`/`-v`, `--assistant`/`-a`,
`--codex`, `--backend`, `--lines`/`-n`, `--all`.

### `ortus dashboard <repo>`

Watch one grind run in a read-only live view. `--replay`.

### `ortus human <repo>`

Emit `HUMAN-TODO.md` for items needing a human decision. `--no-file`.

## Measuring

### `ortus cost <repo>`

Report per-bead token cost by billing bucket from grind logs. `--log`,
`--runs`, `--issue`, `--sessions`, `--json`. See [cost](cost.md).

### `ortus eval <root>`

Run the frozen evaluation set's recipe, or report what it produced. `--matrix`,
`--run`, `--backend`, `--cell-timeout`, `--json`. See [cost](cost.md).

## Prompt overrides

### `ortus prompt list [<repo>]`

Each bundled prompt: name, winning source, phase, description.

### `ortus prompt show <name> [<repo>]`

Resolved prompt text on stdout, header on stderr. `--origin`.

### `ortus prompt eject <name> <repo>`

Copy the bundled default to `<repo>/.ortus/prompts/`. `--user`, `--force`.
See [runtime prompts](prompts.md).

## Judge evidence

### `ortus judge export <input>`

Export validated, deduplicated judge events using only the logger's allowed
fields. `--output`, `--force`.

### `ortus judge replay <input>`

Calculate accuracy, latency, coverage and measured costs without rerunning the
judge. `--labels`, `--output`, `--thresholds`, `--force`. See
[the judge pilot](judge.md).
