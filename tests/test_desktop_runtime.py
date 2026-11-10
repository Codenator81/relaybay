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
import types
from datetime import datetime
from pathlib import Path

import pytest

from relaybay.config import load_settings
from relaybay.desktop import app, single_instance, start_journal, tray, window, window_state
from relaybay.desktop.app import STARTING_PAGE, DesktopController, _page
from relaybay.desktop.autostart import Autostart
from relaybay.desktop.server_thread import EmbeddedServer, PortTaken, healthz
from relaybay.desktop.tray import SEPARATOR, MenuState, build_menu
from relaybay.desktop.window_state import WindowState, WindowTracker
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


@pytest.mark.skipif(sys.platform != "win32", reason="the installer's AppMutex is Windows only")
def test_app_mutex_is_visible_to_the_installer() -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    single_instance.hold_app_mutex()
    handle = kernel32.OpenMutexW(0x00100000, False, single_instance.APP_MUTEX)  # SYNCHRONIZE
    assert handle
    kernel32.CloseHandle(ctypes.c_void_p(handle))


@pytest.mark.skipif(sys.platform != "win32", reason="the installer's mutex is Windows only")
def test_a_failed_mutex_is_a_warning_not_a_failed_start(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import ctypes

    kernel32 = types.SimpleNamespace(CreateMutexW=lambda *args: 0)  # NULL: the call failed
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32)
    with caplog.at_level(logging.WARNING, logger=single_instance.__name__):
        single_instance.hold_app_mutex()
    assert "could not create the mutex" in caplog.text


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
        "Open Inbox",
        SEPARATOR,
        "Quiet notifications",
        "Restart server",
        "Logs",
        "Settings",
        SEPARATOR,
        "Quit relaybay",
    ]
    assert entries[0].default
    flat = _flat(entries)
    assert ("Start at login", "toggle_autostart", True) in flat
    assert ("Check for updates", "check_updates", None) in flat
    assert not [a for _t, a, _c in flat if a == "install_update"]  # only with a newer release
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
    journal = (tmp_path / "config" / start_journal.FILE_NAME).read_text(encoding="utf-8")
    last = start_journal.last_start(journal)
    assert last is not None and "--exit-after" in last.start
    assert last.outcomes[0].startswith("started, log ") and last.outcomes[-1] == "exit 0"


# --- the machine's theme ----------------------------------------------------


@pytest.mark.parametrize("page", [STARTING_PAGE, _page("relaybay cannot show", "<p>why</p>")])
def test_pages_set_colours_for_both_schemes(page: str) -> None:
    light, dark = page.split("@media (prefers-color-scheme:dark)")
    assert "color-scheme:light dark" in light
    assert window.LIGHT_BACKGROUND in light and window.LIGHT_TEXT in light
    assert window.DARK_BACKGROUND in dark and window.DARK_TEXT in dark


def test_window_background_follows_the_apps_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    for theme, colour in (
        ("dark", window.DARK_BACKGROUND),
        ("light", window.LIGHT_BACKGROUND),
        (None, window.LIGHT_BACKGROUND),
    ):
        monkeypatch.setattr(window, "app_theme", lambda theme=theme: theme)
        assert window.background_color() == colour


class FakeUxtheme:
    def __init__(self, missing: int | None = None) -> None:
        self.calls: list[tuple] = []
        self.missing = missing

    def __getitem__(self, ordinal: int):
        if ordinal == self.missing:
            raise AttributeError(f"function ordinal {ordinal} not found")
        return lambda *args: self.calls.append((ordinal, *args))


def test_menu_theme_sets_the_mode_then_flushes() -> None:
    uxtheme = FakeUxtheme()
    loaded = []

    def load(name: str) -> FakeUxtheme:
        loaded.append(name)
        return uxtheme

    tray.follow_system_menu_theme(lambda: tray.DARK_MENUS_MIN_BUILD, load, lambda: "dark")
    assert loaded == ["uxtheme"]
    assert uxtheme.calls == [(135, tray.APP_MODE_FORCE_DARK), (136,)]


@pytest.mark.parametrize("theme", ["light", None])
def test_menu_theme_leaves_a_light_machine_alone(theme: str | None) -> None:
    def load(name: str) -> None:
        raise AssertionError("no dark mode on a light machine")

    tray.follow_system_menu_theme(lambda: tray.DARK_MENUS_MIN_BUILD, load, lambda: theme)


