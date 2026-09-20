# Judge rollout by seat

The optional Jev gate chooses an available configured worker or `skip` before
grind launches a worker, and records every answer for offline review. It never
holds a claim back for low confidence, human need or risk. It is disabled by default. Ordinary installations
need neither the optional SDK nor a TypeSafe key. A key in the environment
does not enable the gate.

Start with one seat, then review its shadow observations before enforcing
decisions. The two-seat recipe below keeps the second seat disabled. These
instructions do not enable any production configuration automatically.

The diamond policy, locked 2026-09-20, is what the rest of this document
describes. System One answers with typed probability vectors — a route and its
confidence, a human-need probability, a risk score and its confidence — and
those vectors travel into System Two, the worker backend, as recorded context.
The pre-turn hard escalate is scrapped: no confidence floor, human-need
probability or risk score withholds a claim, and no offline review can put one
back. What remains for review is soft: bands to log, flag or rewrite a prompt
around. The pre-tool hook described at the end of this document is a separate
phase over irreversible tool calls, and its floors are fixed literals there —
they are not claim gates and calibration never touches them.

## Install and opt in

Install the extra into the same environment that runs the CLI:

```bash
uv tool install --force 'ortus[judge]'
```

For a source checkout, use `uv sync --extra judge` and run the commands below
through `uv run ortus`. The extra pins `typesafe-sdk==0.7.0`; the configured
model is the exact version `jev-1.13.0`, not a floating model alias.

Have the host runner or your secret manager inject `TYPESAFE_API_KEY` into
the process environment. Do not put its value in `.ortusrc`, issue text,
command arguments or checked-in files. Ortus has no dotenv loader and needs
no host-shell gate script.

Merge this table into the product repository's `.ortusrc`, preserving its
existing backend and profile settings. Do not add a second `[judge]` table:

```toml
[judge]
enabled = false
seat = "ortus"
model = "jev-1.13.0"
failure_mode = "open"
include_issue_text = true
```

Keep the gate off in stored configuration during the pilot. Inspect resolution
first, then explicitly enable it for one invocation:

```bash
ortus grind . --judge --dry-run
ortus grind . --judge --tasks 1 --iterations 1
```

Dry-run prints safe resolved settings without a judge request, claim or
decision-log write. The live command can launch a worker and spend provider
budget. `--iterations 1` bounds worker launches; skip and human decisions stop
the invocation without spending an iteration. Only observed bd closes count
toward `--tasks`.

Enable precedence is `--judge` or `--no-judge`, then
`ORTUS_JUDGE_ENABLED`, then layered project/user TOML, then disabled.
`ORTUS_JUDGE_MODEL` overrides the TOML model. Clear an unintended model
override before the pilot so dry-run shows `jev-1.13.0`.

Seats are explicit aliases, not directory names or a new scheduler. Aliases
must start with a letter and contain only letters, digits, `_` or `-`.
A numeric directory such as `01` needs an alias such as `ortus`.

## Defaults and decisions

| Setting | Default | Meaning |
| --- | --- | --- |
| `mode` | `enforce` | Apply decisions; `shadow` records intended actions |
| `pre_tool`, `post_turn` | `false` | Independent tool-hook and advisory outcome phases |
| `routes` | `claude`, `codex`, `skip`, `human` | Allowed choices, filtered by available workers |
| `timeout_seconds` | `1.5` | One request deadline, SDK retries disabled |
| `failure_mode` | `open` | Service failures use the original backend |
| `low_confidence` | `human` | Validated and inert; the pre-turn gate reads no value of it |
| `route_confidence`, `noul_confidence`, `risk_confidence` | `0.8` each | Also inert pre-turn; confidence is recorded, not enforced |
| `human_threshold` | `0.8` | Also inert pre-turn; human-need probability is recorded, not enforced |
| `risk_threshold` | `1.5` | Also inert pre-turn; rubric scores are 0, 1, 2 and are recorded |
| `seat` | `default` | Set explicitly to `ortus` for this pilot |
| `include_issue_text` | `true` | Screened title, first Objective line and AC lines travel |
| `include_log_tail` | `false` | The pre-turn packer never reads log tails |
| `title_cap`, `objective_cap`, `acceptance_cap`, `tool_cap` | `160`, `1024`, `1024`, `512` | Character caps; oversized source fields are omitted |
| `total_bytes_cap` | `8192` | Serialized UTF-8 state budget |
| `sensitive_paths` | empty | Additional literal paths to omit from text |

