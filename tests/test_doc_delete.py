"""Deleting a project document (doc_update with delete, the document page's Delete), finding a
card the archive purge deleted, and a URL parameter without a value that is no secret."""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.domain.environments import find_secrets
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call, connect

FORM = {"Origin": BASE}
SPEC = "# Spec\n\n## Payouts\nWeekly.\n\n## Refunds\nIn 14 days.\n"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop")
    try:
        yield service
    finally:
        await service.close()


async def add_doc(svc: Service, doc: str = "notes", kind: str | None = None) -> None:
    await svc.update_document("acme-shop", doc, SPEC, "first", 0, kind=kind, session="worker")


def export_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def test_a_document_is_deleted_at_its_current_version_with_an_export(svc: Service) -> None:
    await add_doc(svc)
    await svc.update_document("acme-shop", "notes", SPEC + "\nMore.\n", "second", 1)
    task = await svc.create_task("acme-shop", "Payouts", stage="dev", session="worker")
    await svc.link_task_section(task.key, "notes", "payouts", session="worker")

    with pytest.raises(ConflictError, match="at version 2, not 1"):
        await svc.delete_document("acme-shop", "notes", "stale", 1, session="worker")
    with pytest.raises(ValidationError, match="reason"):
        await svc.delete_document("acme-shop", "notes", " ", 2, session="worker")
    assert (await svc.document_meta("acme-shop", "notes")).version == 2

    gone, path = await svc.delete_document("acme-shop", "notes", "superseded", 2, session="worker")

    assert gone.slug == "notes" and path.parent == svc.deleted_dir
    assert path.name.endswith("-acme-shop-doc-notes.json")
    content = export_of(path)
    assert content["document"] == "notes" and content["reason"] == "superseded"
    assert content["deleted_by"] == "worker"
    tables = content["tables"]
    assert [d["slug"] for d in tables["document"]] == ["notes"]
    assert [v["version"] for v in tables["document_version"]] == [1, 2]
    assert [link["anchor"] for link in tables["task_doc_section"]] == ["payouts"]
    assert tables["document_section"]
    with pytest.raises(NotFoundError):
        await svc.document_meta("acme-shop", "notes")
    assert await repo.cascade_rows(svc._r, "document", [gone.id]) == {}
    _, hits = await svc.search_documents("acme-shop", "Weekly")
    assert hits == []
    [event] = [e for e in await svc.events_since(0, limit=500) if e.kind == "document.deleted"]
    assert event.payload["reason"] == "superseded"
    assert event.payload["export"] == f"deleted/{path.name}"
    # The name is free again.
    await add_doc(svc)


@pytest.mark.parametrize("kind", ["spec", "status"])
async def test_a_spec_or_status_document_is_deleted_by_a_coordinator_only(
    svc: Service, kind: str
) -> None:
    await add_doc(svc, "main", kind)
    with pytest.raises(ValidationError, match="only the owner on the board or a coordinator"):
        await svc.delete_document("acme-shop", "main", "gone", 1, session="worker")
    with pytest.raises(ValidationError, match="only the owner"):
        await svc.delete_document("acme-shop", "main", "gone", 1)
    await svc.document_meta("acme-shop", "main")
    await svc.delete_document("acme-shop", "main", "gone", 1, session="lead")
    with pytest.raises(NotFoundError):
        await svc.document_meta("acme-shop", "main")


async def test_a_change_document_is_deleted_by_the_session_bound_to_its_task(
    svc: Service,
) -> None:
    task = await svc.create_task("acme-shop", "Refunds", stage="dev")
    other = await svc.create_task("acme-shop", "Payouts", stage="dev")
    await svc.bind_session(task.key, "worker")
    await svc.bind_session(other.key, "helper")
    await add_doc(svc, f"spec-{task.key}", "spec")
    await add_doc(svc, f"spec-{other.key}", "spec")

    with pytest.raises(ValidationError, match="bound to that task"):
        await svc.delete_document("acme-shop", f"spec-{task.key}", "deployed", 1, session="helper")
    with pytest.raises(ValidationError):
        await svc.delete_document("acme-shop", f"spec-{other.key}", "deployed", 1, session="worker")
    await svc.delete_document("acme-shop", f"spec-{task.key}", "deployed", 1, session="worker")
    with pytest.raises(NotFoundError):
        await svc.document_meta("acme-shop", f"spec-{task.key}")
    await svc.document_meta("acme-shop", f"spec-{other.key}")


