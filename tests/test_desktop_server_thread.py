"""The embedded server's stop with a board window still listening on /events (the SSE stream
never ends by itself): the stop must not wait for it, and the cut stream must not leave a
traceback in the log."""

import logging
import socket
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from relaybay.config import load_settings
from relaybay.desktop.server_thread import EmbeddedServer
from relaybay.security import COOKIE_NAME, load_or_create_token, session_cookie_value


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_stop_does_not_wait_for_an_open_event_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("RELAYBAY_PORT", str(free_port()))
    settings = load_settings(serving=True)
    token = load_or_create_token(settings.token_path)
    server = EmbeddedServer(settings)
    server.start()
    opened = threading.Event()
    seen: list[object] = []

    def listen() -> None:
        # Like the window: the login cookie, then /events held open until the server goes.
        cookie = f"{COOKIE_NAME}={session_cookie_value(token)}"
        request = Request(f"{settings.base_url}/events", headers={"Cookie": cookie})
        try:
            with urlopen(request, timeout=30) as response:
                seen.append(response.status)
                seen.append(response.readline())  # the "retry:" line: the stream is open
                opened.set()
                while response.readline():
                    pass
        except OSError as exc:
            seen.append(exc)
        finally:
            opened.set()

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    assert opened.wait(10)
    assert seen[:2] == [200, b"retry: 3000\n"], seen
    with caplog.at_level(logging.INFO):
        started = time.monotonic()
        assert server.stop()
        took = time.monotonic() - started
    thread.join(10)
    assert took < 4, f"stop took {took:.1f} s with an open /events stream"
    assert "CancelledError" not in caplog.text and "Traceback" not in caplog.text
