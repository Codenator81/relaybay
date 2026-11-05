"""The program without a GUI: single instance over IPC, the embedded server, the controller's
Restart and Quit, the tray menu model and the log file."""

import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from relaybay.config import load_settings
from relaybay.desktop import single_instance
from relaybay.desktop.app import DesktopController
from relaybay.desktop.autostart import Autostart
from relaybay.desktop.server_thread import EmbeddedServer, PortTaken, healthz
from relaybay.desktop.tray import SEPARATOR, MenuState, build_menu
from relaybay.logs import REDACTED, desktop_log_config


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- single instance --------------------------------------------------------

HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from relaybay.desktop import single_instance

    folder = Path(sys.argv[1])
    lock = single_instance.acquire(folder)
    assert lock is not None
    done = []
    marker = folder / "raised"
    server = single_instance.serve_raise(
        folder, lambda: marker.write_text("yes"), lambda: done.append(1)
    )
    print("ready", flush=True)
    deadline = time.monotonic() + 30
    while not done and time.monotonic() < deadline:
        time.sleep(0.05)
    server.close()
    lock.release()
    """
)


def test_second_start_raises_the_first_over_ipc(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        assert single_instance.acquire(tmp_path) is None  # held by the other process
        assert single_instance.notify_running(tmp_path) is True
        marker = tmp_path / "raised"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # The holder may have created the file without its text yet.
            if marker.exists() and marker.read_text() == "yes":
                break
            time.sleep(0.05)
        assert marker.read_text() == "yes"
        assert single_instance.notify_running(tmp_path, single_instance.QUIT) is True
        assert holder.wait(timeout=15) == 0
    finally:
        holder.kill()
    lock = single_instance.acquire(tmp_path)  # released with the process
    assert lock is not None
    lock.release()
    assert not (tmp_path / single_instance.IPC_FILE).exists()
    assert single_instance.notify_running(tmp_path) is False


def _ipc_port(folder: Path) -> int:
    return json.loads((folder / single_instance.IPC_FILE).read_text(encoding="utf-8"))["port"]


def wait_for(condition, seconds: float = 5) -> bool:
    """The handler runs after the answer, on the connection's thread: poll for its effect."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def _raise_within(folder: Path, seconds: float) -> bool:
    started = time.monotonic()
    result = single_instance.notify_running(folder, timeout=seconds)
    assert time.monotonic() - started < seconds + 1
    return result


def test_ipc_survives_peers_that_close_or_half_close(tmp_path: Path) -> None:
    raised: list[int] = []
    server = single_instance.serve_raise(tmp_path, lambda: raised.append(1), lambda: None)
    try:
        port = _ipc_port(tmp_path)
        socket.create_connection(("127.0.0.1", port)).close()
        half = socket.create_connection(("127.0.0.1", port))
        half.shutdown(socket.SHUT_WR)
        time.sleep(0.3)
        assert _raise_within(tmp_path, 3) is True
        half.close()
        assert wait_for(lambda: raised == [1]) and server._thread.is_alive()
    finally:
        server.close()


def test_ipc_answers_while_a_peer_stalls(tmp_path: Path) -> None:
    raised: list[int] = []
    server = single_instance.serve_raise(tmp_path, lambda: raised.append(1), lambda: None)
    try:
        stall = socket.create_connection(("127.0.0.1", _ipc_port(tmp_path)))
        time.sleep(0.2)
        assert _raise_within(tmp_path, 3) is True
        stall.close()
        assert wait_for(lambda: raised == [1])
    finally:
        server.close()


def test_ipc_survives_a_failing_handler(tmp_path: Path) -> None:
    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise ValueError("No token for the external server")

    server = single_instance.serve_raise(tmp_path, boom, lambda: None)
    try:
        assert _raise_within(tmp_path, 3) is True
        time.sleep(0.2)
        assert _raise_within(tmp_path, 3) is True
        assert wait_for(lambda: calls == [1, 1]) and server._thread.is_alive()
    finally:
        server.close()


def test_notify_never_hangs_on_a_mute_listener(tmp_path: Path) -> None:
    with socket.socket() as mute:
        mute.bind(("127.0.0.1", 0))
        mute.listen(1)
        (tmp_path / single_instance.IPC_FILE).write_text(
            json.dumps({"port": mute.getsockname()[1], "key": "00" * 32}), encoding="utf-8"
        )
        assert _raise_within(tmp_path, 1) is False
        assert single_instance.notify_running(tmp_path, single_instance.QUIT, 1) is False


