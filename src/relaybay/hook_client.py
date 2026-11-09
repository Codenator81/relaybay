"""`relaybay hook`: forward one Claude Code hook event (JSON on stdin) to relaybay.

Used as a `command` hook where `http` hooks are not available. Standard library only, so it
starts fast. It must never get in the way of the session: short timeouts, no output on failure
and always exit code 0. The server's JSON answer (session context, owner comments, a Stop block)
goes to stdout.

With `--wait` it is the opt-in wake-up hook (`asyncRewake`, run in the background at Stop): it
waits for an owner comment for the idle session and exits 2 with the comment on stderr, which
wakes the session; it exits 0 silently in every other case.

Events that carry state (session start and end, a prompt, a notification, the end of a turn, a
todo tool's change and a session launch) get one retry with a longer timeout; when the server still
does not answer they go to a bounded spool in the relaybay config folder, and the next run that
reaches the server sends them first, oldest first. The guard (PreToolUse) and other tool calls get
one short try and are never spooled.
"""

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from relaybay.domain.hooks import (
    ATTENDED_ENV,
    ATTENDED_FIELD,
    HOST_SESSION_ENV,
    HOST_SESSION_FIELD,
    LAUNCH_TOOLS,
    SPOOLED_FIELD,
    WAKE_HOLD_SECONDS,
)

TIMEOUT_SECONDS = 0.3  # the one try of the guard and of an activity ping
STATE_TIMEOUT_SECONDS = 1.0  # each of the two tries of a state-bearing event
# Counted from the import of this module: Claude Code ends the command after 3 s
# (machine_setup.COMMAND_TIMEOUT), and the interpreter's start-up and the spool write come on top.
BUDGET_SECONDS = 2.0
SILENT_SECONDS = 0.5  # no answer for this long: the server is away, the event goes to the spool
RETRY_PAUSE_SECONDS = 0.2
LOCK_WAIT_SECONDS = 0.2  # the drain's reads and rewrites: skipped when another run is busy
APPEND_WAIT_SECONDS = 0.5  # an append waits longer before it writes a side file instead
STARTED = time.monotonic()
STATE_EVENTS = ("SessionStart", "SessionEnd", "Stop", "UserPromptSubmit", "Notification")
TODO_TOOLS = ("TaskCreate", "TaskUpdate", "TodoWrite")  # relaybay.domain.plan.TODO_TOOLS
SPOOL_FILE = "hook-spool.jsonl"
SPOOL_LINES_MAX = 200
SPOOL_BYTES_MAX = 256 * 1024
# Read from the raw bytes: a tool's whole input may be in the body and parsing it costs time.
# Inside a JSON string a quote is escaped, so a match is a key, never text in a value.
_EVENT_RE = re.compile(rb'"hook_event_name"\s*:\s*"([A-Za-z]+)"')
_TOOL_RE = re.compile(rb'"tool_name"\s*:\s*"([A-Za-z_]+)"')
# The server answers the long poll within WAKE_HOLD_SECONDS; a little longer than that.
WAIT_TIMEOUT_SECONDS = WAKE_HOLD_SECONDS + 60
WAKE_EXIT_CODE = 2


