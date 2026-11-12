"""Whether relaybay's hooks and MCP server are in the user's settings before a run starts."""

import json
import sys
from pathlib import Path

from relaybay.machine_setup import claude_paths, desired_groups, hook_handler
from relaybay.runner.layers import user_layer_state

BASE = "http://127.0.0.1:8765"


def write(home: Path, handler: dict | None, mcp_url: str | None = f"{BASE}/mcp") -> None:
    paths = claude_paths(home)
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    groups = desired_groups(handler) if handler else {}
    hooks = {"hooks": {event: [group] for event, group in groups.items()}} if handler else {}
    paths.settings.write_text(json.dumps(hooks), encoding="utf-8")
    servers = {"relaybay": {"type": "http", "url": mcp_url}} if mcp_url else {}
    paths.config.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_command_hooks_and_mcp_need_nothing(tmp_path: Path) -> None:
    write(tmp_path, hook_handler("command", BASE, tmp_path / "token", python=sys.executable))
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert (state.hooks, state.mcp, state.refusal) == ("command", True, None)


def test_http_hooks_of_an_older_setup_need_a_reapply(tmp_path: Path) -> None:
    handler = hook_handler("http", BASE, tmp_path / "token")
    write(tmp_path, handler)
    assert user_layer_state(claude_paths(tmp_path), BASE).refusal is None
    del handler["headers"]["X-Relaybay-Run"]
    write(tmp_path, handler)
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert state.hooks == "http-old" and "re-apply relaybay setup" in (state.refusal or "")


def test_missing_or_foreign_hooks_and_mcp_refuse(tmp_path: Path) -> None:
    assert "hooks are not in" in (user_layer_state(claude_paths(tmp_path), BASE).refusal or "")
    other = hook_handler("command", "http://127.0.0.1:9999", tmp_path / "t", python=sys.executable)
    write(tmp_path, other)
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert state.hooks is None and "another server" in state.hooks_detail
    write(tmp_path, hook_handler("command", BASE, tmp_path / "t", python=sys.executable), None)
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert state.hooks == "command" and "MCP server is not registered" in (state.refusal or "")
