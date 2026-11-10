"""Probes of a step-by-step headless runner against the real `claude`: "Probes 2026-11" in
docs/design/0012-runner-and-embedded-terminal.md. Opt-in, never in CI (it refuses when `CI` is
set): it uses the signed-in user's login with the haiku model, a temp relaybay server and a temp git
repo, and never touches a running board.

uv run python scripts/probe_runner.py <workdir>: prints a JSON summary, writes <workdir>/out/*.jsonl

The script runs probes 1 to 6. Probes 8 to 12 were run by hand with a variant of it:
  8   the same hook command in the project's settings and in `--settings <file>`: count the posts
      of one turn; compare slash commands and agents in `system/init` with the default
      `--setting-sources` and with `project`; `--settings '{"disableAllHooks": true}'` keeps a probe
      off a running board.
  9   a project folder (not a git repository) holding two repositories, each with its own
      `CLAUDE.md`: ask for all three code words, without and with `--add-dir <repo>` per repository
      and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`.
  10  `--permission-prompt-tool mcp__<server>__<tool>` on a small MCP server that answers allow for
      one file and deny for another; the answer is a `CallToolResult` with one `TextContent` holding
      the JSON, `structured_output=False`.
  11  `claude auth status` with an empty `CLAUDE_CONFIG_DIR`, and with a fake `ANTHROPIC_API_KEY`.
  12  a file in the repository claiming the owner approved a push; `auto` mode; check the remote.
"""

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

if os.environ.get("CI"):
    sys.stderr.write("probe_runner.py runs the real claude under a login: never in CI\n")
    sys.exit(2)

