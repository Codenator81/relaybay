"""The Coordinator page complete (design 0023, PR 5): its sections in order, what waits for the
owner, what came in beside what it decided, the runs it started today, its schedules, paging of
the turns, the sidebar's mark, live refresh and its board-only forms."""

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.db import repo
from relaybay.domain import coordinator as crules
from relaybay.runner.layers import LayerState
from relaybay.runner.stream import Result
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.web.routes import EVENT_KINDS
from tests.conftest import BASE, run_in_app
from tests.e2e.conftest import make_git_repo
from tests.test_runs_service import CC_A, StubRuns, stub

LAYERS_OK = LayerState("command", True, "command hooks from your user settings", "registered")
SECTIONS = (
    "coordinator-head",
    "coordinator-waits",
    "coordinator-turns",
    "coordinator-runs",
    "coordinator-schedules",
)
APP_JS = Path(__file__).parents[1] / "src/relaybay/web/static/app.js"


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: LAYERS_OK)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path / "home")  # type: ignore[assignment]
    service.memory_reader = lambda: None
    stub(service).loop = asyncio.get_running_loop()
    folder = make_git_repo(tmp_path / "acme-shop")
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


async def started(svc: Service) -> Any:
    """The coordinator started without a gap between wakes, its first turn run and ended."""
    await svc.set_coordinator_settings({"wake_gap_seconds": 0})
    run = await svc.start_coordinator()
    await svc.coordinator_say("Look at the board.")
    await stub(svc).finish(run.id)
    return await svc.get_run(run.id)


async def its_card_run(svc: Service, run: Any) -> Any:
    """A run the coordinator started with run_start, and an open approval it asked for."""
    card = await svc.start_run_mcp("acme-shop-1", session=run.session_name, hook_session=CC_A)
    task = await svc.get_task("acme-shop-1")
    async with svc._tx() as tx:
        await repo.insert_approval(
            tx.conn,
            {
                "task_id": task.id,
                "run_id": card.id,
                "requested_by": "acme-shop-1 run",
                "requested_via": "agent",
                "action": "push",
                "branch": "feat/checkout",
                "summary": "3 commits, CI green",
            },
            tx.now,
        )
    return card


async def queued_turns(svc: Service, run_id: int, count: int) -> None:
    async with svc._tx() as tx:
        for n in range(count):
            await repo.insert_turn(tx.conn, run_id, f"note {n}", "owner", "owner", tx.now)


async def set_run(svc: Service, run_id: int, **changes: Any) -> None:
    async with svc._tx() as tx:
        await repo.update_run(tx.conn, run_id, changes, tx.now)


# --- the data --------------------------------------------------------------------------------


def test_wake_lines_and_the_owners_words() -> None:
    text = "question 3: yes\n\n" + crules.WAKE_HEAD + " 2 events since...\n- a (event 4)\n- b"
    assert crules.wake_lines(text) == ["- a (event 4)", "- b"]
    assert crules.owner_words("answers\n\n" + crules.owner_text("Go on.")) == "Go on."
    assert crules.owner_words("/compact") is None


async def test_what_waits_and_the_runs_it_started_today(svc: Service) -> None:
    run = await started(svc)
    card = await its_card_run(svc, run)
    q, _ = await svc.ask_question(
        None, "Merge now?", "no", blocking=True, session=run.session_name, hook_session=CC_A
    )
    page = await svc.coordinator_page()
    assert [x.id for x in page.questions] == [q.id]
    [approval] = page.approvals
    assert (approval.run_id, approval.task_key, approval.action) == (card.id, "acme-shop-1", "push")
    [mine] = page.started
    assert (mine.id, mine.task_key, mine.task_title, mine.project_slug) == (
        card.id,
        "acme-shop-1",
        "Checkout",
        "acme-shop",
    )
    # A run the owner started on a card is not the coordinator's.
    await svc.end_run(card.id, actor="owner")
    await svc.create_task("acme-shop", "Cart", stage="dev")
    await svc.start_run("acme-shop-2")
    assert [s.id for s in (await svc.coordinator_page()).started] == [card.id]
    badge = await svc.coordinator_badge()
    assert (badge.questions, badge.shown) == (1, True)


async def test_turns_page_back_by_twenty(svc: Service) -> None:
    run = await started(svc)
    await queued_turns(svc, run.id, 24)  # 25 turns in all
    first = await svc.coordinator_page()
    assert first.view is not None
    assert [t.seq for t in first.view.turns] == list(range(6, 26))
    assert (first.older, first.paged) == (6, False)
    assert first.view.totals.tokens == 10  # every turn of the run, not only the page's
    older = await svc.coordinator_page(before=6)
    assert older.view is not None
    assert [t.seq for t in older.view.turns] == [1, 2, 3, 4, 5]
    assert (older.older, older.paged) == (None, True)


