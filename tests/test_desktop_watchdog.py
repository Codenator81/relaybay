"""The desktop program's watchdog with a fake server and a fake clock: no real server starts."""

import logging
import threading
from pathlib import Path

import pytest

from relaybay.desktop import app
from relaybay.desktop.app import DesktopController
from relaybay.desktop.autostart import Autostart
from relaybay.desktop.settings import DesktopSettings, save_desktop_settings


class FakeServer:
    def __init__(self, running: bool = True) -> None:
        self.running = running

    def stop(self) -> bool:
        self.running = False
        return True


class FakeIcon:
    def __init__(self) -> None:
        self.title = "relaybay"
        self.notes: list[str] = []

    def notify(self, message: str, title: str) -> None:
        self.notes.append(message)

    def stop(self) -> None:
        pass


@pytest.fixture
def ctl(
    tmp_path: Path, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> DesktopController:
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path / "data"))
    controller = DesktopController(desktop_config_dir)
    controller.log_config = lambda: None  # leave pytest's logging alone
    controller.autostart = Autostart("linux", tmp_path / "home")
    controller.load()
    controller.icon = FakeIcon()
    controller.refresh_menu = lambda: None
    controller.now = 1000.0
    controller.clock = lambda: controller.now
    controller.health = {"status": "ok"}
    controller.starts = 0
    controller.problems = []

    def start_server() -> None:
        controller.starts += 1
        controller.server = FakeServer()

    monkeypatch.setattr(controller, "start_server", start_server)
    monkeypatch.setattr(controller, "board_url", lambda: "http://127.0.0.1/login")
    monkeypatch.setattr(app, "healthz", lambda *a, **k: controller.health)
    monkeypatch.setattr(
        controller, "show_problem", lambda message, **_: controller.problems.append(message)
    )
    controller.server = FakeServer()
    return controller


def test_a_dead_server_thread_is_restarted(
    ctl: DesktopController, caplog: pytest.LogCaptureFixture
) -> None:
    ctl.watch_once()
    assert ctl.starts == 0  # healthy: nothing to do
    ctl.server.running = False
    with caplog.at_level(logging.WARNING, logger="relaybay.desktop"):
        ctl.watch_once()
    assert ctl.starts == 1 and ctl.server.running
    assert "watchdog: the server thread ended; restarting the server" in caplog.text


def test_two_failed_health_checks_in_a_row_restart(ctl: DesktopController) -> None:
    ctl.health = None
    ctl.watch_once()
    assert ctl.starts == 0  # one failure may be a slow moment
    ctl.health = {"status": "ok"}
    ctl.watch_once()  # the count starts again
    ctl.health = None
    ctl.watch_once()
    assert ctl.starts == 0
    ctl.watch_once()
    assert ctl.starts == 1


def test_at_most_three_restarts_in_ten_minutes(ctl: DesktopController) -> None:
    for _ in range(app.RESTART_CAP):
        ctl.server.running = False
        ctl.watch_once()
        ctl.now += 60
    assert ctl.starts == app.RESTART_CAP
    ctl.server.running = False
    ctl.watch_once()
    assert ctl.starts == app.RESTART_CAP
    message = app.KEEPS_FAILING.format(log=ctl.log_path)
    assert ctl.problems == [message]
    ctl.now += app.RESTART_WINDOW
    ctl.watch_once()  # given up: no more restarts until a manual one
    assert ctl.starts == app.RESTART_CAP and ctl.problems == [message]


def test_old_restarts_leave_the_window(ctl: DesktopController) -> None:
    for _ in range(app.RESTART_CAP + 2):
        ctl.server.running = False
        ctl.watch_once()
        ctl.now += app.RESTART_WINDOW / 2
    assert ctl.starts == app.RESTART_CAP + 2 and not ctl.problems


def test_a_manual_restart_resets_the_count(ctl: DesktopController) -> None:
    for _ in range(app.RESTART_CAP + 1):
        ctl.server.running = False
        ctl.watch_once()
    assert ctl.starts == app.RESTART_CAP and ctl.problems
    ctl.restart()  # tray: Restart server
    assert ctl.starts == app.RESTART_CAP + 1
    ctl.server.running = False
    ctl.watch_once()
    assert ctl.starts == app.RESTART_CAP + 2


def test_nothing_to_watch_without_a_server_or_while_quitting(ctl: DesktopController) -> None:
    ctl.server = None  # never started, e.g. the port is taken: the problem page says so
    ctl.watch_once()
    assert ctl.starts == 0
    ctl.server = FakeServer(running=False)
    ctl.quitting = True
    ctl.watch_once()
    assert ctl.starts == 0


def test_external_mode_never_restarts_and_shows_it_in_the_tooltip(
    ctl: DesktopController, desktop_config_dir: Path
) -> None:
    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:8765")
    )
    ctl.load()
    ctl.server = None
    ctl.health = None
    for _ in range(5):
        ctl.watch_once()
    assert ctl.starts == 0
    assert ctl.icon.title.startswith("relaybay · server unreachable since ")
    since = ctl.unreachable_since
    ctl.watch_once()
    assert ctl.unreachable_since == since  # the first failure's time stays
    ctl.health = {"status": "ok"}
    ctl.watch_once()
    assert ctl.icon.title == "relaybay" and ctl.unreachable_since is None


def test_quit_ends_the_loop(ctl: DesktopController, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "WATCH_SECONDS", 0.01)
    checked = threading.Event()
    monkeypatch.setattr(ctl, "watch_once", checked.set)
    loop = threading.Thread(target=ctl.watch_loop)
    loop.start()
    assert checked.wait(5)
    ctl.quit()
    loop.join(5)
    assert not loop.is_alive()


def test_a_manual_restart_during_the_probe_is_not_counted(
    ctl: DesktopController, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(*args: object, **kwargs: object) -> None:
        ctl.server = FakeServer()  # the owner's Restart server finished meanwhile
        return None

    monkeypatch.setattr(app, "healthz", probe)
    for _ in range(app.WATCH_FAILURES + 1):
        ctl.watch_once()
    assert ctl.starts == 0 and ctl._health_failures == 0 and not ctl._auto_restarts
