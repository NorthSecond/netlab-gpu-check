#!/bin/bash
# Run every test suite and print a summary table.
#
#   ./tests/run-all.sh                 # everything
#   ./tests/run-all.sh relay exporter  # only these suites
#   ./tests/run-all.sh --list          # show suites
#   ./tests/run-all.sh --no-docker     # skip suites that need docker
#
# Requirements: python3 (+ prometheus_client for the exporter suite, a venv is
# created automatically), and docker for the proxy/install/promtool checks.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=/dev/null
. "$HERE/common.sh"

ALL_SUITES="static relay exporter proxy install"
WANT=""
USE_DOCKER=1

while [ $# -gt 0 ]; do
    case "$1" in
        --list) echo "$ALL_SUITES" | tr ' ' '\n'; exit 0 ;;
        --no-docker) USE_DOCKER=0; shift ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) WANT="$WANT $1"; shift ;;
    esac
done
[ -z "$WANT" ] && WANT="$ALL_SUITES"

# Configured once, used by the container-based suites.
NGINX_IMAGE="${NGINX_IMAGE:-nginx:1.27-alpine}"
ALPINE_IMAGE="${ALPINE_IMAGE:-alpine:latest}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.11-slim}"

declare -a ROWS
FAILED=0
SKIPPED=0

record() { ROWS+=("$(printf '%-10s %s' "$1" "$2")"); }

run_suite() {
    local name="$1" rc
    section "suite: $name"
    case "$name" in
        static)
            bash "$HERE/static/checks.sh"
            rc=$?
            ;;
        relay)
            python3 "$HERE/relay/relay_test.py"
            rc=$?
            ;;
        exporter)
            bash "$HERE/exporter/verify.sh"
            rc=$?
            ;;
        proxy)
            if [ "$USE_DOCKER" -ne 1 ] || ! command -v docker >/dev/null 2>&1; then
                skip "docker 不可用"; record "$name" "SKIP (no docker)"; SKIPPED=$((SKIPPED + 1)); return 0
            fi
            docker run --rm \
                --add-host grafana:127.0.0.1 \
                -e TESTS_DIR=/tests \
                -v "$REPO/server/proxy/grafana.conf:/etc/nginx/conf.d/default.conf:ro" \
                -v "$HERE:/tests:ro" \
                "$NGINX_IMAGE" sh /tests/proxy/proxy_test.sh
            rc=$?
            ;;
        install)
            if [ "$USE_DOCKER" -ne 1 ] || ! command -v docker >/dev/null 2>&1; then
                skip "docker 不可用"; record "$name" "SKIP (no docker)"; SKIPPED=$((SKIPPED + 1)); return 0
            fi
            docker run --rm \
                -e TESTS_DIR=/tests \
                -e CLIENT_DIR=/client \
                -v "$REPO/client:/client:ro" \
                -v "$HERE:/tests:ro" \
                "$ALPINE_IMAGE" sh -c \
                'apk add -q bash python3 py3-pip py3-virtualenv curl procps >/dev/null 2>&1; sh /tests/install/install_test.sh'
            rc=$?
            ;;
        *)
            printf '%sERROR%s unknown suite: %s\n' "$C_RED" "$C_OFF" "$name" >&2
            return 2
            ;;
    esac

    if [ "$rc" -eq 0 ]; then
        record "$name" "PASS"
    else
        record "$name" "FAIL (exit $rc)"
        FAILED=$((FAILED + 1))
    fi
    return 0
}

printf '%snetlab-gpu-check test suite%s\n' "$C_BOLD" "$C_OFF"
printf 'repo: %s\n' "$REPO"
command -v docker >/dev/null 2>&1 || printf 'note: docker not found - container suites will be skipped\n'

for suite in $WANT; do
    run_suite "$suite"
done

printf '\n%s===== summary =====%s\n' "$C_BOLD" "$C_OFF"
for row in "${ROWS[@]}"; do
    printf '%s\n' "$row"
done

if [ "$FAILED" -ne 0 ]; then
    printf '\n%s%d suite(s) failed%s\n' "$C_RED" "$FAILED" "$C_OFF"
    exit 1
fi
printf '\n%sall suites passed%s' "$C_GREEN" "$C_OFF"
[ "$SKIPPED" -ne 0 ] && printf ' (%d skipped)' "$SKIPPED"
printf '\n'
