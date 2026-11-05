"""`relaybay desktop`: the tray icon, the board window and the embedded server in one process.

Threads: the GUI loop (pywebview) owns the main thread. The tray runs in its own thread on
Windows; on macOS and Linux it is attached to the GUI loop (`run_detached`) because Cocoa and
GTK want the main thread. Without a web view (Linux without WebKitGTK) the tray takes the main
thread and "Open board" uses the browser. The server runs in a thread (`server_thread`).
"""

import argparse
import html
import logging
import logging.config
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from relaybay import __version__
from relaybay.config import Settings, load_settings
from relaybay.desktop import add_run_arguments, single_instance
from relaybay.desktop.autostart import Autostart, launch_command
from relaybay.desktop.server_thread import EmbeddedServer, PortTaken, healthz
from relaybay.desktop.settings import (
    DesktopSettings,
    cleartext_warning,
    config_dir,
    load_desktop_settings,
    save_desktop_settings,
    settings_path,
)
from relaybay.logs import desktop_log_config
from relaybay.security import load_or_create_token

log = logging.getLogger("relaybay.desktop")

MISSING_EXTRA = (
    "relaybay desktop needs the desktop extra: install relaybay[desktop]"
    " (for example: uv tool install 'relaybay[desktop]')."
)


def extra_installed() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) for name in ("webview", "pystray", "PIL"))


def open_path(path: Path) -> None:
    """Open a file or folder with the OS (Logs, Settings without a window)."""
    if sys.platform == "win32":
        os.startfile(path)
        return
    opener = "open" if sys.platform == "darwin" else (shutil.which("xdg-open") or "xdg-open")
    subprocess.Popen([opener, str(path)], start_new_session=True)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'><title>relaybay</title>"
        "<body style='font:15px system-ui,sans-serif;margin:40px;max-width:640px'>"
        f"<h2>{html.escape(title)}</h2>{body}</body>"
    )


QUIT_WAIT = 60.0  # seconds Quit waits for a restart in progress

STARTING_PAGE = _page("relaybay", "<p>Starting the board...</p>")


