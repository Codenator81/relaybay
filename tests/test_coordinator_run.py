"""The coordinator run (design 0023, PR 2): a run without a card, its folder, its identity and
rights, its questions, the brakes and slots it answers to, and its board page."""

import asyncio
import sqlite3
import sys
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import coordinator as crules
from relaybay.domain import profiles
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.runner import coordinator as folders
from relaybay.runner.claude_cli import build_command
from relaybay.runner.layers import LayerState
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.e2e.conftest import make_git_repo
from tests.test_migrations import LATEST, NOW
from tests.test_runs_service import CC_A, CC_B, StubRuns, stub

WINDOWS = sys.platform == "win32"


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path / "home")  # type: ignore[assignment]
    service.memory_reader = lambda: None  # never the machine's own free memory
    stub(service).loop = asyncio.get_running_loop()
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


async def started(svc: Service) -> Any:
    """The coordinator started, its first turn run and ended with Claude session CC_A."""
    run = await svc.start_coordinator()
    await svc.coordinator_say("Look at the board.")
    await stub(svc).finish(run.id)
    return await svc.get_run(run.id)


async def events(svc: Service, kind: str) -> list[Any]:
    return [e for e in await repo.events_after(svc._r, 0, None, 1000) if e.kind == kind]


# --- migration 0045 ----------------------------------------------------------------------


