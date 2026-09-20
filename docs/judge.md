# Pre-turn judge pilot

The optional Jev gate chooses an available configured worker, `skip`, or `human` before
grind launches a worker. It is disabled by default. Ordinary installations
need neither the optional SDK nor a TypeSafe key. A key in the environment
does not enable the gate.

This pilot covers one product seat, named `ortus`. These instructions do not
enable it automatically. Tool hooks, post-turn classification, shadow mode,
multi-seat rollout and learned thresholds are outside this pilot.

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
include_issue_text = false
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
| `mode` | `enforce` | Apply the pre-turn decision |
| `routes` | `claude`, `codex`, `skip`, `human` | Allowed choices, filtered by available workers |
| `timeout_seconds` | `1.5` | One request deadline, SDK retries disabled |
| `failure_mode` | `open` | Service failures use the original backend |
| `low_confidence` | `human` | Pause uncertain answers; `skip` is also supported |
| `route_confidence`, `noul_confidence`, `risk_confidence` | `0.8` each | Minimum confidence for applying the typed answers |
| `human_threshold` | `0.8` | Human-need probability at or above this pauses work |
| `risk_threshold` | `1.5` | Score at or above this pauses work; rubric scores are 0, 1, 2 |
| `seat` | `default` | Set explicitly to `ortus` for this pilot |
| `include_issue_text`, `include_log_tail` | `false` | Issue prose is opt-in; the pre-turn packer never reads log tails |
| `title_cap`, `objective_cap`, `acceptance_cap`, `tool_cap` | `160`, `1024`, `1024`, `512` | Character caps; oversized source fields are omitted |
| `total_bytes_cap` | `8192` | Serialized UTF-8 state budget |
| `sensitive_paths` | empty | Additional literal paths to omit from text |

The adapter asks Choice, Noul and Score questions in one System One request.
Noul supplies a probability `p`, not a separate confidence field. Ortus computes
its confidence as `max(p, 1-p)`. With the default `0.8` minimum, probabilities
near 0.5 therefore take the low-confidence path.

Missing keys, missing SDKs, timeouts, service errors and malformed answers
follow `failure_mode`. In `open` mode the original worker backend proceeds
with normal verification and closure. In `closed` mode the issue requires
human handling. An unavailable backend is not offered; a response naming it
is an invalid answer and follows the same failure policy.

Low confidence is checked before human need and risk. Confident human routes
or high human-need probabilities pause work, as do confident high-risk scores.
A confident skip stops this invocation and leaves the issue available for a
later run. A proceed decision launches a worker bound to the selected id.

Claude, Codex, Grok and OpenCode baselines support the enabled gate.
Optional workers without binaries or valid profiles are excluded. OpenCode
also requires the existing `[local]` configuration and server preflight.
The `local` backend uses `opencode` in judge requests while retaining its
original backend and profile settings for execution. Explicit backend/model/effort
overrides pin routing to the baseline. Every available route is prepared before
claiming work; a preparation failure requires human handling. Claim ownership,
logging and configuration errors are not service outages and do not fail open.

## Privacy and authority

By default the request carries bounded metadata: issue id, type, labels,
priority, seat, phase and available backends. Title, objective and acceptance
text are empty. Metadata can still identify work; review it before enabling a
seat. Without prose the judge has less evidence for semantic routing.

After reviewing the seat's issue text, an operator may set
`include_issue_text = true`. The packer then includes a screened title, the
first Objective line and AC lines. A `judge-private` label always suppresses
issue prose. Sensitive patterns, credential values and configured sensitive
paths cause whole-field omission, as do size limits. Pattern screening cannot
prove arbitrary prose safe; opt-in requires review. No repository files,
attachments, raw transcripts or environment dumps enter the packed state.

The policy keeps arithmetic and control flow in code. The model classifies
meaning into typed route, probability, risk and confidence values. The adapter
validates the model pin, answer types, ranges and offered routes; policy code
applies thresholds. Provider explanations and exception text do not choose an
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

This overrides an enabled config or environment setting. For lasting rollback,
set `enabled = false` and remove `ORTUS_JUDGE_ENABLED` from the host environment.
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

Packs can set confidence thresholds, human/risk thresholds, routes,
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
