# Ortus against the autonomous-coding landscape

Written 2026-08-11, immediately after the branch-scoped keystone landed and the
lean-pipeline pivot was filed. Knowledge horizon: the public landscape as of
early 2026; systems move monthly, so treat named capabilities as
directionally right rather than release-note precise. The assessment of Ortus
itself is from direct evidence — this repository, its measured baselines, and
one day spent operating and repairing it.

## The map

The field sorts into five families, and Ortus belongs to the smallest one.

**Interactive agents** — Claude Code, OpenAI Codex CLI, Gemini CLI, Cursor,
Windsurf, Cline/Roo, Aider, Goose, Amp. A human steers; the agent executes.
These are Ortus's *workers*, not its competitors: Ortus is a consumer of this
family (Claude default, Codex optional).

**Autonomous task agents** — Devin, GitHub Copilot coding agent, Google Jules,
Factory, Cursor background agents, OpenHands. Hand one an issue or a prompt;
it works in its own workspace and comes back with a PR. The forge (PR + CI +
human review) is their integrity model.

**Issue-to-PR pipelines** — Sweep, Codegen, and the review-side complements
(Qodo PR-Agent, CodeRabbit, Ellipsis, Greptile). Thin orchestration, forge
conventions do the governing.

**Research pipelines** — SWE-agent, AutoCodeRover, Agentless, Moatless, the
program-repair lineage (SapFix/Getafix). Their enduring lesson — *Agentless*
beating agent loops on SWE-bench with a fixed pipeline — is essentially the
lean pivot's thesis arrived at independently: deterministic structure beats
agent improvisation wherever structure is possible.

**Queue schedulers over agent workers** — the family Ortus is actually in,
and it is sparse: the ralph-loop folk pattern Ortus descends from, tmux/swarm
orchestrators (Claude Squad, claude-flow and kin), and the beads/Gas Town
ecosystem Ortus builds on (beads is its tracker substrate). Nearly everything
here is thin: a loop, a prompt, a hope. Ortus is the thickest scheduler in
this family that I am aware of — the only one with a transaction model.

## What is genuinely novel

Ranked by how far ahead of the nearest neighbor each sits. Novelty claims
name their closest neighbors; a claim without a neighbor is usually just
ignorance of one.

**1. Transactional finalization recovered from observable state.** Shipping
is a journaled transaction — report → close → commit → sync, each boundary
written after it lands — and a killed run replays only what never completed,
re-checking *external reality* (bd status, git refs, worktree state) rather
than an internal event log. Durable-execution frameworks (LangGraph
checkpoints, Temporal-backed agents) persist internal state; nothing
mainstream applies write-ahead semantics to the git-and-tracker side effects
themselves, which is where agent pipelines actually die. Copilot's agent and
Devin retry from scratch; Ortus resumes from a boundary. Today's live test:
the keystone's crash-window predicates (commit landed, fast-forward pending)
recovered correctly under fault injection in tests.

**2. The readiness gate: spec quality as a machine-enforced precondition.**
Selection cannot see an issue whose packet fails schema v1 — fifteen required
sections, criterion identifiers counted exactly, checks as executable
commands — and an in-loop repair pass fixes unready packets with a planning
model before any implementer spends tokens. The spec-driven wave (GitHub Spec
Kit, AWS Kiro, Tessl) shares the philosophy but keeps it as human ceremony;
issue-to-PR pipelines accept any prose. Nobody else makes *the work item
itself* pass a validator before an agent may touch it. This is, on the
evidence of this repo's own history, the single highest-leverage idea here.

