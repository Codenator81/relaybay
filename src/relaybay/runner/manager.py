"""The turns that run now: one `claude -p` process tree per run, read in threads.

Threads, not asyncio subprocesses: the event loop the desktop program's server thread runs may
have no subprocess support on Windows, and threads allow the suspended start of `proctree`. One
thread reads stdout and hands parsed lines to the loop, one drains stderr into a small ring kept in
memory only (never stored: it may hold account details), and an asyncio task per turn waits for
the end and tells the sink. No database here: the service is the sink.
"""

import asyncio
import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from relaybay.runner import auth
from relaybay.runner.claude_cli import TurnSpec, build_command, child_env, find_claude
from relaybay.runner.proctree import ProcessTree
from relaybay.runner.stream import LINE_MAX, Init, Result, parse_line

logger = logging.getLogger("relaybay")
STDERR_KEEP = 4096
STOP_GRACE_SECONDS = 10.0
CLOSE_GRACE_SECONDS = 1.5  # within uvicorn's graceful window of 2 s
LOGIN_CACHE_SECONDS = 30.0
DRAIN_SECONDS = 2.0
_END = object()


@dataclass(slots=True)
class RunnerConfig:
    home: Path
    base_url: str
    token_path: Path
    claude_path: Path | None = None
    claude_home: Path | None = None


class TurnSink(Protocol):
    async def turn_init(self, turn_id: int, claude_session_id: str) -> None: ...

    async def turn_done(
        self,
        turn_id: int,
        outcome: str,
        result: Result | None,
        exit_code: int | None,
        reason: str | None,
        stopped: bool,
    ) -> None: ...


@dataclass(slots=True)
class _Active:
    run_id: int
    turn_id: int
    tree: ProcessTree
    stop_reason: str | None = None
    stderr: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


def _read_lines(stream: Any, put: Callable[[Any], None]) -> None:
    """Lines longer than LINE_MAX are skipped whole without being held in memory."""
    skipping = False
    while True:
        chunk = stream.readline(LINE_MAX + 1)
        if not chunk:
            break
        if skipping or len(chunk) > LINE_MAX:
            skipping = not chunk.endswith("\n")
            continue
        item = parse_line(chunk)
        if item is not None:
            put(item)
    put(_END)


def _drain(stream: Any, ring: list[str]) -> None:
    for line in stream:
        ring.append(line)
        while sum(len(x) for x in ring) > STDERR_KEEP and len(ring) > 1:
            ring.pop(0)