The adapter asks Choice, Noul and Score questions in one System One request.
Noul supplies a probability `p`, not a separate confidence field. Ortus computes
its confidence as `max(p, 1-p)`. A probability near 0.5 is recorded as an
uncertain answer; it does not change what runs.

Missing keys, missing SDKs, timeouts, service errors and malformed answers
follow `failure_mode`. In `open` mode the original worker backend proceeds
with normal verification and closure. In `closed` mode the issue requires
human handling. An unavailable backend is not offered; a response naming it
is an invalid answer and follows the same failure policy.

Confidence, human-need probability and risk score are recorded in the decision
row and change nothing about what runs. A `human` route names no worker, so the
baseline runs and the answer stays in the log. A confident skip stops this
invocation and leaves the issue available for a later run. Every other answer
launches a worker bound to the selected id. A `human` label, an explicit policy
denial and a fail-closed service failure remain the only pre-turn stops.

Claude, Codex, Grok and OpenCode baselines support the enabled gate.
Optional workers without binaries or valid profiles are excluded. OpenCode
also requires the existing `[local]` configuration and server preflight.
The `local` backend uses `opencode` in judge requests while retaining its
original backend and profile settings for execution. Explicit backend/model/effort
overrides pin routing to the baseline. Every available route is prepared before
claiming work; a preparation failure requires human handling. Claim ownership,
logging and configuration errors are not service outages and do not fail open.

## Privacy and authority

The request carries bounded metadata: issue id, type, labels, priority, seat,
phase and available backends. Metadata can still identify work; review it
before enabling a seat.

It also carries a screened title, the first Objective line and AC lines,
because metadata alone gives the judge almost no evidence for semantic routing.
Review the seat's issue text before enabling it. A seat whose work must stay
local sets `include_issue_text = false` and gets the metadata-only packet
instead. A `judge-private` label always suppresses issue prose.

Sensitive patterns, credential values and configured sensitive paths cause
whole-field omission, as do size limits. Pattern screening cannot prove
arbitrary prose safe, so review a seat's work before enabling it. No repository
files, attachments, raw transcripts or environment dumps enter the packed state.

The policy keeps arithmetic and control flow in code. The model classifies
meaning into typed route, probability, risk and confidence values. The adapter
validates the model pin, answer types, ranges and offered routes; policy code
applies the routing rules. Provider explanations and exception text do not choose an
action. This is the ZFC audit boundary. Synthetic replay includes contradictory
provider prose to exercise that boundary, but does not prove semantic accuracy.

The gate authorizes a worker launch, not an irreversible tool action. Existing
backend sandboxes, CodeGraph requirements, goal judges and worker checks still
apply. Workers retain commit and `bd close` responsibility. Grind observes bd
state; neither a judge answer nor an outcome log can close an issue.

## Inspect, resolve and roll back

Progress goes to stderr. `logs/jev-decisions.jsonl` contains a decision and a
linked outcome identified by `run_id` and `decision_id`. Inspect the effective
action, reason, failure, backend and observed status. Records contain typed
answers, model/criteria identifiers, latency and reported token counts, without
raw prompts, provider prose or keys. The log file is private, at most mode 0600.
Treat it as operational data and do not commit it.

Human handling is local: a `human` label, a typed-reason bd comment and a
progress banner. Ortus sends no external notification. Fresh unused claims
are released to open on skip or human. Resumed claims stay in progress with
their existing owner and work intact. Once a worker launches, normal orphan
and completion handling applies.

Read the issue and comments, resolve the stated credential, decision or
configuration problem, and then remove the label from that id:

```bash
bd show <issue-id>
bd comments <issue-id>
bd label remove <issue-id> human
ortus grind . --judge --tasks 1 --iterations 1
```

Do not reset a resumed claim or discard its dirty work to clear a banner.
On a claim-cleanup error, inspect status and assignee before retrying. A log
write error also stops enforcement visibly; repair the private log path before
retrying rather than treating the error as a provider failure.

For an immediate kill-switch on the next invocation:

```bash
ortus grind . --no-judge --tasks 1 --iterations 1
```

This overrides pre-turn enablement from config or environment. For lasting rollback,
set the selected seat's `enabled = false` and remove `ORTUS_JUDGE_ENABLED` from
the host environment. Also set `judge.pre_tool = false` and
`judge.post_turn = false` if those independent phases were enabled.
Neither command interrupts a worker already running. Disabling the gate does
not remove existing human labels or change claim ownership.

## Offline evidence and pilot measurements

