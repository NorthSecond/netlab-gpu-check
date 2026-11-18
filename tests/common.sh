# shellcheck shell=bash
# Shared helpers for the test suites. Source this, do not execute it.
#
# Conventions:
#   check "<name>" "<actual>" "<expected>"   compare two strings
#   ok / bad / skip                          report one assertion
#   section "<title>"                        print a section header
# Every suite must finish with `finish` and exit with its return code.

: "${PASS:=0}"
: "${FAIL:=0}"
: "${SKIP:=0}"

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    C_GREEN=$'\033[32m'; C_RED=$'\033[31m'; C_YELLOW=$'\033[33m'
    C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_GREEN=''; C_RED=''; C_YELLOW=''; C_BOLD=''; C_OFF=''
fi

section() { printf '\n%s== %s %s\n' "$C_BOLD" "$1" "$C_OFF"; }
ok()   { printf '  %sPASS%s  %s\n' "$C_GREEN" "$C_OFF" "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  %sFAIL%s  %s%s\n' "$C_RED" "$C_OFF" "$1" "${2:+  ($2)}"; FAIL=$((FAIL + 1)); }
skip() { printf '  %sSKIP%s  %s\n' "$C_YELLOW" "$C_OFF" "$1"; SKIP=$((SKIP + 1)); }

# check <name> <actual> <expected>
check() {
    if [ "$2" = "$3" ]; then
        ok "$1"
    else
        bad "$1" "want [$3] got [$2]"
    fi
}

# assert_contains <name> <haystack> <needle>
assert_contains() {
    case "$2" in
        *"$3"*) ok "$1" ;;
        *) bad "$1" "missing [$3]" ;;
    esac
}

# assert_file_absent <name> <path>
assert_absent() {
    if [ -e "$2" ]; then bad "$1" "$2 exists"; else ok "$1"; fi
}

finish() {
    printf '\n%s%s: %d passed, %d failed, %d skipped%s\n' \
        "$C_BOLD" "${SUITE_NAME:-suite}" "$PASS" "$FAIL" "$SKIP" "$C_OFF"
    [ "$FAIL" -eq 0 ]
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        printf '%sERROR%s: required command not found: %s\n' "$C_RED" "$C_OFF" "$1" >&2
        exit 2
    fi
}
