"""`GET /events`: Server-Sent Events with `Last-Event-ID` replay from the event table."""

import json
from collections.abc import AsyncIterator

from relaybay.domain.models import Event
from relaybay.events import SubscriberDropped
from relaybay.service import Service

RETRY_MS = 3000
_PAYLOAD_KEYS = ("project", "key", "stage", "from", "to", "session", "summary", "attachment_id")


def format_event(event: Event) -> str:
    data = {"kind": event.kind}
    data.update({k: event.payload[k] for k in _PAYLOAD_KEYS if event.payload.get(k) is not None})
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.kind}\ndata: {body}\n\n"


def resync(last_id: int) -> str:
    return f'id: {last_id}\nevent: resync\ndata: {{"kind":"resync"}}\n\n'


async def event_stream(
    service: Service,
    *,
    project_id: int | None,
    last_event_id: int | None,
    replay_limit: int,
    heartbeat: float,
) -> AsyncIterator[str]:
    # Subscribe before reading the backlog so nothing committed in between is lost; duplicates
    # are skipped by id below.
    with service.bus.subscribe(project_id) as sub:
        yield f"retry: {RETRY_MS}\n\n"
        seen = last_event_id
        if last_event_id is not None:
            newest = await service.last_event_id()
            backlog = await service.replay(last_event_id, project_id, replay_limit + 1)
            if last_event_id > newest or len(backlog) > replay_limit:
                yield resync(newest)
                seen = newest
            else:
                for event in backlog:
                    yield format_event(event)
                    seen = event.id
        while True:
            try:
                event = await sub.get(heartbeat)
            except SubscriberDropped:
                return  # the browser reconnects with Last-Event-ID and replays the gap
            if event is None:
                yield ": ping\n\n"
                continue
            if seen is not None and event.id <= seen:
                continue
            seen = event.id
            yield format_event(event)
