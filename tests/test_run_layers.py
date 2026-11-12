"""Whether relaybay's hooks and MCP server are in the user's settings before a run starts."""

import json
import os
import sys
from pathlib import Path

from relaybay.machine_setup import claude_paths, desired_groups, hook_handler
from relaybay.runner.layers import user_layer_state, write_run_files

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


def test_missing_or_foreign_hooks_and_mcp_are_added_by_the_run(tmp_path: Path) -> None:
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert (state.refusal, state.hooks_source, state.mcp_source) == (None, "run", "run")
    assert "added for each run with --settings" in state.line
    other = hook_handler("command", "http://127.0.0.1:9999", tmp_path / "t", python=sys.executable)
    write(tmp_path, other)
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert state.hooks is None and "another server" in state.hooks_detail
    assert state.hooks_source == "run" and state.mcp_source == "user"
    write(tmp_path, hook_handler("command", BASE, tmp_path / "t", python=sys.executable), None)
    state = user_layer_state(claude_paths(tmp_path), BASE)
    assert (state.hooks_source, state.mcp_source, state.refusal) == ("user", "run", None)


def test_run_files_carry_the_setup_command_and_stay_private(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("secret-token\n", encoding="utf-8")
    folder = tmp_path / "runs" / "7"
    settings_file, mcp_file = write_run_files(
        folder, hooks=True, mcp=True, base_url=BASE, token_path=token
    )
    assert settings_file is not None and mcp_file is not None
    hooks = json.loads(settings_file.read_text("utf-8"))["hooks"]
    # The handler setup writes for this install: identical, so Claude Code runs it once.
    wanted = hook_handler("command", BASE, token)
    assert {event: groups[0]["hooks"] for event, groups in hooks.items()} == {
        event: [wanted] for event in desired_groups(wanted)
    }
    entry = json.loads(mcp_file.read_text("utf-8"))["mcpServers"]["relaybay"]
    assert entry == {
        "type": "http",
        "url": f"{BASE}/mcp",
        "headers": {"Authorization": "Bearer secret-token"},
    }
    if os.name == "posix":
        for path in (settings_file, mcp_file):
            assert path.stat().st_mode & 0o777 == 0o600
        assert folder.stat().st_mode & 0o777 == 0o700
    assert write_run_files(folder, hooks=False, mcp=False, base_url=BASE, token_path=token) == (
        None,
        None,
    )
