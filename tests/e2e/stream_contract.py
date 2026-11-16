"""The stream-json shape relaybay's runner will rely on, checked on the fake in every test run and
on the real CLI when opted in (`test_real_claude.py`). Extra fields are ignored on purpose: the
real CLI adds new ones between versions."""

from typing import Any


def _has(message: dict[str, Any], *keys: str) -> None:
    missing = [key for key in keys if key not in message]
    assert not missing, f"{missing} missing in {message}"


def check_stream(messages: list[dict[str, Any]]) -> None:
    """Asserts: one `system/init` before any turn message, the fields of each message kind, and a
    `result` as the last line."""
    kinds = [m.get("type") for m in messages]
    assert kinds and kinds[-1] == "result", kinds
    inits = [m for m in messages if m["type"] == "system" and m.get("subtype") == "init"]
    assert len(inits) == 1, kinds
    _has(inits[0], "session_id", "tools", "mcp_servers", "model", "cwd", "permissionMode")
    for server in inits[0]["mcp_servers"]:
        _has(server, "name", "status")
    session = inits[0]["session_id"]
    first_turn = next((i for i, k in enumerate(kinds) if k in ("assistant", "user")), len(kinds))
    assert messages.index(inits[0]) < first_turn, kinds
    for message in messages:
        if message["type"] in ("assistant", "user"):
            _has(message, "session_id", "message", "parent_tool_use_id")
            assert message["session_id"] == session
        if message["type"] == "assistant":
            _has(message["message"], "id", "model", "content", "usage")
        if message["type"] == "system" and message.get("subtype") == "permission_denied":
            _has(message, "tool_name", "tool_use_id", "message", "session_id")
        if message["type"] == "system" and message.get("subtype") == "compact_boundary":
            _has(message, "session_id", "compact_metadata")
            _has(message["compact_metadata"], "trigger", "pre_tokens")
            assert message["session_id"] == session
    result = messages[-1]
    _has(result, "subtype", "is_error", "session_id", "num_turns", "usage", "permission_denials")
    assert result["session_id"] == session


def check_compact(messages: list[dict[str, Any]]) -> None:
    """A headless `/compact` turn (probe 18 of design 0023): a stream as above with one
    `compact_boundary` of trigger `manual`, no assistant message, and a result with zero usage."""
    check_stream(messages)
    bounds = [
        m for m in messages if m["type"] == "system" and m.get("subtype") == "compact_boundary"
    ]
    assert len(bounds) == 1, messages
    assert bounds[0]["compact_metadata"]["trigger"] == "manual"
    assert not [m for m in messages if m["type"] == "assistant"]
    usage = messages[-1]["usage"]
    assert not any(usage.get(key) for key in ("input_tokens", "output_tokens")), usage
