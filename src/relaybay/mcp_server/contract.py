"""The versioned MCP contract: what a release may change in the tools, resources and prompts
that clients have already loaded.

Clients cache a tool's parameter schema, so within a contract version a release only adds. A
release that removes or narrows something raises `CONTRACT_VERSION`.
`tests/test_mcp_contract.py` holds the live server against the baseline in
`tests/fixtures/mcp_contract_baseline.json`, which `scripts/gen_reference.py --rebaseline` writes.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

CONTRACT_VERSION = 3

RULE = (
    "Within a contract version a release never removes a tool, a parameter, a resource or a"
    " prompt (or a prompt argument), never narrows what a parameter accepts (its type, enum or"
    " range), never adds a required parameter to an existing tool and never turns a read-only"
    " tool into a writing one; new behaviour comes as new optional parameters or new tools, and"
    " `task_update(fields=...)` and `backlog_patch` reach fields a cached schema lacks. While"
    " relaybay has no outside users, a replaced tool or parameter is removed in the same release"
    " that replaces it, and that release raises the contract version; nothing is kept as"
    " deprecated."
)


def summary() -> list[str]:
    """The contract as markdown lines: version and rule (the resource and the generated
    reference share it)."""
    return [f"Contract version {CONTRACT_VERSION}.", "", RULE]


# --- the baseline -------------------------------------------------------------

_KEPT = {
    "type",
    "enum",
    "const",
    "items",
    "properties",
    "required",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}


def normalize(node: Any, defs: dict[str, Any] | None = None) -> Any:
    """A JSON schema without descriptions, titles and defaults, `$ref`s resolved inline."""
    if not isinstance(node, dict):
        return node
    defs = {**(defs or {}), **node.get("$defs", {})}
    if "$ref" in node:
        target = defs[node["$ref"].rsplit("/", 1)[-1]]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEPT:
            continue
        if key == "properties":
            out[key] = {name: normalize(prop, defs) for name, prop in value.items()}
        elif key in ("anyOf", "oneOf"):
            out[key] = [normalize(branch, defs) for branch in value]
        elif key == "required":
            out[key] = sorted(value)
        else:
            out[key] = normalize(value, defs)
    return out


def snapshot(
    version: str,
    tools: Iterable[Any],
    resources: Iterable[Any],
    templates: Iterable[Any],
    prompts: Iterable[Any],
) -> dict[str, Any]:
    """The surface a client caches, as the MCP SDK lists it."""
    return {
        "contract_version": CONTRACT_VERSION,
        "version": version,
        "tools": {
            t.name: {
                "input": normalize(t.input_schema),
                "annotations": t.annotations.model_dump(exclude_none=True, exclude={"title"})
                if t.annotations is not None
                else {},
            }
            for t in sorted(tools, key=lambda t: t.name)
        },
        "resources": sorted(str(r.uri) for r in resources),
        "resource_templates": sorted(t.uri_template for t in templates),
        "prompts": {
            p.name: {"arguments": {a.name: bool(a.required) for a in p.arguments or []}}
            for p in sorted(prompts, key=lambda p: p.name)
        },
    }


# --- the checker --------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """A breaking change: the tool, resource or prompt it touches, and the parameter or argument
    (None for the whole item)."""

    tool: str
    parameter: str | None
    text: str


_LOWER = ("minimum", "exclusiveMinimum", "minLength", "minItems")
_UPPER = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")


def _branches(node: dict[str, Any]) -> list[dict[str, Any]]:
    """One node per accepted type: anyOf/oneOf flattened, a list of types split."""
    if "anyOf" in node or "oneOf" in node:
        rest = {k: v for k, v in node.items() if k not in ("anyOf", "oneOf")}
        found = []
        for branch in node.get("anyOf", []) + node.get("oneOf", []):
            found += _branches({**rest, **branch})
        return found
    kind = node.get("type")
    if isinstance(kind, list):
        return [{**node, "type": k} for k in kind]
    return [node]


def _accepts(old_type: str | None, new_type: str | None) -> bool:
    return new_type is None or old_type == new_type or (old_type, new_type) == ("integer", "number")


def _narrowed(old: dict[str, Any], new: dict[str, Any], where: str) -> list[str]:
    """Why `new` accepts less than `old` (both one branch of the same type)."""
    found = []
    if "enum" in new:
        lost = [v for v in old.get("enum", []) if v not in new["enum"]]
        if "enum" not in old:
            found.append(f"{where} gained an enum")
        elif lost:
            found.append(f"{where} no longer accepts {', '.join(repr(v) for v in lost)}")
    if "const" in new and old.get("const") != new["const"]:
        found.append(f"{where} gained a fixed value")
    for key in _LOWER:
        if key in new and (key not in old or new[key] > old[key]):
            found.append(f"{where} {key} {old.get(key, 'none')} -> {new[key]}")
    for key in _UPPER:
        if key in new and (key not in old or new[key] < old[key]):
            found.append(f"{where} {key} {old.get(key, 'none')} -> {new[key]}")
    if "items" in old or "items" in new:
        found += _schema(old.get("items", {}), new.get("items", {}), f"{where}[]")
    if old.get("type") == "object" or "properties" in old:
        found += _fields(old, new, where)
    return found


def _schema(old: dict[str, Any], new: dict[str, Any], where: str) -> list[str]:
    found = []
    new_branches = _branches(new)
    for branch in _branches(old):
        kind = branch.get("type")
        candidates = [b for b in new_branches if _accepts(kind, b.get("type"))]
        if not candidates:
            found.append(f"{where} no longer accepts {kind or 'any type'}")
            continue
        reasons = [_narrowed(branch, b, where) for b in candidates]
        if all(reasons):
            found += reasons[0]
    return found


def _fields(old: dict[str, Any], new: dict[str, Any], where: str) -> list[str]:
    """Object fields below a top-level parameter; the parameter itself is `where`."""
    found = []
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    for name in old_props:
        if name not in new_props:
            found.append(f"{where}.{name} removed")
        else:
            found += _schema(old_props[name], new_props[name], f"{where}.{name}")
    for name in sorted(set(new.get("required", [])) - set(old.get("required", []))):
        state = "became required" if name in old_props else "added as required"
        found.append(f"{where}.{name} {state}")
    if (
        old.get("additionalProperties", True) is not False
        and new.get("additionalProperties") is False
    ):
        found.append(f"{where} no longer accepts other fields")
    return found


def _tool(name: str, old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    found = []
    old_in, new_in = old["input"], new["input"]
    old_props, new_props = old_in.get("properties", {}), new_in.get("properties", {})
    old_required = set(old_in.get("required", []))
    for param in old_props:
        if param not in new_props:
            found.append(Change(name, param, f"{name}: parameter `{param}` removed"))
            continue
        for reason in _schema(old_props[param], new_props[param], f"`{param}`"):
            found.append(Change(name, param, f"{name}: {reason}"))
    for param in sorted(set(new_in.get("required", [])) - old_required):
        state = "became required" if param in old_props else "added as required"
        found.append(Change(name, param, f"{name}: parameter `{param}` {state}"))
    if (
        old_in.get("additionalProperties", True) is not False
        and new_in.get("additionalProperties") is False
    ):
        found.append(Change(name, None, f"{name}: no longer accepts unknown parameters"))
    was, now = old["annotations"], new["annotations"]
    if was.get("read_only_hint") and not now.get("read_only_hint"):
        found.append(Change(name, None, f"{name}: was read-only, now writes"))
    if not was.get("destructive_hint") and now.get("destructive_hint"):
        found.append(Change(name, None, f"{name}: became destructive"))
    return found


def breaking_changes(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """Everything in `new` that a client holding `old` could trip over; additions are fine."""
    found = []
    for name, tool in old["tools"].items():
        if name not in new["tools"]:
            found.append(Change(name, None, f"tool `{name}` removed"))
        else:
            found += _tool(name, tool, new["tools"][name])
    for key in ("resources", "resource_templates"):
        for uri in old[key]:
            if uri not in new[key]:
                found.append(Change(uri, None, f"resource `{uri}` removed"))
    for name, prompt in old["prompts"].items():
        if name not in new["prompts"]:
            found.append(Change(name, None, f"prompt `{name}` removed"))
            continue
        was, now = prompt["arguments"], new["prompts"][name]["arguments"]
        for arg in was:
            if arg not in now:
                found.append(Change(name, arg, f"prompt {name}: argument `{arg}` removed"))
        for arg in sorted(a for a, required in now.items() if required and not was.get(a)):
            found.append(Change(name, arg, f"prompt {name}: argument `{arg}` became required"))
    return found


def refused(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    """The breaking changes from `old` to `new` that the contract version does not allow: all of
    them unless `CONTRACT_VERSION` is above the one `old` was written with."""
    if old["contract_version"] < CONTRACT_VERSION:
        return []
    return breaking_changes(old, new)
