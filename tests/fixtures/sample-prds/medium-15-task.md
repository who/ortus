# Medium PRD (15-task fixture)

Mid-sized PRD that decomposes into ~15 bd issues across 2 epics.

## Epic A: Auth rewrite

1. Replace session middleware with JWT
2. Add refresh-token rotation
3. Migrate existing sessions on first request
4. Document the new auth flow in README
5. Add integration tests for token rotation
6. Add metrics for token-refresh latency
7. Wire SSO callback handler

## Epic B: Admin dashboard

1. New `/admin` route with role-gate
2. User-list endpoint with pagination
3. Role-edit form
4. Audit-log view (last 90 days)
5. Bulk-action toolbar
6. Unit tests for permission gates
7. E2E test: admin can promote a user
8. README admin-guide section
