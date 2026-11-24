"""Card fields of stage 1: deploy notes and their done mark, a cancelled card, description_add,
a worktree per linked repository, the tracker mark kept by a closing report and a remark on a
review step."""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.domain.errors import ValidationError
from relaybay.domain.stages import Stage
from relaybay.mcp_server import format as fmt
from relaybay.mcp_server.server import build_mcp
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app

CC = "6c1f0a52-2b7e-4d1a-9f43-0e5b8a7c9d11"
DESKTOP = "local_00000000-0000-0000-0000-0000000000a1"
ROOT = "/home/dev/acme-shop"
BASELINE = Path(__file__).parent / "fixtures" / "mcp_contract_baseline.json"
HTMX = {"Origin": BASE, "HX-Request": "true"}


def caller(service: Service) -> Any:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    return call


async def make_task(service: Service, stage: str = "dev") -> str:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage=stage)
    return task.key


async def test_description_add_appends_a_paragraph(service: Service) -> None:
    key = await make_task(service)
    await service.update_task(key, {"description": "Pay by card."})
    task = await service.update_task(key, {"description_add": "Also refunds."})
    assert task.description == "Pay by card.\n\nAlso refunds."
    fresh = await service.create_task("acme-shop", "Empty")
    added = await service.update_task(fresh.key, {"description_add": "First line."})
    assert added.description == "First line."
    with pytest.raises(ValidationError, match="description is longer than"):
        await service.update_task(key, {"description_add": "x" * 20000})
    assert "description_add" in fmt.task_schema()


async def test_deploy_notes_clear_deploy_done_unless_passed(service: Service) -> None:
    key = await make_task(service)
    task = await service.update_task(key, {"deploy_notes": "Run 0060.", "deploy_done": True})
    assert (task.deploy_notes, task.deploy_done) == ("Run 0060.", True)
    task = await service.update_task(key, {"deploy_notes": "Run 0060, then the flag."})
    assert task.deploy_done is False
    task = await service.update_task(key, {"deploy_done": True})
    task = await service.update_task(key, {"deploy_notes": "Run 0061.", "deploy_done": True})
    assert (task.deploy_notes, task.deploy_done) == ("Run 0061.", True)
    # The same notes again are no change: the mark stays.
    task = await service.update_task(key, {"deploy_notes": "Run 0061."})
    assert task.deploy_done is True


async def test_cancelled_stamps_its_time_and_clears(service: Service) -> None:
    key = await make_task(service)
    call = caller(service)
    answer = await call(
        "task_patch", task=key, fields={"cancelled": "The client dropped the feature."}
    )
    assert "is marked cancelled and stays in dev" in answer
    task = await service.get_task(key)
    assert task.cancelled == "The client dropped the feature." and task.cancelled_at
    assert task.stage == Stage.DEV
    context = await service.task_context(key)
    assert f"Cancelled: The client dropped the feature. ({task.cancelled_at[:10]})" in context
    await call("task_patch", task=key, fields={"cancelled": "-"})
    task = await service.get_task(key)
    assert task.cancelled is None and task.cancelled_at is None


async def test_deploy_notes_show_in_task_context_and_task_get(service: Service) -> None:
    key = await make_task(service)
    await service.update_task(key, {"deploy_notes": "Migration 0060; rollback: drop columns."})
    context = await service.task_context(key)
    assert "Deploy notes: not done yet: Migration 0060; rollback: drop columns." in context
    detail = fmt.task_detail(await service.task_detail(key))
    assert "## Deploy notes (not done yet)\nMigration 0060" in detail


async def test_a_report_on_done_keeps_the_mark_and_on_dev_clears_it(service: Service) -> None:
    await service.upsert_project("acme-shop")
    done = await service.create_task("acme-shop", "Shipped", stage="done", tracker_ref="ENG-1")
    await service.update_task(done.key, {"tracker_logged": True})
    task, _ = await service.add_report(done.key, "verified · S · closed", progress="All shipped")
    assert task.tracker_logged is True
    dev = await service.create_task("acme-shop", "Ongoing", stage="dev", tracker_ref="ENG-2")
    await service.update_task(dev.key, {"tracker_logged": True})
    task, _ = await service.add_report(dev.key, "changed · S · more work")
    assert task.tracker_logged is False
    # A move back from done still clears it.
    task = await service.update_task(done.key, {"stage": "dev"})
    assert task.tracker_logged is False


async def test_a_remark_comments_under_the_step_and_keeps_its_status(service: Service) -> None:
    key = await make_task(service, "review")
    await service.set_review_steps(key, ["Login with 2FA", "Logout"])
    call = caller(service)
    answer = await call(
        "review_step_update", task=key, step=1, remark="Fixed the **code** field.", session="dev"
    )
    assert answer == "Step 1 'Login with 2FA' is open. Your remark is under it."
    _, comments = await service.list_comments(key)
    [remark] = comments
    steps = (await service.review(key))[1]
    assert remark.step_id == steps.steps[0].id and remark.author_kind == "agent"
    assert remark.body_md == "Fixed the **code** field."
    assert steps.steps[0].status == "open"


