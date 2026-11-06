"""Tracker history (one entry per issue key) and the automatic unlog on new work."""

import pytest
from starlette.testclient import TestClient

from relaybay.mcp_server import format as fmt
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app


async def logged_task(service: Service, stage: str = "live") -> str:
    await service.upsert_project("acme-shop")
    task = await service.create_task(
        "acme-shop", "Checkout", stage=stage, tracker_name="Linear", tracker_ref="ENG-1"
    )
    await service.update_task(task.key, {"tracker_logged": True})
    return task.key


async def kinds_after(service: Service, since: int) -> list[str]:
    return [e.kind for e in await service.events_since(since, limit=500)]


async def test_new_ref_appends_and_previous_entries_stay(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", tracker_ref="ENG-1")
    history = (await service.task_detail(task.key)).tracker_history
    assert [(e.tracker_ref, e.logged_at) for e in history] == [("ENG-1", None)]

    await service.update_task(task.key, {"tracker_name": "Linear"})  # same key: same entry
    await service.update_task(task.key, {"tracker_ref": "ENG-2", "tracker_name": "Jira"})
    await service.update_task(task.key, {"tracker_ref": None})  # cleared: history stays
    history = (await service.task_detail(task.key)).tracker_history
    assert [(e.tracker_name, e.tracker_ref) for e in history] == [
        ("Jira", "ENG-2"),
        ("Linear", "ENG-1"),
    ]
    current = await service.get_task(task.key)
    assert (current.tracker_ref, current.tracker_name) == (None, "Jira")


async def test_new_ref_on_logged_task_starts_not_logged(service: Service) -> None:
    key = await logged_task(service)
    since = await service.last_event_id()
    task = await service.update_task(key, {"tracker_ref": "ENG-2"})
    assert not task.tracker_logged
    assert await kinds_after(service, since) == ["task.updated"]
    changes = (await service.events_since(since))[0].payload["changes"]
    assert changes == {"tracker_ref": "ENG-2", "tracker_logged": False}
    new, old = (await service.task_detail(key)).tracker_history
    assert (new.tracker_ref, new.logged_at, new.unlogged_at) == ("ENG-2", None, None)
    assert (old.tracker_ref, old.unlogged_at) == ("ENG-1", None) and old.logged_at is not None


async def test_new_ref_with_explicit_mark_is_logged(service: Service) -> None:
    key = await logged_task(service)
    task = await service.update_task(key, {"tracker_ref": "ENG-2", "tracker_logged": True})
    assert task.tracker_logged
    new, old = (await service.task_detail(key)).tracker_history
    assert new.tracker_ref == "ENG-2" and new.logged_at is not None
    assert old.logged_at is not None and old.unlogged_at is None


async def test_logged_and_unlogged_timestamps(service: Service) -> None:
    key = await logged_task(service)
    (entry,) = (await service.task_detail(key)).tracker_history
    assert entry.logged_at is not None and entry.unlogged_at is None

    await service.update_task(key, {"tracker_logged": False})
    (entry,) = (await service.task_detail(key)).tracker_history
    assert entry.unlogged_at is not None and entry.unlogged_at >= entry.logged_at

    await service.update_task(key, {"tracker_logged": True})  # logged again: a fresh mark
    (relogged,) = (await service.task_detail(key)).tracker_history
    assert relogged.unlogged_at is None and relogged.logged_at >= entry.unlogged_at


@pytest.mark.parametrize(
    ("start", "target"), [("live", "test"), ("done", "dev"), ("done", "live"), ("live", "plan")]
)
async def test_stage_moving_back_unlogs(service: Service, start: str, target: str) -> None:
    key = await logged_task(service, stage=start)
    since = await service.last_event_id()
    moved = await service.set_stage(key, target)
    assert not moved.tracker_logged
    assert await kinds_after(service, since) == ["task.stage_changed", "task.tracker_unlogged"]
    event = (await service.events_since(since))[-1]
    assert event.payload["reason"] == f"stage moved back from {start} to {target}"
    assert event.payload["tracker_ref"] == "ENG-1"
    (entry,) = (await service.task_detail(key)).tracker_history
    assert entry.unlogged_at is not None


@pytest.mark.parametrize(("start", "target"), [("live", "done"), ("test", "dev"), ("dev", "plan")])
async def test_other_stage_moves_keep_the_mark(service: Service, start: str, target: str) -> None:
    key = await logged_task(service, stage=start)
    assert (await service.set_stage(key, target)).tracker_logged


async def test_report_unlogs(service: Service) -> None:
    key = await logged_task(service)
    since = await service.last_event_id()
    task, _ = await service.add_report(key, "changed · S · hotfix")
    assert not task.tracker_logged
    # The report's size sizes the task (task.updated) and clears the mark.
    assert await kinds_after(service, since) == [
        "task.updated",
        "task.tracker_unlogged",
        "report.added",
    ]
    assert (await service.events_since(since))[1].payload["reason"] == "report added"

    await service.update_task(key, {"tracker_logged": True})
    task, _ = await service.add_report(key, "verified · S · again", progress="Hotfix shipped")
    assert not task.tracker_logged
    unlogged = [e for e in await service.events_since(0) if e.kind == "task.tracker_unlogged"]
    assert unlogged[-1].payload["reason"] == "report added, progress changed"


async def test_progress_change_unlogs_in_one_write(service: Service) -> None:
    key = await logged_task(service)
    before = await service.get_task(key)
    since = await service.last_event_id()
    task = await service.update_task(key, {"progress": "More work"})
    assert not task.tracker_logged and task.version == before.version + 1
    assert await kinds_after(service, since) == ["task.updated", "task.tracker_unlogged"]
    updated = (await service.events_since(since))[0]
    assert "tracker_logged" not in updated.payload["changes"]


async def test_metadata_edits_keep_the_mark(service: Service) -> None:
    key = await logged_task(service)
    for changes in (
        {"title": "Checkout v2"},
        {"branch": "feat/checkout"},
        {"waiting_on": "owner"},
        {"next_step": "Watch the logs"},
        {"tracker_url": "https://tracker.example/ENG-1"},
        {"description": "Why"},
    ):
        assert (await service.update_task(key, changes)).tracker_logged, changes
    kinds = await kinds_after(service, 0)
    assert "task.tracker_unlogged" not in kinds


async def test_explicit_mark_wins_over_auto_unlog(service: Service) -> None:
    key = await logged_task(service)
    task = await service.update_task(key, {"progress": "Logged with this", "tracker_logged": True})
    assert task.tracker_logged


async def test_unlogged_task_gets_no_event(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="done", tracker_ref="ENG-1")
    await service.set_stage(task.key, "dev")
    await service.add_report(task.key, "changed · S · more")
    assert "task.tracker_unlogged" not in await kinds_after(service, 0)


async def test_mcp_task_get_shows_history_and_snapshot_stays_short(service: Service) -> None:
    key = await logged_task(service)
    await service.update_task(
        key,
        {"tracker_ref": "ENG-2", "tracker_url": "https://t.example/2", "tracker_logged": True},
    )
    await service.add_report(key, "changed · S · follow-up")
    text = fmt.task_detail(await service.task_detail(key))
    history = text.split("## Tracker history (newest first)\n", 1)[1].split("\n\n", 1)[0]
    lines = history.splitlines()
    assert lines[0].startswith("- Linear ENG-2 https://t.example/2 — logged ")
    assert " · unlogged " in lines[0]
    assert lines[1].startswith("- Linear ENG-1 — logged ") and "unlogged" not in lines[1]
    snapshot = fmt.snapshot(await service.snapshot("acme-shop"))
    assert "tracker: Linear ENG-2 https://t.example/2 · work log not entered" in snapshot
    assert "ENG-1" not in snapshot


def test_task_view_renders_history_escaped(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    task = run_in_app(
        board, service.create_task, "acme-shop", "Checkout", tracker_ref="<b>ENG-1</b>"
    )
    run_in_app(board, service.update_task, task.key, {"tracker_logged": True})
    run_in_app(board, service.add_report, task.key, "changed · S · more work")  # unlogs
    run_in_app(
        board,
        service.update_task,
        task.key,
        {"tracker_ref": "ENG-2", "tracker_name": "Linear", "tracker_url": "https://t.example/2"},
    )
    for url in (f"/t/{task.key}", f"/p/acme-shop/t/{task.key}/view"):
        page = board.get(url).text
        assert "<h2>Tracker history</h2>" in page
        section = page.split("<h2>Tracker history</h2>", 1)[1].split("</details>", 1)[0]
        assert section.index("Linear ENG-2") < section.index("&lt;b&gt;ENG-1&lt;/b&gt;")
        assert '<a class="history-name" href="https://t.example/2"' in section
        assert "<b>ENG-1</b>" not in section
        assert "— not logged" in " ".join(section.split())
        assert " logged <time" in section and " · unlogged <time" in section


def test_board_form_resave_does_not_block_auto_unlog(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Checkout", stage="done")
    task = run_in_app(board, service.update_task, task.key, {"tracker_logged": True})
    form = {
        "csrf_token": csrf_token(token),
        "version": str(task.version),
        "title": task.title,
        "stage": "dev",
        "tracker_logged": ["0", "1"],  # resent unchanged with the stage edit
    }
    headers = {"HX-Request": "true", "Origin": BASE}
    response = board.post(f"/p/acme-shop/t/{task.key}", data=form, headers=headers)
    assert "Saved." in response.text
    assert not run_in_app(board, service.get_task, task.key).tracker_logged