async def test_a_file_document_loses_its_rows_and_keeps_its_file(
    svc: Service, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("outside") / "guide.md"
    path.write_bytes(b"# Guide\r\n\r\n## Payouts\r\nWeekly.\r\n")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    saved = await svc.update_document(
        "acme-shop", "guide", "-", "the guide", 0, source_path=str(path), session="worker"
    )
    gone, export = await svc.delete_document(
        "acme-shop", "guide", "moved", saved.version, session="worker"
    )
    assert gone.is_file
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    content = export_of(export)
    assert content["tables"]["document"][0]["source_path"] == gone.source_path
    with pytest.raises(NotFoundError):
        await svc.document_meta("acme-shop", "guide")


async def test_a_missing_file_document_can_be_deleted(
    svc: Service, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("outside") / "status.md"
    path.write_text("# Status\n", encoding="utf-8")
    saved = await svc.update_document(
        "acme-shop", "old-status", "-", "status", 0, source_path=str(path)
    )
    path.unlink()
    found, _, _ = await svc.document_outline("acme-shop", "old-status")
    assert found.file_missing_at
    await svc.delete_document("acme-shop", "old-status", "file gone", saved.version, session="w")
    assert not path.exists()


async def test_a_purged_card_is_named_with_its_export(svc: Service) -> None:
    task = await svc.create_task("acme-shop", "Receipts", stage="dev", session="worker")
    await svc.set_stage(task.key, "done", session="worker")
    await svc.set_archived(task.key, True, actor="owner")
    [export] = await svc.purge_archived(now=datetime.now(UTC) + timedelta(days=31))

    day = datetime.now(UTC).date().isoformat()
    message = (
        f"Task {task.key} was deleted from the archive on {day}; its export: deleted/{export.name}."
    )
    with pytest.raises(NotFoundError) as caught:
        await svc.get_task(task.key.upper())
    assert str(caught.value) == message
    with pytest.raises(NotFoundError, match="was deleted from the archive"):
        await svc.task_context(task.key)
    with pytest.raises(NotFoundError, match="not found"):
        await svc.get_task("acme-shop-999")


async def test_a_purge_recorded_without_its_export_says_so(svc: Service) -> None:
    async with svc._tx() as tx:
        await tx.event("task.purged", "relaybay", key="acme-shop-77", title="Old")
    with pytest.raises(NotFoundError, match=r"on \d{4}-\d\d-\d\d; export file not recorded"):
        await svc.get_task("acme-shop-77")


async def test_mcp_doc_update_deletes(
    tmp_path: Path, live_server: Callable[[], LiveServer]
) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", session="lead")
        base = {"project": "acme-shop", "body": "-", "delete": True}
        await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body=SPEC,
            reason="first",
            base_version=0,
            kind="spec",
            session="worker",
        )
        refused = await client.call_tool(
            "doc_update",
            {**base, "doc": "spec", "reason": "old", "base_version": 1, "session": "worker"},
        )
        assert refused.is_error and "coordinator" in refused.content[0].text
        stale = await client.call_tool(
            "doc_update",
            {**base, "doc": "spec", "reason": "old", "base_version": 0, "session": "lead"},
        )
        assert stale.is_error and "at version 1, not 0" in stale.content[0].text
        text = await call(
            client, "doc_update", **base, doc="spec", reason="old", base_version=1, session="lead"
        )
        assert "Deleted the document acme-shop/spec" in text and "deleted/" in text
        assert "spec" not in await call(client, "doc_list", project="acme-shop")
    assert len(list((tmp_path / "deleted").glob("*-acme-shop-doc-spec.json"))) == 1


def test_board_delete_needs_csrf_and_the_typed_name(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.update_document, "acme-shop", "spec", SPEC, "first", 0, kind="spec")
    page = board.get("/p/acme-shop/docs/spec").text
    assert 'action="/p/acme-shop/docs/spec/delete"' in page and 'name="confirm"' in page
    url = "/p/acme-shop/docs/spec/delete"
    data = {"csrf_token": csrf_token(token), "confirm": "spec", "base_version": "1"}

    no_csrf = board.post(url, data={**data, "csrf_token": "wrong"}, headers=FORM)
    assert no_csrf.status_code == 403
    wrong = board.post(url, data={**data, "confirm": "spe"}, headers=FORM, follow_redirects=False)
    assert wrong.status_code == 303
    assert "confirm with its name" in board.get(wrong.headers["location"]).text
    stale = board.post(url, data={**data, "base_version": "0"}, headers=FORM)
    assert "at version 1, not 0" in stale.text
    assert run_in_app(board, service.document_meta, "acme-shop", "spec").slug == "spec"

    done = board.post(url, data=data, headers=FORM, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/p/acme-shop/docs"
    assert "Deleted the document spec" in board.get("/p/acme-shop/docs").text
    with pytest.raises(NotFoundError):
        run_in_app(board, service.document_meta, "acme-shop", "spec")
    again = board.post(url, data=data, headers=FORM, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == "/p/acme-shop/docs"
    assert "There is no document spec" in board.get("/p/acme-shop/docs").text


def test_the_docs_list_marks_a_missing_file(
    board: TestClient, tmp_path_factory: pytest.TempPathFactory
) -> None:
    service: Service = board.app.state.service
    path = tmp_path_factory.mktemp("outside") / "guide.md"
    path.write_text("# Guide\n", encoding="utf-8")
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(
        board, service.update_document, "acme-shop", "guide", "-", "g", 0, source_path=str(path)
    )
    path.unlink()
    assert "file missing since" in board.get("/p/acme-shop/docs/guide").text
    assert "file missing since" in board.get("/p/acme-shop/docs").text


@pytest.mark.parametrize(
    "text",
    [
        "Invite link: /admin-invitation?token= followed by the code",
        "`/admin-invitation?token=`",
        "/admin-invitation?token=&next=/home",
        "[invite](/admin-invitation?token=)",
        "/admin-invitation?token=<token>",
        '<a href="/admin-invitation?token=">invite</a>',
        "/admin-invitation?token=",
        "TOKEN=\nOTHER=1",
    ],
)
def test_a_parameter_without_a_value_is_no_secret(text: str) -> None:
    assert find_secrets(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "/admin-invitation?token=a1b2c3d4e5",
        "/admin-invitation?token=a1b2c3d4e5&next=/home",
        "password=hunter2x",
        "password = hunter2x",
        "password=`hunter2x`",
        'token="a1b2c3d4e5"',
        'password= "hunter2"',
        "api_key= 'sk_live_abcdef'",
        "password= hunter2x",
        "export TOKEN=  abc123xyz",
        'TOKEN=\t"abc123"',
    ],
)
def test_a_parameter_with_a_value_is_still_a_secret(text: str) -> None:
    assert [name for _, name in find_secrets(text)] == ["secret-assignment"]


async def test_doc_update_takes_a_text_with_an_empty_token_parameter(svc: Service) -> None:
    body = "# Invites\n\nThe link is `/admin-invitation?token=` plus the code from the mail.\n"
    saved = await svc.update_document("acme-shop", "invites", body, "invite flow", 0)
    assert saved.version == 1
    with pytest.raises(ValidationError, match="secret"):
        await svc.update_document("acme-shop", "invites", body + "\n?token=a1b2c3d4e5\n", "leak", 1)
