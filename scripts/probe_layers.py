"""Probes 8 and 9 of the runner, second round (0.9): the user layer in a resumed turn, hooks from
two sources on a resumed turn, and worktrees outside the cwd through `--add-dir`. Opt-in, never in
CI: it uses the signed-in user's login with the haiku model and temp git repos; hooks write to a
local log file only and the user's MCP servers stay off, so no running board is reached.

uv run python scripts/probe_layers.py <workdir>: prints a JSON summary, writes <workdir>/out/*.jsonl
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if os.environ.get("CI"):
    sys.stderr.write("probe_layers.py runs the real claude under a login: never in CI\n")
    sys.exit(2)

W = Path(sys.argv[1]).resolve()
USER_PROBE = sys.argv[2] if len(sys.argv) > 2 else ""
OUT = W / "out"
CLAUDE = shutil.which("claude")
PY = Path(sys.executable)
MODEL = "haiku"
NO_MCP = json.dumps({"mcpServers": {}})
NO_HOOKS = json.dumps({"disableAllHooks": True})
summary: dict = {}


def run(cmd, cwd=None, timeout=60, env=None):
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def git(*args, cwd):
    p = run(["git", "-c", "core.excludesFile=", *args], cwd=cwd)
    if p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


def make_repo(path: Path, word: str) -> None:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "probe@example.invalid", cwd=path)
    git("config", "user.name", "probe", cwd=path)
    (path / "CLAUDE.md").write_text(f"The code word of this repo is {word}.\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "first", cwd=path)


def turn(name, prompt, *, cwd, resume=None, extra=(), env_add=None, sources=None, timeout=240):
    cmd = [CLAUDE, "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", MODEL]
    cmd += ["--strict-mcp-config", "--mcp-config", NO_MCP, "--max-budget-usd", "0.5"]
    if sources:
        cmd += ["--setting-sources", sources]
    if resume:
        cmd += ["--resume", resume]
    cmd += list(extra)
    env = dict(os.environ)
    env.update(env_add or {})
    began = time.time()
    proc = run(cmd, cwd=cwd, timeout=timeout, env=env)
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
        (b.get("name"), str(b.get("input"))[:160])
        for m in lines
        if m.get("type") == "assistant"
        for b in (m.get("message", {}).get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    info = {
        "rc": proc.returncode,
        "secs": round(time.time() - began, 1),
        "session_id": init.get("session_id") or result.get("session_id"),
        "cwd": init.get("cwd"),
        "commands": len(init.get("slash_commands", []) or []),
        "agents": len(init.get("agents", []) or []),
        "mcp": {s.get("name"): s.get("status") for s in init.get("mcp_servers", [])},
        "tools_called": tools,
        "answer": "\n".join(texts)[-600:],
        "is_error": result.get("is_error"),
        "denials": [d.get("tool_name") for d in (result.get("permission_denials") or [])],
        "cost_usd": result.get("total_cost_usd"),
        "stderr": proc.stderr[:300],
    }
    summary[name] = info
    return info


OUT.mkdir(parents=True, exist_ok=True)

# ---- probe 8: hooks from two sources on first and resumed turns; RELAYBAY_RUN_ID reaches them.
P8 = W / "p8repo"
make_repo(P8, "MANGO-8")
hook_log = W / "hooks.log"
logger = W / "hook_logger.py"
logger.write_text(
    "import json, os, sys\n"
    "d = json.load(sys.stdin)\n"
    f"with open(r'{hook_log}', 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps({'event': d.get('hook_event_name'), 'session': d.get('session_id'),"
    " 'run': os.environ.get('RELAYBAY_RUN_ID')}) + '\\n')\n",
    encoding="utf-8",
)
hook_cmd = f'"{PY}" -I "{logger}"'
events = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
hooks = {"hooks": {e: [{"hooks": [{"type": "command", "command": hook_cmd}]}] for e in events}}
(P8 / ".claude").mkdir()
(P8 / ".claude" / "settings.json").write_text(json.dumps(hooks), encoding="utf-8")
(W / "p8settings.json").write_text(json.dumps(hooks), encoding="utf-8")


def hook_lines():
    if not hook_log.exists():
        return []
    return [json.loads(x) for x in hook_log.read_text(encoding="utf-8").splitlines() if x]


def count_turn(name, *args, **kw):
    before = len(hook_lines())
    info = turn(name, *args, **kw)
    new = hook_lines()[before:]
    counts: dict = {}
    for h in new:
        counts[h["event"]] = counts.get(h["event"], 0) + 1
    info["hook_events"] = counts
    info["hook_run_ids"] = sorted({str(h["run"]) for h in new})
    info["hook_sessions"] = sorted({str(h["session"]) for h in new})
    return info


ask_tool = "Read the file CLAUDE.md with the Read tool, then answer its code word in one word."
one = count_turn(
    "p8_one_source_t1", ask_tool, cwd=P8, sources="project", env_add={"RELAYBAY_RUN_ID": "run-1"}
)
count_turn(
    "p8_one_source_t2",
    "Read CLAUDE.md again with the Read tool and say the code word.",
    cwd=P8,
    sources="project",
    resume=one["session_id"],
    env_add={"RELAYBAY_RUN_ID": "run-1"},
)
two = count_turn(
    "p8_two_sources_t1",
    ask_tool,
    cwd=P8,
    sources="project",
    extra=["--settings", str(W / "p8settings.json")],
    env_add={"RELAYBAY_RUN_ID": "run-2"},
)
count_turn(
    "p8_two_sources_t2",
    "Read CLAUDE.md again with the Read tool and say the code word.",
    cwd=P8,
    sources="project",
    resume=two["session_id"],
    extra=["--settings", str(W / "p8settings.json")],
    env_add={"RELAYBAY_RUN_ID": "run-2"},
)

# ---- probe 8: the user layer (default setting sources) in a resumed turn; all hooks off.
u1 = turn(
    "p8_user_t1", "Reply with the single word: ready.", cwd=P8, extra=["--settings", NO_HOOKS]
)
turn(
    "p8_user_t2",
    "Look only at the instructions you were given (user-level CLAUDE.md), not at this chat. "
    + (USER_PROBE or "Quote the first heading of the user-level instructions."),
    cwd=P8,
    resume=u1["session_id"],
    extra=["--settings", NO_HOOKS],
)
turn(
    "p8_user_t2_project_only",
    "Look only at the instructions you were given (user-level CLAUDE.md), not at this chat. "
    + (USER_PROBE or "Quote the first heading of the user-level instructions."),
    cwd=P8,
    sources="project",
    resume=u1["session_id"],
    extra=["--settings", NO_HOOKS],
)

# ---- probe 9: a project folder with two repositories, worktrees outside the cwd, --add-dir.
PROJ = W / "proj"
PROJ.mkdir()
(PROJ / "CLAUDE.md").write_text("The code word of the project folder is OAK-9.\n", encoding="utf-8")
make_repo(PROJ / "backend", "BACKEND-REPO")
make_repo(PROJ / "frontend", "FRONTEND-REPO")
WT = W / "worktrees"
wts = {}
for name in ("backend", "frontend"):
    path = WT / f"{name}-run1"
    git("worktree", "add", "-q", "-b", "run1", str(path), cwd=PROJ / name)
    # The worktree's own CLAUDE.md differs from the repository's, so the source is visible.
    (path / "CLAUDE.md").write_text(
        f"The code word of this worktree is {name.upper()}-WORKTREE.\n", encoding="utf-8"
    )
    wts[name] = path
add = []
for p in wts.values():
    add += ["--add-dir", str(p)]
env9 = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}
ask_words = (
    "Without using any tools, list every code word that appears in the CLAUDE.md instructions "
    "loaded into your context, one per line, nothing else."
)
base9 = ["--settings", NO_HOOKS]
a = turn("p9_no_add_dir", ask_words, cwd=PROJ, extra=base9)
turn(
    "p9_resume_with_add_dir",
    ask_words,
    cwd=PROJ,
    resume=a["session_id"],
    extra=base9 + add,
    env_add=env9,
)
b = turn("p9_first_with_add_dir", ask_words, cwd=PROJ, extra=base9 + add, env_add=env9)
turn(
    "p9_write_both",
    "Create a file named probe9.txt containing the word done in each of these two folders, "
    f"using the Write tool: {wts['backend']} and {wts['frontend']}. Then say ok.",
    cwd=PROJ,
    resume=b["session_id"],
    extra=base9 + add + ["--permission-mode", "acceptEdits"],
    env_add=env9,
)
summary["p9_files"] = {
    n: {"worktree": (p / "probe9.txt").exists(), "repo": (PROJ / n / "probe9.txt").exists()}
    for n, p in wts.items()
}
summary["p9_worktree_status"] = {n: git("status", "--short", cwd=p) for n, p in wts.items()}

(W / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
print(json.dumps(summary, indent=1, default=str, ensure_ascii=False)[:14000])