def forward(url: str, token_file: Path, body: bytes, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """The server's answer when it is 200 with a JSON object, else b""."""
    return post(url, token_file.read_text(encoding="utf-8").strip(), body, timeout)


def post(url: str, token: str, body: bytes, timeout: float) -> bytes:
    """`forward` with the token already read."""
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    opener = build_opener(ProxyHandler({}))  # localhost: never through a system proxy
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            return b""
        answer = response.read()
    return answer if isinstance(json.loads(answer), dict) else b""


def with_host_session(body: bytes) -> bytes:
    """SessionStart gets the desktop session id from the environment Claude Code runs hooks in;
    any other body is forwarded as it is (it is not even parsed)."""
    host = os.environ.get(HOST_SESSION_ENV)
    if not host or b'"SessionStart"' not in body:
        return body
    payload = json.loads(body)
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return body
    payload[HOST_SESSION_FIELD] = host
    payload[ATTENDED_FIELD] = os.environ.get(ATTENDED_ENV, "")
    return json.dumps(payload).encode("utf-8")


def event_kind(body: bytes) -> str:
    """The kind of event: `guard` (PreToolUse, answered at once), `state` (worth a retry and
    the spool) or `ping` (any other tool call, or a body this cannot read)."""
    found = _EVENT_RE.search(body)
    event = found.group(1).decode() if found else ""
    if event == "PreToolUse":
        return "guard"
    if event in STATE_EVENTS:
        return "state"
    if event == "PostToolUse":
        tool = _TOOL_RE.search(body)
        if tool is not None and tool.group(1).decode() in (*TODO_TOOLS, *LAUNCH_TOOLS):
            return "state"
    return "ping"


def config_folder(environ: Mapping[str, str] = os.environ) -> Path:
    """The desktop program's config folder (`relaybay.desktop.settings.config_dir`), worked out
    without platformdirs: `RELAYBAY_CONFIG_DIR`, else the per-user config folder of the OS."""
    value = environ.get("RELAYBAY_CONFIG_DIR")
    if value:
        return Path(value).expanduser()
    if sys.platform == "win32":
        base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "relaybay"


def spool_path(environ: Mapping[str, str] = os.environ) -> Path:
    return config_folder(environ) / SPOOL_FILE


def spool_parts(path: Path) -> list[Path]:
    """Side files of runs that did not get the lock (`hook-spool.<pid>.<ns>.part`), oldest
    first; the next run that holds the lock moves them into the spool."""
    stem = path.name.removesuffix(".jsonl")
    return sorted(path.parent.glob(f"{stem}.*.part"), key=lambda p: p.name.split(".")[-2])


def _send(url: str, token: str, body: bytes, timeout: float) -> bytes | None:
    """The answer (b"" when there is nothing to pass on), or None when the server did not answer
    at all. An HTTP error or a broken answer counts as answered: a resend would get the same."""
    try:
        return post(url, token, body, timeout)
    except (HTTPError, ValueError):
        return b""
    except OSError:  # refused, reset or timed out
        return None


def _utc_now() -> str:
    """The time as the server writes it (`service.now_iso`)."""
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{stamp}.{int(now * 1000) % 1000:03d}+00:00"


class _SpoolLock:
    """A short exclusive lock on `hook-spool.lock` beside the spool, so two forwarders do not
    append, trim or rewrite it at the same moment. `held` is False when it was not got within
    LOCK_WAIT_SECONDS; the caller then does only what is safe without it."""

    def __init__(self, path: Path, wait: float | None = None) -> None:
        self.lock_path = path.with_name(path.name.removesuffix(".jsonl") + ".lock")
        self.wait = LOCK_WAIT_SECONDS if wait is None else wait
        self.handle: int | None = None
        self.held = False

    def __enter__(self) -> "_SpoolLock":
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return self
        until = time.monotonic() + self.wait
        while True:
            try:
                _lock(self.handle)
                self.held = True
                return self
            except OSError:
                if time.monotonic() >= until:
                    return self
                time.sleep(0.002)

    def __exit__(self, *exc: object) -> None:
        if self.handle is None:
            return
        try:
            if self.held:
                _unlock(self.handle)
        except OSError:
            pass  # closing the handle releases the lock as well
        os.close(self.handle)


def _lock(handle: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)


def spool(
    path: Path, body: bytes, at: str | None = None, wait: float = APPEND_WAIT_SECONDS
) -> None:
    """Keep a state-bearing event the server did not take, with the time it happened (`at`);
    the oldest lines go when the spool is over SPOOL_LINES_MAX lines or SPOOL_BYTES_MAX bytes.
    Without the lock (not got within `wait`) the line goes to a side file of its own: appends of
    two processes to one file can tear on Windows, where an append is a seek and a write."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        return
    payload.pop("prompt", None)  # the server never keeps the prompt text, nor does the spool
    payload[SPOOLED_FIELD] = at or _utc_now()
    line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(line) > SPOOL_BYTES_MAX:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SpoolLock(path, wait) as lock:
        if not lock.held:
            part = path.with_name(
                f"{path.name.removesuffix('.jsonl')}.{os.getpid()}.{time.time_ns()}.part"
            )
            _write_new(part, line)
            return
        _append(path, line + _take_parts(path))
        data = path.read_bytes()
        lines = data.splitlines(keepends=True)
        if len(lines) <= SPOOL_LINES_MAX and len(data) <= SPOOL_BYTES_MAX:
            return
        kept: list[bytes] = []
        size = 0
        for entry in reversed(lines):
            if len(kept) == SPOOL_LINES_MAX or size + len(entry) > SPOOL_BYTES_MAX:
                break
            kept.append(entry)
            size += len(entry)
        _replace(path, b"".join(reversed(kept)))


def _write_new(path: Path, data: bytes) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def _append(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)


def _take_parts(path: Path) -> bytes:
    """The side files' lines (under the lock); each file is removed once read. One that cannot be
    read or removed yet stays for a later run."""
    taken = b""
    for part in spool_parts(path):
        try:
            data = part.read_bytes()
            part.unlink()
        except OSError:
            continue
        taken += data
    return taken


def _replace(path: Path, data: bytes) -> None:
    """Write `data` in place of `path` (0600 like the spool itself). On Windows the rename fails
    while another process has the file open: the spool then stays as it was."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(temporary, flags, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


def drain(path: Path, url: str, token: str, until: float, timeout: float) -> bool | None:
    """Send the spooled events oldest first until one gets no answer or `time.monotonic()`
    reaches `until`; the sent ones leave the spool. True when the server answered, False when
    it did not, None when nothing was sent (no spool, or another run is draining it)."""
    if not path.exists() and not spool_parts(path):  # the common case: no lock is taken
        return None
    # The lock is held only to read and to rewrite, never over the network: other runs append
    # meanwhile. Two runs may send the same lines; the server skips the older copy.
    with _SpoolLock(path) as lock:
        if not lock.held:
            return None
        try:
            parts = _take_parts(path)
            if parts:
                _append(path, parts)
            data = path.read_bytes()
        except OSError:
            return None
    reached = None
    consumed = 0
    for entry in data.splitlines(keepends=True):
        left = until - time.monotonic()
        if left <= 0.05:
            break
        if entry.strip():
            allowed = min(timeout, left)
            begun = time.monotonic()
            if _send(url, token, entry.strip(), allowed) is None:
                # A refusal or a timeout of at least SILENT_SECONDS says the server is silent; a
                # shorter try, cut by the budget's remainder, says nothing, and the live event
                # still gets its own try.
                ran_out = (
                    allowed < min(timeout, SILENT_SECONDS)
                    and time.monotonic() - begun >= allowed - 0.05
                )
                if not ran_out:
                    reached = False
                break
            reached = True
        consumed += len(entry)
    if consumed:
        with _SpoolLock(path) as lock:
            if lock.held:
                _drop_head(path, data[:consumed])
    return reached


def _drop_head(path: Path, head: bytes) -> None:
    """Remove the sent lines; lines appended meanwhile stay. When the head changed or the
    rewrite fails, nothing is removed."""
    try:
        current = path.read_bytes()
        if current.startswith(head):
            rest = current[len(head) :]
            if rest:
                _replace(path, rest)
            else:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def deliver(url: str, token: str, body: bytes, started: float | None = None) -> bytes:
    """Forward one event by its kind and return the answer to print (or b""). Spooled events go
    first, so the server sees them in order. The budget counts from `started` (the process
    start, `STARTED`), else from now."""
    at = _utc_now()
    kind = event_kind(body)
    if kind == "guard":
        return _send(url, token, body, TIMEOUT_SECONDS) or b""
    deadline = (time.monotonic() if started is None else started) + BUDGET_SECONDS
    state = kind == "state"
    path = spool_path()
    own = STATE_TIMEOUT_SECONDS if state else TIMEOUT_SECONDS
    # The spool leaves the event at least one try of its own.
    reached = drain(path, url, token, deadline - own, own)
    if reached is False:
        # The server did not answer just now: a ping is dropped, a state event goes to the spool
        # at once, so while the server is down only the first event waits for its tries.
        if state:
            spool(path, body, at, _append_wait(deadline))
        return b""
    if not state:
        return _send(url, token, body, TIMEOUT_SECONDS) or b""
    answer = _send(url, token, body, max(0.05, min(own, deadline - time.monotonic())))
    if answer is None and deadline - time.monotonic() > RETRY_PAUSE_SECONDS + 0.1:
        time.sleep(RETRY_PAUSE_SECONDS)
        answer = _send(url, token, body, min(own, deadline - time.monotonic()))
    if answer is None:
        spool(path, body, at, _append_wait(deadline))
        return b""
    return answer


def _append_wait(deadline: float) -> float:
    return max(LOCK_WAIT_SECONDS, min(APPEND_WAIT_SECONDS, deadline - time.monotonic()))


def main(argv: list[str] | None = None, started: float | None = None) -> int:
    """`started`: when the process started (`time.monotonic()`), for the budget; this module's
    import when not given."""
    args_in = sys.argv[1:] if argv is None else argv
    if args_in == ["--version"]:
        # `doctor` and `setup --check` compare this with their own version (a stale hook path).
        from relaybay import __version__

        sys.stdout.write(f"relaybay-hook {__version__}\n")
        return 0
    parser = argparse.ArgumentParser(prog="relaybay hook", description=__doc__)
    parser.add_argument("--url", required=True, help="relaybay hook URL (…/hooks/claude-code).")
    parser.add_argument("--token-file", type=Path, required=True, help="relaybay token file.")
    parser.add_argument(
        "--wait", action="store_true", help="Wake-up hook: wait for an owner comment."
    )
    try:
        args = parser.parse_args(args_in)
    except SystemExit:
        return 0  # a broken hook entry must not show errors in the session either
    try:
        body = sys.stdin.buffer.read()
        if args.wait:
            answer = forward(args.url, args.token_file, body, WAIT_TIMEOUT_SECONDS)
            text = json.loads(answer).get("wake") if answer else None
            if isinstance(text, str) and text:
                sys.stderr.write(text + "\n")
                sys.stderr.flush()
                return WAKE_EXIT_CODE
            return 0
        token = args.token_file.read_text(encoding="utf-8").strip()
        answer = deliver(
            args.url, token, with_host_session(body), STARTED if started is None else started
        )
        if answer:
            sys.stdout.buffer.write(answer)
            sys.stdout.flush()
    except Exception:
        # relaybay down, slow or misconfigured: drop the event silently, the session goes on.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
