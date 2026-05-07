#!/usr/bin/env bash
# test-bd-retry.sh - Behavioral test for the bd_retry helper.
#
# Acceptance criteria from the issue (bd_retry helper):
#   - A fake bd that exits non-zero with 'locked by another dolt process' is
#     retried up to BD_RETRY_MAX times.
#   - One that fails with any other message returns immediately.
#
# This test extracts the bd_retry function from ortus/ralph.sh (the canonical
# definition per the issue), sources it, and exercises both branches against
# fake `bd` shims placed first on PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RALPH_SH="$REPO_ROOT/ortus/ralph.sh"

if [ ! -f "$RALPH_SH" ]; then
  echo "FAIL: $RALPH_SH not found" >&2
  exit 1
fi

# Extract the bd_retry function from ralph.sh into a sourceable file.
# The function spans from `bd_retry() {` to the next line that is exactly `}`.
WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

EXTRACTED="$WORK_DIR/bd_retry.sh"
awk '
  /^bd_retry\(\) \{/ { capture=1 }
  capture { print }
  capture && /^\}$/ { capture=0 }
' "$RALPH_SH" > "$EXTRACTED"

if ! grep -q '^bd_retry() {' "$EXTRACTED" || ! grep -q '^}$' "$EXTRACTED"; then
  echo "FAIL: could not extract bd_retry function from $RALPH_SH" >&2
  cat "$EXTRACTED" >&2
  exit 1
fi

# Counter file used by fake bd shims to record invocation count.
COUNTER="$WORK_DIR/count"
echo 0 > "$COUNTER"

# Fake bd that always emits the lock-contention error and exits non-zero.
# Exists at $WORK_DIR/bd; we put $WORK_DIR first on PATH so this shim wins.
cat > "$WORK_DIR/bd-lock-fail" <<'SHIM'
#!/usr/bin/env bash
counter_file="${BD_TEST_COUNTER:?BD_TEST_COUNTER must be set}"
n=$(cat "$counter_file")
echo $((n + 1)) > "$counter_file"
echo 'database "dolt" is locked by another dolt process; either clone the database to run a second server, or stop the dolt process which currently holds an exclusive write lock on the database' >&2
exit 1
SHIM
chmod +x "$WORK_DIR/bd-lock-fail"

# Fake bd that fails with an unrelated error (must NOT be retried).
cat > "$WORK_DIR/bd-other-fail" <<'SHIM'
#!/usr/bin/env bash
counter_file="${BD_TEST_COUNTER:?BD_TEST_COUNTER must be set}"
n=$(cat "$counter_file")
echo $((n + 1)) > "$counter_file"
echo 'fatal: connection refused (a real, unrelated error)' >&2
exit 1
SHIM
chmod +x "$WORK_DIR/bd-other-fail"

# Fake bd that succeeds on the Nth call. N is read from $WORK_DIR/threshold.
cat > "$WORK_DIR/bd-eventually-ok" <<'SHIM'
#!/usr/bin/env bash
counter_file="${BD_TEST_COUNTER:?BD_TEST_COUNTER must be set}"
threshold_file="${BD_TEST_THRESHOLD:?BD_TEST_THRESHOLD must be set}"
n=$(cat "$counter_file")
n=$((n + 1))
echo "$n" > "$counter_file"
threshold=$(cat "$threshold_file")
if [ "$n" -ge "$threshold" ]; then
  echo "ok-on-attempt-$n"
  exit 0
fi
echo 'database is locked' >&2
exit 1
SHIM
chmod +x "$WORK_DIR/bd-eventually-ok"

# helper: run a sub-bash that sources bd_retry, points PATH at a fake bd,
# resets the counter, and invokes bd_retry.
run_subtest() {
  local fake_name="$1"
  local extra_env="$2"
  echo 0 > "$COUNTER"
  BD_TEST_COUNTER="$COUNTER" \
  BD_RETRY_MAX="${BD_RETRY_MAX:-2}" \
  ${extra_env} \
  bash -c '
    set -e
    # Place fake bd directory first on PATH; alias the named shim to "bd".
    fake_dir="$1"; fake_name="$2"; extracted="$3"
    workdir=$(mktemp -d)
    trap "rm -rf \"$workdir\"" EXIT
    ln -s "$fake_dir/$fake_name" "$workdir/bd"
    export PATH="$workdir:$PATH"
    # shellcheck disable=SC1090
    source "$extracted"
    # Make non-zero from bd_retry visible without aborting the subshell.
    set +e
    bd_retry whatever-args
    echo "exit=$?"
  ' _ "$WORK_DIR" "$fake_name" "$EXTRACTED"
}

