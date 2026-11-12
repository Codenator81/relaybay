"""The turns that run now: one `claude -p` process tree per run, read in threads.

Threads, not asyncio subprocesses: the event loop the desktop program's server thread runs may
have no subprocess support on Windows, and threads allow the suspended start of `proctree`. One
thread reads stdout, masks and compresses every line and hands it to the loop, one drains stderr
into a small ring kept in memory only (never stored: it may hold account details), and an asyncio
task per turn hands the lines to the sink in batches, waits for the end and tells the sink. No
database here: the service is the sink.
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
from relaybay.runner.desktop import Launcher, default_launcher
from relaybay.runner.proctree import ProcessTree
from relaybay.runner.stream import (
    HEAD_MAX,
    LINE_MAX,
    Init,
    Result,
    StreamEvent,
    read_line,
    truncated_event,
)

logger = logging.getLogger("relaybay")
STDERR_KEEP = 4096
STOP_GRACE_SECONDS = 10.0
CLOSE_GRACE_SECONDS = 1.5  # within uvicorn's graceful window of 2 s
LOGIN_CACHE_SECONDS = 30.0
DRAIN_SECONDS = 2.0
# Lines go to the sink in batches: at this many, or this long after the first one waiting.
EVENT_BATCH = 200
EVENT_FLUSH_SECONDS = 0.5
_END = object()
_TICK = object()


@dataclass(slots=True)
class RunnerConfig:
    home: Path
    base_url: str
    token_path: Path
    claude_path: Path | None = None
    claude_home: Path | None = None


class TurnSink(Protocol):
    async def turn_init(self, turn_id: int, claude_session_id: str) -> None: ...

    async def turn_events(self, turn_id: int, events: list[StreamEvent]) -> None: ...

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
    """Every line becomes a masked event (masked and compressed here, off the loop), followed by
    its item when it steers the run. A line longer than LINE_MAX is never held whole: its head
    is kept and the rest only counted."""
    seq = 0
    head: str | None = None  # the start of a line over LINE_MAX being skipped
    size = 0
    while True:
        chunk = stream.readline(LINE_MAX + 1)
        if not chunk:
            break
        if head is None and len(chunk) <= LINE_MAX:
            event, item = read_line(chunk, seq + 1)
            if event is not None:
                seq += 1
                put(event)
            if item is not None:
                put(item)
            continue
        if head is None:
            head, size = chunk[:HEAD_MAX], 0
        size += len(chunk)
        if chunk.endswith("\n"):
            seq += 1
            put(truncated_event(head, size, seq))
            head = None
    if head is not None:
        put(truncated_event(head, size, seq + 1))
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
        # `claude --desktop` in a console of its own; None where relaybay cannot open one.
        # Tests put their own here, so no console opens.
        self.desktop_launcher: Launcher | None = default_launcher()

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
        batch: list[StreamEvent] = []
        due = 0.0  # when the lines waiting in `batch` go to the sink

        async def next_item(due_in: float | None) -> Any:
            # A child left running may hold stdout open after claude exited: once the process
            # is gone, the lines still in the pipe get DRAIN_SECONDS, then the turn ends.
            # `due_in`: until the waiting lines are due, then _TICK.
            while not waiter.done():
                getter = asyncio.ensure_future(queue.get())
                await asyncio.wait(
                    {getter, waiter}, timeout=due_in, return_when=asyncio.FIRST_COMPLETED
                )
                if getter.done():
                    return getter.result()
                getter.cancel()
                if not waiter.done():
                    return _TICK
            wait = DRAIN_SECONDS if due_in is None else min(due_in, DRAIN_SECONDS)
            try:
                return await asyncio.wait_for(queue.get(), wait)
            except TimeoutError:
                return _END if due_in is None else _TICK

        async def flush() -> None:
            if not batch:
                return
            lines = batch[:]
            batch.clear()
            try:
                await self.sink.turn_events(active.turn_id, lines)
            except Exception:
                # The turn goes on and still ends on the card, without these lines.
                logger.exception(
                    "Storing %d line(s) of run %d's turn failed", len(lines), active.run_id
                )

        try:
            while True:
                item = await next_item(max(0.0, due - time.monotonic()) if batch else None)
                if item is _TICK:
                    await flush()
                    continue
                if item is _END:
                    break
                if isinstance(item, StreamEvent):
                    if not batch:
                        due = time.monotonic() + EVENT_FLUSH_SECONDS
                    batch.append(item)
                    if len(batch) >= EVENT_BATCH:
                        await flush()
                elif isinstance(item, Init):
                    try:
                        await self.sink.turn_init(active.turn_id, item.session_id)
                    except Exception:
                        # The turn goes on and still ends on the card; the next one then starts
                        # without --resume.
                        logger.exception("Storing run %d's session id failed", active.run_id)
                elif isinstance(item, Result):
                    result = item
            await flush()
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