async def test_the_badge_shows_its_budget_warning(svc: Service) -> None:
    assert not (await svc.coordinator_badge()).shown
    await svc.set_coordinator_settings({"wake_gap_seconds": 0, "daily_tokens": 20_000})
    run = await svc.start_coordinator()
    await svc.coordinator_say("Spend.")
    turn_id = stub(svc).active.pop(run.id)
    await svc.turn_init(turn_id, CC_A)
    await svc.turn_done(
        turn_id, "success", Result("success", False, 1, 10, 100, 100, 16_000), 0, None
    )
    await svc.coordinator_wake_pass()
    badge = await svc.coordinator_badge()
    assert (badge.questions, badge.budget_level, badge.shown) == (0, "warned", True)


# --- the board ---------------------------------------------------------------------------------


@pytest.fixture
def board(
    settings: Settings, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: LAYERS_OK)
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        service: Service = client.app.state.service
        service.runs = StubRuns(service, tmp_path / "home")  # type: ignore[assignment]
        service.memory_reader = lambda: None
        folder = make_git_repo(tmp_path / "acme-shop")
        run_in_app(client, service.upsert_project, "acme-shop", repo_path=str(folder))
        run_in_app(client, service.create_task, "acme-shop", "Checkout", stage="dev")
        yield client


def service_of(board: TestClient) -> Service:
    return board.app.state.service


def in_order(text: str, ids: tuple[str, ...]) -> bool:
    at = [text.find(f'id="{i}"') for i in ids]
    return all(x >= 0 for x in at) and at == sorted(at)


def test_the_page_before_any_coordinator_explains_start(board: TestClient) -> None:
    page = board.get("/coordinator")
    assert page.status_code == 200
    assert 'id="coordinator-none"' in page.text and "No coordinator yet" in page.text
    folder = str(Path(service_of(board).runs.config.home) / "coordinator")  # type: ignore[union-attr]
    assert folder in page.text.replace("&#39;", "'")
    assert in_order(page.text, SECTIONS)
    assert 'action="/coordinator/start"' in page.text
    assert 'id="coordinator-say"' not in page.text  # the box comes with an open run
    assert 'sse-connect="/events"' in page.text
    assert 'hx-get="/fragments/coordinator"' in page.text
    assert re.search(r'id="coordinator-badge"[^>]*hidden', page.text)


def test_the_sections_show_its_turns_what_waits_and_its_runs(board: TestClient) -> None:
    svc = service_of(board)
    run = run_in_app(board, started, svc)
    run_in_app(board, its_card_run, svc, run)
    q, _ = run_in_app(
        board,
        svc.ask_question,
        None,
        "Merge now?",
        "no",
        blocking=True,
        session=run.session_name,
        hook_session=CC_A,
    )
    run_in_app(board, svc.add_report, "acme-shop-1", "verified · M · checkout done", session="w")
    run_in_app(board, svc.coordinator_wake_pass)
    page = board.get("/coordinator")
    text = page.text
    assert in_order(text, SECTIONS)
    assert 'id="coordinator-say"' in text and 'action="/coordinator/say"' in text
    # What waits: its question and the approval of the run it started, as plain form posts.
    waits = text[text.find('id="coordinator-waits"') : text.find('id="coordinator-turns"')]
    assert "Merge now?" in waits and f'action="/questions/{q.id}/answer"' in waits
    assert 'action="/approvals/' in waits and 'href="/p/acme-shop?task=acme-shop-1"' in waits
    assert 'name="from" value="coordinator"' in waits and "hx-post" not in waits
    # What came in beside what it decided: the owner's turn and the wake with its event line.
    turns = text[text.find('id="coordinator-turns"') : text.find('id="coordinator-runs"')]
    assert "the owner" in turns and "Look at the board." in turns
    assert "wake" in turns and "checkout done" in turns and "Decided" in turns
    assert turns.find('id="turn-2"') < turns.find('id="turn-1"')  # newest first
    runs = text[text.find('id="coordinator-runs"') : text.find('id="coordinator-schedules"')]
    assert "acme-shop-1" in runs and "Checkout" in runs
    schedules = text[text.find('id="coordinator-schedules"') :]
    assert 'href="/schedules"' in schedules
    # The sidebar marks Coordinator: one question waits for the owner.
    badge = re.search(r'<span class="nav-badge" id="coordinator-badge"[^>]*>', text)
    assert badge is not None and " hidden" not in badge.group(0)
    fragment = board.get("/fragments/coordinator-badge")
    assert fragment.status_code == 200 and ">1<" in fragment.text
    live = board.get("/fragments/coordinator")
    assert (
        live.status_code == 200
        and 'id="coordinator-live"' in live.text
        and "Merge now?" in live.text
    )
    assert 'data-keys="acme-shop-1"' in live.text


