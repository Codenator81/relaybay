import asyncio

import pytest

from relaybay.db import repo
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.domain.models import Event
from relaybay.domain.reports import parse_report
from relaybay.domain.stages import Stage
from relaybay.events import EventBus, SubscriberDropped
from relaybay.service import Service


async def test_project_and_task_keys(service: Service) -> None:
    project, created = await service.upsert_project("acme-shop", name="Acme Shop")
    assert created and project.name == "Acme Shop"
    _, created_again = await service.upsert_project("acme-shop")
    assert not created_again

    first = await service.create_task("acme-shop", "Checkout page")
    second = await service.create_task("acme-shop", "Payment retries", stage="dev")
    assert (first.key, second.key) == ("acme-shop-1", "acme-shop-2")
    assert first.stage is Stage.PLAN and first.version == 1

    await service.upsert_project("demo-api")
    other = await service.create_task("demo-api", "Rate limits")
    assert other.key == "demo-api-1"


async def test_invalid_input_is_rejected(service: Service) -> None:
    with pytest.raises(ValidationError):
        await service.upsert_project("Acme Shop!")
    await service.upsert_project("acme-shop")
    with pytest.raises(ValidationError):
        await service.create_task("acme-shop", "   ")
    with pytest.raises(ValidationError):
        await service.create_task("acme-shop", "x", stage="review")
    with pytest.raises(NotFoundError):
        await service.create_task("nope", "x")


async def test_every_write_appends_and_publishes_an_event(service: Service) -> None:
    await service.upsert_project("acme-shop")
    with service.bus.subscribe() as sub:
        task = await service.create_task("acme-shop", "Checkout", session="session-a")
        received = [await sub.get(1), await sub.get(1), await sub.get(1)]
    assert [e.kind for e in received] == ["session.seen", "task.created", "task.session_bound"]
    created = received[1]
    assert created.payload["key"] == task.key and created.actor == "session-a"

    stored = await service.events_since(0)
    kinds = [e.kind for e in stored]
    assert kinds == ["project.created", "session.seen", "task.created", "task.session_bound"]
    assert stored[2].id == created.id


