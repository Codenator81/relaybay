"""End to end through the official SDK client over Streamable HTTP against a real server."""

from collections.abc import Callable

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import LiveServer

EXPECTED_TOOLS = {
    "project_list",
    "project_upsert",
    "project_snapshot",
    "task_list",
    "task_get",
    "task_create",
    "task_update",
    "task_set_stage",
    "task_bind_session",
    "report_add",
    "decision_add",
    "events_since",
}


def connect(live: LiveServer, mode: str = "legacy") -> Client:
    http = httpx2.AsyncClient(headers=live.auth, timeout=httpx2.Timeout(30, read=60))
    return Client(streamable_http_client(f"{live.url}/mcp", http_client=http), mode=mode)


async def call(client: Client, tool: str, **arguments: object) -> str:
    result = await client.call_tool(tool, arguments)
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    assert not result.is_error, text
    return text


async def test_full_flow(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with connect(live) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert tools >= EXPECTED_TOOLS

        assert "Created project **acme-shop**" in await call(
            client, "project_upsert", slug="acme-shop", name="Acme Shop", session="checkout-dev"
        )
        created = await call(
            client,
            "task_create",
            project="acme-shop",
            title="Checkout page",
            branch="feat/checkout",
            session="checkout-dev",
        )
        assert "**acme-shop-1**" in created

        moved = await call(
            client,
            "task_set_stage",
            task="acme-shop-1",
            stage="dev",
            note="coding",
            session="checkout-dev",
        )
        assert "stage dev" in moved and "version 2" in moved

        report = await call(
            client,
            "report_add",
            task="acme-shop-1",
            body="verified · M · checkout works\n\nTests pass.",
            session="checkout-dev",
        )
        assert "(verified · M): checkout works" in report

        await call(
            client, "decision_add", project="acme-shop", text="No coupons in v1", task="acme-shop-1"
        )

        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert "# acme-shop — Acme Shop" in snapshot
        assert "## dev" in snapshot and "**acme-shop-1** Checkout page" in snapshot
        assert "session: checkout-dev" in snapshot
        assert "last report: verified · M · checkout works" in snapshot
        assert "No coupons in v1" in snapshot

        resource = await client.read_resource("relaybay://project/acme-shop")
        assert "**acme-shop-1**" in resource.contents[0].text

        listing = await call(client, "task_list", project="acme-shop", stage="dev")
        assert "acme-shop-1" in listing
        detail = await call(client, "task_get", task="acme-shop-1")
        assert "Tests pass." in detail and "- version: 2" in detail

        events = await call(client, "events_since", since_id=0, project="acme-shop")
        assert "task.stage_changed acme-shop-1 plan → dev (checkout-dev)" in events
        assert "Next call: events_since(since_id=" in events

        projects = await call(client, "project_list")
        assert "| acme-shop | Acme Shop | 0 | 1 | 0 | 0 | 0 |" in projects


async def test_errors_are_tool_errors(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with connect(live) as client:
        missing = await client.call_tool("task_get", {"task": "nope-1"})
        assert missing.is_error and "not found" in missing.content[0].text

        await call(client, "project_upsert", slug="demo-api")
        await call(client, "task_create", project="demo-api", title="Rate limits")
        await call(client, "task_update", task="demo-api-1", waiting_on="owner", expected_version=1)
        stale = await client.call_tool(
            "task_update", {"task": "demo-api-1", "next_step": "x", "expected_version": 1}
        )
        assert stale.is_error and "version 2, not 1" in stale.content[0].text

        bad_stage = await client.call_tool("task_set_stage", {"task": "demo-api-1", "stage": "qa"})
        assert bad_stage.is_error and "Unknown stage" in bad_stage.content[0].text


async def test_auto_protocol_negotiation(live_server: Callable[[], LiveServer]) -> None:
    """Claude Code speaks the initialize handshake (legacy above); newer clients probe first."""
    live = live_server()
    async with connect(live, mode="auto") as client:
        assert "No projects yet" in await call(client, "project_list")


async def test_desktop_session_binding(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    desktop = "local_00000000-0000-0000-0000-00000000000a"
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop")
        await call(client, "task_create", project="acme-shop", title="Checkout", session="planner")
        bound = await call(
            client,
            "task_bind_session",
            task="acme-shop-1",
            session=desktop,
            session_title="Checkout work",
        )
        assert f"Bound Checkout work ({desktop}) to **acme-shop-1**" in bound

        detail = await call(client, "task_get", task="acme-shop-1")
        assert f"- session: Checkout work ({desktop})" in detail
        assert f"- open in desktop: claude://claude.ai/epitaxy/{desktop}" in detail
        assert "## Sessions (newest first)" in detail
        assert detail.index("Checkout work (") < detail.index("- planner")

        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert f"session: Checkout work ({desktop})" in snapshot

        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["task_bind_session"].input_schema["properties"]
        assert "local_" in schema["session"]["description"] and "session_title" in schema
