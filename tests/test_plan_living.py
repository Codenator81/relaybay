"""The living plan: plan_step_add keeps the acceptance, the stage follows the plan, the
closed-plan warning, the reminder in that state, the draft hint for the coordinator, the MCP
tool, the board and the migration of plan steps."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.domain.errors import ValidationError
from relaybay.domain.plan import Plan, reminder_line
from relaybay.service import Service, collect_notes
from tests.conftest import LiveServer, run_in_app
from tests.test_comments import context_of, fire, linked_task
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import NOW, make_old_db
from tests.test_plan import STEPS, planned

CLOSED = (
    "Warning (rules): the plan is closed (3/3 done) while work goes on: add a step with"
    " plan_step_add or reopen one."
)


async def all_done(svc: Service, key: str) -> None:
    for n in (1, 2, 3):
        await svc.update_plan_step(key, n, "done", conformance="as planned")


async def age_plan(svc: Service, key: str, minutes: int) -> None:
    """Move every plan revision of `key` back in time (the clock seen by the draft hint)."""
    when = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    task = await svc.get_task(key)
    async with svc._tx() as tx:
        await tx.conn.execute(
            "UPDATE plan_revision SET created_at = ? WHERE task_id = ?", (when, task.id)
        )


# --- plan_step_add -------------------------------------------------------------------------


async def test_plan_step_add_keeps_acceptance_and_marks_the_step(service: Service) -> None:
    key = await planned(service)
    await service.accept_plan(key, actor="owner")
    with pytest.raises(ValidationError, match="needs a reason"):
        await service.add_plan_step(key, "Undo button")
    with pytest.raises(ValidationError, match="needs a reason"):
        await service.add_plan_step(key, "Undo button", reason="   ")
    with service.bus.subscribe() as sub:
        task, step = await service.add_plan_step(
            key,
            "Undo button",
            details="Owner asked for it in review.",
            reason="owner's decision after review",
            session="addresses-agent",
        )
        kinds = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert task.plan_status == "accepted" and task.plan_total == 4
    assert (step.position, step.status, step.added_by, step.added_reason) == (
        4,
        "pending",
        "addresses-agent",
        "owner's decision after review",
    )
    assert step.added_after_acceptance_at is not None
    added = next(e for e in kinds if e.kind == "plan.step_added")
    assert added.payload["after_acceptance"] is True and added.payload["position"] == 4
    assert added.payload["reason"] == "owner's decision after review"
    assert "task.stage_changed" not in [e.kind for e in kinds]  # plan stage: no move

    _, history = await service.plan_history(key)
    newest, diff = history[0]
    assert newest.reason == "step 4 added after acceptance: owner's decision after review"
    assert diff == ["+ 4. Undo button (added after acceptance)"]
    assert newest.body["status"] == "accepted"

    # plan_set keeps its rule: a renamed step makes the plan a draft (and drops the mark).
    titles = [*STEPS, {"title": "Undo button v2"}]
    _, plan, reverted = await service.set_plan(key, titles)
    assert reverted and plan.status == "draft"
    assert plan.steps[3].added_after_acceptance_at is None


async def test_plan_step_add_on_a_draft_or_no_plan_has_no_mark(service: Service) -> None:
    key = await planned(service)
    _, step = await service.add_plan_step(key, "Undo button", reason="forgot it")
    assert step.added_after_acceptance_at is None
    assert (await service.get_task(key)).plan_status == "draft"
    empty = await service.create_task("acme-shop", "Empty")
    task, step = await service.add_plan_step(empty.key, "First step", reason="start")
    assert (task.plan_status, step.position) == ("draft", 1)


# --- the stage follows the plan ------------------------------------------------------------


async def test_plan_step_add_on_review_moves_the_task_back_to_dev(service: Service) -> None:
    key = await planned(service, stage="review")
    await service.accept_plan(key)
    with collect_notes() as notes, service.bus.subscribe() as sub:
        task, _ = await service.add_plan_step(key, "Undo button", reason="owner asked")
        events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    assert (task.stage, task.stage_note) == ("dev", "rework: owner asked")
    moved = next(e for e in events if e.kind == "task.stage_changed")
    assert (moved.payload["from"], moved.payload["to"]) == ("review", "dev")
    assert notes == [f"{key} moved from review back to dev (rework: owner asked)."]


async def test_rework_puts_the_card_on_top_of_dev(service: Service) -> None:
    key = await planned(service, stage="review")
    for title in ("Payment", "Refunds"):
        await service.create_task("acme-shop", title, stage="dev")
    await service.add_plan_step(key, "Undo button", reason="owner asked")
    dev = await service.list_tasks(project="acme-shop", stage="dev", by_rank=True)
    assert len(dev) == 3 and dev[0].key == key


async def test_reopening_a_done_step_on_test_moves_the_task_back_to_dev(
    service: Service,
) -> None:
    key = await planned(service, stage="test")
    await all_done(service, key)
    with collect_notes() as notes:
        task, step = await service.update_plan_step(key, 2, "in_progress")
    task = await service.get_task(key)
    assert step.status == "in_progress"
    assert (task.stage, task.stage_note) == ("dev", "rework: Address book page")
    assert notes == [f"{key} moved from test back to dev (rework: Address book page)."]

    # In dev it stays in dev; reopening from pending does not count either.
    await service.update_plan_step(key, 2, "done", conformance="as planned")
    with collect_notes() as notes:
        await service.update_plan_step(key, 2, "in_progress")
    assert (await service.get_task(key)).stage == "dev" and notes == []


async def test_all_done_hint_needs_dev_and_review_steps(service: Service) -> None:
    key = await planned(service, stage="dev")
    for n in (1, 2):
        await service.update_plan_step(key, n, "done", conformance="as planned")
    with collect_notes() as notes:
        await service.update_plan_step(key, 3, "done", conformance="as planned")
    assert notes == []  # no review checklist yet

    await service.update_plan_step(key, 3, "in_progress")
    await service.set_review_steps(key, [{"title": "Addresses page", "details": "Add one."}])
    with collect_notes() as notes:
        await service.update_plan_step(key, 3, "done", conformance="as planned")
    assert notes == [
        "Hint: all steps done and the review checklist is set: move the task to review"
        " (task_set_stage)."
    ]
    assert (await service.get_task(key)).stage == "dev"  # never moved automatically


# --- closed-plan warning -------------------------------------------------------------------


async def test_closed_plan_warning_on_reports_and_progress(service: Service) -> None:
    key = await planned(service, stage="dev")
    for n in (1, 2):
        await service.update_plan_step(key, n, "done", conformance="as planned")
    with collect_notes() as notes:
        await service.add_report(key, "changed · M · step 3 still open")
        await service.update_task(key, {"progress": "Two steps"})
    assert notes == []  # a step is still open

    await service.update_plan_step(key, 3, "done", conformance="as planned")
    with collect_notes() as notes:
        await service.update_task(key, {"progress": "All three steps"})
    assert notes == []  # progress right after closing is the closing itself
    with collect_notes() as notes:
        await service.add_report(key, "verified · M · all three steps")
    assert notes == []  # the report that closes the plan never warns
    for state, warned in (
        ("changed", True),
        ("verified", True),
        ("inspected", False),
        ("blocked", False),
    ):
        with collect_notes() as notes:
            await service.add_report(key, f"{state} · M · more work")
        assert (notes == [CLOSED]) is warned, state
    with collect_notes() as notes:
        saved = await service.update_task_checked(key, {"progress": "Rework of the form"})
    assert notes == [CLOSED] and saved.warnings == []
    with collect_notes() as notes:
        await service.update_task(key, {"next_step": "Ask the owner"})
    assert notes == []  # not a progress change

    no_plan = await service.create_task("acme-shop", "No plan", stage="dev")
    with collect_notes() as notes:
        await service.add_report(no_plan.key, "changed · S · fix")
        await service.add_report(no_plan.key, "changed · S · fix again")
        await service.update_task(no_plan.key, {"progress": "Fixed"})
    assert notes == []


async def test_closed_plan_warning_outside_dev(service: Service) -> None:
    review = await planned(service, stage="review")
    await all_done(service, review)
    await service.add_report(review, "verified · M · ready for review")
    with collect_notes() as notes:
        await service.add_report(review, "changed · M · fixed after the owner's comment")
    assert notes == [CLOSED]  # code changed after the plan closed (any open stage)
    with collect_notes() as notes:
        await service.update_task(review, {"progress": "Fixed the comment"})
    assert notes == []  # progress only warns in dev

    test = await planned(service, stage="test")
    await all_done(service, test)
    await service.add_report(test, "verified · M · on test")
    with collect_notes() as notes:
        await service.add_report(test, "verified · M · checked again on test")
    assert notes == []  # verifying in test is not new work


async def test_a_plan_closed_by_plan_set_counts_from_that_write(service: Service) -> None:
    key = await planned(service, stage="dev")
    await service.add_report(key, "changed · M · step 1")
    for n in (1, 2):
        await service.update_plan_step(key, n, "done", conformance="as planned")
    await service.add_report(key, "changed · M · step 2")
    await service.set_plan(key, STEPS[:2], reason="step 3 is not needed")  # closes the plan
    with collect_notes() as notes:
        await service.add_report(key, "changed · M · both steps done")
    assert notes == []  # the closing report, although reports exist after the last done step
    with collect_notes() as notes:
        await service.add_report(key, "changed · M · more work")
    assert notes == [CLOSED.replace("3/3", "2/2")]


async def test_a_report_in_the_same_millisecond_is_not_after(service: Service) -> None:
    key = await planned(service, stage="dev")
    _, report = await service.add_report(key, "changed · M · first")
    task = await service.get_task(key)
    assert not await repo.has_report_after(service._r, task.id, report.created_at)
    assert await repo.has_report_after(service._r, task.id, "2000-01-01T00:00:00.000+00:00")


async def test_closed_plan_is_a_warning_in_strict_mode_too(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", rules_mode="strict")
    await svc.start()
    try:
        key = await planned(svc)
        await all_done(svc, key)
        await svc.add_report(key, "verified · M · all done")
        with collect_notes() as notes:
            _, report = await svc.add_report(key, "changed · M · more work")
        assert report.id and notes == [CLOSED]
    finally:
        await svc.close()


async def test_mcp_task_update_and_patch_carry_the_closed_plan_warning(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with mcp_connect(live) as session:
        await call(session, "project_upsert", slug="acme-shop")
        await call(
            session, "task_create", project="acme-shop", title="Saved addresses", stage="dev"
        )
        key = "acme-shop-1"
        await call(session, "plan_set", task=key, steps=STEPS)
        for n in (1, 2, 3):
            await call(
                session,
                "plan_step_update",
                task=key,
                step=n,
                status="done",
                conformance="as planned",
            )
        assert CLOSED not in await call(session, "report_add", task=key, body="verified · M · done")
        assert CLOSED in await call(session, "task_update", task=key, progress="More")
        assert CLOSED in await call(session, "task_patch", task=key, fields={"progress": "Again"})
        assert CLOSED in await call(session, "report_add", task=key, body="verified · M · again")


def test_reminder_says_the_plan_is_closed_only_in_dev() -> None:
    from relaybay.domain.plan import PlanStep

    steps = [
        PlanStep(n, 1, n, f"Step {n}", None, "done", NOW, None, None, "as planned", "a", NOW, NOW)
        for n in (1, 2)
    ]
    plan = Plan("accepted", NOW, "owner", steps)
    assert reminder_line("acme-shop-1", "dev", plan, []) == (
        "Plan of acme-shop-1 (accepted): the plan is closed (2/2 done) while work goes on: add a"
        " step with plan_step_add or reopen one."
    )
    assert reminder_line("acme-shop-1", "review", plan, []) == (
        "Plan of acme-shop-1 (accepted): all 2 steps done."
    )


async def test_session_start_says_the_plan_is_closed(service: Service) -> None:
    task = await linked_task(service)
    await service.set_plan(task.key, STEPS)
    await all_done(service, task.key)
    start = context_of(await fire(service, "SessionStart"))
    assert "the plan is closed (3/3 done) while work goes on" in start
    assert "step 3/3" not in start


# --- draft hint ----------------------------------------------------------------------------


async def test_draft_hint_for_the_coordinator_after_an_hour(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", coordinators=["coordinator"])
    await svc.start()
    try:
        key = await planned(svc, stage="dev")
        hint = f"Hint: {key}'s plan is a draft for 1 h: accept it (plan_accept) or ask for changes."
        await age_plan(svc, key, 59)
        assert await svc.plan_draft_hints() == []
        assert svc.plan_draft_hours(await svc.get_task(key)) is None
        await age_plan(svc, key, 61)
        assert await svc.plan_draft_hints() == [hint]
        assert svc.plan_draft_hours(await svc.get_task(key)) == 1
        assert hint in await svc.portfolio_digest(session="coordinator")
        assert hint in await svc.project_digest("acme-shop", session="coordinator")
        assert hint not in await svc.portfolio_digest()
        assert hint not in await svc.project_digest("acme-shop")
        assert hint not in await svc.project_digest("acme-shop", session="addresses-agent")
        assert hint in await svc.task_context(key, session="coordinator")
        assert hint not in await svc.task_context(key, session="addresses-agent")
        assert hint not in await svc.task_context(key)

        # Not in another stage, not once accepted.
        other = await planned(svc, stage="plan")
        await age_plan(svc, other, 300)
        assert await svc.plan_draft_hints("acme-shop") == [hint]
        await svc.accept_plan(key)
        assert await svc.plan_draft_hints() == []
    finally:
        await svc.close()


async def test_draft_hints_are_capped(service: Service) -> None:
    keys = [await planned(service, stage="dev") for _ in range(7)]
    for minutes, key in zip(range(700, 0, -100), keys, strict=True):
        await age_plan(service, key, minutes)  # the first key is the oldest draft
    hints = await service.plan_draft_hints()
    assert len(hints) == 6 and hints[-1] == "…and 2 more drafts."
    assert hints[0].startswith(f"Hint: {keys[0]}'s plan is a draft for 11 h")
    assert all(k in "\n".join(hints) for k in keys[:5])
    assert await service.plan_draft_hints("acme-shop") == hints


async def test_draft_hint_off_with_zero(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", plan_draft_hint_hours=0)
    await svc.start()
    try:
        key = await planned(svc, stage="dev")
        await age_plan(svc, key, 600)
        assert await svc.plan_draft_hints() == []
        assert svc.plan_draft_hours(await svc.get_task(key)) is None
    finally:
        await svc.close()


async def test_plan_step_add_refused_on_done_and_says_so_on_live(service: Service) -> None:
    done = await planned(service, stage="done")
    with pytest.raises(ValidationError, match="the task is done: reopen it"):
        await service.add_plan_step(done, "Late step", reason="after the fact")
    assert (await service.get_task(done)).plan_total == 3
    live = await planned(service, stage="live")
    with collect_notes() as notes:
        task, step = await service.add_plan_step(live, "Hotfix", reason="owner asked")
    assert task.stage == "live" and step.position == 4
    assert notes == [f"{live} is in live: the step does not move it."]


# --- MCP and board -------------------------------------------------------------------------


async def test_mcp_plan_step_add(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as session:
        await call(session, "project_upsert", slug="acme-shop")
        await call(session, "task_create", project="acme-shop", title="Saved addresses")
        key = "acme-shop-1"
        await call(session, "plan_set", task=key, steps=STEPS)
        await call(session, "plan_accept", task=key, session="coordinator")
        await call(session, "task_set_stage", task=key, stage="review")
        text = await call(
            session, "plan_step_add", task=key, title="Undo button", reason="owner asked"
        )
        assert "Step 4 'Undo button' added to the plan of **acme-shop-1** (pending)" in text
        assert "the plan stays accepted" in text
        assert f"{key} moved from review back to dev (rework: owner asked)." in text
        text = await call(session, "plan_get", task=key)
        assert "4. [pending] Undo button (added after acceptance by" in text
        assert "Status: accepted" in text
        events = await call(session, "events_since", since_id=0)
        assert "plan.step_added" in events
        snapshot = await call(session, "project_snapshot", project="acme-shop")
        assert "plan 0/4" in snapshot and "note: rework: owner asked" in snapshot


def test_board_shows_the_mark_and_the_draft_age(board: TestClient) -> None:
    service: Service = board.app.state.service
    key = run_in_app(board, planned, service, "dev")
    run_in_app(board, age_plan, service, key, 150)
    card = board.get(f"/p/acme-shop/t/{key}/card").text
    assert "Plan 0/3 · draft · 2 h" in card

    run_in_app(board, service.accept_plan, key)
    run_in_app(board, service.add_plan_step, key, "Undo button", reason="owner asked")
    panel = board.get(f"/p/acme-shop/t/{key}/plan").text
    assert "Added after acceptance by agent" in panel and "owner asked" in panel
    assert "+ 4. Undo button (added after acceptance)" in panel
    card = board.get(f"/p/acme-shop/t/{key}/card").text
    assert "Plan 0/4" in card and "· draft" not in card


# --- migration -----------------------------------------------------------------------------


async def test_upgrade_keeps_existing_plans_and_revisions(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=15)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO plan_step (task_id, position, title, status, conformance, created_by,"
            " created_at, updated_at) VALUES (1, 1, 'Old step', 'done', 'as planned', 'agent',"
            " ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO plan_revision (task_id, revision, body, author, reason, created_at)"
            ' VALUES (1, 1, \'{"status": "accepted", "steps": [{"title": "Old step"}]}\','
            " 'agent', 'from PLAN.md', ?)",
            (NOW,),
        )
        await conn.execute("UPDATE task SET plan_status = 'accepted' WHERE id = 1")
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        task, plan, _ = await svc.plan("acme-shop-1")
        assert (task.plan_status, task.plan_revised_at) == ("accepted", NOW)
        step = plan.steps[0]
        assert (step.title, step.status, step.added_after_acceptance_at) == (
            "Old step",
            "done",
            None,
        )
        _, history = await svc.plan_history(task.key)
        assert [(r.reason, d) for r, d in history] == [("from PLAN.md", ["+ 1. Old step"])]
        _, added = await svc.add_plan_step(task.key, "New step", reason="rework")
        assert added.added_after_acceptance_at is not None
    finally:
        await svc.close()


def test_plan_head_segments_carry_their_own_separator(board: TestClient) -> None:
    service: Service = board.app.state.service
    key = run_in_app(board, planned, service, "dev")
    for n in (1, 2):
        run_in_app(board, service.update_plan_step, key, n, "done", conformance="deviation: x")
    run_in_app(board, service.accept_plan, key, actor="owner")
    panel = board.get(f"/p/acme-shop/t/{key}/plan").text
    assert '<span>2 of 3 done</span><span class="deviation-count"> · 2 deviations</span>' in panel
    assert "<span> · accepted by owner <time" in panel
    head = panel.split('class="plan-head"', 1)[1].split("</div>", 1)[0]
    assert "</span> · <" not in head and " · · " not in head
    draft = run_in_app(board, planned, service, "dev")
    panel = board.get(f"/p/acme-shop/t/{draft}/plan").text
    assert "<span>0 of 3 done</span></span>" in panel
