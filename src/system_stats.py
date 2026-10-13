"""Best-effort system resource sampling for the Panel.

Reads /proc, sysfs, and standard platform tools directly — no new
dependencies. Every collector degrades to None/empty when the platform
cannot provide it, so the endpoint always responds and the Panel shows
whatever exists on this machine.
"""

import ctypes
import os
import platform
import re
import subprocess
import threading
import time

_OS = platform.system()
_KIB = 1024
_MIB = _KIB * _KIB
_GIB = _MIB * _KIB

_LOCK = threading.Lock()
_CACHE_TTL_S = 5.0
_cache = {"ts": 0.0, "data": None}
_last_proc_stat = None

_CPU_CHIP_HINTS = ("coretemp", "k10temp", "k8temp", "zenpower", "cpu_thermal", "cpu-thermal", "acpitz")
_GPU_CHIP_HINTS = ("amdgpu", "radeon", "nouveau", "nvidia", "i915", "xe", "gpu")


def _run(cmd, timeout=4):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def _read_file(path):
    try:
        with open(path, "r") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


# ---------- CPU ----------

def _read_proc_stat():
    text = _read_file("/proc/stat")
    if not text:
        return None
    fields = text.splitlines()[0].split()[1:]
    try:
        values = [int(x) for x in fields]
    except ValueError:
        return None
    if len(values) < 5:
        return None
    return values[3] + values[4], sum(values)


def _cpu_percent():
    global _last_proc_stat
    if _OS == "Linux":
        current = _read_proc_stat()
        if not current:
            return None
        previous = _last_proc_stat
        if previous is None:
            previous = current
            time.sleep(0.3)
            current = _read_proc_stat() or current
        _last_proc_stat = current
        delta_total = current[1] - previous[1]
        delta_idle = current[0] - previous[0]
        if delta_total <= 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * (1.0 - delta_idle / delta_total))), 1)
    if _OS == "Darwin":
        out = _run(["top", "-l", "2", "-n", "0", "-s", "1"], timeout=6)
        if not out:
            return None
        idle = re.findall(r"(\d+(?:\.\d+)?)% idle", out)
        if not idle:
            return None
        return round(100.0 - float(idle[-1]), 1)
    if _OS == "Windows":
        out = _run(["wmic", "cpu", "get", "LoadPercentage", "/value"])
        match = re.search(r"LoadPercentage=(\d+)", out or "")
        return _to_float(match.group(1)) if match else None
    return None


# ---------- memory ----------

def _mem_payload(total, used, swap_total, swap_used):
    data = {
        "total": total,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }
    if swap_total:
        data["swap_total"] = swap_total
        data["swap_used"] = swap_used
        data["swap_percent"] = round(100.0 * swap_used / swap_total, 1)
    return data


