#!/bin/bash
# Install the GPU Prometheus exporter on one GPU node.
#
# Usage:
#   sudo bash install.sh              # listen on :9745
#   sudo bash install.sh --port 9800  # listen elsewhere
#
# Idempotent: re-running it upgrades the code/deps and restarts the service.
set -euo pipefail

PORT=9745
while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            PORT="${2:?--port requires a value}"
            shift 2
            ;;
        -h|--help)
            echo "usage: $0 [--port N]"
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (use sudo)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Overridable so the installer can be exercised in a container / CI without
# touching the real paths. Production uses the defaults.
INSTALL_DIR="${GPU_EXPORTER_DIR:-/opt/gpu-exporter}"
SERVICE_FILE="${GPU_EXPORTER_SERVICE_FILE:-/etc/systemd/system/gpu-exporter.service}"
NO_SYSTEMD="${GPU_EXPORTER_NO_SYSTEMD:-0}"

# ---- Preflight: fail with an actionable message instead of a stack trace ----
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found" >&2
    exit 1
fi
if ! python3 -c 'import venv' >/dev/null 2>&1; then
    echo "error: the python3 venv module is missing. Install it first, e.g.:" >&2
    echo "         apt install -y python3-venv      # Debian/Ubuntu" >&2
    echo "         dnf install -y python3-libs      # RHEL/Fedora" >&2
    exit 1
fi
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "error: nvidia-smi not found; install the NVIDIA driver before the exporter" >&2
    exit 1
fi
if [ ! -f "${SCRIPT_DIR}/exporter.py" ] || [ ! -f "${SCRIPT_DIR}/requirements.txt" ]; then
    echo "error: exporter.py / requirements.txt missing next to $0" >&2
    exit 1
fi

echo "==> Installing GPU exporter to ${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}"

if [ ! -x "${INSTALL_DIR}/venv/bin/pip" ]; then
    echo "==> Creating Python venv"
    python3 -m venv "${INSTALL_DIR}/venv"
fi

echo "==> Installing dependencies"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
if ! "${INSTALL_DIR}/venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"; then
    echo "error: dependency installation failed (no network / pip index reachable?)" >&2
    exit 1
fi
# Record what actually got installed, for auditing and reproducible reinstalls.
"${INSTALL_DIR}/venv/bin/pip" freeze > "${INSTALL_DIR}/requirements.lock"

echo "==> Copying exporter"
install -m 0644 "${SCRIPT_DIR}/exporter.py" "${INSTALL_DIR}/exporter.py"
install -m 0644 "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"

echo "==> Installing systemd service (port ${PORT})"
# Keep the unit path writable even on minimal images where /etc/systemd/system
# has not been created yet.
install -d -m 0755 "$(dirname "${SERVICE_FILE}")"
install -m 0644 "${SCRIPT_DIR}/gpu-exporter.service" "${SERVICE_FILE}"
# Rewrite the port *before* starting the service, otherwise it would come up on
# the old port and the health check below would fail.
if [ "${PORT}" != "9745" ]; then
    sed -i "s|--port 9745|--port ${PORT}|" "${SERVICE_FILE}"
fi
if [ "${NO_SYSTEMD}" = "1" ]; then
    echo "    GPU_EXPORTER_NO_SYSTEMD=1: unit written, systemd left untouched"
else
    systemctl daemon-reload
    systemctl enable gpu-exporter >/dev/null
    systemctl restart gpu-exporter
fi
if [ "${PORT}" != "9745" ]; then
    sed -i "s|--port 9745|--port ${PORT}|" "${SERVICE_FILE}"
fi

# ---- Health check: do not report success if the exporter is not serving ----
echo "==> Waiting for the exporter to serve metrics on :${PORT}"
if ! "${INSTALL_DIR}/venv/bin/python" - "${PORT}" <<'PY'
import json
import sys
import time
import urllib.request

port = int(sys.argv[1])
url = "http://127.0.0.1:%d/metrics" % port
deadline = time.time() + 30
last = "no response"
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = resp.read().decode("utf-8", "replace")
        if "gpu_info{" in body:
            gpus = sum(1 for l in body.splitlines() if l.startswith("gpu_info{"))
            up = sum(1 for l in body.splitlines() if l.startswith("gpu_up{") and l.endswith(" 1.0"))
            print("    metrics endpoint OK, %d GPU(s) reported (gpu_up==1: %d)" % (gpus, up))
            sys.exit(0)
        last = "endpoint is up but has no gpu_info yet"
    except Exception as exc:  # noqa: BLE001 - retry until the deadline
        last = str(exc)
    time.sleep(1)
print("    " + last)
sys.exit(1)
PY
then
    echo "error: exporter did not become healthy. Last log lines:" >&2
    journalctl -u gpu-exporter -n 20 --no-pager >&2 || true
    exit 1
fi

echo "==> Done: $(systemctl is-active gpu-exporter), listening on :${PORT}"
