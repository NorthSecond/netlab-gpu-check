#!/bin/bash
# Verify that remote-install.sh embeds exactly the current files in client/.
#
# This is the guard against the bug where remote-install.sh silently shipped an
# older exporter (missing the usedGpuMemory=None fix) and the deprecated
# `pynvml` dependency. Run it in CI or before tagging a release.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT_DIR="${REPO_DIR}/client"
REMOTE="${REPO_DIR}/remote-install.sh"

[ -f "${REMOTE}" ] || { echo "missing ${REMOTE}" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

rc=0
for f in exporter.py install.sh gpu-exporter.service requirements.txt; do
    if ! grep -qF "$(base64 -w 0 < "${CLIENT_DIR}/${f}")" "${REMOTE}"; then
        echo "STALE: remote-install.sh does not embed the current client/${f}" >&2
        rc=1
    else
        echo "ok: client/${f} matches the embedded copy"
    fi
done

if [ "${rc}" -ne 0 ]; then
    echo "" >&2
    echo "Run scripts/embed-client.sh to regenerate remote-install.sh." >&2
fi
exit "${rc}"
