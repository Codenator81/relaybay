"""One program per user: an OS file lock, and a local IPC socket a second start uses to ask the
running program to show its window (`relaybay desktop --quit` asks it to quit).

The lock is `desktop.lock` in the config folder (msvcrt / fcntl; the OS releases it when the
process dies). The IPC listener binds 127.0.0.1 on a free port with a random key; port and key
are in `desktop.ipc` (0600), so only this user can make it act.
"""

import hashlib
import hmac
import json
import logging
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

from relaybay.security import _write_private

log = logging.getLogger(__name__)

LOCK_FILE = "desktop.lock"
IPC_FILE = "desktop.ipc"
RAISE = "raise"
QUIT = "quit"
ACCEPT_POLL = 0.5
PEER_TIMEOUT = 2.0
LINE_MAX = 256


class Lock:
    def __init__(self, handle: IO[bytes]) -> None:
        self._handle: IO[bytes] | None = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def acquire(folder: Path) -> Lock | None:
    """The program's lock, or None when another process holds it."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / LOCK_FILE).open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return Lock(handle)


class RaiseServer:
    """Answers `notify_running`: `raise` from a second start calls `on_raise`, `quit` (a
    clean stop without the tray, for scripts) calls `on_quit`.

    Protocol, one line each way with a socket timeout on both sides: the server sends a random
    nonce, the client answers `HMAC-SHA256(key, nonce) message`, the server checks it, answers
    `ok` and runs the handler. Each connection gets its own short thread, so a peer that stalls
    or a handler that fails never stops the listener.
    """

    def __init__(
        self, folder: Path, on_raise: Callable[[], None], on_quit: Callable[[], None]
    ) -> None:
        self.path = folder / IPC_FILE
        self.handlers = {RAISE: on_raise, QUIT: on_quit}
        self._key = secrets.token_bytes(32)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(ACCEPT_POLL)  # lets close() end the loop on every OS
        port = self._listener.getsockname()[1]
        _write_private(self.path, json.dumps({"port": port, "key": self._key.hex()}))
        self._closed = False
        self._thread = threading.Thread(target=self._serve, name="relaybay-ipc", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn, _addr = self._listener.accept()
            except TimeoutError:
                continue
            except Exception:
                if self._closed:
                    return
                log.warning("IPC accept failed", exc_info=True)
                continue
            threading.Thread(
                target=self._handle, args=(conn,), name="relaybay-ipc-conn", daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(PEER_TIMEOUT)
                nonce = secrets.token_hex(16)
                conn.sendall(f"{nonce}\n".encode())
                line = _read_line(conn)
                digest, _sep, message = line.partition(" ")
                if not hmac.compare_digest(digest, _sign(self._key, nonce)):
                    log.warning("IPC request with a wrong key refused")
                    return
                handler = self.handlers.get(message)
                if handler is None:
                    log.warning("IPC request %r unknown", message[:40])
                    return
                conn.sendall(b"ok\n")
            except Exception:
                log.info("IPC peer went away", exc_info=True)
                return
        try:
            handler()
        except Exception:
            log.exception("IPC %s handler failed", message)

    def close(self) -> None:
        self._closed = True
        self._listener.close()
        self.path.unlink(missing_ok=True)


def _sign(key: bytes, nonce: str) -> str:
    return hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()


def _read_line(conn: socket.socket) -> str:
    """One line of at most LINE_MAX bytes; the socket's timeout bounds the wait."""
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(LINE_MAX)
        if not chunk:
            raise ConnectionError("the peer closed the connection")
        data += chunk
        if len(data) > LINE_MAX:
            raise ValueError("IPC line too long")
    return data.decode("ascii").strip()


def serve_raise(
    folder: Path, on_raise: Callable[[], None], on_quit: Callable[[], None]
) -> RaiseServer:
    return RaiseServer(folder, on_raise, on_quit)


def notify_running(folder: Path, message: str = RAISE, timeout: float = 5.0) -> bool:
    """Ask the running program to show its window (or to quit); False when none answers within
    `timeout` (a stale desktop.ipc, a port reused by another program)."""
    try:
        info = json.loads((folder / IPC_FILE).read_text(encoding="utf-8"))
        address = ("127.0.0.1", int(info["port"]))
        key = bytes.fromhex(info["key"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    try:
        with socket.create_connection(address, timeout=timeout) as conn:
            nonce = _read_line(conn)
            conn.sendall(f"{_sign(key, nonce)} {message}\n".encode())
            return _read_line(conn) == "ok"
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        log.info("no running program answered on %s: %s", address, exc)
        return False
