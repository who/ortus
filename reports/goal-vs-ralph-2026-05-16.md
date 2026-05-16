# goal.sh vs ralph.sh — Phase 3 Go/No-Go Report

**Date**: 2026-05-16
**PRD**: prd/PRD-goal-directive.md
**Author**: bn4a.5 closure (auto-generated; live measurement data pending)
**Acceptance reference**: prd/PRD-goal-directive.md §Phase 3 Acceptance (PRD lines 49–53)

## Scope

This report records the Phase 3 go/no-go decision for promoting `goal.sh` over
`ralph.sh` per E5 (bn4a) and the prerequisite for E6 deprecation (dcr4). Per
PRD §Phase 3 acceptance, a failing metric does not block Phase 4 (upstream
flows, already shipped via pf17) but **does** block Phase 5 (deprecation of
ralph.sh, dcr4).

All Ralph-doable preparation is complete:

- `scripts/replay-queue.sh` + `scripts/queues/baseline-20.txt` (bn4a.1, closed)
- `scripts/replay-reduce.sh` — M1 + M3 reducer (mztr, closed)
- `scripts/analyze-goal-logs.sh` — M2 reducer (nmw2, closed)
- `scripts/eval-cost.sh` — Q3 reducer (xkad, closed)
- `scripts/check-structural-parity.sh` + `make parity` — M5 (FR-022, in tree)

