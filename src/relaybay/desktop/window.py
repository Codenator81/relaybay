"""The board window: the system web view (WebView2, WKWebView, WebKitGTK) through pywebview.

Closing the window hides it; Quit is in the tray menu. Private mode (the default) keeps no
cookies, so each start signs in again through the login link, which carries the token: a local
URL in embedded mode, the external server's URL in external mode (use https there). The access
log redacts it. Links with `target="_blank"` open in the default browser
(pywebview's OPEN_EXTERNAL_LINKS_IN_BROWSER).
"""

import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from relaybay.desktop.tray import app_theme
from relaybay.desktop.window_state import MIN_SIZE, WindowState, WindowTracker, load_state

log = logging.getLogger("relaybay.desktop")

TITLE = "relaybay"
# WinForms' restore() also un-maximizes; GTK and Cocoa raise a window only through restore().
RESTORE_UNMAXIMIZES = sys.platform == "win32"
# Cocoa: pywebview's "maximized" event means full screen and its maximize is a plain resize that no
# event reports, so a restored flag could never clear.
KEEPS_MAXIMIZED = sys.platform != "darwin"
# GTK: `moved` reports global coordinates and create_window takes them relative to the first
# monitor, possibly also shifted by the title bar: the window would drift.
KEEPS_POSITION = not sys.platform.startswith("linux")
# --bg and --text of web/static/app.css.
LIGHT_BACKGROUND, LIGHT_TEXT = "#f6f7f9", "#1c2026"
DARK_BACKGROUND, DARK_TEXT = "#111418", "#e6e8eb"


def window_available() -> bool:
    """True when a web view can be shown: pywebview imports and, on Linux, WebKitGTK is there.
    False means a tray-only program that opens the board in the browser."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux"):
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("WebKit2", "4.1")
            from gi.repository import Gtk, WebKit2  # noqa: F401
        except (ImportError, ValueError):
            return False
    return True


def background_color() -> str:
    """What a window shows before its page paints; pywebview's default is white."""
    return DARK_BACKGROUND if app_theme() == "dark" else LIGHT_BACKGROUND


def saved_state(folder: Path) -> WindowState:
    """The remembered window for this machine's screens. Call before the first window exists: on
    Windows pywebview makes the process DPI aware when it creates one, and until then the screens
    come in the same logical pixels as the saved position. A remembered window is a convenience:
    no failure here may stop the program from starting."""
    import webview

    try:
        state = load_state(folder, webview.screens)
    except Exception as exc:
        log.warning("using the default window: %s", exc)
        return WindowState()
    if not KEEPS_MAXIMIZED:
        state = replace(state, maximized=False)
    if not KEEPS_POSITION:
        state = replace(state, x=None, y=None)
    return state


def create_window(
    page: str,
    hidden: bool,
    keep_open: Callable[[], bool],
    tracker: WindowTracker,
    save_state: Callable[[], None],
) -> Any:
    """The main window showing the HTML `page` until the board URL is loaded, with the geometry of
    `tracker`, which then follows it; `save_state()` runs on every close. `keep_open()` False lets
    a close through (Quit), else a close only hides the window."""
    import webview

    state = tracker.snapshot()
    window = webview.create_window(
        TITLE,
        html=page,
        width=state.width,
        height=state.height,
        x=state.x,
        y=state.y,
        maximized=state.maximized,
        min_size=MIN_SIZE,
        hidden=hidden,
        background_color=background_color(),
    )
    tracker.visible = not hidden

    def on_closing() -> bool:
        save_state()
        if keep_open():
            window.hide()
            tracker.visible = False
            return False  # cancels the close
        return True

    window.events.closing += on_closing
    window.events.minimized += tracker.on_minimized
    window.events.maximized += tracker.on_maximized
    window.events.restored += tracker.on_restored
    window.events.moved += tracker.on_moved
    window.events.resized += tracker.on_resized
    return window


def show(window: Any, tracker: WindowTracker) -> None:
    """Raise the window as it was. On Windows `restore()` also un-maximizes, so only a minimized
    window gets a state change, back to what it was before; elsewhere `restore()` is what raises."""
    window.show()
    tracker.visible = True
    if not RESTORE_UNMAXIMIZES:
        window.restore()
    elif tracker.minimized:
        if tracker.maximized:
            window.maximize()
        else:
            window.restore()