`tests/fixtures/jev/replay.jsonl` contains synthetic authored responses and
explicit expected actions, not recordings from a live provider. Cases cover
ordinary coding, missing human credentials, uncertainty, unavailable backends,
dangerous actions, skip and service failure. The contract tests run the real
adapter and policy through grind with an in-memory tracker and fake workers.
They also compare disabled prompts, argv, tracker mutations and counters, with
and without a key, and exercise missing SDK/key policy without network access.

```bash
uv run pytest tests/test_judge_mvp_contract.py -n auto --test-timeout=60 -q
uv run pytest tests/test_core_config.py tests/test_grind_prompt_content.py -n auto --test-timeout=60 -q
```

For the single-seat pilot, record the sample count, model pin, effective config,
decision distribution, service failures and operator corrections. Measure added
gate latency with targets below 500 ms p50 and 1.5 s p95. Decision `latency_ms`
covers the adapter and policy; measure end-to-end overhead separately to include
route preparation, claims and durable logging. Compare gated-turn cost with a
normal coding-worker turn, targeting at least an order of magnitude less.
The offline fixtures establish none of these latency, cost or accuracy claims.

Token usage is recorded only when supplied by the service; absent usage stays
unknown. `measured_cost_usd` is null because no pricing measurement is available.
Use actual billing and an explicit price basis for cost comparisons. Do not
infer savings from skips alone or turn missing usage into zero cost. Review the
pilot evidence before changing thresholds or enabling any other seat.

## Threshold calibration runbook

Calibration answers one question offline: if a soft band had been in place,
what would it have said about the decisions that already happened? It reads the
private event log and the operator's labels, writes one metrics file, and stops
there. No provider is called, no configuration is read for enforcement or
written back, and no candidate changes what ran or what will run.

Collect a pilot sample first, as described above, and label its decisions. A
label names `expected_action` (`proceed`, `human` or `skip`), `needs_human` as
a boolean, and optionally `worker_cost_usd` for that decision's worker turn.
Labels are the operator's judgement about the work, not the gate's opinion
about itself; read the issue and the outcome before writing one. Rows recorded
with `include_issue_text = false` were judged on screened metadata alone, so
they measure that thinner input and are weak evidence about anything else.

Then write the candidates as explicit TOML tables, at most twenty of them:

```toml
[[candidate]]
name = "unsure-route"
action = "flag"
route_confidence_min = 0.8

[[candidate]]
name = "risky-and-unsure"
action = "rewrite"
action_risk_max = 1.0
risk_confidence_min = 0.8
note = "candidate rewrite trigger for the pre-turn criteria"
```

Each table needs a `name`, an `action` and at least one bound. The action is
`log`, `flag` or `rewrite` — a report, an item for review, or a prompt change.
A bound reads exactly one recorded answer: `route_confidence_min`,
`noul_confidence_min` and `risk_confidence_min` fire below their value, and
`needs_human_max` and `action_risk_max` fire above it. The pre-diamond setting
names `route_confidence`, `noul_confidence`, `risk_confidence`,
`human_threshold` and `risk_threshold` still parse and are read as the
equivalent soft bound, so an older candidate file stays usable without becoming
a gate again. A table that names an enforcement outcome instead — `escalate`,
`block`, `blocks_claims`, `enforce`, `effective_action`, `low_confidence`,
`mode`, `deny`, `stop`, `gate` — is refused, and so is an `action` outside the
three soft verbs. There is no hard escalate to calibrate.

Run the comparison against the same events the metrics came from:

```bash
ortus judge replay logs/jev-decisions.jsonl --labels pilot-labels.json \
  --thresholds candidates.toml --output pilot-metrics.json
```

The report appears under `calibration` beside the usual metrics. Each candidate
carries its bounds and four counts, all over explicit denominators: `fired` over
the labeled decisions that had answers, `caught_needs_human` and
`missed_needs_human` over the decisions labeled as needing a human, and
`over_caution` over those that did not. A service failure has no vector to band
and is counted in `unanswered` rather than scored. `recorded_actions` repeats
the action distribution read from the log, and it is identical whatever
candidates are supplied — that is the evidence the comparison changed nothing.
Every candidate reports `blocks_claims: false` and `action_changes: 0` because
neither can be otherwise. `production_soft_settings` carries the current values
with `enforced_pre_turn: false`, so a candidate is always read next to what
production actually holds. An unlabeled sample is an error, not an empty
report.

Judge the result against the pilot bars: added gate latency below 500 ms p50
and 1.5 s p95, and gate cost no more than a tenth of a measured coding turn
where billing data exists. Where it does not exist, record that it is unknown;
`measured_cost_usd` stays null rather than becoming zero. A candidate that
catches more labeled human-need cases is not automatically better — read its
`over_caution` count at the same time, since under this policy a firing band
costs review attention, never a withheld claim.

