"""The runner without a database: argv and environment of a turn, the stream lines it reads, the
claude it finds, and the process tree it kills."""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from relaybay.domain.errors import ValidationError
from relaybay.domain.run_events import unpack
from relaybay.runner import desktop, proctree
from relaybay.runner.auth import LoginState
from relaybay.runner.claude_cli import (
    TurnSpec,
    build_command,
    child_env,
    find_claude,
    is_batch,
)
from relaybay.runner.manager import EVENT_BATCH, RunManager, RunnerConfig
from relaybay.runner.proctree import ProcessTree
from relaybay.runner.stream import (
    HEAD_MAX,
    LINE_MAX,
    Init,
    Refusal,
    Result,
    parse_line,
    read_line,
    truncated_event,
)
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


def test_each_folder_and_config_file_gets_its_own_flag_before_resume() -> None:
    argv, _ = build_command(
        spec(
            add_dirs=("/data/worktrees/acme-shop-1-r1/api", "/data/worktrees/acme-shop-1-r1/web"),
            settings_file="/data/runs/1/settings.json",
            mcp_file="/data/runs/1/mcp.json",
            resume="cc-1",
        )
    )
    tail = argv[argv.index("Bash(git diff *)") + 1 :]
    assert tail == [
        "--add-dir",
        "/data/worktrees/acme-shop-1-r1/api",
        "--add-dir",
        "/data/worktrees/acme-shop-1-r1/web",
        "--settings",
        "/data/runs/1/settings.json",
        "--mcp-config",
        "/data/runs/1/mcp.json",
        "--resume",
        "cc-1",
    ]
    assert "--strict-mcp-config" not in argv


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
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
    }


