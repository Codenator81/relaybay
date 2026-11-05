"""The tray icon and its menu.

The menu is first built as plain data from the program's state (`build_menu`), then turned into
pystray objects (`to_pystray`), so it is testable without a display or pystray.
"""

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

SEPARATOR = "-"
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
SM_CXSMICON = 49


@dataclass(frozen=True, slots=True)
class MenuState:
    server_mode: str  # embedded | external
    autostart: bool
    has_window: bool  # False: no web view (Linux without WebKitGTK), the board opens in a browser


@dataclass(frozen=True, slots=True)
class Entry:
    text: str
    action: str | None = None  # the controller method name; None for a submenu or separator
    checked: bool | None = None  # None: not a checkbox
    default: bool = False  # the item a click (Windows: double click) on the icon runs
    submenu: tuple["Entry", ...] = ()


def build_menu(state: MenuState) -> tuple[Entry, ...]:
    restart = "Restart server" if state.server_mode == "embedded" else "Reconnect"
    return (
        Entry("Open board", "show_window", default=True),
        Entry("Open in browser", "open_in_browser"),
        Entry(SEPARATOR),
        Entry(restart, "restart"),
        Entry("Logs", "open_logs"),
        Entry(
            "Settings",
            submenu=(
                Entry("Start at login", "toggle_autostart", checked=state.autostart),
                Entry("Data folder...", "open_settings"),
                Entry("Server...", "open_settings"),
            ),
        ),
        Entry(SEPARATOR),
        Entry("Quit relaybay", "quit"),
    )


def panel_theme() -> str | None:
    """`light` or `dark`: the colour of the panel the tray icon sits on, where the OS tells it
    (Windows taskbar, macOS menu bar); None elsewhere, which means the colour icon."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
                value, _kind = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        except FileNotFoundError:
            return None  # Windows before 10 1903: no light taskbar
        return "light" if value else "dark"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True
        )
        # The key exists only in dark mode; "does not exist" means light.
        return "dark" if result.stdout.strip() == "Dark" else "light"
    return None


def icon_file(theme: str | None, small: bool) -> str:
    """The icon for a panel: monochrome (dark figure on a light panel, white on a dark one) when
    the theme is known, else the colour mark."""
    if theme is None:
        return "relaybay.png"
    return f"tray-{theme}{'-16' if small else ''}.png"


def small_icons() -> bool:
    """Windows at 100 % scale draws tray icons at 16 px: use the drawing made for 16 px."""
    if sys.platform != "win32":
        return False
    import ctypes

    return ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON) <= 16


def icon_image() -> Any:
    from PIL import Image

    name = icon_file(panel_theme(), small_icons())
    with files("relaybay.desktop").joinpath("icons", name).open("rb") as fh:
        image = Image.open(fh)
        image.load()
    return image


def to_pystray(entries: tuple[Entry, ...], dispatch: Callable[[str], None]) -> Any:
    import pystray

    def item(entry: Entry) -> Any:
        if entry.text == SEPARATOR:
            return pystray.Menu.SEPARATOR
        if entry.submenu:
            return pystray.MenuItem(entry.text, pystray.Menu(*map(item, entry.submenu)))
        action = entry.action
        assert action is not None
        checked = entry.checked
        return pystray.MenuItem(
            entry.text,
            lambda _icon, _item: dispatch(action),
            checked=None if checked is None else (lambda _item: checked),
            default=entry.default,
        )

    return pystray.Menu(*map(item, entries))


def create_icon(entries: tuple[Entry, ...], dispatch: Callable[[str], None]) -> Any:
    import pystray

    return pystray.Icon("relaybay", icon_image(), "relaybay", menu=to_pystray(entries, dispatch))
