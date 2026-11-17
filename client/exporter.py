#!/usr/bin/env python3
"""GPU Prometheus Exporter for LXC-based clusters.

Design goals:
  * No config file, no on-disk state - everything is discovered from NVML.
  * Metrics are collected by a background thread; prometheus_client serves them.
  * ``/metrics`` must never expose a partial/empty process list while a
    collection cycle is in flight, so ``gpu_process_*`` series are maintained
    by diffing (add/update + remove stale) instead of ``clear()`` + repopulate.
  * Fail loudly: if NVML cannot be initialised the process exits non-zero so
    systemd restarts it, instead of serving an empty-but-"healthy" endpoint.
"""

import argparse
import re
import signal
import sys
import threading
import time

import pynvml
from prometheus_client import Counter, Gauge, start_http_server

PORT = 9745
# Collect every 5s. Prometheus scrapes every 20s (see server/prometheus.yml);
# collecting faster than the scrape interval keeps samples fresh and avoids
# sampling every scrape from the same stale snapshot. Can be overridden with
# --interval.
INTERVAL = 5

# ---------------------------------------------------------------------------
# Static / semi-static metrics
# ---------------------------------------------------------------------------
gpu_info = Gauge(
    "gpu_info",
    "GPU static information",
    ["gpu_index", "uuid", "name", "memory_total"],
)

# ---------------------------------------------------------------------------
# Dynamic per-GPU metrics
# ---------------------------------------------------------------------------
gpu_up = Gauge(
    "gpu_up",
    "1 if NVML reported this GPU during the last collection cycle, 0 otherwise",
    ["gpu_index"],
)
gpu_utilization_percent = Gauge(
    "gpu_utilization_percent", "GPU utilization", ["gpu_index"]
)
gpu_memory_utilization_percent = Gauge(
    "gpu_memory_utilization_percent", "Memory controller utilization", ["gpu_index"]
)
gpu_memory_used_bytes = Gauge(
    "gpu_memory_used_bytes", "GPU memory used", ["gpu_index"]
)
gpu_memory_free_bytes = Gauge(
    "gpu_memory_free_bytes", "GPU memory free", ["gpu_index"]
)
gpu_memory_total_bytes = Gauge(
    "gpu_memory_total_bytes", "GPU memory total", ["gpu_index"]
)
gpu_temperature_celsius = Gauge(
    "gpu_temperature_celsius", "GPU temperature", ["gpu_index"]
)
gpu_power_draw_watts = Gauge(
    "gpu_power_draw_watts", "GPU power draw", ["gpu_index"]
)
gpu_fan_speed_percent = Gauge(
    "gpu_fan_speed_percent", "GPU fan speed", ["gpu_index"]
)
gpu_clock_sm_mhz = Gauge(
    "gpu_clock_sm_mhz", "SM clock", ["gpu_index"]
)
gpu_clock_memory_mhz = Gauge(
    "gpu_clock_memory_mhz", "Memory clock", ["gpu_index"]
)

# ---------------------------------------------------------------------------
# Process-level metrics (kept in sync by diffing, never cleared in bulk)
# ---------------------------------------------------------------------------
gpu_process_info = Gauge(
    "gpu_process_info",
    "GPU process metadata",
    ["gpu_index", "pid", "container", "command"],
)
gpu_process_memory_bytes = Gauge(
    "gpu_process_memory_bytes",
    "GPU memory used by process",
    ["gpu_index", "pid", "container"],
)

# ---------------------------------------------------------------------------
# Self-check metrics: make exporter failures detectable from the outside.
# ---------------------------------------------------------------------------
gpu_count_metric = Gauge(
    "gpu_count", "Number of GPUs currently reported by NVML"
)
gpu_exporter_last_success_timestamp_seconds = Gauge(
    "gpu_exporter_last_success_timestamp_seconds",
    "Unix timestamp of the last successful collection cycle",
)
gpu_exporter_collect_errors_total = Counter(
    "gpu_exporter_collect_errors_total",
    "Total number of collection cycles that failed",
)

# Label sets currently published for the process metrics. Guarded by _lock so
# the HTTP thread only ever sees a consistent (old or new) set of series.
_lock = threading.Lock()
_seen_process_info = set()
_seen_process_memory = set()
# gpu_index values that NVML has reported at least once, so we can flag the
# ones that disappear (bus drop / Xid) without deleting their series.
_known_gpu_indexes = set()

_shutdown = threading.Event()


