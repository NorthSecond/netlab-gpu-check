#!/usr/bin/env python3
"""
GPU Prometheus Exporter for LXC-based cluster.
Minimal design: no config file, no logs, no persistence on client.
"""

import argparse
import re
import time

import pynvml
from prometheus_client import start_http_server, Gauge

PORT = 9745
INTERVAL = 20

# Static / semi-static metrics
gpu_info = Gauge(
    "gpu_info",
    "GPU static information",
    ["gpu_index", "uuid", "name", "memory_total"],
)

# Dynamic per-GPU metrics
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

# Process-level metrics (cleared on every scrape cycle)
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


def pid_to_container(pid: int):
    """Map PID to LXC 4.0+ container name via cgroup."""
    try:
        with open(f"/proc/{pid}/cgroup", "r") as f:
            content = f.read()
    except (OSError, IOError):
        return None
    m = re.search(r"lxc\.payload\.([a-zA-Z0-9_-]+)", content)
    if m:
        return m.group(1)
    return None


def read_comm(pid: int):
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip()
    except (OSError, IOError):
        return "unknown"


def collect_metrics():
    try:
        count = pynvml.nvmlDeviceGetCount()
    except pynvml.NVMLError as e:
        print(f"nvmlDeviceGetCount failed: {e}")
        return

    # Clear process metrics before repopulating
    gpu_process_info.clear()
    gpu_process_memory_bytes.clear()

    for i in range(count):
        idx = str(i)
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        except pynvml.NVMLError as e:
            print(f"nvmlDeviceGetHandleByIndex({i}) failed: {e}")
            continue

        try:
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
            uuid = pynvml.nvmlDeviceGetUUID(handle)
            if isinstance(uuid, bytes):
                uuid = uuid.decode("utf-8", errors="replace")
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            temp = pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU
            )
            power = pynvml.nvmlDeviceGetPowerUsage(handle)

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
            gpu_temperature_celsius.labels(gpu_index=idx).set(temp)
            gpu_power_draw_watts.labels(gpu_index=idx).set(power / 1000.0)

            # Optional metrics (may not be supported on all GPUs)
            try:
                fan = pynvml.nvmlDeviceGetFanSpeed(handle)
                gpu_fan_speed_percent.labels(gpu_index=idx).set(fan)
            except pynvml.NVMLError:
                pass

            try:
                sm_clock = pynvml.nvmlDeviceGetClockInfo(
                    handle, pynvml.NVML_CLOCK_SM
                )
                gpu_clock_sm_mhz.labels(gpu_index=idx).set(sm_clock)
            except pynvml.NVMLError:
                pass

            try:
                mem_clock = pynvml.nvmlDeviceGetClockInfo(
                    handle, pynvml.NVML_CLOCK_MEM
                )
                gpu_clock_memory_mhz.labels(gpu_index=idx).set(mem_clock)
            except pynvml.NVMLError:
                pass

        except pynvml.NVMLError as e:
            print(f"GPU {i} basic metrics failed: {e}")
            continue

        # Process-level metrics
        try:
            procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
            for proc in procs:
                container = pid_to_container(proc.pid)
                comm = read_comm(proc.pid)
                gpu_process_info.labels(
                    gpu_index=idx,
                    pid=str(proc.pid),
                    container=container or "host",
                    command=comm,
                ).set(1)
                gpu_process_memory_bytes.labels(
                    gpu_index=idx,
                    pid=str(proc.pid),
                    container=container or "host",
                ).set(proc.usedGpuMemory)
        except pynvml.NVMLError as e:
            print(f"GPU {i} process query failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="GPU Prometheus Exporter")
    parser.add_argument(
        "--port", type=int, default=PORT, help="HTTP port to expose metrics on"
    )
    args = parser.parse_args()

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as e:
        print(f"nvmlInit failed: {e}")

    start_http_server(args.port)
    print(f"GPU exporter listening on :{args.port}")

    while True:
        collect_metrics()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
