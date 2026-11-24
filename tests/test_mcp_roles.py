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
from mcp.server.mcpserver.exceptions import ToolError

from relaybay.domain.rules import DEFAULT_RULES, Rules
from relaybay.domain.task_fields import FIELD_NAMES, LINK_FIELDS
from relaybay.mcp_server import roles
from relaybay.mcp_server.server import build_mcp, instructions
from relaybay.service import Service
from tests.conftest import LiveServer
from tests.test_mcp import REMOVED_TOOLS

SRC = Path(__file__).resolve().parents[1] / "src" / "relaybay"
GOLDEN = Path(__file__).parent / "fixtures" / "mcp_instructions_full.txt"
# The texts answers and hooks are made of (their string constants).
HINT_FILES = [
    *(SRC / "domain").glob("*.py"),
    SRC / "service.py",
    SRC / "mcp_server" / "format.py",
    SRC / "mcp_server" / "server.py",
    SRC / "mcp_server" / "contract.py",
    SRC / "hook_client.py",
    SRC / "web" / "hooks.py",
]
# Identifiers in those texts that start like a tool name but are not one: settings, rule
# keys, ids and payload fields. Tool parameters and task fields are allowed without a list.
NOT_TOOLS = {
    "annotation_kind",
    "approval_id",
    "attachment_roots",
    "backlog_id",
    "decision_id",
    "decision_text",
    "events_expired",
    "plan_accepted",
    "plan_draft_hint_hours",
    "plan_reminder_every",
    "policy_file",
    "project_id",
    "project_rules",
    "project_slug",
    "project_version",
    "question_id",
    "question_kind",
    "report_check",
    "report_format",
    "report_items",
    "report_marks_from",
    "report_since_stage_change",
    "rules_mode",
    "task_id",
    "task_key",
    "task_session",
}


def rules() -> Rules:
    return Rules(DEFAULT_RULES)


async def all_tools(tmp_path: Path) -> set[str]:
    service = Service(tmp_path / "roles.sqlite")
    return {t.name for t in await build_mcp(service).list_tools()}


async def all_parameters(tmp_path: Path) -> set[str]:
    service = Service(tmp_path / "params.sqlite")
    tools = await build_mcp(service).list_tools()
    return {p for t in tools for p in t.input_schema.get("properties") or {}}


def tool_like(text: str, names: set[str]) -> set[str]:
    """snake_case words that start like a registered tool (`task_`, `plan_`, …)."""
    prefixes = sorted({n.split("_")[0] + "_" for n in names})
    pattern = re.compile(r"\b(?:" + "|".join(prefixes) + r")[a-z0-9_]*[a-z0-9]\b")
    return set(pattern.findall(text))


def hint_texts() -> list[tuple[str, str]]:
    return [
        (path.name, node.value)
        for path in HINT_FILES
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


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
        assert full == everything  # /mcp: the full set
        assert text == GOLDEN.read_text(encoding="utf-8")
    for role in roles.ROLES:
        async with connect(live, f"/mcp/{role}") as client:
            text = client.instructions
            listed = {t.name for t in (await client.list_tools()).tools}
            assert listed == roles.role_tools(role, everything), role
            assert text is not None and text.startswith("relaybay is the shared")
            assert text != GOLDEN.read_text(encoding="utf-8")
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
    for role in roles.ROLES:
        for coordinated in (False, True):
            text = roles.role_instructions(rules(), role, coordinated)
            outside = named(text, everything) - roles.role_tools(role, everything)
            assert not outside, f"{role} instructions name tools it does not list: {outside}"
    assert "approval_request" in roles.agent_instructions(rules())


async def test_every_moment_of_a_card_has_a_listed_agent_tool(tmp_path: Path) -> None:
    current = await all_tools(tmp_path)
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
    """Every tool name in a string of the service, the domain, the formatter or the hooks is in
    the agent set, or named for someone else on purpose."""
    everything = await all_tools(tmp_path)
    found: dict[str, set[str]] = {}
    for where, text in hint_texts():
        for tool in named(text, everything):
            found.setdefault(tool, set()).add(where)
    allowed = roles.AGENT_TOOLS | set(roles.NAMED_FOR_OTHERS)
    outside = {t for t in found if t not in allowed}
    assert not outside, {t: sorted(found[t]) for t in outside}
    assert set(roles.NAMED_FOR_OTHERS) <= everything


async def test_no_text_names_a_tool_that_does_not_exist(tmp_path: Path) -> None:
    """The instructions of every role and of `/mcp`, and every hint text, name only registered
    tools: a removed or misspelt name is caught here."""
    everything = await all_tools(tmp_path)
    known = (
        everything
        | await all_parameters(tmp_path)
        | set(FIELD_NAMES)
        | {f.name for f in LINK_FIELDS}
        | NOT_TOOLS
    )
    texts = [(f"/mcp coordinated={c}", instructions(rules(), c)) for c in (False, True)]
    texts += [
        (f"{role} coordinated={c}", roles.role_instructions(rules(), role, c))
        for role in roles.ROLES
        for c in (False, True)
    ]
    texts += hint_texts()
    unknown: dict[str, set[str]] = {}
    # Removed names are matched as whole words too: their prefix may belong to no tool now.
    for where, text in texts:
        for word in (tool_like(text, everything) - known) | named(text, REMOVED_TOOLS):
            unknown.setdefault(word, set()).add(where)
        if "/environments" in text:
            unknown.setdefault("relaybay://project/{slug}/environments", set()).add(where)
    assert not unknown, {w: sorted(unknown[w]) for w in unknown}
    assert NOT_TOOLS.isdisjoint(everything)


async def test_removed_tools_are_unknown(tmp_path: Path) -> None:
    mcp = build_mcp(Service(tmp_path / "gone.sqlite"))
    for name, args in (
        ("task_set_stage", {"task": "acme-shop-1", "stage": "dev"}),
        ("task_patch", {"task": "acme-shop-1", "fields": {"priority": 1}}),
    ):
        with pytest.raises(ToolError, match=f"^Unknown tool: {name}$"):
            await mcp.call_tool(name, args)


def test_the_full_instructions_are_unchanged() -> None:
    """The golden file freezes what `/mcp` sends with the default rules: a user without a
    coordinator sees no change. Rewrite it only on purpose."""
    assert instructions(rules()) == GOLDEN.read_text(encoding="utf-8")


def test_unknown_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown role"):
        build_mcp(Service(tmp_path / "x.sqlite"), role="owner")
