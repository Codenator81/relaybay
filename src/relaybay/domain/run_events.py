"""A turn's stored stream-json lines read back for people and agents: tool calls with their
results, subagent work nested under the call that started it, refusals and the result.

The lines arrive masked (relaybay.runner.stream); this module only shapes and shortens them.
"""

import json
import zlib
from dataclasses import dataclass, field
from typing import Any

BOARD_TEXT_MAX = 4000
COMPACT_TEXT_MAX = 160
BOARD_LINES = 300  # the board shows a turn's last lines; run_events reads the rest
BOARD_TOTAL_MAX = 200_000  # characters of text in one turn's view; past it items get COMPACT


@dataclass(slots=True)
class EventItem:
    seq: int
    kind: str  # init, text, tool, tool_result, refusal, result, truncated, other
    name: str = ""  # the tool, or the line's type/subtype
    text: str = ""
    tool_use_id: str | None = None
    parent: str | None = None  # parent_tool_use_id: the subagent's call
    result: str | None = None  # a tool's result, when its line came
    is_error: bool = False
    children: list["EventItem"] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One line as stored: `data` is the zlib of the masked line (JSON)."""

    seq: int
    type: str
    subtype: str | None
    parent_tool_use_id: str | None
    ts: str
    size: int
    truncated: bool
    data: bytes
    secret: bool = False  # the masking replaced something in it: the run "saw a secret"


@dataclass(frozen=True, slots=True)
class StoredLine:
    seq: int
    type: str
    subtype: str | None
    parent: str | None
    truncated: bool
    message: Any  # the masked line


def pack(message: Any) -> bytes:
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8", "surrogatepass"))


def unpack(data: bytes) -> Any:
    return json.loads(zlib.decompress(data).decode("utf-8", "surrogatepass"))


def _short(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, list):  # tool_result content blocks
        value = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in value)
    if isinstance(value, dict):
        if isinstance(value.get("command"), str):
            value = value["command"]
        else:
            value = json.dumps(value, ensure_ascii=False)
    text = " ".join(str(value).split()) if limit <= COMPACT_TEXT_MAX else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def items(
    lines: list[StoredLine], limit: int = BOARD_TEXT_MAX, total: int | None = None
) -> list[EventItem]:
    """The lines in order as items; a tool result is put on its call when that call was read.
    With `total`, the texts together stay within it (then COMPACT_TEXT_MAX each)."""
    out = _items(lines, limit)
    if total is not None:
        _fit(out, total)
    return out


def _fit(out: list[EventItem], total: int) -> None:
    used = 0
    for item in out:
        for attr in ("text", "result"):
            value = getattr(item, attr)
            if not value:
                continue
            room = max(COMPACT_TEXT_MAX, total - used)
            if len(value) > room:
                value = value[: room - 1] + "…"
                setattr(item, attr, value)
            used += len(value)


def _items(lines: list[StoredLine], limit: int) -> list[EventItem]:
    out: list[EventItem] = []
    tools: dict[str, EventItem] = {}
    for line in lines:
        m = line.message if isinstance(line.message, dict) else {}
        base = {"seq": line.seq, "parent": line.parent}
        if line.truncated:
            out.append(
                EventItem(
                    kind="truncated", name=line.type, text=_short(m.get("head"), limit), **base
                )
            )
            continue
        if line.type == "system" and line.subtype == "init":
            out.append(EventItem(kind="init", name=_short(m.get("model"), 80), **base))
        elif line.type == "system" and line.subtype == "permission_denied":
            out.append(
                EventItem(
                    kind="refusal",
                    name=_short(m.get("tool_name"), 200),
                    text=_short(m.get("decision_reason") or m.get("message"), limit),
                    tool_use_id=m.get("tool_use_id"),
                    **base,
                )
            )
        elif line.type in ("assistant", "user"):
            message = m.get("message") if isinstance(m.get("message"), dict) else {}
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and block.get("text"):
                    out.append(EventItem(kind="text", text=_short(block["text"], limit), **base))
                elif kind == "tool_use":
                    tool = EventItem(
                        kind="tool",
                        name=_short(block.get("name"), 200),
                        text=_short(block.get("input"), limit),
                        tool_use_id=block.get("id") if isinstance(block.get("id"), str) else None,
                        **base,
                    )
                    out.append(tool)
                    if tool.tool_use_id:
                        tools[tool.tool_use_id] = tool
                elif kind == "tool_result":
                    text = _short(block.get("content"), limit)
                    error = block.get("is_error") is True
                    call = tools.get(str(block.get("tool_use_id")))
                    if call is not None and call.result is None:
                        call.result, call.is_error = text, error
                    else:
                        out.append(EventItem(kind="tool_result", text=text, is_error=error, **base))
        elif line.type == "result":
            out.append(
                EventItem(
                    kind="result",
                    name=_short(m.get("subtype"), 60),
                    text=_short(m.get("result"), limit),
                    is_error=m.get("is_error") is True,
                    **base,
                )
            )
        elif line.type == "invalid":
            out.append(
                EventItem(kind="other", name="not JSON", text=_short(m.get("raw"), limit), **base)
            )
        else:
            name = "/".join(x for x in (line.type, line.subtype) if x)
            out.append(EventItem(kind="other", name=name, **base))
    return out


def nest(flat: list[EventItem]) -> list[EventItem]:
    """Subagent items under the tool call whose id they carry as parent_tool_use_id; an item
    whose call is unknown stays at the top."""
    calls = {i.tool_use_id: i for i in flat if i.kind == "tool" and i.tool_use_id}
    roots: list[EventItem] = []
    for item in flat:
        holder = calls.get(item.parent) if item.parent else None
        if holder is not None and holder is not item:
            holder.children.append(item)
        else:
            roots.append(item)
    return roots
