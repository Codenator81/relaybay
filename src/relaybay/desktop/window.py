"""The board window: the system web view (WebView2, WKWebView, WebKitGTK) through pywebview.

Closing the window hides it; Quit is in the tray menu. Private mode (the default) keeps no
cookies, so each start signs in again through the login link, which carries the token: a local
URL in embedded mode, the external server's URL in external mode (use https there). The access
log redacts it. Links with `target="_blank"` open in the default browser
(pywebview's OPEN_EXTERNAL_LINKS_IN_BROWSER).
"""

import sys
from collections.abc import Callable
from typing import Any

TITLE = "relaybay"


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


def create_window(page: str, hidden: bool, keep_open: Callable[[], bool]) -> Any:
    """The main window showing the HTML `page` until the board URL is loaded; `keep_open()`
    False lets a close through (Quit), else a close only hides the window."""
    import webview

    window = webview.create_window(
        TITLE, html=page, width=1280, height=860, min_size=(640, 480), hidden=hidden
    )

    def on_closing() -> bool:
        if keep_open():
            window.hide()
            return False  # cancels the close
        return True

    window.events.closing += on_closing
    return window


def show(window: Any) -> None:
    window.show()
    window.restore()
