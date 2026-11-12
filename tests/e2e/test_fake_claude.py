"""The fake itself, without a server: its flags, its stream and how it obeys hook answers. A stub
hook stands in for relaybay, so answers the server does not give yet are covered too."""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import FAKE, FakeRun, poll, write_shim
from tests.e2e.stream_contract import check_stream

REPO = Path(__file__).parents[2]
# Answers per event from a JSON file; every call is logged as `event tool stop_hook_active`.
STUB_HOOK = """
import json, pathlib, sys
here = pathlib.Path(sys.argv[1])
event = json.load(sys.stdin)
name = event["hook_event_name"]
line = {"event": name, "tool": event.get("tool_name"), "active": event.get("stop_hook_active"),
        "reason": event.get("reason"), "source": event.get("source"),
        "transcript": event.get("transcript_path")}
with (here / "calls.jsonl").open("a") as out:
    out.write(json.dumps(line) + "\\n")
answers = json.loads((here / "answers.json").read_text()).get(name, [])
log = here / "count.json"
counts = json.loads(log.read_text()) if log.exists() else {}
index = counts.get(name, 0)
counts[name] = index + 1
log.write_text(json.dumps(counts))
answer = answers[index] if index < len(answers) else None
if answer:
    print(json.dumps(answer))
"""
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")
BASH = {"tool": "Bash", "input": {"command": "npm test"}, "result": "ok"}


