from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from typing import Callable

from ctypes import wintypes


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    sampled_at: float
    system_ram_used_mb: float | None
    server_rss_mb: float | None
    gpu_vram_used_mb: float | None
    gpu_utilization_percent: float | None
    vts_online: bool | None = None


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    samples: int
    baseline_system_ram_used_mb: float | None
    peak_system_ram_used_mb: float | None
    system_ram_delta_mb: float | None
    baseline_gpu_vram_used_mb: float | None
    peak_gpu_vram_used_mb: float | None
    gpu_vram_delta_mb: float | None
    peak_server_rss_mb: float | None
    peak_gpu_utilization_percent: float | None
    vts_connectivity_samples: int
    vts_online_samples: int
    vts_online_throughout: bool | None


class ResourceSampler:
    def __init__(
        self,
        *,
        server_pid: int | None,
        interval_seconds: float = 0.5,
        snapshotter: Callable[[int | None], ResourceSnapshot] | None = None,
        vts_probe: Callable[[], bool] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Resource sample interval must be greater than zero")
        self.server_pid = server_pid
        self.interval_seconds = interval_seconds
        self.snapshotter = snapshotter or collect_resource_snapshot
        self.vts_probe = vts_probe
        self.snapshots: list[ResourceSnapshot] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ResourceSampler:
        self.snapshots.append(await asyncio.to_thread(self._take_snapshot))
        self._task = asyncio.create_task(self._sample_loop())
        return self

    async def __aexit__(self, *_: object) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
        self.snapshots.append(await asyncio.to_thread(self._take_snapshot))

    async def _sample_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.interval_seconds,
                )
                return
            except TimeoutError:
                self.snapshots.append(
                    await asyncio.to_thread(self._take_snapshot)
                )

    def _take_snapshot(self) -> ResourceSnapshot:
        snapshot = self.snapshotter(self.server_pid)
        if self.vts_probe is None:
            return snapshot
        return replace(snapshot, vts_online=self.vts_probe())

    def summary(self) -> ResourceSummary:
        if not self.snapshots:
            raise ValueError("No resource samples were collected")
        baseline = self.snapshots[0]
        peak_system = _maximum(
            snapshot.system_ram_used_mb for snapshot in self.snapshots
        )
        peak_gpu = _maximum(snapshot.gpu_vram_used_mb for snapshot in self.snapshots)
        vts_states = [
            snapshot.vts_online
            for snapshot in self.snapshots
            if snapshot.vts_online is not None
        ]
        return ResourceSummary(
            samples=len(self.snapshots),
            baseline_system_ram_used_mb=baseline.system_ram_used_mb,
            peak_system_ram_used_mb=peak_system,
            system_ram_delta_mb=_difference(
                peak_system,
                baseline.system_ram_used_mb,
            ),
            baseline_gpu_vram_used_mb=baseline.gpu_vram_used_mb,
            peak_gpu_vram_used_mb=peak_gpu,
            gpu_vram_delta_mb=_difference(
                peak_gpu,
                baseline.gpu_vram_used_mb,
            ),
            peak_server_rss_mb=_maximum(
                snapshot.server_rss_mb for snapshot in self.snapshots
            ),
            peak_gpu_utilization_percent=_maximum(
                snapshot.gpu_utilization_percent for snapshot in self.snapshots
            ),
            vts_connectivity_samples=len(vts_states),
            vts_online_samples=sum(vts_states),
            vts_online_throughout=all(vts_states) if vts_states else None,
        )


def collect_resource_snapshot(server_pid: int | None) -> ResourceSnapshot:
    gpu_memory, gpu_utilization = _query_nvidia_smi()
    return ResourceSnapshot(
        sampled_at=time.perf_counter(),
        system_ram_used_mb=_windows_system_ram_used_mb(),
        server_rss_mb=(
            _windows_process_rss_mb(server_pid) if server_pid is not None else None
        ),
        gpu_vram_used_mb=gpu_memory,
        gpu_utilization_percent=gpu_utilization,
    )


def _windows_system_ram_used_mb() -> float | None:
    if sys.platform != "win32":
        return None

    class MemoryStatusEx(ctypes.Structure):
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

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatusEx)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return (status.ullTotalPhys - status.ullAvailPhys) / (1024 * 1024)


def _windows_process_rss_mb(pid: int) -> float | None:
    if sys.platform != "win32":
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    process = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not process:
        return None
    try:
        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            return None
        return counters.WorkingSetSize / (1024 * 1024)
    finally:
        kernel32.CloseHandle(process)


def _query_nvidia_smi() -> tuple[float | None, float | None]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first_gpu = completed.stdout.splitlines()[0]
        memory, utilization = (part.strip() for part in first_gpu.split(",", 1))
        return float(memory), float(utilization)
    except (
        FileNotFoundError,
        IndexError,
        subprocess.SubprocessError,
        ValueError,
    ):
        return None, None


def _maximum(values: object) -> float | None:
    present = [value for value in values if isinstance(value, (int, float))]
    return max(present) if present else None


def _difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right