# Disable the per-iteration sleep so tests are fast even with retries.
# bd_retry calls `sleep "$delay"`; override `sleep` via a function in the
# subshell using SLEEP_OVERRIDE.
override_sleep() {
  echo 'sleep() { :; }'
}

# Variant that overrides sleep in the subshell BEFORE sourcing bd_retry,
# so the function's `sleep "$delay"` invocations are no-ops.
run_subtest_fast() {
  local fake_name="$1"
  echo 0 > "$COUNTER"
  BD_TEST_COUNTER="$COUNTER" \
  BD_TEST_THRESHOLD="${BD_TEST_THRESHOLD:-3}" \
  BD_RETRY_MAX="${BD_RETRY_MAX:-2}" \
  bash -c '
    set -e
    fake_dir="$1"; fake_name="$2"; extracted="$3"
    workdir=$(mktemp -d)
    trap "rm -rf \"$workdir\"" EXIT
    ln -s "$fake_dir/$fake_name" "$workdir/bd"
    export PATH="$workdir:$PATH"
    sleep() { :; }
    export -f sleep 2>/dev/null || true
    # shellcheck disable=SC1090
    source "$extracted"
    set +e
    bd_retry whatever-args
    echo "exit=$?"
  ' _ "$WORK_DIR" "$fake_name" "$EXTRACTED"
}

PASSED=0
FAILED=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "PASS: $label (got $actual)"
    PASSED=$((PASSED + 1))
  else
    echo "FAIL: $label (expected $expected, got $actual)" >&2
    FAILED=$((FAILED + 1))
  fi
}

# --- Test 1: lock-error retries up to BD_RETRY_MAX, then fails ---------------
echo "--- Test 1: lock error retried up to BD_RETRY_MAX ---"
T1_OUT="$WORK_DIR/t1.out"
BD_RETRY_MAX=2 run_subtest_fast "bd-lock-fail" >"$T1_OUT" 2>&1 || true
calls=$(cat "$COUNTER")
exit_line=$(grep '^exit=' "$T1_OUT" || echo "exit=missing")
# With BD_RETRY_MAX=2: first attempt + 2 retries = 3 calls total, then fail.
assert_eq "lock error: bd invoked 3 times (1 + 2 retries)" "3" "$calls"
assert_eq "lock error: final exit non-zero" "exit=1" "$exit_line"

# --- Test 2: non-lock error fails immediately, no retry ----------------------
echo "--- Test 2: non-lock error fails immediately ---"
T2_OUT="$WORK_DIR/t2.out"
BD_RETRY_MAX=5 run_subtest_fast "bd-other-fail" >"$T2_OUT" 2>&1 || true
calls=$(cat "$COUNTER")
exit_line=$(grep '^exit=' "$T2_OUT" || echo "exit=missing")
assert_eq "non-lock error: bd invoked exactly 1 time" "1" "$calls"
assert_eq "non-lock error: final exit non-zero" "exit=1" "$exit_line"

# --- Test 3: succeeds on retry within budget ---------------------------------
echo "--- Test 3: lock error then success on attempt 3 ---"
echo 3 > "$WORK_DIR/threshold"
T3_OUT="$WORK_DIR/t3.out"
BD_TEST_THRESHOLD="$WORK_DIR/threshold" BD_RETRY_MAX=5 run_subtest_fast "bd-eventually-ok" >"$T3_OUT" 2>&1 || true
calls=$(cat "$COUNTER")
exit_line=$(grep '^exit=' "$T3_OUT" || echo "exit=missing")
assert_eq "eventual success: bd invoked exactly 3 times" "3" "$calls"
assert_eq "eventual success: final exit zero" "exit=0" "$exit_line"
if ! grep -q "ok-on-attempt-3" "$T3_OUT"; then
  echo "FAIL: eventual success: stdout payload not propagated" >&2
  FAILED=$((FAILED + 1))
else
  echo "PASS: eventual success: stdout payload propagated"
  PASSED=$((PASSED + 1))
fi

echo ""
echo "Results: $PASSED passed, $FAILED failed"
[ "$FAILED" -eq 0 ]