Applying anything is a separate, human decision: edit the repository's
`[judge]` table yourself after review. Calibration ranks nothing, optimizes
nothing and learns nothing. `tests/fixtures/jev/labeled_replay.jsonl` is a
small synthetic sample that exercises these mechanics in the test suite; it
establishes no latency, cost or accuracy claim about a real pilot.

```bash
uv run pytest tests/test_judge_calibration.py -n auto --test-timeout=60 -q
```

## Named packs and seat selection

A seat registry selects configuration for one invocation. It does not schedule
workers or discover directories. Select an alias with `--judge-seat`, then
`ORTUS_JUDGE_SEAT`, then `judge.seat`, with `default` as the fallback. Aliases
contain at most 64 characters. Once `[judge.seats]` exists, the selected alias
must be defined, including `default` if no other alias is selected. Legacy
single-seat configuration without a registry still works. Numeric directories
need an explicit named alias.

Merge these tables into the configuration, setting `judge.seat = "product"`
in the existing `[judge]` table:

```toml
[judge.packs.reviewed]
route_confidence = 0.9
routes = ["claude", "codex", "grok", "opencode", "human", "skip"]
include_issue_text = false
sensitive_paths = ["private/"]

[judge.packs.reviewed.question_criteria.route.grok]
what = "Implementation using the configured Grok tools."
not_for = "Work requiring tools absent from this seat."
examples = ["Apply the bounded change described by the issue."]

[judge.seats.product]
enabled = false
pack = "reviewed"
route_confidence = 0.95

[judge.seats.research]
enabled = false
```

Resolution applies defaults, layered judge settings, the selected pack, the
selected seat, and finally existing environment and CLI overrides. A pack
cannot enable a seat. Each seat can set `enabled`, `pack`, and the same safe
overrides as a pack. `--no-judge` still wins over seat enablement.

Packs can set the inert confidence and human/risk thresholds, routes,
`include_issue_text`, `sensitive_paths`, and `question_criteria`. They cannot
change endpoints, models, hooks, execution commands or hard tool guards.
All definitions are validated at load time, including unused packs and seats.
Unknown fields, missing references, invalid aliases, duplicate or empty routes,
and nonfinite thresholds stop startup.

`question_criteria.route.<route>` takes `what`, `not_for`, and an `examples`
array. `question_criteria.needs_human` takes `true` and `false` arrays.
`question_criteria.action_risk` takes three tables with `level` and `what`,
ordered `routine`, `elevated`, `dangerous`. These are literal reviewed text;
Ortus never evaluates them as code or opens referenced files. Omitted criteria
retain their defaults. Keep credentials out of criteria text.

The resolved criteria enter the provider questions. Request metadata and decision events record
`pre-turn-v2` and a canonical SHA-256 hash of the offered questions and resolved
threshold/text policy without recording the question text. Explicit backend,
model and effort flags still pin the baseline. With `pre_tool = true`, only
Claude can be offered and a non-Claude baseline fails startup.

## Two-seat shadow rollout

Use separate repository checkouts for separate seats. For example, directories
`01` and `02` can have explicit aliases `atlas` and `birch`. Their names have no
effect on scheduling. Each invocation loads its target repository's `.ortusrc`
over the shared user configuration and writes its own `logs/jev-decisions.jsonl`.
Aliases alone do not create separate log files inside one repository.

The following complete judge example can be merged into each repository's
configuration. Set `seat = "birch"` in the second repository. Retain existing
backend settings and replace any existing judge tables instead of duplicating
them. The example enables only atlas's pre-turn shadow observation.

<!-- BEGIN two-seat rollout example -->
```toml
[judge]
enabled = false
seat = "atlas"
model = "jev-1.13.0"
mode = "shadow"
failure_mode = "open"
include_issue_text = false
pre_tool = false
post_turn = false

[judge.packs.atlas_review]
route_confidence = 0.9
routes = ["claude", "codex", "skip", "human"]
sensitive_paths = ["atlas-private/"]

[judge.packs.atlas_review.question_criteria.needs_human]
true = ["The atlas change requires an unapproved product decision."]
false = ["The atlas issue fully specifies an authorized repository change."]

[judge.packs.birch_review]
route_confidence = 0.95
routes = ["claude", "skip", "human"]
sensitive_paths = ["birch-private/"]

[judge.packs.birch_review.question_criteria.needs_human]
true = ["The birch change requires an unavailable credential."]
false = ["The birch issue can be completed with existing credentials."]

[judge.seats.atlas]
enabled = true
pack = "atlas_review"

[judge.seats.birch]
enabled = false
pack = "birch_review"
```
<!-- END two-seat rollout example -->