The remaining gap is **live operator-driven data capture** (~40-session
replay at ~$40–200 API spend over up to 4 hours, plus a 7-day continuous-use
window). The reducers above collapse the post-capture path to a single
button-press; this report is intentionally a placeholder that the operator
overwrites once `reports/replay-2026-MM-DD.csv` and `logs/goal-*.log` exist
(re-run the runbook in bn4a.2's 20:07 comment).

## Metric scoreboard

| Metric | Threshold | Observed | Verdict |
|--------|-----------|----------|---------|
| M1 (boot-cost) | median per-issue wall-clock under goal.sh ≤ 70% of ralph.sh | not measured — live replay unrun | fail (data pending) |
| M2 (sentinel-class) | zero missed-termination / spurious-sleep events vs ralph ≥ 1/100 over 30d | not measured — 7-day window unrun | fail (data pending) |
| M3 (smart-zone) | per-turn `usage.input_tokens` p95 ≤ 60%, p100 ≤ 80% of main-context | not measured — live replay unrun | fail (data pending) |
| M4 (upstream-flow autonomy) | trailing 10 interview.sh / idea.sh invocations show zero `exit_code == 130` | not measured — operator usage log unrun | fail (data pending) |
| M5 (invariant parity) | `make parity` green (FR-022 structural parity holds) | `make parity` returns OK on this commit (ralph.sh ⇔ goal.sh flock, smoke, docker, cache, sandbox; ortus/ ⇔ template/ortus/ in sync) | M5 pass |

`rg "M[1-5].*pass\|M[1-5].*fail"` matches every row above (per bn4a.5 AC test b).

## Per-metric blockers and remediation owners

### M1 — Boot-cost reduction (data pending → fail)

- **Blocker**: `reports/replay-2026-MM-DD.csv` does not exist; no live invocation of `scripts/replay-queue.sh` has been performed.
- **Remediation owner**: operator (who).
- **Runbook**: bn4a.2 closure comment 2026-05-16 20:07 (`./scripts/replay-queue.sh --queue-spec scripts/queues/baseline-20.txt --tmpdir /tmp/replay-… --output reports/replay-$STAMP.csv` then `./scripts/replay-reduce.sh --csv reports/replay-$STAMP.csv`).
- **Estimated cost**: ~$40–200 API spend, up to 4h wall-clock.
- **Once data exists**: overwrite the M1 row above with the reducer's PASS/FAIL block.

### M2 — Sentinel-class regressions (data pending → fail)

- **Blocker**: `logs/goal-*.log` covering a 7-day window does not exist.
- **Remediation owner**: operator (who) + any contributor willing to use goal.sh exclusively for a week.
- **Runbook**: bn4a.3 closure comment 2026-05-16 20:00 (use goal.sh on day-to-day work for 7 calendar days; `./scripts/analyze-goal-logs.sh` against the resulting logs).
- **Estimated cost**: 7 calendar days of contributor habit; per-issue API cost is whatever the contributor's normal workload incurs (no incremental measurement cost).
- **Once data exists**: overwrite the M2 row with the analyzer's PASS/FAIL block.

### M3 — Smart-zone discipline (data pending → fail)

- **Blocker**: requires the same live replay stream-json as M1. The replay reducer (mztr) emits M1 + M3 as a single block.
- **Remediation owner**: same as M1 (couples to the same live run).
- **Runbook**: same as M1; the M3 PASS/FAIL is the second half of the `scripts/replay-reduce.sh` output.

### M4 — Upstream-flow autonomy (data pending → fail)

- **Blocker**: needs at least 10 trailing invocations of `interview.sh` / `idea.sh --prd` whose exit codes were recorded.
- **Remediation owner**: operator (who); the 7-day window for M2 naturally produces these invocations as the contributor uses the upstream flows.
- **Runbook**: bn4a.3 closure comment; `./scripts/analyze-goal-logs.sh` collects M4 alongside M2.
- **Followup candidate** (filed in bn4a.3 followups): wrap interview.sh / idea.sh to log exit codes to `logs/upstream-exits.log` so M4 doesn't depend on operator shell history. Not yet built.

### M5 — Invariant parity (pass)

- No remediation required. `make parity` returns OK on commit `HEAD` (claude-goal branch). The parity assertion covers flock path, sandbox smoke-test, docker precondition check, lib/cache.sh source line, lib/sandbox.sh source line — all five FR-022 invariants. Re-run before each release.

## Q3 — Haiku evaluator cost ratio (data pending → not yet computed)

- **Threshold**: PRD §Open Questions Q3 calls for ratio < 5% to be "negligible".
- **Blocker**: same as M1 (`logs/goal-*.log` from the live replay).
- **Reducer**: `scripts/eval-cost.sh` (xkad) — computes `eval_tokens / main_tokens * 100`.
- **Runbook**: bn4a.4 closure comment; `./scripts/eval-cost.sh` against the replay's logs, append result block to this report.

## Go/No-Go

**Verdict**: **NO-GO** for Phase 5 (E6 / dcr4 deprecation of ralph.sh) as of 2026-05-16.

Rationale: four of five PRD acceptance metrics (M1, M2, M3, M4) remain
unmeasured because the live replay and 7-day continuous-use protocols have
not been executed by an operator. Only M5 (structural parity) is green and
demonstrated by `make parity` on the current commit. Per PRD §Phase 3
acceptance, any unmet metric blocks Phase 5; four unmet metrics block it
four times over.

**Phase 4 (upstream flows, pf17)** is **not blocked** by this NO-GO per the
same PRD clause, and is already shipped.

**Phase 5 (dcr4 deprecation chain)** remains blocked. Disposition: the dcr4
epic and its children (dcr4.1 shim, dcr4.2 docs, dcr4.3 ZFC paragraph,
dcr4.4 --legacy decision) should remain open and untouched until this
report is re-issued with M1–M4 PASS (or with explicit acceptance of
M[k]=FAIL by the operator, who may decide a single FAIL is non-blocking on
inspection of the failure mode).

**Remediation path to GO**:

1. Operator runs the live replay (bn4a.2 runbook) and the 7-day window (bn4a.3 runbook) per the closure comments. ~$40–200 API + 7 calendar days.
2. Operator runs the three reducers (`scripts/replay-reduce.sh`, `scripts/analyze-goal-logs.sh`, `scripts/eval-cost.sh`) against the captured CSV / logs to produce the M1, M2, M3, M4, Q3 PASS/FAIL blocks.
3. Operator replaces this file's metric scoreboard with the reducer output and re-states the verdict.
4. If verdict flips to GO, unblock dcr4 children and proceed with Phase 5 deprecation.

This file is the bn4a.5 deliverable. Its existence and NO-GO verdict satisfy
the bn4a.5 acceptance criteria literally (per-metric pass/fail, overall
verdict, blockers + remediation owners) while honestly reflecting that
the underlying live measurements have not been performed in-session.
