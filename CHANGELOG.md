# Changelog

All notable changes to Ortus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Ortus uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] - 2026-10-11

### Added

- **Stealth mode for forks.** `ortus init --stealth` (alias `--local-only`)
  sets Ortus up in a fork that must stay mergeable with upstream: no commit,
  no `.gitignore` edit, no `core.hooksPath`, and every Ortus, beads, and agent
  file kept out of git by one fenced block in `.git/info/exclude`. bd runs with
  `no-git-ops`, agent blocks go to `AGENTS.override.md` or `CLAUDE.local.md`
  when upstream tracks the originals, and `--tracker-remote` gives the tracker
  a private Dolt remote. `pre-push` and `pre-commit` hooks (`ortus guard`),
  plus the same check in grind's own push, refuse a force-added Ortus path, a
  commit naming a bead id or an agent, and tracker refs bound for origin.
  `--commit-issue-ref` picks how commits name their issue, and grind keeps its
  caches out of the tree. `ortus check` reports the exclude block, tracked
  Ortus paths, the hook wiring, bd's git-ops setting, and any Dolt remote that
  points at origin. See the stealth section in `docs/configuration.md`.
- **`ortus cost`.** Reports what each bead cost, read offline from the grind
  logs already on disk, in the billing buckets providers price separately:
  output, uncached input, and cached input, normalized across Claude, Codex,
  and Grok. A field a provider never reported stays unreported and flags the
  row rather than reading as zero. A Claude window reaped before it wrote its
  session totals is billed from its per-message usage and priced from a
  versioned table, and every row says whether its dollars came from the
  provider or an estimate. `--runs`, `--issue`, `--sessions`, `--tree`, and
  `--json` widen, narrow, or reshape the rollup, and failed worker windows are
  counted by cause per backend and model.
- **`ortus eval`.** Prints the run recipe for a frozen evaluation set
  (`--matrix`), runs every cell of it (`--run`), or reports what the cells
  produced, so a harness change can be compared against a control arm on cost
  per closed bead and close rate. See `docs/cost.md`.
- **`ortus export`.** Refreshes the tracked beads export with this clone's
  home directory and login name scrubbed out first, so recording tracker state
  in git no longer publishes the host's identity.
- **Grind stops before claiming work it cannot check.** If a program named by
  an issue's check commands is not on PATH, grind halts with exit 1 before the
  claim, names the missing programs and the PATH it searched, and leaves the
  issue untouched, instead of letting a worker discover "command not found"
  mid-window.
- **A worker window that dies says why.** Each failed window is tagged in the
  run log with a named cause, such as a watchdog kill, an operator interrupt,
  or an environment or provider failure. `ortus tail` and `ortus dashboard`
  show it on the line, and `ortus cost` counts it. A Grok worker's live view
  now prints what each turn billed.
- **Oversized output is filed, not cut.** Command output too large for a
  tracker comment or a prompt is written whole to a spill directory beside the
  grind logs, and only its path, size, and tail are injected, so the middle of
  a long log is no longer thrown away. The directory is bounded by file count
  and byte budget.
- **Version-skew parks resolve themselves.** The worker contracts treat a
  declared version or toolchain value past what the installed tool supports as
  something to pin, not a plan gap: lower the value, file a follow-up bead to
  restore it, and carry on. When a worker parks one anyway, grind recognizes
  it, labels it `pin-skew`, comments the evidence, and releases it back to the
  queue.
- **`ortus check` reports the beads pre-commit hook.** An informational row
  says whether git will actually run beads' `pre-commit` hook and, if not,
  prints the install command. `ortus init` now installs the bd hooks on both
  init paths, so `ortus init --force` retrofits a clone that never had them.

### Changed

- **The audited worker prompts and the cache-friendly prompt order are the
  defaults.** Both were adopted after measured comparisons. `prompt_audit`
  serves the shorter goal and work-issue texts, and `stable_prompt_prefix`
  puts the per-bead parts of the prompt after the parts every bead shares. Set
  either to `false` in `.ortusrc` to go back, or use `ORTUS_PROMPT_AUDIT=0` or
  `ORTUS_STABLE_PREFIX=0` for a single run.
- **A stalled claim keeps the run going.** A window that ends with its claim
  still in progress no longer ends the grind: the claim is resumed in the same
  run, and after two consecutive windows without a close it goes to the human
  queue. A claim a worker flagged for a human is released back to open with
  its label kept rather than stranded in progress, and the exit line counts
  the whole queue, including escalated issues.
- **Every filing path goes through the readiness gate.** Workers file
  follow-up and leftover work through `ortus ingest` instead of a raw
  `bd create`, and a refused packet says that no bead was created and which
  sections to supply, while the context to fix it is still open.
- **Codex write workers can run their own checks.** Write sessions launch with
  Codex's sandbox bypassed, so the npm, shell, or test runner an acceptance
  check spawns no longer fails with EPERM. Verifiers still run read-only, and
  `ortus check` no longer demands a workspace-write pin in the Codex config.
- **Grind asks the tracker less.** One tracker listing answers a step's
  repeated questions, roughly halving the bd processes a window spends.
