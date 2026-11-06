import functools
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings, load_settings
from relaybay.security import load_or_create_token
from relaybay.service import Service

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def desktop_config_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Keep every test (and its subprocesses) off the real desktop.toml in the user's config."""
    folder = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("RELAYBAY_CONFIG_DIR", str(folder))
    monkeypatch.setenv("RELAYBAY_CACHE_DIR", str(folder / "cache"))
    monkeypatch.setattr("relaybay.desktop.settings.default_config_dir", lambda: folder)
    return folder


class _NoNetwork:
    def open(self, request: object, timeout: float) -> None:
        raise AssertionError(f"a test reached the network: {getattr(request, 'full_url', '')}")


@pytest.fixture(autouse=True)
def no_update_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks GitHub: the default opener fails, and servers in subprocesses skip the
    update check."""
    monkeypatch.setattr("relaybay.updates.default_opener", _NoNetwork)
    monkeypatch.setenv("RELAYBAY_UPDATE_CHECK", "false")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(home=tmp_path, environ={})


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[Service]:
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        yield svc
    finally:
        await svc.close()


@pytest.fixture
def token(settings: Settings) -> str:
    return load_or_create_token(settings.token_path)


@pytest.fixture
def client(settings: Settings, token: str) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE) as test_client:
        yield test_client


@pytest.fixture
def board(client: TestClient, token: str) -> TestClient:
    """A client that signed in through /login, as a browser would."""
    response = client.get(f"/login?token={token}", follow_redirects=False)
    assert response.status_code == 303
    return client


@dataclass
class LiveServer:
    url: str
    token: str
    settings: Settings

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[Callable[[], LiveServer]]:
    """Factory: start a real uvicorn server on a free port (data in tmp_path)."""
    started: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start() -> LiveServer:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        settings = load_settings(home=tmp_path, port=port, environ={})
        settings.heartbeat_seconds = 0.5
        app = create_app(settings)
        config = uvicorn.Config(
            app, log_level="warning", lifespan="on", timeout_graceful_shutdown=1
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        started.append((server, thread))
        token = settings.token_path.read_text(encoding="utf-8").strip()
        return LiveServer(url=f"http://127.0.0.1:{port}", token=token, settings=settings)

    yield start
    for server, thread in started:
        server.should_exit = True
        thread.join(timeout=10)


def run_in_app(client: TestClient, func: Callable, *args, **kwargs):
    """Run a service coroutine on the app's own event loop (the TestClient portal)."""
    assert client.portal is not None
    return client.portal.call(functools.partial(func, *args, **kwargs))


@pytest.fixture
def seeded(board: TestClient) -> str:
    """acme-shop with one dev task that has a report; returns the task key."""
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    task = run_in_app(
        board, service.create_task, "acme-shop", "Checkout page", stage="dev", branch="feat/x"
    )
    run_in_app(board, service.add_report, task.key, "changed · S · first pass <b>bold</b>")
    return task.key