class DesktopController:
    def __init__(self, folder: Path, background: bool = False) -> None:
        self.config_dir = folder
        self.background = background
        self.autostart = Autostart()
        self.desktop = DesktopSettings()
        self.settings: Settings | None = None
        self.problem: str | None = None
        self.server: EmbeddedServer | None = None
        self.window: Any = None
        self.settings_window: Any = None
        self.icon: Any = None
        self.has_window = False
        self.quitting = False
        self._restart_lock = threading.Lock()

    # --- state -------------------------------------------------------------

    def load(self) -> None:
        """Read desktop.toml and the settings; a bad file becomes `self.problem`."""
        try:
            self.desktop = load_desktop_settings(self.config_dir)
            self.settings = load_settings(serving=self.desktop.server_mode == "embedded")
            self.problem = None
            warning = cleartext_warning(self.desktop)
            if warning:
                log.warning("%s", warning)
        except (ValueError, OSError) as exc:
            self.problem = f"The settings could not be read: {exc}"

    @property
    def external(self) -> bool:
        return self.desktop.server_mode == "external"

    @property
    def log_path(self) -> Path:
        base = self.settings.home if self.settings is not None else self.config_dir
        return base / "logs" / "relaybay.log"

    def log_config(self) -> dict[str, Any] | None:
        return desktop_log_config(self.log_path)

    def configure_logging(self) -> None:
        config = self.log_config()
        if config is not None:
            logging.config.dictConfig(config)

    def token(self) -> str:
        assert self.settings is not None
        path = self.settings.token_path
        if not self.external:
            return load_or_create_token(path)
        if not path.is_file():
            raise ValueError(f"No token for the external server: {path} is missing.")
        return path.read_text(encoding="utf-8").strip()

    def browser_url(self) -> str:
        """The login link exactly as `relaybay token` prints it."""
        assert self.settings is not None
        return f"{self.settings.base_url}/login?token={self.token()}"

    def board_url(self) -> str:
        return self.browser_url() + "&next=/"

    # --- server ------------------------------------------------------------

    def start_server(self) -> None:
        assert self.settings is not None
        if self.external:
            if healthz(self.settings.base_url, timeout=5) is None:
                raise RuntimeError(
                    f"The server at {self.settings.base_url} does not answer."
                    " Check the URL in Settings, or switch to the embedded server."
                )
            return
        self.server = EmbeddedServer(self.settings, self.log_config())
        self.server.start()

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _start_and_load(self) -> None:
        if self.problem:
            self.show_problem(self.problem)
            return
        try:
            self.start_server()
            url = self.board_url()
        except PortTaken as exc:
            self.show_problem(str(exc), ask_settings=True)
            return
        except (RuntimeError, ValueError, OSError) as exc:
            self.show_problem(f"{exc} Log: {self.log_path}", ask_settings=self.external)
            return
        if self.window is not None:
            self.window.load_url(url)

    def restart(self) -> None:
        if self.quitting:
            return
        with self._restart_lock:
            if self.quitting:
                return
            log.info("restart")
            self.stop_server()
            self.load()
            self.configure_logging()
            self.refresh_menu()
            self._start_and_load()

    def restart_async(self) -> None:
        threading.Thread(target=self.restart, name="relaybay-restart", daemon=True).start()

    # --- window, tray, dialogs --------------------------------------------

    def show_problem(self, message: str, ask_settings: bool = False) -> None:
        log.error("%s", message)
        if self.window is None:
            if self.icon is not None:
                self.icon.notify(message, "relaybay")
            return
        hint = "Use the tray icon: Settings to change the server, or Quit."
        self.window.load_html(
            _page("relaybay cannot show the board", f"<p>{html.escape(message)}</p><p>{hint}</p>")
        )
        self.show_window()
        if ask_settings and self.window.create_confirmation_dialog(
            "relaybay", f"{message}\n\nOpen Settings now?"
        ):
            self.open_settings()

    def show_window(self) -> None:
        if self.window is None:
            webbrowser.open(self.browser_url())
            return
        from relaybay.desktop import window

        window.show(self.window)

    def open_in_browser(self) -> None:
        webbrowser.open(self.browser_url())

    def open_logs(self) -> None:
        path = self.log_path
        open_path(path if path.is_file() else path.parent)

    def open_settings(self) -> None:
        if not self.has_window:
            path = settings_path(self.config_dir)
            if not path.is_file():
                save_desktop_settings(self.config_dir, self.desktop)
            open_path(path)
            return
        from relaybay.desktop.settings_window import open_settings

        self.settings_window = open_settings(self, self.settings_window)

    def set_autostart(self, on: bool) -> None:
        if on:
            self.autostart.enable(launch_command())
        else:
            self.autostart.disable()
        current = load_desktop_settings(self.config_dir)
        if current.autostart != on:
            current.autostart = on
            save_desktop_settings(self.config_dir, current)
        self.desktop.autostart = on
        self.refresh_menu()

    def toggle_autostart(self) -> None:
        self.set_autostart(not self.autostart.is_enabled())

    def menu_entries(self) -> Any:
        from relaybay.desktop.tray import MenuState, build_menu

        state = MenuState(
            server_mode=self.desktop.server_mode,
            autostart=self.autostart.is_enabled(),
            has_window=self.has_window,
        )
        return build_menu(state)

    def refresh_menu(self) -> None:
        if self.icon is None:
            return
        from relaybay.desktop.tray import to_pystray

        self.icon.menu = to_pystray(self.menu_entries(), self.dispatch)
        self.icon.update_menu()

    def dispatch(self, action: str) -> None:
        """Run a menu action off the tray thread, so a restart never freezes the menu."""

        def run() -> None:
            try:
                getattr(self, action)()
            except Exception as exc:
                log.exception("menu action %s failed", action)
                self.show_problem(f"{action} failed: {exc}")

        threading.Thread(target=run, name=f"relaybay-{action}", daemon=True).start()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        log.info("quit")
        # A restart in progress would start the server again after we stopped it: wait for it.
        locked = self._restart_lock.acquire(timeout=QUIT_WAIT)
        try:
            self.stop_server()
        finally:
            if locked:
                self._restart_lock.release()
        if self.icon is not None:
            self.icon.stop()
        if self.has_window:
            import webview

            for window in list(webview.windows):
                window.destroy()

    # --- run ---------------------------------------------------------------

    def on_ready(self, exit_after: float | None) -> None:
        if exit_after:
            timer = threading.Timer(exit_after, self.quit)
            timer.daemon = True
            timer.start()
        self._start_and_load()

    def run(self, exit_after: float | None = None) -> int:
        from relaybay.desktop import tray, window

        self.load()
        self.configure_logging()
        log.info("relaybay %s desktop, config %s", __version__, self.config_dir)
        if self.desktop.autostart:
            # Heals the entry after an upgrade or a move of the executable.
            self.autostart.enable(launch_command())
        self.has_window = window.window_available()
        self.icon = tray.create_icon(self.menu_entries(), self.dispatch)
        if self.quitting:  # --quit or Quit arrived during start-up
            return 0
        if not self.has_window:
            log.warning("no web view available: tray only, the board opens in the browser")
            self.icon.run(setup=lambda icon: self._tray_only_ready(icon, exit_after))
            return 0

        import webview

        self.window = window.create_window(
            STARTING_PAGE, hidden=self.background, keep_open=lambda: not self.quitting
        )
        if sys.platform == "win32":
            threading.Thread(target=self.icon.run, name="relaybay-tray", daemon=True).start()
        else:
            self.icon.run_detached()
        if self.quitting:
            self.icon.stop()
            return 0
        webview.start(self.on_ready, (exit_after,), private_mode=True)
        if not self.quitting:
            self.quit()
        return 0

    def _tray_only_ready(self, icon: Any, exit_after: float | None) -> None:
        icon.visible = True
        self.on_ready(exit_after)
        if not self.problem and not self.background:
            self.show_window()


def run(background: bool = False, exit_after: float | None = None, quit: bool = False) -> int:
    if quit:
        if single_instance.notify_running(config_dir(), single_instance.QUIT):
            return 0
        print("relaybay desktop: no running program answered.", file=sys.stderr)
        return 1
    if not extra_installed():
        print(MISSING_EXTRA, file=sys.stderr)
        return 2
    folder = config_dir()
    lock = single_instance.acquire(folder)
    if lock is None:
        if single_instance.notify_running(folder):
            return 0
        print(
            f"relaybay desktop: another instance holds {folder / single_instance.LOCK_FILE}"
            " but does not answer.",
            file=sys.stderr,
        )
        return 1
    controller = DesktopController(folder, background=background)
    ipc = single_instance.serve_raise(folder, controller.show_window, controller.quit)
    try:
        return controller.run(exit_after)
    finally:
        controller.stop_server()
        ipc.close()
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relaybay-desktop", description=__doc__.splitlines()[0])
    add_run_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(background=args.background, exit_after=args.exit_after, quit=args.quit)
