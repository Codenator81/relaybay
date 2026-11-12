"""The end-to-end stand: a real server on a free port, hooks installed by the real `machine_setup`
into a temporary Claude home, and `claude` on PATH being `fake_claude.py` behind a shim."""

import asyncio
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from relaybay import machine_setup
from relaybay.config import Settings, load_settings
from relaybay.desktop.server_thread import EmbeddedServer, PortTaken
from relaybay.security import csrf_token
from tests.conftest import LiveServer
from tests.test_desktop_server_thread import free_port
from tests.test_mcp import call as mcp_call
from tests.test_mcp import connect as mcp_connect

FAKE = Path(__file__).with_name("fake_claude.py")
WAIT = 30.0  # for one run or one condition; a slow Windows runner stays well inside it
# Set when the tests run inside a Claude Code session; they would change who a session is.
HOST_VARIABLES = (
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "RELAYBAY_TOKEN",
)


def poll(check: Callable[[], Any], timeout: float = WAIT, what: str = "the condition") -> Any:
    """Wait until `check()` is truthy and return it: timing on a CI runner is never assumed."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not hold within {timeout} s")
        time.sleep(0.05)


def write_shim(folder: Path) -> Path:
    """`claude` for this OS, found by `shutil.which` like the real one."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        shim = folder / "claude.cmd"
        shim.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8", newline="")
    else:
        shim = folder / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return shim


