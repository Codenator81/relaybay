"""The forwarder's retry and spool, the server's handling of replayed events and the doctor item."""

import io
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import platformdirs
import pytest
from starlette.testclient import TestClient

from relaybay import doctor, hook_client
from relaybay.domain.plan import TODO_TOOLS
from relaybay.service import Service
from tests.conftest import run_in_app
from tests.test_hooks import CC_A, CWD, DESKTOP_A, DESKTOP_B, RECORDED, bound_task, hook

URL = "http://127.0.0.1:9/hooks/claude-code"


def body(event: str, **extra: Any) -> bytes:
    return json.dumps(hook(event, **extra)).encode()


def todo_create(item_id: str = "1", subject: str = "Write the tests") -> dict[str, Any]:
    return {
        "session_id": CC_A,
        "cwd": CWD,
        "hook_event_name": "PostToolUse",
        "tool_name": "TaskCreate",
        "tool_input": {"subject": subject},
        "tool_response": {"task": {"id": item_id, "subject": subject}},
    }


def todo_update(item_id: str, status: str) -> dict[str, Any]:
    return {
        "session_id": CC_A,
        "cwd": CWD,
        "hook_event_name": "PostToolUse",
        "tool_name": "TaskUpdate",
        "tool_input": {"taskId": item_id, "status": status},
        "tool_response": {},
    }


class FakePost:
    """Stands in for `hook_client.post`: `down` refuses every call, `fail_at` refuses from that
    call on; every body that got through is kept."""

    def __init__(self, down: bool = False, fail_at: int | None = None) -> None:
        self.down = down
        self.fail_at = fail_at
        self.calls: list[tuple[bytes, float]] = []
        self.taken: list[dict[str, Any]] = []

    def __call__(self, url: str, token: str, data: bytes, timeout: float) -> bytes:
        self.calls.append((data, timeout))
        if self.down or (self.fail_at is not None and len(self.calls) >= self.fail_at):
            raise ConnectionRefusedError("refused")
        self.taken.append(json.loads(data))
        return b""


@pytest.fixture
def fake_post(monkeypatch: pytest.MonkeyPatch) -> FakePost:
    fake = FakePost()
    monkeypatch.setattr(hook_client, "post", fake)
    monkeypatch.setattr(hook_client, "RETRY_PAUSE_SECONDS", 0.01)
    return fake


