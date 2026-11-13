# Changelog

All notable changes to Ortus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Ortus uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.6.0] - 2026-11-13

### Added

- **`ortus dream`.** Reads the grind logs a project already has and suggests
  changes to the project's bd memories: lessons to add, ones to update, and
  ones to drop. It reads each bead's part of a log on its own, with cheap
  read-only reader models (Claude Haiku 5.5 by default for both roles), then
  makes one more call that looks for the same problem across beads. Code
  checks every suggestion before you see it: each one has to quote the logs
  word for word, show up in at least two separate beads (`--min-beads`), and
  not repeat anything you already rejected or have not answered yet. The
  result is a report in `logs/dream-<time>.md` and one decision bead,
  labeled `human` and `dream`, for you to answer. Dream only reads: it never
  runs `bd remember`, never edits code, and never files follow-up work
  itself. It writes only its own caches under `.ortus/dream/`, which must be
  git-ignored, and it refuses to start while a grind holds the repo's lock. `--dry-run` shows which runs and beads would be read, how many
  tokens that should cost, and which models would do it, without calling a
  model, and `--budget-tokens` refuses a run estimated above a limit. Notes
  are cached per bead, so a second run only reads what is new. A `[dream]`
  table in `.ortusrc` sets the defaults. See `ortus dream` in
  `docs/commands.md`.
- **A Dream report reads without a manual.** The report has two sections,
  "Suggested memory changes" and "Possible follow-up tasks". Each item is
  named in plain words: a memory change by its memory key, such as
  `commit-message-rules`, and a follow-up by the first words of its title,
  such as `dolt-ssh-push`, with `-2` added to a repeated name. Every item
  says in one sentence what accepting it does, and every memory change
  comes with the exact `bd remember` or `bd forget` line to run.
- **Your answer is one line, and the report writes the example for you.**
  Reply with something like
  `accept commit-message-rules; reject pinned-prompt-hash-tests (already a test); file follow-up dolt-ssh-push`
  and record it with `bd human respond <decision-id> "..."`. The example in
  the report is built from the items that run actually kept, so it never
  names one that is not there, and the report's "How to respond" section
  walks an AI agent through the whole answer: run the memory lines, record
  the verdicts, and file any wanted follow-up through `ortus ingest`. At the
  end of a run Dream prints the next steps on stderr: the decision bead with
  its counts, the report path, and a line to hand to an agent. The decision
  bead id is still the last line of stdout, so `id=$(ortus dream .)` keeps
  working. Answers written in the older numbered form, such as
  `accepted P1 P2; F2 filed`, still parse for decision beads already open.
- **`ortus dream --last N` reads the newest N grind runs, however old.** A
  project whose last grind ran months ago can be dreamed without guessing an
  age for `--since`. Runs are ordered by the time in their log name, so a
  copied or touched log keeps its place.
- **Dream reads Codex runs.** Grind logs from the Codex backend are read like
  Claude ones. A run from a backend Dream cannot read yet is skipped with its
  backend named, such as `backend-grok`, instead of a blanket "unknown shape".
- **Dream ignores work that never landed.** A bead with no commit on the
  default branch, such as a reverted spike or an abandoned branch, is skipped
  as not on main, so it cannot teach a lesson about code that does not exist.
  `--include-unmerged` reads it anyway. A suggestion that names a file or
  function no longer in the code is dropped as stale, and a follow-up that
  does is kept with the missing names listed, so you can tell what moved.
- **Dream checks whether past lessons helped.** For each memory you accepted
  from an earlier run, the report compares how often its problem showed up
  in grind logs before and after you accepted it.
- **`ortus guard commit-msg`.** A `commit-msg` hook, installed by every
  `ortus init`, refuses a commit whose subject is longer than 72 characters
  before the commit exists, instead of the stop hook catching it after the
  push. Merge, fixup, squash, and revert subjects are exempt, and
  `ORTUS_ALLOW_LONG_SUBJECT=1` waives it once. Claude grind workers get the
  same check on `git commit -m` even where no hook is installed, and
  `ortus check` reports whether the hook is wired.
- **Grind workers cannot rewrite published history.** A new worker hook,
  `ortus guard worker-bash`, refuses every force-push spelling and an amend
  of a commit already on the remote. It acts only inside grind workers, so
  your own sessions are untouched, and `ORTUS_ALLOW_FORCE_PUSH=1` turns it
  off.

### Changed

- **Workers close the issue before they commit.** The session-close steps now
  run close, commit, pull, push, so the tracker export that rides along with
  the work no longer shows the issue as still in progress. Run
  `ortus init --force` to refresh the steps in an existing repo's agent
  files. A prompt override copied from the older order is still served as
  written.
- **A worker that cannot finish its last step stops cleanly.** When the only
  step left needs something a worker cannot do, such as a push the remote
  refuses or a human approval, one `BLOCKED:` comment naming the step now
  ends the session instead of the goal check firing again until Claude gives
  up.
- **Workers are told up front that `rm -rf` inside a chained command is
  refused,** and to build scratch trees in a fresh temporary directory
  instead, so they stop losing turns splitting the chain after the fact.

### Fixed

- **`python` works in worker shells that only have `python3`.** Grind puts a
  `python` link on the worker PATH, pointing at the repo's `.venv` Python when
  there is one, so `python - <<EOF` edits no longer fail with exit 127.
- **An Ortus command aimed at another repo uses that repo's tracker.** A
  `BEADS_DIR` inherited from a grind no longer sends bd writes for an
  unrelated repo, such as a stealth init in a scratch directory, into the
  grind's own database. Ortus drops a foreign `BEADS_DIR` for that command and
  says so once on stderr.

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

[0.6.0]: https://github.com/who/ortus/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/who/ortus/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/who/ortus/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/who/ortus/compare/v0.2.0...v0.3.0
