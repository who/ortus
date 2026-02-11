# Claude 4.6 Opportunities for Ortus

Report on leveraging Claude Opus 4.6 features within the Ortus template and Ralph Wiggum loop methodology.

---

## Executive Summary

Claude Opus 4.6 introduces six features directly relevant to Ralph loops: **adaptive thinking**, **effort parameter**, **compaction API**, **fast mode**, **128K output tokens**, and **1M context windows**. Three of these (effort, adaptive thinking, interleaved thinking) can improve agent quality immediately through prompt-level changes. Two (compaction, fast mode) require `ralph.sh` modifications. One (1M context) changes the math in our Context Management guidance.

The Ralph philosophy of **one task per invocation with fresh context** remains sound. These features enhance it rather than replace it.

---

## Feature Analysis

### 1. Effort Parameter (GA) — HIGH IMPACT

**What it does:** Controls how many tokens Claude spends on thinking, text, and tool calls. Four levels: `max`, `high` (default), `medium`, `low`.

**Why it matters for Ralph:** The effort parameter is a direct lever for the subagent allocation model. Different effort levels map perfectly to different subagent categories:

| Subagent Category | Recommended Effort | Rationale |
|-------------------|--------------------|-----------|
| Reads (explore, find, summarize) | `low` | Speed and cost over depth — scanning is simple |
| Writes (implement, edit) | `high` | Need careful code generation |
| Validation (tests, lint, build) | `medium` | Mechanical execution, moderate reasoning |
| Reasoning (architecture, security) | `max` | Deepest possible analysis, no token constraints |

**Key detail:** At `low` effort, Claude makes fewer tool calls and skips thinking for simple tasks. At `max` (Opus 4.6 only), Claude thinks with no constraints. This gives us fine-grained cost/quality control per subagent type.

**Current gap in Ortus:** The Subagent Strategy allocation table in `prompt.md` and `ralph-prompt.md.jinja` specifies model and parallelism but not effort level. Adding an `Effort` column would let Ralph make smarter subagent invocations.

**Recommendation:**
- Add `Effort` column to the allocation table in both prompt files
- This is a prompt-level change only — no `ralph.sh` modification needed
- Claude Code's `--model` flag already exists; effort is controlled via the API, but the *prompt guidance* to the agent about how to think about effort allocation is what matters here

### 2. Adaptive Thinking — MEDIUM IMPACT

**What it does:** Replaces manual `budget_tokens` with `thinking: {type: "adaptive"}`. Claude dynamically decides when and how much to think. Automatically enables interleaved thinking (thinking between tool calls).

**Why it matters for Ralph:** Interleaved thinking is significant for agent quality. Claude can now reason between each tool call — after reading a file, after seeing test results, after each code edit. This was previously a beta feature requiring a special header; it's now automatic with adaptive thinking on Opus 4.6.

**Current gap in Ortus:**
- The Ultrathink Directive in both prompts says "Use extended thinking to reason through the problem before implementing" — this language is outdated. With adaptive thinking, Claude thinks *throughout* execution, not just upfront.
- `ralph.sh` invokes Claude Code via `claude -p`, which handles API params internally. The prompt language should reflect that thinking now happens interleaved, not as a single upfront block.

**Recommendation:**
- Update Ultrathink Directive language: "extended thinking" → "adaptive thinking" or just "deep reasoning"
- Note that Claude will now think between tool calls automatically — the agent doesn't need to explicitly plan everything upfront
- No `ralph.sh` changes needed (Claude Code handles the API params)

### 3. Compaction API (Beta) — LOW IMPACT for Ralph, HIGH for derived projects

**What it does:** Server-side context summarization. When input tokens exceed a threshold (default 150K), the API automatically summarizes earlier conversation turns. Supports custom summarization instructions and `pause_after_compaction` for injecting preserved context.

**Why it matters:**
- **For Ralph loops: LOW impact.** Ralph's core design — one task per invocation with fresh context — sidesteps the need for compaction entirely. Each iteration starts at 0 tokens. This is the right design.
- **For projects built from the Ortus template: HIGH impact.** If a generated project uses long-running interactive sessions (not Ralph loops), compaction prevents context window exhaustion.

