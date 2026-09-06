#!/bin/bash

set -euo pipefail

source "$(dirname -- "${BASH_SOURCE[0]}")/base-test.sh"

test_tmp=$(mktemp -d)
trap 'rm -rf "$test_tmp"' EXIT
mkdir -p "$test_tmp/bin"

export PACKAGE_TEST_DB="$test_tmp/packages"
export PACKAGE_TEST_LOG="$test_tmp/calls"
export PATH="$test_tmp/bin:$ROOT/bin:$PATH"

cat >"$test_tmp/bin/pacman" <<'STUB'
#!/bin/bash
printf '%s\n' "$*" >>"$PACKAGE_TEST_LOG"
operation=$1
shift
case "$operation" in
  -Q)
    [[ ${PACKAGE_TEST_QUERY_ERROR:-0} == "0" ]] || exit 2
    status=0
    for pkg in "$@"; do
      if grep -qxF "$pkg" "$PACKAGE_TEST_DB"; then
        printf '%s 1.0-1\n' "$pkg"
      else
        printf 'error: package %s was not found\n' "$pkg" >&2
        status=1
      fi
    done
    exit "$status"
    ;;
  -S)
    [[ ${PACKAGE_TEST_INSTALL_ERROR:-0} == "0" ]] || exit 1
    [[ $1 == "--noconfirm" && $2 == "--needed" ]] || exit 2
    shift 2
    for pkg in "$@"; do
      [[ $pkg == "${PACKAGE_TEST_OMIT:-}" ]] || printf '%s\n' "$pkg" >>"$PACKAGE_TEST_DB"
    done
    ;;
  *) exit 2 ;;
esac
STUB

# Exercise the non-root dispatch too when the suite runs as an ordinary user.
cat >"$test_tmp/bin/sudo" <<'STUB'
#!/bin/bash
exec "$@"
STUB
chmod +x "$test_tmp/bin/"*

reset_db() {
  printf '%s\n' alpha beta gamma >"$PACKAGE_TEST_DB"
  : >"$PACKAGE_TEST_LOG"
}

expect_status() {
  local expected=$1 actual=0
  shift
  "$@" >"$test_tmp/stdout" 2>"$test_tmp/stderr" || actual=$?
  (( actual == expected )) || fail "$* exits $expected (got $actual)"
  [[ ! -s $test_tmp/stdout ]] || fail "$* does not print package query output"
}

for helper in omarchy-pkg-present omarchy-pkg-missing; do
  if [[ $helper == "omarchy-pkg-present" ]]; then
    found=0
    missing=1
  else
    found=1
    missing=0
  fi

  reset_db
  expect_status "$found" "$helper"
  [[ ! -s $PACKAGE_TEST_LOG ]] || fail "$helper does not query the database for no targets"

  expect_status "$found" "$helper" alpha beta gamma alpha
  [[ $(wc -l <"$PACKAGE_TEST_LOG") == "1" ]] || fail "$helper reads the database once for installed targets"
  [[ ! -s $test_tmp/stderr ]] || fail "$helper keeps successful queries silent"

  # A missing target in any position must change the predicate, regardless of
  # whether other targets are present or the same name appears twice.
  for targets in 'absent alpha beta' 'alpha absent beta' 'alpha beta absent' 'absent absent'; do
    read -ra packages <<<"$targets"
    expect_status "$missing" "$helper" "${packages[@]}"
    [[ ! -s $test_tmp/stderr ]] || fail "$helper keeps missing queries silent"
  done

  PACKAGE_TEST_QUERY_ERROR=1 expect_status "$missing" "$helper" alpha beta
done
pass "package predicates preserve empty, all-present, any-missing, and query-error results"

reset_db
expect_status 0 omarchy-pkg-add
[[ ! -s $PACKAGE_TEST_LOG ]] || fail "adding no packages performs no database work"

expect_status 0 omarchy-pkg-add alpha beta gamma
[[ $(wc -l <"$PACKAGE_TEST_LOG") == "2" ]] || fail "already-installed packages use one preflight and one postflight"
! grep -q '^-S' "$PACKAGE_TEST_LOG" || fail "already-installed packages are not reinstalled"
pass "package add skips installed packages and batches successful verification"

reset_db
expect_status 0 omarchy-pkg-add alpha delta epsilon
grep -qxF delta "$PACKAGE_TEST_DB" || fail "package add installs the first missing package"
grep -qxF epsilon "$PACKAGE_TEST_DB" || fail "package add installs the second missing package"
[[ $(wc -l <"$PACKAGE_TEST_LOG") == "3" ]] || fail "package installation uses a query, transaction, and verification"
grep -qxF -- '-S --noconfirm --needed alpha delta epsilon' "$PACKAGE_TEST_LOG" || fail "package add passes every target to the transaction"
pass "package add installs missing targets together and verifies them"

reset_db
PACKAGE_TEST_OMIT=delta expect_status 1 omarchy-pkg-add alpha delta epsilon
grep -q "Package 'delta' did not install" "$test_tmp/stderr" || fail "a falsely successful transaction identifies the first missing target"
pass "package add catches falsely successful transactions"

reset_db
PACKAGE_TEST_INSTALL_ERROR=1 expect_status 1 omarchy-pkg-add delta epsilon
[[ $(wc -l <"$PACKAGE_TEST_LOG") == "2" ]] || fail "a failed transaction stops before verification"
pass "package add propagates failed transactions"
