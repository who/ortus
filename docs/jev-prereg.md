# Jev preregistered decision rule

This rule decides whether Jev becomes a default, stays on a few sites, or is
scrapped. It is written down before the evidence is read, so the call is
computed rather than argued. `ortus judge replay --outcomes` applies the
offline half to every site and reports it as `sites.<site>.prereg`. The
thresholds below are the constants in `src/ortus/core/judge_scorers.py`, and
`tests/test_judge_scorers.py` fails when the two disagree.

## Offline, per site

A site is kept only if it has 100 or more labeled decisions AND all of these
hold:

- Jev's accuracy beats the code baseline on the same decisions (McNemar
  two-sided p < 0.05, Jev ahead).
- Every binary probability has AUC ≥ 0.70 with a bootstrap 95% CI
  lower bound > 0.60.
- p95 latency ≤ 1.5 s.
- Failure rate (service, key, sdk, text_unavailable) ≤ 5%.
- First-shown-option share ≤ 40% on picks with 3 or more options.

A site with fewer than 100 labeled decisions is "insufficient" and is not
kept.

## End to end

Jev-on (the kept sites, enforce, vectors as context, code decides) is run
against a Jev-off control, with 5 or more paired runs per fixture. Jev-on wins
only if both hold:

- Its median token-based cost per closed bead (worker plus Jev tokens priced
  from one fixed table) is lower, with the paired-ratio bootstrap 95% CI
  entirely below 1.0.
- There is no guardrail loss: closed beads per run not lower by more than 5%,
  reopen or rework rate not higher by more than 5 percentage points, human
  parks per run not higher, worker timeouts per run not higher.

## Decision

Jev becomes the default only if end to end passes and every site enabled in it
passed offline. Otherwise Jev is scrapped as a default and kept only on the
individual sites that passed offline, and only with no guardrail loss. A null
result is a scrap.

## How replay labels each site

Labels come from the tracker snapshot `ortus judge outcomes` takes through
`bd --readonly`, plus the logs themselves. "Clean" means closed, never
reopened, and without the `human` label. A decision whose issue bd no longer
lists is excluded as `issue_missing`, one whose bead is still open is
`unresolved`, and one whose history bd could not answer is
`history_unavailable`.

- **readiness**: ready when clean, not ready when human-labeled or reopened.
  The probability is 1 - `open_product_decision` (or the legacy
  1 - `needs_human`).
- **pre_turn**: proceed when clean, human when human-labeled.
- **router**: a cheap tier that was reopened, timed out, or needed more than
  one window was under-routed; a tier that closed cleanly in one window makes
  it and every tier above it correct. Over-routing is not observable, so it is
  never charged against Jev.
- **pre_tool**: only a `park_bead` is labeled automatically, wrong when the
  bead later closed without the human label. Allow and deny labels come from
  the `--tool-labels` rubric, keyed by decision id.
- **progress**: not looping when the head advanced, files appeared, or the
  bead closed in that window; looping when the window timed out with no
  commits.
- **stuck**: escalate when human-labeled, replan when the spec changed in bd
  history between the decision and the close, continue when the bead closed
  within the next 2 windows.
- **triage**: planner_fix when a spec change preceded the close, needs_human
  when the closed bead carries the human label, pin_skew when neither did.
- **post_turn**: human when human-labeled, preserve when closed without one.
  `work_complete` is true when this window closed the bead and it was never
  reopened; `transient_failure` is true when the next window closed it with no
  spec change between.

A decision whose Jev request failed scores as the baseline action, because
code decides when Jev has no answer; the failure counts against the failure
rate instead. Records written before `baseline_action` existed are scored
against the documented baseline: `baseline_stuck_action` for stuck, proceed,
the baseline tier, allow, continue, the human park, and preserve elsewhere.