def test_stale_ipc_file_answers_false(tmp_path: Path) -> None:
    port = free_port()
    (tmp_path / single_instance.IPC_FILE).write_text(
        f'{{"port": {port}, "key": "00"}}', encoding="utf-8"
    )
    assert single_instance.notify_running(tmp_path, timeout=1) is False


def test_wrong_key_is_refused(tmp_path: Path) -> None:
    raised: list[int] = []
    server = single_instance.serve_raise(tmp_path, lambda: raised.append(1), lambda: None)
    try:
        path = tmp_path / single_instance.IPC_FILE
        good = path.read_text(encoding="utf-8")
        port = good.split('"port": ')[1].split(",")[0]
        path.write_text(f'{{"port": {port}, "key": "{"11" * 32}"}}', encoding="utf-8")
        assert single_instance.notify_running(tmp_path, timeout=1) is False
        path.write_text(good, encoding="utf-8")
        assert single_instance.notify_running(tmp_path) is True
        assert wait_for(lambda: raised == [1])
    finally:
        server.close()


# --- embedded server --------------------------------------------------------


def test_embedded_server_start_stop_restart(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, port=free_port(), environ={}, serving=True)
    server = EmbeddedServer(settings)
    server.start()
    try:
        assert server._thread is not None and server._thread.daemon is False
        health = healthz(settings.base_url)
        assert health is not None and health["status"] == "ok"
        assert server.stop() is True
        assert healthz(settings.base_url) is None
        server.start()  # the same port again right away
        assert healthz(settings.base_url) is not None
    finally:
        assert server.stop() is True
    assert not server.running


def test_port_taken_by_another_relaybay(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, port=free_port(), environ={}, serving=True)
    first = EmbeddedServer(settings)
    first.start()
    try:
        with pytest.raises(PortTaken) as caught:
            EmbeddedServer(settings).start()
        assert caught.value.relaybay is not None
        assert "used by another relaybay (version" in str(caught.value)
    finally:
        first.stop()


def test_port_taken_by_another_program(tmp_path: Path) -> None:
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = other.getsockname()[1]
        settings = load_settings(home=tmp_path, port=port, environ={}, serving=True)
        with pytest.raises(PortTaken, match="used by another program"):
            EmbeddedServer(settings).start()


# --- controller -------------------------------------------------------------


