"""Tray notifications: asks the server what needs the owner, groups it, shows at most one toast
per window. The icon, the clock and the HTTP call come from outside, so this is testable without
a display, and it is the same for the embedded and an external server.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request

from relaybay.desktop.server_thread import server_opener
from relaybay.domain.notifications import LABELS
from relaybay.security import _write_private

log = logging.getLogger("relaybay.desktop")

STATE_FILE = "notify_state.json"
WINDOW = 20.0  # seconds: at most one toast per window, repeats in it are dropped
FETCH_TIMEOUT = 5.0
PAGE = 100
SUMMARY_TITLE = "relaybay"
# NOTIFYICONDATA's fields on Windows, in UTF-16 units without the terminator: pystray raises on
# more.
TITLE_MAX = 63
TEXT_MAX = 255
FETCH_ERRORS = (URLError, OSError, ValueError, HTTPException)


@dataclass(frozen=True, slots=True)
class Item:
    kind: str
    title: str
    text: str
    key: str  # the task (or session) it is about; "" for a notice of the program itself

    @property
    def group(self) -> tuple[str, str]:
        return self.kind, self.key or self.title


@dataclass(frozen=True, slots=True)
class Page:
    last_id: int
    items: tuple[Item, ...]


def fetch_page(base_url: str, token: str, after: int | None) -> Page:
    """One page of `GET /api/notifications`; raises one of FETCH_ERRORS when it cannot."""
    query = {"limit": PAGE} | ({} if after is None else {"after": after})
    request = Request(
        f"{base_url}/api/notifications?{urlencode(query)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with server_opener(base_url).open(request, timeout=FETCH_TIMEOUT) as response:
        data = json.load(response)
    try:
        items = tuple(
            Item(i["kind"], i["title"], i["text"], i.get("key") or "") for i in data["items"]
        )
        return Page(int(data["last_id"]), items)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"not a notifications answer: {exc!r}") from None


def summary(items: Iterable[Item]) -> str:
    """`3 need you: 2 reviews, 1 reply`, in the order of the kinds."""
    items = list(items)
    parts = []
    for kind, (one, many) in LABELS.items():
        count = sum(item.kind == kind for item in items)
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    return f"{len(items)} need you: {', '.join(parts)}"


def fit(text: str, limit: int) -> str:
    def units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    if units(text) <= limit:
        return text
    while units(text) > limit - 3:
        text = text[:-1]
    return text.rstrip() + "..."


class Notifier:
    """All of it runs under one lock: the poll thread, the watchdog's `local` and the tray's
    `set_quiet` arrive on different threads. The HTTP call is outside it."""

    def __init__(
        self,
        folder: Path,
        *,
        fetch: Callable[[int | None], Page],
        toast: Callable[[str, str], None],
        dot: Callable[[bool], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = folder / STATE_FILE
        self._fetch = fetch
        self._toast = toast
        self._dot = dot
        self._clock = clock
        self._lock = threading.Lock()
        self.enabled = True
        self.quiet = False
        self.kinds: tuple[str, ...] = tuple(LABELS)
        self._server = ""
        self.cursor: int | None = None  # None: start from the newest event
        self.pending: dict[tuple[str, str], Item] = {}
        self._last_toast: float | None = None
        self._shown: set[tuple[str, str]] = set()
        self._dot_on = False
        self._saved: object = None
        self._failing = False
        self._load()

    # --- state file ---------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._server = str(data["server"])
            cursor = data["cursor"]
            self.cursor = None if cursor is None else int(cursor)
            items = [Item(**item) for item in data["pending"]]
            self.pending = {item.group: item for item in items}
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
            log.warning("%s could not be read, notifications start anew: %s", self._path, exc)
            self._server, self.cursor, self.pending = "", None, {}
        self._saved = self._snapshot()

    def _snapshot(self) -> dict[str, object]:
        return {
            "server": self._server,
            "cursor": self.cursor,
            "pending": [asdict(item) for item in self.pending.values()],
        }

    def _save(self) -> None:
        """A failure costs at most a repeated or a lost notice after a restart."""
        snapshot = self._snapshot()
        if snapshot == self._saved:
            return
        try:
            _write_private(self._path, json.dumps(snapshot))
        except OSError as exc:
            log.warning("the notification state was not saved: %s", exc)
            return
        self._saved = snapshot

    # --- settings -----------------------------------------------------------

    def configure(self, enabled: bool, quiet: bool, kinds: tuple[str, ...], server: str) -> None:
        """`server` names the board the cursor belongs to: another one starts from its newest."""
        with self._lock:
            self.enabled, self.quiet, self.kinds = enabled, quiet, kinds
            if server != self._server:
                self._server, self.cursor = server, None
                self.pending.clear()

    def set_quiet(self, on: bool) -> None:
        """Leaving quiet mode shows what it held, whatever the window says."""
        with self._lock:
            self.quiet = on
            try:
                self._flush(force=True)
            finally:
                self._save()

    # --- the flow -----------------------------------------------------------

    def tick(self) -> None:
        """One poll: read everything new (so a long downtime ends in one summary), then toast."""
        if not self.enabled:
            with self._lock:
                # Off means off: on again starts from the newest event, not with a summary.
                self.cursor = None
                self.pending.clear()
                self._set_dot(False)
                self._save()
            return
        try:
            self._drain()
        except FETCH_ERRORS as exc:
            # The watchdog tells the owner about a server that is gone; the cursor stays put.
            if not self._failing:
                log.warning("tray notifications paused: %s", exc)
            self._failing = True
        else:
            if self._failing:
                log.info("tray notifications resumed")
            self._failing = False
        with self._lock:
            try:
                self._flush()
            finally:
                self._save()

    def _drain(self) -> None:
        while True:
            before = self.cursor
            page = self._fetch(before)
            with self._lock:
                self.cursor = page.last_id
                if before is not None:
                    self._add(page.items)
            if before is None or page.last_id == before:
                return

    def local(self, kind: str, title: str, text: str) -> None:
        """A notice of the program itself: no cursor, it goes the same way as the server's."""
        with self._lock:
            if not self.enabled:
                return
            self._add((Item(kind, title, text, ""),))
            try:
                self._flush()
            finally:
                self._save()

    def _in_window(self) -> bool:
        return self._last_toast is not None and self._clock() - self._last_toast < WINDOW

    def _add(self, items: Iterable[Item]) -> None:
        shown = self._shown if self._in_window() else set()
        for item in items:
            if item.kind not in self.kinds or item.group in shown:
                continue
            # A move to review that also sets waiting_on is two events and one thing to do.
            review = ("review", item.key)
            if item.kind == "waiting" and (review in self.pending or review in shown):
                continue
            if item.kind == "review":
                self.pending.pop(("waiting", item.key), None)
            self.pending.pop(item.group, None)
            self.pending[item.group] = item  # the newest wording, last in the order

    def _set_dot(self, on: bool) -> None:
        if on != self._dot_on:
            self._dot(on)
            self._dot_on = on

    def _flush(self, force: bool = False) -> None:
        if self.quiet:
            self._set_dot(bool(self.pending))
            return
        self._set_dot(False)
        if not self.pending or (self._in_window() and not force):
            return
        items = list(self.pending.values())
        if len(items) == 1:
            title, text = items[0].title, items[0].text or items[0].title
        else:
            title, text = SUMMARY_TITLE, summary(items)
        # Before the call: a tray that refuses is asked again a window later, not every poll.
        self._last_toast = self._clock()
        self._toast(fit(text, TEXT_MAX), fit(title, TITLE_MAX))
        self._shown = set(self.pending)
        self.pending.clear()