def test_child_env_drops_a_host_sessions_markers_and_keeps_the_users_own() -> None:
    # A server started from inside a Claude Code session: its child marker would turn off the
    # run's transcript ("Transcript saving is off").
    base = {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret-ish",
        "CLAUDE_CODE_DESKTOP_APP_VERSION": "9.9",
        "CLAUDE_AGENT_SDK_VERSION": "1",
        "CLAUDE_PREVIEW_CLASSIFIER_FLOOR": "x",
        "CLAUDE_PID": "123",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_GIT_BASH_PATH": "C:/Git/bin/bash.exe",
        "CLAUDE_CONFIG_DIR": "/home/dev/.claude-alt",
    }
    env = child_env(base, 3)
    assert env == {
        "PATH": "/usr/bin",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_GIT_BASH_PATH": "C:/Git/bin/bash.exe",
        "CLAUDE_CONFIG_DIR": "/home/dev/.claude-alt",
        "RELAYBAY_RUN_ID": "3",
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
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
    assert parsed == Result(
        "success",
        False,
        3,
        1200,
        1,
        2,
        3,
        4,
        1,
        ["Bash"],
        "",
        [Refusal("Bash", "git push", "git push", "t1")],  # the id names the refused call
    )
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


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError])
def test_a_group_with_nothing_left_alive_is_already_stopped(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    # macOS answers EPERM to killpg when only zombies are left in the group.
    def killpg(pid: int, sig: int) -> None:
        raise error(1, "gone")

    monkeypatch.setattr(proctree.os, "killpg", killpg)
    tree = ProcessTree()
    tree.proc = SimpleNamespace(pid=4242)  # type: ignore[assignment]
    tree.terminate()
    tree.kill()


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
        self.batches: list[tuple[float, list]] = []
        self.ended_at: float | None = None
        self.result: Result | None = None

    async def turn_init(self, turn_id: int, claude_session_id: str) -> None:
        return None

    async def turn_events(self, turn_id: int, events: list) -> None:
        self.batches.append((time.monotonic(), events))

    async def turn_done(self, turn_id, outcome, result, code, reason, stopped) -> None:
        self.done.append((outcome, reason, stopped))
        self.ended_at = time.monotonic()
        self.result = result


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


def test_desktop_launch_gets_a_console_of_its_own_and_no_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class Info:
        dwFlags = 0
        wShowWindow = 0

    class Proc:
        def __init__(self, argv: str, **kwargs: Any) -> None:
            calls.append({"argv": argv, **kwargs})
            self.pid = 4321

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and calls[-1]["argv"].endswith("slow"):
                raise desktop.subprocess.TimeoutExpired("claude", timeout)
            return 0

        def kill(self) -> None:
            calls[-1]["killed"] = True

    killed: list[int] = []
    monkeypatch.setattr(desktop.subprocess, "STARTUPINFO", Info, raising=False)
    monkeypatch.setattr(desktop.subprocess, "Popen", Proc)
    monkeypatch.setattr(desktop, "_kill_tree", killed.append)
    err = Path("C:/data/err.txt")
    line = desktop.desktop_command_line("C:/bin/claude.cmd", "abc", err)
    assert line == f'cmd.exe /d /c ""C:/bin/claude.cmd" --desktop --resume abc 2> "{err}""'
    assert desktop.launch_in_console(line, "C:/w", {"A": "1"}, 60) == 0
    [call] = calls
    assert call["creationflags"] == desktop.CREATE_NEW_CONSOLE and call["cwd"] == "C:/w"
    assert call["startupinfo"].dwFlags & desktop.STARTF_USESHOWWINDOW
    assert call["startupinfo"].wShowWindow == 7  # minimized, not focused
    assert not {"stdin", "stdout", "stderr"} & set(call)
    with pytest.raises(desktop.subprocess.TimeoutExpired):
        desktop.launch_in_console("claude slow", "C:/w", {}, 1)
    assert calls[-1]["killed"] and killed == [4321]  # the whole tree, cmd.exe in front


def test_desktop_launcher_only_on_windows_and_env_without_the_host() -> None:
    assert desktop.default_launcher("win32") is desktop.launch_in_console
    assert desktop.default_launcher("linux") is None
    assert desktop.default_launcher("darwin") is None
    env = desktop.desktop_env({"CLAUDECODE": "1", "PATH": "/bin", "RELAYBAY_RUN_ID": "3"})
    assert env == {"PATH": "/bin"}
    assert desktop.terminal_command("abc") == "claude --resume abc"


def test_desktop_command_line_refuses_what_cmd_would_read_and_reads_the_reason(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="would read as a command"):
        desktop.desktop_command_line('C:/a"b/claude.exe', "abc", tmp_path / "e.txt")
    with pytest.raises(ValidationError, match="the data folder"):
        desktop.desktop_command_line("claude.exe", "abc", tmp_path / "100%" / "e.txt")
    reason = tmp_path / "e.txt"
    reason.write_text("\x1b[1mfirst\x1b[0m\n  second " + "x" * 600, encoding="utf-8")
    text = desktop.take_reason(reason)
    assert text.startswith("first second x") and len(text) == desktop.REASON_MAX
    assert text.endswith("…") and not reason.exists()
    assert desktop.take_reason(reason) == ""


# Built at run time so no token shape sits in the source.
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6" * 3
ANTHROPIC_KEY = "sk-ant-" + "api03-" + "Zx9Yw8Vu7Ts6" * 4


def stored(raw: str) -> str:
    event, _ = read_line(raw, 1)
    assert event is not None
    return json.dumps(unpack(event.data))


def test_every_line_is_stored_masked_also_behind_json_escapes() -> None:
    line = {
        "type": "user",
        "parent_tool_use_id": "toolu_1",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": f"x\n{GITHUB_TOKEN}"},
                {"type": "text", "text": f"key {ANTHROPIC_KEY}"},
            ]
        },
    }
    raw = json.dumps(line)
    assert "\\n" + GITHUB_TOKEN in raw  # the escape sits right before the token in the line
    event, item = read_line(raw + "\n", 7)
    assert item is None and event is not None
    assert (event.seq, event.type, event.parent_tool_use_id) == (7, "user", "toolu_1")
    text = json.dumps(unpack(event.data))
    assert GITHUB_TOKEN not in text and ANTHROPIC_KEY not in text
    assert "ghp_***" in text and "sk-***" in text
    escaped = "".join(f"\\u{ord(c):04x}" for c in GITHUB_TOKEN)
    assert GITHUB_TOKEN not in stored('{"type": "assistant", "x": "' + escaped + '"}')
    assert GITHUB_TOKEN not in stored(f"not json {GITHUB_TOKEN}")
    deep = "[" * 200_000 + f'"{GITHUB_TOKEN}"' + "]" * 200_000
    event, item = read_line(deep, 2)  # deeper than Python follows: stored as text, masked
    assert item is None and event is not None and event.type == "invalid"
    assert GITHUB_TOKEN not in json.dumps(unpack(event.data))


def user_text(text: str) -> dict:
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": text}]
    return {"type": "user", "message": {"content": content}}


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        (user_text('{\n  "password": "hunter2pass",\n  "user": "a"\n}'), "hunter2pass"),
        (user_text('{"api_key":"abc123secretvalue"}'), "abc123secretvalue"),
        (user_text("{'password': 'hunter2pass'}"), "hunter2pass"),
        (user_text('{"body":"{\\"password\\":\\"hunter2pass\\"}"}'), "hunter2pass"),
        (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "mcp__db__connect",
                            "input": {"host": "x", "password": "s3cr3tPW"},
                        }
                    ]
                },
            },
            "s3cr3tPW",
        ),
        ({"type": "user", "headers": {"Authorization": "Basic ZXhhbXBsZQ"}}, "ZXhhbXBsZQ"),
        (
            user_text(
                "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQIVBF\n-----END PGP PRIVATE KEY BLOCK-----"
            ),
            "lQIVBF",
        ),
    ],
)
def test_secret_names_and_fields_are_masked_in_every_form(line: dict, secret: str) -> None:
    assert secret in json.dumps(line)
    assert secret not in stored(json.dumps(line))


