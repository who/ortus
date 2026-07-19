# Choosing a backend: `claude` vs `codex`

Ortus loops run on one of two agent CLIs. This project was generated with a
default baked in (the `agent_cli` answer), but that is a default, not a lock-in:

```
--backend <name>  >  ORTUS_BACKEND  >  the agent_cli answer baked in at generation
```

So `./ortus/goal.sh --backend codex "..."` runs one loop on Codex in a
Claude-generated project, and vice versa. What the `agent_cli` answer *does*
decide permanently is which config file the project ships
(`.claude/settings.json` vs `.codex/config.toml`) and which instruction file the
agent reads (`CLAUDE.md` vs `AGENTS.md`) — flipping the backend at runtime does
not retro-generate the other one. If you expect to run a backend most of the
time, generate for it.

## At a glance

| | `claude` (Claude Code) | `codex` (ChatGPT Codex CLI) |
| --- | --- | --- |
| Install | see [claude-code](https://github.com/anthropics/claude-code) | `npm install -g @openai/codex` |
| Auth | `claude` then `/login`, or `ANTHROPIC_API_KEY` | `codex login`, or `OPENAI_API_KEY` |
| Billing | Claude subscription or Anthropic API credits | ChatGPT subscription or OpenAI API credits |
| Model choice | Claude models | OpenAI models (`gpt-5-codex`, …) |
| Sandbox Tier 1 (native OS) | yes | yes |
| Sandbox Tier 2 (`--docker`) | yes | **no** — silently runs Tier 1 |
| `interview.sh` / `triage.sh` | yes | **no** — refuse with a capability message |

## Auth and subscription

Both backends authenticate on the host before a loop starts, and `goal.sh`
preflights auth so an unauthenticated CLI fails fast with a login hint instead
of burning a loop.

- **claude** — run `claude` and complete `/login` once, or export
  `ANTHROPIC_API_KEY` for unattended/CI runs.
- **codex** — run `codex login` once, or export `OPENAI_API_KEY`. Ortus pins
  `CODEX_HOME` to `$PWD/.codex`, so log in **from the project root** or the
  credentials land where the loop will not look for them.

If you already pay for one of the two subscriptions, that is usually the
deciding factor — neither backend gives Ortus a capability the other lacks in
the core `goal.sh` loop.

## Cost

Ortus does not meter or cap spend on either backend; cost is whatever the CLI
and the selected model charge. The loop shape is the same on both, so the
practical lever is the model, not the backend. Long unattended runs (`goal.sh`
with a large `--tasks` bound) are the expensive case on either side — bound them
with `--tasks` and a tight `-c` condition rather than relying on the backend.

## Model choice

- **claude** — the model is whatever your Claude Code install defaults to; set
  it there.
- **codex** — Ortus does **not** pin a model. The default is whatever the Codex
  CLI itself defaults to. To pin one for the project, set it in
  `.codex/config.toml`:

  ```toml
  model = "gpt-5-codex"
  ```

  For a single run, use `ORTUS_CODEX_MODEL=gpt-5-codex ./ortus/goal.sh "..."`.
  Precedence: `ORTUS_CODEX_MODEL` > `.codex/config.toml` > the CLI's own
  default. There is no `codex_model` Copier question.

Running two backends across sessions is a real reason to pick `codex` for some
work: a second model family reviewing the same queue catches different things.

## Known gaps under `codex`

These are current limitations, not permanent design:

- **Interactive flows are unavailable.** `ortus/interview.sh` and
  `ortus/triage.sh` depend on Claude Code's structured-question mechanism, which
  the Codex CLI has no equivalent for. Under `--backend codex` they **refuse
  immediately** with a capability message rather than launching a session that
  would stall waiting for input. Run them with `--backend claude` — they are
  scoped, short sessions, so mixing backends here is cheap.
- **Sandbox Tier 2 (`--docker`) is Claude-only.** The wrapper shells out to
  `docker sandbox run claude`, which has no Codex equivalent, so
  `goal.sh --docker` under Codex silently runs Tier 1. Codex projects get their
  isolation from the native OS sandbox plus `sandbox_mode = "workspace-write"`
  in `.codex/config.toml`. Use `--backend claude` for a run that needs Tier 2.
- **No `SessionStart` priming.** Claude Code hooks call `bd prime`
  automatically; under Codex nothing fires it, so `AGENTS.md` instructs the
  agent to call it explicitly.

The non-interactive path — `goal.sh`, `idea.sh`, `tail.sh`, PRD decomposition —
works on both backends.

## Recommendation

Pick **`claude`** if you want every Ortus script to work, including the
interactive interview/triage flows and Docker Tier 2 sandboxing, or if you
already have a Claude subscription. Pick **`codex`** if you have a ChatGPT
subscription or want a second model family on the queue, and you are content
driving work through `goal.sh` with the interactive flows run under
`--backend claude` when you need them.
