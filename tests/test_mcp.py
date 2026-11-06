"""End to end through the official SDK client over Streamable HTTP against a real server."""

import itertools
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import LiveServer

EXPECTED_TOOLS = {
    "brake_set",
    "brake_status",
    "budget_get",
    "project_list",
    "project_upsert",
    "project_snapshot",
    "task_list",
    "task_get",
    "task_create",
    "task_update",
    "task_patch",
    "task_set_stage",
    "task_bind_session",
    "task_unbind_session",
    "report_add",
    "review_get",
    "review_steps_set",
    "review_step_update",
    "attach_file",
    "attachment_caption",
    "attachment_get",
    "annotation_reply",
    "decision_add",
    "events_since",
    "audit_list",
    "rules_get",
    "backlog_patch",
    "message_send",
    "messages_list",
    "message_ack",
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
        # The report's size M sized the task: version 3.
        assert "Tests pass." in detail and "- version: 3" in detail

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


async def test_description_progress_and_unbind(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    long_progress = "- cart\n- totals\n" + "- more work " * 30
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("task_create", "task_update", "report_add"):
            props = tools[name].input_schema["properties"]
            assert "keep it current with every report" in props["progress"]["description"]
        assert "description" in tools["task_update"].input_schema["properties"]

        await call(client, "project_upsert", slug="acme-shop")
        await call(
            client,
            "task_create",
            project="acme-shop",
            title="Checkout",
            description="Build the **checkout** page so users can pay.",
            session="checkout-dev",
        )
        report = await call(
            client,
            "report_add",
            task="acme-shop-1",
            body="changed · S · cart",
            progress=long_progress,
            session="checkout-dev",
        )
        assert "(changed · S): cart" in report

        detail = await call(client, "task_get", task="acme-shop-1")
        assert "## Description\nBuild the **checkout** page so users can pay." in detail
        assert f"## Progress (done so far)\n{long_progress.strip()}" in detail
        assert "- version: 2" in detail  # the progress from report_add bumped it

        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert "  about: Build the **checkout** page so users can pay." in snapshot
        done_line = next(line for line in snapshot.splitlines() if line.startswith("  done: "))
        assert done_line.startswith("  done: - cart - totals") and done_line.endswith("…")
        assert len(done_line) <= len("  done: ") + 160

        await call(client, "task_update", task="acme-shop-1", description="")
        assert "## Description" not in await call(client, "task_get", task="acme-shop-1")

        stale = await client.call_tool(
            "task_unbind_session", {"task": "acme-shop-1", "session": "someone-else"}
        )
        assert stale.is_error and "bound to checkout-dev" in stale.content[0].text
        unbound = await call(client, "task_unbind_session", task="acme-shop-1")
        assert "Unbound checkout-dev from **acme-shop-1**" in unbound
        assert "No session is bound" in await call(
            client, "task_unbind_session", task="acme-shop-1"
        )
        detail = await call(client, "task_get", task="acme-shop-1")
        assert "- session:" not in detail and "- checkout-dev — bound" in detail

        events = await call(client, "events_since", since_id=0, project="acme-shop")
        assert "task.session_unbound acme-shop-1 ✕ checkout-dev (mcp)" in events


async def test_alias_tracker_and_dash_clears(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    desktop = "local_00000000-0000-0000-0000-00000000000b"
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        bind_help = tools["task_bind_session"].description or ""
        assert "If you don't know your local_" in bind_help and "coordinator binds you" in bind_help
        update_props = tools["task_update"].input_schema["properties"]
        for name in ("waiting_on", "description", "progress", "tracker_ref", "tracker_url"):
            assert "'-' or '' clears it" in update_props[name]["description"], name
        assert {"tracker_name", "tracker_logged"} <= set(update_props)
        assert {"tracker_ref", "tracker_name", "tracker_url", "tracker_logged"} <= set(
            tools["task_create"].input_schema["properties"]
        )
        assert (
            "'-' or '' clears it"
            in (tools["report_add"].input_schema["properties"]["progress"]["description"])
        )

        await call(client, "project_upsert", slug="acme-shop")
        await call(
            client,
            "task_create",
            project="acme-shop",
            title="Checkout",
            waiting_on="-",
            tracker_name="Linear",
            tracker_ref="ENG-12",
            session=desktop,
            session_title="Checkout work",
        )
        kept = await call(client, "task_bind_session", task="acme-shop-1", session="checkout-dev")
        assert kept == (
            f"Kept the desktop session Checkout work ({desktop}) on **acme-shop-1**;"
            " recorded 'checkout-dev' as its alias. Pass your local_ id to rebind."
        )
        await call(client, "task_create", project="acme-shop", title="Other", session="planner")
        refused = await call(client, "task_bind_session", task="acme-shop-1", session="planner")
        assert refused.startswith(
            f"**acme-shop-1** is already bound to Checkout work ({desktop}); pass your local id"
        )
        mine = await call(client, "task_list", project="acme-shop", session="checkout-dev")
        assert "**acme-shop-1**" in mine
        events = await call(client, "events_since", since_id=0, project="acme-shop")
        assert "session.alias_added acme-shop-1 'checkout-dev' = Checkout work" in events

        await call(
            client,
            "task_update",
            task="acme-shop-1",
            next_step="Ship",
            tracker_url="https://tracker.example/ENG-12",
            tracker_logged=True,
        )
        detail = await call(client, "task_get", task="acme-shop-1")
        assert "- waiting on:" not in detail and "- next step: Ship" in detail
        assert "- tracker: Linear ENG-12 https://tracker.example/ENG-12 · work log ✓" in detail
        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert "tracker: Linear ENG-12 https://tracker.example/ENG-12 · work log ✓" in snapshot

        bad = await client.call_tool(
            "task_update", {"task": "acme-shop-1", "tracker_url": "file:///etc/passwd"}
        )
        assert bad.is_error and "http" in bad.content[0].text

        await call(client, "task_update", task="acme-shop-1", next_step="-", tracker_url=" - ")
        await call(client, "report_add", task="acme-shop-1", body="note", progress="Cart")
        await call(client, "report_add", task="acme-shop-1", body="note", progress="-")
        await call(client, "task_set_stage", task="acme-shop-1", stage="done")
        await call(client, "task_update", task="acme-shop-1", tracker_logged=False)
        detail = await call(client, "task_get", task="acme-shop-1")
        assert "- next step:" not in detail and "## Progress" not in detail
        assert "- tracker: Linear ENG-12 · work log not entered" in detail


async def test_stage_and_report_keep_next_step_current(
    live_server: Callable[[], LiveServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    # "(stale)" compares millisecond timestamps of two writes; a strictly increasing clock keeps
    # a create and a stage move in the same millisecond from tying.
    ticks = itertools.count()
    start = datetime(2026, 11, 5, tzinfo=UTC)
    monkeypatch.setattr(
        "relaybay.service.now_iso",
        lambda: (start + timedelta(milliseconds=next(ticks))).isoformat(timespec="milliseconds"),
    )
    live = live_server()
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        hint = "Also update next_step (what happens next) and waiting_on (who acts next)"
        for name in ("task_set_stage", "report_add", "task_update"):
            assert hint in " ".join((tools[name].description or "").split())
            props = tools[name].input_schema["properties"]
            assert "next_step" in props and "waiting_on" in props

        await call(client, "project_upsert", slug="demo-api")
        await call(
            client, "task_create", project="demo-api", title="Rate limits", next_step="Plan it"
        )
        moved = await call(client, "task_set_stage", task="demo-api-1", stage="dev")
        assert "version 2" in moved
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- next step: Plan it (stale)" in detail
        snapshot = await call(client, "project_snapshot", project="demo-api")
        assert "next: Plan it (stale)" in snapshot

        moved = await call(
            client,
            "task_set_stage",
            task="demo-api-1",
            stage="test",
            next_step="Review the limits",
            waiting_on="reviewer",
        )
        assert "version 3" in moved  # stage and both fields in one write
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- next step: Review the limits" in detail and "(stale)" not in detail
        assert "- waiting on: reviewer" in detail

        await call(
            client, "report_add", task="demo-api-1", body="verified", next_step="-", waiting_on="-"
        )
        detail = await call(client, "task_get", task="demo-api-1")
        assert "next step" not in detail and "waiting on" not in detail
        assert "- version: 4" in detail


async def test_task_patch_and_schema(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="demo-api")
        await call(
            client, "task_create", project="demo-api", title="Rate limits", branch="feat/limits"
        )

        unknown = await client.call_tool(
            "task_patch", {"task": "demo-api-1", "fields": {"owner": "x", "branch": "y"}}
        )
        text = unknown.content[0].text
        assert unknown.is_error and "Unknown task field(s): owner" in text
        assert "Allowed: title, stage," in text and "tracker_logged" in text
        empty = await client.call_tool("task_patch", {"task": "demo-api-1", "fields": {}})
        assert empty.is_error and "Nothing to update" in empty.content[0].text
        bad_type = await client.call_tool(
            "task_patch", {"task": "demo-api-1", "fields": {"tracker_logged": "yes"}}
        )
        assert bad_type.is_error and "true or false" in bad_type.content[0].text

        patched = await call(
            client,
            "task_patch",
            task="demo-api-1",
            fields={"stage": "live", "next_step": "Watch", "branch": "-", "waiting_on": None},
            expected_version=1,
        )
        assert "stage live" in patched and "version 2" in patched
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- stage: live" in detail and "- next step: Watch" in detail
        assert "- branch:" not in detail and "- waiting on:" not in detail

        await call(
            client,
            "task_patch",
            task="demo-api-1",
            fields={"tracker_ref": "ENG-7", "tracker_name": "Linear", "tracker_logged": True},
        )
        moved_back = await call(client, "task_patch", task="demo-api-1", fields={"stage": "dev"})
        assert "stage dev" in moved_back
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- tracker: Linear ENG-7 · work log not entered" in detail  # auto-unlogged
        assert "- Linear ENG-7 — logged " in detail and " · unlogged " in detail
        await call(client, "task_patch", task="demo-api-1", fields={"tracker_logged": True})
        await call(client, "task_patch", task="demo-api-1", fields={"tracker_ref": "ENG-8"})
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- tracker: Linear ENG-8 · work log not entered" in detail  # new issue: not logged
        assert "- Linear ENG-8 — not logged" in detail
        await call(
            client,
            "task_patch",
            task="demo-api-1",
            fields={"tracker_ref": "ENG-9", "tracker_logged": True},
        )
        detail = await call(client, "task_get", task="demo-api-1")
        assert "- tracker: Linear ENG-9 · work log ✓" in detail
        last = detail.rstrip().splitlines()[-1]
        assert last.startswith("Editable fields: title, stage,") and "tracker_logged" in last

        stale = await client.call_tool(
            "task_patch",
            {"task": "demo-api-1", "fields": {"title": "x"}, "expected_version": 1},
        )
        assert stale.is_error and "not 1" in stale.content[0].text
        no_title = await client.call_tool(
            "task_patch", {"task": "demo-api-1", "fields": {"title": "-"}}
        )
        assert no_title.is_error and "title must not be empty" in no_title.content[0].text

        schema = (await client.read_resource("relaybay://schema/task")).contents[0].text
        assert "| `tracker_logged` | boolean |" in schema
        assert "| `stage` | string |" in schema and "cannot be cleared" in schema
        assert schema.count("\n| `") == len(last.split(":", 1)[1].split(","))


async def test_hook_session_links_the_live_status(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    desktop = "local_00000000-0000-0000-0000-00000000000c"
    hook_id = "5e1d0c2b-1111-4222-8333-944455556666"
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in (
            "project_upsert",
            "task_create",
            "task_update",
            "task_patch",
            "task_set_stage",
            "task_bind_session",
            "report_add",
            "decision_add",
        ):
            assert "hook_session" in tools[name].input_schema["properties"], name

        await call(client, "project_upsert", slug="acme-shop")
        await call(
            client,
            "task_create",
            project="acme-shop",
            title="Checkout",
            session=desktop,
            hook_session=hook_id,
        )
    async with httpx2.AsyncClient(headers=live.auth) as http:
        payload = {"hook_event_name": "Notification", "session_id": hook_id, "message": "Hi"}
        response = await http.post(f"{live.url}/hooks/claude-code", json=payload)
        assert response.status_code == 204
    async with connect(live) as client:
        assert "waiting" in await call(client, "task_get", task="acme-shop-1")


async def test_a_long_waiting_on_gets_a_hint_and_is_saved_as_it_is(
    live_server: Callable[[], LiveServer],
) -> None:
    hint = "Hint: waiting_on is who acts next; put what happens into next_step."
    long = "owner: accept the pull request"
    live = live_server()
    async with connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("task_create", "task_update", "task_set_stage", "report_add"):
            help_text = tools[name].input_schema["properties"]["waiting_on"]
            assert "no text after it" in str(help_text)

        await call(client, "project_upsert", slug="demo-api")
        args = {"project": "demo-api", "title": "Rate limits"}
        created = await call(client, "task_create", **args, waiting_on=long)
        assert hint in created and "Warning (rules)" not in created
        task = "demo-api-1"
        writes = [
            ("task_update", {"waiting_on": long}),
            ("task_patch", {"fields": {"waiting_on": long}}),
            ("task_set_stage", {"stage": "plan", "waiting_on": long}),
            ("report_add", {"body": "changed · S · done", "waiting_on": long}),
        ]
        for tool, extra in writes:
            assert hint in await call(client, tool, task=task, **extra), tool
        assert f"- waiting on: {long}" in await call(client, "task_get", task=task)

        # Four words, a clear, or a write without the field: no hint.
        quiet = [
            ("task_update", {"waiting_on": "the release team lead"}),
            ("task_update", {"waiting_on": "-"}),
            ("task_update", {"next_step": "one two three four five six"}),
            ("report_add", {"body": "changed · S · again", "waiting_on": "owner"}),
        ]
        for tool, extra in quiet:
            assert hint not in await call(client, tool, task=task, **extra), tool