def spooled(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


# --- forwarder ---------------------------------------------------------------


def test_events_are_classified_by_what_they_carry() -> None:
    assert hook_client.TODO_TOOLS == TODO_TOOLS
    kinds = {event: hook_client.event_kind(body(event)) for event in RECORDED}
    assert kinds == {
        "SessionStart": "state",
        "UserPromptSubmit": "state",
        "PreToolUse": "guard",
        "PostToolUse": "ping",
        "Notification": "state",
        "Stop": "state",
        "SessionEnd": "state",
    }
    for tool in TODO_TOOLS:
        payload = json.dumps(todo_create() | {"tool_name": tool}).encode()
        assert hook_client.event_kind(payload) == "state"
    spaced = json.dumps(hook("Stop"), indent=2).encode()
    assert hook_client.event_kind(spaced) == "state"
    assert hook_client.event_kind(b"{}") == "ping"
    assert hook_client.event_kind(b"not json") == "ping"


def test_only_state_bearing_events_are_retried_and_spooled(
    fake_post: FakePost, desktop_config_dir: Path
) -> None:
    fake_post.down = True
    path = desktop_config_dir / hook_client.SPOOL_FILE
    assert hook_client.deliver(URL, "t", body("PreToolUse")) == b""
    assert hook_client.deliver(URL, "t", body("PostToolUse")) == b""
    assert [t for _, t in fake_post.calls] == [hook_client.TIMEOUT_SECONDS] * 2
    assert not path.exists()  # neither the guard nor a ping is ever spooled

    fake_post.calls.clear()
    assert hook_client.deliver(URL, "t", body("UserPromptSubmit")) == b""
    assert [t for _, t in fake_post.calls] == [hook_client.STATE_TIMEOUT_SECONDS] * 2
    [line] = spooled(path)
    assert line["hook_event_name"] == "UserPromptSubmit"
    assert "prompt" not in line  # the prompt text is never written to disk
    moment = datetime.fromisoformat(line[hook_client.SPOOLED_FIELD])
    assert abs(datetime.now(UTC) - moment) < timedelta(minutes=1)


def test_the_next_run_that_reaches_the_server_sends_the_spool_first(
    fake_post: FakePost, desktop_config_dir: Path
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    fake_post.down = True
    hook_client.deliver(URL, "t", body("Stop"))
    hook_client.deliver(URL, "t", body("SessionEnd"))
    assert [e["hook_event_name"] for e in spooled(path)] == ["Stop", "SessionEnd"]
    # Still down: a ping tries the spool once and gives up, the spool stays as it was.
    fake_post.calls.clear()
    hook_client.deliver(URL, "t", body("PostToolUse"))
    assert len(fake_post.calls) == 1
    assert len(spooled(path)) == 2

    fake_post.down = False
    hook_client.deliver(URL, "t", body("SessionStart"))
    assert [e["hook_event_name"] for e in fake_post.taken] == ["Stop", "SessionEnd", "SessionStart"]
    assert [hook_client.SPOOLED_FIELD in e for e in fake_post.taken] == [True, True, False]
    assert not path.exists()


def test_a_failure_halfway_through_the_spool_keeps_the_rest(
    fake_post: FakePost, desktop_config_dir: Path
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    fake_post.down = True
    for event in ("Stop", "Notification", "SessionEnd"):
        hook_client.deliver(URL, "t", body(event))
    fake_post.down = False
    fake_post.calls.clear()
    fake_post.fail_at = 2  # the first spooled event gets through, the second does not
    hook_client.deliver(URL, "t", body("PostToolUse"))
    assert [e["hook_event_name"] for e in fake_post.taken] == ["Stop"]
    assert [e["hook_event_name"] for e in spooled(path)] == ["Notification", "SessionEnd"]


def test_the_spool_is_bounded_and_drops_the_oldest(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    for n in range(hook_client.SPOOL_LINES_MAX + 5):
        hook_client.spool(path, body("Stop", n=n))
    lines = spooled(path)
    assert len(lines) == hook_client.SPOOL_LINES_MAX
    assert (lines[0]["n"], lines[-1]["n"]) == (5, hook_client.SPOOL_LINES_MAX + 4)

    monkeypatch.setattr(hook_client, "SPOOL_BYTES_MAX", 2000)
    path.unlink()
    for n in range(20):
        hook_client.spool(path, body("Stop", n=n, pad="x" * 100))
    assert path.stat().st_size <= 2000
    kept = [e["n"] for e in spooled(path)]
    assert kept == list(range(20 - len(kept), 20)) and len(kept) >= 3
    hook_client.spool(path, body("Stop", pad="x" * 5000))  # one line over the limit: not kept
    assert [e["n"] for e in spooled(path)] == kept


def test_the_forwarder_finds_the_desktop_config_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RELAYBAY_CONFIG_DIR")
    expected = Path(platformdirs.user_config_dir("relaybay", appauthor=False))
    assert hook_client.config_folder() == expected
    assert hook_client.spool_path({"RELAYBAY_CONFIG_DIR": "/x"}) == Path("/x/hook-spool.jsonl")


def test_main_spools_on_a_closed_port_and_replays_to_a_live_server(
    live_server, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = live_server()
    token = str(server.settings.token_path)
    path = desktop_config_dir / hook_client.SPOOL_FILE
    monkeypatch.setattr(hook_client, "BUDGET_SECONDS", 0.6)
    monkeypatch.setattr(hook_client, "STATE_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(hook_client, "STARTED", time.monotonic())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body("SessionStart"))))
    closed = "http://127.0.0.1:1/hooks/claude-code"
    assert hook_client.main(["--url", closed, "--token-file", token]) == 0
    assert [e["hook_event_name"] for e in spooled(path)] == ["SessionStart"]

    # Room for a slow runner's first request: this checks the replay, not its speed.
    monkeypatch.setattr(hook_client, "BUDGET_SECONDS", 10.0)
    monkeypatch.setattr(hook_client, "STATE_TIMEOUT_SECONDS", 4.0)
    monkeypatch.setattr(hook_client, "STARTED", time.monotonic())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body("PostToolUse"))))
    live = f"{server.url}/hooks/claude-code"
    assert hook_client.main(["--url", live, "--token-file", token]) == 0
    assert not path.exists()


def test_a_state_event_goes_to_the_spool_at_once_when_the_spool_got_no_answer(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    stamps = iter(["2026-01-01T00:00:00.000+00:00", "2026-01-01T00:00:09.000+00:00"])
    monkeypatch.setattr(hook_client, "_utc_now", lambda: next(stamps))
    fake_post.down = True
    hook_client.deliver(URL, "t", body("Stop"))
    assert len(fake_post.calls) == 2
    fake_post.calls.clear()
    hook_client.deliver(URL, "t", body("SessionEnd"))
    assert len(fake_post.calls) == 1  # the spool's first line only, no tries of its own
    # Each line carries the time its run started, not the time it was written.
    assert [e[hook_client.SPOOLED_FIELD] for e in spooled(path)] == [
        "2026-01-01T00:00:00.000+00:00",
        "2026-01-01T00:00:09.000+00:00",
    ]


def test_a_failed_rewrite_keeps_the_spool_and_still_sends_the_event(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: the rename fails while another forwarder holds the file (WinError 5 / 32)."""
    path = desktop_config_dir / hook_client.SPOOL_FILE
    hook_client.spool(path, body("Stop"))

    unlink = Path.unlink

    def held(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == hook_client.SPOOL_FILE or args:  # the spool, or a rename onto it
            raise PermissionError(13, "in use")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", held)
    monkeypatch.setattr(Path, "unlink", held)
    assert hook_client.deliver(URL, "t", body("UserPromptSubmit")) == b""
    assert [e["hook_event_name"] for e in fake_post.taken] == ["Stop", "UserPromptSubmit"]
    assert [e["hook_event_name"] for e in spooled(path)] == ["Stop"]  # nothing removed
    hook_client._replace(path, b"x\n")  # a failed rename leaves no temporary file
    assert [e["hook_event_name"] for e in spooled(path)] == ["Stop"]
    monkeypatch.undo()
    assert sorted(f.name for f in desktop_config_dir.iterdir() if f.suffix == ".tmp") == []


def test_another_forwarder_holding_the_lock_skips_the_drain(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    monkeypatch.setattr(hook_client, "LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(hook_client, "APPEND_WAIT_SECONDS", 0.05)
    hook_client.spool(path, body("Stop"))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path; from relaybay import hook_client;"
            "lock = hook_client._SpoolLock(Path(sys.argv[1])).__enter__();"
            "print(lock.held, flush=True); time.sleep(30)",
            str(path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "True"
        hook_client.deliver(URL, "t", body("PostToolUse"))
        assert [e["hook_event_name"] for e in fake_post.taken] == ["PostToolUse"]
        fake_post.down = True
        hook_client.deliver(URL, "t", body("Notification"))  # a side file of its own
        assert [e["hook_event_name"] for e in spooled(path)] == ["Stop"]
        [part] = hook_client.spool_parts(path)
        assert [e["hook_event_name"] for e in spooled(part)] == ["Notification"]
        assert doctor._hook_spool_check({}).detail.startswith("2 events waiting since ")
    finally:
        holder.kill()
        holder.wait()
    hook_client.spool(path, body("SessionEnd"))  # the next run with the lock takes it in
    assert [e["hook_event_name"] for e in spooled(path)] == ["Stop", "SessionEnd", "Notification"]
    assert hook_client.spool_parts(path) == []
    fake_post.down = False
    hook_client.spool(path, body("Stop"))
    # A side file nobody took in yet is sent by the drain too.
    part = path.with_name("hook-spool.1.2.part")
    part.write_bytes(json.dumps(hook("UserPromptSubmit")).encode() + b"\n")
    fake_post.taken.clear()
    hook_client.deliver(URL, "t", body("PostToolUse"))
    sent = [e["hook_event_name"] for e in fake_post.taken]
    assert sent == ["Stop", "SessionEnd", "Notification", "Stop", "UserPromptSubmit", "PostToolUse"]
    assert not path.exists() and hook_client.spool_parts(path) == []


def test_a_spool_line_cut_short_by_the_budget_leaves_the_event_its_own_try(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server answering slowly: the spool uses up the budget, the live event is still sent."""
    path = desktop_config_dir / hook_client.SPOOL_FILE
    for n in range(40):
        hook_client.spool(path, body("Stop", n=n))
    clock = [0.0]
    monkeypatch.setattr(hook_client.time, "monotonic", lambda: clock[0])

    def slow(url: str, token: str, data: bytes, timeout: float) -> bytes:
        fake_post.calls.append((data, timeout))
        if timeout < 0.3:  # the try cut by the budget times out
            clock[0] += timeout
            raise TimeoutError("timed out")
        clock[0] += 0.3
        fake_post.taken.append(json.loads(data))
        return b""

    monkeypatch.setattr(hook_client, "post", slow)
    hook_client.deliver(URL, "t", body("Stop", n="live"), started=0.0)
    assert fake_post.taken[-1]["n"] == "live"
    assert "live" not in [e.get("n") for e in spooled(path)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_spool_stays_private_after_a_trim(
    fake_post: FakePost, desktop_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    monkeypatch.setattr(hook_client, "SPOOL_LINES_MAX", 2)
    old = os.umask(0o022)
    try:
        for n in range(4):
            hook_client.spool(path, body("Stop", n=n))
    finally:
        os.umask(old)
    assert [e["n"] for e in spooled(path)] == [2, 3]
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_dead_server_never_reaches_claude_codes_kill(
    desktop_config_dir: Path, tmp_path: Path
) -> None:
    """End to end, as Claude Code runs it: the interpreter's start, the tries and the spool."""
    token = tmp_path / "token"
    token.write_text("t0ken", encoding="utf-8")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    command = [
        sys.executable,
        *("-I", "-m", "relaybay.hook_client"),
        *("--url", f"http://127.0.0.1:{port}/hooks/claude-code", "--token-file", str(token)),
    ]
    times = []
    for event in ("Stop", "UserPromptSubmit", "Notification", "Stop", "SessionEnd", "Stop") * 2:
        started = time.monotonic()
        done = subprocess.run(command, input=body(event), capture_output=True, check=False)
        times.append(time.monotonic() - started)
        assert (done.returncode, done.stdout, done.stderr) == (0, b"", b"")
    path = desktop_config_dir / hook_client.SPOOL_FILE
    assert len(spooled(path)) == 12  # every event kept
    assert statistics.median(times) < 1.8, times  # while it stays down, later events pay less
    assert max(times) < 2.8, times  # the first event pays its two tries; Claude Code kills at 3


def test_relaybay_hook_counts_the_budget_from_the_process_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from relaybay import cli

    seen = []
    monkeypatch.setattr(hook_client, "main", lambda args, started=None: seen.append(started) or 0)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    assert cli.main(["hook", "--home", str(tmp_path), "--port", "1"]) == 0
    assert seen == [cli.STARTED]


# --- server --------------------------------------------------------------------


def at(minutes: int) -> str:
    """A time long before the test runs, so a live event is always newer."""
    return (datetime(2020, 1, 1, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


async def test_replays_keep_order_and_an_old_end_does_not_end_a_new_start(
    service: Service,
) -> None:
    task = await bound_task(service)
    await service.record_hook(hook("SessionStart"))  # live, now: newer than every replay below
    await service.record_hook(hook("SessionEnd") | {"relaybay_spooled_at": at(0)})
    current = await service.get_task(task.key)
    assert current.session_status == "working"
    assert (await service.task_detail(task.key)).bindings[0].unbound_at is None

    # Replays newer than what the session has apply in their own order and time.
    other = await bound_task(service, session=DESKTOP_B, title="Refunds")
    cc = "3a9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7"
    host = {"relaybay_host_session": DESKTOP_B, "relaybay_host_attended": "1"}
    for event, minute, extra in (
        ("SessionStart", 0, host),
        ("Stop", 1, {}),
        ("UserPromptSubmit", 2, {}),
        ("Notification", 3, {}),
    ):
        replay = hook(event, cc, **extra) | {"relaybay_spooled_at": at(minute)}
        await service.record_hook(replay)
    replayed = await service.get_task(other.key)
    assert replayed.session_status == "waiting"
    # The same events again (a run killed halfway): older than the session, nothing changes.
    await service.record_hook(hook("Stop", cc) | {"relaybay_spooled_at": at(1)})
    assert (await service.get_task(other.key)).session_status == "waiting"


async def test_a_replayed_todo_change_applies_only_over_older_items(service: Service) -> None:
    task = await bound_task(service)
    await service.record_hook(hook("SessionStart"))
    await service.record_hook(todo_create("1"))
    await service.record_hook(todo_update("1", "completed"))  # live: written now
    stale = todo_update("1", "in_progress") | {"relaybay_spooled_at": at(0)}
    await service.record_hook(stale)
    todo = (await service.task_detail(task.key)).todo
    assert [(i.item_id, i.status) for i in todo] == [("1", "completed")]

    fresh = todo_create("2", "Second") | {"relaybay_spooled_at": datetime.now(UTC).isoformat()}
    await service.record_hook(fresh)
    assert [i.item_id for i in (await service.task_detail(task.key)).todo] == ["1", "2"]


def test_a_replay_gets_no_answer_and_takes_no_comments(board: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    service: Service = board.app.state.service

    async def seed() -> str:
        await service.upsert_project("acme-shop", repo_path=CWD)
        task = await service.create_task(
            "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=CC_A
        )
        await service.add_owner_comment(task.key, "Check the totals")
        return task.key

    run_in_app(board, seed)
    replay = hook("UserPromptSubmit") | {"relaybay_spooled_at": datetime.now(UTC).isoformat()}
    response = board.post("/hooks/claude-code", json=replay, headers=auth)
    assert (response.status_code, response.content) == (204, b"")
    live = board.post("/hooks/claude-code", json=hook("UserPromptSubmit"), headers=auth)
    assert "Check the totals" in live.json()["hookSpecificOutput"]["additionalContext"]


# --- doctor --------------------------------------------------------------------


def test_doctor_reports_the_spool(desktop_config_dir: Path) -> None:
    path = desktop_config_dir / hook_client.SPOOL_FILE
    item = doctor._hook_spool_check({})
    assert (item.ok, item.detail) == (True, f"empty ({path})")

    now = datetime.now(UTC)
    lines = [
        json.dumps(hook("Stop") | {"relaybay_spooled_at": (now - timedelta(minutes=m)).isoformat()})
        for m in (3, 1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    item = doctor._hook_spool_check({}, now=now)
    assert item.ok and item.detail.startswith("2 events waiting since ")
    late = doctor._hook_spool_check({}, now=now + timedelta(minutes=8))
    assert not late.ok and "the next hook event that reaches the server sends them" in late.detail
