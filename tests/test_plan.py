"""Task plan: steps and checkpoints, history, acceptance, the 'work by plan' rule, task size, the
agent's Claude Code todo from hooks, plan reminders, MCP tools, the board's Plan tab, migration."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.db.connection import connect
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.domain.plan import (
    TODO_ITEMS_MAX,
    TODO_TEXT_MAX,
    clean_conformance,
    parse_todo,
    title_diff,
)
from relaybay.domain.rules import validate_rules
from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_comments import context_of, fire, linked_task
from tests.test_hooks import CC_A, CC_B, DESKTOP_A
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import NOW, make_old_db

HTMX = {"HX-Request": "true", "Origin": BASE}
STEPS = [
    {"title": "Migration and model", "details": "Store addresses per account."},
    {"title": "Address book page", "details": "Add, edit and remove addresses."},
    {"title": "Picker at checkout", "details": "Pick a saved address instead of typing."},
]


def todo_create(item_id: str, subject: str, active: str | None = None) -> dict[str, Any]:
    """PostToolUse of TaskCreate as Claude Code 2.1 sends it."""
    return {
        "tool_name": "TaskCreate",
        "tool_input": {"subject": subject, "description": "", "activeForm": active or subject},
        "tool_response": {"task": {"id": item_id, "subject": subject}},
    }


def todo_update(item_id: str, status: str) -> dict[str, Any]:
    return {
        "tool_name": "TaskUpdate",
        "tool_input": {"taskId": item_id, "status": status},
        "tool_response": {
            "success": True,
            "taskId": item_id,
            "updatedFields": ["status"],
            "statusChange": {"from": "pending", "to": status},
        },
    }


def todo_write(*items: tuple[str, str]) -> dict[str, Any]:
    """PostToolUse of TodoWrite (older builds): the whole list each time."""
    todos = [{"content": c, "status": s, "activeForm": f"Doing {c}"} for c, s in items]
    return {"tool_name": "TodoWrite", "tool_input": {"todos": todos}, "tool_response": {}}


def post(**extra: Any) -> dict[str, Any]:
    return {"hook_event_name": "PostToolUse", "session_id": CC_A, **extra}


# --- domain --------------------------------------------------------------------------------


def test_parse_todo_reads_the_three_tools_and_ignores_other_shapes() -> None:
    created = parse_todo(post(**todo_create("1", "Write the migration", "Writing it")))
    assert created is not None
    assert (created.kind, created.item_id, created.subject, created.active_form) == (
        "create",
        "1",
        "Write the migration",
        "Writing it",
    )
    updated = parse_todo(post(**todo_update("1", "in_progress")))
    assert updated is not None and (updated.kind, updated.status) == ("update", "in_progress")
    written = parse_todo(post(**todo_write(("A", "completed"), ("B", "in_progress"))))
    assert written is not None and written.kind == "replace"
    assert [(i.item_id, i.subject, i.status) for i in written.items] == [
        ("1", "A", "completed"),
        ("2", "B", "in_progress"),
    ]
    # The status can also come only from the response; a JSON-string response is read too.
    only_response = post(
        tool_name="TaskUpdate",
        tool_input={"taskId": 2},
        tool_response='{"taskId": "2", "statusChange": {"from": "pending", "to": "completed"}}',
    )
    parsed = parse_todo(only_response)
    assert parsed is not None and (parsed.item_id, parsed.status) == ("2", "completed")

    ignored = [
        post(tool_name="Bash", tool_input={"command": "ls"}),
        {"hook_event_name": "PreToolUse", **todo_create("1", "x")},
        post(tool_name="TaskCreate", tool_input={"subject": "x"}, tool_response={}),
        post(tool_name="TaskUpdate", tool_input={"taskId": "1", "status": "weird"}),
        post(tool_name="TodoWrite", tool_input={"todos": "not a list"}),
        post(tool_name="TaskUpdate", tool_input=["not", "a", "dict"]),
        "not a payload",
    ]
    assert [parse_todo(p) for p in ignored] == [None] * len(ignored)


def test_parse_todo_caps_sizes_and_skips_bad_items() -> None:
    many = [(f"item {n}", "pending") for n in range(TODO_ITEMS_MAX + 20)]
    payload = todo_write(*many)
    payload["tool_input"]["todos"].insert(0, {"content": 7, "status": "pending"})
    payload["tool_input"]["todos"].insert(1, {"content": "ok", "status": "nope"})
    parsed = parse_todo(post(**payload))
    assert parsed is not None and len(parsed.items) <= TODO_ITEMS_MAX
    assert all(i.subject.startswith("item ") for i in parsed.items)
    long = parse_todo(post(**todo_create("1", "x" * 5000)))
    assert long is not None and long.subject is not None
    assert len(long.subject) == TODO_TEXT_MAX


def test_conformance_is_as_planned_or_a_deviation_with_a_reason() -> None:
    assert clean_conformance(" As Planned ") == "as planned"
    assert clean_conformance("Deviation:  kept the old API") == "deviation: kept the old API"
    for bad in ("", None, "deviation:", "fine", "deviation"):
        with pytest.raises(ValidationError, match="as planned"):
            clean_conformance(bad)


def test_title_diff_names_the_changed_steps() -> None:
    assert title_diff(["A", "B"], ["A", "B"]) == []
    assert title_diff(["A", "B"], ["A", "C", "D"]) == ["- 2. B", "+ 2. C", "+ 3. D"]
    assert title_diff([], ["A"]) == ["+ 1. A"]


def test_plan_rule_validation() -> None:
    clean = validate_rules(
        {"stage_requirements": {"dev": [{"kind": "plan_accepted", "unless_size": ["S", "M"]}]}},
        "test",
    )
    assert clean["stage_requirements"]["dev"] == [
        {"kind": "plan_accepted", "unless_size": ["S", "M"]}
    ]
    assert validate_rules({"stage_requirements": {"dev": [{"kind": "plan_accepted"}]}}, "test")[
        "stage_requirements"
    ]["dev"] == [{"kind": "plan_accepted", "unless_size": []}]
    with pytest.raises(ValueError, match="unless_size: unknown size"):
        validate_rules(
            {"stage_requirements": {"dev": [{"kind": "plan_accepted", "unless_size": ["XL"]}]}},
            "test",
        )
    with pytest.raises(ValueError, match="unknown key"):
        validate_rules(
            {"stage_requirements": {"dev": [{"kind": "plan_accepted", "field": "x"}]}}, "test"
        )


# --- plan: replace, checkpoints, acceptance, history --------------------------------------


async def planned(svc: Service, stage: str = "plan") -> str:
    await svc.upsert_project("acme-shop")
    task = await svc.create_task("acme-shop", "Saved addresses", stage=stage)
    await svc.set_plan(task.key, STEPS, reason="from PLAN.md", session="addresses-agent")
    return task.key


async def test_plan_set_keeps_steps_whose_title_stays(service: Service) -> None:
    key = await planned(service)
    task, plan, _ = await service.plan(key)
    assert plan.status == "draft" and task.plan_status == "draft"
    assert [s.status for s in plan.steps] == ["pending"] * 3
    await service.update_plan_step(key, 1, "done", conformance="as planned", outcome="Table")
    await service.update_plan_step(key, 2, "in_progress")

    # Step 1 keeps its checkpoint; step 2 is renamed (starts again); step 3 is gone.
    changed = [STEPS[0] | {"details": "New details only"}, {"title": "Address book v2"}]
    task, plan, reverted = await service.set_plan(key, changed)
    assert not reverted
    assert [(s.title, s.status) for s in plan.steps] == [
        ("Migration and model", "done"),
        ("Address book v2", "pending"),
    ]
    assert plan.steps[0].details_md == "New details only"
    assert plan.steps[0].conformance == "as planned" and plan.steps[0].outcome == "Table"
    assert (task.plan_total, task.plan_done, task.plan_now) == (2, 1, None)

    # The same plan again changes nothing and is not kept as a revision.
    before = len((await service.plan_history(key))[1])
    _, again, _ = await service.set_plan(key, changed)
    assert len((await service.plan_history(key))[1]) == before and again.done == 1

    # An empty list removes the plan.
    task, plan, _ = await service.set_plan(key, [])
    assert plan.steps == [] and plan.status is None and task.plan_status is None


async def test_plan_set_validates_its_input(service: Service) -> None:
    key = await planned(service)
    for bad, message in (
        ("not a list", "must be a list"),
        ([{"title": ""}], "needs a title"),
        ([{"title": "x", "why": "y"}], "title and details"),
        ([{"title": "x"}] * 31, "at most 30"),
    ):
        with pytest.raises(ValidationError, match=message):
            await service.set_plan(key, bad)


async def test_accepted_plan_turns_draft_only_when_titles_or_order_change(
    service: Service,
) -> None:
    key = await planned(service)
    with service.bus.subscribe() as sub:
        task, plan, accepted = await service.accept_plan(key, actor="owner")
        assert sub.queue.get_nowait().kind == "plan.accepted"
    assert accepted and plan.status == "accepted" and task.plan_accepted_by == "owner"
    assert not (await service.accept_plan(key))[2]  # already accepted: nothing written

    # Step statuses and details do not make it a draft.
    await service.update_plan_step(key, 1, "in_progress")
    await service.set_plan(key, [STEPS[0] | {"details": "Tweaked"}, *STEPS[1:]])
    assert (await service.get_task(key)).plan_status == "accepted"

    # A new order does.
    _, plan, reverted = await service.set_plan(key, [STEPS[1], STEPS[0], STEPS[2]])
    task = await service.get_task(key)
    assert reverted and plan.status == "draft"
    assert (task.plan_status, task.plan_accepted_at, task.plan_accepted_by) == ("draft", None, None)


async def test_accepting_needs_a_plan(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Empty")
    with pytest.raises(ValidationError, match="no plan to accept"):
        await service.accept_plan(task.key)


async def test_done_is_a_checkpoint(service: Service) -> None:
    key = await planned(service)
    _, report = await service.add_report(key, "changed · M · table")
    with pytest.raises(ValidationError, match="as planned"):
        await service.update_plan_step(key, 1, "done")
    with pytest.raises(ValidationError, match="checkpoint of a done step"):
        await service.update_plan_step(key, 1, "in_progress", conformance="as planned")
    with pytest.raises(ValidationError, match="status must be one of"):
        await service.update_plan_step(key, 1, "finished")
    with pytest.raises(NotFoundError, match="no plan step 9"):
        await service.update_plan_step(key, 9, "done", conformance="as planned")
    with pytest.raises(ValidationError, match="not a report of"):
        await service.update_plan_step(key, 1, "done", conformance="as planned", link=999)
    with pytest.raises(ValidationError, match="http"):
        await service.update_plan_step(
            key, 1, "done", conformance="as planned", link="ftp://example.com"
        )

    with service.bus.subscribe() as sub:
        _, step = await service.update_plan_step(
            key, 1, "done", conformance="as planned", outcome="Table", link=f"#{report.id}"
        )
        event = sub.queue.get_nowait()
    assert (step.status, step.link, step.conformance) == (
        "done",
        f"report:{report.id}",
        "as planned",
    )
    assert step.done_at is not None
    assert (event.kind, event.payload["position"], event.payload["status"]) == (
        "plan.step_updated",
        1,
        "done",
    )
    _, step = await service.update_plan_step(
        key,
        2,
        "done",
        conformance="deviation: editing comes later",
        link="https://git.example.com/acme-shop/pull/7",
    )
    assert step.deviation and step.link == "https://git.example.com/acme-shop/pull/7"
    task = await service.get_task(key)
    assert (task.plan_done, task.plan_deviations) == (2, 1)

    # Leaving done clears the checkpoint; the same status again writes nothing.
    _, step = await service.update_plan_step(key, 2, "blocked", outcome="waiting for the form")
    assert (step.status, step.done_at, step.link, step.conformance, step.outcome) == (
        "blocked",
        None,
        None,
        None,
        "waiting for the form",
    )
    before = len((await service.plan_history(key))[1])
    await service.update_plan_step(key, 2, "blocked", outcome="waiting for the form")
    assert len((await service.plan_history(key))[1]) == before


async def test_every_plan_write_is_kept_with_the_title_diff(service: Service) -> None:
    key = await planned(service)
    await service.accept_plan(key, actor="owner")
    await service.update_plan_step(key, 1, "in_progress")
    await service.set_plan(key, [STEPS[0], {"title": "Address book page v2"}], reason="split")
    _, history = await service.plan_history(key)
    assert [r.reason for r, _ in history] == [
        "split",
        "step 1 in_progress",
        "accepted",
        "from PLAN.md",
    ]
    newest, diff = history[0]
    assert newest.author == "agent" and newest.body["status"] == "draft"
    assert diff == [
        "- 2. Address book page",
        "+ 2. Address book page v2",
        "- 3. Picker at checkout",
    ]
    first, first_diff = history[-1]
    assert first.author == "addresses-agent" and len(first_diff) == 3
    assert [s["title"] for s in first.body["steps"]] == [s["title"] for s in STEPS]


# --- rules and size ------------------------------------------------------------------------


async def test_moving_to_dev_needs_an_accepted_plan_unless_size_s(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.set_pulse("acme-shop", "On track.")
    unknown = await service.create_task("acme-shop", "Unknown size")
    with collect_notes() as notes:
        saved = await service.set_stage_checked(unknown.key, "dev")
    assert saved.warnings == [
        "Warning (rules): moving to dev usually needs an accepted plan (not needed for size S)."
        " Write it with plan_set (2-5 steps), then ask for acceptance."
    ]
    assert notes == [
        f"Hint: {unknown.key} has no plan and no todo yet: write down what you are going to do"
        " (todo or 2-3 steps with plan_set)."
    ]

    small = await service.create_task("acme-shop", "Typo")
    await service.update_task(small.key, {"size": "s"})
    with collect_notes() as notes:
        assert (await service.set_stage_checked(small.key, "dev")).warnings == []
    assert len(notes) == 1  # S needs no accepted plan, but still gets the soft hint

    draft = await planned(service)
    with collect_notes() as notes:
        saved = await service.set_stage_checked(draft, "dev")
    assert "The plan is not accepted yet" in saved.warnings[0] and notes == []

    accepted = await planned(service)
    await service.accept_plan(accepted)
    assert (await service.set_stage_checked(accepted, "dev")).warnings == []


async def test_strict_mode_refuses_dev_without_an_accepted_plan(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", rules_mode="strict")
    await svc.start()
    try:
        key = await planned(svc)
        with pytest.raises(ValidationError, match="moving to dev requires an accepted plan"):
            await svc.set_stage(key, "dev")
        await svc.accept_plan(key)
        assert (await svc.set_stage(key, "dev")).stage == "dev"
    finally:
        await svc.close()


async def test_size_is_a_task_field_and_the_first_report_sets_it(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    with pytest.raises(ValidationError, match="size must be one of S, M, L"):
        await service.update_task(task.key, {"size": "XL"})
    task, _ = await service.add_report(task.key, "changed · M · first pass")
    assert task.size == "M"
    task, _ = await service.add_report(task.key, "verified · L · more")
    assert task.size == "M"  # a size already set is the owner's or the agent's choice
    assert (await service.update_task(task.key, {"size": None})).size is None


# --- the agent's todo from hooks -----------------------------------------------------------


async def test_todo_tools_are_stored_even_when_activity_is_coalesced(service: Service) -> None:
    task = await linked_task(service)
    await fire(service, "PostToolUse", tool_name="Bash")  # starts the activity window
    with service.bus.subscribe() as sub:
        await fire(service, "PostToolUse", **todo_create("1", "Write the migration"))
        await fire(service, "PostToolUse", **todo_create("2", "Add the page", "Adding the page"))
        await fire(service, "PostToolUse", **todo_update("2", "in_progress"))
        await fire(service, "PostToolUse", **todo_update("9", "completed"))  # never created
        kinds = [sub.queue.get_nowait().kind for _ in range(sub.queue.qsize())]
    assert kinds.count("session.todo_updated") == 3
    _, plan, todo = await service.plan(task.key)
    assert plan.steps == []
    assert [(i.item_id, i.status, i.active_form) for i in todo] == [
        ("1", "pending", "Write the migration"),
        ("2", "in_progress", "Adding the page"),
    ]
    await fire(service, "PostToolUse", **todo_update("1", "deleted"))
    assert [i.item_id for i in (await service.plan(task.key))[2]] == ["2"]

    # TodoWrite replaces the whole list.
    await fire(service, "PostToolUse", **todo_write(("Only item", "completed")))
    assert [(i.subject, i.status) for i in (await service.plan(task.key))[2]] == [
        ("Only item", "completed")
    ]


async def test_a_new_claude_code_id_starts_a_fresh_list(service: Service) -> None:
    task = await linked_task(service)
    await fire(service, "PostToolUse", **todo_create("1", "Old context"))
    # After /clear the agent links its new id: the old list is not shown any more.
    await service.update_task(task.key, {"branch": "x"}, session=DESKTOP_A, hook_session=CC_B)
    assert (await service.plan(task.key))[2] == []
    await fire(service, "PostToolUse", cc_id=CC_B, **todo_create("1", "New context"))
    assert [i.subject for i in (await service.plan(task.key))[2]] == ["New context"]


async def test_todo_written_before_the_exact_link_moves_with_it(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await fire(service, "PostToolUse", **todo_create("1", "Read the code"))
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session="checkout-agent", hook_session=CC_A
    )
    assert [i.subject for i in (await service.plan(task.key))[2]] == ["Read the code"]


# --- reminders -----------------------------------------------------------------------------


async def test_reminders_carry_the_current_step_and_the_todo_item(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", plan_reminder_every=3)
    await svc.start()
    try:
        task = await linked_task(svc)
        await svc.set_plan(task.key, STEPS)
        await svc.update_plan_step(task.key, 2, "in_progress")
        start = context_of(await fire(svc, "SessionStart"))
        assert "Your relaybay hook session" in start
        assert (
            f"Plan of {task.key} (draft, not accepted yet): step 2/3 'Address book page'"
            " — Add, edit and remove addresses." in start
        )
        # A todo change reminds again, now with the item in progress.
        await fire(svc, "PostToolUse", **todo_create("1", "Build the form", "Building the form"))
        answer = await fire(svc, "PostToolUse", **todo_update("1", "in_progress"))
        assert "Now doing: Building the form." in context_of(answer)
        # Every 3rd tool event repeats it (the todo events counted too); nothing in between.
        answers = [await fire(svc, "PostToolUse", tool_name="Bash") for _ in range(4)]
        assert answers[1:3] == [None, None]
        assert "Now doing: Building the form." in context_of(answers[0])
        assert context_of(answers[3]) == context_of(answers[0])
        # The agent bound to the task does not accept its own plan.
        with pytest.raises(ValidationError, match="does not accept its own plan"):
            await svc.accept_plan(task.key, session=DESKTOP_A)
        await svc.accept_plan(task.key)
        answers = [await fire(svc, "PostToolUse", tool_name="Bash") for _ in range(3)]
        told = f"Owner accepted the plan of {task.key} (3 steps): go on with step 1."
        assert context_of(answers[0]) == told  # the owner's acceptance, once
        assert answers[1] is None and "(accepted)" in context_of(answers[2])
        # A new SessionStart is a new context: the reminder comes again.
        assert "(accepted)" in context_of(await fire(svc, "SessionStart"))
    finally:
        await svc.close()


async def test_reminder_hint_without_plan_and_only_for_exact_sessions(service: Service) -> None:
    task = await linked_task(service)
    start = context_of(await fire(service, "SessionStart"))
    assert f"{task.key} is in dev without a plan: write down what you are going to do" in start
    await fire(service, "PostToolUse", **todo_create("1", "Fix it"))
    answer = await fire(service, "PostToolUse", **todo_update("1", "in_progress"))
    assert "has no plan; your todo (0/1 done) is shown as the agent's plan" in context_of(answer)

    # A session linked by a folder guess (not by the agent) gets no plan text.
    await service.create_task("acme-shop", "Other", stage="dev", session="guessed")
    other = context_of(await fire(service, "SessionStart", cc_id=CC_B))
    assert "relaybay plan reminder" not in other


async def test_reminder_shares_the_answer_with_owner_comments(service: Service) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, STEPS)
    await service.add_owner_comment(task.key, "Use the green button")
    answer = await fire(service, "PostToolUse", **todo_create("1", "Paint it"))
    text = context_of(answer)
    assert "Owner comment on" in text and "relaybay plan reminder:" in text
    assert len(text) < 10_000


# --- MCP -----------------------------------------------------------------------------------


async def test_mcp_plan_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as session:
        await call(session, "project_upsert", slug="acme-shop")
        await call(session, "task_create", project="acme-shop", title="Saved addresses")
        key = "acme-shop-1"
        text = await call(session, "plan_get", task=key)
        assert "has no plan yet" in text
        text = await call(session, "plan_set", task=key, steps=STEPS, reason="from PLAN.md")
        assert "3 step(s), 0 done, draft" in text and "plan_accept" in text
        text = await call(session, "task_set_stage", task=key, stage="dev")
        assert "The plan is not accepted yet" in text
        text = await call(session, "plan_accept", task=key, session="coordinator")
        assert "accepted: 3 step(s)" in text
        text = await call(session, "plan_accept", task=key)
        assert "already accepted" in text
        text = await call(
            session, "plan_step_update", task=key, step=1, status="done", conformance="as planned"
        )
        assert "Step 1 'Migration and model' is done (as planned)." in text
        text = await call(
            session,
            "plan_step_update",
            task=key,
            step=2,
            status="done",
            conformance="deviation: no editing yet",
            link="https://git.example.com/acme-shop/pull/7",
        )
        assert "deviation: no editing yet" in text
        await call(session, "plan_step_update", task=key, step=3, status="in_progress")
        text = await call(session, "plan_get", task=key)
        assert "Status: accepted by coordinator" in text and "2/3 done, 1 deviation(s)" in text
        assert "3. [in_progress] Picker at checkout" in text
        text = await call(session, "task_update", task=key, size="M")
        assert "Updated" in text
        text = await call(session, "project_snapshot", project="acme-shop")
        assert "size M" in text and "plan 2/3, 1 deviation(s) · now: 3 Picker at checkout" in text
        text = await call(session, "task_get", task=key)
        assert "- size: M" in text and "- plan: plan 2/3" in text
        text = await call(session, "rules_get")
        assert "moving to dev needs an accepted plan (not needed for size S)" in text
        events = await call(session, "events_since", since_id=0)
        assert "plan.accepted" in events and "plan.step_updated" in events


# --- board ---------------------------------------------------------------------------------


def board_plan(board: TestClient) -> str:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Saved addresses", stage="dev")
    run_in_app(board, service.set_plan, task.key, STEPS)
    run_in_app(board, service.update_plan_step, task.key, 1, "done", conformance="as planned")
    run_in_app(
        board,
        service.update_plan_step,
        task.key,
        2,
        "done",
        conformance="deviation: no editing yet",
    )
    run_in_app(board, service.update_plan_step, task.key, 3, "in_progress")
    return task.key


def test_board_card_and_plan_tab(board: TestClient) -> None:
    key = board_plan(board)
    card = board.get(f"/p/acme-shop/t/{key}/card").text
    assert "Plan 2/3 · draft" in card and "1 deviation" in card
    assert "now: Picker at checkout" in card and 'data-tab="plan"' in card
    dialog = board.get(f"/p/acme-shop/t/{key}/dialog?tab=plan").text
    assert 'data-tab="plan"' in dialog and 'id="panel-plan-' in dialog
    assert 'id="plan-' in dialog and "Accept plan" in dialog
    assert "deviation: no editing yet" in dialog and "is-deviation" in dialog
    assert "History (3)" not in dialog and "History (4)" in dialog
    page = board.get(f"/t/{key}").text
    assert "<h2>Plan</h2>" in page and "Picker at checkout" in page
    board_page = board.get(f"/p/acme-shop?task={key}&tab=plan").text
    assert 'aria-selected="true" data-tab="plan"' in board_page


def test_board_accept_needs_cookie_and_csrf(board: TestClient, token: str) -> None:
    key = board_plan(board)
    url = f"/p/acme-shop/t/{key}/plan/accept"
    assert board.post(url, data={}, headers=HTMX).status_code == 403
    bearer = {"Authorization": f"Bearer {token}", **HTMX}
    assert (
        board.post(url, data={"csrf_token": csrf_token(token)}, headers=bearer).status_code == 403
    )
    response = board.post(url, data={"csrf_token": csrf_token(token)}, headers=HTMX)
    assert response.status_code == 200 and "Accepted" in response.text
    assert "Accept plan" not in response.text and "accepted by owner" in response.text
    service: Service = board.app.state.service
    assert run_in_app(board, service.get_task, key).plan_status == "accepted"


def test_board_shows_the_agent_todo_when_there_is_no_plan(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, linked_task, service)
    run_in_app(board, service.record_hook, post(**todo_create("1", "Fix it", "Fixing it")))
    run_in_app(board, service.record_hook, post(**todo_update("1", "in_progress")))
    panel = board.get(f"/p/acme-shop/t/{task.key}/plan").text
    assert "Agent&#39;s plan, not accepted" in panel or "Agent's plan, not accepted" in panel
    assert "Now doing:</span> Fixing it" in panel and "Accept plan" not in panel


def test_board_form_sets_the_size(board: TestClient, seeded: str, token: str) -> None:
    form = board.get(f"/p/acme-shop/t/{seeded}/form").text
    assert '<select name="size">' in form and '<option value="S" selected>' in form
    data = {"title": "Checkout page", "size": "L", "version": "2", "csrf_token": csrf_token(token)}
    board.post(f"/p/acme-shop/t/{seeded}", data=data, headers=HTMX)
    service: Service = board.app.state.service
    assert run_in_app(board, service.get_task, seeded).size == "L"


# --- migration -----------------------------------------------------------------------------


async def test_upgrade_from_0012_keeps_tasks_and_adds_the_plan(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=12)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO session (name, cc_session_id, cc_exact, last_seen_at, created_at)"
            " VALUES ('acme-agent', ?, 1, ?, ?)",
            (CC_A, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO report (task_id, author, summary, body_md, created_at)"
            " VALUES (1, 'agent', 'first', 'first', ?)",
            (NOW,),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.get_task("acme-shop-1")
        assert (task.title, task.stage, task.size, task.plan_status) == (
            "Checkout page",
            "dev",
            None,
            None,
        )
        assert (await svc.task_detail(task.key)).reports[0].summary == "first"
        _, plan, _ = await svc.set_plan(task.key, STEPS)
        assert plan.status == "draft" and len(plan.steps) == 3
        assert (await svc.update_task(task.key, {"size": "S"})).size == "S"
    finally:
        await svc.close()


# --- the owner's acceptance told through hooks ------------------------------------------------


async def test_owner_acceptance_is_told_to_the_bound_session_once(service: Service) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, STEPS, session=DESKTOP_A, hook_session=CC_A)
    await service.accept_plan(task.key, actor="owner")  # the board's call
    line = f"Owner accepted the plan of {task.key} (3 steps): go on with step 1."
    assert line in context_of(await fire(service, "PreToolUse", tool_name="Bash"))
    for event in ("PostToolUse", "PreToolUse", "UserPromptSubmit"):
        answer = await fire(service, event, tool_name="Bash")
        assert answer is None or line not in context_of(answer)  # told once
    events = await service.events_since(0, limit=500)
    assert [e.kind for e in events].count("plan.acceptance_delivered") == 1


async def test_acceptance_over_mcp_or_before_the_binding_is_not_told(service: Service) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, STEPS, session=DESKTOP_A, hook_session=CC_A)
    await service.accept_plan(task.key, actor="mcp", session="coordinator")
    answer = await fire(service, "PostToolUse", tool_name="Bash")
    assert answer is None or "Owner accepted" not in context_of(answer)

    # Accepted on the board before this session was bound: old news for it.
    other = await service.create_task("acme-shop", "Payment", stage="dev")
    await service.set_plan(other.key, STEPS[:1])
    await service.accept_plan(other.key, actor="owner")
    await service.bind_session(other.key, DESKTOP_A, hook_session=CC_A)
    answer = await fire(service, "PostToolUse", tool_name="Bash")
    assert answer is None or "Owner accepted" not in context_of(answer)


async def test_acceptance_names_the_first_step_not_done_and_old_ones_are_not_told(
    service: Service,
) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, STEPS, session=DESKTOP_A, hook_session=CC_A)
    for position in (1, 2):
        await service.update_plan_step(task.key, position, "done", conformance="as planned")
    await service.update_plan_step(task.key, 3, "in_progress")
    await service.accept_plan(task.key, actor="owner")
    told = context_of(await fire(service, "PostToolUse", tool_name="Bash"))
    assert f"Owner accepted the plan of {task.key} (3 steps): go on with step 3." in told

    done = await service.create_task(
        "acme-shop", "Payment", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await service.set_plan(done.key, STEPS[:1], session=DESKTOP_A, hook_session=CC_A)
    await service.update_plan_step(done.key, 1, "done", conformance="as planned")
    await service.accept_plan(done.key, actor="owner")
    told = context_of(await fire(service, "PostToolUse", tool_name="Bash"))
    assert f"Owner accepted the plan of {done.key} (1 step): all steps are done." in told

    # Accepted three hours ago (or before an upgrade that brought this delivery): stale news.
    old = await service.create_task(
        "acme-shop", "Refunds", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await service.set_plan(old.key, STEPS, session=DESKTOP_A, hook_session=CC_A)
    await service.accept_plan(old.key, actor="owner")
    async with service._tx() as tx:
        await tx.conn.execute(
            "UPDATE task SET plan_accepted_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="milliseconds"), old.id),
        )
        await tx.conn.execute("UPDATE task_session SET bound_at = '2020-01-01T00:00:00.000+00:00'")
    answer = await fire(service, "PostToolUse", tool_name="Bash")
    assert answer is None or "Owner accepted" not in context_of(answer)
