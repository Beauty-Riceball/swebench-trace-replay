"""Low-overhead Linux host/cgroup-v2 sampling using only Python's stdlib.

    monitor = Monitor("metrics.jsonl").start()
    monitor.register("sandbox-1", "/sys/fs/cgroup/example", 1234,
                     {"concurrency": 8, "instance_id": "example"})
    ...
    monitor.unregister("sandbox-1")
    monitor.stop()

Each JSONL row is a complete sample. Counter values remain cumulative; derive
rates using monotonic_ns. Memory byte files retain bytes, meminfo reports its
original units, CPU ticks use clock_ticks_per_second, and disk sectors are 512 B.
Missing files and exited processes appear in read_errors, rather than as zero.
Registrations may be changed while the background sampler is running.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq",
              "steal", "guest", "guest_nice")
DISK_FIELDS = (
    "reads_completed", "reads_merged", "sectors_read", "read_ms",
    "writes_completed", "writes_merged", "sectors_written", "write_ms",
    "ios_in_progress", "io_ms", "weighted_io_ms", "discards_completed",
    "discards_merged", "sectors_discarded", "discard_ms", "flushes_completed",
    "flush_ms",
)
VMSTAT_KEYS = {
    "pgfault", "pgmajfault", "pswpin", "pswpout", "pgpgin", "pgpgout",
    "oom_kill", "pgactivate", "pgdeactivate", "pgrefill", "pgrotated",
    "pglazyfree", "pglazyfreed", "zone_reclaim_failed", "nr_free_pages",
    "nr_anon_pages", "nr_file_pages", "nr_shmem", "nr_dirty", "nr_writeback",
    "nr_unevictable", "nr_slab_reclaimable", "nr_slab_unreclaimable",
    "nr_slab_reclaimable_b", "nr_slab_unreclaimable_b", "nr_page_table_pages",
    "nr_kernel_stack", "nr_kernel_stack_kb", "nr_swapcached", "nr_foll_pin_acquired",
    "nr_foll_pin_released",
}
VMSTAT_PREFIXES = ("pgscan_", "pgsteal_", "allocstall_", "compact_", "workingset_",
                   "numa_", "pgdemote_", "pgpromote_", "nr_active_", "nr_inactive_")


def _key_values(raw: str) -> dict[str, int]:
    return {parts[0]: int(parts[1]) for line in raw.splitlines()
            if len(parts := line.split()) == 2}


def _pressure(raw: str) -> dict[str, dict[str, int | float]]:
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            result[parts[0]] = {
                key: int(value) if key == "total" else float(value)
                for token in parts[1:] for key, value in [token.split("=", 1)]
            }
    return result


def _indexed_values(raw: str) -> dict[str, dict[str, int]]:
    """Parse memory.numa_stat and io.stat without assuming field ordering."""
    result = {}
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            result[parts[0]] = {
                key: int(value) for token in parts[1:]
                for key, value in [token.split("=", 1)]
            }
    return result


class Monitor:
    """Append JSONL snapshots; start/stop are idempotent and thread-safe.

    register(name, cgroup_path, pid, metadata) replaces an existing registration.
    pid may be None; metadata must be JSON-serializable. unregister(name) returns
    whether the name existed. A snapshot already being read may contain a just
    unregistered entry. last_error records unexpected sampler/output failures;
    stop() raises RuntimeError for those failures. Ordinary read failures only
    populate the snapshot's read_errors.
    """

    def __init__(self, out_path: str | os.PathLike, interval: float = 1):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be a positive finite number")
        self.out_path = Path(out_path)
        self.interval = float(interval)
        self.last_error: str | None = None
        self._registrations: dict[str, dict[str, Any]] = {}
        self._registration_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._clock_ticks = os.sysconf("SC_CLK_TCK")
        self._page_size = os.sysconf("SC_PAGE_SIZE")

    def register(self, container_name: str, cgroup_path: str | os.PathLike,
                 pid: int | None, metadata: Any = None) -> None:
        if not container_name:
            raise ValueError("container_name must be nonempty")
        if pid is not None and (not isinstance(pid, int) or pid <= 0):
            raise ValueError("pid must be a positive integer or None")
        # Snapshot caller-owned metadata now, so later caller mutation is safe.
        frozen_metadata = json.loads(json.dumps(metadata, allow_nan=False))
        registration = {
            "container_name": str(container_name),
            "cgroup_path": os.fspath(cgroup_path), "pid": pid,
            "metadata": frozen_metadata,
        }
        with self._registration_lock:
            self._registrations[str(container_name)] = registration

    def unregister(self, container_name: str) -> bool:
        with self._registration_lock:
            return self._registrations.pop(str(container_name), None) is not None

    def start(self) -> "Monitor":
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = self.out_path.open("a", encoding="utf-8", buffering=1)
            self.last_error = None
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run,
                                            name="baseline-monitor", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join()
            if self.last_error is not None:
                raise RuntimeError("monitor failed: " + self.last_error)

    @staticmethod
    def _read(path: Path, errors: dict[str, str]) -> str | None:
        try:
            return path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            errors[str(path)] = f"{type(exc).__name__}: {exc}"
            return None

    def _parse_file(self, path: Path, parser, errors: dict[str, str]):
        raw = self._read(path, errors)
        if raw is None:
            return None
        try:
            return parser(raw)
        except (ValueError, IndexError, TypeError) as exc:
            errors[str(path)] = f"parse {type(exc).__name__}: {exc}"
            return None

    @staticmethod
    def _proc_stat(raw: str) -> dict[str, Any]:
        result = {}
        for line in raw.splitlines():
            fields = line.split()
            if not fields:
                continue
            key = fields[0]
            if key.startswith("cpu"):
                result[key] = dict(zip(CPU_FIELDS, map(int, fields[1:])))
            elif key in {"intr", "ctxt", "btime", "processes", "procs_running",
                          "procs_blocked", "softirq"}:
                # First intr/softirq value is the cumulative total.
                result[key] = int(fields[1])
        return result

    @staticmethod
    def _meminfo(raw: str) -> dict[str, Any]:
        values, units = {}, {}
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                key = fields[0].rstrip(":")
                values[key] = int(fields[1])
                units[key] = fields[2] if len(fields) >= 3 else "count"
        return {"values": values, "units": units}

    @staticmethod
    def _vmstat(raw: str) -> dict[str, int]:
        return {key: value for key, value in _key_values(raw).items()
                if key in VMSTAT_KEYS or key.startswith(VMSTAT_PREFIXES)}

    @staticmethod
    def _diskstats(raw: str) -> dict[str, Any]:
        devices = {}
        for line in raw.splitlines():
            fields = line.split()
            if len(fields) >= 14 and fields[2].startswith("nvme"):
                devices[fields[2]] = {
                    "major": int(fields[0]), "minor": int(fields[1]),
                    **dict(zip(DISK_FIELDS, map(int, fields[3:]))),
                }
        return devices

    @staticmethod
    def _pid_stat(raw: str) -> dict[str, Any]:
        # comm can contain spaces and parentheses; fields begin after its last ).
        fields = raw[raw.rfind(")") + 2:].split()
        indices = {"ppid": 1, "minflt": 7, "cminflt": 8, "majflt": 9,
                   "cmajflt": 10, "utime_ticks": 11, "stime_ticks": 12,
                   "num_threads": 17, "starttime_ticks": 19,
                   "vsize_bytes": 20, "rss_pages": 21, "processor": 36}
        return {"state": fields[0], **{
            name: int(fields[index]) for name, index in indices.items()
            if index < len(fields)
        }}

    def _host(self) -> dict[str, Any]:
        errors: dict[str, str] = {}
        parsers = {"stat": self._proc_stat, "meminfo": self._meminfo,
                   "vmstat": self._vmstat, "diskstats": self._diskstats}
        result = {name: self._parse_file(Path("/proc") / name, parser, errors)
                  for name, parser in parsers.items()}
        result["pressure"] = {
            resource: self._parse_file(Path("/proc/pressure") / resource,
                                       _pressure, errors)
            for resource in ("cpu", "memory", "io")
        }
        result["read_errors"] = errors
        return result

    def _container(self, registration: dict[str, Any]) -> dict[str, Any]:
        result = dict(registration)
        errors: dict[str, str] = {}
        base = Path(registration["cgroup_path"])
        parsers = {
            "cpu.stat": _key_values, "memory.current": int, "memory.peak": int,
            "memory.stat": _key_values, "memory.events": _key_values,
            "memory.events.local": _key_values, "memory.swap.current": int,
            "io.stat": _indexed_values, "memory.numa_stat": _indexed_values,
            "cpu.pressure": _pressure, "memory.pressure": _pressure,
            "io.pressure": _pressure,
        }
        result["cgroup"] = {
            name: self._parse_file(base / name, parser, errors)
            for name, parser in parsers.items()
        }
        if registration["pid"] is not None:
            result["process_stat"] = self._parse_file(
                Path("/proc") / str(registration["pid"]) / "stat",
                self._pid_stat, errors)
        result["read_errors"] = errors
        return result

    def _sample(self) -> dict[str, Any]:
        started = time.monotonic_ns()
        wall_ns = time.time_ns()
        with self._registration_lock:
            registrations = copy.deepcopy(list(self._registrations.values()))
        result = {
            "schema_version": 1, "type": "sample", "unix_ns": wall_ns,
            "timestamp_utc": dt.datetime.fromtimestamp(
                wall_ns / 1e9, tz=dt.timezone.utc).isoformat(),
            "monotonic_ns": started, "interval_seconds": self.interval,
            "clock_ticks_per_second": self._clock_ticks,
            "page_size_bytes": self._page_size,
            "host": self._host(),
            "containers": [self._container(item) for item in registrations],
        }
        result["collection_duration_ms"] = (time.monotonic_ns() - started) / 1e6
        return result

    def _run(self) -> None:
        deadline = time.monotonic()
        try:
            while not self._stop_event.is_set():
                row = self._sample()
                self._stream.write(json.dumps(row, separators=(",", ":"),
                                              allow_nan=False) + "\n")
                deadline += self.interval
                now = time.monotonic()
                if deadline < now:
                    deadline = now + self.interval
                self._stop_event.wait(deadline - now)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop_event.set()
        finally:
            try:
                self._stream.close()
            except Exception as exc:
                self.last_error = self.last_error or f"{type(exc).__name__}: {exc}"
