#!/bin/bash
# Exporter behaviour tests against a mock NVML module (no GPU, no real driver).
#
# Covers: metric/label compatibility with the pre-rewrite exporter (the
# dashboards depend on it), the self-check metrics, process-series diffing,
# GPU disappearance, isolation of unsupported optional metrics, graceful
# shutdown and fail-fast behaviour when NVML cannot be initialised.
set -u
SUITE_NAME="exporter"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/tests/common.sh"

EXPORTER="$REPO/client/exporter.py"
MOCK_DIR="$HERE/mock"
PORT="${EXPORTER_TEST_PORT:-19745}"
WORK="$(mktemp -d)"
SCENARIO="$WORK/scenario.json"
export PYTHONPATH="$MOCK_DIR"
export MOCK_NVML_SCENARIO="$SCENARIO"

cleanup() {
    [ -n "${EPID:-}" ] && kill "$EPID" 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

# ---- interpreter with prometheus_client ------------------------------------
PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import prometheus_client' >/dev/null 2>&1; then
    VENV="$REPO/tests/.venv"
    if [ ! -x "$VENV/bin/python" ]; then
        printf 'creating %s (needs network once)\n' "$VENV"
        "$PYTHON" -m venv "$VENV" >/dev/null 2>&1 || true
    fi
    if [ -x "$VENV/bin/python" ]; then
        "$VENV/bin/pip" install -q prometheus_client >/dev/null 2>&1
        PYTHON="$VENV/bin/python"
    fi
fi
if ! "$PYTHON" -c 'import prometheus_client' >/dev/null 2>&1; then
    echo "ERROR: prometheus_client unavailable and could not be installed." >&2
    echo "       pip install prometheus_client  (or set PYTHON=/path/to/python)" >&2
    exit 2
fi

scrape() { curl -s --max-time 3 "http://127.0.0.1:$PORT/metrics"; }
upv() { echo "$1" | grep "^gpu_up{gpu_index=\"$2\"}" | awk '{print $2}'; }

write_scenario() { cat > "$SCENARIO"; }

# ============================================================== scenario 1
# 所有能力都可用，用来核对完整指标面（含风扇这类可选指标）
write_scenario <<'JSON'
{"gpus":[
 {"name":"Tesla P4","uuid":"GPU-aaa","mem_total":8589934592,"mem_used":1073741824,
  "fan_fails":false,"fan":35,"procs":[{"pid":1,"mem":268435456}]},
 {"name":"Tesla P4","uuid":"GPU-bbb","mem_total":8589934592,"mem_used":2147483648,
  "fan_fails":false,"fan":40,"procs":[{"pid":1,"mem":1048576}]}
]}
JSON

section "启动 exporter (interval=1s)"
"$PYTHON" "$EXPORTER" --port "$PORT" --addr 127.0.0.1 --interval 1 > "$WORK/exporter.log" 2>&1 &
EPID=$!
for _ in $(seq 1 40); do
    curl -s --max-time 1 "http://127.0.0.1:$PORT/metrics" >/dev/null 2>&1 && break
    sleep 0.25
done
sleep 1.5
M1=$(scrape)

section "与旧版本指标兼容（看板依赖这些名字与标签）"
OLD_METRICS="gpu_info gpu_utilization_percent gpu_memory_utilization_percent
gpu_memory_used_bytes gpu_memory_free_bytes gpu_memory_total_bytes
gpu_temperature_celsius gpu_power_draw_watts gpu_fan_speed_percent
gpu_clock_sm_mhz gpu_clock_memory_mhz gpu_process_info gpu_process_memory_bytes"
MISSING=""
for m in $OLD_METRICS; do
    echo "$M1" | grep -q "^$m" || MISSING="$MISSING $m"
done
check "旧指标全部保留" "$MISSING" ""

labels_of() { echo "$1" | grep -oP "$2"'\{[^}]*' | head -1 | grep -oP '\w+(?==)' | sort | tr '\n' ','; }
check "gpu_info 标签" "$(labels_of "$M1" gpu_info)" "$(printf 'gpu_index\nmemory_total\nname\nuuid\n' | tr '\n' ',')"
check "gpu_process_info 标签" "$(labels_of "$M1" gpu_process_info)" "$(printf 'command\ncontainer\ngpu_index\npid\n' | tr '\n' ',')"
check "gpu_process_memory_bytes 标签" "$(labels_of "$M1" gpu_process_memory_bytes)" "$(printf 'container\ngpu_index\npid\n' | tr '\n' ',')"

section "自检指标"
for m in gpu_count gpu_exporter_last_success_timestamp_seconds gpu_exporter_collect_errors_total gpu_up; do
    echo "$M1" | grep -q "^$m" && ok "$m 存在" || bad "$m 缺失"
done
check "gpu_count=2" "$(echo "$M1" | awk '/^gpu_count /{print $2}')" "2.0"
check "gpu_up 全为 1" "$(echo "$M1" | grep '^gpu_up{' | awk '{print $2}' | sort -u | tr '\n' ',')" "1.0,"
check "进程数 (2 卡各 1 个)" "$(echo "$M1" | grep -c '^gpu_process_memory_bytes{')" "2"

section "进程退出后 series 被移除，不残留"
write_scenario <<'JSON'
{"gpus":[
 {"name":"Tesla P4","uuid":"GPU-aaa","mem_total":8589934592,"mem_used":0,"procs":[]},
 {"name":"Tesla P4","uuid":"GPU-bbb","mem_total":8589934592,"mem_used":2147483648,
  "procs":[{"pid":1,"mem":1048576}]}
]}
JSON
sleep 2.5
check "剩余进程数" "$(scrape | grep -c '^gpu_process_memory_bytes{')" "1"

section "GPU 掉卡：gpu_up 置 0，series 保留（曲线不断档）"
write_scenario <<'JSON'
{"gpus":[
 {"name":"Tesla P4","uuid":"GPU-aaa","mem_total":8589934592,"mem_used":0,"procs":[]}
]}
JSON
sleep 2.5
M3=$(scrape)
check "gpu_count=1" "$(echo "$M3" | awk '/^gpu_count /{print $2}')" "1.0"
check "gpu_up{0}=1" "$(upv "$M3" 0)" "1.0"
check "gpu_up{1}=0" "$(upv "$M3" 1)" "0.0"
check "掉卡 GPU 的 gpu_info 仍保留" "$(echo "$M3" | grep -c '^gpu_info{')" "2"

section "可选指标失败不影响同卡其它指标，且不保留陈旧值"
write_scenario <<'JSON'
{"gpus":[
 {"name":"Tesla P4","uuid":"GPU-aaa","mem_total":8589934592,"mem_used":0,
  "power_fails":true,"temp_fails":true,"procs":[]}
]}
JSON
sleep 2.5
M4=$(scrape)
check "利用率仍在" "$(echo "$M4" | grep -c '^gpu_utilization_percent{gpu_index="0"}')" "1"
check "显存仍在" "$(echo "$M4" | grep -c '^gpu_memory_used_bytes{gpu_index="0"}')" "1"
check "temperature series 已移除" "$(echo "$M4" | grep -c '^gpu_temperature_celsius{gpu_index="0"}')" "0"
check "power series 已移除" "$(echo "$M4" | grep -c '^gpu_power_draw_watts{gpu_index="0"}')" "0"

section "日志不刷屏（不支持的查询只报一次）"
check "unavailable 日志条数 <= 4" "$([ "$(grep -c unavailable "$WORK/exporter.log")" -le 4 ] && echo yes || echo no)" "yes"

section "SIGTERM 优雅退出"
kill -TERM "$EPID"; wait "$EPID" 2>/dev/null; RC=$?
EPID=""
check "退出码" "$RC" "0"
grep -q "shutting down" "$WORK/exporter.log" && ok "记录了 graceful shutdown" || bad "未记录 graceful shutdown"

section "NVML 初始化失败必须非 0 退出（旧版会假装健康）"
write_scenario <<'JSON'
{"init_fails":true,"gpus":[]}
JSON
MOCK_NVML_SCENARIO="$SCENARIO" "$PYTHON" "$EXPORTER" --port $((PORT + 1)) --addr 127.0.0.1 > "$WORK/fail.log" 2>&1
RC=$?
check "退出码" "$RC" "1"
assert_contains "输出了原因" "$(cat "$WORK/fail.log")" "cannot initialise NVML"

finish