def log(msg):
    """Write a single line to stderr (systemd captures it into the journal)."""
    print(msg, file=sys.stderr, flush=True)


_warned = set()


def warn_once(key, msg):
    """Log a condition only the first time it is seen.

    Unsupported NVML queries (graphics-process accounting, fan/temperature
    sensors on passively cooled cards, ...) are permanent per GPU and would
    otherwise fill the journal with the same line every collection cycle.
    """
    if key in _warned:
        return
    _warned.add(key)
    log(msg)


def pid_to_container(pid: int):
    """Map a PID to its LXC/Docker container name via cgroup, if any."""
    try:
        with open("/proc/%d/cgroup" % pid, "r") as f:
            content = f.read()
    except OSError:
        return None
    # LXC:      lxc.payload.<name> / lxc.monitor.<name>
    # Docker:   docker-<id>.scope / cri-containerd-<id>.scope
    m = re.search(r"lxc\.(?:payload|monitor)\.([A-Za-z0-9_-]+)", content)
    if m:
        return m.group(1)
    return None


def read_comm(pid: int):
    try:
        with open("/proc/%d/comm" % pid, "r") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _drop(metric, *labelvalues):
    """Stop publishing a series we can no longer read.

    Leaving the previous value in place would make the dashboard show a stale
    (and therefore misleading) reading forever - a gap is the honest signal.
    """
    try:
        metric.remove(*labelvalues)
    except KeyError:
        pass


def _publish_process_metrics(collected_info, collected_memory):
    """Publish process series and drop the ones that vanished.

    Replacing the bulk ``clear()`` call removes the window in which a scrape
    could observe an empty process list.
    """
    global _seen_process_info, _seen_process_memory
    with _lock:
        for labels, value in collected_info.items():
            gpu_process_info.labels(*labels).set(value)
        for labels, value in collected_memory.items():
            gpu_process_memory_bytes.labels(*labels).set(value)

        for stale in _seen_process_info - set(collected_info):
            try:
                gpu_process_info.remove(*stale)
            except KeyError:
                pass
        for stale in _seen_process_memory - set(collected_memory):
            try:
                gpu_process_memory_bytes.remove(*stale)
            except KeyError:
                pass

        _seen_process_info = set(collected_info)
        _seen_process_memory = set(collected_memory)


def _collect_processes(handle, idx, collected_info, collected_memory):
    """Collect compute + graphics processes attached to one GPU."""
    procs = []
    for getter_name in (
        "nvmlDeviceGetComputeRunningProcesses",
        "nvmlDeviceGetGraphicsRunningProcesses",
    ):
        getter = getattr(pynvml, getter_name, None)
        if getter is None:
            continue
        try:
            procs.extend(getter(handle))
        except pynvml.NVMLError as e:
            # NotSupported on older drivers / headless GPUs: not fatal, and by
            # definition permanent, so only mention it once per GPU.
            warn_once(
                (idx, getter_name),
                "GPU %s %s unavailable: %s" % (idx, getter_name, e),
            )

    seen_pids = set()
    for proc in procs:
        if proc.pid in seen_pids:
            continue
        seen_pids.add(proc.pid)

        container = pid_to_container(proc.pid) or "host"
        collected_info[(idx, str(proc.pid), container, read_comm(proc.pid))] = 1
        # usedGpuMemory is None on some drivers (e.g. WDDM) and for processes
        # whose accounting is not exposed; never publish None as a metric.
        if proc.usedGpuMemory is not None:
            collected_memory[(idx, str(proc.pid), container)] = proc.usedGpuMemory


