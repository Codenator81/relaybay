"""Probes of 0.11 (design 0023): what the coordinator's permanent run and the schedules rely on.
Opt-in, never in CI: it uses the signed-in user's login with the haiku model in temp folders;
hooks are off and the user's MCP servers stay off, so no running board is reached.

  A. CLAUDE.md of a folder nested in a git repository: does the repository's own file load too,
     and does `claudeMdExcludes` keep it out?
  B. Profiles: `--disallowedTools` in auto mode, and a read-only `dontAsk` profile.
  C. A long-lived session: `/compact` as a headless turn with --resume; the session id after it.
  D. The cost of a small wake turn on a resumed session (result.usage).
  E. A one-shot check: `--no-session-persistence` and `--max-turns`.

uv run python scripts/probe_coordinator.py <workdir>: prints a JSON summary, writes <workdir>/out/
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if os.environ.get("CI"):
    sys.stderr.write("probe_coordinator.py runs the real claude under a login: never in CI\n")
    sys.exit(2)

W = Path(sys.argv[1]).resolve()
OUT = W / "out"
CLAUDE = shutil.which("claude")
MODEL = "haiku"
NO_MCP = json.dumps({"mcpServers": {}})
summary: dict = {}


def run(cmd, cwd=None, timeout=180):
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def git(*args, cwd):
    p = run(["git", "-c", "core.excludesFile=", *args], cwd=cwd)
    if p.returncode:
        raise RuntimeError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


def turn(name, prompt, cwd, *, resume=None, mode="auto", extra=(), settings=None):
    s = {"disableAllHooks": True, **(settings or {})}
    cmd = [
        CLAUDE,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        MODEL,
        "--permission-mode",
        mode,
        "--settings",
        json.dumps(s),
        "--mcp-config",
        NO_MCP,
        "--strict-mcp-config",
        *extra,
    ]
    if resume:
        cmd += ["--resume", resume]
    p = run(cmd, cwd=cwd)
    (OUT / f"{name}.jsonl").write_text(p.stdout + "\n#stderr\n" + p.stderr, encoding="utf-8")
    events = []
    for line in p.stdout.splitlines():
        with contextlib.suppress(ValueError):
            events.append(json.loads(line))
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    tools = [
        c.get("name")
        for e in events
        if e.get("type") == "assistant"
        for c in e.get("message", {}).get("content", [])
        if c.get("type") == "tool_use"
    ]
    return {
        "exit": p.returncode,
        "session": init.get("session_id") or result.get("session_id"),
        "subtype": result.get("subtype"),
        "text": (result.get("result") or "")[:400],
        "usage": result.get("usage"),
        "denials": [d.get("tool_name") for d in result.get("permission_denials") or []],
        "tools": tools,
        "init_tools_has_write": "Write" in (init.get("tools") or []),
        "stderr": p.stderr[-300:],
    }


def probe_a():
    repo = W / "a" / "repo"
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "CLAUDE.md").write_text("The code word of the parent repo is PARENT-OWL.\n", "utf-8")
    nested = repo / ".dev-data" / "coordinator"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("The code word of this folder is COORD-FOX.\n", "utf-8")
    alone = W / "a" / "alone" / "coordinator"
    alone.mkdir(parents=True)
    (alone / "CLAUDE.md").write_text("The code word of this folder is COORD-FOX.\n", "utf-8")
    ask = (
        "Which code words are written in the CLAUDE.md files of your instructions?"
        " Answer only the words."
    )
    summary["A_nested"] = turn("a_nested", ask, nested)
    summary["A_alone"] = turn("a_alone", ask, alone)
    excl = {"claudeMdExcludes": [str(repo / "CLAUDE.md").replace("\\", "/")]}
    summary["A_nested_excluded"] = turn("a_excl", ask, nested, settings=excl)


def probe_b():
    d = W / "b"
    d.mkdir()
    ask = "Create a file named made.txt containing hi in the current folder, using any tool."
    r = turn(
        "b_disallowed",
        ask,
        d,
        mode="auto",
        extra=["--disallowedTools", "Write,Edit,Bash,NotebookEdit"],
    )
    r["file_made"] = (d / "made.txt").exists()
    summary["B_auto_disallowed"] = r
    r = turn("b_readonly", ask, d, mode="dontAsk", extra=["--allowedTools", "Read,Glob,Grep"])
    r["file_made"] = (d / "made.txt").exists()
    summary["B_dontAsk_readonly"] = r


def probe_cd():
    d = W / "c"
    d.mkdir()
    t1 = turn(
        "c1", "Remember: the project of today is acme-shop and its blocker is the CI. Reply ok.", d
    )
    sid = t1["session"]
    t2 = turn(
        "c2",
        "Event: a report was added on acme-shop#12. Reply with one word: noted.",
        d,
        resume=sid,
    )
    t3 = turn("c3", "/compact", d, resume=sid)
    t4 = turn("c4", "What is the project of today and its blocker? One line.", d, resume=sid)
    summary["C"] = {
        "t1": t1,
        "t2_wake": t2,
        "t3_compact": t3,
        "t4_after": t4,
        "same_session": len({t1["session"], t2["session"], t3["session"], t4["session"]}) == 1,
    }


def probe_e():
    d = W / "e"
    d.mkdir()
    r = turn("e1", "Reply with the word done.", d, extra=["--no-session-persistence"])
    sid = r["session"]
    again = turn("e1b", "What did I ask before?", d, resume=sid) if sid else {}
    summary["E_no_persist"] = {"first": r, "resume_after": again}
    summary["E_max_turns"] = turn(
        "e2",
        "List the files here with Glob, then read each one with Read, then say done.",
        d,
        extra=["--max-turns", "1"],
    )


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for step in (probe_a, probe_b, probe_cd, probe_e):
        try:
            step()
        except Exception as exc:  # a probe that fails must not hide the others
            summary[step.__name__] = {"error": repr(exc)}
    print(json.dumps(summary, indent=1, ensure_ascii=True))
