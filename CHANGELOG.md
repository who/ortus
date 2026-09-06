# Changelog

All notable changes to Ortus are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Ortus uses
[semantic versioning](https://semver.org/).

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

[0.3.0]: https://github.com/who/ortus/compare/v0.2.0...v0.3.0
