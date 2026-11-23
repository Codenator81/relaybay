"""The merge order of the cards on test: blocked_by first, then the owner's order of the column;
warnings for shared repositories and blockers not yet on test; the project digest's and
snapshot's section and the board's fold, shown with two or more cards on test."""

from starlette.testclient import TestClient

from relaybay.domain.blockers import BlockLink
from relaybay.domain.merge_order import MERGE_ORDER_TITLE, merge_order
from relaybay.domain.models import Task
from relaybay.domain.stages import Stage
from relaybay.mcp_server import format as fmt
from relaybay.service import Service
from tests.conftest import run_in_app

ROOT = "/home/dev/acme-shop"
REPOS = [{"name": "web", "path": f"{ROOT}/web"}, {"name": "api", "path": f"{ROOT}/api"}]


async def on_test(service: Service, *titles: str) -> list[Task]:
    """New cards go to the top of the column: the ranks put them in the order given."""
    await service.upsert_project("acme-shop", repos=REPOS)
    tasks = [await service.create_task("acme-shop", t, stage="test") for t in titles]
    return [await service.set_task_rank(t.key, n, actor="board") for n, t in enumerate(tasks)]


async def keys(service: Service) -> list[str]:
    order = await service.merge_order(await service.get_project("acme-shop"))
    return [e.task.key for e in order.entries]


async def test_a_chain_goes_by_blocked_by_whatever_the_rank(service: Service) -> None:
    c, b, a = await on_test(service, "Receipts", "Payments", "Checkout")
    for rank, task in enumerate((c, b, a)):
        await service.set_task_rank(task.key, rank, actor="board")
    await service.update_task(c.key, {"blocked_by": [b.key]})
    await service.update_task(b.key, {"blocked_by": [a.key]})
    assert await keys(service) == [a.key, b.key, c.key]


async def test_ties_follow_the_rank(service: Service) -> None:
    x, y, z = await on_test(service, "One", "Two", "Three")
    for rank, task in enumerate((z, x, y)):
        await service.set_task_rank(task.key, rank, actor="board")
    assert await keys(service) == [z.key, x.key, y.key]
    # A blocked card waits for its blocker, the others keep the rank.
    await service.update_task(z.key, {"blocked_by": [y.key]})
    assert await keys(service) == [x.key, y.key, z.key]


async def test_shared_repos_and_open_blockers_outside_test_warn(service: Service) -> None:
    a, b = await on_test(service, "Checkout", "Payments")
    dev = await service.create_task("acme-shop", "Gift cards", stage="dev")
    live = await service.create_task("acme-shop", "Coupons", stage="live")
    await service.update_task(a.key, {"repos": ["web", "api"]})
    await service.update_task(
        b.key, {"repos": ["web"], "prs": ["https://git.example.com/acme/web/pull/41"]}
    )
    await service.update_task(b.key, {"blocked_by": [dev.key, live.key]})
    order = await service.merge_order(await service.get_project("acme-shop"))
    assert order.warnings == [
        f"repos overlap: web — {a.key}, {b.key}",
        f"{b.key} waits for {dev.key} (dev)",
    ]
    lines = order.lines()
    assert lines[0] == f"1. {a.key} Checkout · repos web, api"
    assert lines[1] == f"2. {b.key} Payments · repos web · PR #41 · waits for {dev.key} · dev"

    digest = await service.project_digest("acme-shop")
    section = digest.split(MERGE_ORDER_TITLE, 1)[1].split("\n\n", 1)[0]
    assert lines[0] in section and f"repos overlap: web — {a.key}, {b.key}" in section
    snapshot = fmt.snapshot(await service.snapshot("acme-shop"))
    assert f"{MERGE_ORDER_TITLE}\n{lines[0]}" in snapshot


async def test_no_section_with_fewer_than_two_cards_on_test(service: Service) -> None:
    await on_test(service, "Checkout")
    await service.create_task("acme-shop", "Payments", stage="dev")
    assert MERGE_ORDER_TITLE not in await service.project_digest("acme-shop")
    assert MERGE_ORDER_TITLE not in fmt.snapshot(await service.snapshot("acme-shop"))


def _task(id_: int, rank: int, stage: Stage = Stage.TEST) -> Task:
    return Task(
        id=id_,
        project_id=1,
        key=f"acme-shop-{id_}",
        title=f"Card {id_}",
        stage=stage,
        stage_note=None,
        project_slug="acme-shop",
        waiting_on=None,
        next_step=None,
        branch=None,
        doc_path=None,
        description=None,
        progress=None,
        session_id=None,
        priority=0,
        version=1,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        closed_at=None,
        rank=rank,
    )


def _link(task_id: int, blocker_id: int, stage: str = "test") -> BlockLink:
    return BlockLink(
        task_id, blocker_id, f"acme-shop-{blocker_id}", "x", stage, "acme-shop", False, "t", "now"
    )


def test_a_cycle_falls_back_to_the_rank() -> None:
    cards = [_task(1, 2), _task(2, 1), _task(3, 0, Stage.DEV)]
    blockers = {1: [_link(1, 2)], 2: [_link(2, 1)]}
    order = merge_order(cards, {}, blockers)
    assert [e.task.id for e in order.entries] == [2, 1]
    assert order.warnings == []


def test_the_board_fold_shows_with_two_cards_on_test(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", repos=REPOS)
    a = run_in_app(board, service.create_task, "acme-shop", "Checkout", stage="test")
    run_in_app(board, service.update_task, a.key, {"repos": ["web"]})
    page = board.get("/p/acme-shop").text
    assert 'class="fold merge-order"' not in page

    b = run_in_app(board, service.create_task, "acme-shop", "Payments", stage="test")
    run_in_app(board, service.update_task, b.key, {"repos": ["web"]})
    run_in_app(board, service.set_task_rank, b.key, 5, actor="board")
    column = board.get("/p/acme-shop/col/test").text
    assert 'class="fold merge-order"' in column
    assert f"repos overlap: web — {a.key}, {b.key}" in column
    assert 'class="fold merge-order"' not in board.get("/p/acme-shop/col/dev").text
