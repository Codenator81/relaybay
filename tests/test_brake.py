"""The brake: who sets and releases it, what a covered session's hooks answer, the row merge, the
fail-open paths, MCP and the board."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, run_in_app
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_usage import report_session, usage_line, write_lines

FORM = {"Origin": BASE}
DESKTOP = "local_00000000-0000-0000-0000-000000000001"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop", repo_path="/d/acme")
    await service.upsert_project("demo-api", repo_path="/d/demo")
    try:
        yield service
    finally:
        await service.close()


def hook(event: str, cc: str = "cc-1", cwd: str = "/d/acme", **extra: Any) -> dict[str, Any]:
    return {"hook_event_name": event, "session_id": cc, "cwd": cwd, **extra}


async def fire(svc: Service, event: str, cc: str = "cc-1", cwd: str = "/d/acme", **extra: Any):
    payload = hook(event, cc, cwd, **extra)
    await svc.record_hook(payload)
    return await svc.hook_answer(payload)


async def bash(svc: Service, cc: str = "cc-1", cwd: str = "/d/acme"):
    return await fire(svc, "PreToolUse", cc, cwd, tool_name="Bash", tool_input={"command": "ls"})


async def linked(svc: Service, cc: str = "cc-1", name: str = "worker") -> str:
    """A session whose agent linked its hook id itself, bound to a new task."""
    await fire(svc, "SessionStart", cc)
    task = await svc.create_task("acme-shop", f"Work {cc}", session=name, hook_session=cc)
    return task.key


def assert_stopped(answer: dict[str, Any] | None, text: str) -> None:
    assert answer is not None
    assert answer["continue"] is False
    assert text in answer["stopReason"]
    out = answer["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == answer["stopReason"]


# --- what a covered session gets --------------------------------------------


async def test_stop_all_refuses_the_next_tool_call_and_release_lets_it_go(svc: Service) -> None:
    assert await bash(svc) is None
    with svc.bus.subscribe() as sub:
        brake = await svc.set_board_brake("all", reason="Deploy went wrong")
        kinds = [sub.queue.get_nowait().kind for _ in range(sub.queue.qsize())]
    assert kinds == ["brake.set"]
    assert (brake.source, brake.created_by, brake.target) == ("owner", "owner", "all agents")
    assert_stopped(await bash(svc), "Deploy went wrong")
    # A guessed link and no link at all are covered too.
    assert_stopped(await bash(svc, "cc-other", "/nowhere"), "Deploy went wrong")

    await svc.release_brake(brake.id)
    assert await bash(svc) is None
    await svc.release_brake(brake.id)  # again: nothing changes
    assert await svc.open_brakes() == []


async def test_own_tools_pass_and_every_other_tool_does_not(svc: Service) -> None:
    await svc.set_board_brake("all")
    own = await fire(svc, "PreToolUse", tool_name="mcp__relaybay__report_add", tool_input={})
    assert own is None
    for tool in ("Read", "Edit", "mcp__other__thing"):
        answer = await fire(svc, "PreToolUse", tool_name=tool, tool_input={})
        assert_stopped(answer, "Stopped by the owner")


async def test_project_brake_covers_cwd_and_bound_tasks_only(svc: Service) -> None:
    await svc.set_board_brake("project", project="acme-shop", reason="Acme stops")
    assert_stopped(await bash(svc, "cc-a", "/d/acme"), "Acme stops")
    assert await bash(svc, "cc-d", "/d/demo") is None
    # Working elsewhere, bound to a task of the project: its tasks count.
    await svc.create_task("acme-shop", "Far", session="far", hook_session="cc-far")
    assert_stopped(await bash(svc, "cc-far", "/elsewhere"), "Acme stops")


async def test_two_projects_on_one_folder_are_both_braked_there(svc: Service) -> None:
    await svc.upsert_project("acme-web", name="Acme web", repo_path="/d/acme")
    await svc.set_board_brake("project", project="acme-shop", reason="Shared folder")
    assert_stopped(await bash(svc, "cc-a", "/d/acme"), "Shared folder")
    assert_stopped(await bash(svc, "cc-b", "/d/acme/sub"), "Shared folder")
    assert await bash(svc, "cc-d", "/d/demo") is None


async def test_a_task_brake_says_when_it_reaches_nobody(svc: Service) -> None:
    free = (await svc.create_task("acme-shop", "Free")).key
    with collect_notes() as notes:
        await svc.set_brake("task", task=free, reason="Nobody", session="lead")
    assert any(f"no session is bound to {free}" in note for note in notes)

    plain = (await svc.create_task("acme-shop", "Plain", session="plain")).key
    with collect_notes() as notes:
        await svc.set_brake("task", task=plain, reason="Not linked", session="lead")
    assert any("plain, bound to" in note and "not linked" in note for note in notes)

    real = await linked(svc, "cc-1", "worker")
    with collect_notes() as notes:
        await svc.set_brake("task", task=real, reason="Real", session="lead")
    assert not any("Warning" in note for note in notes)


async def test_session_and_task_brakes(svc: Service) -> None:
    key = await linked(svc, "cc-1", "worker")
    other = await linked(svc, "cc-2", "other")
    await svc.set_brake("session", target_session="worker", reason="Only you", session="lead")
    assert_stopped(await bash(svc, "cc-1"), "Only you")
    assert await bash(svc, "cc-2") is None
    assert key != other
    await svc.set_brake("task", task=other, reason="That task", session="lead")
    assert_stopped(await bash(svc, "cc-2"), "That task")
    # A task brake holds a session only while it is bound.
    await svc.unbind_session(other, actor="board")
    assert await bash(svc, "cc-2") is None


async def test_the_owners_prompt_goes_through_with_a_line(svc: Service) -> None:
    await linked(svc)
    await fire(svc, "SessionStart", "cc-guess", "/nowhere")
    assert await fire(svc, "UserPromptSubmit") is None
    await svc.set_board_brake("all", reason="Hold on")
    for cc, cwd in (("cc-1", "/d/acme"), ("cc-guess", "/nowhere")):
        answer = await fire(svc, "UserPromptSubmit", cc, cwd)
        assert answer is not None and "continue" not in answer
        text = answer["hookSpecificOutput"]["additionalContext"]
        assert "relaybay brake on (all agents)" in text and "Hold on" in text


async def test_stop_does_not_block_and_the_wake_up_ends_while_braked(svc: Service) -> None:
    key = await linked(svc)
    await svc.add_owner_comment(key, "Look at this")
    await svc.set_board_brake("all")
    assert await fire(svc, "Stop") is None
    text = await svc.wait_for_comments(hook("Stop"), hold=0.2, poll=0.01)
    assert text is None
    [brake] = await svc.open_brakes()
    await svc.release_brake(brake.id)
    # The comment was kept, not delivered to a session that could not act on it.
    answer = await fire(svc, "Stop")
    assert answer is not None and answer["decision"] == "block"


async def test_a_braked_call_delivers_no_comment(svc: Service) -> None:
    key = await linked(svc)
    await svc.add_owner_comment(key, "Look at this")
    await svc.set_board_brake("all")
    assert_stopped(await bash(svc), "Stopped by the owner")
    [brake] = await svc.open_brakes()
    await svc.release_brake(brake.id)
    answer = await bash(svc)
    assert "Look at this" in answer["hookSpecificOutput"]["additionalContext"]


async def test_the_brake_fails_open(svc: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    await svc.set_board_brake("all")
    await fire(svc, "SessionStart")

    async def broken(*args: Any) -> list[Any]:
        raise RuntimeError("no database")

    monkeypatch.setattr(repo, "open_brakes", broken)
    assert await bash(svc) is None
    assert await fire(svc, "UserPromptSubmit") is None
    assert await fire(svc, "Stop") is None


# --- rows -------------------------------------------------------------------


async def test_merging_a_guessed_row_carries_the_session_brake(svc: Service) -> None:
    await fire(svc, "SessionStart", "cc-9", "/somewhere")
    guessed = await repo.get_session_by_cc(svc._r, "cc-9")
    assert guessed is not None and guessed.kind == "hook"
    await svc.set_brake("session", target_session="cc-9", reason="Caught it", session="lead")
    # The agent links itself: the guessed row is deleted, the brake must survive.
    await svc.create_task(
        "acme-shop", "Link", session=DESKTOP, session_title="desk", hook_session="cc-9"
    )
    assert await repo.get_session_by_id(svc._r, guessed.id) is None
    assert_stopped(await bash(svc, "cc-9"), "Caught it")
    [brake] = await svc.open_brakes()
    assert brake.session_name == "desk"


async def test_a_braked_session_cannot_move_to_another_hook_id(svc: Service) -> None:
    await linked(svc, "cc-1", "worker")
    await svc.set_brake("session", target_session="worker", reason="Stay", session="lead")
    await svc.create_task("acme-shop", "Escape", session="worker", hook_session="made-up")
    assert_stopped(await bash(svc, "cc-1"), "Stay")


async def test_same_target_is_one_brake(svc: Service) -> None:
    first = await svc.set_board_brake("project", project="acme-shop", reason="One")
    again = await svc.set_board_brake("project", project="acme-shop", reason="Two")
    other = await svc.set_board_brake("project", project="demo-api", reason="Three")
    assert again.id == first.id and again.reason == "One"
    assert other.id != first.id
    assert [b.id for b in await svc.open_brakes(project="demo-api")] == [other.id]
    everyone = await svc.set_board_brake("all")
    assert {b.id for b in await svc.open_brakes(project="demo-api")} == {other.id, everyone.id}


async def test_who_may_set(svc: Service) -> None:
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await svc.set_brake("all", reason="No", session="worker")
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await svc.set_brake("all", reason="No")
    assert await svc.open_brakes() == []
    with pytest.raises(ValidationError, match="scope must be"):
        await svc.set_brake("everything", reason="No", session="lead")
    with pytest.raises(ValidationError, match="needs its session"):
        await svc.set_brake("session", reason="No", session="lead")
    with pytest.raises(NotFoundError):  # a misspelt name makes no new session
        await svc.set_brake("session", target_session="typo", reason="No", session="lead")
    with pytest.raises(ValidationError, match="say why"):
        await svc.set_brake("all", reason="  ", session="lead")
    with pytest.raises(ValidationError, match="secret"):
        await svc.set_brake("all", reason="token ghp_" + "Ab12" * 8, session="lead")
    brake = await svc.set_brake("all", reason="Yes", session="lead")
    assert (brake.source, brake.created_by) == ("coordinator", "lead")


async def test_several_sessions_of_one_name_are_refused(svc: Service) -> None:
    await svc.create_task("acme-shop", "A", session=DESKTOP, session_title="same")
    await svc.create_task(
        "acme-shop",
        "B",
        session="local_00000000-0000-0000-0000-000000000002",
        session_title="same",
    )
    with pytest.raises(ValidationError, match="several sessions"):
        await svc.set_brake("session", target_session="same", reason="No", session="lead")


async def test_inbox_lists_an_open_brake(svc: Service) -> None:
    before = (await svc.inbox(["owner"])).count
    brake = await svc.set_board_brake("all", reason="Wait")
    inbox = await svc.inbox(["owner"])
    assert [b.id for b in inbox.brakes] == [brake.id]
    assert inbox.count == before + 1


# --- MCP and board ----------------------------------------------------------


async def test_mcp_has_no_release_and_refuses_a_non_coordinator(live_server: Any) -> None:
    async with mcp_connect(live_server()) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"brake_set", "brake_status", "budget_get"} <= names
        assert not {n for n in names if "release" in n or n.startswith("budget_s")}
        assert "No brake is on" in await call(client, "brake_status")
        refused = await client.call_tool(
            "brake_set", {"scope": "all", "reason": "Stop", "session": "worker"}
        )
        assert refused.is_error and "only for the coordinator" in refused.content[0].text
        assert "No brake is on" in await call(client, "brake_status")
        assert "No token budgets" in await call(client, "budget_get")


def test_board_stop_all_needs_a_browser_and_only_the_board_releases(
    board: TestClient, token: str
) -> None:
    csrf = {"csrf_token": csrf_token(token)}
    service: Service = board.app.state.service
    page = board.get("/sessions").text
    assert "Stop all agents" in page and "data-confirm=" in page
    assert 'id="brake-banner"' in page and "Brake on" not in page

    denied = board.post("/brake", data={"scope": "all"}, headers=FORM, follow_redirects=False)
    assert denied.status_code in (400, 403)
    assert run_in_app(board, service.open_brakes) == []

    done = board.post(
        "/brake",
        data={**csrf, "scope": "all", "reason": "<b>Now</b>", "next": "/sessions"},
        headers=FORM,
        follow_redirects=False,
    )
    assert (done.status_code, done.headers["location"]) == (303, "/sessions")
    page = board.get("/sessions").text
    assert "Brake on: all agents." in page and "&lt;b&gt;Now&lt;/b&gt;" in page
    assert "Release" in page
    assert "Brake on: all agents." in board.get("/usage").text
    assert "Brake on: all agents." in board.get("/inbox").text
    fragment = board.get("/fragments/brake-banner?next=/usage").text
    assert "Brake on: all agents." in fragment and 'value="/usage"' in fragment
    # A form field cannot send the browser elsewhere.
    assert 'value="https://' not in board.get("/fragments/brake-banner?next=https://x.test/").text

    [brake] = run_in_app(board, service.open_brakes)
    denied = board.post(f"/brake/{brake.id}/release", data={}, headers=FORM)
    assert denied.status_code in (400, 403)
    bearer = board.post(
        f"/brake/{brake.id}/release",
        data=csrf,
        headers={**FORM, "Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert bearer.status_code == 403
    assert len(run_in_app(board, service.open_brakes)) == 1
    released = board.post(
        f"/brake/{brake.id}/release",
        data={**csrf, "next": "https://x.test/"},
        headers=FORM,
        follow_redirects=False,
    )
    assert (released.status_code, released.headers["location"]) == (303, "/sessions")
    assert run_in_app(board, service.open_brakes) == []
    assert "Brake on" not in board.get("/sessions").text


def test_board_stop_of_a_project(board: TestClient, token: str) -> None:
    csrf = {"csrf_token": csrf_token(token)}
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    page = board.get("/p/acme-shop/sessions").text
    assert 'name="scope" value="project"' in page and "Stop all agents of Acme Shop?" in page
    board.post(
        "/brake",
        data={**csrf, "scope": "project", "project": "acme-shop", "next": "/p/acme-shop/sessions"},
        headers=FORM,
    )
    [brake] = run_in_app(board, service.open_brakes)
    assert brake.target == "project acme-shop"
    bad = board.post(
        "/brake",
        data={**csrf, "scope": "session", "next": "/sessions"},
        headers=FORM,
    )
    assert "The board stops all agents or the agents of one project." in bad.text


# --- review round 1 -----------------------------------------------------------


async def test_a_braked_guessed_row_keeps_its_hooks_against_a_made_up_id(svc: Service) -> None:
    await fire(svc, "SessionStart", "cc-g", "/somewhere")
    row = await repo.get_session_by_cc(svc._r, "cc-g")
    assert row is not None and not row.cc_exact
    await svc.set_brake("session", target_session="cc-g", reason="Stay", session="lead")
    await svc.create_task("acme-shop", "Escape", session=row.name, hook_session="made-up")
    assert_stopped(await bash(svc, "cc-g", "/somewhere"), "Stay")


async def test_a_real_id_the_hooks_saw_carries_the_brake(svc: Service) -> None:
    await fire(svc, "SessionStart", "cc-old", "/somewhere")
    row = await repo.get_session_by_cc(svc._r, "cc-old")
    assert row is not None
    await svc.set_brake("session", target_session="cc-old", reason="Stay", session="lead")
    await fire(svc, "SessionStart", "cc-new", "/elsewhere")  # a /clear the hooks saw
    await svc.create_task("acme-shop", "Go on", session=row.name, hook_session="cc-new")
    assert_stopped(await bash(svc, "cc-new", "/elsewhere"), "Stay")


async def test_an_exact_live_braked_row_stays_even_for_a_real_id(svc: Service) -> None:
    await linked(svc, "cc-1", "worker")
    await svc.set_brake("session", target_session="worker", reason="Stay", session="lead")
    await fire(svc, "SessionStart", "cc-seen", "/elsewhere")
    await svc.create_task("acme-shop", "Escape", session="worker", hook_session="cc-seen")
    assert_stopped(await bash(svc, "cc-1"), "Stay")


async def test_the_desktop_windows_clear_keeps_the_session_brake(svc: Service) -> None:
    await fire(svc, "SessionStart", "cc-1")
    await svc.create_task(
        "acme-shop", "Desk", session=DESKTOP, session_title="desk", hook_session="cc-1"
    )
    await svc.set_brake("session", target_session=DESKTOP, reason="Stay", session="lead")
    await fire(
        svc,
        "SessionStart",
        "cc-2",
        source="clear",
        relaybay_host_session=DESKTOP,
        relaybay_host_attended="1",
    )
    row = await repo.get_session_by_cc(svc._r, "cc-2")
    assert row is not None and row.external_id == DESKTOP
    assert_stopped(await bash(svc, "cc-2"), "Stay")


async def test_a_project_brake_holds_a_session_in_its_folder_with_other_links(svc: Service) -> None:
    await fire(svc, "SessionStart", "cc-1", "/d/acme")
    await svc.create_task("demo-api", "Elsewhere", session="worker", hook_session="cc-1")
    await svc.set_board_brake("project", project="acme-shop", reason="Acme stops")
    assert_stopped(await bash(svc, "cc-1", "/d/acme"), "Acme stops")


async def test_an_agent_cannot_lift_a_brake_by_its_binding(svc: Service) -> None:
    key = await linked(svc, "cc-1", "worker")
    await svc.set_brake("task", task=key, reason="Hold", session="lead")
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.unbind_session(key, caller="worker")
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.bind_session(key, "other", caller="worker")
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.unbind_session(key)  # no caller at all
    assert_stopped(await bash(svc), "Hold")

    # The owner on the board may, and so may a coordinator.
    await svc.unbind_session(key, actor="board")
    assert await bash(svc) is None
    await svc.bind_session(key, "other", caller="lead")
    assert (await svc.get_task(key)).session_name == "other"


async def test_a_project_brake_holds_the_bindings_and_all_does_not(svc: Service) -> None:
    key = await linked(svc, "cc-1", "worker")
    brake = await svc.set_board_brake("project", project="acme-shop", reason="Hold")
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.unbind_session(key, caller="worker")
    await svc.release_brake(brake.id)
    await svc.set_board_brake("all")
    await svc.unbind_session(key, caller="worker")  # a binding cannot lift `all`


async def test_a_stop_budgets_brake_holds_the_binding(svc: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    await report_session(svc, path)
    task = await svc.create_task("acme-shop", "Work", session="worker", hook_session="cc-1")
    await svc.set_budget("task", task=task.key, period="total", max_tokens=100, action="stop")
    write_lines(path, usage_line("m1", tokens=20, day=datetime.now(UTC) + timedelta(seconds=1)))
    await svc.usage_pass()
    await svc.limits_pass()
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.unbind_session(task.key, caller="worker")


async def test_tool_search_passes_under_the_brake(svc: Service) -> None:
    await svc.set_board_brake("all")
    assert await fire(svc, "PreToolUse", tool_name="ToolSearch", tool_input={}) is None


def test_the_stop_survives_a_failing_write(
    board: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.set_board_brake, "all", reason="Hold")

    async def broken(*args: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service, "record_hook", broken)
    answer = board.post(
        "/hooks/claude-code",
        json=hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert answer.status_code == 200 and answer.json()["continue"] is False


def test_a_spooled_call_gets_no_stop_and_is_recorded(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.set_board_brake, "all", reason="Hold")
    answer = board.post(
        "/hooks/claude-code",
        json=hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "ls"},
            relaybay_spooled_at="2026-01-01T00:00:00Z",
        ),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert answer.status_code == 204


def test_the_stopped_event_is_still_recorded(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.set_board_brake, "all", reason="Hold")
    board.post(
        "/hooks/claude-code",
        json=hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}),
        headers={"Authorization": f"Bearer {token}"},
    )
    row = run_in_app(board, repo.get_session_by_cc, service._r, "cc-1")
    assert row is not None and row.status == "working"


async def test_a_session_brake_without_hooks_says_it_reaches_nobody(svc: Service) -> None:
    await svc.create_task("acme-shop", "Named", session="named")
    with collect_notes() as notes:
        await svc.set_brake("session", target_session="named", reason="Hold", session="lead")
    assert any("not linked to hooks yet" in n for n in notes)


async def test_a_merge_moves_open_bindings(svc: Service) -> None:
    # A guessed row bound to a task; the agent then links the id to its own session.
    await fire(svc, "SessionStart", "cc-9", "/somewhere")
    guessed = await repo.get_session_by_cc(svc._r, "cc-9")
    assert guessed is not None
    task = await svc.create_task("acme-shop", "Bound")
    await svc.bind_session(task.key, guessed.name)
    await svc.set_brake("task", task=task.key, reason="Hold", session="lead")
    assert_stopped(await bash(svc, "cc-9", "/somewhere"), "Hold")
    await svc.create_task(
        "demo-api", "Link", session=DESKTOP, session_title="desk", hook_session="cc-9"
    )
    assert (await svc.get_task(task.key)).session_name == "desk"
    assert_stopped(await bash(svc, "cc-9", "/somewhere"), "Hold")
