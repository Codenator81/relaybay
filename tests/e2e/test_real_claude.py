"""Opt-in: the real `claude` keeps the stream shape the fake imitates. Skipped unless
RELAYBAY_E2E_REAL_CLAUDE=1; it needs the runner's own login, spends a little quota and fires the
hooks of the user's own settings. Never in CI."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relaybay.runner.auth import check_login
from relaybay.runner.claude_cli import TurnSpec, build_command, is_batch
from tests.e2e.stream_contract import check_stream

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAYBAY_E2E_REAL_CLAUDE") != "1",
    reason="set RELAYBAY_E2E_REAL_CLAUDE=1 to run against the real claude",
)


def test_real_claude_keeps_the_stream_shape(tmp_path: Path) -> None:
    claude = shutil.which("claude")
    assert claude is not None, "no claude on PATH"
    done = subprocess.run(
        [claude, "-p", "reply ok", "--output-format", "stream-json", "--verbose"],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    check_stream([json.loads(line) for line in done.stdout.splitlines() if line.strip()])


def test_real_claude_auth_status_answers_the_fields_a_run_reads() -> None:
    """Only the three fields relaybay reads are looked at; the answer is not printed."""
    claude = shutil.which("claude")
    assert claude is not None, "no claude on PATH"
    state = check_login(claude)
    assert state.readable and state.error is None
    assert state.method is not None


def test_real_claude_takes_the_prompt_on_stdin_and_the_effort_flag(tmp_path: Path) -> None:
    """The runner's argv for a batch shim: the prompt on stdin, `--effort` passed."""
    claude = shutil.which("claude")
    assert claude is not None, "no claude on PATH"
    argv, _ = build_command(
        TurnSpec(claude, "reply ok", str(tmp_path), "haiku", "low", "default", ("Read",))
    )
    if not is_batch(claude):
        argv.remove("reply ok")
    done = subprocess.run(
        argv,
        cwd=tmp_path,
        input="reply ok",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    check_stream([json.loads(line) for line in done.stdout.splitlines() if line.strip()])