Inspect both seats before running atlas:

```bash
ortus grind ./01 --judge-seat atlas --dry-run
ortus grind ./02 --judge-seat birch --dry-run
ortus grind ./01 --judge-seat atlas --tasks 1 --iterations 1
```

Expect atlas to report enabled with `mode=shadow`, and birch disabled. Shared
user TOML cannot override the project seat's explicit false. Environment and
CLI enable overrides still can, so keep `ORTUS_JUDGE_ENABLED` unset and do not
pass `--judge` when checking the disabled seat. Supply the key only to the
intended process through the host's secret manager. Ortus does not separate
credentials by alias or remove secrets inherited by workers; use separate
process environments when seats require different credentials.

Shadow mode still runs the baseline worker and its normal checks. It records
the intended decision without rerouting, skipping or escalating because of that
decision. Review atlas's decision/outcome pairs, failure counts and operator
corrections. Exclude outcomes with `accuracy_eligible = false` from accuracy
claims, including observations that do not match the worker's actual issue.
Use the latency and billing measurements described above before expanding the
rollout. This recipe requires no replay or calibration run; the runbook above
is there when you want candidate bands measured rather than guessed.

After review, change `judge.mode` to `"enforce"` in atlas's repository only and
run the same bounded command. Leave birch false until it has its own reviewed
shadow sample. The pre-turn gate has no escalation to tune: compare soft
candidate bands against the recorded answers offline instead, and change routes
or criteria one pack at a time. Keep the model pin and criteria hash with each
measurement so different policies are not pooled.

For an immediate pre-turn rollback on the next invocation:

```bash
ortus grind ./01 --judge-seat atlas --no-judge --tasks 1 --iterations 1
```

For persistent rollback, set `judge.seats.atlas.enabled = false`, clear any
environment enable override, and turn off the independent phases below.
Existing human labels require the explicit review and label-removal steps
above. Neither rollback discards work or interrupts a running worker.

## Independent phases and troubleshooting

`judge.pre_tool = true` installs a private, temporary Claude PreToolUse hook
for the run. Only Claude supports it; Codex, Grok, OpenCode and Docker launches
fail startup with this option. Disabled Claude hooks or unreadable hook context
also fail before launch. Existing goal hooks remain active. A human signal
stops the worker and preserves its claim and dirty work for operator review.

Unlike the pre-turn gate, this phase does withhold on confidence and risk. An
answer less than 0.8 confident either way about needing a human, or whose risk
confidence is under 0.8, returns `low_confidence`; a human-need probability of
0.8 or more, or a risk score of 1.5 or more, returns `needs_human`. Both hand
the proposed call to a human. Those four numbers are fixed in `decide_tool` and
are not the `human_threshold`, `risk_threshold` or `*_confidence` settings,
which stay inert. The difference is deliberate: a misrouted claim costs one
worker turn and is recorded for calibration, while a tool call can be
irreversible, so an unsure answer stops it rather than spending the action to
find out. A malformed answer, an unsupported tool and a local policy denial
still stop the call whatever those answers say. Offline calibration cannot move
these four numbers: they are not settings, this phase is not the claim gate,
and a candidate band is a report either way.
The temporary settings contain no credential and are removed after the worker
is reaped. This supplements the existing worker trust boundary; repository
write access is not a tamper-proof boundary.

`judge.post_turn = true` independently classifies observed worker outcomes.
Closed bd state remains authoritative. A model's `done` cannot close an open
issue; `plan_gap`, `auth` and `needs_human` can request human handling in enforce
mode. Shadow mode records intended handling without changing tracker state.
Neither phase replaces worker verification or the goal/Stop judge.

Both toggles belong to the repository's base `[judge]` table, not a pack or seat
override. They are independent of `enabled` and `--no-judge`. To keep a seat
fully disabled, leave both false in its repository. Do not enable them in shared
user configuration during a per-repository rollout. Optional semantic readiness
and a learned calibration head remain deferred; readiness schema validation
continues to be deterministic.

If a seat resolves unexpectedly, inspect the alias selected by the CLI,
`ORTUS_JUDGE_SEAT`, project TOML and user TOML in that order. A missing alias or
pack is a startup configuration error even when another seat is disabled.
`key_missing` affects an active evaluation according to its failure mode;
an inactive pre-turn gate makes no provider request or decision-log entry.
Repair credential injection in that process rather than copying keys into
configuration. Inspect each repository's private log independently and keep
the logs out of version control.
