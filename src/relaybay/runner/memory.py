"""Free memory of this machine, read with the platform's own interface (no psutil).

Windows: `GlobalMemoryStatusEx`, `ullAvailPhys`. Linux: `MemAvailable` of `/proc/meminfo`.
macOS: `vm_stat`, the pages free, speculative and inactive (the ones the system hands out without
paging) times its page size. Each reader returns megabytes, or None when this machine does not
answer; the caller then lets the turn start (fails open) and the log says why.
"""

import logging
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("relaybay")
MB = 1024 * 1024
VM_STAT = "/usr/bin/vm_stat"
VM_STAT_TIMEOUT_SECONDS = 5.0
_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_PAGES_RE = re.compile(r"^Pages (free|speculative|inactive):\s+(\d+)\.?\s*$", re.MULTILINE)


def windows_free_mb() -> int | None:
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        logger.warning("GlobalMemoryStatusEx failed (error %d)", ctypes.get_last_error())
        return None
    return int(status.ullAvailPhys // MB)


def parse_meminfo(text: str) -> int | None:
    """`MemAvailable` of a `/proc/meminfo` text in MB (the file counts in kB)."""
    match = re.search(r"^MemAvailable:\s+(\d+)\s+kB\s*$", text, re.MULTILINE)
    return int(match.group(1)) // 1024 if match else None


def linux_free_mb(path: Path = Path("/proc/meminfo")) -> int | None:
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError as exc:
        logger.warning("Reading %s failed: %s", path, exc)
        return None
    found = parse_meminfo(text)
    if found is None:
        logger.warning("%s has no MemAvailable line (a kernel before 3.14)", path)
    return found


def parse_vm_stat(text: str) -> int | None:
    """Free, speculative and inactive pages of a `vm_stat` text times its page size, in MB."""
    size = _PAGE_SIZE_RE.search(text)
    pages = {name: int(count) for name, count in _PAGES_RE.findall(text)}
    if size is None or "free" not in pages or "inactive" not in pages:
        return None
    return sum(pages.values()) * int(size.group(1)) // MB


def macos_free_mb(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> int | None:
    try:
        done = run(
            [VM_STAT],
            capture_output=True,
            text=True,
            timeout=VM_STAT_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("vm_stat did not answer: %s", exc)
        return None
    found = parse_vm_stat(done.stdout or "") if done.returncode == 0 else None
    if found is None:
        logger.warning("vm_stat gave no free page counts (exit %d)", done.returncode)
    return found


def free_mb() -> int | None:
    """This machine's free memory in MB, or None when it cannot be read here."""
    if sys.platform == "win32":
        return windows_free_mb()
    if sys.platform == "darwin":
        return macos_free_mb()
    if sys.platform.startswith("linux"):
        return linux_free_mb()
    return None