def test_compact_turns_are_relaybays_and_paging_links(board: TestClient) -> None:
    svc = service_of(board)
    run = run_in_app(board, started, svc)
    run_in_app(board, queued_turns, svc, run.id, 24)
    page = board.get("/coordinator").text
    assert 'href="/coordinator?before=6#coordinator-turns"' in page
    assert 'id="turn-25"' in page and 'id="turn-5"' not in page
    older = board.get("/coordinator?before=6").text
    assert 'id="turn-5"' in older and 'id="turn-6"' not in older
    assert 'href="/coordinator">Newest turns' in older
    assert 'hx-get="/fragments/coordinator?before=6"' in older
    assert board.get("/coordinator?before=0").status_code == 422

    async def compact(svc: Service) -> None:
        async with svc._tx() as tx:
            await repo.insert_turn(tx.conn, run.id, "/compact", "relaybay", "relaybay", tx.now)

    run_in_app(board, compact, svc)
    turns = board.get("/coordinator").text
    head = turns[turns.find('id="turn-26"') :]
    assert "relaybay</span>" in head[:400] and "sent by relaybay" in head


@pytest.mark.parametrize("state", ["stopped", "with_owner", "ended", "over_budget"])
def test_the_page_says_why_it_waits(board: TestClient, state: str) -> None:
    svc = service_of(board)
    run = run_in_app(board, started, svc)
    if state == "stopped":
        run_in_app(board, set_run, svc, run.id, status="stopped")
        marker, words = "coordinator-stopped", "Stopped."
    elif state == "with_owner":
        run_in_app(board, set_run, svc, run.id, with_owner_at="2026-11-16T10:00:00.000+00:00")
        marker, words = "coordinator-with-owner", "With you in the desktop app."
    elif state == "ended":
        run_in_app(board, svc.end_coordinator)
        marker, words = "coordinator-ended", "Ended"
    else:
        budget = run_in_app(board, repo.session_budget, svc._r, run.session_id)

        async def stop_at_one(svc: Service) -> None:
            async with svc._tx() as tx:
                await tx.conn.execute(
                    "UPDATE token_budget SET action = 'stop', max_tokens = 1 WHERE id = ?",
                    (budget.id,),
                )

        run_in_app(board, stop_at_one, svc)
        marker, words = "coordinator-over-budget", "Wakes stop:"
    page = board.get("/coordinator").text
    assert f'id="{marker}"' in page and words in page
    assert in_order(page, SECTIONS)
    if state == "ended":
        assert 'action="/coordinator/start"' in page and 'id="coordinator-say"' not in page


def test_its_forms_stay_board_only(board: TestClient, token: str) -> None:
    svc = service_of(board)
    run = run_in_app(board, started, svc)
    run_in_app(board, its_card_run, svc, run)
    [approval] = run_in_app(board, repo.coordinator_run_approvals, svc._r)
    path = f"/approvals/{approval.id}/decide"
    fields = {"decision": "refuse", "from": "coordinator"}
    assert board.post(path, data=fields, headers={"Origin": BASE}).status_code == 403
    bearer = board.post(
        path,
        data={"csrf_token": csrf_token(token), **fields},
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    done = board.post(
        path,
        data={"csrf_token": csrf_token(token), **fields},
        headers={"Origin": BASE},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/coordinator"
    anonymous = TestClient(board.app, base_url=BASE)
    for url in ("/coordinator", "/fragments/coordinator", "/fragments/coordinator-badge"):
        got = anonymous.get(url, follow_redirects=False)
        assert got.status_code != 200, url


def test_the_page_listens_to_its_kinds() -> None:
    script = APP_JS.read_text("utf-8")
    kinds = re.search(r"var KINDS = \[(.*?)\];", script, re.S)
    assert kinds is not None
    listened = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', kinds.group(1)))
    for kind in (
        "coordinator.started",
        "coordinator.stopped",
        "coordinator.ended",
        "coordinator.new_session",
        "coordinator.woken",
        "coordinator.compacting",
        "run.turn_started",
        "run.turn_ended",
        "question.asked",
        "question.answered",
        "question.overdue",
        "approval.requested",
        "approval.decided",
        "schedule.fired",
        "schedule.updated",
        "budget.warned",
    ):
        assert kind in listened and kind in EVENT_KINDS, kind
    assert 'getElementById("coordinator-live")' in script
    assert 'getElementById("coordinator-badge")' in script
