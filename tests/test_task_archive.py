"""Archiving a done card by hand: the mark, the done window it overrides, the board, MCP for
coordinators only, and the events on Activity and the Audit page."""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.domain.tasks import is_archived
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.web import routes
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call, connect

FORM = {"Origin": BASE}
HTMX = {"HX-Request": "true", "Origin": BASE}
NOT_DONE = "Only a done or live task goes to the archive"


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
    await asyncio.sleep(0.01)  # the window counts from the close: let the clock pass it
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


async def test_an_unlogged_task_archived_by_hand_stays_in_the_inbox_not_logged(
    svc: Service,
) -> None:
    task = await svc.create_task("acme-shop", "Unlogged", stage="done", tracker_ref="T-1")
    await svc.set_archived(task.key, True, actor="owner")
    inbox = await svc.inbox([], require_tracker_log=True)
    assert [t.key for t in inbox.not_logged] == [task.key]
    assert (await svc.inbox([])).not_logged == []


# --- a live card, in bulk, and projects (design 0026) ----------------------------------------


async def test_a_live_card_is_closed_and_archived_and_comes_back_to_live(tmp_path: Path) -> None:
    svc = Service(tmp_path / "strict.sqlite", rules_mode="strict")
    await svc.start()
    try:
        await svc.upsert_project("acme-shop")
        task = await svc.create_task("acme-shop", "Looked at by eye", stage="live")
        with pytest.raises(ValidationError):
            await svc.set_stage(task.key, "done")  # strict: done needs its fields
        archived = await svc.set_archived(task.key, True, actor="owner")
        # The owner's own act: closed and archived in one step, no rule asked.
        assert archived.stage.value == "done" and archived.closed_at is not None
        assert (archived.archived_by, archived.archived_from) == ("owner", "live")
        assert is_archived(archived, datetime.now(UTC), 7)
        events = [e for e in await svc.events_since(0) if e.task_id == task.id]
        moved, marked = events[-2:]
        assert (moved.kind, moved.payload["from"], moved.payload["to"]) == (
            "task.stage_changed",
            "live",
            "done",
        )
        assert "warnings" not in moved.payload
        assert (marked.kind, marked.payload["from"]) == ("task.archived", "live")

        back = await svc.set_archived(task.key, False, actor="owner")
        assert back.stage.value == "live" and back.closed_at is None
        assert (back.archived_at, back.archived_by, back.archived_from) == (None, None, None)
        last = [e for e in await svc.events_since(0) if e.kind == "task.unarchived"][-1]
        assert (last.payload["to"], last.payload["archived_by"]) == ("live", "owner")
        # Back again on a live card is a repeat: nothing changes, nothing is recorded.
        before = await svc.last_event_id()
        assert (await svc.set_archived(task.key, False, actor="owner")).stage.value == "live"
        assert await svc.last_event_id() == before
    finally:
        await svc.close()


