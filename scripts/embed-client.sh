#!/bin/bash
# Regenerate remote-install.sh from the real files in client/.
#
# remote-install.sh is a single self-contained file that can be pasted onto a
# node without copying the directory, so it embeds client/ as base64. Embedding
# by hand is what previously caused it to drift (it still installed the
# deprecated `pynvml` package and lacked the usedGpuMemory=None fix), so it is
# generated here and verified by scripts/check-embedded.sh.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT_DIR="${REPO_DIR}/client"
OUT="${REPO_DIR}/remote-install.sh"

embed() { base64 -w 0 < "$1"; }

cat > "${OUT}" <<HEADER
#!/bin/bash
# GENERATED FILE - do not edit by hand.
# Regenerate with: scripts/embed-client.sh
# Verify it is in sync with: scripts/check-embedded.sh
#
# Single-file installer for GPU nodes: decodes client/ into a temp dir and runs
# install.sh there. Usage: sudo bash remote-install.sh [--port N]
set -euo pipefail

TMPDIR="\$(mktemp -d /tmp/gpu-exporter-install.XXXXXX)"
cleanup() { rm -rf "\${TMPDIR}"; }
trap cleanup EXIT INT TERM
cd "\${TMPDIR}"

HEADER

for f in exporter.py install.sh gpu-exporter.service requirements.txt; do
    printf 'echo "%s" | base64 -d > %s\n' "$(embed "${CLIENT_DIR}/${f}")" "$f" >> "${OUT}"
done

cat >> "${OUT}" <<'FOOTER'

echo "==> Files extracted to ${TMPDIR}"
sudo bash install.sh "$@"
echo "==> Remote install complete."
FOOTER

chmod +x "${OUT}"
echo "wrote ${OUT} ($(wc -c < "${OUT}") bytes)"
