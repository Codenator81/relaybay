"""`relaybay hook`: forward one Claude Code hook event (JSON on stdin) to relaybay.

Used as a `command` hook where `http` hooks are not available. Standard library only, so it
starts fast. It must never get in the way of the session: short timeouts, no output on failure
and always exit code 0. The server's JSON answer (session context, owner comments, a Stop block)
goes to stdout.

With `--wait` it is the opt-in wake-up hook (`asyncRewake`, run in the background at Stop): it
waits for an owner comment for the idle session and exits 2 with the comment on stderr, which
wakes the session; it exits 0 silently in every other case.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from relaybay.domain.hooks import WAKE_HOLD_SECONDS

TIMEOUT_SECONDS = 0.3
# The server answers the long poll within WAKE_HOLD_SECONDS; a little longer than that.
WAIT_TIMEOUT_SECONDS = WAKE_HOLD_SECONDS + 60
WAKE_EXIT_CODE = 2


def forward(url: str, token_file: Path, body: bytes, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """The server's answer when it is 200 with a JSON object, else b""."""
    token = token_file.read_text(encoding="utf-8").strip()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    opener = build_opener(ProxyHandler({}))  # localhost: never through a system proxy
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            return b""
        answer = response.read()
    return answer if isinstance(json.loads(answer), dict) else b""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relaybay hook", description=__doc__)
    parser.add_argument("--url", required=True, help="relaybay hook URL (…/hooks/claude-code).")
    parser.add_argument("--token-file", type=Path, required=True, help="relaybay token file.")
    parser.add_argument(
        "--wait", action="store_true", help="Wake-up hook: wait for an owner comment."
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0  # a broken hook entry must not show errors in the session either
    try:
        body = sys.stdin.buffer.read()
        if args.wait:
            answer = forward(args.url, args.token_file, body, WAIT_TIMEOUT_SECONDS)
            text = json.loads(answer).get("wake") if answer else None
            if isinstance(text, str) and text:
                sys.stderr.write(text + "\n")
                sys.stderr.flush()
                return WAKE_EXIT_CODE
            return 0
        answer = forward(args.url, args.token_file, body)
        if answer:
            sys.stdout.buffer.write(answer)
            sys.stdout.flush()
    except Exception:
        # relaybay down, slow or misconfigured: drop the event silently, the session goes on.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
