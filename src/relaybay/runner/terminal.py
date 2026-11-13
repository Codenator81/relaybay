"""The Terminal tab of a run: `claude --resume <id>` in a pseudo-terminal, streamed to the board.

Windows: a ConPTY through pywinpty. macOS and Linux: the standard library's `pty` with a
subprocess on the slave side that takes it as its controlling terminal, so the TUI gets SIGHUP
when the master closes, the server's crash included (not probed there). A PTY's read blocks, so
each terminal reads in a thread of its own and hands the chunks to the loop; writes go through a
second thread in the order they came. The process goes into a `ProcessTree` (a job object, or its
own process group), so it dies with the tab, the run's End or the server.
"""

import asyncio
import codecs
import contextlib
import functools
import importlib.util
import logging
import os
import queue
import secrets
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from relaybay.domain.errors import ValidationError
from relaybay.runner.claude_cli import UNSAFE_BATCH_CHARS, child_env, is_batch
from relaybay.runner.proctree import ProcessTree

logger = logging.getLogger("relaybay")

TOKEN_TTL_SECONDS = 30.0
READ_SIZE = 65536
KILL_GRACE_SECONDS = 2.0
ROWS_MAX = 500
COLS_MAX = 1000
_STOP = object()


class Pty(Protocol):
    """One process in a pseudo-terminal. `read` blocks and raises EOFError at the end; `kill`
    ends the process and every child of it; `close` frees the PTY once nothing reads it."""

    pid: int

    def read(self) -> str: ...

    def write(self, text: str) -> None: ...

    def resize(self, rows: int, cols: int) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...

    def exit_code(self) -> int | None: ...


class PtyBackend(Protocol):
    def spawn(
        self, argv: list[str], cwd: str, env: Mapping[str, str], rows: int, cols: int
    ) -> Pty: ...


class _WinPty:
    def __init__(self, proc: Any, tree: ProcessTree) -> None:
        self._proc = proc
        self._tree = tree
        self.pid: int = proc.pid

    def read(self) -> str:
        try:
            return self._proc.read(READ_SIZE)
        except OSError as exc:  # the socket pywinpty reads through was closed by `close`
            raise EOFError(str(exc)) from exc

    def write(self, text: str) -> None:
        self._proc.write(text)

    def resize(self, rows: int, cols: int) -> None:
        self._proc.setwinsize(rows, cols)

    def kill(self) -> None:
        self._tree.terminate()

    def close(self) -> None:
        self._proc.close(force=True)
        self._tree.close()

    def exit_code(self) -> int | None:
        return self._proc.exitstatus


class WinPtyBackend:
    """ConPTY through pywinpty (`PtyProcess.spawn(argv, cwd, env, dimensions=(rows, cols))`)."""

    def spawn(self, argv: list[str], cwd: str, env: Mapping[str, str], rows: int, cols: int) -> Pty:
        from winpty import PtyProcess

        proc = PtyProcess.spawn(argv, cwd=cwd, env=dict(env), dimensions=(rows, cols))
        tree = ProcessTree()
        try:
            tree.adopt(proc.pid)
        except BaseException:
            proc.close(force=True)
            raise
        return _WinPty(proc, tree)


class _PosixPty:
    def __init__(self, proc: subprocess.Popen[bytes], master: int, tree: ProcessTree) -> None:
        self._proc = proc
        self._master: int | None = master
        self._tree = tree
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.pid = proc.pid

    def read(self) -> str:
        assert self._master is not None
        try:
            data = os.read(self._master, READ_SIZE)
        except OSError as exc:  # EIO once the slave side has no process left (Linux)
            raise EOFError(str(exc)) from exc
        if not data:
            raise EOFError("the terminal closed")
        return self._decoder.decode(data)

    def write(self, text: str) -> None:
        assert self._master is not None
        data = text.encode("utf-8")
        while data:
            data = data[os.write(self._master, data) :]

    def resize(self, rows: int, cols: int) -> None:
        import fcntl
        import termios

        assert self._master is not None
        # The kernel sends SIGWINCH to the foreground group of the controlling terminal.
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def kill(self) -> None:
        self._tree.terminate()
        # Past the grace time SIGKILL follows, for the leader and whatever is left in its group.
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(KILL_GRACE_SECONDS)
        self._tree.kill()
        self._proc.wait()

    def close(self) -> None:
        if self._master is not None:
            os.close(self._master)
            self._master = None

    def exit_code(self) -> int | None:
        return self._proc.poll()