async def test_repos_entry_with_an_absolute_path_sets_the_worktree(service: Service) -> None:
    await service.upsert_project("acme-shop", repos=[{"name": "web", "path": f"{ROOT}/web"}])
    task = await service.create_task("acme-shop", "Checkout", stage="dev")
    await service.update_task(task.key, {"repos": [f"web {ROOT}/web/.worktrees/pay"]})
    code = (await service.task_detail(task.key)).code
    assert [(r.repo.name, r.worktree) for r in code.repos] == [
        ("web", f"{ROOT}/web/.worktrees/pay")
    ]
    await service.update_task(task.key, {"repos": ["web"]})  # the name alone keeps it
    code = (await service.task_detail(task.key)).code
    assert code.repos[0].worktree == f"{ROOT}/web/.worktrees/pay"
    await service.update_task(task.key, {"repos": ["web C:\\code\\acme-web-pay"]})
    code = (await service.task_detail(task.key)).code
    assert code.repos[0].worktree == "C:\\code\\acme-web-pay"
    with pytest.raises(ValidationError, match="not an absolute path"):
        await service.update_task(task.key, {"repos": ["web worktrees/pay"]})
    detail = fmt.task_detail(await service.task_detail(task.key))
    assert "- web `/home/dev/acme-shop/web` · worktree `C:\\code\\acme-web-pay`" in detail


async def test_hook_auto_link_stores_the_worktree(service: Service, tmp_path: Path) -> None:
    web = tmp_path / "web"
    await service.upsert_project("acme-shop", repos=[{"name": "web", "path": str(web)}])
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP, hook_session=CC
    )
    worktree = web / ".claude" / "worktrees" / "pay"
    (worktree / "src").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    folder = str(worktree)
    hook = {
        "session_id": CC,
        "cwd": str(worktree / "src"),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "go",
    }
    await service.record_hook(hook)
    before = await service.last_event_id()
    await service.record_hook({**hook, "cwd": folder, "hook_event_name": "Stop"})
    assert not [e for e in await service.replay(before, None, 50) if e.kind == "task.updated"]
    code = (await service.task_detail(task.key)).code
    assert [(r.repo.name, r.how, r.worktree) for r in code.repos] == [("web", "auto", folder)]
    assert f"Worktree: web: {folder}" in await service.task_context(task.key)


async def test_a_deploy_request_shows_the_cards_notes(service: Service) -> None:
    key = await make_task(service)
    await service.save_environment(
        "acme-shop", {"name": "live", "kind": "live", "level": "full"}, confirm_live=True
    )
    call = caller(service)
    asked = await call(
        "approval_request",
        task=key,
        action="deploy",
        target="live",
        summary="Release 1.2",
        rollback="kubectl rollout undo deploy/web",
        session="checkout-dev",
    )
    assert "The card has no deploy notes" in asked
    await service.update_task(key, {"deploy_notes": "Run migration 0060 first."})
    asked = await call(
        "approval_request",
        task=key,
        action="deploy",
        target="live",
        summary="Release 1.2",
        rollback="kubectl rollout undo deploy/web",
        session="checkout-dev",
    )
    assert "Deploy notes of the card (not done yet)" in asked
    assert "Run migration 0060 first." in asked
    [approval] = await service.open_approval_requests()
    assert approval.task_deploy_notes == "Run migration 0060 first."


async def test_the_tool_list_is_unchanged_and_task_update_takes_the_card_fields(
    service: Service,
) -> None:
    """Since relaybay-40 task_update names the deploy and cancel fields and takes the rest in
    `fields`; description_add stays a `fields` entry."""
    mcp = build_mcp(service)
    tools = {t.name: t for t in await mcp.list_tools()}
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(baseline["tools"]) <= set(tools)  # additions arrive with the next rebaseline
    assert "remark" in tools["review_step_update"].input_schema["properties"]
    update = tools["task_update"].input_schema["properties"]
    assert {"deploy_notes", "deploy_done", "cancelled", "fields", "stage"} <= set(update)
    assert "description_add" not in update
    with pytest.raises(ToolError, match="Unknown task field"):
        await mcp.call_tool("task_patch", {"task": "x-1", "fields": {"nope": 1}})


def test_board_shows_cancelled_and_saves_deploy_fields(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Checkout", stage="dev")
    data = {
        "csrf_token": csrf_token(token),
        "version": str(task.version),
        "deploy_notes": "Run <b>0060</b>.",
        "deploy_done": ["0", "1"],
        "cancelled": "Dropped by the client",
    }
    response = board.post(f"/p/acme-shop/t/{task.key}", data=data, headers=HTMX)
    assert 'class="form-ok"' in response.text
    saved = run_in_app(board, service.get_task, task.key)
    assert (saved.deploy_notes, saved.deploy_done) == ("Run <b>0060</b>.", True)
    assert saved.cancelled == "Dropped by the client" and saved.cancelled_at
    # The checkbox resent as it is with edited notes: the notes clear the mark.
    data.update(version=str(saved.version), deploy_notes="Run 0061.")
    board.post(f"/p/acme-shop/t/{task.key}", data=data, headers=HTMX)
    assert run_in_app(board, service.get_task, task.key).deploy_done is False
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert "cancelled-chip" in card
    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "<dt>Cancelled</dt>" in view and "Deploy notes" in view and "<b>" not in view


def test_the_worktree_is_the_top_of_the_working_copy(tmp_path: Path) -> None:
    from relaybay.service import _work_root

    (tmp_path / "web" / ".git").mkdir(parents=True)
    (tmp_path / "web" / "src" / "pages").mkdir(parents=True)
    assert _work_root(str(tmp_path / "web" / "src" / "pages")) == str(tmp_path / "web")
    linked = tmp_path / "web-wt"
    (linked / "src").mkdir(parents=True)
    (linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a linked worktree
    assert _work_root(str(linked / "src")) == str(linked)
    assert _work_root("/no/such/folder/at/all") == "/no/such/folder/at/all"


def test_the_worktree_search_stops_at_the_repository(tmp_path: Path) -> None:
    from relaybay.service import _work_root

    (tmp_path / ".git").mkdir()  # an outer checkout above the repository
    repo_path = tmp_path / "sub" / "web"
    (repo_path / "src").mkdir(parents=True)
    assert _work_root(str(repo_path / "src"), str(repo_path)) == str(repo_path)