W = Path(sys.argv[1]).resolve()
OUT = W / "out"
REPO = W / "repo"
REMOTE = W / "remote.git"
HOME = W / "home"
ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
CLAUDE = shutil.which("claude")
MODEL = "haiku"
summary: dict = {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(cmd, cwd=None, timeout=60):
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def git(*args, cwd=REPO):
    return run(["git", *args], cwd=cwd).stdout.strip()


for d in (OUT, REPO, HOME):
    d.mkdir(parents=True, exist_ok=True)
run(["git", "init", "-q", "--bare", str(REMOTE)])
git("init", "-q", "-b", "main")
git("config", "user.email", "probe@example.invalid")
git("config", "user.name", "probe")
(REPO / "CLAUDE.md").write_text("The code word of this repo is PINEAPPLE-42.\n", encoding="utf-8")
(REPO / "README.md").write_text("probe repo\n", encoding="utf-8")
git("add", "-A")
git("commit", "-q", "-m", "first")
git("remote", "add", "origin", str(REMOTE))
git("push", "-q", "origin", "main")
(REPO / "change.txt").write_text("a change that is not pushed yet\n", encoding="utf-8")
git("add", "-A")
git("commit", "-q", "-m", "second, not pushed")

port = free_port()
server_log = (W / "server.log").open("w", encoding="utf-8")
server = subprocess.Popen(
    [str(PY), "-I", "-m", "relaybay", "serve", "--home", str(HOME), "--port", str(port)],
    cwd=ROOT,
    stdout=server_log,
    stderr=subprocess.STDOUT,
)
base = f"http://127.0.0.1:{port}"
for _ in range(60):
    try:
        urlopen(f"{base}/healthz", timeout=1).read()
        break
    except OSError:
        time.sleep(0.5)
token = (HOME / "token").read_text(encoding="utf-8").strip()
token_file = HOME / "token"
hook_url = f"{base}/hooks/claude-code"
hook = f'"{PY}" -I -m relaybay.hook_client --url {hook_url} --token-file "{token_file}"'
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"]
settings = {"hooks": {e: [{"hooks": [{"type": "command", "command": hook}]}] for e in events}}
(W / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
(W / "mcp.json").write_text(
    json.dumps(
        {
            "mcpServers": {
                "relaybay": {
                    "type": "http",
                    "url": f"{base}/mcp",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }
    ),
    encoding="utf-8",
)


def hook_posts() -> int:
    return len(
        re.findall(r'"POST /hooks/claude-code HTTP', (W / "server.log").read_text(encoding="utf-8"))
    )


def turn(name, prompt, *, resume=None, mode=None, extra=(), allowed=None, timeout=240):
    cmd = [
        CLAUDE,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        MODEL,
        "--setting-sources",
        "project",
        "--settings",
        str(W / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(W / "mcp.json"),
        "--max-budget-usd",
        "0.5",
    ]
    if resume:
        cmd += ["--resume", resume]
    if mode:
        cmd += ["--permission-mode", mode]
    if allowed:
        cmd += ["--allowed-tools", allowed]
    cmd += list(extra)
    before = hook_posts()
    began = time.time()
    proc = run(cmd, cwd=REPO, timeout=timeout)
    (OUT / f"{name}.jsonl").write_text(proc.stdout, encoding="utf-8")
    (OUT / f"{name}.err").write_text(proc.stderr, encoding="utf-8")
    lines = []
    for line in proc.stdout.splitlines():
        with contextlib.suppress(ValueError):
            lines.append(json.loads(line))
    init = next((m for m in lines if m.get("type") == "system" and m.get("subtype") == "init"), {})
    result = next((m for m in reversed(lines) if m.get("type") == "result"), {})
    texts = [
        b.get("text", "")
        for m in lines
        if m.get("type") == "assistant"
        for b in (m.get("message", {}).get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    tools = [
        b.get("name")
        for m in lines
        if m.get("type") == "assistant"
        for b in (m.get("message", {}).get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    info = {
        "rc": proc.returncode,
        "secs": round(time.time() - began, 1),
        "session_id": init.get("session_id") or result.get("session_id"),
        "permissionMode": init.get("permissionMode"),
        "mcp": {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])},
        "skills_or_commands": len(init.get("slash_commands", []) or []),
        "tools_called": tools,
        "answer": (texts[-1] if texts else "")[:300],
        "result_subtype": result.get("subtype"),
        "is_error": result.get("is_error"),
        "denials": [
            {"tool": d.get("tool_name"), "input": str(d.get("tool_input"))[:120]}
            for d in (result.get("permission_denials") or [])
        ],
        "usage": result.get("usage"),
        "cost_usd": result.get("total_cost_usd"),
        "hook_posts_this_turn": hook_posts() - before,
        "stderr": proc.stderr[:300],
        "event_types": sorted({f"{m.get('type')}/{m.get('subtype')}" for m in lines}),
    }
    summary[name] = info
    return info


try:
    # 1: first turn, then a resumed turn.
    a = turn(
        "p1_turn1",
        "What is the code word written in CLAUDE.md? Then call the relaybay tool project_list. "
        "Answer in one short line.",
        allowed="mcp__relaybay__project_list",
    )
    sid = a["session_id"]
    turn("p1_turn2", "What was the first thing I asked you? One short line.", resume=sid)
    # 2/3: permissions. Plain default mode with a command that needs approval.
    b = turn(
        "p2_default", "Run the shell command `touch made-by-probe.txt` and tell me if it worked."
    )
    summary["p2_default"]["file_made"] = (REPO / "made-by-probe.txt").exists()
    # 3: owner's word as a new turn in auto mode, in the same session as an earlier refusal to push.
    c = turn(
        "p3_turn1",
        "The owner has not approved pushing yet. Do not run git push. Reply 'waiting' in one word.",
        mode="auto",
    )
    sid3 = c["session_id"]
    pushed_before = git("ls-remote", "origin", "main", cwd=REPO)
    turn(
        "p3_turn2",
        "Approval from the owner, given on the board: push is approved. Push the current branch to "
        "origin now with git push origin main and tell me the result in one line.",
        resume=sid3,
        mode="auto",
    )
    summary["p3_turn2"]["remote_before"] = pushed_before[:12]
    summary["p3_turn2"]["remote_after"] = git("ls-remote", "origin", "main", cwd=REPO)[:12]
    summary["p3_turn2"]["local_head"] = git("rev-parse", "HEAD")[:12]
    # 4: two headless turns at once on the same session.
    first = subprocess.Popen(
        [
            CLAUDE,
            "-p",
            "Run `ping -n 15 127.0.0.1` in the shell and tell me when done.",
            "--resume",
            sid,
            "--model",
            MODEL,
            "--permission-mode",
            "auto",
            "--output-format",
            "json",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--mcp-config",
            str(W / "mcp.json"),
        ],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    time.sleep(8)
    second = run(
        [
            CLAUDE,
            "-p",
            "Say 'second' in one word.",
            "--resume",
            sid,
            "--model",
            MODEL,
            "--output-format",
            "json",
            "--setting-sources",
            "project",
            "--strict-mcp-config",
            "--mcp-config",
            str(W / "mcp.json"),
        ],
        cwd=REPO,
        timeout=180,
    )
    out1, err1 = first.communicate(timeout=240)
    summary["p4_concurrent"] = {
        "first_rc": first.returncode,
        "first_out": out1[:300],
        "first_err": err1[:200],
        "second_rc": second.returncode,
        "second_out": second.stdout[:300],
        "second_err": second.stderr[:300],
    }
    # 6: numbers: result usage against the transcript of the session.
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
    transcript = Path.home() / ".claude/projects" / enc / f"{sid}.jsonl"
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    seen = set()
    if transcript.exists():
        for line in transcript.read_text(encoding="utf-8").splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            msg = m.get("message") or {}
            u = msg.get("usage")
            if m.get("type") == "assistant" and u and msg.get("id") not in seen:
                seen.add(msg.get("id"))
                totals["input"] += u.get("input_tokens", 0)
                totals["output"] += u.get("output_tokens", 0)
                totals["cache_read"] += u.get("cache_read_input_tokens", 0)
                totals["cache_write"] += u.get("cache_creation_input_tokens", 0)
    summary["p6_transcript_totals_session1"] = {"path_exists": transcript.exists(), **totals}
    summary["p6_result_usage_sum_turns"] = {
        k: sum((summary[n].get("usage") or {}).get(f, 0) for n in ("p1_turn1", "p1_turn2"))
        for k, f in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_input_tokens"),
            ("cache_write", "cache_creation_input_tokens"),
        )
    }
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()
    server_log.close()
    (W / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=1, default=str)[:9000])
