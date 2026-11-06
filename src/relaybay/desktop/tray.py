"""The tray icon and its menu.

The menu is first built as plain data from the program's state (`build_menu`), then turned into
pystray objects (`to_pystray`), so it is testable without a display or pystray.
"""

import ctypes
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

log = logging.getLogger("relaybay.desktop")

SEPARATOR = "-"
PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
SM_CXSMICON = 49
UPDATE_DOT = "#c77700"  # the board's --warn (light theme)
PENDING_DOT = "#3b5bdb"  # the board's --accent (light theme)
DARK_MENUS_MIN_BUILD = 18362  # Windows 10 1903
APP_MODE_FORCE_DARK = 2  # needs no per-window call, unlike 1 (allow dark)


@dataclass(frozen=True, slots=True)
class MenuState:
    server_mode: str  # embedded | external
    autostart: bool
    has_window: bool  # False: no web view (Linux without WebKitGTK), the board opens in a browser
    update_version: str | None = None  # a newer release the program found
    notify: bool = True  # tray notifications are on
    quiet: bool = False


@dataclass(frozen=True, slots=True)
class Entry:
    text: str
    action: str | None = None  # the controller method name; None for a submenu or separator
    checked: bool | None = None  # None: not a checkbox
    default: bool = False  # the item a click (Windows: double click) on the icon runs
    submenu: tuple["Entry", ...] = ()


def build_menu(state: MenuState) -> tuple[Entry, ...]:
    restart = "Restart server" if state.server_mode == "embedded" else "Reconnect"
    update = (
        (Entry(f"Update to {state.update_version}", "install_update"),)
        if state.update_version
        else ()
    )
    quiet = (
        (Entry("Quiet notifications", "toggle_quiet", checked=state.quiet),) if state.notify else ()
    )
    return (
        Entry("Open board", "show_window", default=True),
        Entry("Open in browser", "open_in_browser"),
        Entry("Open Inbox", "open_inbox"),
        Entry(SEPARATOR),
        *quiet,
        Entry(restart, "restart"),
        Entry("Logs", "open_logs"),
        Entry(
            "Settings",
            submenu=(
                Entry("Start at login", "toggle_autostart", checked=state.autostart),
                Entry("Data folder...", "open_settings"),
                Entry("Server...", "open_settings"),
                Entry("Check for updates", "check_updates"),
            ),
        ),
        *update,
        Entry(SEPARATOR),
        Entry("Quit relaybay", "quit"),
    )


def _os_theme(windows_value: str) -> str | None:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
                value, _kind = winreg.QueryValueEx(key, windows_value)
        except FileNotFoundError:
            return None  # a Windows older than this setting
        return "light" if value else "dark"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True
        )
        # The key exists only in dark mode; "does not exist" means light.
        return "dark" if result.stdout.strip() == "Dark" else "light"
    return None


def panel_theme() -> str | None:
    """`light` or `dark`: the colour of the panel the tray icon sits on, where the OS tells it
    (Windows taskbar, macOS menu bar); None elsewhere, which means the colour icon."""
    return _os_theme("SystemUsesLightTheme")


def app_theme() -> str | None:
    """`light` or `dark`: the theme for application windows, where the OS tells it; on Windows it
    is set apart from the taskbar's. None elsewhere."""
    return _os_theme("AppsUseLightTheme")


def follow_system_menu_theme(
    windows_build: Callable[[], int] = lambda: sys.getwindowsversion().build,
    load_library: Callable[[str], Any] = lambda name: ctypes.WinDLL(name),
    theme: Callable[[], str | None] = app_theme,
) -> None:
    """Make this process's native popup menus dark on a machine in the dark theme (Windows), read
    once at start; a light machine is left alone. Any failure leaves the light menu and a debug
    line.

    Undocumented uxtheme.dll exports, by ordinal, used widely in native apps: 135 is
    SetPreferredAppMode only from build 18362 on (it means something else before), 136 is
    FlushMenuThemes. The mode is process-wide, so the thread that calls does not matter.
    """
    try:
        if theme() != "dark":
            return
        build = windows_build()
        if build < DARK_MENUS_MIN_BUILD:
            log.debug("Windows build %s: the tray menu stays light", build)
            return
        uxtheme = load_library("uxtheme")
        uxtheme[135](APP_MODE_FORCE_DARK)
        uxtheme[136]()
    except Exception as exc:
        log.debug("the tray menu stays light: %s", exc)


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
    return ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON) <= 16


def icon_image(update: bool = False, pending: bool = False) -> Any:
    """The tray icon; with `update`, a warn-coloured dot in the lower right corner; with `pending`
    (quiet mode holds notifications), an accent-coloured dot in the upper right one."""
    from PIL import Image, ImageDraw

    name = icon_file(panel_theme(), small_icons())
    with files("relaybay.desktop").joinpath("icons", name).open("rb") as fh:
        image = Image.open(fh)
        image.load()
    if update or pending:
        image = image.convert("RGBA")
        draw = ImageDraw.Draw(image)
        size = min(image.size)
        dot = max(size * 3 // 8, 6)
        left, right, width = image.width - dot, image.width - 1, max(size // 16, 1)
        if update:
            box = (left, image.height - dot, right, image.height - 1)
            draw.ellipse(box, fill=UPDATE_DOT, outline="white", width=width)
        if pending:
            draw.ellipse((left, 0, right, dot - 1), fill=PENDING_DOT, outline="white", width=width)
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

    if sys.platform == "win32":
        follow_system_menu_theme()  # pystray builds its first HMENU when its thread starts
    return pystray.Icon("relaybay", icon_image(), "relaybay", menu=to_pystray(entries, dispatch))
