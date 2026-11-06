"""Ask the real `claude` what a `-p` session sees: the MCP servers in `system/init`, without and
with `--chrome`. Answers "verify before start" in docs/design/0012-runner-and-embedded-terminal.md.

python scripts/probe_claude.py [--timeout 180] [--cwd FOLDER]

It runs `claude -p "reply ok" --output-format stream-json --verbose` under your own login, so it
spends a little quota, and the hooks of your own settings fire. Never in CI: it refuses when `CI`
is set. It prints names and status only, never tool results or the model's text.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

BASE = ["-p", "reply ok", "--output-format", "stream-json", "--verbose"]
STDERR_LINES = 3


def probe(claude: str, extra: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    """One run: `exit`, `init` (the system/init message or None), `result`, `stderr`."""
    try:
        done = subprocess.run(
            [claude, *BASE, *extra],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"exit": None, "init": None, "result": None, "stderr": f"timed out, {timeout} s"}
    init, result = None, None
    for line in done.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("type") == "system" and message.get("subtype") == "init" and init is None:
            init = message
        elif message.get("type") == "result":
            result = message
    return {"exit": done.returncode, "init": init, "result": result, "stderr": done.stderr}


def servers(run: dict[str, Any]) -> dict[str, str]:
    init = run["init"] or {}
    return {s.get("name", "?"): s.get("status", "?") for s in init.get("mcp_servers", [])}


def report(title: str, run: dict[str, Any]) -> None:
    print(f"--- claude {title}")
    result = run["result"] or {}
    print(f"exit {run['exit']}, result {result.get('subtype', 'none')}")
    if run["init"] is None:
        print("no system/init message")
        for line in run["stderr"].splitlines()[:STDERR_LINES]:
            print(f"stderr: {line}")
        return
    print(f"permission mode: {run['init'].get('permissionMode', '?')}")
    print(f"tools: {len(run['init'].get('tools', []))}")
    found = servers(run)
    print("mcp_servers:" if found else "mcp_servers: none")
    for name, status in found.items():
        print(f"  {name}: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cwd", type=Path, help="Folder to run in; default a new temporary one.")
    args = parser.parse_args()
    if os.environ.get("CI"):
        sys.stderr.write("probe_claude.py runs the real claude under a login: never in CI\n")
        return 2
    claude = shutil.which("claude")
    if claude is None:
        sys.stderr.write("no claude on PATH\n")
        return 2
    print(f"claude: {claude}")
    with tempfile.TemporaryDirectory(prefix="relaybay-probe-") as temporary:
        cwd = args.cwd or Path(temporary)
        plain = probe(claude, [], cwd, args.timeout)
        report(" ".join(BASE[:2]), plain)
        chrome = probe(claude, ["--chrome"], cwd, args.timeout)
        report(" ".join([*BASE[:2], "--chrome"]), chrome)
    print("--- only with --chrome:", ", ".join(sorted(set(servers(chrome)) - set(servers(plain)))))
    return 0 if plain["init"] is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
