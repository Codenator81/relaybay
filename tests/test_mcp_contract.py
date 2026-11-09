"""The versioned MCP contract: the checker on synthetic schemas, the deprecation marks, and the
live server against the committed baseline."""

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import pytest
from mcp.server import MCPServer
from pydantic import Field

import relaybay
from relaybay.domain.rules import DEFAULT_RULES, Rules
from relaybay.mcp_server import contract
from relaybay.mcp_server.contract import Deprecation, breaking_changes, uncovered
from relaybay.mcp_server.server import build_mcp, instructions
from relaybay.service import Service
from tests.test_release import load_script

BASELINE = Path(__file__).parent / "fixtures" / "mcp_contract_baseline.json"
OPTIONAL_TEXT = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def surface(**tools: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "version": "0.8.0",
        "tools": {
            name: {"input": schema, "annotations": {"read_only_hint": True}}
            for name, schema in tools.items()
        },
        "resources": ["relaybay://rules"],
        "resource_templates": ["relaybay://project/{slug}"],
        "prompts": {"start_task": {"arguments": {"project": True, "task": False}}},
    }


BEFORE = surface(
    task_list={
        "type": "object",
        "properties": {
            "project": OPTIONAL_TEXT,
            "stage": {"type": "string", "enum": ["plan", "dev", "done"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
    },
    task_get={"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
)


def changed(edit: Any) -> list[str]:
    after = copy.deepcopy(BEFORE)
    edit(after)
    return [c.text for c in breaking_changes(BEFORE, after)]


def props(s: dict[str, Any]) -> dict[str, Any]:
    return s["tools"]["task_list"]["input"]["properties"]


# --- the checker ----------------------------------------------------------------


def test_an_unchanged_surface_has_no_breaking_change() -> None:
    assert breaking_changes(BEFORE, copy.deepcopy(BEFORE)) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda s: s["tools"].pop("task_get"), "tool `task_get` removed"),
        (lambda s: props(s).pop("limit"), "task_list: parameter `limit` removed"),
        (
            lambda s: props(s).update(project={"type": "string"}),
            "task_list: `project` no longer accepts null",
        ),
        (
            lambda s: props(s).update(limit={"type": "string"}),
            "task_list: `limit` no longer accepts integer",
        ),
        (
            lambda s: props(s)["stage"].update(enum=["plan", "dev"]),
            "task_list: `stage` no longer accepts 'done'",
        ),
        (
            lambda s: props(s)["tags"]["items"].update(enum=["a"]),
            "task_list: `tags`[] gained an enum",
        ),
        (
            lambda s: props(s)["limit"].update(maximum=100),
            "task_list: `limit` maximum 500 -> 100",
        ),
        (
            lambda s: props(s)["tags"].update(items={"type": "integer"}),
            "task_list: `tags`[] no longer accepts string",
        ),
        (
            lambda s: s["tools"]["task_list"]["input"].update(required=["stage"]),
            "task_list: parameter `stage` became required",
        ),
        (
            lambda s: (
                props(s).update(scope={"type": "string"}),
                s["tools"]["task_list"]["input"].update(required=["scope"]),
            ),
            "task_list: parameter `scope` added as required",
        ),
        (
            lambda s: s["tools"]["task_get"].update(annotations={"read_only_hint": False}),
            "task_get: was read-only, now writes",
        ),
        (lambda s: s["resources"].clear(), "resource `relaybay://rules` removed"),
        (
            lambda s: s["resource_templates"].clear(),
            "resource `relaybay://project/{slug}` removed",
        ),
        (lambda s: s["prompts"].clear(), "prompt `start_task` removed"),
        (
            lambda s: s["prompts"]["start_task"]["arguments"].pop("task"),
            "prompt start_task: argument `task` removed",
        ),
        (
            lambda s: s["prompts"]["start_task"]["arguments"].update(task=True),
            "prompt start_task: argument `task` became required",
        ),
    ],
)
def test_a_breaking_change_is_found(edit: Any, expected: str) -> None:
    assert changed(edit) == [expected]


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s["tools"].update(task_new={"input": {"type": "object"}, "annotations": {}}),
        lambda s: props(s).update(new_optional=OPTIONAL_TEXT),
        lambda s: props(s).update(limit={"anyOf": [{"type": "integer"}, {"type": "null"}]}),
        lambda s: props(s).update(limit={"type": "number"}),
        lambda s: props(s)["stage"].update(enum=["plan", "dev", "done", "live"]),
        lambda s: props(s)["stage"].pop("enum"),
        lambda s: props(s)["limit"].update(maximum=1000, minimum=0),
        lambda s: props(s).update(tags={}),
        lambda s: s["tools"]["task_get"]["input"].update(required=[]),
        lambda s: s["tools"]["task_list"].update(annotations={"read_only_hint": True, "x": 1}),
        lambda s: s["resources"].append("relaybay://contract"),
        lambda s: s["prompts"]["start_task"]["arguments"].update(extra=False),
        lambda s: s.update(prompts={**s["prompts"], "finish_stage": {"arguments": {}}}),
    ],
)
def test_an_addition_or_a_widening_passes(edit: Any) -> None:
    assert changed(edit) == []


