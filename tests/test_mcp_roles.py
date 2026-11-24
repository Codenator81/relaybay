"""Role endpoints (relaybay-40): `/mcp` stays the full set with its instructions; `/mcp/agent`,
`/mcp/coordinator` and `/mcp/reader` list their role's tools with their own instructions. Claude
Code shows only listed tools, so a role's instructions and the hints an agent can get must name
only tools of that role's list, and every moment of a card has a listed tool."""

import ast
import re
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from relaybay.domain.rules import DEFAULT_RULES, Rules
from relaybay.mcp_server import roles
from relaybay.mcp_server.server import build_mcp, current_tools, instructions
from relaybay.service import Service
from tests.conftest import LiveServer

SRC = Path(__file__).resolve().parents[1] / "src" / "relaybay"
GOLDEN = Path(__file__).parent / "fixtures" / "mcp_instructions_full.txt"


def rules() -> Rules:
    return Rules(DEFAULT_RULES)


async def all_tools(tmp_path: Path) -> set[str]:
    service = Service(tmp_path / "roles.sqlite")
    return {t.name for t in await build_mcp(service).list_tools()}


def named(text: str, names: set[str]) -> set[str]:
    pattern = re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\b")
    return set(pattern.findall(text))


def connect(live: LiveServer, path: str) -> Client:
    http = httpx2.AsyncClient(headers=live.auth, timeout=httpx2.Timeout(30, read=60))
    return Client(streamable_http_client(f"{live.url}{path}", http_client=http), mode="legacy")


async def test_every_role_lists_its_tools_with_its_instructions(
    live_server: Callable[[], LiveServer], tmp_path: Path
) -> None:
    everything = await all_tools(tmp_path)
    live = live_server()
    async with connect(live, "/mcp") as client:
        text = client.instructions
        full = {t.name for t in (await client.list_tools()).tools}
        assert full == everything  # /mcp: unchanged, deprecated names included
        assert text == GOLDEN.read_text(encoding="utf-8")
    for role in roles.ROLES:
        async with connect(live, f"/mcp/{role}") as client:
            text = client.instructions
            listed = {t.name for t in (await client.list_tools()).tools}
            assert listed == roles.role_tools(role, current_tools(everything)), role
            assert text is not None and text.startswith("relaybay is the shared")
            assert text != GOLDEN.read_text(encoding="utf-8")
            assert not listed & {"task_set_stage", "task_patch", "environments_get"}
            # A tool left out of the list still runs when called by name.
            hidden = await client.call_tool("project_list", {}) if role == roles.READER else None
            assert hidden is None or not hidden.is_error


async def test_a_role_endpoint_needs_the_token(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with httpx2.AsyncClient() as http:
        answer = await http.post(f"{live.url}/mcp/agent", json={})
    assert answer.status_code == 401


async def test_role_instructions_name_only_listed_tools(tmp_path: Path) -> None:
    everything = await all_tools(tmp_path)
    current = current_tools(everything)
    for role in roles.ROLES:
        text = roles.role_instructions(rules(), role)
        outside = named(text, everything) - roles.role_tools(role, current)
        assert not outside, f"{role} instructions name tools it does not list: {outside}"
    assert "approval_request" in roles.agent_instructions(rules())


async def test_every_moment_of_a_card_has_a_listed_agent_tool(tmp_path: Path) -> None:
    current = current_tools(await all_tools(tmp_path))
    assert set(roles.AGENT_MOMENTS) >= {
        "start",
        "plan",
        "work",
        "question",
        "decision",
        "finding",
        "report",
        "approval",
        "closing",
    }
    for moment, tools in roles.AGENT_MOMENTS.items():
        assert tools and set(tools) <= current, f"{moment}: {set(tools) - current}"
    text = roles.agent_instructions(rules())
    for moment, tools in roles.AGENT_MOMENTS.items():
        assert named(text, set(tools)), f"the agent instructions name no tool for {moment}"


async def test_hints_in_answers_name_agent_tools(tmp_path: Path) -> None:
    """Every tool name in a string of the service, the domain or the formatter (the texts the
    answers and hooks are made of) is in the agent set, or named for someone else on purpose."""
    everything = await all_tools(tmp_path)
    deprecated = everything - current_tools(everything)
    # The tool descriptions and the contract may name the deprecated tools (their own text and
    # their replacement); the texts answers are made of may not.
    own_texts = {"server.py", "contract.py"}
    files = [
        *(SRC / "domain").glob("*.py"),
        SRC / "service.py",
        SRC / "mcp_server" / "format.py",
        SRC / "mcp_server" / "server.py",
        SRC / "mcp_server" / "contract.py",
    ]
    found: dict[str, set[str]] = {}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for tool in named(node.value, everything):
                    found.setdefault(tool, set()).add(path.name)
    allowed = roles.AGENT_TOOLS | set(roles.NAMED_FOR_OTHERS)
    outside = {
        t
        for t, where in found.items()
        if t not in allowed and not (t in deprecated and where <= own_texts)
    }
    assert not outside, {t: sorted(found[t]) for t in outside}
    assert set(roles.NAMED_FOR_OTHERS) <= everything


def test_the_full_instructions_are_unchanged() -> None:
    """The golden file freezes what `/mcp` sends with the default rules: a user without a
    coordinator sees no change. Rewrite it only on purpose."""
    assert instructions(rules()) == GOLDEN.read_text(encoding="utf-8")


def test_unknown_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        build_mcp(Service(tmp_path / "x.sqlite"), role="owner")
