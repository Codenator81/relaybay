"""Cards blocked by other cards (`blocked_by`): links of any project, refusals (unknown key, the
card itself, a cycle), whether a blocker still blocks read from its stage, task_context and
task_get, the board's chips and rows, and the export of a purged blocker."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.domain.errors import ValidationError
from relaybay.mcp_server import format as fmt
from relaybay.mcp_server.server import build_mcp
from relaybay.service import Service
from tests.conftest import run_in_app

BASELINE = Path(__file__).parent / "fixtures" / "mcp_contract_baseline.json"
BOARD = "http://127.0.0.1:8765"


def caller(service: Service) -> Any:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    return call


async def cards(service: Service, *titles: str, project: str = "acme-shop") -> list[str]:
    await service.upsert_project(project)
    return [(await service.create_task(project, t, stage="dev")).key for t in titles]


async def blockers_of(service: Service, key: str) -> list[str]:
    blocked_by, _ = await service.task_blocks(await service.get_task(key))
    return [b.key for b in blocked_by]


async def test_add_and_remove_with_an_event_and_no_new_version(service: Service) -> None:
    a, b, c = await cards(service, "Checkout", "Payments", "Receipts")
    before = await service.get_task(a)
    since = await service.last_event_id()
    task = await service.update_task(a, {"blocked_by": [b, c.upper()]})
    assert task.version == before.version
    assert await blockers_of(service, a) == [b, c]
    # The same link again writes nothing.
    await service.update_task(a, {"blocked_by": [b]})
    await service.update_task(a, {"blocked_by": [f"-{b}"]})
    assert await blockers_of(service, a) == [c]
    events = [e for e in await service.replay(since, None, 50) if e.kind == "task.updated"]
    assert [e.payload["changes"] for e in events] == [
        {"blocked_by": {"added": [b, c]}},
        {"blocked_by": {"removed": [b]}},
    ]
    _, blocks = await service.task_blocks(await service.get_task(c))
    assert [x.key for x in blocks] == [a]


async def test_refusals_name_the_problem(service: Service) -> None:
    a, b, c = await cards(service, "Checkout", "Payments", "Receipts")
    with pytest.raises(ValidationError, match="no card 'acme-shop-99'"):
        await service.update_task(a, {"blocked_by": ["acme-shop-99"]})
    with pytest.raises(ValidationError, match="cannot be blocked by itself"):
        await service.update_task(a, {"blocked_by": [a]})
    with pytest.raises(ValidationError, match="is not a card key"):
        await service.update_task(a, {"blocked_by": ["https://example.com/acme-shop-2"]})
    await service.update_task(a, {"blocked_by": [b]})
    with pytest.raises(ValidationError, match=f"{a} is blocked by {b}"):
        await service.update_task(b, {"blocked_by": [a]})
    await service.update_task(b, {"blocked_by": [c]})
    with pytest.raises(ValidationError, match=f"{a} is blocked by {b} is blocked by {c}"):
        await service.update_task(c, {"blocked_by": [a]})
    # A refused entry undoes the whole write, the fields with it.
    with pytest.raises(ValidationError):
        await service.update_task(c, {"next_step": "Ship", "blocked_by": [b, "acme-shop-99"]})
    assert await blockers_of(service, c) == []
    assert (await service.get_task(c)).next_step is None


async def test_a_card_of_another_project_can_block(service: Service) -> None:
    [a] = await cards(service, "Checkout")
    [api] = await cards(service, "Refund endpoint", project="demo-api")
    await service.update_task(a, {"blocked_by": [api]})
    assert await blockers_of(service, a) == [api]


async def test_a_blocker_stops_blocking_at_live_or_done_and_blocks_again(
    service: Service,
) -> None:
    a, b = await cards(service, "Checkout", "Payments")
    await service.update_task(a, {"blocked_by": [b]})
    task_a = await service.get_task(a)
    project_id = task_a.project_id

    async def active() -> list[str]:
        return [x.key for x in (await service.active_blockers(project_id)).get(task_a.id, [])]

    for stage, blocks in (
        ("plan", True),
        ("review", True),
        ("test", True),
        ("live", False),
        ("done", False),
        ("dev", True),
    ):
        await service.set_stage(b, stage)
        assert await active() == ([b] if blocks else []), stage
    await service.set_stage(b, "done")
    await service.set_archived(b, True, actor="owner")
    assert await active() == []
    assert await blockers_of(service, a) == [b]  # the link stays, it only no longer blocks


async def test_task_context_and_task_get_show_both_directions(service: Service) -> None:
    a, b, c = await cards(service, "Checkout", "Payments", "Receipts")
    await service.update_task(a, {"blocked_by": [b, c]})
    await service.set_stage(c, "live")
    context = await service.task_context(a)
    assert "## Blocked by" in context
    assert f"- {b} · dev · Payments · {BOARD}/p/acme-shop?task={b}" in context
    assert f"- {c} · live · Receipts · {BOARD}/p/acme-shop?task={c} (no longer blocks)" in context
    blocker = await service.task_context(b)
    assert f"Blocks: {a}" in blocker and "## Blocked by" not in blocker
    # A card without links has neither.
    [d] = await cards(service, "Invoices")
    plain = await service.task_context(d)
    assert "## Blocked by" not in plain and "Blocks:" not in plain
    # A done card it blocks is not listed.
    await service.set_stage(a, "done")
    assert "Blocks:" not in await service.task_context(b)

    call = caller(service)
    await service.set_stage(a, "dev")
    detail = await call("task_get", task=a)
    assert "## Blocked by" in detail and f"- {b} · dev · Payments" in detail
    assert f"- blocks: {a}" in await call("task_get", task=b)


async def test_mcp_task_update_and_task_patch_take_blocked_by(service: Service) -> None:
    a, b, c = await cards(service, "Checkout", "Payments", "Receipts")
    call = caller(service)
    await call("task_update", task=a, blocked_by=[b])
    await call("task_patch", task=a, fields={"blocked_by": [c]})
    assert await blockers_of(service, a) == [b, c]
    with pytest.raises(ToolError, match="no card 'acme-shop-42'"):
        await call("task_update", task=a, blocked_by=["acme-shop-42"])
    assert "blocked_by" in fmt.task_schema()


async def test_the_tool_list_is_unchanged_and_task_update_takes_blocked_by(
    service: Service,
) -> None:
    mcp = build_mcp(service)
    tools = {t.name: t for t in await mcp.list_tools()}
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert set(tools) == set(baseline["tools"])
    update = tools["task_update"].input_schema["properties"]
    assert update["blocked_by"]["anyOf"][0] == {"items": {"type": "string"}, "type": "array"}
    assert "blocked_by" not in tools["task_create"].input_schema["properties"]


def test_the_board_shows_the_chip_and_the_dialog_rows(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    run_in_app(board, service.upsert_project, "demo-api")
    a = run_in_app(board, service.create_task, "acme-shop", "Checkout", stage="dev")
    b = run_in_app(board, service.create_task, "acme-shop", "Payments", stage="review")
    api = run_in_app(board, service.create_task, "demo-api", "Refund endpoint", stage="live")
    run_in_app(board, service.update_task, a.key, {"blocked_by": [b.key, api.key]})

    page = board.get("/p/acme-shop").text
    assert f"blocked by {b.key} · review" in page
    assert f"blocked by {api.key}" not in page  # live: no longer blocks
    card = board.get(f"/p/acme-shop/t/{a.key}/card").text
    assert f'href="/p/acme-shop?task={b.key}" data-task="{b.key}"' in card

    view = board.get(f"/p/acme-shop/t/{a.key}/view").text
    assert "<dt>Blocked by</dt>" in view and f"?task={api.key}" in view
    assert "(no longer blocks)" in view
    blocker = board.get(f"/p/acme-shop/t/{b.key}/view").text
    assert "<dt>Blocks</dt>" in blocker and f"?task={a.key}" in blocker


async def test_a_purged_blocker_exports_and_removes_its_links(tmp_path: Path) -> None:
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    try:
        a, b, c = await cards(service, "Checkout", "Payments", "Receipts")
        await service.update_task(a, {"blocked_by": [b]})
        await service.update_task(b, {"blocked_by": [c]})
        await service.set_stage(b, "done")
        await service.set_archived(b, True, actor="owner")

        [export] = await service.purge_archived(now=datetime.now(UTC) + timedelta(days=31))

        assert export.suffix == ".json"  # no attachments: a plain file
        tables = json.loads(export.read_text(encoding="utf-8"))["tables"]
        ids = {(await service.get_task(k)).id for k in (a, c)}
        rows = {(r["task_id"], r["blocker_id"]) for r in tables["task_block"]}
        assert len(rows) == 2 and all(ids & set(pair) for pair in rows)
        assert await blockers_of(service, a) == []
        _, blocks = await service.task_blocks(await service.get_task(c))
        assert blocks == []
    finally:
        await service.close()
