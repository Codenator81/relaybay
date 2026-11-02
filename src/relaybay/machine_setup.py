"""`relaybay setup`: connect Claude Code on this machine to relaybay (hooks and MCP).

Only the user settings file (`~/.claude/settings.json`) is ever written, and only on an explicit
apply or remove: a backup first, then a JSON merge that touches nothing but relaybay's own hook
entries (see `is_ours`). The MCP server is registered by Claude Code itself
(`claude mcp add`); relaybay only prints that command and reads `~/.claude.json` to check it.
"""

import copy
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from relaybay import __version__
from relaybay.domain.hooks import HOOK_EVENTS, HOOK_PATH
from relaybay.security import token_id

# The hooks reference documents `http` hooks without the version that introduced them; this is
# the release that added them according to the Claude Code changelog (unverified).
HTTP_HOOKS_MIN_VERSION = (2, 1, 63)
TOKEN_ENV = "RELAYBAY_TOKEN"
COMMAND_TIMEOUT = 3  # seconds; the command itself gives up after 0.3 s per network step
HTTP_TIMEOUT = 2
MODES = ("command", "http")
DEFAULT_MODE = "command"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
BACKUP_INFIX = ".relaybay-backup-"
MCP_NAME = "relaybay"
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class SetupError(Exception):
    """A file relaybay must not touch (invalid JSON, unexpected shape)."""


# --- detection -------------------------------------------------------------


@dataclass(slots=True)
class Detection:
    claude_path: str | None = None
    claude_version: str | None = None
    version_error: str | None = None
    desktop_app: str | None = None

    @property
    def version_tuple(self) -> tuple[int, int, int] | None:
        match = _VERSION_RE.search(self.claude_version or "")
        return (int(match[1]), int(match[2]), int(match[3])) if match else None

    @property
    def http_hooks(self) -> bool:
        version = self.version_tuple
        return version is not None and version >= HTTP_HOOKS_MIN_VERSION


def _desktop_app(platform: str, environ: Mapping[str, str], home: Path) -> str | None:
    """Best effort: the usual install locations of the Claude desktop app."""
    if platform == "win32":
        local = environ.get("LOCALAPPDATA")
        if not local:
            return None
        base = Path(local)
        found = sorted((base / "Packages").glob("Claude_*")) if (base / "Packages").is_dir() else []
        candidates = [*found, base / "AnthropicClaude", base / "Programs" / "Claude"]
    elif platform == "darwin":
        candidates = [Path("/Applications/Claude.app"), home / "Applications" / "Claude.app"]
    else:
        linux = shutil.which("claude-desktop")
        return linux
    return next((str(p) for p in candidates if p.exists()), None)