def test_menu_theme_leaves_an_old_build_alone() -> None:
    def load(name: str) -> None:
        raise AssertionError("ordinal 135 means something else before build 18362")

    tray.follow_system_menu_theme(lambda: tray.DARK_MENUS_MIN_BUILD - 1, load, lambda: "dark")


def test_menu_theme_survives_any_failure(caplog: pytest.LogCaptureFixture) -> None:
    def no_library(name: str) -> None:
        raise OSError("no uxtheme")

    def no_version() -> int:
        raise AttributeError("getwindowsversion")

    with caplog.at_level(logging.DEBUG, logger="relaybay.desktop"):
        dark = lambda: "dark"  # noqa: E731
        tray.follow_system_menu_theme(lambda: 22000, no_library, dark)
        tray.follow_system_menu_theme(no_version, no_library, dark)
        tray.follow_system_menu_theme(lambda: 22000, lambda name: FakeUxtheme(missing=135), dark)
        partial = FakeUxtheme(missing=136)
        tray.follow_system_menu_theme(lambda: 22000, lambda name: partial, dark)
    assert partial.calls == [(135, tray.APP_MODE_FORCE_DARK)]
    assert caplog.text.count("the tray menu stays light") == 4


# --- the window's state -----------------------------------------------------

SCREENS = [types.SimpleNamespace(x=0, y=0, width=1920, height=1080)]


def write_state(folder: Path, **values) -> None:
    (folder / window_state.FILE_NAME).write_text(json.dumps(values), encoding="utf-8")


def test_state_missing_file_means_the_defaults(tmp_path: Path) -> None:
    state = window_state.load_state(tmp_path, SCREENS)
    assert state == WindowState(width=1280, height=860, x=None, y=None, maximized=False)


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"\xff\xfe\x00", b"[1, 2]", b"null", b""],
    ids=["broken", "not utf-8", "a list", "null", "empty"],
)
def test_state_corrupt_file_means_the_defaults(tmp_path: Path, content: bytes) -> None:
    (tmp_path / window_state.FILE_NAME).write_bytes(content)
    assert window_state.load_state(tmp_path, SCREENS) == WindowState()


def test_state_bad_values_are_left_out(tmp_path: Path) -> None:
    write_state(tmp_path, width="wide", height=True, x=10**12, y=5, maximized="yes")
    assert window_state.load_state(tmp_path, SCREENS) == WindowState()


def test_state_too_small_is_raised_to_the_minimum(tmp_path: Path) -> None:
    write_state(tmp_path, width=100, height=-5)
    state = window_state.load_state(tmp_path, SCREENS)
    assert (state.width, state.height) == window_state.MIN_SIZE


def test_state_larger_than_every_screen_is_lowered(tmp_path: Path) -> None:
    write_state(tmp_path, width=3000, height=2000, x=0, y=0)
    state = window_state.load_state(tmp_path, SCREENS)
    assert (state.width, state.height) == (1920, 1080)
    second = types.SimpleNamespace(x=1920, y=0, width=2560, height=1440)
    state = window_state.load_state(tmp_path, [*SCREENS, second])
    assert (state.width, state.height) == (2560, 1440)
    state = window_state.load_state(tmp_path, [])
    assert (state.width, state.height) == (3000, 2000)


def test_state_round_trip(tmp_path: Path) -> None:
    saved = WindowState(width=1000, height=700, x=-8, y=20, maximized=True)
    window_state.save_state(tmp_path, saved)
    assert window_state.load_state(tmp_path, SCREENS) == saved


@pytest.mark.parametrize(
    "x, y", [(4000, 100), (-3000, 100), (100, 2000), (-32000, -32000)], ids=str
)
def test_state_off_every_screen_is_centred(tmp_path: Path, x: int, y: int) -> None:
    write_state(tmp_path, width=1000, height=700, x=x, y=y, maximized=True)
    state = window_state.load_state(tmp_path, SCREENS)
    assert state == WindowState(width=1000, height=700, x=None, y=None, maximized=True)


def test_state_on_a_second_screen_stays(tmp_path: Path) -> None:
    write_state(tmp_path, width=1000, height=700, x=2000, y=100)
    screens = [*SCREENS, types.SimpleNamespace(x=1920, y=0, width=1920, height=1080)]
    assert window_state.load_state(tmp_path, screens).x == 2000
    assert window_state.load_state(tmp_path, SCREENS).x is None
    assert window_state.load_state(tmp_path, []).x is None


