"""The Terminal tab of a run with a fake PTY: the real `claude` is never started here.

The hard rule under test: while a terminal is open the run is with the owner, no turn spawns and
texts sent wait; closing it kills the process first and then hands the run back."""

import asyncio
import base64
import os
import queue
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.db import repo
from relaybay.domain.errors import ValidationError
from relaybay.mcp_server import format as fmt
from relaybay.runner import terminal as run_terminal
from relaybay.runner.layers import LayerState
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_runs_service import CC_A, StubRuns

LAYERS_OK = LayerState("command", True, "command hooks from your user settings", "registered")


class FakePty:
    """What the service sees of a PTY: output the test pushes, input and sizes it records."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], rows: int, cols: int):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.pid = 4343
        self.sizes = [(rows, cols)]
        self.written: list[str] = []
        self.out: queue.Queue[str | None] = queue.Queue()
        self.killed = False
        self.closed = False
        self.code: int | None = None
        self.resized = threading.Event()

    def read(self) -> str:
        item = self.out.get(timeout=30)
        if item is None:
            raise EOFError("closed")
        return item

    def write(self, text: str) -> None:
        self.written.append(text)

    def resize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))
        self.resized.set()

    def kill(self) -> None:
        self.killed = True
        if self.code is None:
            self.code = 1
        self.out.put(None)

    def close(self) -> None:
        self.closed = True

    def exit_code(self) -> int | None:
        return self.code

    def exit(self, code: int = 0) -> None:
        """The TUI ends by itself (`/exit`)."""
        self.code = code
        self.out.put(None)


class FakeBackend:
    def __init__(self) -> None:
        self.spawned: list[FakePty] = []
        self.fail: Exception | None = None

    def spawn(self, argv: list[str], cwd: str, env: Any, rows: int, cols: int) -> run_terminal.Pty:
        if self.fail is not None:
            raise self.fail
        pty = FakePty(argv, cwd, dict(env), rows, cols)
        self.spawned.append(pty)
        return pty


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: LAYERS_OK)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
    service.terminals.backend = FakeBackend()
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


def stub(service: Service) -> StubRuns:
    assert isinstance(service.runs, StubRuns)
    return service.runs


def backend(service: Service) -> FakeBackend:
    assert isinstance(service.terminals.backend, FakeBackend)
    return service.terminals.backend


async def idle_run(service: Service) -> Any:
    run = await service.start_run("acme-shop-1")
    await stub(service).finish(run.id)
    return await service.get_run(run.id)


async def events(service: Service, kind: str) -> list[Any]:
    return [e for e in await repo.events_after(service._r, 0, None, 1000) if e.kind == kind]


async def test_an_open_terminal_holds_the_run_and_its_close_hands_it_back(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    terminal = await svc.open_terminal(run.id, 30, 100)
    [pty] = backend(svc).spawned
    assert pty.argv == ["/usr/local/bin/claude", "--resume", CC_A] and pty.cwd == run.cwd
    assert pty.sizes == [(30, 100)]
    assert pty.env["RELAYBAY_RUN_ID"] == str(run.id) and "CLAUDECODE" not in pty.env
    held = await svc.get_run(run.id)
    assert held.with_owner and held.in_terminal and held.with_owner_by == "owner"
    [opened] = await events(svc, "run.terminal_opened")
    assert (opened.actor, opened.payload["run_id"], opened.payload["pid"]) == (
        "owner",
        run.id,
        4343,
    )

    # The hard rule: a text waits, the queue starts nothing, Start anyway is refused.
    waiting = await svc.send_to_run(run.id, "After the terminal")
    assert waiting.state == "queued" and len(stub(svc).started) == 1
    assert await svc.dispatch_queued() == 0 and len(stub(svc).started) == 1
    with pytest.raises(ValidationError, match="in the Terminal tab"):
        await svc.start_anyway(run.id)
    task, view = await svc.run_status("acme-shop-1")
    assert "With the owner in the board's Terminal tab since" in fmt.run_status(task, view)

    # The process dies before the waiting text starts.
    killed_first: list[bool] = []
    original = stub(svc).start_turn

    async def start_turn(*args: Any, **kwargs: Any) -> int:
        killed_first.append(pty.killed)
        return await original(*args, **kwargs)

    stub(svc).start_turn = start_turn  # type: ignore[method-assign]
    terminal.write("ignored after the close")
    assert await svc.close_terminal(run.id, reason="the tab closed")
    assert killed_first == [True] and pty.closed
    back = await svc.get_run(run.id)
    assert not back.with_owner and back.with_owner_in is None and back.status == "running"
    spec = stub(svc).started[-1][2]
    assert (spec.prompt, spec.resume) == ("After the terminal", CC_A)
    [closed] = await events(svc, "run.terminal_closed")
    assert closed.payload["reason"] == "the tab closed" and closed.payload["exit_code"] == 1
    assert await svc.close_terminal(run.id, reason="again") is False


async def test_no_turn_spawns_while_a_terminal_is_open_even_without_the_hold(
    svc: Service,
) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    await svc.open_terminal(run.id, 24, 80)
    await svc.send_to_run(run.id, "Must wait")
    async with svc._tx() as tx:  # the column lost: the open terminal still holds the run
        await repo.update_run(tx.conn, run.id, {"with_owner_at": None}, tx.now)
    assert await svc.dispatch_queued() == 0
    assert len(stub(svc).started) == 1


async def test_one_terminal_per_run_and_the_refusals_before_it_opens(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    with pytest.raises(ValidationError, match="Terminal tab is off"):
        await svc.terminal_token(run.id)
    await svc.set_terminal_enabled(True)
    with pytest.raises(ValidationError, match="is running"):
        await svc.terminal_token(run.id)
    await svc.stop_run(run.id)
    with pytest.raises(ValidationError, match="no Claude session yet"):
        await svc.open_terminal(run.id, 24, 80)
    await svc.end_run(run.id)
    await svc.create_task("acme-shop", "Second", stage="dev")
    second = await svc.start_run("acme-shop-2")
    await stub(svc).finish(second.id)
    await svc.open_terminal(second.id, 24, 80)
    with pytest.raises(ValidationError, match="open already"):
        await svc.terminal_token(second.id)
    with pytest.raises(ValidationError, match="open already"):
        await svc.open_terminal(second.id, 24, 80)
    with pytest.raises(ValidationError, match="already with the owner in the Terminal tab"):
        await svc.open_in_desktop(second.id)
    assert len(backend(svc).spawned) == 1


async def test_the_desktop_app_holding_the_run_keeps_the_terminal_shut(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    await svc.open_in_desktop(run.id)
    with pytest.raises(ValidationError, match="already with the owner in the desktop app"):
        await svc.terminal_token(run.id)
    assert backend(svc).spawned == []


async def test_end_kills_the_terminal_and_starts_no_waiting_text(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    await svc.open_terminal(run.id, 24, 80)
    await svc.send_to_run(run.id, "Never runs")
    ended = await svc.end_run(run.id)
    [pty] = backend(svc).spawned
    assert pty.killed and ended.status == "ended" and not ended.with_owner
    assert len(stub(svc).started) == 1
    view = await svc.run_view(await svc.get_task("acme-shop-1"))
    assert view is not None and view.turns[-1].outcome == "cancelled"
    [closed] = await events(svc, "run.terminal_closed")
    assert closed.payload["reason"] == "ended by owner"


async def test_back_to_the_program_and_the_switch_off_close_the_terminal(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    await svc.open_terminal(run.id, 24, 80)
    back = await svc.return_run(run.id)
    assert not back.with_owner and backend(svc).spawned[0].killed
    await svc.open_terminal(run.id, 24, 80)
    await svc.set_terminal_enabled(False)
    assert backend(svc).spawned[1].killed and not (await svc.get_run(run.id)).with_owner
    reasons = [e.payload["reason"] for e in await events(svc, "run.terminal_closed")]
    assert reasons == ["Back to the program", "the Terminal tab was turned off"]
    [switch, off] = await events(svc, "settings.terminal_enabled")
    assert (switch.payload["on"], off.payload["on"]) == (True, False)


async def test_a_kill_that_fails_keeps_the_run_held_until_a_later_close(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    await svc.open_terminal(run.id, 24, 80)
    [pty] = backend(svc).spawned
    kill = pty.kill

    def refuse() -> None:
        raise OSError("TerminateJobObject failed")

    pty.kill = refuse  # type: ignore[method-assign]
    await svc.send_to_run(run.id, "Waits")
    with pytest.raises(OSError, match="TerminateJobObject"):
        await svc.close_terminal(run.id, reason="the tab closed")
    assert (await svc.get_run(run.id)).in_terminal and svc.terminals.busy(run.id)
    assert await svc.dispatch_queued() == 0 and len(stub(svc).started) == 1
    pty.kill = kill  # type: ignore[method-assign]
    back = await svc.return_run(run.id)
    assert not back.with_owner and len(stub(svc).started) == 2


async def test_a_failed_start_hands_the_run_back(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.set_terminal_enabled(True)
    backend(svc).fail = FileNotFoundError(2, "no claude")
    await svc.send_to_run(run.id, "Before")  # starts at once: the run is relaybay's
    await stub(svc).finish(run.id)
    with pytest.raises(
        ValidationError, match=r"did not start in the terminal.*stays with relaybay"
    ):
        await svc.open_terminal(run.id, 24, 80)
    after = await svc.get_run(run.id)
    assert not after.with_owner and not svc.terminals.busy(run.id)
    assert await events(svc, "run.terminal_opened") == []


async def test_a_server_stop_kills_the_terminal_and_the_next_start_hands_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: LAYERS_OK)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
    service.terminals.backend = FakeBackend()
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    run = await idle_run(service)
    await service.set_terminal_enabled(True)
    await service.open_terminal(run.id, 24, 80)
    [pty] = backend(service).spawned
    await service.close()
    assert pty.killed
    again = Service(tmp_path / "relaybay.sqlite")
    await again.start()
    try:
        recovered = await again.get_run(run.id)
        assert not recovered.with_owner and recovered.with_owner_in is None
        [closed] = await events(again, "run.terminal_closed")
        assert (closed.actor, closed.payload["reason"]) == ("relaybay", "server stopped")
        [returned] = await events(again, "run.returned")
        assert returned.actor == "relaybay"
    finally:
        await again.close()


def test_a_token_is_single_use_short_lived_and_for_one_run() -> None:
    now = [100.0]
    terminals = run_terminal.TerminalManager(None, clock=lambda: now[0])
    first = terminals.mint(1)
    assert terminals.take(first, 1) and not terminals.take(first, 1)
    other = terminals.mint(1)
    assert not terminals.take(other, 2) and not terminals.take(other, 1)  # spent by the miss
    late = terminals.mint(1)
    now[0] += run_terminal.TOKEN_TTL_SECONDS + 1
    assert not terminals.take(late, 1) and not terminals.take("", 1)


def test_the_command_and_environment_of_the_terminal() -> None:
    assert run_terminal.terminal_command("claude", CC_A, ("/w/b",)) == [
        "claude",
        "--resume",
        CC_A,
        "--add-dir",
        "/w/b",
    ]
    with pytest.raises(ValidationError, match=r"claude\.cmd shim"):
        run_terminal.terminal_command("C:/npm/claude.cmd", CC_A, ("C:/a&b",))
    base = {"PATH": "/bin", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDECODE": "1"}
    env = run_terminal.terminal_env(base, 7, platform="linux")
    assert env["RELAYBAY_RUN_ID"] == "7" and env["TERM"] == "xterm-256color"
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "TERM" not in run_terminal.terminal_env(base, 7, platform="win32")
    assert run_terminal.clean_size(1, 1) == (2, 10)
    assert run_terminal.clean_size("40", "9999") == (40, run_terminal.COLS_MAX)


@pytest.mark.skipif(sys.platform == "win32", reason="the stdlib pty is POSIX (not probed there)")
def test_the_posix_pty_runs_a_program_and_kills_its_group(tmp_path: Path) -> None:
    pty = run_terminal.PosixPtyBackend().spawn(
        [sys.executable, "-c", "import sys; print(sys.stdin.readline().upper(), flush=True)"],
        str(tmp_path),
        {**os.environ, "TERM": "xterm-256color"},
        24,
        80,
    )
    try:
        pty.write("hello\n")
        seen = ""
        deadline = time.monotonic() + 30
        while "HELLO" not in seen and time.monotonic() < deadline:
            seen += pty.read()
        assert "HELLO" in seen
    finally:
        pty.kill()
        pty.close()
    assert pty.exit_code() is not None


# --- the board: the switch, the page, the token and the WebSocket ----------------------------


@pytest.fixture
def board(
    settings: Settings, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: LAYERS_OK)
    settings.claude_path = tmp_path / "no-such-claude"
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        service: Service = client.app.state.service
        service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
        service.terminals.backend = FakeBackend()
        folder = tmp_path / "acme-shop"
        folder.mkdir()
        run_in_app(client, service.upsert_project, "acme-shop", repo_path=str(folder))
        run_in_app(client, service.create_task, "acme-shop", "Checkout", stage="dev")
        yield client


def form(token: str, **fields: str) -> dict[str, str]:
    return {"csrf_token": csrf_token(token), **fields}


BASE_RUN = "/p/acme-shop/t/acme-shop-1/run"
# TestClient joins a relative socket URL to ws://testserver: the Host must be the board's.
WS = BASE.replace("http://", "ws://")


def board_idle_run(board: TestClient) -> int:
    service: Service = board.app.state.service
    run = run_in_app(board, service.start_run, "acme-shop-1", brief="Build it")
    run_in_app(board, stub(service).finish, run.id)
    return run.id


def switch(board: TestClient, token: str, on: bool) -> None:
    fields = ["0", "1"] if on else "0"
    saved = board.post(
        "/settings/agents/terminal",
        data=form(token, on=fields),  # type: ignore[arg-type]
        headers={"Origin": BASE},
    )
    assert saved.status_code == 200 and "Saved." in saved.text


def mint(board: TestClient, token: str, run_id: int) -> str:
    response = board.post(
        f"{BASE_RUN}/{run_id}/terminal/token", data=form(token), headers={"Origin": BASE}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["token"]


def wait_for(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 15
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def test_the_switch_the_button_and_the_page(board: TestClient, token: str) -> None:
    run_id = board_idle_run(board)
    assert ">Terminal</a>" not in board.get(BASE_RUN).text
    assert board.get(f"{BASE_RUN}/{run_id}/terminal").status_code == 404
    off = board.post(
        f"{BASE_RUN}/{run_id}/terminal/token", data=form(token), headers={"Origin": BASE}
    )
    assert off.status_code == 404
    page = board.get("/settings/agents")
    assert 'id="terminal"' in page.text and "Show the Terminal button" in page.text
    bearer = board.post(
        "/settings/agents/terminal",
        data=form(token, on="1"),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    switch(board, token, True)
    assert f'href="{BASE_RUN}/{run_id}/terminal">Terminal</a>' in board.get(BASE_RUN).text
    shown = board.get(f"{BASE_RUN}/{run_id}/terminal")
    assert shown.status_code == 200 and "Do you trust" not in shown.text
    assert "choose Yes (arrow down)" in shown.text and "vendor/xterm/xterm.js" in shown.text
    [csp] = shown.headers.get_list("content-security-policy")
    assert "style-src 'self' 'unsafe-inline'" in csp and "script-src" not in csp
    assert board.get("/settings/agents").headers.get_list("content-security-policy") == [
        csp.removesuffix("; style-src 'self' 'unsafe-inline'")
    ]
    assert board.get("/static/vendor/xterm/xterm.js").status_code == 200
    assert (
        "Permission is hereby granted" in board.get("/static/vendor/xterm/LICENSE-xterm.txt").text
    )
    bearer_token = board.post(
        f"{BASE_RUN}/{run_id}/terminal/token",
        data=form(token),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer_token.status_code == 403


def refused(board: TestClient, url: str, headers: dict[str, str]) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        board.websocket_connect(url, headers=headers),
    ):
        pass
    assert caught.value.code == 1008


def test_the_socket_refuses_without_a_valid_token_origin_cookie_or_with_bearer(
    board: TestClient, token: str, settings: Settings
) -> None:
    run_id = board_idle_run(board)
    switch(board, token, True)
    url = f"{WS}{BASE_RUN}/{run_id}/terminal?rows=24&cols=80&token="
    origin = {"Origin": BASE}
    refused(board, url, origin)  # no token
    refused(board, url + "made-up", origin)
    refused(board, url + mint(board, token, run_id), {"Origin": "http://evil.example"})
    refused(board, url + mint(board, token, run_id), {})  # no Origin at all
    bearer = {"Origin": BASE, "Authorization": f"Bearer {token}"}
    refused(board, url + mint(board, token, run_id), bearer)
    other_run = mint(board, token, run_id)
    refused(
        board, f"/p/acme-shop/t/acme-shop-1/run/{run_id + 1}/terminal?token={other_run}", origin
    )
    signed_out = TestClient(board.app, base_url=BASE)
    refused(signed_out, url + mint(board, token, run_id), origin)
    used = mint(board, token, run_id)
    with board.websocket_connect(url + used, headers=origin) as socket_:
        socket_.close()
    service: Service = board.app.state.service
    wait_for(
        lambda: not run_in_app(board, service.get_run, run_id).with_owner, "the run handed back"
    )
    refused(board, url + used, origin)  # a token works once
    settings.host = "0.0.0.0"
    refused(board, url + mint(board, token, run_id), origin)  # not loopback only
    settings.host = "127.0.0.1"
    switch(board, token, False)
    refused(board, url + "anything", origin)
    assert len(backend(service).spawned) == 1


def test_the_terminal_over_the_socket_and_its_hand_back(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_id = board_idle_run(board)
    switch(board, token, True)
    url = f"{WS}{BASE_RUN}/{run_id}/terminal?rows=30&cols=120&token={mint(board, token, run_id)}"
    htmx = {"Origin": BASE, "HX-Request": "true"}
    with board.websocket_connect(url, headers={"Origin": BASE}) as socket_:
        [pty] = backend(service).spawned
        assert pty.sizes == [(30, 120)]
        pty.out.put("\x1b[1mclaude\x1b[0m> ")
        assert socket_.receive_text() == "\x1b[1mclaude\x1b[0m> "
        socket_.send_json({"type": "input", "data": "/help\r"})
        socket_.send_json({"type": "resize", "rows": 40, "cols": 132})
        wait_for(lambda: pty.written == ["/help\r"] and len(pty.sizes) == 2, "input and size")
        assert pty.sizes[-1] == (40, 132)
        panel = board.get(BASE_RUN).text
        assert "With you in the Terminal tab since" in panel and "terminal closes" in panel
        sent = board.post(f"{BASE_RUN}/{run_id}/send", data=form(token, text="Later"), headers=htmx)
        assert sent.status_code == 200 and "form-error" not in sent.text
        assert len(stub(service).started) == 1  # waits
        second = board.post(
            f"{BASE_RUN}/{run_id}/terminal/token", data=form(token), headers={"Origin": BASE}
        )
        assert second.status_code == 409 and "open already" in second.text
    wait_for(lambda: len(stub(service).started) == 2, "the waiting text started")
    assert pty.killed and stub(service).started[-1][2].prompt == "Later"
    run = run_in_app(board, service.get_run, run_id)
    assert not run.with_owner and run.status == "running"
    audit = board.get("/audit?changes=1").text
    assert "run.terminal_opened" in audit and "run.terminal_closed" in audit


def test_the_tui_exiting_closes_the_socket_and_hands_back(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_id = board_idle_run(board)
    switch(board, token, True)
    url = f"{WS}{BASE_RUN}/{run_id}/terminal?rows=24&cols=80&token={mint(board, token, run_id)}"
    with board.websocket_connect(url, headers={"Origin": BASE}) as socket_:
        [pty] = backend(service).spawned
        pty.exit(0)
        with pytest.raises(WebSocketDisconnect) as caught:
            socket_.receive_text()
        assert caught.value.code == 1000
    wait_for(lambda: not run_in_app(board, service.get_run, run_id).with_owner, "handed back")
    assert pty.closed


def test_a_refused_open_says_why_after_the_handshake(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_id = board_idle_run(board)
    switch(board, token, True)
    url = f"{WS}{BASE_RUN}/{run_id}/terminal?rows=24&cols=80&token={mint(board, token, run_id)}"
    backend(service).fail = OSError("no console")
    with board.websocket_connect(url, headers={"Origin": BASE}) as socket_:
        assert "did not start in the terminal: no console" in socket_.receive_text()
        with pytest.raises(WebSocketDisconnect) as caught:
            socket_.receive_text()
        assert caught.value.code == 4000
    assert not run_in_app(board, service.get_run, run_id).with_owner


def test_a_real_server_takes_the_upgrade_and_refuses_it_with_403(
    live_server: Callable[[], Any],
) -> None:
    """uvicorn needs a WebSocket library for the upgrade (wsproto): without one it would answer
    the request as plain HTTP. A refusal before the handshake reaches the browser as 403."""
    server = live_server()
    host = server.url.removeprefix("http://")
    path = "/p/acme-shop/t/acme-shop-1/run/1/terminal?token=nothing"

    def handshake(origin: str) -> str:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", int(host.split(":")[1])), timeout=15) as conn:
            conn.sendall(request.encode())
            return conn.recv(1024).decode("latin-1").split("\r\n", 1)[0]

    status = handshake(server.url)
    assert status.startswith("HTTP/1.1 403"), status


async def test_the_reader_thread_hands_chunks_to_the_loop_and_ends_with_none() -> None:
    pty = FakePty([], "", {}, 24, 80)
    terminal = run_terminal.Terminal(1, pty, asyncio.get_running_loop())
    pty.out.put("a")
    pty.out.put("b")
    assert await terminal.output() == "a" and await terminal.output() == "b"
    terminal.write("x")
    terminal.resize(10, 20)
    assert await asyncio.to_thread(pty.resized.wait, 15)
    assert await terminal.kill() == 1
    assert await terminal.output() is None and pty.closed and pty.written == ["x"]
    assert pty.sizes[-1] == (10, 20)