def test_normalize_drops_descriptions_and_resolves_refs() -> None:
    schema = {
        "type": "object",
        "title": "x",
        "properties": {
            "kind": {"$ref": "#/$defs/Kind", "description": "d", "default": "a"},
            "n": {"type": "integer", "title": "N", "maximum": 5},
        },
        "required": ["n", "kind"],
        "$defs": {"Kind": {"type": "string", "enum": ["a", "b"], "title": "Kind"}},
    }
    assert contract.normalize(schema) == {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["a", "b"]},
            "n": {"type": "integer", "maximum": 5},
        },
        "required": ["kind", "n"],
    }


# --- deprecations -----------------------------------------------------------------

FAKE = Deprecation(
    tool="task_get",
    parameter=None,
    since="0.8.0",
    remove_not_before=date(2026, 12, 15),
    replacement="task_context",
)


def removed_tool() -> list[contract.Change]:
    after = copy.deepcopy(BEFORE)
    after["tools"].pop("task_get")
    return breaking_changes(BEFORE, after)


def test_a_removal_needs_an_expired_deprecation(monkeypatch: pytest.MonkeyPatch) -> None:
    changes = removed_tool()
    assert uncovered(changes, "0.9.0", date(2027, 1, 1)) == changes
    monkeypatch.setattr(contract, "DEPRECATIONS", [FAKE])
    assert uncovered(changes, "0.9.0", date(2026, 12, 14)) == changes, "before the date"
    assert uncovered(changes, "0.8.3", date(2027, 1, 1)) == changes, "same minor version"
    assert uncovered(changes, "0.9.0", date(2026, 12, 15)) == []


def test_a_parameter_deprecation_covers_only_that_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = Deprecation("task_list", "limit", "0.8.0", date(2026, 12, 15), None)
    monkeypatch.setattr(contract, "DEPRECATIONS", [entry])
    after = copy.deepcopy(BEFORE)
    props(after).pop("limit")
    props(after).pop("tags")
    left = uncovered(breaking_changes(BEFORE, after), "0.9.0", date(2027, 1, 1))
    assert [c.text for c in left] == ["task_list: parameter `tags` removed"]


