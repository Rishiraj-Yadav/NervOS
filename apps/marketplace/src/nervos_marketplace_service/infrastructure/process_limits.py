"""OS-enforced static parser limits. This is not an agent execution sandbox."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_job: object | None = None


def apply_limits(memory_bytes: int, cpu_seconds: int) -> None:
    if memory_bytes < 64 * 1024 * 1024 or cpu_seconds < 1:
        raise ValueError("Invalid verifier limits")
    if os.name != "nt":
        import resource
        import sys

        # Linux ignores RLIMIT_NPROC for root and privileged identities. A verifier
        # that cannot enforce the no-descendant contract must fail closed.
        if sys.platform != "linux" or os.geteuid() == 0:
            raise RuntimeError("Static verifier requires an unprivileged Linux identity")

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
        return
    _windows_limits(memory_bytes, cpu_seconds)


def _windows_limits(memory_bytes: int, cpu_seconds: int) -> None:
    # Structures mirror the public Win32 JOBOBJECT_EXTENDED_LIMIT_INFORMATION ABI.
    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", IO),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x200 | 0x8 | 0x2 | 0x2000
    limits.basic.processes = 1
    limits.basic.process_time = cpu_seconds * 10_000_000
    limits.process_memory = memory_bytes
    limits.job_memory = memory_bytes
    if (
        not job
        or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
    ):
        if job:
            kernel.CloseHandle(job)
        raise RuntimeError("Windows verifier containment unavailable")
    global _job
    _job = job  # Retain until process exit: KILL_ON_JOB_CLOSE contains descendants.


def usage_snapshot() -> dict[str, int | float]:
    if os.name != "nt":
        import resource
        import sys

        value = resource.getrusage(resource.RUSAGE_SELF)
        result: dict[str, int | float] = {
            "peak_rss_bytes": int(value.ru_maxrss * (1 if sys.platform == "darwin" else 1024)),
            "cpu_seconds": value.ru_utime + value.ru_stime,
            "user_cpu_seconds": value.ru_utime,
            "system_cpu_seconds": value.ru_stime,
        }
        if sys.platform == "linux":
            from pathlib import Path

            for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmPeak:"):
                    result["peak_virtual_bytes"] = int(line.split()[1]) * 1024
                elif line.startswith("VmHWM:"):
                    # ru_maxrss can retain a larger parent's pre-exec watermark.
                    # /proc's VmHWM describes this executable's resident peak.
                    result["peak_rss_bytes"] = int(line.split()[1]) * 1024
        return result

    class Counters(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            )
        ]

    counters = Counters()
    counters.size = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.size
    ):
        raise RuntimeError("Verifier resource measurement unavailable")
    value = os.times()
    return {
        "peak_rss_bytes": int(counters.peak_working_set),
        # Windows Job Object memory limits govern committed memory, which can be
        # higher than working set when the OS pages memory out. Measure both.
        "peak_commit_bytes": int(counters.peak_pagefile),
        "cpu_seconds": value.user + value.system,
        "user_cpu_seconds": value.user,
        "system_cpu_seconds": value.system,
    }


def usage() -> tuple[int, float]:
    snapshot = usage_snapshot()
    return int(snapshot["peak_rss_bytes"]), float(snapshot["cpu_seconds"])
