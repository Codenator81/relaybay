"""`desktop-start.log` in the config folder: one line when the program starts, one for how the
start ended and one on exit, written before logging is set up.

A windowed program has no console: a start that ends before the log file is configured (a second
instance, a held lock, a missing extra, an exception) would leave no trace anywhere else. Standard
library only, and a failure to write never stops the start: it is kept in `error` and logged once
the log file exists.

Line format: `<local ISO time> [<pid>] <message>`; a traceback follows its `exception:` line,
each of its lines indented by two spaces.
"""

import os
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FILE_NAME = "desktop-start.log"
MAX_BYTES = 64 * 1024
KEEP_BYTES = 32 * 1024  # what is left after a trim: the newest lines
START = "start "
LINE = re.compile(r"^(\S+) \[(\d+)\] (.*)$")


def journal_path(folder: Path) -> Path:
    return folder / FILE_NAME


class StartJournal:
    """Appends to the journal in the folder `folder()` returns; the folder is resolved inside the
    guard too, so even a failing config folder lookup never stops the start."""

    def __init__(self, folder: Callable[[], Path]) -> None:
        self._folder = folder
        self._path: Path | None = None
        self.error: str | None = None

    @property
    def path(self) -> Path | None:
        if self._path is None and self.error is None:
            try:
                self._path = journal_path(self._folder())
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
        return self._path

    def write(self, message: str, detail: str = "") -> None:
        path = self.path
        if path is None:
            return
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        text = f"{stamp} [{os.getpid()}] {message}\n"
        if detail:
            text += "".join(f"  {line}\n" for line in detail.rstrip("\n").splitlines())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _trim(path)
            with path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write(text)
        except OSError as exc:
            self.error = f"{path}: {exc}"

    def start(self, version: str) -> None:
        self.write(
            f"{START}relaybay {version} argv={sys.argv[1:]!r} cwd={_cwd()}"
            f" frozen={bool(getattr(sys, 'frozen', False))} executable={sys.executable}"
        )

    def exception(self, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(exc))
        # One line: the message's other lines are in the traceback below it.
        first = str(exc).splitlines()[0] if str(exc) else ""
        self.write(f"exception: {type(exc).__name__}: {first}", detail)

    def exit(self, code: int) -> None:
        self.write(f"exit {code}")


def _cwd() -> str:
    try:
        return str(Path.cwd())
    except OSError as exc:  # a deleted working folder
        return f"unknown ({exc})"


def _trim(path: Path) -> None:
    """Above MAX_BYTES keep only the newest KEEP_BYTES, cut at a line start."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= MAX_BYTES:
        return
    with path.open("rb") as fh:
        fh.seek(size - KEEP_BYTES)
        tail = fh.read()
    cut = tail.find(b"\n")
    path.write_bytes(tail[cut + 1 :] if cut >= 0 else b"")


@dataclass(frozen=True, slots=True)
class LastStart:
    """The newest start in the journal and the lines its process wrote after it."""

    time: str
    start: str  # the start line's message after `start `
    outcomes: tuple[str, ...]

    @property
    def failed(self) -> bool:
        """An exception, or no line at all after the start: the process ended (or was ended)
        before it got anywhere."""
        return not self.outcomes or any(line.startswith("exception:") for line in self.outcomes)


def last_start(text: str) -> LastStart | None:
    entries = [m.groups() for m in map(LINE.match, text.splitlines()) if m]
    for index in range(len(entries) - 1, -1, -1):
        stamp, pid, message = entries[index]
        if message.startswith(START):
            outcomes = tuple(m for _t, p, m in entries[index + 1 :] if p == pid)
            return LastStart(stamp, message[len(START) :], outcomes)
    return None