**3. Trust as architecture, now migrating to trust as measurement.** The seal
(candidate hash → SHA), packet-hash binding, isolation guards, verdicts with
per-criterion evidence bound to the exact candidate — no other system
adversarially verifies its own agents' claims; the field trusts CI plus human
review. The lean pivot keeps the irreplaceable parts and makes them
deterministic: the **red–green proof** (a `proves-new` criterion must fail on
the merge base — mechanical test-vacuity detection, cousin to mutation
testing and Meta's assured test generation, absent from every agent harness)
and the **claim diff** (worker assertions checked against machine results,
lying strictly worse than silence). The pre-committed **escape-rate reversal
threshold** — remove the reviewer, reinstate it by config if escapes exceed
baseline — is a governance pattern I have not seen anywhere: capability
removal as a falsifiable experiment.

**4. Self-measurement as repo culture.** Counted two-day baselines, an error
ledger in the review brief calibrated to its author, adversarial
review-by-successor, tokens-per-landed-change as a first-class metric.
Devin publishes marketing benchmarks; research systems publish SWE-bench
scores; no production system measures *itself* against its own recorded
failure classes. Today closed the loop live: a CI escape (bd 1.0.4's
post-checkout hook) was caught, diagnosed by reproducing CI's exact
environment, and codified into tracker memory and a dependency-currency epic
within hours.

**5. Structural code intelligence as a hard phase contract.** Repo maps are
everywhere (Aider's tree-sitter map, Cursor indexing, Devin's wiki); making
the index a *required, per-phase, handshake-verified* capability with
blast-radius injection — and aborting the run when it's absent — is not.

## The gaps

Ranked by consequence. Each names who does it well.

**1. One issue at a time.** Devin runs parallel sessions; Copilot's agent
fans out across issues; Cursor runs background agents; Gas Town's whole
thesis is fleets. Ortus's serial loop was a correct consequence of the
worktree-state candidate — two issues in one tree entangle — but the
keystone just removed the reason: candidates are now commits on branches.
Parallelism is no longer blocked by the model, only unbuilt. This is the
largest capability gap and the one the architecture is finally ready for.

**2. No forge surface.** Work lands as pushes to main (soon gated by branch
checks), not as PRs a human can review with normal tools. PR-optionality is a
deliberate non-goal — Ortus must work without a remote — but the absence of
an *opt-in* PR mode costs supervision ergonomics, review culture, and
adoption: every competitor meets maintainers where they live, in the PR view.

**3. No intake.** Ortus assumes a PRD or hand-authored packets. Sweep took
raw GitHub issues; Copilot's agent takes any assigned issue; Devin takes
Slack messages; nothing routes a failing CI run, a Sentry event, or a user
bug report *into* the readiness pipeline. Ortus has the middle (`interview`,
`plan`, readiness repair) and the end (grind); it lacks the mouth.

**4. Host-coupled workers.** Workers run on the operator's machine and
inherit its fragility — today's srt-mux sandbox death took the whole fleet
down, and the bd version skew produced an environment escape. Devin,
Copilot, Jules, OpenHands all run containerized, reproducible workers. The
`--docker` flag exists as a sandbox tier; it is not a hermetic worker
runtime.

**5. No external evaluation.** All metrics are Ortus-on-Ortus, which the
review brief itself flags as a biased upper bound. The field speaks
SWE-bench; Ortus has never run against a public benchmark, so its central
claim — spec gating plus deterministic verification beats agent cleverness —
is unproven outside its own repo.

**6. Memory designed, not landed.** Epic D's curated, falsifiable,
pending-until-reviewed lessons are a better *design* than Devin Knowledge or
Cursor memories (which accrete unreviewed), but those ship today and Ortus's
sits in the queue. The compounding-reviewer vision is doc-ware until then.

**7. No security posture.** No secret scan on candidate diffs, no SAST step,
no provenance attestation on finalized commits, and packets are
integrity-hashed but not treated as an injection surface — a poisoned issue
description steers a worker with full tool access. The forge-native systems
inherit at least secret-scanning and branch protection from their platforms.

**8. Cost is observed, not governed.** The lean PRD makes tokens-per-change a
metric; nothing enforces a per-issue budget or parks work on exhaustion.
Devin's ACUs and OpenHands' budget caps are cruder but operational.

## What to add

Ordered by leverage against effort, each slotted into the existing
programme rather than beside it.

**Now (small, compounding):**

1. **bd/tooling version-matrix CI leg** — run the bd-sensitive suites under
   the pinned bd *and* its latest release in one extra job. Today's escape,
   generalized into a standing tripwire. Fits Epic `ortus-k46v` as a fourth
   leaf.
2. **Security step in the machine pipeline** — a deterministic secret/entropy
   scan plus a packet-injection lint (flag imperative instructions in issue
   bodies that address the worker rather than describe the work) as another
   AC-runner-style check in L1.3's pipeline. Cheap, and it makes the lean
   pipeline's "machines verify facts" story cover the risk the field ignores.
3. **Escape ledger now, not at L3** — the join of merges to reverts/red
   runs/follow-up bugs is a small deterministic pass, and every design bet in
   the lean PRD pays out through it. Promote it.

**Next (the capability jumps):**

4. **Parallel workers with a merge queue.** N issue branches, selection
   already collision-aware via concrete locations, integration serialized
   through the ff-gate (6a0a.1) as a merge queue — bors semantics with
   packets. This revives the integrator question with real data and is the
   single biggest throughput multiplier the keystone unlocked.
5. **Hermetic worker runtime.** Promote `--docker` into worker-per-container
   with a pinned toolchain image (bd, uv, codegraph baked in). Kills the
   host-fragility class (sandbox death, version skew) at the root; makes
   "gate with the environment masked" the default rather than a discipline.
6. **Opt-in PR-mode finalization.** Same transaction, but the fast-forward is
   replaced by push-branch-and-open-PR where a forge exists, with the
   verifier record as the PR body. Costs little (the branch and record
   already exist post-keystone); buys the entire supervision surface the
   field considers table stakes.
7. **Intake triage.** A bounded pass turning a red CI run or a raw issue into
   an interview-grade draft packet, pending readiness repair. Builds on
   `ortus triage` and the repair pass; closes the mouth-of-funnel gap.

**Later (the flag-plants):**

8. **Public benchmark run.** Package a SWE-bench-Verified subset as bd
   packets (readiness-gated, machine-verified) and publish close rates and
   tokens-per-close against Agentless/SWE-agent baselines. Either result is
   valuable: win, and the readiness-gate thesis has external evidence; lose,
   and the gap list writes the next PRD.
9. **Fleet/multi-repo.** The beads substrate is already distributed
   (dolt-backed); a scheduler-of-schedulers over per-repo Ortus instances is
   the Gas Town-adjacent endgame, but it should wait for parallelism,
   containers, and the escape ledger — scale multiplies whatever integrity
   properties exist, including the missing ones.

## Comparison table

| | Ortus (today) | Copilot agent | Devin | OpenHands | Aider | Sweep-class | Agentless (research) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Spec gating | **Schema-enforced + auto-repair** | none | knowledge, informal | none | none | none | fixed pipeline |
| Verification | Verifier + hash-bound verdicts → deterministic AC runner + red–green (L1) | CI + human PR review | self-check + CI | tests it writes | user + tests | CI | fixed localization/repair |
| Crash recovery | **Journaled boundaries, observable-state replay** | retry | retry | resume session | n/a | retry | n/a |
| Parallelism | serial (branch model now permits) | per-issue fan-out | parallel sessions | parallel | n/a | per-issue | batch |
| Surface | CLI + TUI + tracker | **PRs native** | web/Slack/IDE | web/CLI | CLI | PRs | n/a |
| Worker isolation | host + sandbox | **containers** | **containers** | **containers** | host | cloud | n/a |
| Memory | designed (Epic D), tracker-native | instructions files | Knowledge | condenser | conventions file | none | n/a |
| Self-measurement | **counted baselines, escape thresholds** | none public | marketing | benchmarks | none | none | benchmark-only |
| Works without a forge | **yes** | no | partial | yes | yes | no | yes |

## The one-paragraph thesis

Ortus is the only system in this landscape built on the premise that the
bottleneck in autonomous coding is not agent capability but **work-item
quality and integrity of the shipping path** — and its novel machinery
(readiness gating, transactional finalization, hash-bound verification
becoming deterministic proof, escape-rate governance) all serves that
premise. The field's leaders made the opposite bet: capable agents, forge
conventions, human review as the backstop, containers for safety, fleets for
throughput. The gaps list is exactly the leaders' bet — parallelism, forge
surface, intake, hermetic workers — and the keystone just removed the
architectural obstacle to most of it. The strategy that follows: keep the
integrity moat (nobody is close), adopt the leaders' operational table
stakes on top of it, and then prove the thesis in public with a benchmark
run — because a measured claim is the one kind this repository knows how to
defend.
