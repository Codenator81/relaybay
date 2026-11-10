"""`relaybay desktop`: the tray icon, the board window and the embedded server in one process.

Threads: the GUI loop (pywebview) owns the main thread. The tray runs in its own thread on
Windows; on macOS and Linux it is attached to the GUI loop (`run_detached`) because Cocoa and
GTK want the main thread. Without a web view (Linux without WebKitGTK) the tray takes the main
thread and "Open board" uses the browser. The server runs in a thread (`server_thread`).
"""

import argparse
import html
import contextlib
import logging
import logging.config
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import platformdirs

from relaybay import __version__, machine_setup, updates
from relaybay.config import Settings, load_settings
from relaybay.desktop import add_run_arguments, single_instance
from relaybay.desktop.autostart import Autostart, launch_command
from relaybay.desktop.notifier import Notifier, Page, fetch_page
from relaybay.desktop.server_thread import EmbeddedServer, PortTaken, healthz
from relaybay.desktop.settings import (
    DesktopSettings,
    cleartext_warning,
    config_dir,
    load_desktop_settings,
    save_desktop_settings,
    settings_path,
)
from relaybay.desktop.start_journal import StartJournal
from relaybay.desktop.window import DARK_BACKGROUND, DARK_TEXT, LIGHT_BACKGROUND, LIGHT_TEXT
from relaybay.desktop.window_state import (
    WindowState,
    WindowTracker,
    mark_reopen,
    save_state,
    take_reopen,
)
from relaybay.install import DESKTOP_KINDS, hook_executable, install_kind
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


def update_folder() -> Path:
    """Where Update downloads the installer; cleared at the next start of the program.
    `RELAYBAY_CACHE_DIR` moves it (tests keep off the user's cache)."""
    base = os.environ.get("RELAYBAY_CACHE_DIR")
    root = (
        Path(base).expanduser()
        if base
        else Path(platformdirs.user_cache_dir("relaybay", appauthor=False))
    )
    return root / "updates"


def open_path(path: Path) -> None:
    """Open a file or folder with the OS (Logs, Settings without a window)."""
    if sys.platform == "win32":
        os.startfile(path)
        return
    opener = "open" if sys.platform == "darwin" else (shutil.which("xdg-open") or "xdg-open")
    subprocess.Popen([opener, str(path)], start_new_session=True)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><meta charset='utf-8'><title>relaybay</title><style>"
        ":root{color-scheme:light dark}"
        f"html{{background:{LIGHT_BACKGROUND};color:{LIGHT_TEXT}}}"
        f"@media (prefers-color-scheme:dark){{html{{background:{DARK_BACKGROUND};"
        f"color:{DARK_TEXT}}}}}"
        "body{font:15px system-ui,sans-serif;margin:40px;max-width:640px}"
        f"</style><body><h2>{html.escape(title)}</h2>{body}</body>"
    )


QUIT_WAIT = 60.0  # seconds Quit waits for a restart in progress
# Seconds after Update starts the installer during which a quit is the one it asked for: the
# silent Windows setup sends its --quit within seconds; a setup that never does was cancelled.
INSTALLER_QUIT_WAIT = 60.0
# The watchdog: the embedded server's /healthz every WATCH_SECONDS, restarted after WATCH_FAILURES
# failed checks in a row (or a dead server thread), at most RESTART_CAP times in RESTART_WINDOW;
# an external server is only checked, every EXTERNAL_WATCH_SECONDS, for the tray tooltip.
WATCH_SECONDS = 30.0
EXTERNAL_WATCH_SECONDS = 60.0
WATCH_FAILURES = 2
RESTART_CAP = 3
RESTART_WINDOW = 600.0
NOTIFY_SECONDS = 5.0
KEEPS_FAILING = "The server keeps failing: see the log {log}"
TRAY_TITLE = "relaybay"

STARTING_PAGE = _page("relaybay", "<p>Starting the board...</p>")


