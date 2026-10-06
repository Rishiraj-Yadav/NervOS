"""Windows Job Object containment for package-host child processes (ADR 0035).

Uses ``ctypes`` against ``kernel32`` directly — no new dependency. A nested Job Object
(restricted to the frozen limits) is assigned to the package-host PID; every descendant
it spawns joins the same Job, so process-count and memory caps cover the whole tree.

The Job is created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so a Worker crash cannot
orphan an uncontained package: the kernel kills the tree when the handle dies.

Honest limits (documented in ADR 0035): a Windows Job Object cannot deny the child read
access to arbitrary same-user files. Filesystem read protection on Windows is therefore
**partial** — the threat model discloses this and nothing here claims otherwise.
"""

from __future__ import annotations

import ctypes
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path

from nervos_core.application.sandbox import (
    CPU_TIME_SECONDS,
    MAX_ACTIVE_PROCESSES,
    MEMORY_LIMIT_BYTES,
    ContainmentResult,
    ContainmentTier,
    ContainmentUnavailable,
    SandboxLaunch,
)

_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_UILIMIT_HANDLES = 0x00000001
_JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x00000002
_JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x00000004
_JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x00000008
_JOB_OBJECT_UILIMIT_DESKTOP = 0x00000010
_JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x00000020
_JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x00000040
_JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x00000080

# Access rights on the job handle.
_JOB_ASSIGN_PROCESS = 0x0001
_JOB_SET_INFORMATION = 0x0007
_JOB_QUERY_INFORMATION = 0x0004

# `JOBOBJECTINFOCLASS` values. These are *not* the access rights above: SetInformationJobObject
# and QueryInformationJobObject take a class selector in that argument.
_JOB_OBJECT_BASIC_LIMIT_INFORMATION = 2
_JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_handle_close = _kernel32.CloseHandle
_handle_close.argtypes = [wintypes.HANDLE]
_handle_close.restype = wintypes.BOOL

_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
]
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.QueryInformationJobObject.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryInformationJobObject.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateJobObject.restype = wintypes.BOOL

_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_UILIMIT_ALL = (
    _JOB_OBJECT_UILIMIT_HANDLES
    | _JOB_OBJECT_UILIMIT_READCLIPBOARD
    | _JOB_OBJECT_UILIMIT_WRITECLIPBOARD
    | _JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
    | _JOB_OBJECT_UILIMIT_DESKTOP
    | _JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
    | _JOB_OBJECT_UILIMIT_GLOBALATOMS
    | _JOB_OBJECT_UILIMIT_EXITWINDOWS
)
# 100ns ticks of job-wide user CPU time.
_CPU_TIME_100NS = CPU_TIME_SECONDS * 10_000_000


class WindowsJobObjectContainment:
    """Assign one child to a frozen nested Job Object and keep the handle alive."""

    def __init__(self) -> None:
        self._handles: dict[int, ctypes.c_void_p] = {}

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: Mapping[str, str],
        arguments: tuple[str, ...],
    ) -> SandboxLaunch:
        return SandboxLaunch(
            command=(str(python), *arguments), cwd=scratch, environment=environment
        )

    def establish(self, process_id: int) -> ContainmentResult:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ContainmentUnavailable

        limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_TIME
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limits.ProcessMemoryLimit = MEMORY_LIMIT_BYTES
        limits.JobMemoryLimit = MEMORY_LIMIT_BYTES
        limits.BasicLimitInformation.PerJobUserTimeLimit = _CPU_TIME_100NS
        limits.BasicLimitInformation.ActiveProcessLimit = MAX_ACTIVE_PROCESSES
        if not _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            _handle_close(job)
            raise ContainmentUnavailable

        ui = _JOBOBJECT_BASIC_UI_RESTRICTIONS(_UILIMIT_ALL)
        if not _kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)
        ):
            _handle_close(job)
            raise ContainmentUnavailable

        process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process_id)
        if not process:
            _handle_close(job)
            raise ContainmentUnavailable
        try:
            if not _kernel32.AssignProcessToJobObject(job, process):
                raise ContainmentUnavailable
        finally:
            _handle_close(process)

        self._handles[process_id] = job
        return ContainmentResult(
            tier=ContainmentTier.RESOURCE,
            platform="windows",
            details=(
                f"job-object memory={MEMORY_LIMIT_BYTES} processes={MAX_ACTIVE_PROCESSES} "
                f"cpu_seconds={CPU_TIME_SECONDS} ui-restricted kill-on-close"
            ),
        )

    def verify(self, process_id: int) -> None:
        job = self._handles.get(process_id)
        if job is None:
            raise ContainmentUnavailable
        returned = wintypes.DWORD(0)
        limits = _JOBOBJECT_BASIC_LIMIT_INFORMATION()
        ok = _kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
            ctypes.byref(returned),
        )
        if not ok:
            raise ContainmentUnavailable
        flags = limits.LimitFlags
        if not flags & _JOB_OBJECT_LIMIT_PROCESS_MEMORY:
            raise ContainmentUnavailable
        if limits.ActiveProcessLimit != MAX_ACTIVE_PROCESSES:
            raise ContainmentUnavailable

    def terminate(self, process_id: int) -> None:
        job = self._handles.pop(process_id, None)
        if job is not None:
            _kernel32.TerminateJobObject(job, 1)
            _handle_close(job)

    def release(self, process_id: int) -> None:
        """Close the handle without terminating: the process has already exited."""
        job = self._handles.pop(process_id, None)
        if job is not None:
            _handle_close(job)