class PosixPtyBackend:
    """The standard library's `pty.openpty` and a subprocess in a session of its own."""

    def spawn(self, argv: list[str], cwd: str, env: Mapping[str, str], rows: int, cols: int) -> Pty:
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
                # In the child after setsid: the slave (fd 0) becomes its controlling terminal.
                # A bound builtin, so nothing is imported between fork and exec.
                preexec_fn=functools.partial(fcntl.ioctl, 0, termios.TIOCSCTTY, 0),
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        tree = ProcessTree()
        tree.adopt(proc.pid)
        return _PosixPty(proc, master, tree)


def default_backend(platform: str = sys.platform) -> PtyBackend | None:
    """None on Windows without pywinpty (a source install that skipped it)."""
    if platform == "win32":
        return WinPtyBackend() if importlib.util.find_spec("winpty") is not None else None
    return PosixPtyBackend()


SELF_TEST_SECONDS = 15.0


def self_test(backend: PtyBackend | None, platform: str = sys.platform) -> str:
    """Start `echo ok` in a pseudo-terminal and read it back (doctor, the frozen build's CI
    smoke): raises with the reason when the PTY cannot run a program here."""
    if backend is None:
        raise RuntimeError("no pseudo-terminal: on Windows the pywinpty package is missing")
    if platform == "win32":
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", "echo relaybay-pty-ok"]
    else:
        argv = ["/bin/sh", "-c", "echo relaybay-pty-ok"]
    pty = backend.spawn(argv, str(Path.cwd()), os.environ, 24, 80)
    seen = ""
    done = threading.Event()

    def read() -> None:
        nonlocal seen
        try:
            while "relaybay-pty-ok" not in seen:
                seen += pty.read()
        except EOFError:
            pass  # the program ended; what it wrote is in `seen`
        finally:
            done.set()

    threading.Thread(target=read, daemon=True).start()
    try:
        done.wait(SELF_TEST_SECONDS)
    finally:
        pty.kill()
        pty.close()
    if "relaybay-pty-ok" not in seen:
        raise RuntimeError(f"the program in the PTY wrote {seen[-200:]!r}, not its echo")
    return f"{type(backend).__name__}: a program ran and was read back"


def terminal_command(claude: str, claude_session_id: str, add_dirs: tuple[str, ...]) -> list[str]:
    """`claude --resume <id>` with an `--add-dir` for every folder of the run besides the cwd, as
    its turns have. A `claude.cmd` shim runs through cmd.exe, which parses the line once more:
    a word with a character it would act on is refused, as for a turn."""
    argv = [claude, "--resume", claude_session_id]
    for folder in add_dirs:
        argv += ["--add-dir", folder]
    if is_batch(claude):
        for word in argv:
            bad = sorted(set(word) & UNSAFE_BATCH_CHARS)
            if bad:
                shown = " ".join(repr(c) for c in bad)
                raise ValidationError(
                    f"{word[:80]!r} holds {shown}, which a claude.cmd shim would let cmd.exe read"
                    " as a command; set claude_path to claude.exe or change it."
                )
    return argv


def terminal_env(
    base: Mapping[str, str], run_id: int, platform: str = sys.platform
) -> dict[str, str]:
    """A turn's environment (no host session markers, the run's id); on macOS and Linux also a
    TERM, without which a PTY counts as a dumb terminal."""
    env = child_env(base, run_id)
    if platform != "win32":
        env.setdefault("TERM", "xterm-256color")
    return env


def clean_size(rows: Any, cols: Any) -> tuple[int, int]:
    """The browser's terminal size, within bounds (a 2x1 PTY comes from a fit before layout)."""
    try:
        r, c = int(rows), int(cols)
    except (TypeError, ValueError) as exc:
        raise ValidationError("The terminal size must be whole numbers.") from exc
    return max(2, min(r, ROWS_MAX)), max(10, min(c, COLS_MAX))


