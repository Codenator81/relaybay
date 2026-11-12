"""Archiving a done card by hand: the mark, the done window it overrides, the board, MCP for
coordinators only, and the events on Activity and the Audit page."""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.domain.errors import ValidationError
from relaybay.domain.tasks import is_archived
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.web import routes
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call, connect

FORM = {"Origin": BASE}
HTMX = {"HX-Request": "true", "Origin": BASE}
NOT_DONE = "Only a done task goes to the archive"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop")
    try:
        yield service
    finally:
        await service.close()


async def kinds(service: Service) -> list[str]:
    return [e.kind for e in await service.events_since(0)]


async def test_the_hand_mark_archives_at_once_and_a_stage_change_clears_it(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Receipts", stage="done")
    now = datetime.now(UTC)
    assert not is_archived(task, now, 7)
    archived = await svc.set_archived(task.key, True, actor="owner")
    assert archived.archived_at is not None and archived.archived_by == "owner"
    assert archived.version == task.version  # no conflict for an open edit form
    assert is_archived(archived, now, 7)
    again = await svc.set_archived(task.key, True, actor="someone")
    assert (again.archived_at, again.archived_by) == (archived.archived_at, "owner")
    assert (await kinds(svc)).count("task.archived") == 1  # a repeat records nothing

    moved = await svc.set_stage(task.key, "dev")
    assert (moved.archived_at, moved.archived_by, moved.unarchived_at) == (None, None, None)
    back = await svc.set_stage(task.key, "done")
    assert not is_archived(back, datetime.now(UTC), 7)


async def test_only_a_done_task_is_archived(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Checkout", stage="dev")
    for archived in (True, False):
        with pytest.raises(ValidationError, match=NOT_DONE):
            await svc.set_archived(task.key, archived, actor="owner")
    assert (await svc.get_task(task.key)).archived_at is None
    assert "task.archived" not in await kinds(svc)


async def test_the_hand_mark_ignores_require_tracker_log(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Unlogged", stage="done")
    later = datetime.now(UTC) + timedelta(days=30)
    assert not is_archived(task, later, 7, require_tracker_log=True)  # waits for the work log
    archived = await svc.set_archived(task.key, True, actor="owner")
    assert not archived.tracker_logged
    assert is_archived(archived, datetime.now(UTC), 7, require_tracker_log=True)


async def test_back_from_the_archive_starts_the_window_again(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Old", stage="done")
    await svc.set_archived(task.key, False, actor="owner")
    assert "task.unarchived" not in await kinds(svc)  # not archived: nothing to bring back
    # Brought back from the done window's archive (a window of 0 days archives it at once).
    svc.done_window_days = 0
    back = await svc.set_archived(task.key, False, actor="owner")
    svc.done_window_days = 7
    assert back.unarchived_at is not None and back.archived_at is None
    assert not is_archived(back, datetime.now(UTC) + timedelta(days=6), 7)
    assert is_archived(back, datetime.now(UTC) + timedelta(days=8), 7)
    [event] = [e for e in await svc.events_since(0) if e.kind == "task.unarchived"]
    assert event.actor == "owner" and event.payload["archived_by"] is None

    await svc.set_archived(task.key, True, actor="owner")
    again = await svc.set_archived(task.key, False, actor="owner")
    assert again.archived_at is None
    assert not is_archived(again, datetime.now(UTC), 7)
    last = [e for e in await svc.events_since(0) if e.kind == "task.unarchived"][-1]
    assert last.payload["archived_by"] == "owner"


async def test_mcp_archive_is_for_coordinators_only(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Receipts", stage="done")
    with pytest.raises(ValidationError, match="task_archive is only for the coordinator"):
        await svc.set_archived_mcp(task.key, True, session="worker")
    with pytest.raises(ValidationError, match="task_unarchive is only for the coordinator"):
        await svc.set_archived_mcp(task.key, False, session="worker")
    assert (await svc.get_task(task.key)).archived_at is None

    archived = await svc.set_archived_mcp(task.key, True, session="lead")
    assert archived.archived_by == "lead"
    back = await svc.set_archived_mcp(task.key, False, session="lead")
    assert back.archived_at is None and back.unarchived_at is not None

    open_task = await svc.create_task("acme-shop", "Open", stage="review")
    with pytest.raises(ValidationError, match="is in review"):
        await svc.set_archived_mcp(open_task.key, True, session="lead")


async def test_archive_events_are_on_the_audit_page_with_changes(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Receipts", stage="done")
    await svc.set_archived_mcp(task.key, True, session="lead")
    await svc.set_archived(task.key, False, actor="owner")
    audited = await svc.audit(changes=True)
    assert [(e.kind, e.actor) for e in audited] == [
        ("task.unarchived", "owner"),
        ("task.archived", "lead"),
    ]
    assert audited[1].payload["key"] == task.key and audited[1].session_id is not None
    assert await svc.audit() == []  # without changes: live actions only


async def test_mcp_tools(tmp_path: Path, live_server: Callable[[], LiveServer]) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", session="lead")
        await call(client, "task_create", project="acme-shop", title="Pay", session="lead")
        refused = await client.call_tool("task_archive", {"task": "acme-shop-1", "session": "x"})
        text = refused.content[0].text
        assert refused.is_error and "only for the coordinator" in text and "Nothing changed" in text
        not_done = await client.call_tool(
            "task_archive", {"task": "acme-shop-1", "session": "lead"}
        )
        assert not_done.is_error and NOT_DONE in not_done.content[0].text
        await call(client, "task_set_stage", task="acme-shop-1", stage="done", session="lead")
        text = await call(client, "task_archive", task="acme-shop-1", session="lead")
        assert "is in the archive since" in text and "(by lead)" in text
        assert "- archived: " in await call(client, "task_get", task="acme-shop-1")
        text = await call(client, "task_unarchive", task="acme-shop-1", session="lead")
        assert "is in the done column, not in the archive" in text
        refused = await client.call_tool("task_unarchive", {"task": "acme-shop-1"})
        assert refused.is_error and "task_unarchive is only for the coordinator" in (
            refused.content[0].text
        )


def archive(client: TestClient, token: str, key: str, action: str = "archive", **extra: str):
    data = {"csrf_token": csrf_token(token), **extra}
    return client.post(
        f"/p/acme-shop/t/{key}/{action}", data=data, headers=FORM, follow_redirects=False
    )


def done_column(board: TestClient) -> str:
    return board.get("/p/acme-shop/col/done").text


def test_board_archive_and_back(board: TestClient, seeded: str, token: str) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, service.create_task, "acme-shop", "Receipts", stage="done")
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert '<button type="button" data-archive="archive">Archive</button>' in card
    assert "data-archive" not in board.get(f"/p/acme-shop/t/{seeded}/card").text  # a dev card
    assert f'id="card-{task.key}"' in done_column(board)

    response = archive(board, token, task.key)
    assert response.status_code == 200 and response.json() == {"archived": True}
    column = done_column(board)
    assert f'id="card-{task.key}"' not in column
    assert "1 in the archive (closed more than 7 days ago or archived by hand)" in column
    shown = board.get("/p/acme-shop/col/done?archived=1").text
    assert f'id="card-{task.key}"' in shown
    assert 'data-archive="unarchive">Back from the archive</button>' in shown
    found = board.get("/p/acme-shop?q=receipts").text
    assert f'id="card-{task.key}"' in found  # search covers the archive

    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "archived <time" in view and "by owner" in view
    assert f'hx-post="/p/acme-shop/t/{task.key}/unarchive"' in view
    back = archive(board, token, task.key, "unarchive", target="view")
    assert back.status_code == 303 and back.headers["location"] == f"/t/{task.key}"
    assert f'id="card-{task.key}"' in done_column(board)

    fragment = board.post(
        f"/p/acme-shop/t/{task.key}/archive",
        data={"csrf_token": csrf_token(token), "target": "view"},
        headers=HTMX,
    )
    assert fragment.status_code == 200 and 'class="task-view"' in fragment.text
    assert "Back from the archive</button>" in fragment.text

    activity = board.get("/activity").text
    assert "task.archived" in activity and "task.unarchived" in activity
    audit = board.get("/audit?changes=1").text
    assert "task.archived" in audit and "archived by owner" in audit


def test_board_archive_refuses_a_task_not_in_done(
    board: TestClient, seeded: str, token: str
) -> None:
    response = archive(board, token, seeded)
    assert response.status_code == 400
    assert response.text == f"{NOT_DONE}; {seeded} is in dev. Nothing changed."
    view = board.post(
        f"/p/acme-shop/t/{seeded}/archive",
        data={"csrf_token": csrf_token(token), "target": "view"},
        headers=HTMX,
    )
    assert view.status_code == 200 and NOT_DONE in view.text


def test_board_archive_is_for_the_signed_in_board_only(
    client: TestClient, board: TestClient, token: str
) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Receipts", stage="done")
    assert archive(board, "wrong", task.key).status_code == 403  # a wrong CSRF token
    bearer = {"Authorization": f"Bearer {token}", "Origin": BASE}
    for action in ("archive", "unarchive"):
        response = client.post(
            f"/p/acme-shop/t/{task.key}/{action}",
            data={"csrf_token": csrf_token(token)},
            headers=bearer,
        )
        assert response.status_code == 403
    assert run_in_app(board, service.get_task, task.key).archived_at is None


def test_the_window_counts_from_back_from_the_archive(
    board: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Old", stage="done")
    later = datetime.now(UTC) + timedelta(days=8)
    monkeypatch.setattr(routes, "utcnow", lambda: later)
    assert f'id="card-{task.key}"' not in done_column(board)  # archived by the done window
    assert 'data-archive="unarchive"' in board.get(f"/p/acme-shop/t/{task.key}/card").text
    service.done_window_days = 0  # the service sees the age the board sees
    assert archive(board, token, task.key, "unarchive").json() == {"archived": False}
    service.done_window_days = 7
    saved = run_in_app(board, service.get_task, task.key)
    assert saved.unarchived_at is not None
    shown_until = datetime.fromisoformat(saved.unarchived_at) + timedelta(days=6)
    monkeypatch.setattr(routes, "utcnow", lambda: shown_until)
    assert f'id="card-{task.key}"' in done_column(board)
