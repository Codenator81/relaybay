import uuid
from pathlib import Path

import pytest

from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.domain.sessions import parse_session_id
from relaybay.service import Service

DESKTOP_A = f"local_{uuid.UUID(int=1)}"
DESKTOP_B = f"local_{uuid.UUID(int=2)}"


def test_parse_session_id() -> None:
    assert parse_session_id(DESKTOP_A) == DESKTOP_A
    assert parse_session_id("checkout-dev") is None
    canonical = "local_123e4567-e89b-12d3-a456-426614174000"
    assert parse_session_id("LOCAL_123E4567-E89B-12D3-A456-426614174000") == canonical
    assert parse_session_id(f"  {canonical} ") == canonical
    for bad in (
        "local_not-a-uuid",
        "local_{123e4567-e89b-12d3-a456-426614174000}",
        "local_urn:uuid:123e4567-e89b-12d3-a456-426614174000",
        "local_123e4567e89b12d3a456426614174000",
        f"{canonical}0",
    ):
        with pytest.raises(ValidationError, match="local_<uuid>"):
            parse_session_id(bad)


async def test_uppercase_id_finds_the_same_session(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", session=DESKTOP_A)
    again = await service.bind_session(task.key, DESKTOP_A.upper())
    assert again.session_id == task.session_id
    assert again.session_link == f"claude://claude.ai/epitaxy/{DESKTOP_A}"


async def test_desktop_session_gets_a_link(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task(
        "acme-shop", "Checkout", session=DESKTOP_A, session_title="Checkout work"
    )
    assert task.session_name == "Checkout work"
    assert task.session_external_id == DESKTOP_A
    assert task.session_link == f"claude://claude.ai/epitaxy/{DESKTOP_A}"
    assert task.session_status is None  # unknown until something reports it

    # A later call with a new title renames the session; without a title the name stays.
    await service.add_report(task.key, "note", session=DESKTOP_A, session_title="Checkout v2")
    await service.add_report(task.key, "note", session=DESKTOP_A)
    assert (await service.get_task(task.key)).session_name == "Checkout v2"


async def test_link_template_is_configurable(tmp_path: Path) -> None:
    svc = Service(tmp_path / "db.sqlite", desktop_link_template="claude://open/{id}?from=board")
    await svc.start()
    try:
        await svc.upsert_project("demo-api")
        task = await svc.create_task("demo-api", "Limits", session=DESKTOP_A)
        assert task.session_link == f"claude://open/{DESKTOP_A}?from=board"
        assert task.session_name == DESKTOP_A  # no title given: the id is the name
    finally:
        await svc.close()
    with pytest.raises(ValueError, match="id"):
        Service(tmp_path / "db.sqlite", desktop_link_template="claude://no-placeholder")


async def test_plain_names_have_no_link(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", session="checkout-dev")
    assert task.session_name == "checkout-dev"
    assert task.session_external_id is None and task.session_link is None


async def test_desktop_titles_may_repeat(service: Service) -> None:
    await service.upsert_project("acme-shop")
    one = await service.create_task("acme-shop", "A", session=DESKTOP_A, session_title="Same")
    two = await service.create_task("acme-shop", "B", session=DESKTOP_B, session_title="Same")
    assert one.session_id != two.session_id
    by_id = await service.list_tasks(project="acme-shop", session=DESKTOP_B)
    assert [t.key for t in by_id] == [two.key]
    by_upper_id = await service.list_tasks(project="acme-shop", session=DESKTOP_B.upper())
    assert [t.key for t in by_upper_id] == [two.key]
    by_title = await service.list_tasks(project="acme-shop", session="Same")
    assert {t.key for t in by_title} == {one.key, two.key}


async def test_binding_history(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", session=DESKTOP_A, session_title="A")
    version = task.version

    same = await service.bind_session(task.key, DESKTOP_A)
    assert same.version == version  # already bound: nothing recorded

    moved = await service.bind_session(task.key, DESKTOP_B, session_title="B")
    assert moved.session_external_id == DESKTOP_B and moved.version == version + 1
    await service.bind_session(task.key, "reviewer")
    await service.bind_session(task.key, DESKTOP_A)

    bindings = (await service.task_detail(task.key)).bindings
    assert [b.session_name for b in bindings] == ["A", "reviewer", "B", "A"]  # newest first
    assert bindings[0].unbound_at is None
    assert all(b.unbound_at is not None for b in bindings[1:])
    assert bindings[1].unbound_at == bindings[0].bound_at
    assert bindings[0].bound_by == "A" and bindings[-1].bound_by == "A"

    events = [e for e in await service.events_since(0) if e.kind == "task.session_bound"]
    assert [(e.payload["session"], e.payload["previous"]) for e in events] == [
        ("A", None),
        ("B", "A"),
        ("reviewer", "B"),
        ("A", "reviewer"),
    ]
    assert events[1].payload["session_external_id"] == DESKTOP_B


async def test_unbind_keeps_history(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", session=DESKTOP_A, session_title="A")

    with pytest.raises(ConflictError, match="bound to A, not someone-else"):
        await service.unbind_session(task.key, "someone-else")
    assert (await service.get_task(task.key)).session_id == task.session_id

    with service.bus.subscribe() as sub:
        unbound_task, unbound = await service.unbind_session(task.key, DESKTOP_A.upper())
        event = await sub.get(1)
    assert unbound is not None and unbound.name == "A"
    assert unbound_task.session_id is None and unbound_task.session_name is None
    assert unbound_task.version == task.version + 1
    assert event.kind == "task.session_unbound" and event.actor == "mcp"
    assert (event.payload["session"], event.payload["session_external_id"]) == ("A", DESKTOP_A)
    assert event.session_id == task.session_id

    bindings = (await service.task_detail(task.key)).bindings
    assert [b.session_name for b in bindings] == ["A"] and bindings[0].unbound_at is not None

    # Nothing bound: no write, no event; with an expected session it is an error.
    last = await service.last_event_id()
    same, none = await service.unbind_session(task.key)
    assert none is None and same.version == unbound_task.version
    assert await service.last_event_id() == last
    with pytest.raises(ConflictError, match="No session is bound"):
        await service.unbind_session(task.key, "A")

    # Binding again opens a new history row.
    await service.bind_session(task.key, "reviewer")
    unbound_task, unbound = await service.unbind_session(task.key, "reviewer")
    assert unbound is not None and unbound.name == "reviewer"
    history = (await service.task_detail(task.key)).bindings
    assert [b.session_name for b in history] == ["reviewer", "A"]
    assert all(b.unbound_at for b in history)


async def test_bind_requires_a_session(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    with pytest.raises(ValidationError):
        await service.bind_session(task.key, "  ")
    with pytest.raises(ValidationError):
        await service.bind_session(task.key, "local_123")
    assert (await service.task_detail(task.key)).bindings == []
