import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx

from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import LiveServer


async def seed(home: Path) -> None:
    """project.created (1), task.created (2, 3, 4)."""
    service = Service(home / "relaybay.sqlite")
    await service.start()
    try:
        await service.upsert_project("acme-shop")
        for title in ("One", "Two", "Three"):
            await service.create_task("acme-shop", title)
    finally:
        await service.close()


async def read_events(response: httpx.Response, count: int) -> list[dict[str, str]]:
    """Collect `count` SSE messages (comments and the retry hint are skipped)."""
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    async for line in response.aiter_lines():
        if line == "":
            if "event" in current:
                events.append(current)
                if len(events) == count:
                    return events
            current = {}
        elif not line.startswith(":"):
            field, _, value = line.partition(": ")
            current[field] = value
    return events


async def stream(live: LiveServer, count: int, *, path: str, headers: dict[str, str]):
    async with (
        httpx.AsyncClient(base_url=live.url, headers=live.auth, timeout=10) as client,
        client.stream("GET", path, headers=headers) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return await asyncio.wait_for(read_events(response, count), 10)


async def test_replay_from_last_event_id(
    tmp_path: Path, live_server: Callable[..., LiveServer]
) -> None:
    await seed(tmp_path)
    live = live_server()
    events = await stream(live, 2, path="/events?project=acme-shop", headers={"Last-Event-ID": "2"})
    assert [e["id"] for e in events] == ["3", "4"]
    assert all(e["event"] == "task.created" for e in events)
    data = json.loads(events[0]["data"])
    assert data == {
        "kind": "task.created",
        "project": "acme-shop",
        "key": "acme-shop-2",
        "stage": "plan",
    }


async def test_header_wins_over_query(
    tmp_path: Path, live_server: Callable[..., LiveServer]
) -> None:
    await seed(tmp_path)
    live = live_server()
    events = await stream(live, 1, path="/events?last_event_id=1", headers={"Last-Event-ID": "3"})
    assert events[0]["id"] == "4"
    events = await stream(live, 1, path="/events?last_event_id=3", headers={})
    assert events[0]["id"] == "4"


async def test_too_far_behind_gets_resync(
    tmp_path: Path, live_server: Callable[..., LiveServer]
) -> None:
    await seed(tmp_path)
    live = live_server()
    live.settings.replay_limit = 2
    events = await stream(live, 1, path="/events", headers={"Last-Event-ID": "0"})
    assert events[0]["event"] == "resync" and events[0]["id"] == "4"
    # An id from a different (reset) database is also answered with resync.
    events = await stream(live, 1, path="/events", headers={"Last-Event-ID": "999"})
    assert events[0]["event"] == "resync"


async def test_live_event_and_heartbeat(
    tmp_path: Path, live_server: Callable[..., LiveServer]
) -> None:
    await seed(tmp_path)
    live = live_server()

    async def edit_card_soon() -> None:
        await asyncio.sleep(0.8)
        async with httpx.AsyncClient(base_url=live.url, headers=live.auth) as client:
            response = await client.post(
                "/p/acme-shop/t/acme-shop-1",
                # Bearer-authenticated: no Origin needed, the CSRF field still is.
                data={
                    "title": "One",
                    "stage": "test",
                    "version": "1",
                    "csrf_token": csrf_token(live.token),
                },
            )
            assert response.status_code == 200

    async with (
        httpx.AsyncClient(base_url=live.url, headers=live.auth, timeout=10) as client,
        client.stream("GET", "/events?project=acme-shop") as response,
    ):
        lines: list[str] = []
        editor = asyncio.create_task(edit_card_soon())

        async def until_event() -> None:
            async for line in response.aiter_lines():
                lines.append(line)
                if line.startswith("data: ") and "task.stage_changed" in line:
                    return

        await asyncio.wait_for(until_event(), 10)
        await editor
    assert "id: 5" in lines and "event: task.stage_changed" in lines
    assert ": ping" in lines  # heartbeat (0.5 s in tests) while waiting for the edit
    stage_data = json.loads(lines[-1][len("data: ") :])
    assert stage_data["from"] == "plan" and stage_data["to"] == "test"
