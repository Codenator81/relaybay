"""Sessions at once: the `max_concurrent_sessions` warning (an event and an Inbox line, it stops
nothing), and a session merge carrying its bindings."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from relaybay.db import repo
from relaybay.service import Service

DESKTOP = "local_00000000-0000-0000-0000-000000000001"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    await service.upsert_project("acme-shop", repo_path="/d/acme")
    await service.upsert_project("demo-api", repo_path="/d/demo")
    try:
        yield service
    finally:
        await service.close()


async def two_working(service: Service) -> None:
    for cc in ("a", "b"):
        await service.record_hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": cc, "cwd": f"/d/{cc}"}
        )


async def test_concurrency_warns_once_per_ten_minutes_and_stops_nothing(tmp_path: Path) -> None:
    service = Service(tmp_path / "relaybay.sqlite", max_concurrent_sessions=1)
    await service.start()
    try:
        await two_working(service)
        await service._check_concurrency(datetime.now(UTC))
        await service._check_concurrency(datetime.now(UTC))
        [event] = await service.activity(kind="concurrency.over")
        assert (event.payload["running"], event.payload["max_sessions"]) == (2, 1)
        inbox = await service.inbox(["owner"])
        assert (inbox.running_over, inbox.running_cap) == (2, 1)
        old = (datetime.now(UTC) - timedelta(minutes=11)).isoformat(timespec="milliseconds")
        async with service._tx() as tx:
            await tx.conn.execute("UPDATE event SET ts = ? WHERE kind = 'concurrency.over'", (old,))
        await service._check_concurrency(datetime.now(UTC))
        assert len(await service.activity(kind="concurrency.over")) == 2
        # Sessions quiet for ten minutes are not running.
        async with service._tx() as tx:
            await tx.conn.execute("UPDATE session SET last_seen_at = ?", (old,))
        assert (await service.inbox(["owner"])).running_over is None
    finally:
        await service.close()


async def test_the_loop_checks_only_with_a_cap(tmp_path: Path) -> None:
    import asyncio

    service = Service(tmp_path / "relaybay.sqlite", max_concurrent_sessions=1)
    await service.start()
    try:
        await two_working(service)
        loop = asyncio.create_task(service.concurrency_loop(interval=0.01))
        for _ in range(200):
            if await service.activity(kind="concurrency.over"):
                break
            await asyncio.sleep(0.01)
        loop.cancel()
        assert len(await service.activity(kind="concurrency.over")) == 1
    finally:
        await service.close()


async def test_concurrency_is_off_by_default(svc: Service) -> None:
    await two_working(svc)
    await svc._check_concurrency(datetime.now(UTC))
    assert await svc.activity(kind="concurrency.over") == []
    assert (await svc.inbox(["owner"])).running_over is None


async def test_a_merge_moves_open_bindings(svc: Service) -> None:
    # A guessed row bound to a task; the agent then links the id to its own session.
    payload = {"hook_event_name": "SessionStart", "session_id": "cc-9", "cwd": "/somewhere"}
    await svc.record_hook(payload)
    guessed = await repo.get_session_by_cc(svc._r, "cc-9")
    assert guessed is not None
    task = await svc.create_task("acme-shop", "Bound")
    await svc.bind_session(task.key, guessed.name)
    await svc.create_task(
        "demo-api", "Link", session=DESKTOP, session_title="desk", hook_session="cc-9"
    )
    assert (await svc.get_task(task.key)).session_name == "desk"