def test_state_half_a_position_is_centred(tmp_path: Path) -> None:
    write_state(tmp_path, x=100)
    state = window_state.load_state(tmp_path, SCREENS)
    assert state.x is None and state.y is None


def test_state_save_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window_state.save_state(tmp_path, WindowState(width=1000))

    def refuse(self: Path, target: Path) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            window_state.save_state(tmp_path, WindowState(width=2000))
    assert window_state.load_state(tmp_path, SCREENS).width == 1000
    assert [p.name for p in tmp_path.iterdir()] == [window_state.FILE_NAME]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def tracker(saved: WindowState | None = None) -> tuple[WindowTracker, Clock]:
    clock = Clock()
    return WindowTracker(saved or WindowState(width=1000, height=700, x=50, y=60), clock), clock


def test_tracker_follows_a_normal_window() -> None:
    track, clock = tracker()
    track.on_moved(10, 20)
    track.on_resized(900.0, 650.0)  # Cocoa reports floats
    clock.now += 5
    track.on_moved(30, 40)
    assert track.snapshot() == WindowState(width=900, height=650, x=30, y=40, maximized=False)


@pytest.mark.parametrize(
    "order",
    [
        ("moved", "maximized", "resized"),
        ("resized", "moved", "maximized"),
        ("maximized", "resized", "moved"),
    ],
    ids="-".join,
)
def test_tracker_maximize_keeps_the_normal_geometry_in_any_order(order: tuple) -> None:
    # Each event has its own thread, so the WinForms order Move, maximized, Resize can arrive mixed.
    track, _clock = tracker()
    events = {
        "moved": lambda: track.on_moved(-8, -8),
        "maximized": track.on_maximized,
        "resized": lambda: track.on_resized(1936, 1048),
    }
    for name in order:
        events[name]()
    assert track.snapshot() == WindowState(width=1000, height=700, x=50, y=60, maximized=True)


def test_tracker_minimize_keeps_the_normal_geometry() -> None:
    track, _clock = tracker()
    track.on_moved(-32000, -32000)
    track.on_minimized()
    track.on_resized(160, 28)
    assert track.minimized
    assert track.snapshot() == WindowState(width=1000, height=700, x=50, y=60, maximized=False)


def test_tracker_keeps_geometry_older_than_the_maximize() -> None:
    track, clock = tracker()
    track.on_moved(300, 200)
    clock.now += window_state.SETTLE + 0.1
    track.on_maximized()
    assert track.snapshot().x == 300


def test_tracker_restore_from_maximized_goes_back_to_following() -> None:
    track, clock = tracker(WindowState(width=1000, height=700, x=50, y=60, maximized=True))
    track.on_resized(1936, 1048)  # the first show of a window saved as maximized
    assert track.snapshot() == WindowState(width=1000, height=700, x=50, y=60, maximized=True)
    track.on_restored()
    track.on_resized(1000, 700)
    clock.now += 5
    track.on_moved(70, 80)
    assert track.snapshot() == WindowState(width=1000, height=700, x=70, y=80, maximized=False)


def test_tracker_minimized_to_maximized_without_restored() -> None:
    # WinForms: un-minimizing a maximized window raises `maximized`, never `restored`.
    track, _clock = tracker()
    track.on_maximized()
    track.on_minimized()
    assert track.minimized and track.maximized
    assert track.snapshot().maximized is True  # closed while minimized: comes back maximized
    track.on_maximized()
    assert not track.minimized and track.maximized


def test_tracker_restored_from_minimized_keeps_maximized() -> None:
    # GTK: un-minimizing a maximized window raises `restored`.
    track, _clock = tracker()
    track.on_maximized()
    track.on_minimized()
    track.on_restored()
    assert not track.minimized and track.maximized


def test_tracker_minimized_then_restored_is_normal_again() -> None:
    track, clock = tracker()
    track.on_minimized()
    track.on_restored()
    assert not track.minimized and not track.maximized
    clock.now += 5
    track.on_moved(1, 2)
    assert track.snapshot().x == 1


