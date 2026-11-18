#!/bin/sh
# Integration test for client/install.sh.
#
# Runs *inside* an alpine container that imitates a GPU node: a stub
# nvidia-smi, a stub systemctl that really launches the exporter, and the mock
# NVML module (which shadows the installed nvidia-ml-py because it sits next to
# exporter.py). Verifies a real install, idempotency, the preflight failures and
# argument handling.
set -u
SUITE_NAME="install"
TESTS_DIR="${TESTS_DIR:-/tests}"
CLIENT_DIR="${CLIENT_DIR:-/client}"
# shellcheck source=/dev/null
. "$TESTS_DIR/common.sh"

[ -f "$CLIENT_DIR/install.sh" ] || { echo "ERROR: $CLIENT_DIR/install.sh missing" >&2; exit 2; }
require_cmd python3

PORT=${INSTALL_TEST_PORT:-19800}

# ---------------------------------------------------------------- fake node
mkdir -p /usr/local/bin /opt/gpu-exporter /etc/systemd/system
cat > /usr/local/bin/systemctl <<'STUB'
#!/bin/bash
UNIT=/etc/systemd/system/gpu-exporter.service
case "$1" in
  restart)
    pkill -f 'exporter.py' 2>/dev/null
    sleep 0.2
    PORT=$(sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p' "$UNIT")
    nohup /opt/gpu-exporter/venv/bin/python /opt/gpu-exporter/exporter.py --port "$PORT" \
      >> /tmp/exporter.log 2>&1 &
    sleep 0.5
    ;;
  is-active)
    if pgrep -f 'exporter.py' >/dev/null; then echo active; exit 0; else echo inactive; exit 3; fi
    ;;
  enable|daemon-reload) : ;;
esac
STUB
cat > /usr/local/bin/journalctl <<'STUB'
#!/bin/bash
echo "(journal stub)"; cat /tmp/exporter.log 2>/dev/null
STUB
cat > /usr/local/bin/nvidia-smi <<'STUB'
#!/bin/bash
echo "GPU 0: Mock P4 (UUID: GPU-mock)"
STUB
chmod +x /usr/local/bin/systemctl /usr/local/bin/journalctl /usr/local/bin/nvidia-smi

cp "$TESTS_DIR/exporter/mock/pynvml.py" /opt/gpu-exporter/pynvml.py
cat > /tmp/scenario.json <<'JSON'
{"gpus":[{"name":"Mock P4","uuid":"GPU-mock","mem_total":8589934592,"mem_used":1048576,
          "procs":[{"pid":1,"mem":1048576}]}]}
JSON
export MOCK_NVML_SCENARIO=/tmp/scenario.json

section "正常安装"
if bash "$CLIENT_DIR/install.sh" --port "$PORT" > /tmp/install.log 2>&1; then
    ok "install.sh 退出码 0"
else
    bad "install.sh 失败"; tail -20 /tmp/install.log | sed 's/^/       /'
fi
assert_contains "健康检查确认有 gpu_info 上报" "$(cat /tmp/install.log)" "metrics endpoint OK, 1 GPU"
assert_contains "报告服务 active" "$(cat /tmp/install.log)" "Done: active"
[ -x /opt/gpu-exporter/venv/bin/python ] && ok "venv 已创建" || bad "venv 缺失"
[ -s /opt/gpu-exporter/requirements.lock ] && ok "requirements.lock 已生成" || bad "requirements.lock 缺失"
assert_contains "service 使用指定端口" "$(cat /etc/systemd/system/gpu-exporter.service)" "--port $PORT"
check "指标端点可访问" "$(curl -s --max-time 3 "http://127.0.0.1:$PORT/metrics" | grep -c '^gpu_up{')" "1"

section "幂等重跑（不重建 venv，端口回落默认）"
INODE_BEFORE=$(stat -c %i /opt/gpu-exporter/venv/bin/python)
if bash "$CLIENT_DIR/install.sh" > /tmp/install2.log 2>&1; then
    ok "重跑退出码 0"
else
    bad "重跑失败"; tail -20 /tmp/install2.log | sed 's/^/       /'
fi
check "venv 未被重建" "$(stat -c %i /opt/gpu-exporter/venv/bin/python)" "$INODE_BEFORE"
assert_contains "端口回到默认 9745" "$(cat /etc/systemd/system/gpu-exporter.service)" "--port 9745"
grep -q "Creating Python venv" /tmp/install2.log && bad "重跑时又创建了 venv" || ok "重跑未重复创建 venv"

section "前置检查：缺少 NVIDIA 驱动要明确报错"
rm -f /usr/local/bin/nvidia-smi
bash "$CLIENT_DIR/install.sh" > /tmp/install3.log 2>&1
check "退出码" "$?" "1"
assert_contains "提示可操作" "$(cat /tmp/install3.log)" "install the NVIDIA driver"

section "参数处理"
bash "$CLIENT_DIR/install.sh" --bogus > /tmp/install4.log 2>&1
check "未知参数退出码" "$?" "2"
bash "$CLIENT_DIR/install.sh" --port > /tmp/install5.log 2>&1
[ "$?" -ne 0 ] && ok "--port 缺值时失败退出" || bad "--port 缺值未失败"

finish