def collect_metrics():
    """Run one collection cycle. Returns True when it completed."""
    try:
        count = pynvml.nvmlDeviceGetCount()
    except pynvml.NVMLError as e:
        log("nvmlDeviceGetCount failed: %s" % e)
        return False

    gpu_count_metric.set(count)

    collected_info = {}
    collected_memory = {}
    present = set()

    for i in range(count):
        idx = str(i)
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        except pynvml.NVMLError as e:
            if not isinstance(e, pynvml.NVMLError_NotSupported):
                log("nvmlDeviceGetHandleByIndex(%d) failed: %s" % (i, e))
            else:
                warn_once(
                    ("handle", i), "GPU %d handle unavailable: %s" % (i, e)
                )
            continue

        try:
            name = _decode(pynvml.nvmlDeviceGetName(handle))
            uuid = _decode(pynvml.nvmlDeviceGetUUID(handle))
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        except pynvml.NVMLError as e:
            log("GPU %d basic metrics failed: %s" % (i, e))
            continue

        present.add(idx)
        _known_gpu_indexes.add(idx)

        gpu_up.labels(gpu_index=idx).set(1)
        gpu_info.labels(
            gpu_index=idx,
            uuid=uuid,
            name=name,
            memory_total=str(mem.total),
        ).set(1)
        gpu_utilization_percent.labels(gpu_index=idx).set(util.gpu)
        gpu_memory_utilization_percent.labels(gpu_index=idx).set(util.memory)
        gpu_memory_used_bytes.labels(gpu_index=idx).set(mem.used)
        gpu_memory_free_bytes.labels(gpu_index=idx).set(mem.free)
        gpu_memory_total_bytes.labels(gpu_index=idx).set(mem.total)

        # Optional metrics: unsupported on some GPUs/drivers, and one of them
        # must not take the whole GPU's metrics down with it.
        try:
            gpu_temperature_celsius.labels(gpu_index=idx).set(
                pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            )
        except pynvml.NVMLError as e:
            _drop(gpu_temperature_celsius, idx)
            warn_once(
                ("temperature", idx),
                "GPU %d temperature unavailable: %s" % (i, e),
            )

        try:
            gpu_power_draw_watts.labels(gpu_index=idx).set(
                pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
            )
        except pynvml.NVMLError as e:
            _drop(gpu_power_draw_watts, idx)
            warn_once(("power", idx), "GPU %d power unavailable: %s" % (i, e))

        try:
            gpu_fan_speed_percent.labels(gpu_index=idx).set(
                pynvml.nvmlDeviceGetFanSpeed(handle)
            )
        except pynvml.NVMLError:
            # Passive cooling has no fan; publishing nothing is correct.
            _drop(gpu_fan_speed_percent, idx)

        try:
            gpu_clock_sm_mhz.labels(gpu_index=idx).set(
                pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
            )
        except pynvml.NVMLError:
            _drop(gpu_clock_sm_mhz, idx)

        try:
            gpu_clock_memory_mhz.labels(gpu_index=idx).set(
                pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            )
        except pynvml.NVMLError:
            _drop(gpu_clock_memory_mhz, idx)

        _collect_processes(handle, i, collected_info, collected_memory)

    # Flag GPUs that used to be visible but are gone now. Series are kept
    # (so time ranges stay continuous) but gpu_up drops to 0.
    for idx in _known_gpu_indexes - present:
        gpu_up.labels(gpu_index=idx).set(0)

    _publish_process_metrics(collected_info, collected_memory)
    return True


def init_nvml(attempts=5, delay=6.0):
    """Initialise NVML, retrying briefly (driver may still be loading)."""
    for attempt in range(1, attempts + 1):
        try:
            pynvml.nvmlInit()
            return True
        except pynvml.NVMLError as e:
            log("nvmlInit failed (attempt %d/%d): %s" % (attempt, attempts, e))
            if attempt < attempts:
                _shutdown.wait(delay)
    return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="GPU Prometheus Exporter")
    parser.add_argument(
        "--port", type=int, default=PORT, help="HTTP port to expose metrics on"
    )
    parser.add_argument(
        "--addr", default="0.0.0.0", help="address to bind the HTTP server to"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=INTERVAL,
        help="seconds between collection cycles (default: %d)" % INTERVAL,
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    def _handle_signal(signum, _frame):
        log("received signal %d, shutting down" % signum)
        _shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not init_nvml():
        log("cannot initialise NVML, exiting so systemd can restart us")
        return 1

    start_http_server(args.port, addr=args.addr)
    log("GPU exporter listening on %s:%d (collect interval %ds)"
        % (args.addr, args.port, args.interval))

    # Collect once before serving so the first scrape is never empty.
    try:
        if collect_metrics():
            gpu_exporter_last_success_timestamp_seconds.set(time.time())
        else:
            gpu_exporter_collect_errors_total.inc()
    except Exception as e:  # noqa: BLE001 - keep the exporter alive
        gpu_exporter_collect_errors_total.inc()
        log("collect_metrics failed: %s" % e)

    while not _shutdown.wait(args.interval):
        try:
            if collect_metrics():
                gpu_exporter_last_success_timestamp_seconds.set(time.time())
            else:
                gpu_exporter_collect_errors_total.inc()
        except Exception as e:  # noqa: BLE001 - keep the exporter alive
            gpu_exporter_collect_errors_total.inc()
            log("collect_metrics failed: %s" % e)

    try:
        pynvml.nvmlShutdown()
    except pynvml.NVMLError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