class DesktopController:
    def __init__(
        self, folder: Path, background: bool = False, journal: StartJournal | None = None
    ) -> None:
        self.config_dir = folder
        self.background = background
        self.journal = journal
        self.autostart = Autostart()
        self.desktop = DesktopSettings()
        self.settings: Settings | None = None
        self.problem: str | None = None
        self.server: EmbeddedServer | None = None
        self.window: Any = None
        self.window_tracker = WindowTracker(WindowState())
        self.settings_window: Any = None
        self.icon: Any = None
        self.has_window = False
        self.quitting = False
        self._restart_lock = threading.Lock()
        self.update = updates.UpdateStatus()  # the program's own version against the release
        self._stopped = threading.Event()  # set by Quit: ends the update and watchdog loops
        self._updating = threading.Lock()  # held while Update downloads or runs an installer
        self.clock = time.monotonic
        self.wall_clock = time.time  # the reopen marker outlives the process
        self.installer_started_at: float | None = None  # see quitting_for_update
        self.clock = time.monotonic  # the watchdog's clock (tests pass a fake one)
        self._health_failures = 0
        self._auto_restarts: deque[float] = deque()
        self._gave_up = False
        self.unreachable_since: str | None = None  # external mode: since when, for the tooltip
        self.pending_dot = False  # quiet mode holds notifications
        self.notifier = Notifier(
            folder,
            fetch=self._fetch_notifications,
            toast=self._toast,
            dot=self._set_pending_dot,
            clock=lambda: self.clock(),
        )

    # --- state -------------------------------------------------------------

    def load(self) -> None:
        """Read desktop.toml and the settings; a bad file becomes `self.problem`."""
        try:
            self.desktop = load_desktop_settings(self.config_dir)
            self.settings = load_settings(serving=self.desktop.server_mode == "embedded")
            self.problem = None
            self.notifier.configure(
                enabled=self.desktop.notify,
                quiet=self.desktop.notify_quiet,
                kinds=self.desktop.notify_kinds,
                server=f"{self.settings.base_url} {self.settings.home}",
            )
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
            # A setting to change, not a crash: nothing for the watchdog to restart.
            self.server = None
            self.show_problem(str(exc), ask_settings=True)
            return
        except (RuntimeError, ValueError, OSError) as exc:
            self.show_problem(f"{exc} Log: {self.log_path}", ask_settings=self.external)
            return
        if self.window is not None:
            self.window.load_url(url)

    def restart(self) -> None:
        """Restart server / Reconnect from the menu or Settings; also gives the watchdog its
        automatic restarts back."""
        self._auto_restarts.clear()
        self._gave_up = False
        self._restart()

    def _restart(self) -> None:
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

    # --- watchdog ----------------------------------------------------------

    def watch_loop(self) -> None:
        """Keeps the server up while the program runs; ends with Quit."""
        while not self._stopped.wait(EXTERNAL_WATCH_SECONDS if self.external else WATCH_SECONDS):
            try:
                self.watch_once()
            except Exception:
                log.exception("the watchdog check failed; the next one tries again")

    def watch_once(self) -> None:
        """One check. Embedded: restart a server whose thread died or whose /healthz failed
        WATCH_FAILURES times in a row, at most RESTART_CAP times in RESTART_WINDOW; then the
        problem page. External: never a restart, only the tray tooltip."""
        settings = self.settings
        if self.quitting or settings is None or self._restart_lock.locked():
            return
        if self.external:
            self._watch_external(settings.base_url)
            return
        server = self.server
        if server is None or self._gave_up:
            return  # not started (a problem the page shows), or given up until a manual restart
        healthy = server.running and healthz(settings.base_url, timeout=5) is not None
        if self.quitting or self.server is not server or self._restart_lock.locked():
            return  # Quit or a manual restart came during the probe: it is not this server's
        if healthy:
            self._health_failures = 0
            return
        self._health_failures += 1
        if server.running and self._health_failures < WATCH_FAILURES:
            return
        reason = "the server thread ended" if not server.running else "/healthz does not answer"
        now = self.clock()
        while self._auto_restarts and now - self._auto_restarts[0] > RESTART_WINDOW:
            self._auto_restarts.popleft()
        if len(self._auto_restarts) >= RESTART_CAP:
            self._gave_up = True
            message = KEEPS_FAILING.format(log=self.log_path)
            log.error("watchdog: %s; %d restarts in 10 minutes, giving up", reason, RESTART_CAP)
            self.show_problem(message)
            if self.window is not None and self.icon is not None:
                self.icon.notify(message, "relaybay")  # show_problem notifies without a window
            return
        self._auto_restarts.append(now)
        self._health_failures = 0
        log.warning("watchdog: %s; restarting the server", reason)
        self._restart()

    def _watch_external(self, base_url: str) -> None:
        if healthz(base_url, timeout=5) is not None:
            self.unreachable_since = None
        elif self.unreachable_since is None:
            self.unreachable_since = datetime.now().strftime("%Y-%m-%d %H:%M")
            log.warning("the server at %s does not answer", base_url)
            self.notifier.local(
                "server", "The relaybay server is unreachable", f"{base_url} does not answer."
            )
        if self.icon is not None:
            self.icon.title = self.tray_title()

    def tray_title(self) -> str:
        if self.external and self.unreachable_since:
            return f"{TRAY_TITLE} · server unreachable since {self.unreachable_since}"
        return TRAY_TITLE

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

        window.show(self.window, self.window_tracker)

    def save_window_state(self) -> None:
        """Keep the window's state for the next start; a failure costs only that, so it never
        stands in the way of hiding or quitting."""
        if self.window is None:  # no window yet: the saved file is still the truth
            return
        try:
            save_state(self.config_dir, self.window_tracker.snapshot())
        except OSError as exc:
            log.warning("the window state was not saved: %s", exc)

    def mark_reopen(self) -> None:
        """Quitting for an update with the window open: the program the installer starts again
        (with `--background` on Windows) shows it. A failure costs only that."""
        try:
            mark_reopen(self.config_dir, self.wall_clock())
        except OSError as exc:
            log.warning("the window will not reopen after the update: %s", exc)

    def take_reopen(self) -> bool:
        try:
            return take_reopen(self.config_dir, self.wall_clock())
        except OSError as exc:
            # Left in place it would show the window at every later logon: stay hidden instead.
            log.warning("the reopen marker could not be removed, staying hidden: %s", exc)
            return False

    def open_in_browser(self) -> None:
        webbrowser.open(self.browser_url())

    def open_inbox(self) -> None:
        url = self.browser_url() + "&next=/inbox"
        if self.window is None:
            webbrowser.open(url)
            return
        self.window.load_url(url)
        self.show_window()

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

    def toggle_quiet(self) -> None:
        on = not self.desktop.notify_quiet
        current = load_desktop_settings(self.config_dir)
        if current.notify_quiet != on:
            current.notify_quiet = on
            save_desktop_settings(self.config_dir, current)
        self.desktop.notify_quiet = on
        self.refresh_menu()
        self.notifier.set_quiet(on)

    def menu_entries(self) -> Any:
        from relaybay.desktop.tray import MenuState, build_menu

        latest = self.update.latest
        state = MenuState(
            server_mode=self.desktop.server_mode,
            autostart=self.autostart.is_enabled(),
            has_window=self.has_window,
            update_version=latest.version if latest and self.update.available else None,
            notify=self.desktop.notify,
            quiet=self.desktop.notify_quiet,
        )
        return build_menu(state)

    def refresh_menu(self) -> None:
        if self.icon is None:
            return
        from relaybay.desktop.tray import to_pystray

        self.icon.menu = to_pystray(self.menu_entries(), self.dispatch)
        self.icon.update_menu()

    def _redraw_icon(self) -> None:
        if self.icon is None:
            return
        from relaybay.desktop.tray import icon_image

        self.icon.icon = icon_image(update=self.update.available, pending=self.pending_dot)

    def notify(self, message: str) -> None:
        log.info("%s", message)
        if self.icon is not None:
            self.icon.notify(message, "relaybay")

    # --- tray notifications --------------------------------------------------

    def notify_loop(self) -> None:
        """Asks the server what needs the owner while the program runs; ends with Quit."""
        while not self._stopped.wait(NOTIFY_SECONDS):
            try:
                if not self.quitting and self.settings is not None and not self.problem:
                    self.notifier.tick()
            except Exception:
                log.exception("the notification check failed; the next one tries again")

    def _fetch_notifications(self, after: int | None) -> Page:
        assert self.settings is not None
        return fetch_page(self.settings.base_url, self.token(), after)

    def _toast(self, text: str, title: str) -> None:
        log.info("notification: %s: %s", title, text)
        # pystray's X11 backend has none, and its notify raises: the log line is all there is.
        if self.icon is not None and getattr(self.icon, "HAS_NOTIFICATION", True):
            self.icon.notify(text, title)

    def _set_pending_dot(self, on: bool) -> None:
        self.pending_dot = on
        self._redraw_icon()

    # --- updates -------------------------------------------------------------

    def _check_update(self) -> None:
        was = self.update.available
        updates.check(self.update)
        if self.update.error:
            log.debug("update check failed: %s", self.update.error)
        if self.update.available != was:
            self._redraw_icon()
            self.refresh_menu()

    def check_updates(self) -> None:
        """Settings → Check for updates: a check now, with its answer as a notification."""
        self._check_update()
        latest = self.update.latest
        if self.update.error:
            self.notify(f"The update check failed: {self.update.error}")
        elif latest is not None and self.update.available:
            self.notify(f"relaybay {latest.version} is available: tray menu, Update.")
        else:
            self.notify(f"relaybay {__version__} is the latest version.")

    def update_loop(self) -> None:
        """The program's own check: shortly after the start, then every `update_check_hours`
        while `update_check` is on in desktop.toml; a failure is only logged."""
        wait = updates.START_DELAY
        while not self._stopped.wait(wait):
            try:
                if self.desktop.update_check:
                    self._check_update()
            except Exception:
                log.exception("the update check failed; the next one tries again")
            # Event.wait raises OverflowError above TIMEOUT_MAX (about 49 days on Windows).
            wait = min(self.desktop.update_check_hours * 3600, threading.TIMEOUT_MAX)

    def install_update(self) -> None:
        """Download this system's installer of the newer release, verify it against the
        release's SHA256SUMS.txt and start it; a file that fails the check never runs. A second
        click while one download or install runs is ignored."""
        if not self._updating.acquire(blocking=False):
            self.notify("The update is already running.")
            return
        started = False
        try:
            started = self._install_update()
        finally:
            # A started installer keeps the flag: the program is about to be replaced
            # (_after_installer gives it back when that did not happen).
            if not started:
                self._updating.release()

    def _install_update(self) -> bool:
        latest = self.update.latest
        if latest is None or not self.update.available:
            self.notify(f"relaybay {__version__} is the latest version.")
            return False
        kind = install_kind()
        if kind not in DESKTOP_KINDS:
            # Not the installed program (a uv tool, pipx, a checkout): its own upgrade command.
            command = updates.upgrade_hint(kind, latest.version)
            self.notify(f"relaybay {latest.version} is available; upgrade with: {command}")
            return False
        folder = update_folder()
        self.notify(f"Downloading relaybay {latest.version}...")
        try:
            path = updates.prepare_installer(latest, kind, updates.machine_arch(), folder)
        except updates.UpdateError as exc:
            self.show_problem(f"relaybay {latest.version} was not installed: {exc}")
            return False
        log.info("starting the installer %s", path)
        # Before the start: the silent Windows setup sends its `desktop --quit` within seconds.
        self.installer_started_at = self.clock()
        try:
            process = updates.launch_installer(path, kind)
        except BaseException:
            self.installer_started_at = None
            raise
        if kind != "desktop-windows":
            self.quit()
            return True
        # Windows: setup itself asks this program to quit (desktop --quit) and starts the new
        # one afterwards, which it does only when it found one running.
        threading.Thread(
            target=self._after_installer, args=(process,), name="relaybay-installer", daemon=True
        ).start()
        return True

    def _after_installer(self, process: subprocess.Popen[bytes]) -> None:
        """Setup asks for the quit before it installs, so its exit without one means it was
        cancelled or failed: Update works again without a restart."""
        code = process.wait()
        if self.quitting:
            return
        log.info("the installer exited with %s without a quit", code)
        self.installer_started_at = None
        self._updating.release()

    def dispatch(self, action: str) -> None:
        """Run a menu action off the tray thread, so a restart never freezes the menu."""

        def run() -> None:
            try:
                getattr(self, action)()
            except Exception as exc:
                log.exception("menu action %s failed", action)
                self.show_problem(f"{action} failed: {exc}")

        threading.Thread(target=run, name=f"relaybay-{action}", daemon=True).start()

    def quitting_for_update(self) -> bool:
        """Whether a quit now is the one the started installer asks for."""
        started = self.installer_started_at
        return started is not None and self.clock() - started < INSTALLER_QUIT_WAIT

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self._stopped.set()
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

            # Not left to the closing event: Cocoa and GTK do not raise it for destroy().
            self.save_window_state()
            if self.quitting_for_update() and self.window_tracker.visible:
                self.mark_reopen()
            for window in list(webview.windows):
                window.destroy()

    # --- run ---------------------------------------------------------------

    # --- Claude Code ---------------------------------------------------------

    def check_hooks(self) -> None:
        """At start, read-only: relaybay's entries in Claude Code's files (the `config_checks`
        of doctor). Hooks that start a missing or another install's forwarder become a notice
        pointing to Settings; nothing is ever written from here."""
        settings = self.settings
        if settings is None:
            return
        paths = machine_setup.claude_paths(settings.claude_home)
        checks = machine_setup.config_checks(base_url=settings.base_url, token=None, paths=paths)
        for check in checks:
            if not check.ok:
                log.info("Claude Code setup: %s: %s", check.name, check.detail)
        target = machine_setup.reconnect_target(checks, paths, settings.base_url, hook_executable())
        if target:
            self.notify(f"Claude Code hooks point to {target}: open Settings -> Reconnect")

    def reconnect_plan(self) -> machine_setup.Plan:
        """Settings -> Reconnect Claude Code: the plan of `relaybay setup` and the Integrations
        page for this program (its forwarder, its server URL and token file), in the hook type
        already installed (command when there are none)."""
        if self.settings is None:
            raise ValueError(self.problem or "The settings could not be read.")
        settings = self.settings
        paths = machine_setup.claude_paths(settings.claude_home)
        try:
            _, data = machine_setup.read_json(paths.settings)
            installed = machine_setup.installed_mode(
                data, machine_setup.owned_hosts(settings.base_url)
            )
        except (machine_setup.SetupError, OSError):
            installed = None  # make_plan below reports the same error in the plan
        mode = "http" if installed == "http" else machine_setup.DEFAULT_MODE
        return machine_setup.make_plan(
            base_url=settings.base_url,
            token=self.token(),
            token_path=settings.token_path,
            paths=paths,
            detection=machine_setup.detect(),
            mode=mode,
        )

    def reconnect_apply(self, expect: str) -> str:
        """Write the plan the user saw (`expect` is its hash), with a backup; the message for
        the Settings window. Raises SetupError or ValueError when it cannot."""
        plan = self.reconnect_plan()
        if plan.error or plan.hooks is None:
            raise ValueError(plan.error or "Nothing to apply.")
        backup = machine_setup.write_change(plan.hooks, expect=expect)
        if not plan.hooks.changed:
            return "Nothing to change."
        return f"Wrote {plan.hooks.path}" + (f" (backup: {backup.name})" if backup else "")

    def on_ready(self, exit_after: float | None) -> None:
        if exit_after:
            timer = threading.Timer(exit_after, self.quit)
            timer.daemon = True
            timer.start()
        self._start_and_load()
        threading.Thread(
            target=self._check_hooks_logged, name="relaybay-hooks", daemon=True
        ).start()

    def _check_hooks_logged(self) -> None:
        try:
            self.check_hooks()
        except Exception:
            log.exception("the check of Claude Code's hooks failed")

    def run(self, exit_after: float | None = None) -> int:
        from relaybay.desktop import tray, window

        self.load()
        self.configure_logging()
        log.info("relaybay %s desktop, config %s", __version__, self.config_dir)
        # Taken before the rest of the start, so a later logon start stays hidden whatever follows.
        if self.take_reopen() and self.background:
            log.info("the window was open before the update: showing it")
            self.background = False
        if self.journal is not None:
            self.journal.write(f"started, log {self.log_path}")
            if self.journal.error:
                log.warning("the start journal was not written: %s", self.journal.error)
        # The installer of the last update is of no use once the program runs again.
        updates.clean_folder(update_folder())
        if self.desktop.autostart:
            # Heals the entry after an upgrade or a move of the executable.
            self.autostart.enable(launch_command())
        self.has_window = window.window_available()
        self.icon = tray.create_icon(self.menu_entries(), self.dispatch)
        threading.Thread(target=self.update_loop, name="relaybay-updates", daemon=True).start()
        threading.Thread(target=self.watch_loop, name="relaybay-watchdog", daemon=True).start()
        threading.Thread(target=self.notify_loop, name="relaybay-notify", daemon=True).start()
        if self.quitting:  # --quit or Quit arrived during start-up
            return 0
        if not self.has_window:
            log.warning("no web view available: tray only, the board opens in the browser")
            self.icon.run(setup=lambda icon: self._tray_only_ready(icon, exit_after))
            return 0

        import webview

        self.window_tracker = WindowTracker(window.saved_state(self.config_dir))
        self.window = window.create_window(
            STARTING_PAGE,
            hidden=self.background,
            keep_open=lambda: not self.quitting,
            tracker=self.window_tracker,
            save_state=self.save_window_state,
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
    journal = StartJournal(config_dir)
    journal.start(__version__)
    try:
        code = _start(background, exit_after, journal)
    except BaseException as exc:
        journal.exception(exc)
        raise
    journal.exit(code)
    if getattr(sys, "frozen", False):
        _hard_exit(code)
    return code


def _hard_exit(code: int) -> None:
    """The installed program ends here once everything is closed and logged. On macOS the
    process could stay alive after a clean quit (AppKit at interpreter shutdown), so Quit and
    an update left a hung process; nothing is left to clean up at this point."""
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            stream.flush()
    os._exit(code)


def _ipc_age(folder: Path) -> str:
    try:
        stamp = (folder / single_instance.IPC_FILE).stat().st_mtime
    except FileNotFoundError:
        return f"no {single_instance.IPC_FILE}"
    except OSError as exc:
        return f"{single_instance.IPC_FILE} unreadable: {exc}"
    written = datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")
    return f"{single_instance.IPC_FILE} written {written}"


def _start(background: bool, exit_after: float | None, journal: StartJournal) -> int:
    if not extra_installed():
        journal.write("extra missing")
        print(MISSING_EXTRA, file=sys.stderr)
        return 2
    folder = config_dir()
    lock = single_instance.acquire(folder)
    if lock is None:
        if single_instance.notify_running(folder):
            journal.write("another instance answered")
            return 0
        journal.write(f"lock held, no answer ({_ipc_age(folder)})")
        print(
            f"relaybay desktop: another instance holds {folder / single_instance.LOCK_FILE}"
            " but does not answer.",
            file=sys.stderr,
        )
        return 1
    single_instance.hold_app_mutex()
    controller = DesktopController(folder, background=background, journal=journal)
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
