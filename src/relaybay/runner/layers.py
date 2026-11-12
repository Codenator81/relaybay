"""Whether relaybay's hooks and MCP server are in the user's Claude Code settings, the same files
`relaybay setup --check` reads. A run adds nothing when they are: its turns load what a manual
session loads. When they are not, the run passes them in files of its own (`--settings`,
`--mcp-config`) in the data folder; the user's settings are only read, never written."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from relaybay.domain.hooks import HOOK_EVENTS, HOOK_PATH, RUN_HEADER
from relaybay.machine_setup import (
    MCP_NAME,
    ClaudePaths,
    SetupError,
    desired_groups,
    hook_handler,
    installed_mode,
    mcp_status,
    our_handlers,
    owned_hosts,
    read_json,
)
from relaybay.security import _write_private

SETTINGS_FILE = "settings.json"
MCP_FILE = "mcp.json"


@dataclass(frozen=True, slots=True)
class LayerState:
    # 'command' or 'http': relaybay's hooks for this server; 'http-old': http hooks without the
    # run header, written by an older setup; None: none, some or another server's.
    hooks: str | None
    mcp: bool
    hooks_detail: str
    mcp_detail: str

    @property
    def refusal(self) -> str | None:
        """http hooks without the run header would fire next to the run's command hooks, and
        could not link the turn: setup has to be applied again."""
        if self.hooks == "http-old":
            return (
                "relaybay's http hooks predate runs: re-apply relaybay setup (Settings →"
                " Integrations) so turns can be linked."
            )
        return None

    @property
    def hooks_source(self) -> str:
        """'user': the user's settings carry them; 'run': the run passes them (--settings)."""
        return "user" if self.hooks in ("command", "http") else "run"

    @property
    def mcp_source(self) -> str:
        return "user" if self.mcp else "run"

    @property
    def line(self) -> str:
        hooks = (
            self.hooks_detail
            if self.hooks_source == "user"
            else f"added for each run with --settings ({self.hooks_detail})"
        )
        mcp = (
            self.mcp_detail
            if self.mcp_source == "user"
            else f"added for each run with --mcp-config ({self.mcp_detail})"
        )
        return f"Hooks: {hooks}; MCP: {mcp}"


def user_layer_state(paths: ClaudePaths, base_url: str) -> LayerState:
    hosts = owned_hosts(base_url)
    mcp_ok, mcp_detail = mcp_status(paths.config, base_url)
    try:
        _, data = read_json(paths.settings)
    except (SetupError, OSError) as exc:
        return LayerState(None, mcp_ok, str(exc), mcp_detail)
    mode = installed_mode(data, hosts)
    if mode not in ("command", "http"):
        detail = "some events are missing" if mode == "partial" else f"none in {paths.settings}"
        return LayerState(None, mcp_ok, detail, mcp_detail)
    handlers = our_handlers(data, HOOK_EVENTS[0], hosts)
    target = handlers[0].get("url") or handlers[0].get("command") or ""
    if base_url + HOOK_PATH not in target:
        return LayerState(None, mcp_ok, f"they point at another server, not {base_url}", mcp_detail)
    if mode == "http":
        headers = handlers[0].get("headers")
        if not (isinstance(headers, dict) and RUN_HEADER in headers):
            return LayerState("http-old", mcp_ok, "http hooks without the run header", mcp_detail)
    return LayerState(mode, mcp_ok, f"{mode} hooks from your user settings", mcp_detail)


def run_settings(base_url: str, token_path: Path) -> dict:
    """relaybay's hooks for a run's --settings: exactly the command hook `relaybay setup` writes,
    so Claude Code runs it once even when the user's settings carry it too."""
    groups = desired_groups(hook_handler("command", base_url, token_path))
    return {"hooks": {event: [group] for event, group in groups.items()}}


def run_mcp_config(base_url: str, token: str) -> dict:
    entry = {
        "type": "http",
        "url": f"{base_url}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }
    return {"mcpServers": {MCP_NAME: entry}}


def run_files(folder: Path, *, hooks: bool, mcp: bool) -> tuple[Path | None, Path | None]:
    """Where a run's --settings and --mcp-config files are; None: the user's settings do it."""
    return (folder / SETTINGS_FILE if hooks else None, folder / MCP_FILE if mcp else None)


def write_run_files(
    folder: Path, *, hooks: bool, mcp: bool, base_url: str, token_path: Path
) -> tuple[Path | None, Path | None]:
    """Write them, mode 0600 as the token file: the MCP file holds the token. Written again
    before every turn, so a rotated token reaches the next turn."""
    settings_file, mcp_file = run_files(folder, hooks=hooks, mcp=mcp)
    if settings_file is not None:
        _write_private(settings_file, json.dumps(run_settings(base_url, token_path), indent=2))
    if mcp_file is not None:
        token = token_path.read_text(encoding="utf-8").strip()
        _write_private(mcp_file, json.dumps(run_mcp_config(base_url, token), indent=2))
    if os.name == "posix" and (settings_file or mcp_file):
        folder.chmod(0o700)
    return settings_file, mcp_file
