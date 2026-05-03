#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/opt/gpu-exporter"

echo "==> Installing GPU exporter to ${INSTALL_DIR}"

mkdir -p "${INSTALL_DIR}"

if [ ! -f "${INSTALL_DIR}/venv/bin/pip" ]; then
    echo "==> Creating Python venv"
    rm -rf "${INSTALL_DIR}/venv"
    python3 -m venv "${INSTALL_DIR}/venv"
fi

echo "==> Installing dependencies"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install pynvml prometheus_client

echo "==> Copying exporter"
cp "${SCRIPT_DIR}/exporter.py" "${INSTALL_DIR}/exporter.py"

echo "==> Installing systemd service"
cp "${SCRIPT_DIR}/gpu-exporter.service" /etc/systemd/system/gpu-exporter.service
systemctl daemon-reload
systemctl enable gpu-exporter
systemctl restart gpu-exporter

echo "==> Done. Status:"
systemctl status gpu-exporter --no-pager
