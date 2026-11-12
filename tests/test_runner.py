"""The runner without a database: argv and environment of a turn, the stream lines it reads, the
claude it finds, and the process tree it kills."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from relaybay.domain.errors import ValidationError
from relaybay.runner.auth import LoginState
from relaybay.runner.claude_cli import (
    TurnSpec,
    build_command,
    child_env,
    find_claude,
    is_batch,
)
from relaybay.runner.manager import RunManager, RunnerConfig
from relaybay.runner.proctree import ProcessTree
from relaybay.runner.stream import LINE_MAX, Init, Result, parse_line
from tests.e2e.conftest import alive, poll

ENV = dict(os.environ)
FORBIDDEN = ("--bare", "--setting-sources", "--strict-mcp-config", "--dangerously-skip-permissions")


def spec(claude: str = "/usr/local/bin/claude", **changes) -> TurnSpec:
    values = {
        "claude": claude,
        "prompt": "Work on acme-shop-1",
        "cwd": "/code/acme-shop",
        "model": "opus",
        "effort": "medium",
        "mode": "auto",
        "allowed": ("mcp__relaybay", "Bash(git diff *)"),
        **changes,
    }
    return TurnSpec(**values)


def test_first_turn_argv_puts_the_prompt_after_p() -> None:
    argv, stdin = build_command(spec())
    assert stdin is None
    assert argv == [
        "/usr/local/bin/claude",
        "-p",
        "Work on acme-shop-1",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--effort",
        "medium",
        "--permission-mode",
        "auto",
        "--allowedTools",
        "mcp__relaybay",
        "--allowedTools",
        "Bash(git diff *)",
    ]
    assert not set(argv) & set(FORBIDDEN)


def test_resume_and_chrome_come_last_and_a_dash_prompt_stays_text() -> None:
    argv, _ = build_command(spec(resume="cc-1", chrome=True, prompt="-p is not a flag here"))
    assert argv[2] == " -p is not a flag here"
    assert argv[-3:] == ["--chrome", "--resume", "cc-1"]


def test_batch_shim_takes_the_prompt_on_stdin() -> None:
    claude = "C:/Users/dev/AppData/Roaming/npm/claude.cmd"
    argv, stdin = build_command(spec(claude, prompt='say "hi" & 100% done', resume="cc-1"))
    assert stdin == 'say "hi" & 100% done'
    assert argv[:3] == [claude, "-p", "--output-format"]
    assert argv[-2:] == ["--resume", "cc-1"]
    assert is_batch("X:/tools/CLAUDE.BAT") and not is_batch("/usr/bin/claude")


@pytest.mark.parametrize("rule", ['Bash(echo "x")', "Bash(a & b)", "Bash(a | b)", "Bash(50%)"])
def test_batch_shim_refuses_an_argument_cmd_would_reparse(rule: str) -> None:
    with pytest.raises(ValidationError, match=r"cmd.exe"):
        build_command(spec("C:/npm/claude.cmd", allowed=(rule,)))
    build_command(spec("/usr/local/bin/claude", allowed=(rule,)))  # no shell elsewhere


def test_child_env_drops_the_host_session_and_sets_the_run() -> None:
    base = {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_HOST_SESSION_ID": "local_x",
        "CLAUDE_CODE_SESSION_ATTENDED": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
        "CLAUDE_CONFIG_DIR": "/home/dev/.claude-alt",
        "RELAYBAY_TOKEN": "t",
    }
    env = child_env(base, 7)
    assert env == {
        "PATH": "/usr/bin",
        "CLAUDE_CONFIG_DIR": "/home/dev/.claude-alt",
        "RELAYBAY_TOKEN": "t",
        "RELAYBAY_RUN_ID": "7",
    }


def test_parse_line_reads_init_and_result_only() -> None:
    init = {"type": "system", "subtype": "init", "session_id": "cc-1", "tools": ["Bash"]}
    assert parse_line(json.dumps(init) + "\n") == Init("cc-1")
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 1200,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 4,
        },
        "permission_denials": [
            {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git push"}}
        ],
    }
    parsed = parse_line(json.dumps(result))
    assert parsed == Result("success", False, 3, 1200, 1, 2, 3, 4, 1, ["Bash"])
    assert "git push" not in repr(parsed)
    assert parse_line(json.dumps({"type": "assistant", "message": {}})) is None
    assert parse_line("not json") is None
    assert parse_line(" " * (LINE_MAX + 1)) is None


def test_find_claude_order(tmp_path: Path) -> None:
    configured = tmp_path / "my-claude"
    configured.write_text("", encoding="utf-8")
    assert find_claude(configured, {}, which=lambda *_, **__: "/bin/claude") == str(configured)
    assert find_claude(tmp_path / "missing", {}, which=lambda *_, **__: "/bin/claude") is None
    assert find_claude(None, {"PATH": "/bin"}, which=lambda *_, **__: "/bin/claude") == (
        "/bin/claude"
    )
    local = tmp_path / ".local" / "bin" / "claude"
    local.parent.mkdir(parents=True)
    local.write_text("", encoding="utf-8")
    found = find_claude(None, {}, which=lambda *_, **__: None, platform="linux", home=tmp_path)
    assert found == str(local)
    npm = tmp_path / "npm" / "claude.cmd"
    npm.parent.mkdir()
    npm.write_text("", encoding="utf-8")
    env = {"USERPROFILE": str(tmp_path / "none"), "APPDATA": str(tmp_path)}
    found = find_claude(None, env, which=lambda *_, **__: None, platform="win32", home=tmp_path)
    assert found == str(npm)


def test_process_tree_kill_takes_the_grandchild(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    grandchild = (
        "import os, sys, time; from pathlib import Path;"
        " Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(600)"
    )
    parent = (
        "import subprocess, sys, time;"
        f" subprocess.Popen([sys.executable, '-c', {grandchild!r}, sys.argv[1]],"
        " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL);"
        " print('started', flush=True); time.sleep(600)"
    )
    tree = ProcessTree()
    proc = tree.spawn([sys.executable, "-c", parent, str(pidfile)], str(tmp_path), ENV, None)
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "started"
        poll(pidfile.exists, what="the grandchild")
        child = int(pidfile.read_text("utf-8"))
        assert alive(proc.pid) and alive(child)
        tree.terminate()
        proc.wait(30)
        tree.kill()
        poll(lambda: not alive(child), what="the grandchild gone")
    finally:
        tree.kill()
        tree.close()
        if proc.poll() is None:
            proc.kill()


def test_process_tree_writes_stdin_text(tmp_path: Path) -> None:
    tree = ProcessTree()
    code = "import sys; print(sys.stdin.read().upper(), flush=True)"
    proc = tree.spawn([sys.executable, "-c", code], str(tmp_path), ENV, "hello")
    try:
        assert proc.stdin is not None
        proc.stdin.write("hello")
        proc.stdin.close()
        assert proc.stdout is not None and proc.stdout.read().strip() == "HELLO"
        assert proc.wait(30) == 0
    finally:
        tree.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows only")
def test_closing_the_job_kills_what_is_left(tmp_path: Path) -> None:
    tree = ProcessTree()
    proc = tree.spawn(
        [sys.executable, "-c", "import time; time.sleep(600)"], str(tmp_path), ENV, None
    )
    assert alive(proc.pid)
    tree.close()
    deadline = time.monotonic() + 30
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None


class Sink:
    def __init__(self) -> None:
        self.done: list[tuple[str, str | None, bool]] = []

    async def turn_init(self, turn_id: int, claude_session_id: str) -> None:
        return None

    async def turn_done(self, turn_id, outcome, result, code, reason, stopped) -> None:
        self.done.append((outcome, reason, stopped))


def manager(sink: Sink, monkeypatch: pytest.MonkeyPatch, child: str) -> RunManager:
    monkeypatch.setattr(
        "relaybay.runner.manager.build_command", lambda spec: ([sys.executable, "-c", child], None)
    )
    return RunManager(sink, RunnerConfig(Path(), "http://127.0.0.1:1", Path("token")))


async def test_stop_after_the_result_reports_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = (
        "import json, time;"
        " print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False}),"
        " flush=True); time.sleep(30)"
    )
    sink = Sink()
    runs = manager(sink, monkeypatch, child)
    await runs.start_turn(1, 1, spec(cwd=str(tmp_path)))
    await asyncio.sleep(1.0)  # the result is read, the process still ends
    assert await runs.stop(1, "stopped by owner", grace=5)
    assert sink.done == [("success", "stopped by owner", True)]


async def test_no_turn_starts_while_the_server_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = manager(Sink(), monkeypatch, "pass")
    await runs.close()
    with pytest.raises(RuntimeError, match="stopping"):
        await runs.start_turn(1, 1, spec(cwd=str(tmp_path)))


async def test_one_sign_in_check_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def check(claude: str) -> LoginState:
        calls.append(claude)
        time.sleep(0.2)
        return LoginState(True, "claude.ai", None, 0, readable=True)

    monkeypatch.setattr("relaybay.runner.manager.auth.check_login", check)
    runs = RunManager(Sink(), RunnerConfig(Path(), "x", Path("t")))
    runs.claude = lambda: "/bin/claude"  # type: ignore[method-assign]
    states = await asyncio.gather(runs.login(), runs.login(), runs.login(force=True))
    assert len(calls) == 1 and all(s.is_subscription for s in states)
    await runs.login(force=True)  # asked after the last check: runs again
    assert len(calls) == 2
