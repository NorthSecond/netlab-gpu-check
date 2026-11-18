"""A controllable fake of the `pynvml` (nvidia-ml-py) API, for testing.

The scenario is read from $MOCK_NVML_SCENARIO (a JSON file) on every call, so a
test can mutate GPU/process state between exporter collection cycles.
"""

import json
import os

NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_SM = 1
NVML_CLOCK_MEM = 2

SCENARIO = os.environ.get("MOCK_NVML_SCENARIO", "/tmp/gputest/scenario.json")


class NVMLError(Exception):
    pass


class NVMLError_NotSupported(NVMLError):
    pass


class NVMLError_NoPermission(NVMLError):
    pass


class _Mem:
    def __init__(self, total, used):
        self.total = total
        self.used = used
        self.free = total - used


class _Util:
    def __init__(self, gpu, memory):
        self.gpu = gpu
        self.memory = memory


class _Proc:
    def __init__(self, pid, used):
        self.pid = pid
        self.usedGpuMemory = used


def _load():
    try:
        with open(SCENARIO) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"init_fails": True, "gpus": []}


class _Handle:
    def __init__(self, index, cfg):
        self.index = index
        self.cfg = cfg


def nvmlInit():
    if _load().get("init_fails"):
        raise NVMLError("NVML Shared Library Not Found")


def nvmlShutdown():
    return None


def nvmlDeviceGetCount():
    s = _load()
    if s.get("count_fails"):
        raise NVMLError("unknown error")
    return len(s.get("gpus", []))


def nvmlDeviceGetHandleByIndex(i):
    gpus = _load().get("gpus", [])
    if i >= len(gpus):
        raise NVMLError("invalid argument")
    return _Handle(i, gpus[i])


def nvmlDeviceGetName(handle):
    if handle.cfg.get("name_fails"):
        raise NVMLError("unknown error")
    return handle.cfg.get("name", "Tesla P4")


def nvmlDeviceGetUUID(handle):
    return handle.cfg.get("uuid", "GPU-%d" % handle.index)


def nvmlDeviceGetMemoryInfo(handle):
    return _Mem(handle.cfg.get("mem_total", 8589934592), handle.cfg.get("mem_used", 1024))


def nvmlDeviceGetUtilizationRates(handle):
    return _Util(handle.cfg.get("util", 42), handle.cfg.get("mem_util", 7))


def nvmlDeviceGetTemperature(handle, sensor):
    if handle.cfg.get("temp_fails"):
        raise NVMLError_NotSupported("not supported")
    return handle.cfg.get("temp", 55)


def nvmlDeviceGetPowerUsage(handle):
    if handle.cfg.get("power_fails"):
        raise NVMLError_NotSupported("not supported")
    return handle.cfg.get("power_mw", 70000)


def nvmlDeviceGetFanSpeed(handle):
    if handle.cfg.get("fan_fails", True):
        raise NVMLError_NotSupported("not supported")
    return handle.cfg.get("fan", 30)


def nvmlDeviceGetClockInfo(handle, kind):
    if kind == NVML_CLOCK_SM:
        return handle.cfg.get("sm_clock", 1500)
    return handle.cfg.get("mem_clock", 3000)


def nvmlDeviceGetComputeRunningProcesses(handle):
    if handle.cfg.get("procs_fails"):
        raise NVMLError_NoPermission("insufficient permissions")
    return [_Proc(p["pid"], p.get("mem")) for p in handle.cfg.get("procs", [])]


def nvmlDeviceGetGraphicsRunningProcesses(handle):
    if handle.cfg.get("graphics_unsupported", True):
        raise NVMLError_NotSupported("not supported")
    return [_Proc(p["pid"], p.get("mem")) for p in handle.cfg.get("graphics_procs", [])]