async def test_a_live_card_moved_on_by_hand_forgets_where_it_came_from(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Shipped", stage="live")
    await svc.set_archived(task.key, True, actor="owner")
    moved = await svc.set_stage(task.key, "test")  # any stage change ends the archive stay
    assert (moved.archived_at, moved.archived_from) == (None, None)
    await svc.set_stage(task.key, "done")
    await svc.set_archived(task.key, True, actor="owner")
    back = await svc.set_archived(task.key, False, actor="owner")
    assert back.stage.value == "done"  # archived from done this time


async def test_mcp_archive_takes_a_live_card(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Live", stage="live")
    archived = await svc.set_archived_mcp(task.key, True, session="lead")
    assert (archived.stage.value, archived.archived_from, archived.archived_by) == (
        "done",
        "live",
        "lead",
    )
    [moved] = [e for e in await svc.events_since(0) if e.kind == "task.stage_changed"]
    assert moved.actor == "lead" and moved.session_id is not None
    back = await svc.set_archived_mcp(task.key, False, session="lead")
    assert back.stage.value == "live"


async def test_archive_all_shown_and_undo(svc: Service) -> None:
    done = [await svc.create_task("acme-shop", f"Done {n}", stage="done") for n in range(3)]
    already = await svc.create_task("acme-shop", "Already", stage="done")
    await svc.set_archived(already.key, True, actor="owner")
    moved = await svc.create_task("acme-shop", "Moved meanwhile", stage="dev")
    live = await svc.create_task("acme-shop", "Live", stage="live")  # the done column only
    keys = [t.key for t in (*done, already, moved, live)]
    batch, archived = await svc.archive_shown(
        "acme-shop", [*keys, keys[0], "acme-shop-99"], actor="owner"
    )
    assert archived == [t.key for t in done]  # a deleted key is skipped, not refused
    marked = [e for e in await svc.events_since(0) if e.kind == "task.archived"]
    assert {e.payload.get("batch") for e in marked[-3:]} == {batch}

    await svc.set_archived(done[1].key, False, actor="owner")  # one came back by hand
    back = await svc.undo_archive("acme-shop", batch, actor="owner")
    assert back == [done[0].key, done[2].key]
    tasks = await svc.list_tasks(include_done=True)
    states = {t.key: (t.stage.value, t.archived_at) for t in tasks}
    assert states[done[0].key] == ("done", None) and states[live.key] == ("live", None)
    assert states[already.key][1] is not None  # not of that batch: stays archived
    assert await svc.undo_archive("acme-shop", batch, actor="owner") == []  # a repeat
    assert await svc.undo_archive("acme-shop", "nope", actor="owner") == []


async def test_archive_all_shown_refuses_a_card_of_another_project(svc: Service) -> None:
    await svc.upsert_project("demo-api")
    other = await svc.create_task("demo-api", "Theirs", stage="done")
    mine = await svc.create_task("acme-shop", "Mine", stage="done")
    with pytest.raises(NotFoundError):
        await svc.archive_shown("acme-shop", [mine.key, other.key], actor="owner")
    assert (await svc.get_task(mine.key)).archived_at is None  # one transaction
    with pytest.raises(ValidationError, match="No cards"):
        await svc.archive_shown("acme-shop", [], actor="owner")


def test_board_archive_all_shown_and_undo(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    live = run_in_app(board, service.create_task, "acme-shop", "Live", stage="live")
    done = run_in_app(board, service.create_task, "acme-shop", "Done", stage="done")
    card = board.get(f"/p/acme-shop/t/{live.key}/card").text
    assert 'data-archive="archive" data-confirm="Archive' in card
    assert "open question or approval is not archived" in card
    assert "Archive all shown" not in board.get("/p/acme-shop/col/live").text
    assert "Archive all shown (1)" in done_column(board)
    view = board.get(f"/p/acme-shop/t/{live.key}/view").text
    assert f'hx-post="/p/acme-shop/t/{live.key}/archive"' in view and "hx-confirm=" in view

    data = {"csrf_token": csrf_token(token), "keys": f"{live.key} {done.key}"}
    answer = board.post("/p/acme-shop/archive", data=data, headers=FORM).json()
    assert answer["keys"] == [done.key] and answer["message"] == "Archived 1 card."
    assert "Archive all shown" not in done_column(board)
    data = {"csrf_token": csrf_token(token), "batch": answer["batch"]}
    undo = board.post("/p/acme-shop/archive/undo", data=data, headers=FORM).json()
    assert undo == {"keys": [done.key], "message": "Brought back 1 card."}
    assert run_in_app(board, service.get_task, live.key).stage.value == "live"
    wrong = {"csrf_token": "wrong", "keys": live.key}
    assert board.post("/p/acme-shop/archive", data=wrong, headers=FORM).status_code == 403


async def test_an_archived_project_leaves_the_lists_and_comes_back(svc: Service) -> None:
    await svc.upsert_project("demo-api")
    waiting = await svc.create_task("demo-api", "Waits", stage="dev", waiting_on="owner")
    assert [t.key for t in (await svc.inbox(["owner"])).waiting] == [waiting.key]

    project, created = await svc.upsert_project("demo-api", archived=True, session="lead")
    assert not created and project.archived_at is not None
    assert [s.project.slug for s in await svc.list_projects()] == ["acme-shop"]
    assert [p.slug for p in await svc.archived_projects()] == ["demo-api"]
    assert (await svc.inbox(["owner"])).waiting == []
    assert "demo-api" not in await svc.portfolio_digest()
    assert (await svc.get_task(waiting.key)).title == "Waits"  # its cards are kept
    [event] = [e for e in await svc.events_since(0) if e.kind == "project.updated"]
    assert event.payload["changes"] == {"archived": True}
    before = await svc.last_event_id()
    await svc.upsert_project("demo-api", archived=True, session="lead")  # a repeat: nothing
    assert await svc.last_event_id() == before

    back = await svc.set_project_archived("demo-api", False, actor="owner")
    assert back.archived_at is None
    assert [t.key for t in (await svc.inbox(["owner"])).waiting] == [waiting.key]
    with pytest.raises(ValidationError, match="archived must be"):
        await svc.upsert_project("demo-api", archived="yes")  # type: ignore[arg-type]


async def test_only_a_coordinator_archives_a_project_over_mcp(svc: Service) -> None:
    await svc.upsert_project("demo-api")
    for session in (None, "random-dev"):
        with pytest.raises(ValidationError, match=r"project_upsert\(archived\) is only for"):
            await svc.upsert_project("demo-api", archived=True, session=session)
    assert (await svc.get_project("demo-api")).archived_at is None
    assert "project.updated" not in await kinds(svc)
    await svc.upsert_project("demo-api", name="Demo", session="random-dev")  # the rest is open


async def test_back_from_the_archive_keeps_the_work_log_mark(svc: Service) -> None:
    single = await svc.create_task(
        "acme-shop", "Logged", stage="live", tracker_ref="ACME-1", tracker_logged=True
    )
    await svc.set_archived(single.key, True, actor="owner")
    back = await svc.set_archived(single.key, False, actor="owner")
    assert (back.stage.value, back.tracker_logged) == ("live", True)
    assert "task.tracker_unlogged" not in await kinds(svc)
    batch_card = await svc.create_task(
        "acme-shop", "Done", stage="done", tracker_ref="ACME-2", tracker_logged=True
    )
    batch, _ = await svc.archive_shown("acme-shop", [batch_card.key], actor="owner")
    await svc.undo_archive("acme-shop", batch, actor="owner")
    assert (await svc.get_task(batch_card.key)).tracker_logged


async def test_a_live_card_with_an_open_question_or_approval_is_not_archived(
    svc: Service,
) -> None:
    asked = await svc.create_task("acme-shop", "Asks", stage="live", session="worker")
    question, _ = await svc.ask_question(asked.key, "Deploy?", "no", session="worker")
    with pytest.raises(ValidationError, match="open question, an open approval request"):
        await svc.set_archived(asked.key, True, actor="owner")
    assert (await svc.get_task(asked.key)).stage.value == "live"
    await svc.withdraw_question(question.id, session="worker")
    assert (await svc.set_archived(asked.key, True, actor="owner")).archived_from == "live"

    approved = await svc.create_task("acme-shop", "Approved", stage="live", session="worker")
    approval, _, _ = await svc.request_approval(
        approved.key, "push", "feat/live", "Two commits", session="worker"
    )
    with pytest.raises(ValidationError, match="answer, withdraw or use it first"):
        await svc.set_archived(approved.key, True, actor="owner")
    await svc.decide_approval(approval.id, True)
    with pytest.raises(ValidationError, match="approved, unused approval"):
        await svc.set_archived(approved.key, True, actor="owner")
    kinds_now = await kinds(svc)
    assert "approval.expired" not in kinds_now
    assert kinds_now.count("question.withdrawn") == 1  # only the session's own withdrawal


def test_board_archive_project_and_back(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.upsert_project, "demo-api", name="Demo API")
    settings = board.get("/p/demo-api/settings").text
    assert 'action="/p/demo-api/project/archive"' in settings and "data-confirm=" in settings
    data = {"csrf_token": csrf_token(token)}
    response = board.post(
        "/p/demo-api/project/archive", data=data, headers=FORM, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == "/?archived=1"
    home = board.get("/?archived=1").text
    assert "Demo API is in the archive" in home and 'id="archived-projects"' in home
    assert 'href="/?archived=1#archived-projects">Archived projects (1)</a>' in home
    assert 'href="/p/demo-api"' not in board.get("/fragments/projects").text
    page = board.get("/p/demo-api").text
    assert "This project is in the archive" in page
    assert 'href="/p/demo-api/settings#archive">Back from the archive' in page
    assert 'action="/p/demo-api/project/unarchive"' in board.get("/p/demo-api/settings").text
    response = board.post(
        "/p/demo-api/project/unarchive", data=data, headers=FORM, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/p/demo-api/settings#archive"
    assert 'href="/p/demo-api"' in board.get("/fragments/projects").text
    assert board.post("/p/demo-api/project/delete", data=data, headers=FORM).status_code == 404


async def test_a_hook_id_passed_as_session_is_the_same_caller_for_the_coordinator_check(
    svc: Service,
) -> None:
    cc_id = "8b0c1f6e-3a52-4c1e-9d44-0d6f2a7c9e11"
    task = await svc.create_task("acme-shop", "Done", stage="done", session="lead")
    await svc.create_task("acme-shop", "Link", session="lead", hook_session=cc_id)
    archived = await svc.set_archived_mcp(task.key, True, session=cc_id)
    assert archived.archived_by == "lead"
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await svc.set_archived_mcp(task.key, False, session="2f9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7")