def _mem_linux():
    text = _read_file("/proc/meminfo")
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        try:
            values[key] = int(rest.split()[0]) * _KIB
        except (IndexError, ValueError):
            continue
    total = values.get("MemTotal", 0)
    if not total:
        return None
    available = values.get("MemAvailable", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_used = max(0, swap_total - values.get("SwapFree", 0))
    return _mem_payload(total, max(0, total - available), swap_total, swap_used)


def _parse_size(value, unit):
    multipliers = {"B": 1, "K": _KIB, "M": _MIB, "G": _GIB, "T": _GIB * _KIB, "P": _GIB * _MIB}
    number = _to_float(value)
    if number is None:
        return 0
    return number * multipliers.get(unit.upper(), 1)


def _mem_macos():
    total_text = _run(["sysctl", "-n", "hw.memsize"])
    total = _to_float(total_text.strip()) if total_text else None
    if not total:
        return None
    total = int(total)
    vm = _run(["vm_stat"]) or ""
    page_size = 16384
    size_match = re.search(r"page size of (\d+) bytes", vm)
    if size_match:
        page_size = int(size_match.group(1))
    counts = {}
    for match in re.finditer(r"^([^:]+):\s+(\d+)\.$", vm, re.MULTILINE):
        counts[match.group(1)] = int(match.group(2))
    free = counts.get("Pages free", 0) + counts.get("Pages speculative", 0)
    available = (free + counts.get("Pages inactive", 0) + counts.get("Pages purgeable", 0)) * page_size
    swap_total = swap_used = 0
    swap_text = _run(["sysctl", "-n", "vm.swapusage"]) or ""
    total_m = re.search(r"total = ([\d.]+)([BKMGTP])", swap_text)
    used_m = re.search(r"used = ([\d.]+)([BKMGTP])", swap_text)
    if total_m and used_m:
        swap_total = int(_parse_size(total_m.group(1), total_m.group(2)))
        swap_used = int(_parse_size(used_m.group(1), used_m.group(2)))
    return _mem_payload(total, max(0, total - available), swap_total, swap_used)


def _mem_windows():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        if not total:
            return None
        swap_total = max(0, int(status.ullTotalPageFile) - total)
        swap_used = max(0, swap_total - max(0, int(status.ullAvailPageFile) - int(status.ullAvailPhys)))
        return _mem_payload(total, total - int(status.ullAvailPhys), swap_total, swap_used)
    except Exception:
        return None


def _memory():
    if _OS == "Linux":
        return _mem_linux()
    if _OS == "Darwin":
        return _mem_macos()
    if _OS == "Windows":
        return _mem_windows()
    return None


# ---------- temperatures ----------

def _hwmon_chips():
    chips = []
    base = "/sys/class/hwmon"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return chips
    for entry in entries:
        path = os.path.join(base, entry)
        chip = _read_file(os.path.join(path, "name"))
        if not chip:
            continue
        try:
            files = sorted(os.listdir(path))
        except OSError:
            continue
        hottest = None
        for name in files:
            if not (name.startswith("temp") and name.endswith("_input")):
                continue
            raw = _to_float(_read_file(os.path.join(path, name)))
            if raw is None:
                continue
            celsius = raw / 1000.0
            if 0 < celsius <= 150 and (hottest is None or celsius > hottest):
                hottest = celsius
        if hottest is not None:
            chips.append({"name": chip.lower(), "temp": round(hottest, 1)})
    return chips


def _thermal_zones():
    zones = []
    base = "/sys/class/thermal"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return zones
    for entry in entries:
        if not entry.startswith("thermal_zone"):
            continue
        path = os.path.join(base, entry)
        kind = (_read_file(os.path.join(path, "type")) or "").lower()
        raw = _to_float(_read_file(os.path.join(path, "temp")))
        if raw is None:
            continue
        celsius = raw / 1000.0
        if 0 < celsius <= 150:
            zones.append({"name": kind or entry, "temp": round(celsius, 1)})
    return zones


def _cpu_temp():
    if _OS == "Linux":
        for chip in _hwmon_chips():
            if any(hint in chip["name"] for hint in _CPU_CHIP_HINTS):
                return {"cpu": chip["temp"], "source": chip["name"]}
        for zone in _thermal_zones():
            if any(hint in zone["name"] for hint in ("cpu", "x86", "soc")):
                return {"cpu": zone["temp"], "source": zone["name"]}
        return {"cpu": None, "source": None}
    if _OS == "Windows":
        out = _run(["wmic", "/namespace:\\\\root\\wmi", "PATH", "MSAcpi_ThermalZoneTemperature", "get", "CurrentTemperature"])
        match = re.search(r"CurrentTemperature\s+(\d+)", out or "")
        if match:
            celsius = int(match.group(1)) / 10.0 - 273.15
            if -20 <= celsius <= 120:
                return {"cpu": round(celsius, 1), "source": "thermal zone"}
    return {"cpu": None, "source": None}


# ---------- GPUs ----------

def parse_nvidia_smi(text):
    gpus = []
    for line in (text or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        name = ", ".join(parts[1:-4]).strip()
        mem_used = _to_float(parts[-2])
        mem_total = _to_float(parts[-1])
        gpus.append({
            "name": name or ("GPU " + parts[0]),
            "temp": _to_float(parts[-4]),
            "util": _to_float(parts[-3]),
            "mem_used": int(mem_used * _MIB) if mem_used is not None else None,
            "mem_total": int(mem_total * _MIB) if mem_total is not None else None,
        })
    return gpus


def _macos_gpu():
    out = _run(["ioreg", "-r", "-c", "AGXAccelerator"])
    util = None
    if out:
        match = re.search(r'"Device Utilization %"\s*=\s*([\d.]+)', out)
        if not match:
            match = re.search(r'"Renderer Utilization %"\s*=\s*([\d.]+)', out)
        if match:
            util = _to_float(match.group(1))
    if util is None:
        return []
    brand = (_run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "").strip()
    name = (brand + " GPU") if brand else "Integrated GPU"
    return [{
        "name": name,
        "temp": None,
        "util": util,
        "mem_used": None,
        "mem_total": None,
    }]


def _gpus():
    out = _run(["nvidia-smi", "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"])
    if out:
        gpus = parse_nvidia_smi(out)
        if gpus:
            return gpus
    if _OS == "Linux":
        gpus = []
        for chip in _hwmon_chips():
            if any(hint in chip["name"] for hint in _GPU_CHIP_HINTS):
                gpus.append({"name": chip["name"], "temp": chip["temp"], "util": None, "mem_used": None, "mem_total": None})
        return gpus
    if _OS == "Darwin":
        return _macos_gpu()
    return []


# ---------- disks ----------

def _apfs_container_key(device):
    # /dev/disk3s1s1 -> /dev/disk3 : APFS volumes in one container share space
    return re.sub(r"s\d+(s\d+)?$", "", device)


def parse_df(text, group_containers=False):
    disks = []
    seen_usage = set()
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        device = parts[0]
        try:
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            avail_kb = int(parts[3])
        except ValueError:
            continue
        mount = " ".join(parts[5:])
        if not (device.startswith("/dev/") or device == "overlay"):
            continue
        if total_kb < _GIB // _KIB:
            continue
        signature = (total_kb, used_kb)
        if signature in seen_usage:
            continue
        seen_usage.add(signature)
        disks.append({
            "device": device,
            "mount": mount,
            "total": total_kb * _KIB,
            "used": used_kb * _KIB,
            "free": avail_kb * _KIB,
            "percent": round(100.0 * used_kb / total_kb, 1) if total_kb else 0.0,
        })
    if group_containers:
        # Keep one row per APFS container: the volume with the most usage
        # (the data volume) represents the whole container.
        best = {}
        order = []
        for disk in disks:
            key = _apfs_container_key(disk["device"])
            if key not in best:
                best[key] = disk
                order.append(key)
            elif disk["used"] > best[key]["used"]:
                best[key] = disk
        disks = [best[key] for key in order]
    return disks


def parse_wmic_disks(text):
    disks = []
    for caption, free, size in re.findall(r"Caption=(\w:)\s+FreeSpace=(\d+)\s+Size=(\d+)", text or ""):
        total = int(size)
        if total < _GIB:
            continue
        free_bytes = int(free)
        used = total - free_bytes
        disks.append({
            "device": caption,
            "mount": caption + "\\",
            "total": total,
            "used": used,
            "free": free_bytes,
            "percent": round(100.0 * used / total, 1),
        })
    return disks


def _disks():
    if _OS == "Windows":
        return parse_wmic_disks(_run(["wmic", "logicaldisk", "where", "DriveType=3", "get", "Caption,FreeSpace,Size", "/value"]))
    return parse_df(_run(["df", "-P", "-k"]), group_containers=(_OS == "Darwin"))


# ---------- snapshot ----------

def _collect():
    load = None
    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        pass
    return {
        "ok": True,
        "ts": round(time.time(), 3),
        "platform": _OS,
        "cpu": {"percent": _cpu_percent(), "cores": os.cpu_count(), "load": load},
        "mem": _memory(),
        "temps": _cpu_temp(),
        "gpus": _gpus(),
        "disks": _disks(),
    }


def snapshot():
    now = time.time()
    with _LOCK:
        if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_S:
            return _cache["data"]
    data = _collect()
    with _LOCK:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data
