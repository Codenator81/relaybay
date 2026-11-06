"""The embedded server: the same app and uvicorn options as `relaybay serve`, in a thread.

uvicorn installs signal handlers only on the main thread (`Server.capture_signals`), so a
server thread leaves the GUI's signals alone; it is stopped with `should_exit`.
"""

import json
import logging
import socket
import sys
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, ProxyHandler, build_opener

from relaybay.config import Settings
from relaybay.desktop.settings import LOOPBACK

log = logging.getLogger(__name__)

START_TIMEOUT = 30.0
STOP_TIMEOUT = 5.0


class PortTaken(Exception):
    """The port is in use; `relaybay` is the /healthz answer when another relaybay holds it."""

    def __init__(self, port: int, relaybay: dict[str, Any] | None) -> None:
        self.port = port
        self.relaybay = relaybay
        if relaybay is not None:
            what = f"another relaybay (version {relaybay.get('version', 'unknown')})"
        else:
            what = "another program"
        super().__init__(
            f"Port {port} is used by {what}. Stop it, or switch to an external server."
        )


def server_opener(base_url: str) -> OpenerDirector:
    """Loopback never goes through a system proxy; a remote server may need one."""
    host = urlsplit(base_url).hostname or ""
    return build_opener(ProxyHandler({})) if host in LOOPBACK else build_opener()


def healthz(base_url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """The /healthz answer of a relaybay at `base_url`, or None when nothing answers like one."""
    try:
        with server_opener(base_url).open(f"{base_url}/healthz", timeout=timeout) as response:
            data = json.load(response)
    except (URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("status") == "ok" else None


def bind(host: str, port: int) -> socket.socket:
    """A listening socket, refusing a port another process listens on.

    Windows: SO_REUSEADDR would let us bind over a live listener, so it uses
    SO_EXCLUSIVEADDRUSE instead; POSIX: SO_REUSEADDR only skips TIME_WAIT on a restart.
    """
    family = socket.AF_INET6 if ":" in host.strip("[]") else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
        sock.listen(2048)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


class EmbeddedServer:
    def __init__(self, settings: Settings, log_config: dict[str, Any] | None = None) -> None:
        self.settings = settings
        self.log_config = log_config
        self._server: Any = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = START_TIMEOUT) -> None:
        """Start and wait until /healthz answers. Raises PortTaken, or RuntimeError when the
        server stopped during start-up (the reason is in the log)."""
        from relaybay.app import create_app, make_server, server_options

        if self.running:
            raise RuntimeError("the server is already running")
        settings = self.settings
        try:
            sock = bind(settings.host, settings.port)
        except OSError as exc:
            raise PortTaken(settings.port, healthz(settings.base_url)) from exc
        options = {**server_options(settings), "log_config": self.log_config}
        if self.log_config is None:
            options["log_level"] = "warning"
        server = make_server(create_app(settings), **options)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, name="relaybay-server", daemon=False
        )
        self._server, self._thread = server, thread
        thread.start()
        deadline = time.monotonic() + timeout
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                self.stop()
                sock.close()
                raise RuntimeError("the server did not start; see the log")
            time.sleep(0.05)
        if healthz(settings.base_url, timeout=5) is None:
            self.stop()
            raise RuntimeError(f"the server does not answer on {settings.base_url}/healthz")
        log.info("embedded server on %s", settings.base_url)

    def stop(self, timeout: float = STOP_TIMEOUT) -> bool:
        """Ask the server to exit and wait; True when the thread ended in time."""
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return True
        server.should_exit = True
        thread.join(timeout)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._server = self._thread = None
        else:
            log.error("the embedded server did not stop within %.0f s", 2 * timeout)
        return stopped
