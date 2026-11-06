"""Time the hook forwarder's start-up, and check it works against a live local server and stays
quiet when none runs.

python scripts/hook_timing.py [--runs 20] [--limit-ms 800] -- dist/relaybay/relaybay-hook
python scripts/hook_timing.py --interpreter   # this interpreter: -I -m relaybay.hook_client

Timed against the limit: N runs of `--version`, which exits before any network work, so the
median is the start-up alone (a bloated bundle takes seconds; Claude Code allows the command
3 s). Then N runs with `{}` on stdin against a threaded HTTP server on 127.0.0.1 that answers
`200 application/json {}`: each must exit 0 and print `{}`; their median is printed for
comparison only, because on Windows a loopback connect can wait for the hook's own 0.3 s timeout
and would measure that, not the program. Last, one run against a dead port must exit 0 with no
output.
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
        starts = []
        for _ in range(args.runs):
            start = time.perf_counter()
            done = subprocess.run([*command, "--version"], capture_output=True, check=False)
            starts.append((time.perf_counter() - start) * 1000)
            if done.returncode != 0 or not done.stdout.startswith(b"relaybay"):
                print(
                    f"{name} --version exited {done.returncode}, stdout {done.stdout!r},"
                    f" stderr {done.stderr!r}",
                    file=sys.stderr,
                )
                return 1
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
    median = statistics.median(starts)
    print(
        f"{name}: start-up median {median:.0f} ms over {args.runs} runs of --version"
        f" (min {min(starts):.0f}, max {max(starts):.0f}, limit {args.limit_ms:.0f});"
        f" live server: {statistics.median(times):.0f} ms median, every run answered {{}};"
        f" dead port: exit 0, no output, {dead_ms:.0f} ms"
    )
    return 0 if median <= args.limit_ms else 1


if __name__ == "__main__":
    raise SystemExit(main())
