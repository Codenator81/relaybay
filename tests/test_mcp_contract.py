"""The versioned MCP contract: the checker on synthetic schemas, the contract version that lets
a breaking change through, and the live server against the committed baseline."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import relaybay
from relaybay.domain.rules import DEFAULT_RULES, Rules
from relaybay.mcp_server import contract
from relaybay.mcp_server.contract import breaking_changes
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


# --- the contract version ---------------------------------------------------------


def removed_tool(contract_version: int) -> tuple[dict[str, Any], dict[str, Any]]:
    old = {**copy.deepcopy(BEFORE), "contract_version": contract_version}
    new = copy.deepcopy(old)
    new["tools"].pop("task_get")
    return old, new


def test_a_breaking_change_needs_a_higher_contract_version() -> None:
    old, new = removed_tool(contract.CONTRACT_VERSION)
    assert [c.text for c in contract.refused(old, new)] == ["tool `task_get` removed"]
    old, new = removed_tool(contract.CONTRACT_VERSION + 1)
    assert [c.text for c in contract.refused(old, new)] == ["tool `task_get` removed"]
    old, new = removed_tool(contract.CONTRACT_VERSION - 1)
    assert contract.refused(old, new) == []


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
    left = contract.refused(old, new)
    assert not left, "Breaking MCP changes (see docs/SPEC.md#spec-mcp-compat):\n" + "\n".join(
        c.text for c in left
    )
    assert old["contract_version"] <= contract.CONTRACT_VERSION


async def test_the_contract_resource_and_the_instructions(tmp_path: Path) -> None:
    mcp = build_mcp(Service(tmp_path / "relaybay.sqlite"))
    text = next(iter(await mcp.read_resource("relaybay://contract"))).content
    assert text.startswith(f"# relaybay MCP contract\n\nServer {relaybay.__version__}.\n\n")
    assert f"Contract version {contract.CONTRACT_VERSION}." in text
    assert contract.RULE in text
    assert "Deprecat" not in text
    assert "(relaybay://schema/task; contract v3: relaybay://contract)" in instructions(
        Rules(DEFAULT_RULES)
    )


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


def test_rebaseline_takes_a_breaking_change_with_a_raised_contract_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = load_script("gen_reference")
    stale = baseline()
    stale["contract_version"] = contract.CONTRACT_VERSION - 1
    stale["tools"]["gone_tool"] = {"input": {"type": "object"}, "annotations": {}}
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    monkeypatch.setattr(gen, "BASELINE", path)
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    assert gen.rebaseline() == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["contract_version"] == contract.CONTRACT_VERSION
    assert "gone_tool" not in written["tools"]
