# Large PRD (50-task fixture)

Stress-size PRD used to verify decomposition scales without truncation.

## Epic 1: Ingest pipeline rewrite (10 tasks)

1. New ingest worker scaffold
2. Schema migration for raw events
3. Retry middleware
4. DLQ wiring
5. Backpressure metrics
6. Replay tool
7. Idempotency keys per source
8. Compaction job
9. Performance benchmark
10. Operational runbook

## Epic 2: Search index v2 (10 tasks)

1. New index template
2. Reindex job
3. Field-level boosts
4. Synonym dictionary
5. Stemming language profiles
6. Highlighting opt-in flag
7. Cluster failover test
8. Query-cost dashboard
9. Migration cutover plan
10. Post-cutover smoke test

## Epic 3: Billing v3 (10 tasks)

1. New tax engine adapter
2. Pro-rated upgrades
3. Refund webhook
4. Invoice PDF redesign
5. Dunning state machine
6. Receipt email template
7. Tax-jurisdiction lookup
8. Currency rounding fixes
9. Audit replay
10. Compliance signoff doc

## Epic 4: Observability rollup (10 tasks)

1. OTel SDK upgrade
2. Trace propagation across queues
3. Log structured-fields baseline
4. Slow-query log
5. RED dashboards per service
6. SLO definitions
7. Synthetic check harness
8. Alert routing
9. Oncall handbook
10. Postmortem template

## Epic 5: Frontend modernization (10 tasks)

1. Vite migration
2. TS strict mode
3. Storybook setup
4. Component-library extraction
5. A11y audit fixes
6. Bundle-size budget
7. CSS-vars theming
8. RTL support
9. Visual-regression CI
10. Old-bundle deprecation
