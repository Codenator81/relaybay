"""In-process event bus: the service publishes committed events, SSE streams subscribe."""

import asyncio
import contextlib
from collections.abc import Iterator

from relaybay.domain.models import Event

DEFAULT_QUEUE_SIZE = 256


class Subscription:
    def __init__(self, project_id: int | None, maxsize: int) -> None:
        self.project_id = project_id
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = asyncio.Event()

    def wants(self, event: Event) -> bool:
        return self.project_id is None or event.project_id == self.project_id

    def close(self) -> None:
        """Drop the subscriber and wake a waiting `get` at once (the server shuts down)."""
        self.dropped.set()
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)  # a full queue wakes the consumer by itself

    async def get(self, idle_seconds: float) -> Event | None:
        """Next event, or None after `idle_seconds` of silence.

        Raises `SubscriberDropped` once the bus gave up on this subscriber; the client is expected
        to reconnect and catch up from the event table.
        """
        if self.dropped.is_set() and self.queue.empty():
            raise SubscriberDropped
        try:
            event = await asyncio.wait_for(self.queue.get(), idle_seconds)
        except TimeoutError:
            if self.dropped.is_set():
                raise SubscriberDropped from None
            return None
        if event is None:  # the close marker
            raise SubscriberDropped
        return event


class SubscriberDropped(Exception):
    pass


class EventBus:
    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @contextlib.contextmanager
    def subscribe(self, project_id: int | None = None) -> Iterator[Subscription]:
        sub = Subscription(project_id, self._queue_size)
        if self._closed:
            sub.close()  # a request accepted just before the shutdown: ends at once
        else:
            self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    def close(self) -> None:
        """End every stream now: the server is shutting down and must not wait for them (nor cut
        them with a traceback). The browsers reconnect to the next server by themselves."""
        self._closed = True
        for sub in list(self._subscribers):
            sub.close()
        self._subscribers.clear()

    def publish(self, event: Event) -> None:
        for sub in list(self._subscribers):
            if not sub.wants(event):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow consumer must not hold events in memory or block writers.
                sub.dropped.set()
                self._subscribers.discard(sub)
