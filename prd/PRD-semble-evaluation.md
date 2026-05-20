# PRD: Semble Evaluation for Ortus

**Status:** Evaluation / Recommendation (not a committed implementation plan)
**Date:** 2026-05-19
**Branch:** `semble-eval`
**Decision owner:** operator
**Companion docs:** `prd/PRD-zero-framework-cognition.md` (ZFC), `ZFC.md`, the bundled CodeGraph integration in `src/ortus/prompts/grind-prompt.md`

---

## 0. TL;DR

Semble is a CPU-only, static-embedding ([model2vec](https://github.com/MinishLab/model2vec)) semantic code-search tool that returns the exact code chunks an agent needs and claims **~98% fewer tokens than grep+read**. This document evaluates whether Ortus should adopt it as a **token-efficient replacement for the grep+read fallback** in its agent loops, kept **complementary to** (not competing with) the existing CodeGraph integration.

**Headline finding:** Semble's value proposition is *amplified* by Ortus's architecture. Because Ortus runs **one task per fresh `claude -p` process** (the no-compaction principle), every task pays its investigation token cost from scratch — investigation never amortizes across the queue. Semble attacks exactly that recurring cost. The biggest open risks are (a) the agent-sandbox network policy vs Semble's `uvx`-on-demand install path, and (b) retrieval quality on Ortus's bash+markdown-heavy corpus, which differs from Semble's benchmark set.

**Recommendation (preview, full version in §11):** run a **time-boxed, single-repo trial** — optional + runtime-gated, grind Investigate step only — instrumented with `semble savings`. Expand to the other surfaces only if the trial clears the decision criteria in §10.

---

## 1. Motivation

Ortus drives a bd issue queue to zero by spawning a fresh `claude -p` subprocess per task. Each subprocess independently runs an **Investigate** phase before implementing: today that phase uses CodeGraph (when available) as the primary surface and **subagent grep + `Read`** as the fallback.

`Read`-ing whole files is the dominant token sink in investigation: to understand one function the agent often reads the entire file it lives in, plus a few neighbors. Semble's pitch is to return *only the relevant chunk* instead of the whole file.

Two properties of Ortus make this more valuable here than in a typical IDE agent:

1. **No compaction / fresh window per task.** (See the `ortus-design-principle-no-compaction` memory.) Investigation tokens are spent fresh on every task; there is no long-lived context that amortizes a one-time exploration. A 15-task grind run pays the investigation tax 15 times.
2. **Subprocess-per-task isolation.** Each task's investigation is independent, so per-task token reduction multiplies directly by queue depth.

If Semble's ~98% chunk-vs-file savings hold even partially on real grind investigations, the compounded savings across a queue are material.

---

## 2. What Semble is (accurate technical description)

| Property | Detail |
|---|---|
| Core tech | Static embeddings via model2vec (MinishLab); no transformer inference at query time |
| Runtime | Pure CPU, no API keys, no GPU, no external service |
| Indexing | ~250 ms for an average repo; local paths watched + auto-reindexed; index cached for session lifetime |
| Query | ~1.5 ms; NDCG@10 = 0.854 on Semble's benchmark (≈ code-specialized transformer quality at a fraction of size/cost) |
| Token claim | ~98% fewer tokens than grep+read (returns chunks, not files) |
| Distribution | PyPI: `pip install semble` or `uv tool install semble`; MCP extra: `semble[mcp]` |
| Interfaces | (a) MCP server, (b) bash CLI, (c) `semble init` drops a dedicated search sub-agent |
| MCP tools | `search(query, repo)` → relevant chunks; `find_related(file_path, line, repo)` → semantically similar chunks |
| Bash CLI | `semble search "<query>" <path> [--top-k N]`; `semble find-related <file> <line> <path>`; `semble savings` |
| Sub-agent constraint | **Sub-agents cannot call MCP tools** — they must use the bash CLI. (Identical constraint to CodeGraph.) |
| Telemetry | `semble savings` reads `~/.semble/savings.jsonl`; conservative `(file_chars − snippet_chars)/4` token estimate |

**Two distinct capabilities:**
- `search` — natural-language *or* identifier query → ranked chunks. Replaces "grep for a guess, then read the file."
- `find_related` — given `file:line`, return *semantically* similar code elsewhere. **No CodeGraph equivalent** — CodeGraph's callers/callees are *structural* (call/import edges); `find_related` finds conceptual kin even with zero shared symbols.

---

## 3. What CodeGraph is (current Ortus state)

CodeGraph is already woven into `grind-prompt.md`:

- **Gating:** `codegraph_available` ⇔ `.codegraph/` exists at repo root **and** a `mcp__codegraph__*` tool is registered.
- **Investigate (step 4):** when available, `codegraph_search` / `codegraph_callers` / `codegraph_callees` / `codegraph_impact` / `codegraph_node` are the primary surface; `codegraph_explore` / `codegraph_context` are subagent-only (large payloads); grep/glob/`Read` is the fallback when CodeGraph returns nothing.
- **Orient (step 1):** an activity-read surfaces symbols for files touched in the last ~20 commits (`codegraph_files` / `codegraph_search`), plus reuse of `**CodeGraph v1**` blocks parsed out of recent bd comments.
- **Refresh (step 6.5):** best-effort `codegraph sync`.

CodeGraph's data model is a **structured symbol graph**: precise, relationship-aware, on-disk (`.codegraph/`), persistent across runs.

---

## 4. Complementarity thesis

| Dimension | grep+read (today's fallback) | CodeGraph | Semble |
|---|---|---|---|
| Data model | none (literal scan) | structured symbol graph | static-embedding vector index |
| Best question | exact literal string | "what calls foo / impact of Bar / where is X defined" | "how is auth handled / where does concept Y live" |
| Query input | regex/literal | symbol name | natural language *or* identifier |
| Output | matching lines + whole files | symbols, edges, impact sets, source | relevant chunks only |
| Token cost | **high** (whole-file reads) | low (graph lookups) | very low (chunks; ~98% vs grep+read) |
| Index | none | `.codegraph/` on disk, persistent | in-memory, ~250 ms rebuild, session-cached |
| Determinism | exact/reproducible | exact/reproducible | similarity-ranked (non-reproducible across index states) |
| Unique trick | — | impact / blast-radius | `find_related` (semantic kin) |

**The clean mental model (matches the chosen routing in §5):**
- **CodeGraph** = *structure you can name* ("trace this symbol").
- **Semble** = *discovery by meaning* ("find code that does X") — i.e. exactly where grep+read sat, but token-cheap.
- **grep** = exact-literal confirmation only.

They overlap only superficially (both have a "search"): CodeGraph searches *symbol names*, Semble searches *meaning*. Positioning Semble as the grep-replacement (§5) keeps the two from competing for the same job.

---

## 5. Proposed routing (CodeGraph-first, Semble-as-grep-replacement)

Per the operator's decision, CodeGraph stays the primary investigation surface; Semble slots into the slot grep+read occupies today. The revised Investigate hierarchy:

```
1. codegraph_available?  → codegraph_search / callers / callees / impact / node     (structure)
2. semble_available?     → semble search "<concept>"   instead of subagent grep+read (discovery)
3. exact literal needed? → grep / glob                                              (confirmation)
4. chunk insufficient?   → Read the full file                                       (last resort)
```

Rationale for *not* making Semble primary: CodeGraph is already wired, trusted, and deterministic; Semble is the experiment. Demoting Semble to the grep slot is the smallest-blast-radius change and isolates the variable being evaluated. If the trial shows Semble's discovery is consistently better than CodeGraph's symbol search for "where does X live" questions, a future PRD can revisit promotion to semantic-first.

`find_related` has no current slot; the trial should probe whether it earns one (e.g. an optional step 2.5: "found a relevant chunk? `semble find-related` it to surface conceptual kin").

---

## 6. Provisioning model (optional, runtime-gated)

Mirror CodeGraph's gating exactly; **do not** touch `ortus init` / `ortus check` during the evaluation.

- **Detection:** `semble_available` ⇔ `semble` resolves on `$PATH` **and** the repo has been indexed at least once (or indexing succeeds on first call). Skip silently when absent — no errors, no mention in completion comments, identical to the CodeGraph degrade-silently contract.
- **Install path:** prefer **`uv tool install semble`** (binary on `$PATH`, offline thereafter) over `uvx --from "semble[mcp]" semble` on-demand. Reason in §7.C — the on-demand path collides with the sandbox network policy.
- **Per-repo opt-in:** an operator enables Semble for a repo by installing it + running `semble init` (drops `.claude/agents/semble-search.md`). Unprovisioned repos behave exactly as today.

This keeps the experiment fully reversible: uninstall `semble`, and every loop reverts to grep+read with zero prompt or code changes (the gate just goes false).

---

## 7. Risks, failure modes, open questions

**A. Index lifecycle vs subprocess-per-task.** Semble caches its index for a *session*; Ortus spawns a fresh process per task. So each task likely re-indexes (~250 ms). Cheap in absolute terms, but it means Semble's session-cache benefit is largely lost in Ortus — unlike CodeGraph's persistent on-disk `.codegraph/`. *Open question:* does the bash CLI persist an on-disk index across invocations, or re-index every call? If every call re-indexes a large repo, the 250 ms figure (an "average repo") may grow. **Measure on the Ortus repo and on a large target repo.**

**B. Retrieval quality on Ortus's corpus.** Semble's 0.854 NDCG@10 is on *their* benchmark. Ortus is Python + a lot of **bash scripts** + **markdown prompts/PRDs**. Static embeddings trained on general code may retrieve poorly on bash and prose. **Evaluation must measure quality on Ortus's own files**, not trust the headline number.

**C. Sandbox network collision.** `uvx --from "semble[mcp]" semble` fetches from PyPI on first run. Ortus's `.claude/settings.json` allowlists only GitHub + Anthropic hosts — the same wall that 403'd npm in a sibling project. Implications: (1) the on-demand `uvx` path will fail inside the sandbox; (2) a **pre-installed `uv tool install semble`** binary works fully **offline** for local-path indexing (no network), which is the right fit. Semble's git-URL/remote-repo mode *would* need network and is out of scope. **Decision input:** local-path mode + pre-installed binary = no allowlist change required.

**D. Overlap/confusion with CodeGraph.** Two "search" primitives risk the agent dithering. The §5 routing mitigates this (Semble occupies the grep slot, not codegraph_search's slot). The trial should watch for agents calling both redundantly for the same question.

**E. ZFC compatibility.** Ortus's Zero-Framework-Cognition principle wants *orchestration* decisions (termination, routing) to be data-driven, not inference-driven. Semble is a *retrieval aid inside investigation*, not an orchestration decision-maker — so it does not violate ZFC. Caveat: Semble results are non-reproducible across index states, which slightly complicates log replay / debugging (`scripts/replay-*.sh`). Note, don't block.

**F. Determinism / reproducibility.** Embedding similarity ranking can shift as the index changes (file edits mid-run trigger re-index + auto-watch). Two tasks in the same grind run might get different chunks for the same query if the codebase changed between them. Usually fine for investigation; flagged for completeness.

**G. Dependency weight & supply chain.** Adds a PyPI dependency (`semble` + model2vec + its model weights). Pinning + provenance should be considered before any non-experimental adoption (out of scope for the eval, in scope for a future implementation PRD).

---

## 8. Surface-by-surface fit analysis

The operator scoped all four surfaces. Each is analyzed for fit; the trial (§10) stages them.

### 8.1 grind Investigate step — **strongest fit**
Direct grep+read replacement per §5. This is where whole-file `Read`s concentrate, so the token win is largest. **Trial here first.**

### 8.2 grind Orient step — **moderate fit**
Orient currently reads recent bd activity + CodeGraph activity for files in the last ~20 commits. Semble could add a *conceptual* recent-work surface: `semble search` the titles/AC of the issue about to be claimed against the codebase to pre-locate the relevant area before Investigate even starts. Benefit: warmer start. Risk: redundant with Investigate; may not pay for itself. **Trial second, only if 8.1 passes.**

### 8.3 plan / PRD-decompose — **interesting, distinct fit**
Before `ortus plan` creates bd issues from a PRD, `semble search` each proposed work item against the existing codebase to detect *already-implemented* functionality → avoid duplicate/mis-scoped issues. This attacks a different problem (decompose accuracy, not investigation tokens) and is arguably the highest-leverage non-obvious use. Note: plan often runs on a *fresh/empty* repo (nothing to search) — value is highest when planning *into an existing* codebase. **Worth a dedicated mini-trial.**

### 8.4 AGENTS.md template + all verbs — **deferred until core proves out**
Baking the Semble snippet into the bundled `template/AGENTS.md` makes every new project + every verb's agent inherit it. High leverage, but premature before §8.1 validates retrieval quality on real loops. Sequence this last; it's the "graduate the experiment to default" step, which conflicts with the "optional, runtime-gated, don't touch init" provisioning choice until the eval clears.

---

## 9. Token economics (the core value)

- **Baseline cost** = whole-file `Read`s during Investigate, paid fresh per task (no-compaction).
- **Semble cost** = one index build (~250 ms, ~0 tokens — it's local compute) + chunk returns (small token payload).
- **Savings instrument:** `semble savings` (`~/.semble/savings.jsonl`) gives a per-call conservative estimate `(file_chars − snippet_chars)/4`. Run a trial grind, then read `semble savings --verbose` to get measured (not theoretical) savings on Ortus's own work.
- **Compounding:** savings-per-task × queue-depth, with no amortization discount (fresh window). This is the number that should drive the go/no-go.

---

## 10. Evaluation plan & decision criteria

**Trial setup:** install `uv tool install semble`; `semble init` in the Ortus repo; add Semble to the grind-prompt Investigate step as the §5 grep-replacement, gated on `semble_available`. Run a real grind over a backlog of ≥8 issues on a non-trivial repo (Ortus itself + one larger target).

**Metrics → thresholds:**

| Metric | How measured | Go threshold |
|---|---|---|
| Token savings | `semble savings --verbose` after the trial grind | ≥ 40% reduction in investigation-phase tokens (conservative vs the ~98% claim) |
| Retrieval quality | hand-score top-k for ~15 representative grind queries against Ortus's Python/bash/markdown | ≥ 0.7 precision@5 on Ortus's own corpus |
| Added latency | wall time of `semble search` per call (incl. any re-index) | ≤ 1.5 s per call on the Ortus repo; ≤ 3 s on the large target |
| Sandbox compatibility | run the trial grind inside the normal sandbox | zero network-policy failures (offline local-path mode) |
| Behavioral cleanliness | inspect grind logs | no redundant CodeGraph+Semble double-querying; no confusion loops |

**Decision gate:** adopt (write the implementation PRD) only if token savings + retrieval quality both clear. If quality clears but savings are marginal, narrow to `find_related` as an additive-only capability. If quality fails on bash/markdown, restrict Semble to Python-only files.

---

## 11. Recommendation

**Conditional adopt, via a staged trial.** The architectural synergy is real and specific to Ortus: the no-compaction / fresh-window-per-task model makes recurring investigation tokens the single biggest controllable cost, and Semble targets exactly that. The complementarity with CodeGraph is clean once Semble is positioned in the grep slot rather than competing with symbol search.

Proceed as:
1. **Phase 0 (this PRD):** evaluation — done.
2. **Phase 1 trial:** optional + gated, **grind Investigate only**, `uv tool install`-based (offline), instrumented with `semble savings`. Gate on §10.
3. **Phase 2 (if Phase 1 clears):** extend to Orient (§8.2) and plan/decompose (§8.3); write a committed implementation PRD.
4. **Phase 3 (if Phase 2 clears):** graduate to the bundled AGENTS.md template + provisioning in `ortus init` (§8.4) — the point at which it stops being "optional/experimental."

**Do not** wire `ortus init` / `ortus check` or the AGENTS.md template until Phase 3. Keep the experiment fully reversible (uninstall `semble` → gate goes false → loops revert to grep+read).

**Single biggest thing to verify first:** retrieval quality on Ortus's bash + markdown files (§7.B), because the headline token savings are worthless if the chunks are wrong.

---

## Appendix A — reference commands

```bash
# Install (offline-capable thereafter; preferred over uvx-on-demand for the sandbox)
uv tool install semble

# Drop the dedicated search sub-agent for Claude Code
semble init                       # → .claude/agents/semble-search.md

# Search (NL or identifier); path defaults to CWD
semble search "how is termination decided" .
semble search "grind_flock" . --top-k 10

# Semantic kin of a known location
semble find-related src/ortus/core/grind_loop.py 42 .

# Measured token savings
semble savings --verbose          # reads ~/.semble/savings.jsonl

# MCP (top-level agent only; sub-agents must use the bash CLI)
claude mcp add semble -s user -- uvx --from "semble[mcp]" semble
```

## Appendix B — references

- Semble: https://github.com/MinishLab/semble
- model2vec (static embeddings): https://github.com/MinishLab/model2vec
- CodeGraph integration: `src/ortus/prompts/grind-prompt.md` (Investigate / Orient steps)
- ZFC: `ZFC.md`, `prd/PRD-zero-framework-cognition.md`
