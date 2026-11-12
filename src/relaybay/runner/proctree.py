"""One turn's process tree: started so that every child can be killed with it.

Windows: a job object with KILL_ON_JOB_CLOSE; the process starts suspended and is resumed only
after it is in the job, so a `claude.cmd`'s cmd.exe cannot start its child outside it. Closing the
job (turn end, or the server process dying) kills what is left in it. ctypes only, no dependency.

macOS and Linux: a new session, so the turn is its own process group; Stop sends SIGTERM to the
group, then SIGKILL. A grandchild that starts its own session escapes (documented risk).
"""

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Mapping

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
THREAD_SUSPEND_RESUME = 0x0002
TH32CS_SNAPTHREAD = 0x00000004


class ProcessTreeError(OSError):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
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

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _k32.Thread32First.restype = wintypes.BOOL
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _k32.Thread32Next.restype = wintypes.BOOL
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.restype = wintypes.DWORD

    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _fail(what: str) -> ProcessTreeError:
        code = ctypes.get_last_error()
        return ProcessTreeError(code, f"{what} failed: {ctypes.FormatError(code).strip()}")

    def _new_job() -> int:
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            raise _fail("CreateJobObjectW")
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = _fail("SetInformationJobObject")
            _k32.CloseHandle(job)
            raise error
        return job

    def _assign(job: int, pid: int) -> None:
        process = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise _fail("OpenProcess")
        try:
            if not _k32.AssignProcessToJobObject(job, process):
                raise _fail("AssignProcessToJobObject")
        finally:
            _k32.CloseHandle(process)

    def _resume(pid: int) -> None:
        """Resume the threads of a process started suspended (the documented Toolhelp path:
        Popen keeps no thread handle)."""
        snapshot = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            raise _fail("CreateToolhelp32Snapshot")
        resumed = 0
        try:
            entry = _ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = _k32.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == pid:
                    thread = _k32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        raise _fail("OpenThread")
                    try:
                        if _k32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise _fail("ResumeThread")
                    finally:
                        _k32.CloseHandle(thread)
                    resumed += 1
                more = _k32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snapshot)
        if not resumed:
            raise ProcessTreeError(0, f"no thread of process {pid} to resume")


class ProcessTree:
    """Spawn one process so that it and every child can be killed together."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self._job: int | None = None

    def spawn(
        self, argv: list[str], cwd: str, env: Mapping[str, str], stdin_text: str | None
    ) -> subprocess.Popen[str]:
        """Blocking: call it from a worker thread. stdin is a pipe only when there is text for
        it; the caller writes it (`write_stdin`) once the reader threads run."""
        options: dict = {
            "cwd": cwd,
            "env": dict(env),
            "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if sys.platform == "win32":
            self._job = _new_job()
            options["creationflags"] = CREATE_SUSPENDED | CREATE_NO_WINDOW
            try:
                proc = subprocess.Popen(argv, **options)
            except BaseException:
                self.close()
                raise
            try:
                _assign(self._job, proc.pid)
                _resume(proc.pid)
            except BaseException:
                proc.kill()
                proc.wait()
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
                self.close()
                raise
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **options)
        self.proc = proc
        return proc

    def terminate(self) -> None:
        """Windows: the whole job at once (no graceful stop exists for it). POSIX: SIGTERM to
        the group; `kill` follows after the grace time."""
        if sys.platform == "win32":
            if self._job is not None and not _k32.TerminateJobObject(self._job, 1):
                raise _fail("TerminateJobObject")
            return
        self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        if sys.platform == "win32":
            self.terminate()
            return
        self._signal_group(signal.SIGKILL)

    def _signal_group(self, sig: int) -> None:
        if self.proc is None:
            return
        # The group id is the leader's pid, reserved while any member lives; an empty group
        # (ProcessLookupError) has nothing left to stop.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, sig)

    def close(self) -> None:
        """Windows: closing the job kills whatever is still in it."""
        if sys.platform == "win32" and self._job is not None:
            _k32.CloseHandle(self._job)
        self._job = None
