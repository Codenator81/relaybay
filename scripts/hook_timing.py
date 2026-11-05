"""Time the hook forwarder against a live local server, and check it stays quiet when none runs.

python scripts/hook_timing.py [--runs 20] [--limit-ms 800] -- dist/relaybay/relaybay-hook
python scripts/hook_timing.py --interpreter   # this interpreter: -I -m relaybay.hook_client

Timed: N runs with `{}` on stdin against a threaded HTTP server on 127.0.0.1 that answers
`200 application/json {}`; each run must exit 0 and print `{}`. Fails when the median is above the
limit (800 ms: a bloated bundle takes seconds; Claude Code allows the command 3 s). Then one run
against a dead port must exit 0 with no output; its time is printed for comparison only, because
Windows retries a refused loopback connect until the hook's own 0.3 s timeout: a live median equal
to the dead-port time means the live run waited for that timeout too.
"""

import argparse
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANSWER = b"{}"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(ANSWER)))
        self.end_headers()
        self.wfile.write(ANSWER)

    def log_message(self, *args: object) -> None:
        pass


def dead_port() -> int:
    """A port nothing listens on: bound and released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(command: list[str], port: int, token: Path) -> subprocess.CompletedProcess[bytes]:
    url = f"http://127.0.0.1:{port}/hooks/claude-code"
    full = [*command, "--url", url, "--token-file", str(token)]
    return subprocess.run(full, input=b"{}", capture_output=True, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--limit-ms", type=float, default=800)
    parser.add_argument(
        "--interpreter", action="store_true", help="Time this interpreter's relaybay.hook_client."
    )
    parser.add_argument("command", nargs="*", help="The hook command, e.g. relaybay-hook.")
    args = parser.parse_args(argv)
    if args.interpreter:
        command = [sys.executable, "-I", "-m", "relaybay.hook_client"]
    elif args.command:
        program = Path(args.command[0])
        # Windows CreateProcess does not resolve a relative path with forward slashes; absolute()
        # rather than resolve(): a venv's python is a symlink that must stay one.
        first = str(program.absolute()) if program.is_file() else args.command[0]
        command = [first, *args.command[1:]]
    else:
        parser.error("give the hook command or --interpreter")
    name = Path(command[0]).name
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token"
            token.write_text("ci", encoding="utf-8")
            times = []
            for _ in range(args.runs):
                start = time.perf_counter()
                done = run(command, server.server_address[1], token)
                times.append((time.perf_counter() - start) * 1000)
                if done.returncode != 0 or done.stdout != ANSWER:
                    print(
                        f"{name} (live server) exited {done.returncode},"
                        f" stdout {done.stdout!r}, stderr {done.stderr!r}",
                        file=sys.stderr,
                    )
                    return 1
            start = time.perf_counter()
            dead = run(command, dead_port(), token)
            dead_ms = (time.perf_counter() - start) * 1000
    finally:
        server.shutdown()
        server.server_close()
    if dead.returncode != 0 or dead.stdout or dead.stderr:
        print(
            f"{name} (dead port) exited {dead.returncode}, stdout {dead.stdout!r},"
            f" stderr {dead.stderr!r}",
            file=sys.stderr,
        )
        return 1
    median = statistics.median(times)
    print(
        f"{name}: median {median:.0f} ms over {args.runs} runs against a live server"
        f" (min {min(times):.0f}, max {max(times):.0f}, limit {args.limit_ms:.0f});"
        f" dead port: exit 0, no output, {dead_ms:.0f} ms"
    )
    return 0 if median <= args.limit_ms else 1


if __name__ == "__main__":
    raise SystemExit(main())