def test_a_reference_under_a_secret_field_stays_readable() -> None:
    line = {"type": "assistant", "input": {"password": "${DB_PASSWORD}", "user": "app"}}
    assert "${DB_PASSWORD}" in stored(json.dumps(line))


def test_a_refusal_input_and_a_non_json_line_with_a_quoted_name_are_masked() -> None:
    denial = {"tool_name": "mcp__db", "tool_input": {"password": "s3cr3tPW"}}
    parsed = parse_line(json.dumps({"type": "result", "permission_denials": [denial]}))
    assert isinstance(parsed, Result) and "s3cr3tPW" not in repr(parsed)
    assert "hunter2pass" not in stored('not json {"password": "hunter2pass"}')


def test_masking_stays_linear_on_long_word_runs() -> None:
    from relaybay.domain.environments import redact

    for text in ("monkey_" * 11_500, base64.urlsafe_b64encode(os.urandom(3_000_000)).decode()):
        started = time.perf_counter()
        redact(text)
        took = time.perf_counter() - started
        # Quadratic before: 11.9 s for the first, 14.5 s for 4 MB; about 0.6 s now for 4 MB on
        # a desktop machine, with room for a slow CI runner.
        assert took < (1.0 if len(text) < 100_000 else 3.0), (len(text), took)


def test_the_result_text_and_refusal_inputs_are_masked() -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "result": f"verified · M · done\nused {GITHUB_TOKEN}",
        "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": f"curl -H 'token {GITHUB_TOKEN}'"}},
            {"tool_name": "Write", "tool_input": {"content": "x" * 1000}},
        ],
    }
    parsed = parse_line(json.dumps(result))
    assert isinstance(parsed, Result)
    assert GITHUB_TOKEN not in repr(parsed)
    assert parsed.text.startswith("verified · M · done")
    assert [r.tool for r in parsed.refusals] == ["Bash", "Write"]
    assert len(parsed.refusals[1].input) <= 300


def test_a_line_over_the_limit_keeps_only_its_masked_head() -> None:
    head = '{"type": "user", "x": "' + "a " * 100 + GITHUB_TOKEN + " " + "b" * 50
    cut_token = '{"type": "assistant", "x": "' + "c" * (HEAD_MAX - 40) + " " + GITHUB_TOKEN
    event = truncated_event(head, LINE_MAX + 10, 3)
    data = unpack(event.data)
    assert event.truncated and event.type == "user" and event.size == LINE_MAX + 10
    assert data["note"] == "[relaybay: line truncated]"
    assert GITHUB_TOKEN not in data["head"] and "ghp_***" in data["head"]
    # The token is cut by the head's limit: what is left of it is dropped, not kept in half.
    half = unpack(truncated_event(cut_token, LINE_MAX + 1, 4).data)["head"]
    assert "ghp_" not in half and half.endswith("c" * 10)


async def test_lines_reach_the_sink_in_batches_and_before_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 1000
    child = (
        "import json, sys\n"
        f"for n in range({count}):\n"
        "    sys.stdout.write(json.dumps({'type': 'assistant', 'n': n}) + '\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'ok'}), flush=True)\n"
    )
    sink = Sink()
    runs = manager(sink, monkeypatch, child)
    await runs.start_turn(1, 1, spec(cwd=str(tmp_path)))
    await poll_async(lambda: sink.done)
    seqs = [e.seq for _, batch in sink.batches for e in batch]
    assert seqs == list(range(1, count + 2))
    assert len(sink.batches) <= count // EVENT_BATCH + 3  # writes per batch, not per line
    assert sink.ended_at is not None and all(at <= sink.ended_at for at, _ in sink.batches)
    assert sink.done == [("success", None, False)] and sink.result.text == "ok"


async def test_a_slow_turn_flushes_its_lines_by_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = (
        "import json, time\n"
        "print(json.dumps({'type': 'assistant'}), flush=True)\n"
        "time.sleep(2.5)\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)\n"
    )
    sink = Sink()
    runs = manager(sink, monkeypatch, child)
    started = time.monotonic()
    await runs.start_turn(1, 1, spec(cwd=str(tmp_path)))
    await poll_async(lambda: sink.done)
    first_at, first = sink.batches[0]
    assert [e.seq for e in first] == [1]
    assert first_at - started < 2.0 < sink.ended_at - started  # stored while the turn ran


async def poll_async(check, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.05)