async def test_stage_change(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    moved = await service.set_stage(task.key, "test", note="PR open")
    assert moved.stage is Stage.TEST and moved.stage_note == "PR open"
    assert moved.version == task.version + 1

    done = await service.set_stage(task.key, "done")
    assert done.closed_at is not None
    reopened = await service.set_stage(task.key, "dev")
    assert reopened.closed_at is None

    kinds = [
        (e.kind, e.payload.get("from"), e.payload.get("to")) for e in await service.events_since(0)
    ]
    assert ("task.stage_changed", "plan", "test") in kinds
    assert ("task.stage_changed", "done", "dev") in kinds


async def test_unchanged_update_is_a_no_op(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="dev")
    before = await service.last_event_id()
    same = await service.update_task(task.key, {"stage": "dev", "title": "Checkout"})
    assert same.version == task.version
    assert await service.last_event_id() == before


async def test_version_conflict(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    await service.update_task(task.key, {"waiting_on": "owner"}, expected_version=task.version)
    with pytest.raises(ConflictError, match="version 2, not 1"):
        await service.update_task(task.key, {"waiting_on": "reviewer"}, expected_version=1)
    current = await service.get_task(task.key)
    assert current.waiting_on == "owner" and current.version == 2


async def test_failed_write_rolls_back(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    before = await service.last_event_id()
    with pytest.raises(ConflictError):
        await service.update_task(task.key, {"title": "New"}, expected_version=99, session="s1")
    assert await service.last_event_id() == before
    assert (await service.get_task(task.key)).title == "Checkout"


async def test_reports_sessions_and_decisions(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    bound, _, _ = await service.bind_session(task.key, "checkout-session")
    assert bound.session_name == "checkout-session"

    _, report = await service.add_report(
        task.key, "verified · M · checkout works\n\nDetails here.", session="checkout-session"
    )
    assert (report.state, report.size, report.summary) == ("verified", "M", "checkout works")

    decision = await service.add_decision("acme-shop", "Ship without coupons", task=task.key)
    assert decision.task_key == task.key

    detail = await service.task_detail(task.key)
    assert detail.reports[0].id == report.id and detail.decisions[0].id == decision.id
    tasks = await service.list_tasks(project="acme-shop", session="checkout-session")
    assert [t.key for t in tasks] == [task.key]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("verified locally · S · login fixed", ("verified", "S", "login fixed")),
        ("blocked · l · needs a decision\nmore", ("blocked", "L", "needs a decision")),
        ("Just a note", (None, None, "Just a note")),
        ("weird · M · not a state", (None, None, "weird · M · not a state")),
        ("\n\n## Heading line\nbody", (None, None, "Heading line")),
    ],
)
def test_parse_report(body: str, expected: tuple[str | None, str | None, str]) -> None:
    assert parse_report(body) == expected


async def test_slow_subscriber_is_dropped() -> None:
    bus = EventBus(queue_size=2)
    with bus.subscribe() as slow:
        for i in range(3):
            bus.publish(Event(i + 1, "ts", "task.updated", None, None, None, "t"))
        assert bus.subscriber_count == 0
        assert (await slow.get(0.1)).id == 1
        assert (await slow.get(0.1)).id == 2
        with pytest.raises(SubscriberDropped):
            await slow.get(0.1)


async def test_project_filter_on_bus(service: Service) -> None:
    acme, _ = await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    with service.bus.subscribe(acme.id) as sub:
        await service.create_task("demo-api", "Other project")
        await service.create_task("acme-shop", "Mine")
        event = await sub.get(1)
        assert event is not None and event.payload["key"] == "acme-shop-1"
        assert await asyncio.wait_for(sub.get(0.05), 1) is None


async def test_description_and_progress(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task(
        "acme-shop", "Checkout", description="  Build the **checkout**.\r\nCards only.  "
    )
    assert task.description == "Build the **checkout**.\nCards only." and task.progress is None

    updated = await service.update_task(task.key, {"progress": "- cart"})
    assert updated.progress == "- cart" and updated.version == task.version + 1
    event = (await service.events_since(0))[-1]
    assert event.kind == "task.updated" and event.payload["changes"] == {"progress": "- cart"}

    cleared = await service.update_task(task.key, {"description": None})
    assert cleared.description is None
    with pytest.raises(ValidationError, match="20000"):
        await service.update_task(task.key, {"progress": "x" * 20001})


async def test_report_with_progress_is_one_transaction(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    with service.bus.subscribe() as sub:
        saved, report = await service.add_report(
            task.key, "changed · S · cart", session="checkout-dev", progress="- cart"
        )
        kinds = [(await sub.get(1)).kind for _ in range(3)]
    assert kinds == ["session.seen", "task.updated", "report.added"]
    assert saved.progress == "- cart" and saved.version == task.version + 1
    assert report.summary == "cart"

    # Same progress again: only the report is added, the version stays.
    again, _ = await service.add_report(task.key, "second", progress="- cart")
    assert again.version == saved.version
    # Without progress the summary is kept; '' clears it.
    kept, _ = await service.add_report(task.key, "third")
    assert kept.progress == "- cart"
    cleared, _ = await service.add_report(task.key, "fourth", progress="")
    assert cleared.progress is None


async def test_failed_report_rolls_back_progress(service: Service, monkeypatch) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", progress="- cart")
    last = await service.last_event_id()

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo, "insert_report", broken)
    with pytest.raises(RuntimeError):
        await service.add_report(task.key, "report", progress="- cart\n- totals")
    after = await service.get_task(task.key)
    assert (after.progress, after.version) == ("- cart", task.version)
    assert await service.last_event_id() == last
