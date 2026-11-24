"""Coordinator mode (relaybay-40): with `coordinators` set, an agent (a session bound to an open
card that is not a coordinator) creates no cards and no backlog items, leaves the tracker mark to
the coordinator, and its `waiting_on` names one of the fixed roles. With `coordinators` empty
every text and behaviour is as before."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from relaybay.domain.errors import ValidationError
from relaybay.domain.rules import DEFAULT_RULES, Rules
from relaybay.domain.waiting_on import normalise
from relaybay.mcp_server import roles
from relaybay.mcp_server.server import build_mcp, current_tools, instructions
from relaybay.service import Service, collect_notes
from tests.conftest import LiveServer
from tests.test_mcp_roles import GOLDEN, all_tools, connect, named

REFUSED = "In coordinator mode new cards and backlog items come from the coordinator"
TRACKER_NOTE = "Tracker mark: the coordinator's (it sets tracker_logged after the work log)."
TRACKER_WARNING = "the work log entered in the tracker (tracker_logged)"
ARCHIVE_REFUSED = "Set tracker_logged first (the work log in the tracker), then archive."
HOOK_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OWNER_RU = "\u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446"


async def started(
    path: Path,
    *,
    coordinators: list[str] | None = None,
    require_tracker_log: bool = False,
    rules_mode: str = "warn",
) -> Service:
    service = Service(
        path / "relaybay.sqlite",
        coordinators=coordinators,
        require_tracker_log=require_tracker_log,
        rules_mode=rules_mode,
    )
    await service.start()
    await service.upsert_project("acme-shop")
    return service


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = await started(tmp_path, coordinators=["lead"], require_tracker_log=True)
    try:
        yield service
    finally:
        await service.close()


@pytest.fixture
async def plain(tmp_path: Path) -> AsyncIterator[Service]:
    service = await started(tmp_path, require_tracker_log=True)
    try:
        yield service
    finally:
        await service.close()


async def agent_card(
    service: Service, *, tracker_ref: str | None = None, stage: str | None = None
) -> str:
    """A card the coordinator created and the session `worker` is bound to."""
    task = await service.create_task(
        "acme-shop", "Checkout", session="lead", bind=False, tracker_ref=tracker_ref, stage=stage
    )
    await service.bind_session(task.key, "worker")
    return task.key


# --- who is an agent: task_create and backlog_add ---


async def test_a_bound_agent_creates_no_card_and_no_backlog_item(svc: Service) -> None:
    key = await agent_card(svc)
    with pytest.raises(ValidationError) as card:
        await svc.create_task("acme-shop", "Side quest", session="worker")
    assert str(card.value) == (
        f"In coordinator mode new cards and backlog items come from the coordinator: put this in"
        f" report_add(task={key}, findings=[...]) (a question for the owner: questions=[...])."
        " Nothing created."
    )
    with pytest.raises(ValidationError, match=REFUSED):
        await svc.create_task("acme-shop", "For someone", session="worker", bind=False)
    with pytest.raises(ValidationError, match=REFUSED):
        await svc.add_backlog_item("acme-shop", "Flaky test", session="worker", task=key)
    assert [t.key for t in (await svc.snapshot("acme-shop")).tasks] == [key]
    assert await svc.list_backlog(project="acme-shop") == []


async def test_the_hook_session_alone_names_the_agent(svc: Service) -> None:
    key = await agent_card(svc)
    await svc.update_task(key, {"progress": "Started"}, session="worker", hook_session=HOOK_ID)
    with pytest.raises(ValidationError, match=REFUSED):
        await svc.create_task("acme-shop", "Side quest", hook_session=HOOK_ID)


async def test_the_coordinator_an_unbound_session_and_the_owner_still_create(
    svc: Service,
) -> None:
    await agent_card(svc)
    await svc.create_task("acme-shop", "Bound to the lead", session="lead")
    await svc.create_task("acme-shop", "Next one", session="lead")  # bound, still a coordinator
    await svc.add_backlog_item("acme-shop", "Lead finding", session="lead")
    await svc.create_task("acme-shop", "Stranger", session="stranger", bind=False)
    await svc.add_backlog_item("acme-shop", "Stranger finding", session="nobody-yet")
    await svc.create_task("acme-shop", "From the board", actor="board")
    await svc.add_backlog_item("acme-shop", "Owner note", by_owner=True)
    assert len(await svc.list_backlog(project="acme-shop")) == 3


async def test_without_coordinators_an_agent_creates_as_before(plain: Service) -> None:
    task = await plain.create_task("acme-shop", "Checkout", session="worker")
    await plain.create_task("acme-shop", "Side quest", session="worker")
    await plain.add_backlog_item("acme-shop", "Flaky test", session="worker", task=task.key)
    assert len(await plain.list_backlog(project="acme-shop")) == 1


# --- the tracker mark ---


async def test_an_agents_done_leaves_the_mark_to_the_coordinator(svc: Service) -> None:
    key = await agent_card(svc, tracker_ref="ENG-1")
    with collect_notes() as notes:
        saved = await svc.update_task_checked(
            key, {"stage": "done", "progress": "All done"}, session="worker"
        )
    assert saved.warnings == []
    assert notes.count(TRACKER_NOTE) == 1
    assert not saved.task.tracker_logged


async def test_strict_mode_does_not_refuse_an_agents_done(tmp_path: Path) -> None:
    svc = await started(
        tmp_path, coordinators=["lead"], require_tracker_log=True, rules_mode="strict"
    )
    try:
        key = await agent_card(svc, tracker_ref="ENG-1")
        with collect_notes() as notes:
            saved = await svc.update_task_checked(
                key, {"stage": "done", "progress": "All done"}, session="worker"
            )
        assert saved.task.stage.value == "done" and TRACKER_NOTE in notes
        other = await svc.create_task("acme-shop", "Lead's", session="lead", tracker_ref="E-2")
        with pytest.raises(ValidationError, match="tracker_logged"):
            await svc.update_task_checked(
                other.key, {"stage": "done", "progress": "All done"}, session="lead"
            )
    finally:
        await svc.close()


async def test_the_coordinators_and_the_boards_done_keep_the_warning(svc: Service) -> None:
    key = await agent_card(svc, tracker_ref="ENG-1")
    with collect_notes() as notes:
        by_lead = await svc.update_task_checked(
            key, {"stage": "done", "progress": "All done"}, session="lead"
        )
    assert any(TRACKER_WARNING in w for w in by_lead.warnings) and TRACKER_NOTE not in notes
    await svc.update_task_checked(key, {"stage": "dev"}, actor="board")
    by_board = await svc.update_task_checked(
        key, {"stage": "done", "progress": "All done"}, actor="board"
    )
    assert any(TRACKER_WARNING in w for w in by_board.warnings)


async def test_without_coordinators_an_agents_done_warns_as_before(plain: Service) -> None:
    task = await plain.create_task("acme-shop", "Checkout", session="worker", tracker_ref="E-1")
    with collect_notes() as notes:
        saved = await plain.update_task_checked(
            task.key, {"stage": "done", "progress": "All done"}, session="worker"
        )
    assert saved.warnings == [
        "Warning (rules): moving to done usually needs the work log entered in the tracker"
        " (tracker_logged)."
    ]
    assert TRACKER_NOTE not in notes


async def test_archive_waits_for_the_tracker_mark(svc: Service) -> None:
    key = await agent_card(svc, tracker_ref="ENG-1", stage="done")
    with pytest.raises(ValidationError) as refused:
        await svc.set_archived_mcp(key, True, session="lead")
    assert str(refused.value) == ARCHIVE_REFUSED
    assert (await svc.get_task(key)).archived_at is None
    await svc.update_task(key, {"tracker_logged": True}, session="lead")
    assert (await svc.set_archived_mcp(key, True, session="lead")).archived_at is not None


async def test_archive_of_a_card_without_a_tracker_needs_no_mark(tmp_path: Path) -> None:
    svc = await started(tmp_path, coordinators=["lead"], require_tracker_log=True)
    try:
        task = await svc.create_task("acme-shop", "No tracker", stage="done", session="lead")
        assert (await svc.set_archived_mcp(task.key, True, session="lead")).archived_at
    finally:
        await svc.close()


async def test_archive_without_the_requirement_needs_no_mark(tmp_path: Path) -> None:
    svc = await started(tmp_path, coordinators=["lead"])
    try:
        task = await svc.create_task("acme-shop", "Logged later", stage="done", tracker_ref="E")
        assert (await svc.set_archived_mcp(task.key, True, session="lead")).archived_at
    finally:
        await svc.close()


# --- waiting_on ---

OWNERS = ["Alex", "owner", "me"]
LEADS = ["lead", "Lead Session"]


@pytest.mark.parametrize(
    ("given", "who"),
    [
        ("owner", "Alex"),
        ("OWNER", "Alex"),
        ("me", "Alex"),
        ("alex", "Alex"),
        (OWNER_RU, "Alex"),
        (OWNER_RU.upper(), "Alex"),
        ("master", "coordinator"),
        ("Coordinator", "coordinator"),
        ("lead", "coordinator"),
        ("lead session", "coordinator"),
        ("agent", "session"),
        ("developer", "session"),
        ("Reviewer", "session"),
        ("dev", "session"),
        ("SESSION", "session"),
        ("CI", "ci"),
        ("Customer: Acme", "customer:Acme"),
    ],
)
def test_waiting_on_synonyms(given: str, who: str) -> None:
    saved, moved, note = normalise(given, owner_names=OWNERS, coordinator_names=LEADS)
    assert (saved, moved, note) == (who, None, f'waiting_on: "{given}" saved as "{who}"')


@pytest.mark.parametrize(
    "given", ["Alex", "coordinator", "session", "ci", "customer:acme", "Bob", "Bob: review it"]
)
def test_waiting_on_kept_as_written(given: str) -> None:
    assert normalise(given, owner_names=OWNERS, coordinator_names=LEADS) == (given, None, None)


@pytest.mark.parametrize(
    ("given", "who", "rest"),
    [
        ("owner: merge the PR", "Alex", "merge the PR"),
        ("dev (fixing the tests)", "session", "fixing the tests"),
        ("ci \u2014 the macOS runner", "ci", "the macOS runner"),
        ("customer:acme (the invoice)", "customer:acme", "the invoice"),
        (f"{OWNER_RU}: approve", "Alex", "approve"),
    ],
)
def test_waiting_on_text_after_the_who(given: str, who: str, rest: str) -> None:
    saved, moved, note = normalise(given, owner_names=OWNERS, coordinator_names=LEADS)
    assert (saved, moved) == (who, rest)
    assert note == f'waiting_on: "{given}" saved as "{who}" \u2014 the rest went to next_step'
    kept = normalise(given, owner_names=OWNERS, coordinator_names=LEADS, keep_rest=False)
    assert kept[:2] == (who, None) and "the rest was not kept" in (kept[2] or "")


def test_waiting_on_empty_is_left_alone() -> None:
    for value in (None, ""):
        assert normalise(value, owner_names=OWNERS, coordinator_names=LEADS) == (value, None, None)
    assert normalise("x", owner_names=[], coordinator_names=[])[0] == "x"
    assert normalise("Owner", owner_names=[], coordinator_names=[])[0] == "owner"


async def test_mcp_writes_save_the_fixed_who(svc: Service) -> None:
    key = await agent_card(svc)
    with collect_notes() as notes:
        task, _ = await svc.add_report(
            key, "inspected \u00b7 S \u00b7 done", session="worker", waiting_on="me: merge the PR"
        )
    assert (task.waiting_on, task.next_step) == ("owner", "merge the PR")
    assert notes == [
        'waiting_on: "me: merge the PR" saved as "owner" \u2014 the rest went to next_step'
    ]
    with collect_notes() as notes:
        saved = await svc.update_task_checked(
            key, {"waiting_on": "reviewer (second round)"}, session="worker"
        )
    assert (saved.task.waiting_on, saved.task.next_step) == ("session", "merge the PR")
    assert "the rest was not kept" in notes[0]
    moved = await svc.set_stage(key, "review", waiting_on="master", session="worker")
    assert moved.waiting_on == "coordinator"
    created = await svc.create_task("acme-shop", "New", session="lead", waiting_on="ci: build")
    assert (created.waiting_on, created.next_step) == ("ci", "build")


async def test_the_board_and_a_plain_server_keep_waiting_on_as_written(
    svc: Service, plain: Service
) -> None:
    key = await agent_card(svc)
    saved = await svc.update_task_checked(key, {"waiting_on": "me: look"}, actor="board")
    assert saved.task.waiting_on == "me: look"
    task = await plain.create_task("acme-shop", "Checkout", session="worker")
    with collect_notes() as notes:
        written, _ = await plain.add_report(
            task.key,
            "inspected \u00b7 S \u00b7 done",
            session="worker",
            waiting_on="master: triage",
        )
    assert (written.waiting_on, written.next_step, notes) == ("master: triage", None, [])


async def test_the_owners_inbox_finds_a_normalised_card(svc: Service) -> None:
    key = await agent_card(svc)
    await svc.add_report(
        key, "inspected \u00b7 S \u00b7 done", session="worker", waiting_on="Owner: look"
    )
    task = await svc.get_task(key)
    assert task.waiting_on == "owner" and "waiting on owner" in svc.card_state(task).reasons


# --- instructions ---


async def test_coordinated_instructions_name_only_listed_tools(tmp_path: Path) -> None:
    everything = await all_tools(tmp_path)
    rules = Rules(DEFAULT_RULES)
    full = instructions(rules, coordinated=True)
    golden = GOLDEN.read_text(encoding="utf-8")
    assert instructions(rules, coordinated=False) == golden
    assert "do not create cards." in full and "create it with task_create" not in full
    assert (
        "Findings outside your task and questions for the owner go into"
        " report_add(findings=..., questions=...): the coordinator triages them; do not create"
        " cards or backlog items." in full
    )
    assert "The tracker mark is the coordinator's." in full
    assert len(full.splitlines()) == len(golden.splitlines())
    agent = roles.agent_instructions(rules, coordinated=True)
    assert agent != roles.agent_instructions(rules)
    assert "task_create" not in agent and "backlog_add" not in agent
    assert "report_add(findings=..., questions=...)" in agent
    assert "The tracker mark is the coordinator's." in agent
    listed = roles.role_tools(roles.AGENT, current_tools(everything))
    assert not named(agent, everything) - listed
    for role in (roles.COORDINATOR, roles.READER):
        assert roles.role_instructions(rules, role, True) == roles.role_instructions(rules, role)


async def test_build_mcp_follows_the_config(tmp_path: Path) -> None:
    plain = Service(tmp_path / "plain.sqlite")
    coordinated = Service(tmp_path / "lead.sqlite", coordinators=["lead"])
    golden = GOLDEN.read_text(encoding="utf-8")
    assert build_mcp(plain)._lowlevel_server.instructions == golden
    assert build_mcp(plain, role="agent")._lowlevel_server.instructions == (
        roles.agent_instructions(plain.global_rules)
    )
    server = build_mcp(coordinated)
    assert server._lowlevel_server.instructions == instructions(coordinated.global_rules, True)
    coordinated.rules_changed()  # the refresh after a rules change keeps the mode
    assert server._lowlevel_server.instructions == instructions(coordinated.global_rules, True)


async def test_mcp_endpoints_in_coordinator_mode(
    tmp_path: Path, live_server: Callable[[], LiveServer]
) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    async with connect(live, "/mcp") as client:
        assert "The tracker mark is the coordinator's." in (client.instructions or "")
    async with connect(live, "/mcp/agent") as client:
        text = client.instructions or ""
        assert "report_add(findings=..., questions=...)" in text and "task_create" not in text
        await client.call_tool("project_upsert", {"slug": "acme-shop", "session": "lead"})
        created = await client.call_tool(
            "task_create", {"project": "acme-shop", "title": "Mine", "session": "worker"}
        )
        assert not created.is_error
        refused = await client.call_tool(
            "task_create", {"project": "acme-shop", "title": "Side", "session": "worker"}
        )
        assert refused.is_error and REFUSED in refused.content[0].text


async def test_an_agent_cannot_dismiss_its_own_finding(svc: Service) -> None:
    key = await agent_card(svc)
    await svc.add_report_checked(
        key, "inspected · S · x", session="worker", findings=[{"title": "Old route"}]
    )
    with pytest.raises(ValidationError, match="In coordinator mode"):
        await svc.dismiss_report_item(1, 1, session="worker")
    item = await svc.dismiss_report_item(1, 1, session="lead", reason="known")
    assert item.n == 1