@dataclass
class FakeRun:
    """One running `claude -p`, read as a runner would read it."""

    process: subprocess.Popen
    trace_file: Path
    messages: list[dict[str, Any]] = field(default_factory=list)
    stderr: str = ""

    def __post_init__(self) -> None:
        self._threads = [
            threading.Thread(target=self._read_out, daemon=True),
            threading.Thread(target=self._read_err, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _read_out(self) -> None:
        for line in self.process.stdout:
            self.messages.append(json.loads(line))

    def _read_err(self) -> None:
        self.stderr = self.process.stderr.read()

    def wait(self, timeout: float = WAIT) -> int:
        code = self.process.wait(timeout)
        for thread in self._threads:
            thread.join(timeout)
        return code

    def finish(self, timeout: float = WAIT) -> "FakeRun":
        """Wait for a run that must succeed."""
        code = self.wait(timeout)
        assert code == 0, f"exit {code}: {self.stderr}"
        return self

    def of(self, kind: str, subtype: str | None = None) -> list[dict[str, Any]]:
        return [
            m
            for m in self.messages
            if m["type"] == kind and (subtype is None or m.get("subtype") == subtype)
        ]

    @property
    def result(self) -> dict[str, Any]:
        return self.of("result")[-1]

    @property
    def init(self) -> dict[str, Any]:
        return self.of("system", "init")[0]

    def tool_result_text(self, tool: str) -> str | None:
        """The text answered to the latest finished call of `tool`, if any."""
        ids = [
            block["id"]
            for m in self.of("assistant")
            for block in m["message"]["content"]
            if block["type"] == "tool_use" and block["name"] == tool
        ]
        found = None
        for message in self.of("user"):
            for block in message["message"]["content"]:
                if block["tool_use_id"] in ids:
                    found = block["content"]
        return found

    def trace(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.trace_file.exists():
            return []
        entries = [json.loads(x) for x in self.trace_file.read_text("utf-8").splitlines()]
        return [e for e in entries if kind is None or e["kind"] == kind]

    def stop_kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(10)


@dataclass
class Stand:
    root: Path
    settings: Settings
    token: str
    claude_home: Path
    project_dir: Path
    env: dict[str, str]
    server: EmbeddedServer
    runs: list[FakeRun] = field(default_factory=list)

    @property
    def url(self) -> str:
        return self.settings.base_url

    @property
    def claude(self) -> str:
        found = shutil.which("claude", path=self.env["PATH"])
        assert found is not None
        return found

    @property
    def mcp_config(self) -> Path:
        path = self.root / "mcp-config.json"
        entry = {
            "type": "http",
            "url": f"{self.url}/mcp",
            "headers": {"Authorization": f"Bearer {self.token}"},
        }
        path.write_text(json.dumps({"mcpServers": {"relaybay": entry}}), encoding="utf-8")
        return path

    def start_server(self) -> None:
        """Start again on the same port: a Windows port can stay taken for a moment after a stop."""
        server = EmbeddedServer(self.settings)

        def started() -> bool:
            try:
                server.start()
            except PortTaken:
                return False
            return True

        poll(started, what="the port coming free")
        self.server = server

    def stop_server(self) -> None:
        assert self.server.stop()

    def run(
        self,
        scenario: dict[str, Any],
        *flags: str,
        prompt: str = "Work on the task",
        mcp: bool = True,
    ) -> FakeRun:
        """Start `claude -p` in the project folder with the options a runner would pass. `mcp`:
        a run without MCP calls may go without, the SDK takes two seconds to import on Windows."""
        name = uuid.uuid4().hex
        scenario_file = self.root / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        trace_file = self.root / f"trace-{name}.jsonl"
        command = [
            self.claude,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            *(("--mcp-config", str(self.mcp_config)) if mcp else ()),
            "--allowedTools",
            "Bash,mcp__relaybay",
            *flags,
        ]
        process = subprocess.Popen(
            command,
            cwd=self.project_dir,
            env={
                **self.env,
                "FAKE_CLAUDE_SCENARIO": str(scenario_file),
                "FAKE_CLAUDE_TRACE": str(trace_file),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        run = FakeRun(process, trace_file)
        self.runs.append(run)
        return run

    def mcp(self, tool: str, **arguments: Any) -> str:
        """A tool call by the test itself (what another session or the coordinator would do)."""

        async def go() -> str:
            live = LiveServer(url=self.url, token=self.token, settings=self.settings)
            async with mcp_connect(live) as client:
                return await mcp_call(client, tool, **arguments)

        return asyncio.run(go())

    def mcp_error(self, tool: str, **arguments: Any) -> str:
        """The error text of a tool call that has to fail."""

        async def go() -> str:
            live = LiveServer(url=self.url, token=self.token, settings=self.settings)
            async with mcp_connect(live) as client:
                result = await client.call_tool(tool, arguments)
                assert result.is_error, result.content
                return result.content[0].text

        return asyncio.run(go())

    @contextmanager
    def board(self) -> Iterator[httpx.Client]:
        """A browser: signed in with the cookie, posting forms with the CSRF field."""
        with httpx.Client(
            base_url=self.url, follow_redirects=True, timeout=30, trust_env=False
        ) as client:
            assert client.get(f"/login?token={self.token}").status_code == 200
            yield client

    def post_form(self, board: httpx.Client, path: str, **fields: str) -> httpx.Response:
        response = board.post(
            path,
            data={"csrf_token": csrf_token(self.token), **fields},
            headers={"Origin": self.url},
        )
        assert response.status_code == 200, response.text
        return response

    def events(self, project: str = "acme-shop") -> str:
        return self.mcp("events_since", project=project, limit=500)

    @property
    def spool(self) -> Path:
        return Path(os.environ["RELAYBAY_CONFIG_DIR"]) / "hook-spool.jsonl"


@pytest.fixture
def stand_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Stand]]:
    made: list[Stand] = []

    def make(guard_mode: str | None = None) -> Stand:
        shims = tmp_path / "bin"
        write_shim(shims)
        claude_home = tmp_path / "claude-home"
        project_dir = tmp_path / "acme-shop"
        project_dir.mkdir()
        environ = {"RELAYBAY_GUARD_MODE": guard_mode} if guard_mode else {}
        settings = load_settings(home=tmp_path / "data", port=free_port(), environ=environ)
        settings.claude_home = claude_home
        settings.usage_interval_seconds = 1
        server = EmbeddedServer(settings)
        server.start()
        token = settings.token_path.read_text(encoding="utf-8").strip()

        for name in HOST_VARIABLES:
            monkeypatch.delenv(name, raising=False)
        path = f"{shims}{os.pathsep}{os.environ['PATH']}"
        monkeypatch.setenv("PATH", path)
        config_dir = claude_home / ".claude"
        config_dir.mkdir(parents=True)
        plan = machine_setup.make_plan(
            base_url=settings.base_url,
            token=token,
            token_path=settings.token_path,
            paths=machine_setup.claude_paths(claude_home),
            detection=machine_setup.detect(),
            python=sys.executable,
        )
        assert plan.error is None and plan.hooks is not None
        assert plan.detection.version_tuple is not None, plan.detection
        machine_setup.write_change(plan.hooks, expect=plan.hooks.plan_hash)
        stand = Stand(
            root=tmp_path,
            settings=settings,
            token=token,
            claude_home=claude_home,
            project_dir=project_dir,
            env={**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)},
            server=server,
        )
        made.append(stand)
        return stand

    yield make
    for stand in made:
        for run in stand.runs:
            run.stop_kill()
        stand.server.stop()


@pytest.fixture
def stand(stand_factory: Callable[..., Stand]) -> Stand:
    return stand_factory()


def alive(pid: int) -> bool:
    """Whether a process still runs (a zombie child counts as gone)."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    # `ps` on macOS and Linux; reaping here would take the exit code from the runner's wait.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


@dataclass
class Runs:
    """Runs on cards against the stand: the server starts the fake `claude` itself, with the
    scenario, trace and `auth status` answer the test writes into these files."""

    stand: Stand
    scenario_file: Path
    trace_file: Path
    auth_file: Path

    def scenario(self, data: dict[str, Any]) -> None:
        self.scenario_file.write_text(json.dumps(data), encoding="utf-8")

    def auth(self, answer: dict[str, Any] | str, code: int = 0) -> None:
        self.auth_file.write_text(json.dumps({"exit": code, "answer": answer}), encoding="utf-8")

    def trace(self, kind: str) -> list[dict[str, Any]]:
        if not self.trace_file.exists():
            return []
        entries = [json.loads(x) for x in self.trace_file.read_text("utf-8").splitlines()]
        return [e for e in entries if e["kind"] == kind]

    def rows(self, sql: str, *params: Any) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.stand.settings.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def runs(self, key: str = "acme-shop-1") -> list[sqlite3.Row]:
        return self.rows(
            "SELECT r.* FROM run r JOIN task t ON t.id = r.task_id WHERE t.key = ? ORDER BY r.id",
            key,
        )

    def turns(self, run_id: int) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM run_turn WHERE run_id = ? ORDER BY seq", run_id)

    def wait_run(self, check: Callable[[sqlite3.Row], bool], key: str = "acme-shop-1") -> Any:
        def found() -> sqlite3.Row | None:
            rows = self.runs(key)
            return rows[-1] if rows and check(rows[-1]) else None

        return poll(found, what=f"the run of {key}")

    def post(self, board: httpx.Client, path: str, **fields: str) -> str:
        """An htmx post of the Agent block: the panel comes back, with the reason on a refusal."""
        response = board.post(
            path,
            data={"csrf_token": csrf_token(self.stand.token), **fields},
            headers={"Origin": self.stand.url, "HX-Request": "true"},
        )
        assert response.status_code == 200, response.text
        return response.text

    def start(self, board: httpx.Client, key: str = "acme-shop-1", **fields: str) -> str:
        return self.post(board, f"/p/acme-shop/t/{key}/run/start", **fields)

    def act(self, board: httpx.Client, run_id: int, action: str, **fields: str) -> str:
        key = self.rows(
            "SELECT t.key FROM run r JOIN task t ON t.id = r.task_id WHERE r.id = ?", run_id
        )[0][0]
        return self.post(board, f"/p/acme-shop/t/{key}/run/{run_id}/{action}", **fields)


@pytest.fixture
def runs(stand: Stand, monkeypatch: pytest.MonkeyPatch) -> Runs:
    """A project with the stand's folder as its repository, card acme-shop-1, relaybay's MCP
    server registered in the stand's Claude home (setup installed the hooks), and the fake's
    files in the environment the server passes on to the `claude` it starts."""
    made = Runs(
        stand,
        stand.root / "run-scenario.json",
        stand.root / "run-trace.jsonl",
        stand.root / "run-auth.json",
    )
    made.scenario({"turn": [{"text": "Done."}]})
    made.auth(FAKE_AUTH)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", stand.env["CLAUDE_CONFIG_DIR"])
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(made.scenario_file))
    monkeypatch.setenv("FAKE_CLAUDE_TRACE", str(made.trace_file))
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", str(made.auth_file))
    # A run's claude uses the server's home folder: the stand's Claude home is that folder.
    monkeypatch.setenv("HOME", str(stand.claude_home))
    monkeypatch.setenv("USERPROFILE", str(stand.claude_home))
    entry = {"type": "http", "url": f"{stand.url}/mcp", "headers": {"Authorization": "Bearer x"}}
    (stand.claude_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"relaybay": entry}}), encoding="utf-8"
    )
    stand.mcp("project_upsert", slug="acme-shop", repo_path=str(stand.project_dir))
    stand.mcp("task_create", project="acme-shop", title="Checkout", stage="dev")
    return made


FAKE_AUTH = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "subscriptionType": "max",
    "email": "owner@example.invalid",
    "orgName": "Acme Org",
    "orgId": "org-123",
}
