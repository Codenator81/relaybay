"""`window-state.json`: the board window's size, position and maximized flag across restarts.

Machine-written, so it stays out of `desktop.toml`. Nothing here imports pywebview. Sizes and
positions are the logical pixels pywebview's events report and `create_window` takes.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from math import inf
from pathlib import Path
from typing import Any

from relaybay.security import _write_private

log = logging.getLogger("relaybay.desktop")

FILE_NAME = "window-state.json"
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (640, 480)
VALUE_LIMIT = 100_000  # beyond it the file is corrupt: the GUI toolkit would refuse the number
TITLE_BAR = 16  # px below the window's top edge: a point that is inside the title bar
SETTLE = 1.0  # seconds a move or resize stays revocable by a maximize or minimize that follows


@dataclass(frozen=True, slots=True)
class WindowState:
    width: int = DEFAULT_SIZE[0]
    height: int = DEFAULT_SIZE[1]
    x: int | None = None  # None: centred
    y: int | None = None
    maximized: bool = False


def _number(data: dict, key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if abs(value) <= VALUE_LIMIT else None


def _on_a_screen(state: WindowState, screens: Iterable[Any]) -> bool:
    """Whether the middle of the title bar is on a screen: that is what the user grabs."""
    assert state.x is not None and state.y is not None
    x, y = state.x + state.width // 2, state.y + TITLE_BAR
    return any(s.x <= x < s.x + s.width and s.y <= y < s.y + s.height for s in screens)


def load_state(folder: Path, screens: Iterable[Any]) -> WindowState:
    """The saved state fitted to `screens` (objects with x, y, width, height). A missing or corrupt
    file means the defaults; a size below the minimum is raised to it, a size above the largest
    screen is lowered to it; a position off every screen (a monitor was unplugged) is dropped, so
    the window is centred."""
    path = folder / FILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WindowState()
    except (OSError, ValueError, RecursionError) as exc:
        log.warning("%s could not be read, using the default window: %s", path, exc)
        return WindowState()
    if not isinstance(data, dict):
        log.warning("%s is not a window state, using the default window", path)
        return WindowState()
    screens = list(screens)
    width = _number(data, "width") or DEFAULT_SIZE[0]
    height = _number(data, "height") or DEFAULT_SIZE[1]
    if screens:
        width = min(width, max(s.width for s in screens))
        height = min(height, max(s.height for s in screens))
    state = WindowState(
        width=max(width, MIN_SIZE[0]),
        height=max(height, MIN_SIZE[1]),
        x=_number(data, "x"),
        y=_number(data, "y"),
        maximized=data.get("maximized") is True,
    )
    if state.x is None or state.y is None or not _on_a_screen(state, screens):
        return replace(state, x=None, y=None)
    return state


def save_state(folder: Path, state: WindowState) -> None:
    """Write the file atomically (temp file + rename)."""
    fields = {
        "x": state.x,
        "y": state.y,
        "width": state.width,
        "height": state.height,
        "maximized": state.maximized,
    }
    _write_private(folder / FILE_NAME, json.dumps(fields))


class WindowTracker:
    """Follows the window's events and keeps its normal (not maximized, not minimized) geometry.

    pywebview runs each of these events on a thread of its own, so their order is not guaranteed,
    and WinForms reports a maximize as Move (-8, -8), maximized, Resize (the screen) and a minimize
    as Move (-32000, -32000), minimized, Resize (the taskbar stub). The geometry just before a
    maximize or minimize therefore cannot be trusted: it is held for `SETTLE` seconds and dropped
    when one arrives.
    """

    def __init__(self, saved: WindowState, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._normal = saved
        self._recent: list[tuple[float, dict[str, int]]] = []
        self.maximized = saved.maximized  # also while minimized: what un-minimizing returns to
        self.minimized = False

    def _commit(self, before: float) -> None:
        keep = []
        for at, values in self._recent:
            if at < before:
                self._normal = replace(self._normal, **values)
            else:
                keep.append((at, values))
        self._recent = keep

    def _geometry(self, **values: int) -> None:
        with self._lock:
            if not (self.maximized or self.minimized):
                self._recent.append((self._clock(), values))

    def _leave_normal(self) -> None:
        self._commit(self._clock() - SETTLE)
        self._recent.clear()

    def on_moved(self, x: float, y: float) -> None:
        self._geometry(x=int(x), y=int(y))

    def on_resized(self, width: float, height: float) -> None:
        self._geometry(width=int(width), height=int(height))

    def on_minimized(self) -> None:
        with self._lock:
            self._leave_normal()
            self.minimized = True

    def on_maximized(self) -> None:
        # WinForms goes minimized -> maximized with this event alone, never `restored`.
        with self._lock:
            self._leave_normal()
            self.maximized = True
            self.minimized = False

    def on_restored(self) -> None:
        # From minimized the flag stays: GTK reports un-minimizing a maximized window this way.
        with self._lock:
            if self.minimized:
                self.minimized = False
            else:
                self.maximized = False

    def snapshot(self) -> WindowState:
        with self._lock:
            self._commit(inf)
            return replace(self._normal, maximized=self.maximized)