def _write_stdin(stream: Any, text: str) -> None:
    try:
        stream.write(text)
    except OSError as exc:  # the process went away first; its exit is reported by the turn
        logger.debug("Writing the prompt to claude's stdin failed: %s", exc)
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def _report_failure(task: asyncio.Task[None]) -> None:
    """A turn whose end could not be stored stays `running` on the card: say so in the log."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("Recording the end of a run's turn failed", exc_info=task.exception())


class RunManager:
    def __init__(self, sink: TurnSink, config: RunnerConfig) -> None:
        self.sink = sink
        self.config = config
        self._active: dict[int, _Active] = {}
        self._starting: dict[int, asyncio.Event] = {}  # spawning now: not in _active yet
        self._login: tuple[float, auth.LoginState] | None = None
        self._login_lock = asyncio.Lock()
        self.closing = False  # the server stops: no new turn starts
        # Runs whose sign-in was checked since this server started; a restart checks again.
        self.checked_since_start: set[int] = set()

    def claude(self) -> str | None:
        return find_claude(self.config.claude_path)

    def is_running(self, run_id: int) -> bool:
        return run_id in self._active or run_id in self._starting

    async def login(self, force: bool = False) -> auth.LoginState:
        """The sign-in, cached for the Start form; Start always asks again (`force`)."""
        asked = time.monotonic()
        async with self._login_lock:  # one `claude auth status` at a time
            cached = self._login
            # Asked while this call waited (Start), or fresh enough for the form.
            fresh = cached is not None and (
                cached[0] > asked if force else asked - cached[0] < LOGIN_CACHE_SECONDS
            )
            if cached is not None and fresh:
                return cached[1]
            claude = self.claude()
            if claude is None:
                state = auth.LoginState(False, None, None, None, readable=False, error="no claude")
            else:
                state = await asyncio.to_thread(auth.check_login, claude)
            self._login = (time.monotonic(), state)
            return state

    def reserve(self, run_id: int) -> bool:
        """Hold the run's slot from the moment its turn is marked started in the database, so a
        Stop or End before the process exists waits for it instead of seeing no process. Called
        with no await between the commit and it. False: closing, or a turn already holds it."""
        if self.closing or self.is_running(run_id):
            return False
        self._starting[run_id] = asyncio.Event()
        return True

    def release(self, run_id: int) -> None:
        """Give back a reservation that did not become a process."""
        event = self._starting.pop(run_id, None)
        if event is not None:
            event.set()

    async def start_turn(
        self, run_id: int, turn_id: int, spec: TurnSpec, *, reserved: bool = False
    ) -> int:
        """Spawn the turn and return its pid; the sink hears the session id and the end.
        `reserved`: the caller holds the run's slot from `reserve`; it is used up here."""
        if reserved and run_id not in self._starting:
            raise RuntimeError(f"run {run_id} has no reservation")
        if not reserved and self.is_running(run_id):
            raise RuntimeError(f"run {run_id} already has a turn running")
        if not reserved:
            self._starting[run_id] = asyncio.Event()
        spawned = self._starting[run_id]
        try:
            if self.closing:
                raise RuntimeError("the server is stopping")
            argv, stdin_text = build_command(spec)
            tree = ProcessTree()
            env = child_env(os.environ, run_id)
            proc = await asyncio.to_thread(tree.spawn, argv, spec.cwd, env, stdin_text)
        finally:
            del self._starting[run_id]
            spawned.set()
        active = _Active(run_id, turn_id, tree)
        self._active[run_id] = active
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def put(item: Any) -> None:
            # A child left running can keep the pipe open past the turn and the server: lines
            # after that have nowhere to go.
            if not loop.is_closed():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, item)

        threading.Thread(target=_read_lines, args=(proc.stdout, put), daemon=True).start()
        threading.Thread(target=_drain, args=(proc.stderr, active.stderr), daemon=True).start()
        if stdin_text is not None:
            threading.Thread(
                target=_write_stdin, args=(proc.stdin, stdin_text), daemon=True
            ).start()
        active.task = asyncio.create_task(self._follow(active, proc, queue))
        active.task.add_done_callback(_report_failure)
        return proc.pid

    async def _follow(
        self, active: _Active, proc: subprocess.Popen[str], queue: asyncio.Queue[Any]
    ) -> None:
        result: Result | None = None
        code: int | None = None
        waiter = asyncio.ensure_future(asyncio.to_thread(proc.wait))

        async def next_item() -> Any:
            # A child left running may hold stdout open after claude exited: once the process
            # is gone, the lines still in the pipe get DRAIN_SECONDS, then the turn ends.
            while not waiter.done():
                getter = asyncio.ensure_future(queue.get())
                await asyncio.wait({getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    return getter.result()
                getter.cancel()
            try:
                return await asyncio.wait_for(queue.get(), DRAIN_SECONDS)
            except TimeoutError:
                return _END

        try:
            while (item := await next_item()) is not _END:
                if isinstance(item, Init):
                    try:
                        await self.sink.turn_init(active.turn_id, item.session_id)
                    except Exception:
                        # The turn goes on and still ends on the card; the next one then starts
                        # without --resume.
                        logger.exception("Storing run %d's session id failed", active.run_id)
                elif isinstance(item, Result):
                    result = item
            code = await waiter
        finally:
            active.tree.close()
            self._active.pop(active.run_id, None)
        stopped = active.stop_reason is not None
        if result is not None:
            # Stop after the result (SessionEnd hooks run then): the turn's outcome stands, the
            # sink stops the run.
            outcome, reason = ("error" if result.is_error else "success"), active.stop_reason
        elif stopped:
            outcome, reason = "stopped", active.stop_reason
        else:
            outcome = "lost"
            reason = f"claude exited with code {code} without a result"
            logger.debug("Run %d turn ended without a result: %s", active.run_id, active.stderr)
        await self.sink.turn_done(active.turn_id, outcome, result, code, reason, stopped)

    async def stop(self, run_id: int, reason: str, grace: float = STOP_GRACE_SECONDS) -> bool:
        """Kill the run's turn and wait until the sink heard its end. False: none was running."""
        spawning = self._starting.get(run_id)
        if spawning is not None:  # the spawn finishes first, then its tree is killed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(spawning.wait(), grace)
        active = self._active.get(run_id)
        if active is None:
            return False
        active.stop_reason = reason
        active.tree.terminate()
        task = active.task
        assert task is not None
        await asyncio.wait({task}, timeout=grace)
        # Past the grace time, and on POSIX the group's leftovers whatever the leader did.
        active.tree.kill()
        await asyncio.shield(task)
        return True

    async def close(self) -> None:
        """Server stop: every running turn is killed and ends `stopped`; none starts after."""
        self.closing = True
        await asyncio.gather(
            *(
                self.stop(run_id, "server stopped", grace=CLOSE_GRACE_SECONDS)
                for run_id in set(self._active) | set(self._starting)
            )
        )
