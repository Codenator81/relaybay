"""The day time log over MCP (relaybay-42): timelog_mark, timelog_list, timelog_fix and
worklog_add through the SDK client against a real server, and what survives a purge. The server
uses the machine's own zone, so the marks here are written for yesterday with explicit times."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from relaybay.mcp_server import roles
from relaybay.service import Service
from tests.conftest import LiveServer
from tests.test_mcp_unknown_args import connect, text_of

TOOLS = {"timelog_mark", "timelog_list", "timelog_fix", "worklog_add"}
REFUSED = "The day time log is the coordinator's. Nothing changed."


def yesterday() -> date:
    return datetime.now().astimezone().date() - timedelta(days=1)


def local(day: date, hour: int, minute: int = 0) -> str:
    """An ISO time with the machine's offset on that day."""
    return datetime(day.year, day.month, day.day, hour, minute).astimezone().isoformat()


async def test_a_day_with_a_switch_and_a_pause_and_a_new_client_sees_it(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    day = yesterday()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool(
            "task_create", {"project": "acme-shop", "title": "Checkout", "session": "worker"}
        )
        first = await client.call_tool(
            "timelog_mark",
            {"kind": "start", "task": "acme-shop-1", "at": local(day, 9), "session": "worker"},
        )
        assert not first.is_error, text_of(first)
        assert text_of(first).startswith("Marked #1 start at 09:00: acme-shop-1.")
        switched = text_of(
            await client.call_tool(
                "timelog_mark", {"kind": "switch", "label": "call", "at": local(day, 10)}
            )
        )
        assert "Closed: acme-shop-1 09:00-10:00 1h00." in switched
        paused = text_of(
            await client.call_tool("timelog_mark", {"kind": "pause", "at": local(day, 10, 30)})
        )
        assert "Closed: call 10:00-10:30 0h30." in paused and "total 1h30" in paused
        listed = text_of(await client.call_tool("timelog_list", {"day": day.isoformat()}))
    assert f"**Time log {day.isoformat()}**" in listed and "total **1h30**" in listed
    assert "- 09:00-10:00  1h00  acme-shop-1 (acme-shop) (#1 start)" in listed
    assert "- 10:00-10:30  0h30  call (#2 switch)" in listed
    assert "- 10:30  pause (#3)" in listed
    assert "By project: acme-shop 1h00 · labels 0h30" in listed
    async with connect(live, "/mcp/coordinator") as other:
        again = text_of(await other.call_tool("timelog_list", {"day": "yesterday"}))
        card = text_of(await other.call_tool("timelog_list", {"task": "acme-shop-1"}))
        project = text_of(
            await other.call_tool("timelog_list", {"project": "acme-shop", "day": "yesterday"})
        )
    assert again == listed
    assert "**acme-shop-1** time: today 0h00 · total 1h00 · in tracker 0h00" in card
    assert f"Days: {day.isoformat()} 1h00" in card
    assert f"On {day.isoformat()}: 1h00\n- acme-shop-1 1h00" in project


async def test_the_same_mark_twice_is_one_row(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    day = yesterday()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool("task_create", {"project": "acme-shop", "title": "Checkout"})
        args = {"kind": "start", "task": "acme-shop-1", "at": local(day, 9)}
        await client.call_tool("timelog_mark", args)
        again = text_of(await client.call_tool("timelog_mark", {**args, "at": local(day, 9, 1)}))
        listed = text_of(await client.call_tool("timelog_list", {"day": day.isoformat()}))
    assert again.startswith("Nothing written. Already marked: #1 at 09:00.")
    assert "#2" not in listed


async def test_coordinator_mode_refuses_an_agents_mark(
    tmp_path: Path, live_server: Callable[[], LiveServer]
) -> None:
    (tmp_path / "config.toml").write_text('coordinators = ["lead"]\n', encoding="utf-8")
    live = live_server()
    async with connect(live, "/mcp/coordinator") as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop", "session": "lead"})
    async with connect(live, "/mcp/agent") as agent:
        created = await agent.call_tool(
            "task_create", {"project": "acme-shop", "title": "Mine", "session": "worker"}
        )
        assert not created.is_error
        for tool, args in (
            ("timelog_mark", {"kind": "start", "task": "acme-shop-1"}),
            ("timelog_fix", {"mark": 1, "reason": "typo", "delete": True}),
            ("worklog_add", {"task": "acme-shop-1", "minutes": 30}),
        ):
            refused = await agent.call_tool(tool, {**args, "session": "worker"})
            assert refused.is_error and REFUSED in text_of(refused), tool
    async with connect(live, "/mcp/coordinator") as client:
        empty = text_of(await client.call_tool("timelog_list", {}))
        assert "No marks on this day." in empty
        done = await client.call_tool(
            "timelog_mark", {"kind": "start", "task": "acme-shop-1", "session": "lead"}
        )
        assert not done.is_error and text_of(done).startswith("Marked #1 start")


async def test_only_the_coordinator_and_the_full_set_list_the_tools(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    for path, listed in (
        ("/mcp", True),
        ("/mcp/coordinator", True),
        ("/mcp/agent", False),
        ("/mcp/reader", False),
    ):
        async with connect(live, path) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            text = client.instructions or ""
        assert (names >= TOOLS) is listed and (not TOOLS & names) is not listed, path
        assert ("timelog_mark" in text) is (path == "/mcp/coordinator"), path
    assert not TOOLS & (roles.AGENT_TOOLS | roles.READER_TOOLS)


async def test_a_fix_and_a_removal_keep_before_and_reason_on_the_audit(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    day = yesterday()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool("task_create", {"project": "acme-shop", "title": "Checkout"})
        await client.call_tool(
            "timelog_mark", {"kind": "start", "task": "acme-shop-1", "at": local(day, 9)}
        )
        await client.call_tool("timelog_mark", {"kind": "pause", "at": local(day, 10)})
        no_reason = await client.call_tool("timelog_fix", {"mark": 1, "at": local(day, 8)})
        assert no_reason.is_error
        blank = await client.call_tool("timelog_fix", {"mark": 1, "reason": " ", "at": "08:00"})
        assert blank.is_error and "needs a reason" in text_of(blank)
        fixed = text_of(
            await client.call_tool(
                "timelog_fix", {"mark": 1, "reason": "started earlier", "at": local(day, 8, 30)}
            )
        )
        assert fixed.startswith(
            "Mark #1 fixed: 09:00 start acme-shop-1 -> 08:30 start acme-shop-1 (started earlier)."
        )
        assert "total **1h30**" in fixed
        relabelled = text_of(
            await client.call_tool(
                "timelog_fix", {"mark": 1, "reason": "it was a call", "label": "call"}
            )
        )
        assert "08:30 start acme-shop-1 -> 08:30 start call" in relabelled
        # HH:MM in a fix is on the mark's own day (yesterday), not today.
        clock = text_of(
            await client.call_tool("timelog_fix", {"mark": 1, "reason": "later", "at": "08:45"})
        )
        assert "08:30 start call -> 08:45 start call" in clock
        assert f"**Time log {day.isoformat()}**" in clock and "total **1h15**" in clock
        removed = text_of(
            await client.call_tool(
                "timelog_fix", {"mark": 2, "reason": "forgot to stop", "delete": True}
            )
        )
        assert removed.startswith("Mark removed (was 10:00 pause): forgot to stop.")
        # A past day's hint names its date: HH:MM alone would land on today.
        assert f"timelog_mark(kind='pause', at='{day.isoformat()}THH:MM')" in removed
        both = await client.call_tool(
            "timelog_list", {"task": "acme-shop-1", "project": "acme-shop"}
        )
        assert both.is_error and "Pass task or project, not both" in text_of(both)
    async with httpx.AsyncClient(base_url=live.url, follow_redirects=True) as http:
        await http.get(f"/login?token={live.token}")
        page = (await http.get("/audit?changes=1")).text
    assert "timelog.fixed" in page and "timelog.removed" in page
    assert "09:00 start acme-shop-1 -&gt; 08:30 start acme-shop-1: started earlier" in page
    assert "#2 10:00 pause removed: forgot to stop" in page


async def test_worklog_with_a_correction_and_no_day_below_zero(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool(
            "task_create", {"project": "acme-shop", "title": "Checkout", "tracker_ref": "ENG-12"}
        )
        first = text_of(
            await client.call_tool(
                "worklog_add", {"task": "acme-shop-1", "minutes": 90, "ref": "ENG-12"}
            )
        )
        assert ": 1h30. In tracker 1h30" in first
        no_reason = await client.call_tool("worklog_add", {"task": "acme-shop-1", "minutes": -30})
        assert no_reason.is_error and "give the reason" in text_of(no_reason)
        fixed = text_of(
            await client.call_tool(
                "worklog_add", {"task": "acme-shop-1", "minutes": -30, "reason": "double entry"}
            )
        )
        assert ": -0h30. In tracker 1h00" in fixed
        below = await client.call_tool(
            "worklog_add", {"task": "acme-shop-1", "minutes": -61, "reason": "too much"}
        )
        assert below.is_error and "would take it below 0. Nothing changed." in text_of(below)
        tomorrow = (yesterday() + timedelta(days=2)).isoformat()
        ahead = await client.call_tool(
            "worklog_add", {"task": "acme-shop-1", "minutes": 30, "day": tomorrow}
        )
        assert ahead.is_error and f"day {tomorrow} is after today" in text_of(ahead)
        card = text_of(await client.call_tool("timelog_list", {"task": "acme-shop-1"}))
        task = text_of(await client.call_tool("task_get", {"task": "acme-shop-1"}))
    assert "in tracker 1h00" in card and "-0h30 (double entry) by mcp" in card
    # tracker_logged stays the separate mark: the minutes do not set it.
    assert "ENG-12 · work log not entered" in task


async def test_a_purged_cards_marks_stay_on_their_day(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        await svc.upsert_project("acme-shop")
        task = await svc.create_task("acme-shop", "Receipts", stage="dev")
        day = yesterday()
        await svc.mark_time("start", task=task.key, at=local(day, 9))
        await svc.mark_time("pause", at=local(day, 9, 45))
        await svc.add_worklog(task.key, 45)
        await svc.set_stage(task.key, "done")
        await svc.set_archived(task.key, True, actor="owner")
        [export] = await svc.purge_archived(now=datetime.now(UTC) + timedelta(days=31))
        # No attachments: the export is plain JSON.
        tables = json.loads(export.read_text(encoding="utf-8"))["tables"]
        assert "time_mark" not in tables and "tracker_worklog" not in tables
        log = await svc.day_log(day)
        assert log.by_card == [(task.key, "acme-shop", 45 * 60)]
        assert log.segments[0].task_id is None and log.segments[0].key == task.key
    finally:
        await svc.close()