class Terminal:
    """One open terminal: the reader and the writer threads, and the output queue for the loop.
    `output()` gives each chunk, then None once the process ended."""

    def __init__(self, run_id: int, pty: Pty, loop: asyncio.AbstractEventLoop) -> None:
        self.run_id = run_id
        self.pty = pty
        self.opened_at = time.time()
        self.closing: asyncio.Future[int | None] | None = None  # the kill under way
        self._loop = loop
        self._out: asyncio.Queue[str | None] = asyncio.Queue()
        self._in: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._killed = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._reader.start()
        self._writer.start()

    def _put(self, item: str | None) -> None:
        if not self._loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile: nobody listens
                self._loop.call_soon_threadsafe(self._out.put_nowait, item)

    def _read(self) -> None:
        try:
            while True:
                chunk = self.pty.read()
                if chunk:
                    self._put(chunk)
        except EOFError:
            pass  # the process ended: the None below says so
        except Exception:
            logger.exception("Reading the terminal of run %d failed", self.run_id)
        finally:
            self._put(None)

    def _write(self) -> None:
        while True:
            item = self._in.get()
            if item is _STOP:
                return
            try:
                if isinstance(item, tuple):
                    self.pty.resize(*item)
                else:
                    self.pty.write(item)
            except EOFError:
                return  # the process is gone; the reader reports the end
            except Exception:
                logger.exception("Writing to the terminal of run %d failed", self.run_id)
                return

    async def output(self) -> str | None:
        return await self._out.get()

    def write(self, text: str) -> None:
        if text and not self._killed.is_set():
            self._in.put(text)

    def resize(self, rows: int, cols: int) -> None:
        if not self._killed.is_set():
            self._in.put((rows, cols))

    def _kill(self) -> int | None:
        self._killed.set()
        self._in.put(_STOP)
        try:
            self.pty.kill()
        finally:
            self._reader.join(KILL_GRACE_SECONDS)
            self.pty.close()
        return self.pty.exit_code()

    async def kill(self) -> int | None:
        """End the process tree (blocking parts in a thread) and free the PTY; its exit code."""
        return await asyncio.to_thread(self._kill)


class TerminalManager:
    """The open terminals, one per run, and the one-time tokens that open them. A token is
    minted by a board-only POST, names one run, lives TOKEN_TTL_SECONDS and is spent by its
    first use, valid or not."""

    def __init__(
        self, backend: PtyBackend | None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.backend = backend
        self._clock = clock
        self._open: dict[int, Terminal] = {}
        self._opening: set[int] = set()
        self._tokens: dict[str, tuple[int, float]] = {}

    def busy(self, run_id: int) -> bool:
        return run_id in self._open or run_id in self._opening

    def open_runs(self) -> set[int]:
        return set(self._open) | self._opening

    def get(self, run_id: int) -> Terminal | None:
        return self._open.get(run_id)

    def mint(self, run_id: int) -> str:
        now = self._clock()
        self._tokens = {t: v for t, v in self._tokens.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (run_id, now + TOKEN_TTL_SECONDS)
        return token

    def take(self, token: str, run_id: int) -> bool:
        entry = self._tokens.pop(token, None)
        return entry is not None and entry[0] == run_id and entry[1] > self._clock()

    def reserve(self, run_id: int) -> bool:
        """Hold the run's terminal slot from before the run is marked with the owner; False when
        one is open or opening."""
        if self.busy(run_id):
            return False
        self._opening.add(run_id)
        return True

    def release(self, run_id: int) -> None:
        self._opening.discard(run_id)

    async def spawn(
        self, run_id: int, argv: list[str], cwd: str, env: Mapping[str, str], rows: int, cols: int
    ) -> Terminal:
        """Start the process for a reserved run; the reservation becomes the open terminal."""
        if run_id not in self._opening:
            raise RuntimeError(f"run {run_id} has no terminal reservation")
        if self.backend is None:
            self._opening.discard(run_id)
            raise RuntimeError("no pseudo-terminal on this machine")
        try:
            pty = await asyncio.to_thread(self.backend.spawn, argv, cwd, env, rows, cols)
        finally:
            self._opening.discard(run_id)
        terminal = Terminal(run_id, pty, asyncio.get_running_loop())
        self._open[run_id] = terminal
        return terminal

    async def close(self, run_id: int) -> tuple[Terminal, int | None] | None:
        """Kill the run's terminal; None when none was open or another call is closing it (that
        call reports the end). A kill that fails leaves it open, so the run stays held and the
        queue keeps skipping it; a later close tries again."""
        terminal = self._open.get(run_id)
        if terminal is None:
            return None
        if terminal.closing is not None:
            await asyncio.shield(terminal.closing)
            return None
        terminal.closing = asyncio.ensure_future(terminal.kill())
        try:
            code = await asyncio.shield(terminal.closing)
        except Exception:
            terminal.closing = None
            raise
        self._open.pop(run_id, None)
        return terminal, code
