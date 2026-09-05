#!/bin/bash

set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

failing_script="$work_dir/fail.sh"
log_file="$work_dir/install.log"
cat >"$failing_script" <<'SCRIPT'
echo "about to fail"
false
SCRIPT

set +e
(
  set -euo pipefail
  export OMARCHY_INSTALL_LOG_FILE="$log_file"
  source "$ROOT/install/helpers/logging.sh"
  run_logged "$failing_script"
  echo "unreachable"
)
status=$?
set -e

(( status != 0 )) || fail "run_logged returns failing script status"
grep -q "Starting: $failing_script" "$log_file" || fail "run_logged logs script start"
grep -q "about to fail" "$log_file" || fail "run_logged captures script output"
grep -q "Failed: $failing_script (exit code: 1)" "$log_file" || fail "run_logged logs failed script before errexit exits"

stdout_log="$work_dir/stdout.log"
set +e
(
  set -euo pipefail
  export OMARCHY_INSTALL_LOG_FILE="$work_dir/iso-owned.log"
  export OMARCHY_LOG_TO_STDOUT=1
  source "$ROOT/install/helpers/logging.sh"
  run_logged "$failing_script"
) >"$stdout_log" 2>&1
stdout_status=$?
set -e

(( stdout_status != 0 )) || fail "stdout run_logged returns failing script status"
[[ ! -e $work_dir/iso-owned.log ]] || fail "stdout logging mode does not write directly to install log"
grep -q "Starting: $failing_script" "$stdout_log" || fail "stdout logging mode emits script start"
grep -q "about to fail" "$stdout_log" || fail "stdout logging mode emits script output"
grep -q "Failed: $failing_script (exit code: 1)" "$stdout_log" || fail "stdout logging mode emits failure marker"

pass "run_logged records failures under errexit"

success_script="$work_dir/success.sh"
cat >"$success_script" <<'SCRIPT'
echo "setup output"
SCRIPT

(
  set -euo pipefail
  export OMARCHY_LOG_TO_STDOUT=1 TZ=UTC
  unset OMARCHY_START_TIME OMARCHY_START_EPOCH
  source "$ROOT/install/helpers/logging.sh"
  start_install_log
  [[ $OMARCHY_START_EPOCH =~ ^[0-9]+$ ]] || exit 1
  run_logged "$success_script"
  [[ $- == *e* ]] || exit 1
  stop_install_log
) >"$work_dir/success.log"
grep -qE '^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\] Completed:' "$work_dir/success.log" ||
  fail "successful setup keeps timestamped completion records"
grep -qE '^Omarchy setup: [0-9]+m [0-9]+s$' "$work_dir/success.log" ||
  fail "setup keeps the elapsed-time summary"

(
  set +e
  set -uo pipefail
  export OMARCHY_LOG_TO_STDOUT=1
  export OMARCHY_START_TIME="existing setup" OMARCHY_START_EPOCH=123
  source "$ROOT/install/helpers/logging.sh"
  start_install_log
  [[ $OMARCHY_START_TIME == "existing setup" && $OMARCHY_START_EPOCH == "123" ]] || exit 1
  run_logged "$success_script"
  [[ $- != *e* ]] || exit 1
) >/dev/null

pass "logging preserves timestamps, inherited start time, and shell error mode"