class Local:
    """A Claude home with the stub hook on every event, and the shim on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "home" / ".claude"
        self.config.mkdir(parents=True)
        self.project = root / "project"
        self.project.mkdir()
        stub = root / "stub.py"
        stub.write_text(STUB_HOOK, encoding="utf-8")
        command = f'"{Path(sys.executable).as_posix()}" "{stub.as_posix()}" "{root.as_posix()}"'
        handler = {"type": "command", "command": command, "timeout": 30}
        hooks = {event: [{"matcher": "*", "hooks": [handler]}] for event in EVENTS}
        (self.config / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        write_shim(root / "bin")
        self.env = {
            **os.environ,
            "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_CONFIG_DIR": str(self.config),
            "FAKE_CLAUDE_SCENARIO": str(root / "scenario.json"),
            "FAKE_CLAUDE_TRACE": str(root / "trace.jsonl"),
        }

    def start(
        self,
        scenario: dict[str, Any],
        answers: dict[str, list[Any]] | None = None,
        flags: tuple[str, ...] = (),
        direct: bool = False,
        **popen: Any,
    ) -> FakeRun:
        (self.root / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
        (self.root / "answers.json").write_text(json.dumps(answers or {}), encoding="utf-8")
        program = [sys.executable, str(FAKE)] if direct else [self.claude]
        process = subprocess.Popen(
            [*program, "-p", "go", "--output-format", "stream-json", "--verbose", *flags],
            cwd=self.project,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            **popen,
        )
        return FakeRun(process, self.root / "trace.jsonl")

    @property
    def claude(self) -> str:
        found = shutil.which("claude", path=self.env["PATH"])
        assert found is not None
        return found

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in self.env.items() if k != "FAKE_CLAUDE_SCENARIO"}
        return subprocess.run(
            [self.claude, *args], env=env, capture_output=True, text=True, timeout=30, check=False
        )

    def calls(self) -> list[dict[str, Any]]:
        path = self.root / "calls.jsonl"
        return [json.loads(x) for x in path.read_text("utf-8").splitlines()]


@pytest.fixture
def local(tmp_path: Path) -> Local:
    return Local(tmp_path)


def stream_flags() -> tuple[str, ...]:
    return ("--allowedTools", "Bash")


def test_flags_the_real_cli_is_called_with(local: Local) -> None:
    assert local.cli("--version").stdout.strip().endswith("(Claude Code)")
    bare = local.cli("--bare", "-p", "x", "--output-format", "stream-json", "--verbose")
    assert bare.returncode != 0 and "--bare" in bare.stderr
    no_verbose = local.cli("-p", "x", "--output-format", "stream-json")
    assert no_verbose.returncode != 0 and "requires --verbose" in no_verbose.stderr
    unknown = local.cli("-p", "x", "--no-such-flag")
    assert unknown.returncode != 0 and "--no-such-flag" in unknown.stderr
    assert local.cli("-p", "x").returncode == 2  # no scenario: it never falls back to anything


def test_it_needs_a_config_dir_of_its_own(local: Local) -> None:
    local.env.pop("CLAUDE_CONFIG_DIR")
    run = local.start({})
    assert run.wait() == 2 and "CLAUDE_CONFIG_DIR" in run.stderr
    assert not run.messages


def test_stream_transcript_and_hook_order(local: Local) -> None:
    scenario = {
        "model": "demo-model-small",
        "tools": ["Bash"],
        "configured_servers": [{"name": "claude.ai Demo", "status": "needs-auth"}],
        "turn": [
            {"text": "Looking."},
            {
                "tool": "Agent",
                "input": {"prompt": "Run the tests"},
                "result": "passed",
                "steps": [BASH],
            },
            {"text": "Done."},
        ],
    }
    run = local.start(scenario, flags=stream_flags()).finish()
    check_stream(run.messages)
    assert run.init["model"] == "demo-model-small"
    assert run.init["mcp_servers"] == [{"name": "claude.ai Demo", "status": "needs-auth"}]
    assert run.result["subtype"] == "success" and run.result["result"] == "Done."
    # Subagent messages carry the id of the Agent call; the main conversation carries null.
    calls = [
        (m["parent_tool_use_id"], b["name"], b["id"])
        for m in run.of("assistant")
        for b in m["message"]["content"]
        if b["type"] == "tool_use"
    ]
    assert calls[0][:2] == (None, "Agent") and calls[1][:2] == (calls[0][2], "Bash")

    seen = [(c["event"], c["tool"]) for c in local.calls()]
    assert seen == [
        ("SessionStart", None),
        ("UserPromptSubmit", None),
        ("PreToolUse", "Agent"),
        ("PreToolUse", "Bash"),
        ("PostToolUse", "Bash"),
        ("PostToolUse", "Agent"),
        ("Stop", None),
        ("SessionEnd", None),
    ]
    # Shaped like tests/fixtures/transcript.jsonl: the reader counts each message's usage.
    transcript = Path(local.calls()[0]["transcript"])
    lines = [json.loads(x) for x in transcript.read_text("utf-8").splitlines()]
    assistant = [x for x in lines if x["type"] == "assistant"]
    assert len(assistant) == 4 and all("timestamp" in x for x in lines)
    assert assistant[0]["message"]["usage"]["input_tokens"] > 0
    assert assistant[0]["message"]["model"] == "demo-model-small"


def test_deny_context_and_ask_from_the_hook(local: Local) -> None:
    answers = {
        "SessionStart": [{"hookSpecificOutput": {"additionalContext": "start line"}}],
        "PreToolUse": [
            {
                "hookSpecificOutput": {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "no",
                }
            },
            {"hookSpecificOutput": {"permissionDecision": "ask", "permissionDecisionReason": "?"}},
            {"hookSpecificOutput": {"additionalContext": "tool line"}},
        ],
    }
    run = local.start({"turn": [BASH, BASH, BASH]}, answers, stream_flags()).finish()
    check_stream(run.messages)
    denied = run.of("system", "permission_denied")
    assert [d["decision_reason"] for d in denied] == ["no", "?"]
    assert len(run.result["permission_denials"]) == 2
    errors = [
        b["is_error"] for m in run.of("user") for b in m["message"]["content"] if "is_error" in b
    ]
    assert errors == [True, True, False]
    assert [c["event"] for c in local.calls()].count("PostToolUse") == 1
    assert [e["text"] for e in run.trace("context")] == ["start line", "tool line"]


def test_a_tool_without_approval_is_denied_under_print_mode(local: Local) -> None:
    run = local.start({"turn": [BASH, {"tool": "Read", "input": {}, "result": "x"}]}).finish()
    denied = run.of("system", "permission_denied")
    assert [(d["tool_name"], d["decision_reason_type"]) for d in denied] == [("Bash", "mode")]
    allowed = local.start({"turn": [BASH]}, flags=("--allowedTools", "Bash(npm *)")).finish()
    assert not allowed.of("system", "permission_denied")
    other = local.start({"turn": [BASH]}, flags=("--allowedTools", "Bash(git *)")).finish()
    assert other.of("system", "permission_denied")
    bypass = local.start({"turn": [BASH]}, flags=("--permission-mode", "bypassPermissions"))
    assert not bypass.finish().of("system", "permission_denied")


def test_stop_block_continues_the_turn_once_then_ends(local: Local) -> None:
    scenario = {"turn": [{"text": "Done."}], "on_stop_block": [{"text": "Handled it."}]}
    answers = {"Stop": [{"decision": "block", "reason": "not yet"}]}
    run = local.start(scenario, answers).finish()
    assert [e["reason"] for e in run.trace("stop_block")] == ["not yet"]
    stops = [c["active"] for c in local.calls() if c["event"] == "Stop"]
    assert stops == [False, True]
    assert run.result["result"] == "Handled it."


def test_continue_false_stops_the_session_at_the_tool_call(local: Local) -> None:
    answers = {
        "PreToolUse": [
            None,
            {
                "continue": False,
                "stopReason": "stop all",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "stop all",
                },
            },
        ]
    }
    scenario = {"turn": [BASH, BASH, {"text": "Never said."}, BASH]}
    run = local.start(scenario, answers, stream_flags()).finish()
    check_stream(run.messages)
    assert [e["reason"] for e in run.trace("stopped")] == ["stop all"]
    assert [c["event"] for c in local.calls()].count("PreToolUse") == 2
    assert "Stop" not in [c["event"] for c in local.calls()]  # no more processing, nor a Stop hook
    assert "SessionEnd" in [c["event"] for c in local.calls()]
    assert run.result["result"] != "Never said."


def test_a_broken_hook_is_ignored(local: Local) -> None:
    (local.config / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "no-such-program --x"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    run = local.start({"turn": [BASH]}, flags=stream_flags()).finish()
    assert not run.of("system", "permission_denied")


def test_termination_runs_session_end_and_exits_143(local: Local) -> None:
    never = local.root / "never"
    scenario = {"turn": [{"text": "Working."}, {"wait_file": str(never), "timeout": 120}]}
    if sys.platform == "win32":
        # Windows has no SIGTERM to send; the new group limits the console break to this process.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        run = local.start(scenario, direct=True, creationflags=flags)
        stop = signal.CTRL_BREAK_EVENT
    else:
        run = local.start(scenario)
        stop = signal.SIGTERM
    poll(lambda: run.of("assistant"), what="the first message")
    run.process.send_signal(stop)
    assert run.wait() == 143
    assert not run.of("result")  # the turn stays unfinished
    assert [c["event"] for c in local.calls()][-1] == "SessionEnd"
    assert local.calls()[-1]["reason"] == "other"


def test_probe_reads_the_servers_of_both_runs(local: Local) -> None:
    """scripts/probe_claude.py against the fake; only the session runs it against the real one."""
    scenario: dict[str, Any] = {
        "configured_servers": [{"name": "claude.ai Demo", "status": "connected"}],
        "turn": [{"text": "ok"}],
    }
    (local.root / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    (local.root / "answers.json").write_text("{}", encoding="utf-8")
    env = {k: v for k, v in local.env.items() if k != "CI"}
    done = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "probe_claude.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "claude.ai Demo: connected" in done.stdout
    # The fake takes --chrome and connects no browser server for it.
    assert "--- only with --chrome: \n" in done.stdout
    refused = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "probe_claude.py")],
        env={**env, "CI": "true"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert refused.returncode == 2 and "never in CI" in refused.stderr