class FakeEvent(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[str] = []
        names = ("closing", "minimized", "maximized", "restored", "moved", "resized")
        self.events = types.SimpleNamespace(**{name: FakeEvent() for name in names})

    def show(self) -> None:
        self.calls.append("show")

    def hide(self) -> None:
        self.calls.append("hide")

    def restore(self) -> None:
        self.calls.append("restore")

    def maximize(self) -> None:
        self.calls.append("maximize")


@pytest.mark.parametrize(
    "events, calls",
    [
        ([], ["show"]),
        (["on_maximized"], ["show"]),
        (["on_minimized"], ["show", "restore"]),
        (["on_maximized", "on_minimized"], ["show", "maximize"]),
    ],
    ids=["normal", "maximized", "minimized", "maximized then minimized"],
)
def test_show_keeps_the_state(
    events: list[str], calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(window, "RESTORE_UNMAXIMIZES", True)
    track, _clock = tracker()
    for event in events:
        getattr(track, event)()
    fake = FakeWindow()
    window.show(fake, track)
    assert fake.calls == calls


@pytest.mark.parametrize("events", [[], ["on_maximized"], ["on_minimized"]], ids=str)
def test_show_raises_through_restore_outside_windows(
    events: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(window, "RESTORE_UNMAXIMIZES", False)
    track, _clock = tracker()
    for event in events:
        getattr(track, event)()
    fake = FakeWindow()
    window.show(fake, track)
    assert fake.calls == ["show", "restore"]


@pytest.fixture
def fake_webview(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    fake = types.SimpleNamespace(screens=SCREENS, created=[], windows=[])

    def create_window(title: str, **kwargs) -> FakeWindow:
        created = FakeWindow()
        fake.created.append(kwargs)
        return created

    fake.create_window = create_window
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(window, "app_theme", lambda: "dark")
    return fake


def test_create_window_uses_the_saved_state(fake_webview: types.SimpleNamespace) -> None:
    saved = WindowState(width=1000, height=700, x=50, y=60, maximized=True)
    window.create_window("<p>", True, lambda: True, WindowTracker(saved), lambda: None)
    args = fake_webview.created[0]
    assert (args["width"], args["height"], args["x"], args["y"]) == (1000, 700, 50, 60)
    assert args["maximized"] is True and args["hidden"] is True
    assert args["min_size"] == window_state.MIN_SIZE
    assert args["background_color"] == window.DARK_BACKGROUND


class BrokenScreens:
    def __iter__(self):
        raise RuntimeError("no display")


def test_saved_state_survives_a_screens_failure(
    tmp_path: Path, fake_webview: types.SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    write_state(tmp_path, width=1000, height=700, x=50, y=60, maximized=True)
    fake_webview.screens = BrokenScreens()
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        assert window.saved_state(tmp_path) == WindowState()
    assert "no display" in caplog.text


def test_saved_state_survives_a_deeply_nested_file(
    tmp_path: Path, fake_webview: types.SimpleNamespace
) -> None:
    (tmp_path / window_state.FILE_NAME).write_text("[" * 200_000, encoding="utf-8")
    assert window.saved_state(tmp_path) == WindowState()


@pytest.mark.parametrize(
    "keeps_maximized, keeps_position, expected",
    [
        (True, True, WindowState(width=1000, height=700, x=50, y=60, maximized=True)),
        (False, True, WindowState(width=1000, height=700, x=50, y=60, maximized=False)),
        (True, False, WindowState(width=1000, height=700, x=None, y=None, maximized=True)),
    ],
    ids=["windows", "macos", "linux"],
)
def test_saved_state_per_system(
    tmp_path: Path,
    fake_webview: types.SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    keeps_maximized: bool,
    keeps_position: bool,
    expected: WindowState,
) -> None:
    monkeypatch.setattr(window, "KEEPS_MAXIMIZED", keeps_maximized)
    monkeypatch.setattr(window, "KEEPS_POSITION", keeps_position)
    write_state(tmp_path, width=1000, height=700, x=50, y=60, maximized=True)
    assert window.saved_state(tmp_path) == expected


def test_quit_saves_the_window_state_before_destroying(
    tmp_path: Path, fake_webview: types.SimpleNamespace
) -> None:
    seen: list[bool] = []

    class Destroyed:
        def destroy(self) -> None:
            seen.append((tmp_path / window_state.FILE_NAME).exists())

    fake_webview.windows = [Destroyed()]
    ctl = DesktopController(tmp_path)
    ctl.has_window = True
    ctl.window = object()
    ctl.quit()
    assert seen == [True]


def test_saved_state_reads_this_machines_screens(
    tmp_path: Path, fake_webview: types.SimpleNamespace
) -> None:
    write_state(tmp_path, width=1000, height=700, x=4000, y=100)
    state = window.saved_state(tmp_path)
    assert state.width == 1000 and state.x is None


def test_closing_saves_then_hides_or_lets_through(fake_webview: types.SimpleNamespace) -> None:
    saves: list[int] = []
    staying = [True]
    created = window.create_window(
        "<p>", False, lambda: staying[0], WindowTracker(WindowState()), lambda: saves.append(1)
    )
    closing = created.events.closing[0]
    assert closing() is False and created.calls == ["hide"] and len(saves) == 1
    staying[0] = False
    assert closing() is True and created.calls == ["hide"] and len(saves) == 2


def test_window_events_reach_the_tracker(fake_webview: types.SimpleNamespace) -> None:
    track, _clock = tracker()
    created = window.create_window("<p>", False, lambda: True, track, lambda: None)
    created.events.maximized[0]()
    assert track.maximized
    created.events.restored[0]()
    created.events.moved[0](7, 8)
    created.events.resized[0](900, 650)
    assert track.snapshot() == WindowState(width=900, height=650, x=7, y=8, maximized=False)
    created.events.minimized[0]()
    assert track.minimized


def test_controller_saves_the_window_state(tmp_path: Path) -> None:
    ctl = DesktopController(tmp_path)
    ctl.save_window_state()  # no window yet: a saved file must not be overwritten by defaults
    assert not (tmp_path / window_state.FILE_NAME).exists()
    ctl.window = object()
    ctl.window_tracker = WindowTracker(WindowState(width=1000, height=700, x=5, y=6))
    ctl.save_window_state()
    saved = json.loads((tmp_path / window_state.FILE_NAME).read_text(encoding="utf-8"))
    assert saved == {"x": 5, "y": 6, "width": 1000, "height": 700, "maximized": False}


def test_a_failed_save_does_not_stop_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ctl = DesktopController(tmp_path)
    ctl.window = object()

    def refuse(folder: Path, state: WindowState) -> None:
        raise OSError("read-only")

    monkeypatch.setattr("relaybay.desktop.app.save_state", refuse)
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        ctl.save_window_state()
    assert "the window state was not saved: read-only" in caplog.text


# --- the window after an update -----------------------------------------------


def test_reopen_marker_is_one_shot(tmp_path: Path) -> None:
    assert window_state.take_reopen(tmp_path, 1000.0) is False
    window_state.mark_reopen(tmp_path, 1000.0)
    assert (tmp_path / window_state.REOPEN_FILE).is_file()
    assert window_state.take_reopen(tmp_path, 1000.0 + window_state.REOPEN_MAX_AGE) is True
    assert window_state.take_reopen(tmp_path, 1000.0) is False


@pytest.mark.parametrize("content", ["1000.0", "garbage", ""], ids=["old", "garbage", "empty"])
def test_an_old_or_unreadable_marker_is_dropped(tmp_path: Path, content: str) -> None:
    (tmp_path / window_state.REOPEN_FILE).write_text(content, encoding="utf-8")
    later = 1000.0 + window_state.REOPEN_MAX_AGE + 1  # the next morning's logon start, say
    assert window_state.take_reopen(tmp_path, later) is False
    assert not (tmp_path / window_state.REOPEN_FILE).exists()


def test_the_tracker_knows_whether_the_window_is_on_screen(
    fake_webview: types.SimpleNamespace,
) -> None:
    track, _clock = tracker()
    created = window.create_window("<p>", True, lambda: True, track, lambda: None)
    assert track.visible is False
    window.show(created, track)
    assert track.visible is True
    created.events.closing[0]()  # closed to the tray
    assert track.visible is False
    window.create_window("<p>", False, lambda: True, track, lambda: None)
    assert track.visible is True


@pytest.mark.parametrize(
    "started_ago, visible, marked",
    [
        (5.0, True, True),
        (5.0, False, False),
        (None, True, False),
        (app.INSTALLER_QUIT_WAIT + 1, True, False),
    ],
    ids=[
        "update with the window open",
        "update from the tray",
        "plain quit",
        "quit long after a cancelled setup",
    ],
)
def test_quit_marks_the_reopen_only_for_an_update_with_the_window_open(
    tmp_path: Path,
    fake_webview: types.SimpleNamespace,
    started_ago: float | None,
    visible: bool,
    marked: bool,
) -> None:
    ctl = DesktopController(tmp_path)
    ctl.has_window = True
    ctl.window = object()
    ctl.window_tracker.visible = visible
    ctl.clock = lambda: 1000.0
    ctl.installer_started_at = None if started_ago is None else 1000.0 - started_ago
    ctl.quit()
    assert (tmp_path / window_state.REOPEN_FILE).exists() is marked


class TrayIcon:
    def run(self, setup: object = None) -> None:
        pass

    def run_detached(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.mark.parametrize(
    "background, marker, hidden",
    [(True, True, False), (True, False, True), (False, True, False)],
    ids=["background after an update", "background at logon", "plain start"],
)
def test_a_start_takes_the_marker_and_shows_the_window_once(
    controller: DesktopController,
    desktop_config_dir: Path,
    fake_webview: types.SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
    marker: bool,
    hidden: bool,
) -> None:
    fake_webview.start = lambda *args, **kwargs: None
    monkeypatch.setattr(window, "window_available", lambda: True)
    monkeypatch.setattr(tray, "create_icon", lambda entries, dispatch: TrayIcon())
    if marker:
        window_state.mark_reopen(desktop_config_dir, time.time())
    controller.background = background
    assert controller.run() == 0
    assert fake_webview.created[0]["hidden"] is hidden
    assert not (desktop_config_dir / window_state.REOPEN_FILE).exists()


def test_a_marker_that_cannot_be_removed_keeps_the_start_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("in use")

    window_state.mark_reopen(tmp_path, time.time())
    monkeypatch.setattr(Path, "unlink", refuse)
    ctl = DesktopController(tmp_path, background=True)
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        assert ctl.take_reopen() is False
    assert "staying hidden: in use" in caplog.text


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


# --- start journal ------------------------------------------------------------


def journal_lines(folder: Path) -> list[str]:
    return (folder / start_journal.FILE_NAME).read_text(encoding="utf-8").splitlines()


def messages(folder: Path) -> list[str]:
    return [m.group(3) for m in map(start_journal.LINE.match, journal_lines(folder)) if m]


def test_journal_start_line_names_the_process(desktop_config_dir: Path) -> None:
    journal = start_journal.StartJournal(lambda: desktop_config_dir)
    journal.start("9.9.9")
    journal.exit(0)
    first, last = journal_lines(desktop_config_dir)
    match = start_journal.LINE.match(first)
    assert match is not None and match.group(2) == str(os.getpid())
    assert datetime.fromisoformat(match.group(1)).tzinfo is not None
    start = match.group(3)
    assert start.startswith("start relaybay 9.9.9 argv=[")
    assert f"cwd={Path.cwd()}" in start and f"executable={sys.executable}" in start
    assert "frozen=False" in start
    assert last.endswith(f"[{os.getpid()}] exit 0")


def test_journal_extra_missing(desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "extra_installed", lambda: False)
    assert app.run() == 2
    assert messages(desktop_config_dir)[1:] == ["extra missing", "exit 2"]


def test_journal_lock_held_without_an_answer(
    desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "extra_installed", lambda: True)
    lock = single_instance.acquire(desktop_config_dir)
    assert lock is not None
    try:
        assert app.run() == 1
    finally:
        lock.release()
    assert messages(desktop_config_dir)[1:] == ["lock held, no answer (no desktop.ipc)", "exit 1"]


def test_journal_another_instance_answered(
    desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "extra_installed", lambda: True)
    raised = threading.Event()
    lock = single_instance.acquire(desktop_config_dir)
    assert lock is not None
    server = single_instance.serve_raise(desktop_config_dir, raised.set, lambda: None)
    try:
        assert app.run(background=True) == 0
    finally:
        server.close()
        lock.release()
    assert raised.wait(5)
    assert messages(desktop_config_dir)[1:] == ["another instance answered", "exit 0"]


def test_journal_keeps_the_exception_and_reraises(
    desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(folder: Path) -> None:
        raise PermissionError(13, "Access is denied", str(folder))

    monkeypatch.setattr(app, "extra_installed", lambda: True)
    monkeypatch.setattr(single_instance, "acquire", refuse)
    with pytest.raises(PermissionError):
        app.run(background=True)
    lines = journal_lines(desktop_config_dir)
    assert messages(desktop_config_dir)[1].startswith("exception: PermissionError: [Errno 13]")
    assert lines[2] == "  Traceback (most recent call last):"
    assert all(line.startswith("  ") for line in lines[2:])
    assert "in refuse" in "\n".join(lines)
    last = start_journal.last_start("\n".join(lines))
    assert last is not None and last.failed


def test_journal_started_after_logging(
    controller: DesktopController, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Icon:
        def run(self, setup: object = None) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(window, "window_available", lambda: False)
    monkeypatch.setattr(tray, "create_icon", lambda entries, dispatch: Icon())
    controller.journal = start_journal.StartJournal(lambda: desktop_config_dir)
    try:
        assert controller.run() == 0
    finally:
        controller.quit()
    assert messages(desktop_config_dir) == [f"started, log {controller.log_path}"]


def test_journal_never_stops_the_start(tmp_path: Path) -> None:
    def lookup() -> Path:
        raise RuntimeError("no profile")

    broken = start_journal.StartJournal(lookup)
    broken.start("9.9.9")
    broken.exit(0)
    assert broken.error == "RuntimeError: no profile"
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    unwritable = start_journal.StartJournal(lambda: blocked)
    unwritable.start("9.9.9")
    assert unwritable.error is not None and str(blocked) in unwritable.error


def test_journal_keeps_its_newest_tail(desktop_config_dir: Path) -> None:
    path = desktop_config_dir / start_journal.FILE_NAME
    path.write_text("".join(f"old line {n}\n" for n in range(10_000)), encoding="utf-8")
    journal = start_journal.StartJournal(lambda: desktop_config_dir)
    journal.exit(0)
    data = path.read_bytes()
    assert len(data) <= start_journal.KEEP_BYTES + 200
    lines = data.decode().splitlines()
    assert lines[0].startswith("old line ") and lines[-2] == "old line 9999"
    assert lines[-1].endswith("exit 0")


def test_last_start_takes_its_own_process_lines() -> None:
    text = "\n".join(
        [
            "2026-11-05T18:00:00+02:00 [10] start relaybay 0.6.2 argv=[]",
            "2026-11-05T18:00:01+02:00 [10] started, log x",
            "2026-11-06T08:16:40+02:00 [20] start relaybay 0.6.2 argv=['--background']",
            "2026-11-06T08:16:41+02:00 [10] exit 0",
            "2026-11-06T08:16:42+02:00 [20] lock held, no answer (no desktop.ipc)",
            "  not a journal line [20] x",
            "2026-11-06T08:16:42+02:00 [20] exit 1",
        ]
    )
    last = start_journal.last_start(text)
    assert last is not None
    assert last.time == "2026-11-06T08:16:40+02:00"
    assert last.start == "relaybay 0.6.2 argv=['--background']"
    assert last.outcomes == ("lock held, no answer (no desktop.ipc)", "exit 1")
    assert not last.failed
    assert start_journal.last_start("garbage\n") is None


def test_update_folder_is_cleared_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop_config_dir: Path
) -> None:
    from relaybay.desktop.app import update_folder

    folder = update_folder()
    assert folder == desktop_config_dir / "cache" / "updates"  # the fixture's RELAYBAY_CACHE_DIR
    folder.mkdir(parents=True)
    (folder / "relaybay-0.6.1-windows-x64-setup.exe").write_bytes(b"old")
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("RELAYBAY_PORT", str(free_port()))
    ctl = DesktopController(desktop_config_dir)
    ctl.log_config = lambda: None
    ctl.autostart = Autostart("linux", tmp_path / "home")
    ctl.quitting = True  # run() returns right after the start-up work
    monkeypatch.setattr("relaybay.desktop.window.window_available", lambda: False)
    monkeypatch.setattr(
        "relaybay.desktop.tray.create_icon", lambda entries, dispatch: types.SimpleNamespace()
    )
    assert ctl.run() == 0
    assert not list(folder.iterdir())


def test_the_installed_program_ends_its_process_after_a_clean_quit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from relaybay.desktop import app

    ended: list[int] = []
    monkeypatch.setattr(app, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(app, "_start", lambda background, exit_after, journal: 0)
    monkeypatch.setattr(app.os, "_exit", ended.append)
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    assert app.run() == 0
    assert ended == [0]
    monkeypatch.setattr(app.sys, "frozen", False)
    ended.clear()
    assert app.run() == 0
    assert ended == []  # a source run (and the tests) return normally
