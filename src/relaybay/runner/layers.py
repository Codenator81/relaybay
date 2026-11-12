"""Whether relaybay's hooks and MCP server are in the user's Claude Code settings, the same files
`relaybay setup --check` reads. A run adds nothing when they are: its turns load what a manual
session loads. Files are read only; nothing is written or run here."""

from dataclasses import dataclass

from relaybay.domain.hooks import HOOK_EVENTS, HOOK_PATH, RUN_HEADER
from relaybay.machine_setup import (
    ClaudePaths,
    SetupError,
    installed_mode,
    mcp_status,
    our_handlers,
    owned_hosts,
    read_json,
)

SETUP_FIX = "run relaybay setup (Settings → Integrations) first"


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
        if self.hooks is None:
            return (
                f"relaybay's hooks are not in your Claude Code user settings ({self.hooks_detail});"
                f" {SETUP_FIX}."
            )
        if self.hooks == "http-old":
            return (
                "relaybay's http hooks predate runs: re-apply relaybay setup (Settings →"
                " Integrations) so turns can be linked."
            )
        if not self.mcp:
            return (
                f"relaybay's MCP server is not registered for Claude Code ({self.mcp_detail});"
                f" {SETUP_FIX}."
            )
        return None


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
