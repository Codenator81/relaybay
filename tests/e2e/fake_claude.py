"""A stand-in for the `claude` CLI: `claude -p ... --output-format stream-json --verbose`.

No model is involved: the "model's" work is a JSON scenario (`FAKE_CLAUDE_SCENARIO`). Everything
around it behaves as documented for Claude Code, and relaybay is only reached the way the real
CLI reaches it: the hook commands run as real processes and MCP tools are called through the
official SDK client.

Hooks come from the user layer `$CLAUDE_CONFIG_DIR/settings.json` (when `user` is in
`--setting-sources`, the default `user,project,local`) and from each `--settings` (a file or JSON
text); a command handler defined in several of them runs once, as Claude Code does. Memory: the
user `$CLAUDE_CONFIG_DIR/CLAUDE.md`, the cwd's `CLAUDE.md` and `CLAUDE.local.md`, and those of each
`--add-dir` folder only with `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`.

Scenario keys, all optional:
  session_id, model, usage (per message), tools (names besides the MCP ones)
  configured_servers  [{name, status}]: servers listed in system/init without an --mcp-config
  turn                steps of the turn
  turns               [[steps], [steps], ...]: the steps of each turn of a session, by how many
                      turns that session had before (counted in $CLAUDE_CONFIG_DIR/fake-turns);
                      the last list serves every later turn. Used instead of `turn`.
  on_stop_block       steps run when a Stop hook blocks; a later block continues with nothing
  auto_classifier     true: under --permission-mode auto a classifier approves every call a hook
                      did not deny, except a step marked "classifier": "deny" (refused as
                      Claude Code's own auto mode does). Without it auto acts like Manual.
  end_wait_file       after the result, wait for this file before the SessionEnd hooks (slow
                      hooks while the process ends); traced as {"kind": "result"} first
A step is one of:
  {"text": "..."}                                 an assistant message
  {"tool": "Bash", "input": {...}, "result": "..."}  a tool call; its command never runs.
      "tries": n repeats the call until it is denied (the hook fails open when it is slow).
      "classifier": "deny" has the auto mode classifier refuse it (with auto_classifier).
      "steps": [...] runs them as a subagent's messages under this call's id.
  {"mcp": "task_create", "arguments": {...}, "capture": {"key": "regex"}}
      a real MCP call; a failed one ends the run with exit code 1 so that no test passes over it.
  {"wait_file": "path", "timeout": 30}            wait until a test creates that file
  {"spawn_child": "pidfile"}                      start a sleeping child that writes its pid there
  {"exit": n}                                     exit with code n at once: no result, no hooks
Strings may use ${session_id}, ${cwd} and captured names.

The prompt is the positional argument, or stdin when it is missing (a `claude.cmd` shim gets it
there). `claude auth status` prints the answer of `FAKE_CLAUDE_AUTH` (a JSON file
`{"exit": n, "answer": {...}}`); without it, a subscription sign-in that also carries an e-mail
and an organization, so a test can check relaybay keeps neither.

What the session received goes as JSON lines to `FAKE_CLAUDE_TRACE`: `{"kind": "context", "event",
"text"}`, `{"kind": "stop_block", "reason"}`, `{"kind": "stopped", "event", "reason"}`; each run
also writes `{"kind": "argv", "argv", "prompt", "run_id", "started"}` first and
`{"kind": "turn_end", "session_id", "started", "ended"}` when it ends normally. Every hook command
run is `{"kind": "hook", "event", "command"}`; the memory loaded at the start is
`{"kind": "memory", "files", "lines"}` (the files' paths and their non-empty lines).

`claude --desktop --resume <id>` (no -p) hands the session to the desktop app: it writes
`{"kind": "desktop", "argv", "cwd", "tty"}` and, like the real CLI, exits 1 with its message when
stdout is not a terminal, else 0 printing nothing.
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from string import Template
from typing import Any, NoReturn

VERSION = "2.1.320 (Claude Code)"
DEFAULT_MODEL = "demo-model-large"
DEFAULT_USAGE = {
    "input_tokens": 10,
    "output_tokens": 5,
    "cache_read_input_tokens": 100,
    "cache_creation_input_tokens": 20,
}
EXIT_TERMINATED = 143
STOP_BLOCK_LIMIT = 8  # a hook that blocks for ever must not hang a test
HOOK_TIMEOUT_DEFAULT = 60.0
# Tools that need no approval in Manual mode; the rest are denied under -p unless approved.
NO_APPROVAL_TOOLS = ("Read", "Glob", "Grep", "TodoWrite", "TaskCreate", "TaskUpdate", "Agent")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
PERMISSION_MODES = (
    "manual",
    "default",
    "acceptEdits",
    "plan",
    "auto",
    "dontAsk",
    "bypassPermissions",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_AUTH = {
    "exit": 0,
    "answer": {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
        "email": "owner@example.invalid",
        "orgName": "Acme Org",
        "orgId": "org-123",
    },
}
TOOL_EVENTS = ("PreToolUse", "PostToolUse")
DESKTOP_REFUSAL = (
    "--desktop opens the Claude Desktop app and can't run non-interactively (`--print`,"
    " `--sdk-url`, `--init-only`, or redirected output)"
)


class Terminated(BaseException):
    """SIGTERM arrived. Not an `Exception`: nothing on the way may swallow it."""


class ScenarioFailed(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # the CLI exits 1 on a bad flag, not argparse's 2
        sys.stderr.write(f"error: {message}\n")
        raise SystemExit(1)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = _Parser(prog="claude", add_help=False, allow_abbrev=False)
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("-p", "--print", action="store_true", dest="print_mode")
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--resume")
    parser.add_argument("--mcp-config", nargs="+", default=[])
    parser.add_argument("--permission-mode", choices=PERMISSION_MODES, default="default")
    parser.add_argument(
        "--allowedTools", "--allowed-tools", nargs="+", action="extend", default=[], dest="allowed"
    )
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=EFFORTS)
    parser.add_argument("--chrome", action="store_true")
    parser.add_argument("--settings", action="append", default=[])
    parser.add_argument("--setting-sources", default="user,project,local", dest="setting_sources")
    parser.add_argument("--add-dir", nargs="+", action="extend", default=[], dest="add_dirs")
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--desktop", action="store_true")
    args = parser.parse_args(argv)
    if args.version or args.desktop:
        return args
    if args.bare:
        # relaybay's runner must never pass it: bare mode skips hooks and the subscription login.
        parser.error("--bare is refused here: it skips hooks and the subscription login")
    if not args.print_mode:
        parser.error("this stand-in only runs with -p")
    if args.output_format == "stream-json" and not args.verbose:
        parser.error("When using --print, --output-format=stream-json requires --verbose")
    return args


def allowed_by_rule(rule: str, tool: str, tool_input: dict[str, Any]) -> bool:
    """One `--allowedTools` entry: `Bash`, `Bash(git diff *)`, `mcp__server`, `mcp__server__*`."""
    found = re.fullmatch(r"([^()]+)\((.*)\)", rule)
    if found:
        command = str(tool_input.get("command", ""))
        return found[1] == tool and fnmatchcase(command, found[2])
    if rule.startswith("mcp__") and "__" not in rule[5:]:
        return tool.startswith(rule + "__")
    return fnmatchcase(tool, rule)


def hook_handlers(settings: dict[str, Any], event: str, tool: str | None) -> list[dict[str, Any]]:
    """The event's handlers; an identical command handler from several sources runs once."""
    handlers, seen = [], set()
    for group in settings.get("hooks", {}).get(event, []):
        matcher = group.get("matcher") or "*"
        if event in TOOL_EVENTS and matcher != "*" and not re.fullmatch(matcher, tool or ""):
            continue
        for handler in group.get("hooks", []):
            key = (handler.get("type"), handler.get("command"))
            if handler.get("type") == "command" and key in seen:
                continue
            seen.add(key)
            handlers.append(handler)
    return handlers