**Current gap in Ortus:** The Context Management section says "Fresh ~200K token window per invocation" which is correct for Ralph. But the template should also guide projects that use Claude interactively (not just via Ralph).

**Recommendation:**
- Add a brief note in Context Management: "If using long-running interactive sessions instead of Ralph loops, enable the Compaction API to extend effective context beyond 200K"
- Consider adding compaction configuration to `ralph.sh` as an optional safety net for tasks that generate massive tool output within a single invocation (rare but possible)
- Custom compaction instructions could preserve beads state: "Focus on preserving: current issue ID, files modified, test results, and remaining acceptance criteria"

### 4. Fast Mode (Research Preview) — MEDIUM IMPACT

**What it does:** 2.5x faster output token generation for Opus 4.6. Same model, same intelligence. Premium pricing: $30/$150 per MTok (6x standard rates).

**Why it matters for Ralph:** Wall-clock time per loop iteration is a real bottleneck. Fast mode on the main Ralph context (which runs Opus for orchestration) could cut iteration time significantly. The cost tradeoff is steep (6x), but for time-sensitive deployments (production hotfixes, tight deadlines), it's valuable.

**Current gap in Ortus:** `ralph.sh` has no speed configuration. The `claude` CLI invocation is hardcoded without speed parameters.

**Recommendation:**
- Add `--fast` flag to `ralph.sh` that passes through to Claude Code (if/when Claude Code supports it)
- In the Subagent Strategy section, note that fast mode is available for latency-sensitive workflows but at 6x cost
- Do NOT make fast mode the default — standard speed is the right default for cost-conscious automation
- Consider fast mode primarily for the main orchestrator context, not subagents (subagents are already Sonnet, which is fast and cheap)

### 5. 128K Output Tokens — LOW IMPACT

**What it does:** Doubles the previous 64K output limit. More room for thinking + code generation in a single turn.

**Why it matters for Ralph:** Most Ralph iterations don't approach 64K output tokens because the one-task-per-invocation model keeps scope tight. The increased limit provides headroom for tasks involving large file generation or extensive test output, but doesn't change the fundamental workflow.

**Recommendation:**
- No prompt or `ralph.sh` changes needed
- The Context Management section already captures the right philosophy ("one tight task = 100% smart zone utilization")

### 6. 1M Context Window (Beta) — MEDIUM IMPACT

**What it does:** 5x the standard 200K context window. Available for Opus 4.6 on tier 4+ organizations. Premium pricing above 200K (2x input, 1.5x output).

**Why it matters for Ralph:** Changes the Context Management math. With 1M tokens:
- The "smart zone" (40-60%) is 400K-600K tokens — enormous
- Large codebases could load more context directly instead of using read subagents
- BUT: the 2x input pricing above 200K makes this expensive for automation

**Current gap in Ortus:** Context Management section hardcodes "~200K" as the budget.

**Recommendation:**
- Update Context Management: "Fresh ~200K token window per invocation (up to 1M with beta access)"
- Keep the "smart zone" percentages — they're about model quality degradation at high utilization, which likely still applies regardless of window size
- Note that the 200K standard window is the right default for cost-conscious Ralph loops
- 1M context is more relevant for investigation-heavy tasks than for implementation tasks

---

## Breaking Changes Affecting Ortus

### Prefill Removal
**Risk: LOW.** `ralph.sh` uses `claude -p "$(cat prompt.md)"` which sends a user message, not an assistant prefill. No impact on current Ralph workflow. But templates that build custom API integrations need to know.

### Tool Parameter Quoting
**Risk: LOW.** Standard JSON parsers handle the differences. Only affects raw string parsing of tool call arguments.

### Deprecations
- `budget_tokens` deprecated → migrate to adaptive thinking + effort
- `interleaved-thinking` beta header deprecated → removed automatically with adaptive
- `output_format` → `output_config.format`

**These affect API-level code, not Ralph prompts.** If Ortus ever generates API wrapper code, those templates would need updating.

---

## Recommended Changes (Prioritized)