async def test_migration_keeps_runs_and_questions_and_its_checks_hold(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0045")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 44;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (id, slug, name, task_seq, created_at, updated_at)"
            " VALUES (1, 'acme-shop', 'acme-shop', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO task (id, project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 1, 'acme-shop-1', 'Checkout', 'dev', ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO run (id, task_id, project_id, cwd, model, effort, permission_mode,"
            " allowed_tools, status, hooks_source, mcp_source, started_by, started_by_kind,"
            " created_at, updated_at, profile, mode_set, with_owner_in, secret_seen_at)"
            " VALUES (7, 1, 1, '/code/acme-shop', 'sonnet', 'high', 'plan', 'mcp__relaybay',"
            " 'idle', 'user', 'run', 'owner', 'owner', ?, ?, 'agent', 1, 'terminal', ?)",
            (NOW, NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO run_turn (id, run_id, seq, prompt, sent_by, sent_by_kind, queued_at)"
            " VALUES (3, 7, 1, 'Go', 'owner', 'owner', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO question (id, task_id, run_id, asked_by, kind, text, default_answer,"
            " blocking, asked_at, status, turn_id) VALUES (5, 1, 7, 'acme-shop-1 run 7',"
            " 'question', 'Which provider?', 'Stripe', 1, ?, 'open', 3)",
            (NOW,),
        )
        await conn.commit()
        assert await migrate(conn) == LATEST
        conn.row_factory = sqlite3.Row
        async with conn.execute("SELECT * FROM run") as cur:
            [run] = await cur.fetchall()
        assert (run["id"], run["kind"], run["task_id"], run["model"], run["profile"]) == (
            7,
            "card",
            1,
            "sonnet",
            "agent",
        )
        assert (run["mode_set"], run["with_owner_in"], run["secret_seen_at"]) == (
            1,
            "terminal",
            NOW,
        )
        async with conn.execute("SELECT * FROM question") as cur:
            [q] = await cur.fetchall()
        assert (q["id"], q["task_id"], q["run_id"], q["turn_id"], q["text"]) == (
            5,
            1,
            7,
            3,
            "Which provider?",
        )
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
        bad = (
            # a card run without its card, a coordinator run with one
            "INSERT INTO run (kind, cwd, model, effort, permission_mode, allowed_tools, status,"
            " hooks_source, mcp_source, started_by, started_by_kind, created_at, updated_at)"
            " VALUES ('card', '/x', 'opus', 'medium', 'auto', 'mcp__relaybay', 'idle', 'user',"
            " 'user', 'owner', 'owner', 'n', 'n')",
            "INSERT INTO run (kind, task_id, project_id, cwd, model, effort, permission_mode,"
            " allowed_tools, status, hooks_source, mcp_source, started_by, started_by_kind,"
            " created_at, updated_at) VALUES ('coordinator', 1, 1, '/x', 'opus', 'medium',"
            " 'dontAsk', 'Read', 'idle', 'user', 'user', 'owner', 'owner', 'n', 'n')",
            "INSERT INTO run (kind, cwd, model, effort, permission_mode, allowed_tools, status,"
            " hooks_source, mcp_source, started_by, started_by_kind, created_at, updated_at)"
            " VALUES ('check', '/x', 'opus', 'medium', 'dontAsk', 'Read', 'idle', 'user',"
            " 'user', 'owner', 'owner', 'n', 'n')",
            # a question on nothing
            "INSERT INTO question (asked_by, kind, text, default_answer, blocking, asked_at,"
            " status) VALUES ('x', 'question', 'Why?', 'No', 0, 'n', 'open')",
        )
        for statement in bad:
            with pytest.raises(sqlite3.IntegrityError):
                await conn.execute(statement)
        coordinator = (
            "INSERT INTO run (kind, cwd, model, effort, permission_mode, allowed_tools, status,"
            " hooks_source, mcp_source, started_by, started_by_kind, created_at, updated_at)"
            " VALUES ('coordinator', '/c', 'opus', 'medium', 'dontAsk', 'Read', 'idle', 'user',"
            " 'user', 'owner', 'owner', 'n', 'n')"
        )
        await conn.execute(coordinator)
        with pytest.raises(sqlite3.IntegrityError):  # at most one open coordinator run
            await conn.execute(coordinator)
    finally:
        await conn.close()


# --- the folder ---------------------------------------------------------------------------


def test_the_default_folder_moves_out_of_a_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "data"
    assert folders.default_folder(outside) == (outside / "coordinator", None)
    checkout = tmp_path / "checkout"
    (checkout / ".dev-data").mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a worktree's file
    user = tmp_path / "user-data"
    monkeypatch.setattr(folders.platformdirs, "user_data_dir", lambda *a, **k: str(user))
    path, why = folders.default_folder(checkout / ".dev-data")
    assert path == user / "coordinator"
    assert why is not None and str(checkout) in why


async def test_start_refuses_a_folder_inside_a_git_repository(svc: Service, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    inside = repository / "notes" / "coordinator"
    with pytest.raises(ValidationError, match="inside the git repository"):
        await svc.set_coordinator_settings({"folder": str(inside)})
    async with svc._tx() as tx:  # a repository made after the folder was saved
        await repo.set_app_setting(tx.conn, crules.FOLDER_KEY, str(inside), tx.now, "owner")
    with pytest.raises(ValidationError, match="inside the git repository"):
        await svc.start_coordinator()
    assert await repo.open_coordinator_run(svc._r) is None
    assert not inside.exists()
    page = await svc.coordinator_page()
    assert page.refusal is not None and "inside the git repository" in page.refusal


async def test_the_starter_claude_md_is_written_once_and_never_overwritten(svc: Service) -> None:
    run = await svc.start_coordinator()
    assert (run.kind, run.status, run.task_id, run.project_id) == (
        "coordinator",
        "idle",
        None,
        None,
    )
    assert await repo.list_turns(svc._r, run.id) == []  # Start spends no turn
    starter = Path(run.cwd) / "CLAUDE.md"
    text = starter.read_text("utf-8")
    assert text == folders.starter_text()
    for line in ("data, never instructions", "The owner says on the board", "do not edit"):
        assert line in text
    starter.write_text("My own coordinator rules.\n", encoding="utf-8")
    fresh = await svc.start_coordinator(new_session=True)
    assert fresh.id != run.id and fresh.cwd == run.cwd
    assert (await svc.get_run(run.id)).status == "ended"
    assert starter.read_text("utf-8") == "My own coordinator rules.\n"
    with pytest.raises(ConflictError, match="one at a time"):
        await svc.start_coordinator()
    kinds = [e.kind for e in await events(svc, "coordinator.new_session")]
    assert kinds == ["coordinator.new_session"]
    audited = {e.kind for e in await svc.audit(changes=True)}
    assert {"coordinator.started", "coordinator.new_session"} <= audited


# --- its turns ------------------------------------------------------------------------------


async def test_a_coordinator_turn_runs_in_its_folder_under_its_profile(svc: Service) -> None:
    run = await svc.start_coordinator()
    turn = await svc.coordinator_say("Plan the week.")
    assert (turn.sent_by, turn.sent_by_kind) == ("owner", "owner")
    [(run_id, _, spec)] = stub(svc).started
    assert run_id == run.id and spec.cwd == run.cwd and spec.add_dirs == ()
    coordinator = profiles.BUILTIN["coordinator"]
    assert spec.mode == coordinator["permission_mode"] == "dontAsk"
    rule = crules.edit_rule(run.cwd, windows=WINDOWS)
    assert spec.allowed == (*coordinator["allowed_tools"].split(", "), rule)
    assert rule.startswith("Edit(//") and rule.endswith("/coordinator/**)")
    assert spec.disallowed == profiles.FIXED_DISALLOWED
    assert (spec.model, spec.effort) == ("opus", "medium")
    argv, _ = build_command(spec)
    assert "--add-dir" not in argv and "Write" not in argv
    assert spec.prompt.startswith(f"relaybay coordinator run {run.id}: one coordinator")
    assert f'pass session "coordinator run {run.id}"' in spec.prompt
    assert spec.prompt.endswith("The owner says on the board: Plan the week.")
    await stub(svc).finish(run.id)
    view = await svc.coordinator_run_view()
    assert view is not None and view.run.claude_session_id == CC_A
    [done] = view.turns
    assert done.result_md and done.report_id is None
    async with svc._r.execute("SELECT COUNT(*) FROM report") as cur:
        assert (await cur.fetchone())[0] == 0  # no card holds its result
    await svc.set_coordinator_settings({"model": "sonnet", "effort": "low"})
    second = await svc.coordinator_say("And the next one.")
    spec = stub(svc).started[-1][2]
    assert (spec.model, spec.effort, spec.resume) == ("sonnet", "low", CC_A)
    assert not spec.prompt.startswith("relaybay coordinator run")  # the head only on turn 1
    assert second.seq == 2


def test_the_folder_rule_is_anchored_at_the_root() -> None:
    assert crules.edit_rule(r"C:\Data\coordinator", windows=True) == "Edit(//c/Data/coordinator/**)"
    assert crules.edit_rule("/home/demo/coordinator/", windows=False) == (
        "Edit(//home/demo/coordinator/**)"
    )
    with pytest.raises(ValidationError, match="pattern"):
        crules.edit_rule("/home/demo/notes (old)", windows=False)
    for root, windows in (("C:\\", True), ("D:/", True), ("/", False)):
        with pytest.raises(ValidationError, match="root"):
            crules.edit_rule(root, windows=windows)
    with pytest.raises(ValidationError, match="root"):
        crules.edit_rule("/home/demo/../..", windows=False)


def test_the_folder_setting_is_normalised(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    with pytest.raises(ValidationError, match="root"):
        crules.clean_folder(str(root))
    with pytest.raises(ValidationError, match=r"'\.\.'"):
        crules.clean_folder(str(tmp_path / "a" / ".." / ".." / "b"))
    assert crules.clean_folder(str(tmp_path / "coordinator") + "/") == str(
        (tmp_path / "coordinator").resolve()
    )


async def test_its_turn_takes_no_slot_but_waits_for_memory(svc: Service) -> None:
    await svc.set_run_limits(1, 0)
    card = await svc.start_run("acme-shop-1")
    assert card.status == "running"
    run = await svc.start_coordinator()
    await svc.coordinator_say("React now.")
    assert (await svc.get_run(run.id)).status == "running"  # past the one slot in use
    assert await repo.count_running_turns(svc._r) == 1
    await stub(svc).finish(run.id)
    await stub(svc).finish(card.id, cc_id=CC_B)
    await svc.set_run_limits(1, 4000)
    svc.memory_reader = lambda: 1000
    await svc.coordinator_say("Again.")
    waiting = await svc.get_run(run.id)
    assert waiting.status == "queued" and "waiting for memory" in (waiting.status_reason or "")


async def test_a_project_brake_does_not_stop_it_all_does(svc: Service) -> None:
    run = await svc.start_coordinator()
    await svc.set_board_brake("project", project="acme-shop")
    await svc.coordinator_say("Go on.")
    assert (await svc.get_run(run.id)).status == "running"
    await stub(svc).finish(run.id)
    await svc.set_board_brake("all")
    await svc.coordinator_say("Stop?")
    held = await svc.get_run(run.id)
    assert held.status == "queued" and (held.status_reason or "").startswith("braked (all")


# --- identity and rights --------------------------------------------------------------------


async def test_the_coordinator_is_known_by_its_claude_session(svc: Service, tmp_path: Path) -> None:
    run = await started(svc)
    assert await svc._is_coordinator(svc._r, "anyone", CC_A)
    assert not await svc._is_coordinator(svc._r, "anyone", CC_B)
    assert not await svc._is_coordinator(svc._r, run.session_name, None)
    api = make_git_repo(tmp_path / "api")
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    card = await svc.start_run_mcp("acme-shop-1", session=run.session_name, hook_session=CC_A)
    assert (card.started_by, card.started_by_kind, card.started_by_run) == (
        run.session_name,
        "coordinator",
        run.id,
    )
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await svc.start_run_mcp("acme-shop-1", session=run.session_name, hook_session=CC_B)


RIGHTS_CALLS = {
    "run_start": lambda s, kw: s.start_run_mcp("acme-shop-1", **kw),
    "run_settings": lambda s, kw: s.run_settings_mcp("acme-shop-1", model="haiku", **kw),
    "rules_update": lambda s, kw: s.update_rules(
        "acme-shop", {"guidance": "Short reports."}, "Clearer.", **kw
    ),
    "plan_accept": lambda s, kw: s.accept_plan("acme-shop-1", **kw),
    "tasks_write": lambda s, kw: s.create_task("acme-shop", "New card", **kw),
    "messages": lambda s, kw: s.send_session_message("Hello", to="acme-shop-1 run 9", **kw),
}


@pytest.mark.parametrize("right", sorted(RIGHTS_CALLS))
async def test_each_right_off_in_its_profile_is_refused(svc: Service, right: str) -> None:
    run = await started(svc)
    shipped = profiles.BUILTIN["coordinator"]
    await svc.update_profile("coordinator", {**shipped, "rights": {**shipped["rights"], right: 0}})
    caller = {"session": run.session_name, "hook_session": CC_A}
    with pytest.raises(ValidationError, match=f"off for the coordinator.*grant {right}"):
        await RIGHTS_CALLS[right](svc, caller)
    if right in ("tasks_write", "messages"):  # tools open to any session: no hook, no way past
        for hook in (None, CC_B):
            named = {"session": run.session_name, "hook_session": hook}
            with pytest.raises(ValidationError, match=f"grant {right}"):
                await RIGHTS_CALLS[right](svc, named)


async def test_the_eleventh_run_start_of_a_day_is_refused(svc: Service) -> None:
    run = await started(svc)
    cards = [await svc.create_task("acme-shop", f"Card {n}", stage="dev") for n in range(10)]
    async with svc._tx() as tx:
        for n, task in enumerate(cards):
            await repo.insert_run(
                tx.conn,
                {
                    "task_id": task.id,
                    "project_id": task.project_id,
                    "cwd": f"/code/w{n}",
                    "model": "opus",
                    "effort": "medium",
                    "permission_mode": "auto",
                    "allowed_tools": "mcp__relaybay",
                    "status": "ended",
                    "ended_at": tx.now,
                    "hooks_source": "user",
                    "mcp_source": "user",
                    "started_by": run.session_name,
                    "started_by_kind": "coordinator",
                    "started_by_run": run.id,
                },
                tx.now,
            )
    with pytest.raises(ValidationError, match=r"started 10 run.*allows 10 a day"):
        await svc.start_run_mcp("acme-shop-1", session=run.session_name, hook_session=CC_A)


async def test_the_cap_is_counted_again_inside_the_start(svc: Service) -> None:
    run = await started(svc)
    await svc.update_profile(
        "coordinator",
        {
            **profiles.BUILTIN["coordinator"],
            "rights": {**profiles.BUILTIN["coordinator"]["rights"], profiles.PER_DAY: 1},
        },
    )
    other = await svc.create_task("acme-shop", "Other", stage="dev")
    async with svc._tx() as tx:  # a start that won the race meanwhile
        await repo.insert_run(
            tx.conn,
            {
                "task_id": other.id,
                "project_id": other.project_id,
                "cwd": "/code/other",
                "model": "opus",
                "effort": "medium",
                "permission_mode": "auto",
                "allowed_tools": "mcp__relaybay",
                "status": "ended",
                "ended_at": tx.now,
                "hooks_source": "user",
                "mcp_source": "user",
                "started_by": run.session_name,
                "started_by_kind": "coordinator",
                "started_by_run": run.id,
            },
            tx.now,
        )
    with pytest.raises(ValidationError, match="allows 1 a day"):
        # past the check before the checks: the write transaction counts again
        await svc.start_run("acme-shop-1", started_by_run=run.id)
    assert await repo.open_run_of_task(svc._r, 1) is None


async def test_the_coordinators_profile_is_kept_and_a_missing_one_is_named(svc: Service) -> None:
    values = {**profiles.BUILTIN["coordinator"], "description": "Mine."}
    await svc.create_profile("coordinator-mine", values)
    await svc.set_coordinator_settings({"profile": "coordinator-mine"})
    with pytest.raises(ConflictError, match="coordinator's profile in Settings -> Agents"):
        await svc.delete_profile("coordinator-mine")
    async with svc._tx() as tx:
        await repo.set_app_setting(tx.conn, crules.PROFILE_KEY, "gone", tx.now, "owner")
    with pytest.raises(ValidationError, match="profile gone does not exist"):
        await svc.start_coordinator()


async def test_the_head_comes_until_the_session_is_known(svc: Service) -> None:
    run = await svc.start_coordinator()
    await svc.coordinator_say("First.")
    await stub(svc).stop(run.id, "stopped")  # ended before its init: no Claude session yet
    assert (await svc.get_run(run.id)).claude_session_id is None
    again = await svc.coordinator_say("Second.")
    assert again.seq == 2 and again.prompt.startswith(f"relaybay coordinator run {run.id}:")


async def test_board_acceptances_reach_the_coordinator_without_a_config(svc: Service) -> None:
    assert svc.coordinators == []
    run = await started(svc)
    await svc.set_plan("acme-shop-1", [{"title": "Build it"}])
    await svc.accept_plan("acme-shop-1", actor="owner")
    session = await repo.get_session_by_id(svc._r, run.session_id)
    assert session is not None
    told = await svc._take_plan_acceptances(session)
    assert told and "Owner accepted the plan of acme-shop-1" in told[0]


async def test_the_coordinator_requests_no_approvals(svc: Service) -> None:
    run = await started(svc)
    with pytest.raises(ValidationError, match="does not request approvals"):
        await svc.request_approval(
            "acme-shop-1",
            "push",
            "main",
            "Push the fix",
            session=run.session_name,
            hook_session=CC_A,
        )


async def test_no_mcp_path_starts_a_coordinator(svc: Service) -> None:
    run = await started(svc)
    with pytest.raises(NotFoundError):
        await svc.start_run_mcp("coordinator", session=run.session_name, hook_session=CC_A)
    assert [r.id for r in await repo.open_runs(svc._r) if r.is_coordinator] == [run.id]
    task, view = await svc.run_status("coordinator")
    assert task is None and view is not None and view.run.id == run.id


async def test_a_desktop_coordinator_of_the_config_keeps_working(svc: Service) -> None:
    svc.coordinators = ["lead"]
    await started(svc)
    assert await svc._is_coordinator(svc._r, "lead", None)
    await svc.update_rules("acme-shop", {"guidance": "Short."}, "Clearer.", session="lead")


# --- its questions ------------------------------------------------------------------------


async def test_a_question_without_a_card_and_its_answer_as_the_next_turn(svc: Service) -> None:
    run = await svc.start_coordinator()
    await svc.coordinator_say("Ask me.")
    turn_id = stub(svc).active[run.id]
    await svc.turn_init(turn_id, CC_A)
    run = await svc.get_run(run.id)
    with pytest.raises(ValidationError, match="needs the card"):
        await svc.ask_question(None, "Which?", "A", session="worker")
    q, hint = await svc.ask_question(
        None,
        "Which project first?",
        "acme-shop",
        options=["acme-shop", "demo-api"],
        blocking=True,
        session=run.session_name,
        hook_session=CC_A,
    )
    assert (q.task_id, q.run_id, q.origin) == (None, run.id, "Coordinator")
    assert "next turn" in hint
    await stub(svc).finish(run.id)
    assert (await svc.get_run(run.id)).status == "waiting"
    assert [x.id for x in await svc.open_questions()] == [q.id]
    await svc.answer_question(q.id, choice="demo-api")
    spec = stub(svc).started[-1][2]
    assert spec.prompt.startswith(f'Owner\'s answer to question {q.id} ("Which project first?")')
    assert spec.prompt.endswith(": demo-api")
    asked = (await events(svc, "question.asked"))[0]
    assert asked.task_id is None and asked.payload["run_kind"] == "coordinator"


async def test_an_answer_after_end_waits_for_the_next_start(svc: Service) -> None:
    run = await started(svc)
    q, _ = await svc.ask_question(
        None, "Go on?", "yes", blocking=True, session=run.session_name, hook_session=CC_A
    )
    await svc.end_coordinator()
    await svc.answer_question(q.id, text="yes, carefully")
    fresh = await svc.start_coordinator()
    assert (await svc.get_question(q.id)).run_id == fresh.id
    turn = await svc.coordinator_say("Morning.")
    assert turn.prompt.split("\n\n")[0].endswith(f'question {q.id} ("Go on?"): yes, carefully')


async def test_a_card_question_of_an_ended_coordinator_reaches_its_next_turn(
    svc: Service,
) -> None:
    run = await started(svc)
    q, _ = await svc.ask_question(
        "acme-shop-1",
        "Merge now?",
        "wait",
        blocking=True,
        session=run.session_name,
        hook_session=CC_A,
    )
    assert (q.task_id, q.run_id) == (1, run.id)
    await svc.end_coordinator()
    await svc.answer_question(q.id, text="yes")
    assert (await svc.get_question(q.id)).comment_id is None  # not sent to the card's session
    fresh = await svc.start_coordinator()
    assert (await svc.get_question(q.id)).run_id == fresh.id
    turn = await svc.coordinator_say("Morning.")
    assert f'question {q.id} ("Merge now?"): yes' in turn.prompt


# --- the board ------------------------------------------------------------------------------


@pytest.fixture
def board(settings: Settings, token: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        yield client


def test_the_coordinator_page_and_its_forms_are_board_only(board: TestClient, token: str) -> None:
    page = board.get("/coordinator")
    assert page.status_code == 200
    assert "Coordinator" in page.text and 'href="/coordinator"' in page.text
    assert 'action="/settings/agents/coordinator"' in board.get("/settings/agents").text
    for path in ("/coordinator/start", "/coordinator/say", "/settings/agents/coordinator"):
        no_csrf = board.post(path, data={"text": "x"}, headers={"Origin": BASE})
        assert no_csrf.status_code == 403, path
        bearer = board.post(
            path,
            data={"csrf_token": csrf_token(token), "text": "x"},
            headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
        )
        assert bearer.status_code == 403, path
    said = board.post(
        "/coordinator/say",
        data={"csrf_token": csrf_token(token), "text": "Hello"},
        headers={"Origin": BASE},
    )
    assert said.status_code == 200 and "not started" in said.text
    service: Service = board.app.state.service
    assert run_in_app(board, repo.open_coordinator_run, service._r) is None