def load_settings(args: argparse.Namespace, config_dir: Path) -> dict[str, Any]:
    """The hooks of the user layer (if its source is on) and of each --settings, in that order."""
    layers = []
    user_file = config_dir / "settings.json"
    if "user" in args.setting_sources.split(",") and user_file.exists():
        layers.append(json.loads(user_file.read_text(encoding="utf-8")))
    for value in args.settings:
        text = value if value.lstrip().startswith("{") else Path(value).read_text("utf-8")
        layers.append(json.loads(text))
    hooks: dict[str, list[Any]] = {}
    for layer in layers:
        for event, groups in layer.get("hooks", {}).items():
            hooks.setdefault(event, []).extend(groups)
    return {"hooks": hooks}


def memory_files(args: argparse.Namespace, config_dir: Path) -> list[Path]:
    files = [config_dir / "CLAUDE.md", Path.cwd() / "CLAUDE.md", Path.cwd() / "CLAUDE.local.md"]
    if os.environ.get("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD") == "1":
        for folder in args.add_dirs:
            files += [Path(folder) / "CLAUDE.md", Path(folder) / "CLAUDE.local.md"]
    return [f for f in files if f.is_file()]


class Run:
    def __init__(self, args: argparse.Namespace, scenario: dict[str, Any], config_dir: Path):
        self.args = args
        self.scenario = scenario
        self.resuming = bool(args.resume)
        self.session_id = args.resume or scenario.get("session_id") or str(uuid.uuid4())
        self.cwd = str(Path.cwd())
        self.model = scenario.get("model", DEFAULT_MODEL)
        self.usage = scenario.get("usage", DEFAULT_USAGE)
        self.started = time.monotonic()
        self.values = {"session_id": self.session_id, "cwd": Path.cwd().as_posix()}
        self.denials: list[dict[str, Any]] = []
        self.turns = 0
        self.total = dict.fromkeys(DEFAULT_USAGE, 0)
        self.last_text = ""
        self.parent: str | None = None
        self.stopped: str | None = None  # a hook's `continue: false`
        self.mcp: dict[str, dict[str, Any]] = {}
        self.steps: list[dict[str, Any]] = []
        self.settings = load_settings(args, config_dir)
        self.memory = memory_files(args, config_dir)
        # The tail only: a temporary folder's whole name passes Windows' path limit.
        slug = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)[-40:]
        self.transcript = config_dir / "projects" / slug / f"{self.session_id}.jsonl"
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        trace = os.environ.get("FAKE_CLAUDE_TRACE")
        self.trace_file = Path(trace) if trace else None

    # --- output -------------------------------------------------------------------------------

    def emit(self, message: dict[str, Any]) -> None:
        if self.args.output_format == "stream-json":
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def trace(self, **entry: Any) -> None:
        if self.trace_file is not None:
            with self.trace_file.open("a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")

    def write_transcript(self, entry: dict[str, Any]) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        line = {**entry, "timestamp": stamp, "sessionId": self.session_id}
        with self.transcript.open("a", encoding="utf-8") as out:
            out.write(json.dumps(line) + "\n")

    def assistant(self, content: list[dict[str, Any]]) -> None:
        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": content,
            "stop_reason": None,
            "usage": self.usage,
        }
        self.turns += 1
        for key in self.total:
            self.total[key] += self.usage.get(key, 0)
        self.write_transcript({"type": "assistant", "message": message})
        self.emit(
            {
                "type": "assistant",
                "uuid": str(uuid.uuid4()),
                "session_id": self.session_id,
                "parent_tool_use_id": self.parent,
                "message": message,
            }
        )

    def tool_result(self, tool_use_id: str, text: str, is_error: bool = False) -> None:
        content = [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
                "is_error": is_error,
            }
        ]
        message = {"role": "user", "content": content}
        self.write_transcript({"type": "user", "message": message})
        self.emit(
            {
                "type": "user",
                "uuid": str(uuid.uuid4()),
                "session_id": self.session_id,
                "parent_tool_use_id": self.parent,
                "message": message,
                "tool_use_result": text,
            }
        )

    def result(self, subtype: str = "success", is_error: bool = False) -> None:
        self.emit(
            {
                "type": "result",
                "subtype": subtype,
                "uuid": str(uuid.uuid4()),
                "session_id": self.session_id,
                "duration_ms": int((time.monotonic() - self.started) * 1000),
                "duration_api_ms": 0,
                "is_error": is_error,
                "num_turns": self.turns,
                "result": self.last_text,
                "stop_reason": "end_turn",
                "total_cost_usd": 0,
                "usage": self.total,
                "modelUsage": {self.model: self.total},
                "permission_denials": self.denials,
            }
        )
        if self.args.output_format == "text":
            sys.stdout.write(self.last_text + "\n")
        elif self.args.output_format == "json":
            sys.stdout.write(json.dumps({"session_id": self.session_id, "result": self.last_text}))

    # --- hooks --------------------------------------------------------------------------------

    def run_hooks(self, event: str, **fields: Any) -> list[dict[str, Any]]:
        """Run the event's command hooks and return their JSON answers. As in Claude Code a hook
        that fails, hangs or prints no JSON changes nothing; exit code 2 is a blocking error."""
        payload = {
            "session_id": self.session_id,
            "transcript_path": str(self.transcript),
            "cwd": self.cwd,
            "permission_mode": self.args.permission_mode,
            "hook_event_name": event,
            **fields,
        }
        answers = []
        for handler in hook_handlers(self.settings, event, fields.get("tool_name")):
            if handler.get("type") != "command":
                raise ScenarioFailed(f"only command hooks are supported, got {handler}")
            if handler.get("asyncRewake"):
                continue  # a background wake-up hook: outside a -p run
            self.trace(kind="hook", event=event, command=handler["command"])
            answer = self._run_hook(handler, payload)
            if answer is not None:
                answers.append(answer)
        for answer in answers:
            context = answer.get("hookSpecificOutput", {}).get("additionalContext")
            if context:
                self.trace(kind="context", event=event, text=context)
            if answer.get("continue") is False and self.stopped is None:
                self.stopped = str(answer.get("stopReason", ""))
                self.trace(kind="stopped", event=event, reason=self.stopped)
        return answers

    def _run_hook(self, handler: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        words = shlex.split(handler["command"])
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            done = subprocess.run(
                words,
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                timeout=handler.get("timeout", HOOK_TIMEOUT_DEFAULT),
                check=False,
                creationflags=no_window,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode == 2:
            reason = done.stderr.decode("utf-8", "replace").strip()
            if payload["hook_event_name"] == "PreToolUse":
                return {
                    "hookSpecificOutput": {
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            return {"decision": "block", "reason": reason}
        if done.returncode != 0:
            return None
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None

    # --- permissions --------------------------------------------------------------------------

    def needs_approval(self, tool: str, tool_input: dict[str, Any]) -> bool:
        mode = self.args.permission_mode
        if mode == "bypassPermissions" or tool in NO_APPROVAL_TOOLS:
            return False
        if mode == "acceptEdits" and tool in EDIT_TOOLS:
            return False
        rules = [r.strip() for value in self.args.allowed for r in value.split(",") if r.strip()]
        return not any(allowed_by_rule(rule, tool, tool_input) for rule in rules)

    def deny(
        self, tool: str, tool_use_id: str, tool_input: dict[str, Any], kind: str, why: str
    ) -> None:
        self.emit(
            {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": tool,
                "tool_use_id": tool_use_id,
                "decision_reason_type": kind,
                "decision_reason": why,
                "message": why,
                "uuid": str(uuid.uuid4()),
                "session_id": self.session_id,
            }
        )
        self.denials.append(
            {"tool_name": tool, "tool_use_id": tool_use_id, "tool_input": tool_input}
        )
        self.tool_result(tool_use_id, why, is_error=True)

    def pre_tool(
        self,
        tool: str,
        tool_use_id: str,
        tool_input: dict[str, Any],
        classifier_denies: bool = False,
    ) -> bool:
        """True when the call may go on; a denial has then been reported."""
        approved = False
        answers = self.run_hooks(
            "PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=tool_use_id
        )
        if self.stopped is not None:
            self.deny(tool, tool_use_id, tool_input, "hook", self.stopped or "stopped by a hook")
            return False
        for answer in answers:
            out = answer.get("hookSpecificOutput", {})
            decision = out.get("permissionDecision")
            why = str(out.get("permissionDecisionReason", ""))
            if decision == "deny":
                self.deny(tool, tool_use_id, tool_input, "hook", why)
                return False
            if decision == "ask":  # nobody can answer a prompt under -p
                self.deny(tool, tool_use_id, tool_input, "hook", why)
                return False
            approved = approved or decision == "allow"
        if (
            not approved
            and self.args.permission_mode == "auto"
            and self.scenario.get("auto_classifier")
        ):
            if not classifier_denies:
                return True
            why = f"Permission for {tool} was denied by the auto mode classifier."
            self.deny(tool, tool_use_id, tool_input, "classifier", why)
            return False
        if not approved and self.needs_approval(tool, tool_input):
            why = f"Claude requested permissions to use {tool}, but it has not been granted."
            self.deny(tool, tool_use_id, tool_input, "mode", why)
            return False
        return True

    # --- MCP ----------------------------------------------------------------------------------

    def load_mcp_config(self) -> list[dict[str, Any]]:
        """`mcp_server_errors` for entries the CLI skips; the rest land in `self.mcp`."""
        errors = []
        for value in self.args.mcp_config:
            text = value if value.lstrip().startswith("{") else Path(value).read_text("utf-8")
            for name, entry in json.loads(text).get("mcpServers", {}).items():
                if "url" in entry and "type" not in entry:
                    errors.append(
                        {"name": name, "type": "url_missing_type", "message": "no type"},
                    )
                else:
                    self.mcp[name] = entry
        return errors

    async def _client_call(self, server: str, tool: str | None, arguments: dict[str, Any]) -> Any:
        import httpx2
        from mcp.client import Client
        from mcp.client.streamable_http import streamable_http_client

        entry = self.mcp[server]
        http = httpx2.AsyncClient(
            headers=entry.get("headers", {}), timeout=httpx2.Timeout(30, read=60)
        )
        transport = streamable_http_client(entry["url"], http_client=http)
        async with Client(transport, mode="legacy") as client:
            if tool is None:
                return [t.name for t in (await client.list_tools()).tools]
            return await client.call_tool(tool, arguments)

    def connect_mcp(self) -> tuple[list[dict[str, str]], list[str]]:
        servers, tools = [], []
        for name in self.mcp:
            try:
                names = asyncio.run(self._client_call(name, None, {}))
            except Exception:
                servers.append({"name": name, "status": "failed"})
                continue
            servers.append({"name": name, "status": "connected"})
            tools += [f"mcp__{name}__{tool}" for tool in names]
        return servers, tools

    # --- steps --------------------------------------------------------------------------------

    def fill(self, value: Any) -> Any:
        if isinstance(value, str):
            return Template(value).safe_substitute(self.values)
        if isinstance(value, list):
            return [self.fill(item) for item in value]
        if isinstance(value, dict):
            return {key: self.fill(item) for key, item in value.items()}
        return value

    def run_steps(self, steps: list[dict[str, Any]]) -> None:
        for step in steps:
            if self.stopped is not None:
                return
            self.run_step(step)

    def run_step(self, step: dict[str, Any]) -> None:
        if "text" in step:
            self.last_text = self.fill(step["text"])
            self.assistant([{"type": "text", "text": self.last_text}])
        elif "tool" in step:
            for _ in range(step.get("tries", 1)):
                before = len(self.denials)
                self.call_tool(step)
                if len(self.denials) > before:
                    break
        elif "mcp" in step:
            self.call_mcp(step)
        elif "wait_file" in step:
            self.wait_file(Path(self.fill(step["wait_file"])), step.get("timeout", 30))
        elif "spawn_child" in step:
            self.spawn_child(Path(self.fill(step["spawn_child"])))
        elif "exit" in step:
            sys.stdout.flush()
            os._exit(int(step["exit"]))
        else:
            raise ScenarioFailed(f"unknown step: {step}")

    def wait_file(self, path: Path, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() > deadline:
                raise ScenarioFailed(f"{path} did not appear within {timeout} s")
            time.sleep(0.02)  # short: on Windows a long sleep is not cut by every signal

    def spawn_child(self, pidfile: Path) -> None:
        """A grandchild of the runner: a build or a server the agent started. It inherits no
        pipe, so only the process tree ties it to the turn."""
        code = (
            "import os, sys, time; from pathlib import Path;"
            " Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(600)"
        )
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [sys.executable, "-c", code, str(pidfile)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_window,
        )
        self.wait_file(pidfile, 30)

    def turn_steps(self, config_dir: Path) -> list[dict[str, Any]]:
        """`turns` by the number of earlier turns of this session, else `turn`."""
        turns = self.scenario.get("turns")
        if not turns:
            return self.scenario.get("turn", [])
        counter = config_dir / "fake-turns" / self.session_id
        counter.parent.mkdir(parents=True, exist_ok=True)
        done = int(counter.read_text("utf-8")) if counter.exists() else 0
        counter.write_text(str(done + 1), encoding="utf-8")
        return turns[min(done, len(turns) - 1)]

    def call_tool(self, step: dict[str, Any]) -> None:
        tool = step["tool"]
        tool_input = self.fill(step.get("input", {}))
        tool_use_id = f"toolu_{uuid.uuid4().hex}"
        self.assistant([{"type": "tool_use", "id": tool_use_id, "name": tool, "input": tool_input}])
        if not self.pre_tool(tool, tool_use_id, tool_input, step.get("classifier") == "deny"):
            return
        if step.get("steps"):
            outer, self.parent = self.parent, tool_use_id
            try:
                self.run_steps(step["steps"])
            finally:
                self.parent = outer
        text = self.fill(step.get("result", ""))
        self.tool_result(tool_use_id, text)
        self.run_hooks(
            "PostToolUse",
            tool_name=tool,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            tool_response=step.get("response", {"stdout": text}),
        )

    def call_mcp(self, step: dict[str, Any]) -> None:
        server = step.get("server", "relaybay")
        tool = f"mcp__{server}__{step['mcp']}"
        arguments = self.fill(step.get("arguments", {}))
        tool_use_id = f"toolu_{uuid.uuid4().hex}"
        self.assistant([{"type": "tool_use", "id": tool_use_id, "name": tool, "input": arguments}])
        if not self.pre_tool(tool, tool_use_id, arguments):
            return
        if server not in self.mcp:
            raise ScenarioFailed(f"no MCP server {server} in --mcp-config")
        answer = asyncio.run(self._client_call(server, step["mcp"], arguments))
        text = "\n".join(c.text for c in answer.content if hasattr(c, "text"))
        if answer.is_error:
            raise ScenarioFailed(f"{step['mcp']} failed: {text}")
        for name, pattern in step.get("capture", {}).items():
            found = re.search(pattern, text)
            if not found:
                raise ScenarioFailed(f"{step['mcp']}: {pattern!r} is not in {text!r}")
            self.values[name] = found[1]
        self.tool_result(tool_use_id, text)
        self.run_hooks(
            "PostToolUse",
            tool_name=tool,
            tool_input=arguments,
            tool_use_id=tool_use_id,
            tool_response=[{"type": "text", "text": text}],
        )

    # --- the session --------------------------------------------------------------------------

    def turn(self) -> None:
        prompt = self.args.prompt or ""
        answers = self.run_hooks("UserPromptSubmit", prompt=prompt)
        self.write_transcript({"type": "user", "message": {"role": "user", "content": prompt}})
        blocked = [a for a in answers if a.get("decision") == "block"]
        if blocked:
            self.trace(kind="stopped", event="UserPromptSubmit", reason=blocked[0].get("reason"))
            return
        self.run_steps(self.steps)
        active = False
        pending = self.scenario.get("on_stop_block", [])
        for _ in range(STOP_BLOCK_LIMIT):
            if self.stopped is not None:
                return
            answers = self.run_hooks("Stop", stop_hook_active=active)
            blocks = [a for a in answers if a.get("decision") == "block"]
            if not blocks or self.stopped is not None:
                return
            self.trace(kind="stop_block", reason=blocks[0].get("reason", ""))
            active = True
            self.run_steps(pending)
            pending = []
        raise ScenarioFailed(f"a Stop hook blocked {STOP_BLOCK_LIMIT} times in a row")

    def session(self) -> None:
        lines = [
            line.strip()
            for path in self.memory
            for line in path.read_text("utf-8").splitlines()
            if line.strip()
        ]
        self.trace(kind="memory", files=[str(p) for p in self.memory], lines=lines)
        errors = self.load_mcp_config()
        self.run_hooks(
            "SessionStart", source="resume" if self.resuming else "startup", model=self.model
        )
        servers, mcp_tools = self.connect_mcp()
        init = {
            "type": "system",
            "subtype": "init",
            "uuid": str(uuid.uuid4()),
            "session_id": self.session_id,
            "apiKeySource": "none",
            "claude_code_version": VERSION.split()[0],
            "cwd": self.cwd,
            "tools": [*self.scenario.get("tools", ["Bash", "Read"]), *mcp_tools],
            "mcp_servers": [*self.scenario.get("configured_servers", []), *servers],
            "model": self.model,
            "permissionMode": self.args.permission_mode,
            "slash_commands": [],
            "output_style": "default",
            "skills": [],
            "plugins": [],
        }
        if errors:
            init["mcp_server_errors"] = errors
        self.emit(init)
        self.turn()
        self.result()

    def end(self) -> None:
        wait = self.scenario.get("end_wait_file")
        if wait:
            self.trace(kind="result")
            self.wait_file(Path(self.fill(wait)), 60)
        self.run_hooks("SessionEnd", reason="other")


def _terminate(signum: int, frame: Any) -> None:
    raise Terminated


def auth_status() -> int:
    """`claude auth status`: the JSON answer and exit code of FAKE_CLAUDE_AUTH, or a
    subscription sign-in."""
    path = os.environ.get("FAKE_CLAUDE_AUTH")
    auth = json.loads(Path(path).read_text("utf-8")) if path else DEFAULT_AUTH
    answer = auth.get("answer")
    if isinstance(answer, str):
        sys.stdout.write(answer)
    else:
        sys.stdout.write(json.dumps(answer, indent=2) + "\n")
    return int(auth.get("exit", 0))


def desktop(words: list[str]) -> int:
    trace = os.environ.get("FAKE_CLAUDE_TRACE")
    tty = sys.stdout is not None and sys.stdout.isatty()
    if trace:
        entry = {"kind": "desktop", "argv": words, "cwd": str(Path.cwd()), "tty": tty}
        with Path(trace).open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry) + "\n")
    if not tty:
        sys.stderr.write(DESKTOP_REFUSAL + "\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if words[:2] == ["auth", "status"]:
        return auth_status()
    started = time.time()
    args = parse_args(words)
    if args.version:
        print(VERSION)
        return 0
    if args.desktop:
        return desktop(words)
    if args.prompt is None and sys.stdin is not None and not sys.stdin.isatty():
        args.prompt = sys.stdin.read()
    # Never the real settings and transcripts of whoever runs the tests.
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    scenario_file = os.environ.get("FAKE_CLAUDE_SCENARIO")
    if not config or not scenario_file:
        sys.stderr.write("error: CLAUDE_CONFIG_DIR and FAKE_CLAUDE_SCENARIO must be set\n")
        return 2
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    run = Run(args, json.loads(Path(scenario_file).read_text("utf-8")), Path(config))
    run.trace(
        kind="argv",
        argv=words,
        prompt=args.prompt,
        run_id=os.environ.get("RELAYBAY_RUN_ID"),
        started=started,
    )
    run.steps = run.turn_steps(Path(config))
    signal.signal(signal.SIGTERM, _terminate)
    if sys.platform == "win32":
        # Windows has no SIGTERM between processes; a console break is the nearest thing.
        signal.signal(signal.SIGBREAK, _terminate)
    code = 0
    try:
        run.session()
    except Terminated:
        code = EXIT_TERMINATED  # the turn stays unfinished: no result message
    except ScenarioFailed as exc:
        sys.stderr.write(f"error: {exc}\n")
        run.result("error_during_execution", is_error=True)
        code = 1
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # exiting: only SessionEnd still runs
    run.end()
    run.trace(kind="turn_end", session_id=run.session_id, started=started, ended=time.time())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