def detect(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> Detection:
    found = Detection(claude_path=which("claude"))
    if found.claude_path:
        try:
            result = run(
                [found.claude_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            found.claude_version = (result.stdout or "").strip() or None
            if found.claude_version is None:
                found.version_error = (result.stderr or "").strip()[:200] or "no output"
        except (OSError, subprocess.SubprocessError) as exc:
            found.version_error = str(exc)[:200]
    found.desktop_app = _desktop_app(platform, environ, home or Path.home())
    return found


# --- paths -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaudePaths:
    settings: Path  # user settings: hooks
    config: Path  # ~/.claude.json: user-scope MCP servers (written by Claude Code)


def claude_paths(claude_home: Path | None, environ: Mapping[str, str] = os.environ) -> ClaudePaths:
    """User settings and `~/.claude.json` under `claude_home`, or under the real home directory
    (`CLAUDE_CONFIG_DIR` moves the settings folder) when it is None."""
    if claude_home is not None:
        return ClaudePaths(claude_home / ".claude" / "settings.json", claude_home / ".claude.json")
    home = Path.home()
    folder = Path(environ["CLAUDE_CONFIG_DIR"]) if environ.get("CLAUDE_CONFIG_DIR") else None
    return ClaudePaths((folder or home / ".claude") / "settings.json", home / ".claude.json")


# --- desired entries -------------------------------------------------------


MODE_REASONS = {
    "command": "a small forwarder run by this Python; silent when relaybay is down",
    "http": "Claude Code posts to relaybay itself; needs RELAYBAY_TOKEN in its environment",
}


def mode_notes(
    mode: str, detection: Detection, environ: Mapping[str, str], token: str
) -> list[str]:
    """What the user should know about http hooks before choosing them."""
    if mode != "http":
        return []
    notes = []
    if not detection.http_hooks:
        version = detection.claude_version or "an unknown version"
        minimum = ".".join(map(str, HTTP_HOOKS_MIN_VERSION))
        notes.append(f"Claude Code {version} is not known to support http hooks (>= {minimum}).")
    if environ.get(TOKEN_ENV) != token:
        notes.append(
            f"http hooks send 'Authorization: Bearer ${{{TOKEN_ENV}}}': set {TOKEN_ENV} to the"
            " relaybay token in the environment Claude Code starts with, or the hooks are refused."
        )
    notes.append(
        "When relaybay is not running, Claude Code shows a non-blocking hook error notice for"
        " http hooks; the command hook stays silent."
    )
    return notes


_SPECIAL = re.compile(r"[\s\"'$`;&|<>()\\]")


def _arg(value: str, platform: str) -> str:
    """Quote a value for the shell Claude Code runs the command in. POSIX shells: single quotes,
    so `$` and backticks stay literal. Windows (Git Bash or PowerShell): double quotes, which
    both accept around a path; a Windows path with `$` or a backtick is not supported."""
    if not _SPECIAL.search(value):
        return value
    if platform == "win32":
        return f'"{value}"'
    return "'" + value.replace("'", "'\\''") + "'"


def hook_command(
    base_url: str, token_path: Path, python: str | None = None, platform: str = sys.platform
) -> str:
    """The command hook: this interpreter runs the stdlib-only forwarder, so nothing has to be
    on PATH. Paths use forward slashes, which every shell on every OS accepts."""
    interpreter = Path(python or sys.executable).as_posix()
    return " ".join(
        [
            _arg(interpreter, platform),
            "-I -m relaybay.hook_client",
            "--url",
            _arg(base_url + HOOK_PATH, platform),
            "--token-file",
            _arg(token_path.resolve().as_posix(), platform),
        ]
    )


def hook_interpreter(command: str) -> str | None:
    """The interpreter of a relaybay command hook: its first word, unquoted."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    return words[0] if words else None


def hook_handler(mode: str, base_url: str, token_path: Path, python: str | None = None) -> dict:
    if mode == "http":
        return {
            "type": "http",
            "url": base_url + HOOK_PATH,
            "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"},
            "allowedEnvVars": [TOKEN_ENV],
            "timeout": HTTP_TIMEOUT,
        }
    return {
        "type": "command",
        "command": hook_command(base_url, token_path, python),
        "timeout": COMMAND_TIMEOUT,
    }


def desired_groups(handler: dict) -> dict[str, dict]:
    """One matcher group per event; PreToolUse matches every tool."""
    return {
        event: ({"matcher": "*"} if event == "PreToolUse" else {}) | {"hooks": [handler]}
        for event in HOOK_EVENTS
    }


def _our_hook_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.path == HOOK_PATH
        and not parts.query
        and not parts.fragment
    )


def _is_our_command(command: str) -> bool:
    """Only the exact command relaybay writes counts: `<python> [-I] -m relaybay.hook_client` or
    `relaybay hook`, followed by exactly `--url <hook url> --token-file <path>` and nothing else.
    A compound command (`a && b`), a wrapper or extra words make it the user's."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if len(words) >= 4 and words[1:4] == ["-I", "-m", "relaybay.hook_client"]:
        rest = words[4:]
    elif len(words) >= 3 and words[1:3] == ["-m", "relaybay.hook_client"]:
        rest = words[3:]
    elif (
        len(words) >= 2
        and Path(words[0]).name.lower() in ("relaybay", "relaybay.exe")
        and words[1] == "hook"
    ):
        rest = words[2:]
    else:
        return False
    return (
        len(rest) == 4
        and rest[0] == "--url"
        and _our_hook_url(rest[1])
        and rest[2] == "--token-file"
        and bool(rest[3])
    )


def owned_hosts(base_url: str) -> frozenset[str]:
    """Hosts an http hook of relaybay may point at: loopback and the configured one."""
    host = urlsplit(base_url).hostname
    return LOOPBACK | ({host.lower()} if host else set())


def is_ours(handler: Any, hosts: frozenset[str] = LOOPBACK) -> bool:
    """A hook entry relaybay wrote. A command hook runs the relaybay forwarder with
    `--url <scheme>://<host>/hooks/claude-code`; an http hook posts to exactly that path on a
    loopback or the configured host. Anything else, even with a similar path, is the user's."""
    if not isinstance(handler, dict):
        return False
    if handler.get("type") == "http":
        url = handler.get("url")
        if not isinstance(url, str):
            return False
        return _our_hook_url(url) and (urlsplit(url).hostname or "").lower() in hosts
    command = handler.get("command")
    if handler.get("type") != "command" or not isinstance(command, str):
        return False
    return _is_our_command(command)


# --- merge -----------------------------------------------------------------


def _hooks_section(data: dict) -> dict | None:
    hooks = data.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        raise SetupError("'hooks' is not an object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise SetupError(f"'hooks.{event}' is not a list")
        for i, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
                raise SetupError(f"'hooks.{event}[{i}]' is not a matcher group")
    return hooks


def _drop_ours(groups: list, keep_first: dict | None, hosts: frozenset[str]) -> bool:
    """Remove relaybay handlers from `groups` in place; with `keep_first`, the first one is
    replaced by it instead. Groups left empty by the removal go too. True when one was kept."""
    kept = False
    for group in list(groups):
        handlers = group.get("hooks", [])
        if not any(is_ours(h, hosts) for h in handlers):
            continue
        new = []
        for handler in handlers:
            if not is_ours(handler, hosts):
                new.append(handler)
            elif keep_first is not None and not kept:
                new.append(keep_first)
                kept = True
        if new:
            group["hooks"] = new
        else:
            groups.remove(group)
    return kept


def merge_hooks(data: dict, groups: dict[str, dict], hosts: frozenset[str] = LOOPBACK) -> dict:
    """`data` with relaybay's hook groups added (or its existing handlers updated in place).
    Foreign entries keep their content and order."""
    result = copy.deepcopy(data)
    hooks = _hooks_section(result)
    if hooks is None:
        hooks = result["hooks"] = {}
    for event, group in groups.items():
        existing = hooks.get(event)
        if existing is None:
            hooks[event] = [copy.deepcopy(group)]
            continue
        if not _drop_ours(existing, group["hooks"][0], hosts):
            existing.append(copy.deepcopy(group))
    return result


def remove_hooks(data: dict, hosts: frozenset[str] = LOOPBACK) -> dict:
    """`data` without relaybay's handlers; an event list or `hooks` emptied by that goes too."""
    result = copy.deepcopy(data)
    hooks = _hooks_section(result)
    if hooks is None:
        return result
    for event in list(hooks):
        groups = hooks[event]
        had = any(is_ours(h, hosts) for g in groups for h in g.get("hooks", []))
        _drop_ours(groups, None, hosts)
        if had and not groups:
            del hooks[event]
    if not hooks and data.get("hooks"):
        del result["hooks"]
    return result


def our_handlers(data: dict, event: str, hosts: frozenset[str] = LOOPBACK) -> list[dict]:
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    groups = hooks.get(event) if isinstance(hooks.get(event), list) else []
    return [
        h
        for g in groups
        if isinstance(g, dict) and isinstance(g.get("hooks"), list)
        for h in g["hooks"]
        if is_ours(h, hosts)
    ]


def installed_mode(data: dict, hosts: frozenset[str] = LOOPBACK) -> str | None:
    """'http' or 'command' when every event has a relaybay handler of that type, 'partial' when
    some are missing or mixed, None when there are none."""
    types = set()
    present = 0
    for event in HOOK_EVENTS:
        found = [h.get("type") for h in our_handlers(data, event, hosts)]
        if found:
            present += 1
            types.update(found)
    if present == 0:
        return None
    if present < len(HOOK_EVENTS) or len(types) != 1:
        return "partial"
    return types.pop()


# --- files -----------------------------------------------------------------


def _read_text(path: Path) -> str:
    """The file as is (line endings are kept on write); not UTF-8 is an error, not a guess."""
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SetupError(f"{path} is not UTF-8 text; fix it first") from None


def read_json(path: Path) -> tuple[str | None, dict]:
    """(text, data) of a JSON settings file; (None, {}) when it does not exist."""
    if not path.is_file():
        return None, {}
    text = _read_text(path)
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} is not valid JSON ({exc}); fix it first") from None
    if not isinstance(data, dict):
        raise SetupError(f"{path} does not hold a JSON object")
    return text, data


def _indent(text: str | None) -> int:
    for line in (text or "").splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def dump_json(data: dict, like: str | None) -> str:
    """`data` in the indent and line endings of the file it came from."""
    text = json.dumps(data, indent=_indent(like), ensure_ascii=False) + "\n"
    return text.replace("\n", "\r\n") if like and "\r\n" in like else text


@dataclass(slots=True)
class FileChange:
    path: Path
    before: str | None  # None: the file does not exist
    after: str | None  # None: nothing to write
    summary: str = ""

    @property
    def changed(self) -> bool:
        return self.after is not None and self.after != self.before

    @property
    def plan_hash(self) -> str:
        """Fingerprint of what the user approves: the file as it was and as it will be."""
        digest = hashlib.sha256()
        for part in (self.before, self.after):
            digest.update(b"-" if part is None else b"+" + part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    def diff(self) -> str:
        if not self.changed:
            return ""
        name = self.path.as_posix()
        lines = difflib.unified_diff(
            (self.before or "").splitlines(keepends=True),
            (self.after or "").splitlines(keepends=True),
            fromfile=f"{name} (now)" if self.before is not None else "/dev/null",
            tofile=f"{name} (after)",
        )
        return "".join(lines)


def plan_apply(path: Path, groups: dict[str, dict], hosts: frozenset[str] = LOOPBACK) -> FileChange:
    before, data = read_json(path)
    merged = merge_hooks(data, groups, hosts)
    if merged == data and before is not None:
        return FileChange(path, before, None, "already set up, no change")
    return FileChange(path, before, dump_json(merged, before), "add or update relaybay hooks")


def plan_remove(path: Path, hosts: frozenset[str] = LOOPBACK) -> FileChange:
    before, data = read_json(path)
    cleaned = remove_hooks(data, hosts)
    if cleaned == data:
        return FileChange(path, before, None, "no relaybay hooks, no change")
    return FileChange(path, before, dump_json(cleaned, before), "remove relaybay hooks")


def write_change(
    change: FileChange, now: datetime | None = None, expect: str | None = None
) -> Path | None:
    """Back up the file (if it exists), then replace it atomically. Returns the backup path.
    Refuses when the file changed since this plan was made, and with `expect` (the plan hash
    the user saw) when the plan is not the one approved. A symlinked file is written through to
    its target."""
    if expect is not None and expect != change.plan_hash:
        raise SetupError(
            f"the plan for {change.path} is not the one you approved (the file or the settings"
            " changed); review the new plan and apply again"
        )
    if not change.changed:
        return None
    current = _read_text(change.path) if change.path.is_file() else None
    if current != change.before:
        raise SetupError(f"{change.path} changed since the plan was made; run it again")
    target = change.path.resolve()
    backup = None
    if change.before is not None:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = change.path.with_name(f"{change.path.name}{BACKUP_INFIX}{stamp}")
        n = 1
        while backup.exists():
            n += 1
            backup = change.path.with_name(f"{change.path.name}{BACKUP_INFIX}{stamp}-{n}")
        shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.relaybay.tmp")
    try:
        tmp.write_text(change.after or "", encoding="utf-8", newline="")
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return backup


# --- MCP registration ------------------------------------------------------


def mcp_add_command(base_url: str) -> str:
    return (
        f"claude mcp add --transport http --scope user {MCP_NAME} {base_url}/mcp"
        ' --header "Authorization: Bearer <token>"'
    )


def mcp_status(config_path: Path, base_url: str) -> tuple[bool, str]:
    """Whether the user-scope MCP entry in `~/.claude.json` points at this server."""
    try:
        _, data = read_json(config_path)
    except (SetupError, OSError) as exc:
        return False, f"cannot read {config_path}: {exc}"
    servers = data.get("mcpServers")
    entry = servers.get(MCP_NAME) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return False, f"no user-scope '{MCP_NAME}' server in {config_path}"
    url = entry.get("url")
    if url != f"{base_url}/mcp":
        return False, f"'{MCP_NAME}' points at {url!r}, not {base_url}/mcp"
    return True, f"'{MCP_NAME}' registered (user scope) at {url}"


# --- plan and check ---------------------------------------------------------


@dataclass(slots=True)
class Plan:
    detection: Detection
    mode: str
    mode_reason: str
    paths: ClaudePaths
    hooks: FileChange | None
    error: str | None
    mcp_ok: bool
    mcp_detail: str
    mcp_command: str
    notes: list[str] = field(default_factory=list)


def make_plan(
    *,
    base_url: str,
    token: str,
    token_path: Path,
    paths: ClaudePaths,
    detection: Detection,
    mode: str = DEFAULT_MODE,
    remove: bool = False,
    environ: Mapping[str, str] = os.environ,
    python: str | None = None,
) -> Plan:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    hosts = owned_hosts(base_url)
    hooks, error = None, None
    try:
        if remove:
            hooks = plan_remove(paths.settings, hosts)
        else:
            groups = desired_groups(hook_handler(mode, base_url, token_path, python))
            hooks = plan_apply(paths.settings, groups, hosts)
    except (SetupError, OSError) as exc:
        error = str(exc)
    mcp_ok, mcp_detail = mcp_status(paths.config, base_url)
    return Plan(
        detection=detection,
        mode=mode,
        mode_reason=MODE_REASONS[mode],
        paths=paths,
        hooks=hooks,
        error=error,
        mcp_ok=mcp_ok,
        mcp_detail=mcp_detail,
        mcp_command=mcp_add_command(base_url),
        notes=mode_notes(mode, detection, environ, token),
    )


def format_plan(plan: Plan, *, remove: bool = False) -> list[str]:
    d = plan.detection
    lines = ["Detected:"]
    if d.claude_path:
        version = d.claude_version or f"version unknown ({d.version_error})"
        lines.append(f"  claude:       {d.claude_path} ({version})")
    else:
        lines.append("  claude:       not found on PATH")
    lines.append(f"  desktop app:  {d.desktop_app or 'not found'}")
    lines.append(f"  http hooks:   {'supported' if d.http_hooks else 'not confirmed'}")
    if not remove:
        lines.append(f"Hook type: {plan.mode} ({plan.mode_reason})")
    lines.append(f"User settings: {plan.paths.settings}")
    if plan.error:
        lines.append(f"  ERROR: {plan.error}. Nothing will be written.")
    elif plan.hooks is not None:
        lines.append(f"  {plan.hooks.summary}")
        diff = plan.hooks.diff()
        if diff:
            lines.extend("  " + line for line in diff.rstrip("\n").splitlines())
            action = "--remove" if remove else f"--apply --mode {plan.mode}"
            lines.append(
                f"  Plan hash: {plan.hooks.plan_hash} (write exactly this plan:"
                f" relaybay setup {action} --expect {plan.hooks.plan_hash})"
            )
    if not remove:
        if plan.mcp_ok:
            lines.append(f"MCP server: {plan.mcp_detail}")
        else:
            lines.append(f"MCP server: {plan.mcp_detail}. Register it with Claude Code:")
            lines.append(f"  {plan.mcp_command}")
            lines.append("  ('relaybay token' prints this command with the token filled in.)")
    lines.extend(f"Note: {note}" for note in plan.notes)
    return lines


@dataclass(frozen=True, slots=True)
class Check:
    ok: bool
    name: str
    detail: str


def _opener() -> OpenerDirector:
    return build_opener(ProxyHandler({}))  # localhost: never through a system proxy


def _request(
    opener: OpenerDirector, url: str, method: str, headers: dict, body: dict | None = None
) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _interpreter_check(command: str, run: Callable[..., Any]) -> Check:
    """The command hook's interpreter can still import the forwarder (moved venv, upgrade)."""
    name = "command hook runs"
    interpreter = hook_interpreter(command)
    if interpreter is None:
        return Check(False, name, "cannot read the interpreter from the hook command")
    try:
        result = run(
            [interpreter, "-I", "-c", "import relaybay.hook_client"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(False, name, f"{interpreter}: {exc}")
    if result.returncode != 0:
        error = (result.stderr or "").strip().splitlines()[-1:] or ["failed"]
        return Check(False, name, f"{interpreter} cannot import relaybay: {error[0]}")
    return Check(True, name, f"{interpreter} imports relaybay.hook_client")


def run_check(
    *,
    base_url: str,
    token: str,
    paths: ClaudePaths,
    environ: Mapping[str, str] = os.environ,
    opener: OpenerDirector | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> list[Check]:
    """End-to-end checks, each green or red. Talks to the running server over HTTP."""
    opener = opener or _opener()
    hosts = owned_hosts(base_url)
    auth = {"Authorization": f"Bearer {token}"}
    checks: list[Check] = []

    try:
        status, _, body = _request(opener, f"{base_url}/healthz", "GET", {})
        same = status == 200 and json.loads(body).get("token_id") == token_id(token)
        detail = "running, same token" if same else f"answers {status} with another token"
        checks.append(Check(same, "relaybay server", f"{base_url}: {detail}"))
    except (URLError, OSError, ValueError) as exc:
        checks.append(Check(False, "relaybay server", f"{base_url} not reachable: {exc}"))
        return checks

    try:
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "relaybay-setup-check", "version": __version__},
            },
        }
        headers = auth | {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        status, answer_headers, _ = _request(opener, f"{base_url}/mcp", "POST", headers, init)
        session = {k.lower(): v for k, v in answer_headers.items()}.get("mcp-session-id")
        if session:
            _request(opener, f"{base_url}/mcp", "DELETE", auth | {"Mcp-Session-Id": session})
        checks.append(Check(status == 200, "MCP with the token", f"initialize answered {status}"))
    except (URLError, OSError) as exc:
        checks.append(Check(False, "MCP with the token", str(exc)))

    try:
        probe = {"hook_event_name": "RelaybaySetupCheck", "session_id": "relaybay-setup-check"}
        headers = auth | {"Content-Type": "application/json"}
        status, _, _ = _request(opener, base_url + HOOK_PATH, "POST", headers, probe)
        checks.append(Check(200 <= status < 300, "hook endpoint", f"test event answered {status}"))
    except (URLError, OSError) as exc:
        checks.append(Check(False, "hook endpoint", str(exc)))

    try:
        _, data = read_json(paths.settings)
        mode = installed_mode(data, hosts)
        if mode in ("http", "command"):
            first = our_handlers(data, HOOK_EVENTS[0], hosts)[0]
            target = first.get("url") or first.get("command") or ""
            here = (base_url + HOOK_PATH) in target
            detail = f"{mode} hooks for {len(HOOK_EVENTS)} events"
            detail += "" if here else f", but not for {base_url}"
            checks.append(Check(here, "hooks in user settings", detail))
            if mode == "command":
                checks.append(_interpreter_check(first.get("command") or "", run))
            if mode == "http":
                set_here = environ.get(TOKEN_ENV) == token
                checks.append(
                    Check(
                        set_here,
                        f"{TOKEN_ENV} for http hooks",
                        "set in this environment"
                        if set_here
                        else "not set to the token here; Claude Code needs it",
                    )
                )
        else:
            detail = "some events are missing" if mode == "partial" else "none"
            checks.append(Check(False, "hooks in user settings", f"{detail} ({paths.settings})"))
    except (SetupError, OSError) as exc:
        checks.append(Check(False, "hooks in user settings", str(exc)))

    ok, detail = mcp_status(paths.config, base_url)
    checks.append(Check(ok, "MCP registered for Claude Code", detail))
    return checks