- **The README is a short entry point.** Backends, local-model setup,
  configuration, the readiness gate, CodeGraph policy, prompts, and the
  glossary each moved to one page under `docs/`, and the "How it works"
  diagram is redrawn as an SVG with its `.reladraw` source checked in beside
  it.

### Fixed

- **Grok workers start the CodeGraph server.** A headless Grok launch now
  carries the folder trust a repo-local MCP server needs, so a fresh checkout
  no longer fails its CodeGraph handshake after claiming an issue.
- **Codex workers close what they implement.** The implementation contract no
  longer tells a worker to leave its edits for a verification pass that never
  runs, and a prompt override that still carries that sentence is refused
  before the worker starts.
- **Grind sees more than fifty in-progress claims.** The in-progress listing
  was capped at bd's default fifty rows.

### Removed

- **Jev, the TypeSafe judge.** Every judge site is gone: the pre-turn gate,
  readiness advice, the `pre_tool` hook and its tool screening, the progress
  reaper, `post_turn`, the stuck-claim decision, triage, the model router,
  collect mode, replay and its scorers, and the Jev arms of the harness eval.
  So are the `ortus judge` command, the `--judge` and `--judge-seat` flags on
  `ortus grind`, and the `judge` install extra. A `[judge]` table or `jev_*`
  key left in a user or project `.ortusrc` is dropped on load with one
  deprecation warning on stderr, as are `ORTUS_JUDGE_*` and `ORTUS_JEV_*`
  environment variables, so other repositories' configs keep loading. An eval
  script that still passes `--judge` now gets a usage error.

## [0.4.0] - 2026-09-07

### Added

- **`ortus ingest`.** Files one readiness schema v1 bead from a packet: a
  directory holding `description.md`, `design.md`, and `acceptance.md`, or one
  JSON object on stdin. The candidate is validated before anything is written,
  so an unready packet names its readiness gaps and creates nothing rather than
  leaving a half-formed issue to hunt down. Exit 0 puts the new bead id alone on
  stdout, so a caller can capture it with `id=$(ortus ingest --packet ...)`. An
  agent filing work reaches here instead of a multiline `bd create`, where shell
  quoting can mangle a section silently and the issue only fails at claim time.

### Fixed

- **An explicit P0 survives assembly.** A packet carrying priority 0 was filed
  at 2, because the field was read with a falsy-or-default expression and 0 is
  falsy — the one signal that most needed to survive was the one the reading
  discarded, leaving the bead mid-queue with no warning. Assembly now asks
  whether the field is absent or empty before it asks what it parses to. A
  priority that will not parse as an integer still resolves to 2.

### Changed

- **A release page carries its own changelog section.** A tagged release
  publishes the entry for that version instead of GitHub's generated commit
  list, and falls back to generated notes when the tag has no section. The
  release workflow's actions are pinned to their Node 24 majors, and the
  TestPyPI publish path is gone.

## [0.3.0] - 2026-09-05

### Added

- **Local models through opencode.** A new backend drives a model you serve
  yourself with the opencode CLI over the OpenAI chat-completions API. Provision
  it with `ortus init --backend opencode --local-model <id>`, or run
  `ortus init --backend opencode` with no model at a terminal and it lists the
  served models and lets you pick one. CodeGraph runs as an opencode MCP server
  with no shim between the worker and the model server. `--backend local` is the
  older name for the same backend and still works.
- **`ortus validate <repo> [<id>...]`.** Reports whether bd issues satisfy
  readiness schema v1 before a grind, so an unready bead is caught when you
  author it rather than when grind claims it. With no id it checks every open
  issue, and it exits nonzero when any issue is unready.
- **Prototype verification mode.** `ortus grind --prototype`, or
  `verification = "prototype"` in `.ortusrc`, verifies an issue with the
  project's lint and syntax checks instead of its behavioral tests, for fast
  throwaway work. Full verification stays the default.
- **`ortus prompt`.** List, show, or eject the bundled runtime prompts. An
  override resolves repo first, then user, then the bundled default.

### Changed

- **`ortus init` provisions every backend and owns a marked block.** The
  default `--backend all` writes each backend's config and pins claude as the
  concrete run backend. init now manages a fenced block inside `AGENTS.md` and
  `CLAUDE.md` instead of the whole file, so host prose survives re-init, and it
  splices a marked section into `.gitignore`.
- **Readiness accepts more real check commands.** The Targeted-tests matcher
  recognizes non-Python runners (vitest, jest, cargo test, go test), the command
  allowlist adds npx, grep, and the Node tool binaries, and a whole-suite
  command with no narrowing bound is rejected because it cannot finish inside a
  worker window.
- **Grok workers run through the same `/goal` path as Claude.**

### Fixed

- **Stuck claims move forward.** Grind escalates a wedged leftover claim to the
  human queue instead of resuming it every window, and it reaps a Claude worker
  whose claim turns human-flagged rather than holding it until the worker
  timeout.
- **A worker finishes what it starts.** Every check a worker began must complete
  before it exits, so a half-run verification cannot pass as done.

[0.5.0]: https://github.com/who/ortus/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/who/ortus/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/who/ortus/compare/v0.2.0...v0.3.0