@pytest.fixture
def controller(
    tmp_path: Path, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> DesktopController:
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("RELAYBAY_PORT", str(free_port()))
    ctl = DesktopController(desktop_config_dir)
    ctl.log_config = lambda: None  # leave pytest's logging alone
    ctl.autostart = Autostart("linux", tmp_path / "home")
    ctl.load()
    return ctl


def test_controller_restart_and_quit(controller: DesktopController) -> None:
    assert controller.problem is None
    controller._start_and_load()
    assert controller.settings is not None
    base = controller.settings.base_url
    first = healthz(base)
    assert first is not None
    assert controller.browser_url() == f"{base}/login?token={controller.token()}"
    assert controller.board_url().endswith("&next=/")
    controller.restart()
    assert controller.server is not None and controller.server.running
    second = healthz(base)
    assert second is not None and second["token_id"] == first["token_id"]
    controller.quit()
    assert controller.quitting and controller.server is None
    assert healthz(base) is None
    controller.quit()  # twice is harmless


def test_restart_after_quit_starts_nothing(controller: DesktopController) -> None:
    controller._start_and_load()
    controller.quit()
    controller.restart()
    assert controller.server is None


def test_quit_waits_for_a_restart_in_progress(
    controller: DesktopController, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller._start_and_load()
    assert controller.settings is not None
    base = controller.settings.base_url
    load = controller.load
    entered = threading.Event()

    def slow_load() -> None:
        entered.set()
        time.sleep(1)
        load()

    monkeypatch.setattr(controller, "load", slow_load)
    restart = threading.Thread(target=controller.restart)
    restart.start()
    assert entered.wait(10)
    controller.quit()  # arrives while the restart holds its lock
    restart.join(30)
    assert controller.server is None
    assert healthz(base) is None


def test_load_logs_clear_text_external_server(
    controller: DesktopController, desktop_config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from relaybay.desktop.settings import DesktopSettings, save_desktop_settings

    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:8765")
    )
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        controller.load()
    assert "travels in clear text" in caplog.text


def test_controller_reports_a_bad_desktop_toml(
    controller: DesktopController, desktop_config_dir: Path
) -> None:
    (desktop_config_dir / "desktop.toml").write_text('server_mode = "x"\n', encoding="utf-8")
    controller.load()
    assert controller.problem is not None and "server_mode" in controller.problem
    controller._start_and_load()  # shows the problem, starts nothing
    assert controller.server is None


def test_controller_toggles_autostart(controller: DesktopController) -> None:
    from relaybay.desktop.settings import load_desktop_settings

    controller.toggle_autostart()
    assert controller.autostart.is_enabled()
    assert load_desktop_settings(controller.config_dir).autostart is True
    controller.toggle_autostart()
    assert not controller.autostart.is_enabled()
    assert load_desktop_settings(controller.config_dir).autostart is False


# --- tray menu --------------------------------------------------------------


def _flat(entries) -> list[tuple[str, str | None, bool | None]]:
    items = []
    for entry in entries:
        items.append((entry.text, entry.action, entry.checked))
        items += _flat(entry.submenu)
    return items


def test_menu_from_state() -> None:
    entries = build_menu(MenuState(server_mode="embedded", autostart=True, has_window=True))
    texts = [e.text for e in entries]
    assert texts == [
        "Open board",
        "Open in browser",
        SEPARATOR,
        "Restart server",
        "Logs",
        "Settings",
        SEPARATOR,
        "Quit relaybay",
    ]
    assert entries[0].default
    flat = _flat(entries)
    assert ("Start at login", "toggle_autostart", True) in flat
    assert not [t for t, _a, _c in flat if "update" in t.lower()]  # update items come later
    external = build_menu(MenuState(server_mode="external", autostart=False, has_window=False))
    assert "Reconnect" in [e.text for e in external]
    assert ("Start at login", "toggle_autostart", False) in _flat(external)
    actions = {a for _t, a, _c in flat if a}
    assert actions <= {name for name in dir(DesktopController) if not name.startswith("_")}


def test_menu_converts_to_pystray() -> None:
    pystray = pytest.importorskip("pystray", reason="the desktop extra")
    from relaybay.desktop.tray import to_pystray

    called: list[str] = []
    menu = to_pystray(
        build_menu(MenuState("embedded", autostart=True, has_window=True)), called.append
    )
    assert isinstance(menu, pystray.Menu)
    first = next(iter(menu.items))
    assert first.text == "Open board" and first.default


# --- log file ---------------------------------------------------------------


LOGGING = textwrap.dedent(
    """
    import logging, logging.config, sys
    from pathlib import Path
    from relaybay.logs import desktop_log_config

    logging.config.dictConfig(desktop_log_config(Path(sys.argv[1])))
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", "/login?token=secret", "1.1", 303
    )
    logging.getLogger("relaybay.desktop").info("quit")
    """
)


def test_desktop_log_file_redacts_the_login_token(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "relaybay.log"
    config = desktop_log_config(path)
    assert config["handlers"]["file"]["maxBytes"] == 1024 * 1024
    assert config["handlers"]["file"]["backupCount"] == 4
    subprocess.run([sys.executable, "-c", LOGGING, str(path)], check=True, capture_output=True)
    text = path.read_text(encoding="utf-8")
    assert "secret" not in text and REDACTED in text
    assert "relaybay.desktop: quit" in text


# --- the real program -------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="opens a real window; Windows only here")
def test_desktop_exit_after_runs_the_window(tmp_path: Path) -> None:
    pytest.importorskip("webview", reason="the desktop extra")
    port = free_port()
    env = {
        **os.environ,
        "RELAYBAY_HOME": str(tmp_path / "data"),
        "RELAYBAY_PORT": str(port),
        "RELAYBAY_CONFIG_DIR": str(tmp_path / "config"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "relaybay", "desktop", "--exit-after", "3"],
        env=env,
    )
    try:
        seen = None
        deadline = time.monotonic() + 20
        while seen is None and process.poll() is None and time.monotonic() < deadline:
            seen = healthz(f"http://127.0.0.1:{port}")
            time.sleep(0.2)
        assert seen is not None, "/healthz never answered while the program ran"
        assert process.wait(timeout=30) == 0
    finally:
        process.kill()
    assert healthz(f"http://127.0.0.1:{port}") is None
    assert (tmp_path / "data" / "logs" / "relaybay.log").is_file()


# --- icons ------------------------------------------------------------------


def test_tray_icon_follows_the_panel_theme() -> None:
    from importlib.resources import files

    from relaybay.desktop.tray import icon_file

    assert icon_file(None, small=False) == "relaybay.png"
    assert icon_file("light", small=False) == "tray-light.png"
    assert icon_file("dark", small=True) == "tray-dark-16.png"
    icons = files("relaybay.desktop").joinpath("icons")
    for theme in (None, "light", "dark"):
        for small in (False, True):
            assert icons.joinpath(icon_file(theme, small)).is_file()


def test_favicon_is_the_png(client) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
