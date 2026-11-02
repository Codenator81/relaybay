"""`relaybay hook`: forward one Claude Code hook event (JSON on stdin) to relaybay.

Used as a `command` hook where `http` hooks are not available. Standard library only, so it
starts fast. It must never get in the way of the session: short timeouts, no output on failure
and always exit code 0. The server's JSON answer (the SessionStart context) goes to stdout.
"""

import argparse
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

TIMEOUT_SECONDS = 0.3


def forward(url: str, token_file: Path, body: bytes, timeout: float = TIMEOUT_SECONDS) -> bytes:
    token = token_file.read_text(encoding="utf-8").strip()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    opener = build_opener(ProxyHandler({}))  # localhost: never through a system proxy
    with opener.open(request, timeout=timeout) as response:
        return response.read() if response.status == 200 else b""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relaybay hook", description=__doc__)
    parser.add_argument("--url", required=True, help="relaybay hook URL (…/hooks/claude-code).")
    parser.add_argument("--token-file", type=Path, required=True, help="relaybay token file.")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0  # a broken hook entry must not show errors in the session either
    try:
        answer = forward(args.url, args.token_file, sys.stdin.buffer.read())
        if answer:
            sys.stdout.buffer.write(answer)
            sys.stdout.flush()
    except Exception:
        # relaybay down, slow or misconfigured: drop the event silently, the session goes on.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