### P0: Update Subagent Allocation Table with Effort Column

**Files:** `prompt.md`, `template/ortus/prompts/ralph-prompt.md.jinja`

Current table:
```
| Category | Model | Parallelism | Examples |
```

Proposed table:
```
| Category | Model | Effort | Parallelism | Examples |
|----------|-------|--------|-------------|----------|
| Reads | Sonnet | low | up to 500 parallel | explore codebase, find files, read context, summarize |
| Writes | Sonnet | high | N parallel | implement changes, create files, edit code |
| Validation | Sonnet | medium | exactly 1 serial | run tests, linting, builds |
| Reasoning | Opus | max | 1 | architecture decisions, tricky bugs, security review |
```

This gives the agent actionable guidance on effort-per-subagent-type. The `effort` parameter is GA and doesn't require beta headers.

### P1: Update Ultrathink Directive for Adaptive Thinking

**Files:** `prompt.md`, `template/ortus/prompts/ralph-prompt.md.jinja`

Current: "Use extended thinking to reason through the problem before implementing."

Proposed: Rename to "Deep Reasoning Directive" or keep "Ultrathink" but update the body:

```markdown
## Ultrathink Directive

Claude uses adaptive thinking — it decides when and how deeply to reason, including
between tool calls (interleaved thinking). For the following problem types, ensure
the effort level supports deep reasoning (`max` for Opus, `high` for Sonnet):

- Architecture decisions spanning multiple files
- Debugging subtle or intermittent issues
- Performance optimization trade-offs
- Security-sensitive code paths

The agent will think between tool calls automatically. You do not need to
"think first, then act" — reasoning and action are interleaved.
```

### P2: Update Context Management for 1M Window

**Files:** `prompt.md`, `template/ortus/prompts/ralph-prompt.md.jinja`

Add a note about the 1M beta:

```markdown
- Fresh ~200K token window per invocation (1M available in beta for tier 4+ orgs)
- Standard 200K is the recommended default for Ralph loops — larger windows cost more and rarely improve single-task execution
```

### P3: Add ralph.sh Speed Flag (Future)

**File:** `template/ortus/ralph.sh`

When Claude Code supports a `--speed fast` or equivalent flag, add:

```bash
SPEED=""
# In argument parsing:
--fast) SPEED="--fast"; shift ;;
# In invocation:
result=$(claude -p "$(cat ...)" $SPEED --output-format stream-json ...)
```

This is blocked until Claude Code exposes the fast mode parameter. Track as a future issue.

### P4: Compaction Safety Net (Optional)

**File:** `template/ortus/ralph.sh`

For tasks with massive tool output that might approach 200K within a single invocation, compaction could be enabled as a safety net. This is an edge case — Ralph's one-task design normally prevents this. Consider only if users report context exhaustion during single-task execution.

---

## What NOT to Change

1. **One-task-per-invocation model** — Compaction does not replace this. Fresh context per iteration is better than summarized stale context. The Ralph philosophy is validated by 4.6, not challenged by it.

2. **Subagent-heavy architecture** — Effort levels enhance it. Low-effort Sonnet subagents for reads are now even cheaper and faster.

3. **Backpressure/funnel model** — Exactly 1 serial validation remains correct. Nothing in 4.6 changes the argument for serial validation.

4. **Beads as cross-iteration memory** — Compaction is within-session memory. Beads comments/activity are cross-session memory. These are complementary, not competing.

---

## Summary Table

| Feature | Impact on Ralph | Action | Priority |
|---------|----------------|--------|----------|
| Effort parameter | HIGH — direct subagent control | Add effort column to allocation table | P0 |
| Adaptive thinking | MEDIUM — better interleaved reasoning | Update Ultrathink language | P1 |
| 1M context (beta) | MEDIUM — changes budget math | Update Context Management note | P2 |
| Fast mode (preview) | MEDIUM — reduces wall-clock time | Add ralph.sh flag when CLI supports it | P3 |
| Compaction (beta) | LOW for Ralph, HIGH for interactive | Optional safety net | P4 |
| 128K output | LOW — headroom increase | No changes needed | — |
