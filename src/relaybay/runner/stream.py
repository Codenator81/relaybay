"""The two stream-json lines of a turn the runner reads: `system/init` and `result`."""

import json
from dataclasses import dataclass, field

LINE_MAX = 4 * 1024 * 1024  # a longer line is skipped before it is parsed
CC_ID_MAX = 200
DENIED_MAX = 50


@dataclass(frozen=True, slots=True)
class Init:
    session_id: str


@dataclass(frozen=True, slots=True)
class Result:
    subtype: str
    is_error: bool
    num_turns: int | None
    duration_ms: int | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    denials: int = 0
    # Tool names only: a denial's input may hold anything the agent typed.
    denied_tools: list[str] = field(default_factory=list)


StreamItem = Init | Result


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
        return None
    return value


def parse_line(raw: str) -> StreamItem | None:
    if len(raw) > LINE_MAX or not raw.strip():
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        session_id = _text(message.get("session_id"), CC_ID_MAX)
        return Init(session_id) if session_id else None
    if kind != "result":
        return None
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    denials = message.get("permission_denials")
    denials = denials if isinstance(denials, list) else []
    names = [
        name
        for d in denials[:DENIED_MAX]
        if isinstance(d, dict) and (name := _text(d.get("tool_name"), 200))
    ]
    return Result(
        subtype=_text(message.get("subtype"), 60) or "unknown",
        is_error=message.get("is_error") is True,
        num_turns=_int(message.get("num_turns")),
        duration_ms=_int(message.get("duration_ms")),
        input_tokens=_int(usage.get("input_tokens")) or 0,
        output_tokens=_int(usage.get("output_tokens")) or 0,
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")) or 0,
        cache_write_tokens=_int(usage.get("cache_creation_input_tokens")) or 0,
        denials=len(denials),
        denied_tools=names,
    )
