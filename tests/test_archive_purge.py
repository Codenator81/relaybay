"""Deleting from the archive: tasks archived longer than `archive_purge_days` are purged by
relaybay itself, an archived project is deleted on request; an export is written first."""

import base64
import json
import zipfile
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay import service as service_module
from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call, connect
from tests.test_review import png

FORM = {"Origin": BASE}


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    try:
        yield service
    finally:
        await service.close()


def days_later(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def full_task(svc: Service, title: str = "Receipts") -> str:
    """A done card with a report, a comment, a review step with a screenshot, a plan, a
    decision and a backlog item it is the origin of."""
    task = await svc.create_task("acme-shop", title, stage="dev", session="worker")
    await svc.add_report(task.key, "verified · S · receipts zebra", session="worker")
    await svc.add_owner_comment(task.key, "Looks fine, zebra")
    await svc.set_review_steps(task.key, [{"title": "Open receipts"}], session="worker")
    await svc.attach_file(
        task.key,
        content_base64=base64.b64encode(png()).decode(),
        filename="shot.png",
        step=1,
        session="worker",
    )
    await svc.set_plan(task.key, ["Write it", "Test it"], session="worker")
    await svc.add_decision("acme-shop", "Receipts as PDF", task=task.key, session="worker")
    # The coordinator's: in coordinator mode the bound worker adds no backlog item.
    await svc.add_backlog_item("acme-shop", "Receipts in CSV", task=task.key, session="lead")
    await svc.set_stage(task.key, "done", session="worker")
    return task.key


async def test_a_task_archived_31_days_ago_is_purged_with_an_export(svc: Service) -> None:
    key = await full_task(svc)
    task = await svc.get_task(key)
    folder = svc.attachments_dir / "acme-shop" / key
    assert any(folder.iterdir())
    await svc.add_decision("acme-shop", "Keep receipts 10 years")
    await svc.set_archived(key, True, actor="owner")

    [export] = await svc.purge_archived(now=days_later(31))

    assert export.parent == svc.deleted_dir and export.suffix == ".zip"
    assert export.name.endswith(f"-acme-shop-{key}.zip")
    with zipfile.ZipFile(export) as archive:
        content = json.loads(archive.read("export.json"))
        names = archive.namelist()
    tables = content["tables"]
    assert [t["key"] for t in tables["task"]] == [key]
    for table in ("report", "comment", "review_step", "plan_step", "attachment", "event"):
        assert tables[table], table
    assert [d["text"] for d in tables["decision"]] == ["Receipts as PDF"]
    assert content["files"] and all(f in names for f in content["files"])
    assert content["deleted_by"] == "relaybay" and content["task"] == key

    with pytest.raises(NotFoundError):
        await svc.get_task(key)
    assert not folder.exists()
    # The task's decision goes with it, a decision of the project stays.
    _, decisions = await svc.list_decisions("acme-shop")
    assert [d.text for d in decisions] == ["Keep receipts 10 years"]
    assert await svc.search("zebra", project="acme-shop") == []
    [event] = [e for e in await svc.events_since(0) if e.kind == "task.purged"]
    assert event.payload["key"] == key and event.payload["export"] == f"deleted/{export.name}"
    assert event.task_id is None and event.project_id == task.project_id
    assert next(e.kind for e in await svc.audit(changes=True)) == "task.purged"
    # The card's own events stay, without the link to the deleted row.
    assert all(e.task_id != task.id for e in await svc.events_since(0))


async def test_a_task_decision_is_deleted_with_its_task(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Pay", stage="done")
    await svc.add_decision("acme-shop", "Pay by card", task=task.key)
    assert await repo.task_decisions(svc._r, task.id)  # scope task
    await svc.set_archived(task.key, True, actor="owner")
    [export] = await svc.purge_archived(now=days_later(31))
    assert export.suffix == ".json"
    content = json.loads(export.read_text(encoding="utf-8"))
    assert [d["text"] for d in content["tables"]["decision"]] == ["Pay by card"]
    assert not await repo.task_decisions(svc._r, task.id)


async def test_a_backlog_item_loses_the_link_and_stays(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Pay", stage="done")
    item = await svc.add_backlog_item("acme-shop", "Refunds", task=task.key, session="worker")
    await svc.set_archived(task.key, True, actor="owner")
    await svc.purge_archived(now=days_later(31))
    async with svc._r.execute(
        "SELECT origin_task_id, task_id FROM backlog_item WHERE id = ?", (item.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and tuple(row) == (None, None)


async def test_29_days_in_the_archive_is_kept(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Pay", stage="done")
    await svc.set_archived(task.key, True, actor="owner")
    assert await svc.purge_archived(now=days_later(29)) == []
    assert (await svc.get_task(task.key)).archived_at is not None
    assert not svc.deleted_dir.exists()


async def test_an_old_task_not_archived_by_hand_is_kept(svc: Service) -> None:
    done = await svc.create_task("acme-shop", "Done long ago", stage="done")
    open_task = await svc.create_task("acme-shop", "Open", stage="dev")
    # The done window archives `done` by age; that sets no mark, so nothing is purged.
    assert await svc.purge_archived(now=days_later(400)) == []
    assert (await svc.get_task(done.key)).title == "Done long ago"
    assert (await svc.get_task(open_task.key)).title == "Open"


async def test_a_task_brought_back_from_the_archive_is_kept(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Back", stage="done")
    await svc.set_archived(task.key, True, actor="owner")
    await svc.set_archived(task.key, False, actor="owner")
    live = await svc.create_task("acme-shop", "Live", stage="live")
    await svc.set_archived(live.key, True, actor="owner")
    await svc.set_archived(live.key, False, actor="owner")  # back to live
    assert await svc.purge_archived(now=days_later(31)) == []
    assert (await svc.get_task(task.key)).unarchived_at is not None
    assert (await svc.get_task(live.key)).stage.value == "live"


async def test_archive_purge_days_0_deletes_nothing(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Pay", stage="done")
    await svc.set_archived(task.key, True, actor="owner")
    svc.archive_purge_days = 0
    assert await svc.purge_archived(now=days_later(3650)) == []
    assert (await svc.get_task(task.key)).archived_at is not None


async def test_the_setting_is_read_and_checked(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).archive_purge_days == 30
    env = {"RELAYBAY_ARCHIVE_PURGE_DAYS": "0"}
    assert load_settings(home=tmp_path, environ=env).archive_purge_days == 0
    (tmp_path / "config.toml").write_text("archive_purge_days = -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="archive_purge_days must be a whole number >= 0"):
        load_settings(home=tmp_path, environ={})


async def test_a_failed_export_deletes_nothing(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Pay", stage="done")
    await svc.set_archived(task.key, True, actor="owner")
    svc.deleted_dir.parent.mkdir(parents=True, exist_ok=True)
    svc.deleted_dir.write_text("a file where the folder should be", encoding="utf-8")
    assert await svc.purge_archived(now=days_later(31)) == []  # logged, tried again next pass
    assert (await svc.get_task(task.key)).archived_at is not None
    assert "task.purged" not in [e.kind for e in await svc.events_since(0)]


async def test_one_failing_task_does_not_stop_the_pass(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await svc.create_task("acme-shop", "Locked", stage="done")
    second = await svc.create_task("acme-shop", "Free", stage="done")
    for task in (first, second):
        await svc.set_archived(task.key, True, actor="owner")
    real = service_module.write_export

    def flaky(folder: Path, name: str, *args: object) -> Path:
        if first.key in name:
            raise PermissionError("held by another program")
        return real(folder, name, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(service_module, "write_export", flaky)
    [export] = await svc.purge_archived(now=days_later(31))
    assert second.key in export.name
    assert (await svc.get_task(first.key)).archived_at is not None
    with pytest.raises(NotFoundError):
        await svc.get_task(second.key)
    assert sorted(p.name for p in svc.deleted_dir.iterdir()) == [export.name]


async def test_a_rolled_back_purge_leaves_no_export(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = await full_task(svc)
    await svc.set_archived(key, True, actor="owner")

    async def broken(*args: object) -> None:
        raise RuntimeError("the delete failed")

    monkeypatch.setattr(repo, "delete_task", broken)
    assert await svc.purge_archived(now=days_later(31)) == []
    assert (await svc.get_task(key)).archived_at is not None
    assert list(svc.deleted_dir.iterdir()) == []  # neither the export nor the staged files


async def test_a_task_decision_that_replaced_a_project_decision_stays(svc: Service) -> None:
    old = await svc.add_decision("acme-shop", "Receipts as HTML")
    task = await svc.create_task("acme-shop", "PDF receipts", stage="done")
    new = await svc.add_decision("acme-shop", "Receipts as PDF", task=task.key, supersedes=old.id)
    await svc.set_archived(task.key, True, actor="owner")
    [export] = await svc.purge_archived(now=days_later(31))
    content = json.loads(export.read_text(encoding="utf-8"))
    assert "decision" not in content["tables"]
    _, current = await svc.list_decisions("acme-shop")
    assert [d.id for d in current] == [new.id]
    _, every = await svc.list_decisions("acme-shop", include_superseded=True)
    by_id = {d.id: d for d in every}
    assert by_id[old.id].superseded_by == new.id
    async with svc._r.execute("SELECT task_id, scope FROM decision WHERE id = ?", (new.id,)) as cur:
        row = await cur.fetchone()
    assert row is not None and tuple(row) == (None, "project")


# --- deleting a project ----------------------------------------------------------------------


async def archived_project(svc: Service) -> str:
    key = await full_task(svc)
    await svc.add_area("acme-shop", "web shop")
    await svc.add_note(
        "project", "project", "Payments", "Stripe zebra test mode", project="acme-shop"
    )
    await svc.update_document("acme-shop", "spec", "# Spec\n\nzebra rules\n", "first", 0)
    await svc.upsert_project("acme-shop", archived=True, session="lead")
    return key


async def test_project_delete_is_refused_for_an_ordinary_session(svc: Service) -> None:
    await archived_project(svc)
    for session in (None, "worker"):
        with pytest.raises(ValidationError, match="project_delete is only for the coordinator"):
            await svc.delete_project_mcp("acme-shop", "acme-shop", session=session)
    assert (await svc.get_project("acme-shop")).archived_at is not None


async def test_project_delete_is_refused_for_an_active_project(svc: Service) -> None:
    await svc.create_task("demo-api", "Open", stage="dev")
    with pytest.raises(ValidationError, match="demo-api is not in the archive"):
        await svc.delete_project_mcp("demo-api", "demo-api", session="lead")
    with pytest.raises(ValidationError, match="demo-api is not in the archive"):
        await svc.delete_project("demo-api", "demo-api")
    assert (await svc.get_project("demo-api")).slug == "demo-api"
    assert not svc.deleted_dir.exists()


async def test_project_delete_is_refused_with_a_wrong_confirm(svc: Service) -> None:
    await archived_project(svc)
    for confirm in (None, "", "demo-api", "acme"):
        with pytest.raises(ValidationError, match="confirm with the project's slug, acme-shop"):
            await svc.delete_project_mcp("acme-shop", confirm, session="lead")
    assert (await svc.get_project("acme-shop")).slug == "acme-shop"


async def test_a_coordinator_deletes_an_archived_project(svc: Service) -> None:
    key = await archived_project(svc)
    other = await svc.create_task("demo-api", "Zebra endpoint", stage="dev")
    await svc.add_report(other.key, "changed · S · zebra api")
    before = await svc.portfolio_digest()
    assert "demo-api" in before

    project, export = await svc.delete_project_mcp("acme-shop", "acme-shop", session="lead")

    assert project.slug == "acme-shop" and export.suffix == ".zip"
    assert export.name.endswith("-acme-shop.zip")
    with zipfile.ZipFile(export) as archive:
        content = json.loads(archive.read("export.json"))
    tables = content["tables"]
    assert [p["slug"] for p in tables["project"]] == ["acme-shop"]
    assert [t["key"] for t in tables["task"]] == [key]
    for table in (
        "report",
        "comment",
        "attachment",
        "decision",
        "backlog_item",
        "memory",
        "document",
        "project_area",
        "event",
    ):
        assert tables[table], table
    assert content["deleted_by"] == "lead"

    with pytest.raises(NotFoundError):
        await svc.get_project("acme-shop")
    with pytest.raises(NotFoundError):
        await svc.get_task(key)
    assert [s.project.slug for s in await svc.list_projects()] == ["demo-api"]
    assert await svc.archived_projects() == []
    assert "acme-shop" not in await svc.portfolio_digest()
    hits = await svc.search("zebra")
    assert {h.project_slug for h in hits} == {"demo-api"}
    assert not (svc.attachments_dir / "acme-shop").exists()
    assert (await svc.get_task(other.key)).title == "Zebra endpoint"  # others untouched
    assert "demo-api" in await svc.project_digest("demo-api")
    [event] = [e for e in await svc.events_since(0) if e.kind == "project.deleted"]
    assert event.project_id is None and event.actor == "lead"
    assert event.payload["export"] == f"deleted/{export.name}" and event.payload["tasks"] == 1
    assert all(
        e.payload.get("project") != "acme-shop" or e.id == event.id
        for e in await svc.events_since(0)
    )


async def test_a_deleted_project_leaves_nothing_a_new_one_inherits(svc: Service) -> None:
    await svc.create_task("acme-shop", "Seen", stage="dev", session="worker")
    project = await svc.get_project("acme-shop")
    async with svc._r.execute("SELECT id FROM session WHERE name = 'worker'") as cur:
        row = await cur.fetchone()
    assert row is not None
    async with svc._tx() as tx:
        await tx.conn.execute(
            "INSERT OR REPLACE INTO rule_seen (session_id, project_id, version) VALUES (?, ?, 7)",
            (row[0], project.id),
        )
        await tx.conn.execute(
            "INSERT INTO import_item (kind, item, source_path, target_id, content_hash,"
            " target_hash, imported_at) VALUES ('task', 'acme-shop/1', 'TASKS.md', ?, 'a', 'b',"
            " '2026-01-01')",
            ((await svc.list_tasks(project="acme-shop"))[0].id,),
        )
    await svc.upsert_project("acme-shop", archived=True, session="lead")
    export = await svc.delete_project("acme-shop", "acme-shop")
    content = json.loads(export.read_text(encoding="utf-8"))
    assert [r["version"] for r in content["tables"]["rule_seen"]] == [7]
    assert [r["item"] for r in content["tables"]["import_item"]] == ["acme-shop/1"]
    for table in ("rule_seen", "import_item"):
        async with svc._r.execute(f"SELECT count(*) FROM {table}") as cur:
            count = await cur.fetchone()
        assert count is not None and count[0] == 0, table


async def test_deleting_a_project_leaves_file_documents_on_disk(
    svc: Service, tmp_path_factory: pytest.TempPathFactory
) -> None:
    repo_dir = tmp_path_factory.mktemp("acme")  # outside the data folder
    spec = repo_dir / "SPEC.md"
    spec.write_text("# Spec\n\nKept on disk.\n", encoding="utf-8")
    await svc.upsert_project("acme-shop", repo_path=str(repo_dir))
    await svc.update_document(
        "acme-shop", "spec", "-", "file", 0, source_path=str(spec), session="worker"
    )
    await svc.upsert_project("acme-shop", archived=True, session="lead")
    await svc.delete_project("acme-shop", "acme-shop")
    assert spec.read_text(encoding="utf-8") == "# Spec\n\nKept on disk.\n"


async def test_mcp_project_delete(tmp_path: Path, live_server: Callable[[], LiveServer]) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["project_delete"].annotations.destructive_hint is True
        await call(client, "project_upsert", slug="demo-api", session="lead")
        args = {"slug": "demo-api", "confirm": "demo-api"}
        refused = await client.call_tool("project_delete", {**args, "session": "lead"})
        assert refused.is_error and "is not in the archive" in refused.content[0].text
        await call(client, "project_upsert", slug="demo-api", archived=True, session="lead")
        refused = await client.call_tool("project_delete", {**args, "session": "worker"})
        assert refused.is_error and "only for the coordinator" in refused.content[0].text
        text = await call(client, "project_delete", **args, session="lead")
        assert "Deleted the project **demo-api**" in text and "deleted/" in text
        assert "demo-api" not in await call(client, "project_list")
    assert len(list((tmp_path / "deleted").glob("*-demo-api.json"))) == 1


def test_board_delete_button_only_on_an_archived_project(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.upsert_project, "demo-api", name="Demo API")
    action = 'action="/p/demo-api/project/delete"'
    assert action not in board.get("/p/demo-api/settings").text
    data = {"csrf_token": csrf_token(token), "confirm": "demo-api"}
    refused = board.post("/p/demo-api/project/delete", data=data, headers=FORM)
    assert refused.status_code == 400 and "is not in the archive" in refused.text

    run_in_app(board, service.set_project_archived, "demo-api", True)
    page = board.get("/p/demo-api/settings").text
    assert action in page and 'name="confirm"' in page

    wrong = board.post("/p/demo-api/project/delete", data={**data, "confirm": "demo"}, headers=FORM)
    assert wrong.status_code == 400 and "confirm with the project&#39;s slug" in wrong.text
    no_csrf = board.post(
        "/p/demo-api/project/delete", data={**data, "csrf_token": "wrong"}, headers=FORM
    )
    assert no_csrf.status_code == 403
    assert run_in_app(board, service.get_project, "demo-api").slug == "demo-api"

    done = board.post("/p/demo-api/project/delete", data=data, headers=FORM, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/?archived=1"
    assert "Deleted the project demo-api" in board.get("/?archived=1").text
    with pytest.raises(NotFoundError):
        run_in_app(board, service.get_project, "demo-api")
    assert 'href="/p/demo-api"' not in board.get("/fragments/projects").text
    assert run_in_app(board, service.get_project, "acme-shop").slug == "acme-shop"
    again = board.post(
        "/p/demo-api/project/delete", data=data, headers=FORM, follow_redirects=False
    )
    assert again.status_code == 303 and again.headers["location"] == "/?archived=1"
    assert "There is no project demo-api" in board.get("/?archived=1").text


def test_board_delete_shows_why_the_export_failed(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "demo-api", name="Demo API")
    run_in_app(board, service.set_project_archived, "demo-api", True)
    service.deleted_dir.write_text("a file where the folder should be", encoding="utf-8")
    data = {"csrf_token": csrf_token(token), "confirm": "demo-api"}
    failed = board.post("/p/demo-api/project/delete", data=data, headers=FORM)
    assert failed.status_code == 400 and "Nothing was deleted" in failed.text
    assert run_in_app(board, service.get_project, "demo-api").slug == "demo-api"
