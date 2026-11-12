"""The stream-json lines of a turn: every line becomes a masked, compressed event to store, and
two of them, `system/init` and `result`, also steer the run."""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from relaybay.domain.environments import is_reference, is_secret_field, redact
from relaybay.domain.run_events import StreamEvent, pack

LINE_MAX = 4 * 1024 * 1024  # a longer line is not parsed: only its masked head is stored
HEAD_MAX = 16 * 1024  # what is kept of a line over LINE_MAX
CC_ID_MAX = 200
DENIED_MAX = 50
REPORT_TEXT_MAX = 100_000  # the result text a turn's report keeps
REFUSAL_INPUT_MAX = 300
CALL_TEXT_MAX = 8 * 1024  # as much of a refused call as the guard reads
TRUNCATED_MARK = "[relaybay: line truncated]"


@dataclass(frozen=True, slots=True)
class Init:
    session_id: str


@dataclass(frozen=True, slots=True)
class Refusal:
    tool: str
    input: str  # masked, on one line, at most REFUSAL_INPUT_MAX characters
    # Masked with its lines kept, at most CALL_TEXT_MAX: what the approval check reads.
    text: str = ""


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
    denied_tools: list[str] = field(default_factory=list)
    text: str = ""  # the turn's final text, masked
    refusals: list[Refusal] = field(default_factory=list)


StreamItem = Init | Result


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
        return None
    return value


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mask(value: Any) -> Any:
    """`value` with every string masked by the journal's masking. Strings are masked decoded, so
    a secret behind a JSON escape (`\\n`, `\\u0067`) is caught as well. A string under a secret
    field name (`{"password": "x"}`, `api_key`, `Authorization`) is masked whole unless it is a
    reference (`${DB_PASSWORD}`, `vault://…`)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, dict):
        return {k: _field(k, v) for k, v in value.items()}
    return value


def _field(name: str, value: Any) -> Any:
    if isinstance(value, str) and value and is_secret_field(name) and not is_reference(value):
        return "***"
    return mask(value)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _refusal_text(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("command"), str):
        value = value["command"]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text[:CALL_TEXT_MAX]


def _refusal_input(value: Any) -> str:
    return _cut(" ".join(_refusal_text(value).split()), REFUSAL_INPUT_MAX)


def _item(message: dict[str, Any], masked: dict[str, Any]) -> StreamItem | None:
    """Ids and numbers come from the line as read, every text from its masked copy."""
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        session_id = _text(message.get("session_id"), CC_ID_MAX)
        return Init(session_id) if session_id else None
    if kind != "result":
        return None
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
    denials = masked.get("permission_denials")
    denials = denials if isinstance(denials, list) else []
    refusals = [
        Refusal(name, _refusal_input(d.get("tool_input")), _refusal_text(d.get("tool_input")))
        for d in denials[:DENIED_MAX]
        if isinstance(d, dict) and (name := _text(d.get("tool_name"), 200))
    ]
    text = masked.get("result")
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
        denied_tools=[r.tool for r in refusals],
        text=_cut(text, REPORT_TEXT_MAX) if isinstance(text, str) else "",
        refusals=refusals,
    )


def read_line(raw: str, seq: int) -> tuple[StreamEvent | None, StreamItem | None]:
    """The event to store for one line of at most LINE_MAX characters, and the item that steers
    the run, if it is one. A line that is not a JSON object is stored masked as `{"raw": …}`."""
    if not raw.strip():
        return None, None
    try:
        message = json.loads(raw)
        masked = mask(message)
    except (ValueError, RecursionError):  # not JSON, or nested deeper than Python follows
        message = masked = None
    if not isinstance(message, dict):
        head = _masked_head(raw.rstrip("\r\n"), cut=len(raw) > HEAD_MAX)
        event = StreamEvent(seq, "invalid", None, None, now(), len(raw), False, pack({"raw": head}))
        return event, None
    event = StreamEvent(
        seq,
        _text(message.get("type"), 60) or "unknown",
        _text(message.get("subtype"), 60),
        _text(message.get("parent_tool_use_id"), CC_ID_MAX),
        now(),
        len(raw),
        False,
        pack(masked),
    )
    return event, _item(message, masked)


_ESCAPE = re.compile(r'\\(u[0-9a-fA-F]{4}|["\\/bfnrt])')
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_TYPE = re.compile(r'"type"\s*:\s*"([a-z_]{1,60})"')
_PARENT = re.compile(r'"parent_tool_use_id"\s*:\s*"([A-Za-z0-9_-]{1,200})"')


def _unescape(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        code = match.group(1)
        return chr(int(code[1:], 16)) if code[0] == "u" else _SIMPLE[code]

    return _ESCAPE.sub(one, text)


def _masked_head(text: str, cut: bool) -> str:
    """At most HEAD_MAX characters of a line that is not stored parsed, JSON escapes decoded,
    masked. A `cut` text goes back to its last separator first: half a secret is too short to be
    masked, so it is dropped."""
    text = _unescape(text[:HEAD_MAX])
    if cut:
        last = max(text.rfind(c) for c in ' \n\t",')
        text = text[:last] if last > 0 else ""
    return redact(text)


def truncated_event(head: str, size: int, seq: int) -> StreamEvent:
    """A line over LINE_MAX: its masked head with the marker; the rest was never held."""
    text = _masked_head(head, cut=True)
    found = _TYPE.search(head[:HEAD_MAX])
    parent = _PARENT.search(head[:HEAD_MAX])
    data = pack({"head": text, "note": TRUNCATED_MARK, "size": size})
    return StreamEvent(
        seq,
        found.group(1) if found else "unknown",
        None,
        parent.group(1) if parent else None,
        now(),
        size,
        True,
        data,
    )


def parse_line(raw: str) -> StreamItem | None:
    if len(raw) > LINE_MAX:
        return None
    return read_line(raw, 0)[1]
