"""An argument a tool does not take (backlog #262): the SDK would drop it without a word, so a
write with one is refused before it runs and a read names it in its answer. Every parameter of
the released contract (the 0.16 baseline: old task_update shapes, task_set_stage, task_patch,
the environment stubs) stays known."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from relaybay.mcp_server.server import build_mcp, unknown_arguments
from relaybay.service import Service
from tests.conftest import LiveServer

BASELINE = Path(__file__).parent / "fixtures" / "mcp_contract_baseline.json"


def connect(live: LiveServer, path: str = "/mcp") -> Client:
    http = httpx2.AsyncClient(headers=live.auth, timeout=httpx2.Timeout(30, read=60))
    return Client(streamable_http_client(f"{live.url}{path}", http_client=http), mode="legacy")


def text_of(result: object) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))  # type: ignore[attr-defined]


async def test_every_released_parameter_is_still_known(tmp_path: Path) -> None:
    released = json.loads(BASELINE.read_text(encoding="utf-8"))["tools"]
    live = {t.name: t for t in await build_mcp(Service(tmp_path / "u.sqlite")).list_tools()}
    for name, spec in released.items():
        assert name in live, name
        params = dict.fromkeys(spec["input"].get("properties", {}), 1)
        assert unknown_arguments(live[name].input_schema, params) == [], name
    assert {"task_set_stage", "task_patch", "task_update"} <= set(released)


async def test_a_write_with_an_unknown_argument_changes_nothing(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool("task_create", {"project": "acme-shop", "title": "Checkout"})
        bad = await client.call_tool(
            "task_update", {"task": "acme-shop-1", "next_step": "Cart", "nxt_stage": "x"}
        )
        message = text_of(bad)
        assert bad.is_error
        assert "Unknown parameter(s) for task_update: nxt_stage. Nothing changed." in message
        assert "next_step" in message and "reconnect" in message
        detail = text_of(await client.call_tool("task_get", {"task": "acme-shop-1"}))
        assert "Cart" not in detail and "version: 1" in detail
        # Not a create either: a retry with the typo fixed makes one card, not two.
        bad = await client.call_tool(
            "task_create", {"project": "acme-shop", "title": "Cart", "colour": "red"}
        )
        assert bad.is_error and "colour" in text_of(bad)
        listed = text_of(await client.call_tool("task_list", {"project": "acme-shop"}))
        assert "acme-shop-2" not in listed


async def test_a_read_with_an_unknown_argument_answers_and_names_it(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with connect(live) as client:
        answer = await client.call_tool("project_list", {"verbose": True})
        assert not answer.is_error
        assert text_of(answer).endswith("Ignored unknown parameter(s): verbose.")
        plain = text_of(await client.call_tool("project_list", {}))
        assert "Ignored" not in plain


async def test_deprecated_tools_and_old_shapes_still_work(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with connect(live) as client:
        await client.call_tool("project_upsert", {"slug": "acme-shop"})
        await client.call_tool("task_create", {"project": "acme-shop", "title": "Checkout"})
        released = json.loads(BASELINE.read_text(encoding="utf-8"))["tools"]
        # task_set_stage with every parameter it had in 0.16.
        stage_args = {
            "task": "acme-shop-1",
            "stage": "dev",
            "note": "coding",
            "next_step": "Cart",
            "waiting_on": "session",
            "session": "dev",
            "session_title": "Dev",
            "hook_session": None,
        }
        assert set(stage_args) <= set(released["task_set_stage"]["input"]["properties"])
        moved = await client.call_tool("task_set_stage", stage_args)
        assert not moved.is_error and "Deprecated: task_set_stage" in text_of(moved)
        patched = await client.call_tool(
            "task_patch", {"task": "acme-shop-1", "fields": {"priority": 2}, "session": "dev"}
        )
        assert not patched.is_error and "Deprecated: task_patch" in text_of(patched)
        # task_update as a 0.16 client sends it: no stage, no fields.
        old_shape = {
            k: v
            for k, v in {
                "task": "acme-shop-1",
                "title": "Checkout page",
                "waiting_on": "owner",
                "tracker_logged": False,
                "expected_version": None,
                "session": "dev",
            }.items()
            if k in released["task_update"]["input"]["properties"]
        }
        assert "stage" not in released["task_update"]["input"]["properties"]
        updated = await client.call_tool("task_update", old_shape)
        assert not updated.is_error, text_of(updated)
        stub = await client.call_tool("environments_get", {"project": "acme-shop"})
        assert not stub.is_error