async def test_deprecated_marks_the_description_and_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whole = Deprecation("old_tool", None, "0.8.0", date(2026, 12, 15), "new_tool")
    param = Deprecation("old_param", "mode", "0.8.0", date(2026, 12, 15), None)
    monkeypatch.setattr(contract, "DEPRECATIONS", [whole, param])
    mcp = MCPServer(name="test")

    @mcp.tool(structured_output=False)
    @contract.deprecated()
    async def old_tool(task: Annotated[str, Field(description="Task key.")]) -> str:
        """Read a task."""
        return f"task {task}"

    @mcp.tool(structured_output=False)
    @contract.deprecated("mode")
    async def old_param(task: str, mode: str | None = None) -> str:
        """Change a task."""
        return "done"

    notice = (
        "Deprecated: old_tool is deprecated since 0.8.0 and may be removed in a later minor"
        " version, not before 2026-12-15; use new_tool instead."
    )
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["old_tool"].description == f"Read a task.\n\n{notice}"
    assert tools["old_tool"].input_schema["required"] == ["task"]
    assert tools["old_tool"].input_schema["properties"]["task"]["description"] == "Task key."
    assert "parameter `mode` of old_param is deprecated" in tools["old_param"].description

    def text(result: Any) -> str:
        return "\n".join(c.text for c in result.content)

    assert text(await mcp.call_tool("old_tool", {"task": "a-1"})) == f"task a-1\n{notice}"
    assert text(await mcp.call_tool("old_param", {"task": "a-1"})) == "done"
    passed = text(await mcp.call_tool("old_param", {"task": "a-1", "mode": "x"}))
    assert passed.startswith("done\nDeprecated: parameter `mode` of old_param is deprecated")


def test_deprecated_needs_an_entry() -> None:
    async def nowhere() -> str:
        return ""

    with pytest.raises(LookupError):
        contract.deprecated()(nowhere)


# --- the live server ------------------------------------------------------------


async def live_surface(tmp_path: Path) -> dict[str, Any]:
    mcp = build_mcp(Service(tmp_path / "relaybay.sqlite"))
    return contract.snapshot(
        relaybay.__version__,
        await mcp.list_tools(),
        await mcp.list_resources(),
        await mcp.list_resource_templates(),
        await mcp.list_prompts(),
    )


def baseline() -> dict[str, Any]:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


async def test_the_server_keeps_the_baseline_contract(tmp_path: Path) -> None:
    old, new = baseline(), await live_surface(tmp_path)
    changes = breaking_changes(old, new)
    left = uncovered(changes, relaybay.__version__, datetime.now(UTC).date())
    assert not left, "Breaking MCP changes (see docs/SPEC.md#spec-mcp-compat):\n" + "\n".join(
        c.text for c in left
    )
    assert old["contract_version"] <= contract.CONTRACT_VERSION
    if changes:
        assert old["contract_version"] < contract.CONTRACT_VERSION, (
            "Expired deprecations removed: raise CONTRACT_VERSION."
        )


async def test_every_deprecation_is_marked_on_the_server(tmp_path: Path) -> None:
    mcp = build_mcp(Service(tmp_path / "relaybay.sqlite"))
    tools = {t.name: t for t in await mcp.list_tools()}
    for entry in contract.DEPRECATIONS:
        if entry.tool in tools:
            assert entry.notice() in (tools[entry.tool].description or ""), entry


async def test_the_contract_resource_and_the_instructions(tmp_path: Path) -> None:
    mcp = build_mcp(Service(tmp_path / "relaybay.sqlite"))
    text = next(iter(await mcp.read_resource("relaybay://contract"))).content
    assert text.startswith(f"# relaybay MCP contract\n\nServer {relaybay.__version__}.\n\n")
    assert f"Contract version {contract.CONTRACT_VERSION}." in text
    assert contract.RULE in text and "Deprecations: none." in text
    assert "(contract v1: relaybay://contract)" in instructions(Rules(DEFAULT_RULES))


def test_rebaseline_refuses_a_breaking_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gen = load_script("gen_reference")
    stale = baseline()
    stale["tools"]["gone_tool"] = {"input": {"type": "object"}, "annotations": {}}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(gen, "BASELINE", path)
    assert gen.rebaseline() == 1
    assert "- tool `gone_tool` removed" in capsys.readouterr().err
    assert json.loads(path.read_text(encoding="utf-8")) == stale
