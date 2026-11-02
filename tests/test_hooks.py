"""Hook receiver: status mapping, linking hook sessions, coalescing, SessionEnd, hook client."""

import asyncio
import io
import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay import hook_client
from relaybay.domain.hooks import folder_name, parse_hook
from relaybay.service import Service
from tests.conftest import run_in_app

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code_hooks.json"
RECORDED: dict[str, dict[str, Any]] = json.loads(FIXTURES.read_text(encoding="utf-8"))
CWD = RECORDED["SessionStart"]["cwd"]
CC_A = RECORDED["SessionStart"]["session_id"]
CC_B = "2f9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7"
DESKTOP_A = f"local_{uuid.UUID(int=1)}"
DESKTOP_B = f"local_{uuid.UUID(int=2)}"


def hook(event: str, cc_id: str = CC_A, **extra: Any) -> dict[str, Any]:
    return RECORDED[event] | {"session_id": cc_id} | extra


async def bound_task(service: Service, session: str = DESKTOP_A, title: str = "Checkout"):
    await service.upsert_project("acme-shop", repo_path=CWD)
    return await service.create_task("acme-shop", title, stage="dev", session=session)


def test_parse_hook_keeps_no_prompt_or_tool_input() -> None:
    parsed = {e: parse_hook(p) for e, p in RECORDED.items()}
    assert {e: (p.status, p.activity) for e, p in parsed.items() if p} == {
        "SessionStart": ("working", "started (startup)"),
        "UserPromptSubmit": ("working", "prompt"),
        "PreToolUse": ("working", "Bash"),
        "PostToolUse": ("working", "Bash"),
        "Notification": ("waiting", "Claude needs your permission to use Bash"),
        "Stop": ("idle", "turn finished"),
        "SessionEnd": ("ended", "session ended"),
    }
    assert parsed["SessionEnd"].end_reason == "prompt_input_exit"
    assert parse_hook({"hook_event_name": "Stop"}) is None  # no session id
    assert parse_hook(["not", "an", "object"]) is None
    assert folder_name("C:\\code\\acme-shop\\") == "acme-shop"
    assert folder_name("/home/dev/demo-api") == "demo-api"


async def test_status_follows_the_events_and_session_end_unbinds(service: Service) -> None:
    task = await bound_task(service)
    for event, status in (
        ("SessionStart", "working"),
        ("Notification", "waiting"),
        ("Stop", "idle"),
        ("UserPromptSubmit", "working"),
    ):
        await service.record_hook(hook(event))
        current = await service.get_task(task.key)
        assert (current.session_external_id, current.session_status) == (DESKTOP_A, status)

    with service.bus.subscribe() as sub:
        await service.record_hook(hook("SessionEnd"))
        events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    after = await service.get_task(task.key)
    assert after.session_id is None
    unbound = [e for e in events if e.kind == "task.session_unbound"]
    assert unbound and unbound[0].payload["reason"] == "session_end"
    assert unbound[0].payload["end_reason"] == "prompt_input_exit"
    detail = await service.task_detail(task.key)
    assert detail.bindings[0].unbound_at is not None  # history kept


async def test_unknown_events_change_nothing(service: Service) -> None:
    await bound_task(service)
    before = await service.last_event_id()
    assert (
        await service.record_hook({"hook_event_name": "PostToolBatch", "session_id": CC_A}) is None
    )
    assert await service.record_hook({"hook_event_name": "Future", "session_id": CC_A}) is None
    assert await service.last_event_id() == before


async def test_exact_link_from_hook_session_wins_over_the_folder_guess(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    # No desktop session is bound yet: the hook session becomes a plain one named after the folder.
    guessed = await service.record_hook(hook("SessionStart"))
    assert guessed is not None and guessed.name == "acme-shop" and guessed.external_id is None

    task = await service.create_task(
        "acme-shop", "Checkout", session=DESKTOP_A, session_title="Checkout", hook_session=CC_A
    )
    assert (task.session_name, task.session_status) == ("Checkout", "working")
    # The guessed row handed over its status and is gone; later events reach the desktop session.
    assert [s.name for s in await service.unlinked_hook_sessions()] == []
    await service.record_hook(hook("Notification"))
    assert (await service.get_task(task.key)).session_status == "waiting"


async def test_exact_link_through_an_alias_and_report_add(service: Service) -> None:
    from relaybay.service import collect_notes

    task = await bound_task(service)
    # task_bind_session never links a hook id (the caller may bind someone else) ...
    await service.bind_session(task.key, "checkout-dev")
    await service.record_hook(hook("Stop", CC_B))  # linked by the folder guess only
    [row] = [r.session for r in await service.list_sessions() if r.session.cc_session_id == CC_B]
    assert row.external_id == DESKTOP_A and not row.cc_exact
    # ... the agent's own write through its alias does.
    await service.add_report(task.key, "changed · S · a", session="checkout-dev", hook_session=CC_B)
    await service.record_hook(hook("Stop", CC_B))
    assert (await service.get_task(task.key)).session_status == "idle"

    # An id another live session linked itself is not moved; the write says so.
    other = await service.create_task("acme-shop", "Payment", session=DESKTOP_B)
    with collect_notes() as notes:
        await service.add_report(
            other.key, "changed · S · retry", session=DESKTOP_B, hook_session=CC_B
        )
    assert len(notes) == 1 and "belongs to another session" in notes[0]
    await service.record_hook(hook("PreToolUse", CC_B))
    assert (await service.get_task(task.key)).session_status == "working"
    assert (await service.get_task(other.key)).session_status is None

    # Once that session ended, the id may move.
    await service.record_hook(hook("SessionEnd", CC_B))
    await service.add_report(other.key, "changed · S · again", session=DESKTOP_B, hook_session=CC_B)
    await service.record_hook(hook("PreToolUse", CC_B))
    assert (await service.get_task(other.key)).session_status == "working"


async def test_cwd_fallback_links_the_one_bound_desktop_session(service: Service) -> None:
    task = await bound_task(service)
    await service.record_hook(hook("SessionStart", cwd=CWD + "/src"))  # a subfolder counts
    assert (await service.get_task(task.key)).session_status == "working"


async def test_two_sessions_in_the_same_folder(service: Service) -> None:
    task = await bound_task(service)
    first = await service.record_hook(hook("SessionStart", CC_A))
    second = await service.record_hook(hook("SessionStart", CC_B))
    assert first is not None and first.external_id == DESKTOP_A
    # The only desktop session is taken by the first hook session: the second is not guessed.
    assert second is not None and second.external_id is None
    assert second.unlinked_reason and "linked to other hooks" in second.unlinked_reason
    await service.record_hook(hook("Stop", CC_B))
    assert (await service.get_task(task.key)).session_status == "working"
    assert [s.cc_session_id for s in await service.unlinked_hook_sessions()] == [CC_B]

    # The agent of the second session names itself: exact link, out of the unlinked list.
    await service.create_task("acme-shop", "Payment", session=DESKTOP_B, hook_session=CC_B)
    assert await service.unlinked_hook_sessions() == []


async def test_several_bound_desktop_sessions_stay_unlinked(service: Service) -> None:
    first = await bound_task(service)
    second = await service.create_task("acme-shop", "Payment", session=DESKTOP_B)
    session = await service.record_hook(hook("SessionStart"))
    assert session is not None and session.unlinked_reason is not None
    assert "2 desktop sessions in acme-shop could match" in session.unlinked_reason
    for task in (first, second):
        assert (await service.get_task(task.key)).session_status is None


async def test_folder_of_two_projects_is_ambiguous(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api", repo_path=CWD)
    session = await service.record_hook(hook("SessionStart"))
    assert session is not None
    assert session.unlinked_reason == "the folder belongs to several projects (acme-shop, demo-api)"


async def test_activity_is_coalesced_per_window(tmp_path: Path) -> None:
    svc = Service(tmp_path / "db.sqlite", hook_activity_window=0.3)
    await svc.start()
    try:
        task = await bound_task(svc)
        await svc.record_hook(hook("SessionStart"))
        with svc.bus.subscribe() as sub:
            for tool in ("Read", "Edit", "Bash"):
                await svc.record_hook(hook("PreToolUse", tool_name=tool))
            assert sub.queue.qsize() == 0  # held back inside the window
            assert (await svc.get_task(task.key)).session_activity == "started (startup)"
            await asyncio.sleep(0.5)
            events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        assert [e.payload["activity"] for e in events] == ["Bash"]  # only the latest
        assert (await svc.get_task(task.key)).session_activity == "Bash"

        # A status change is never held back.
        with svc.bus.subscribe() as sub:
            await svc.record_hook(hook("Notification"))
            assert sub.queue.qsize() == 1
    finally:
        await svc.close()


# --- endpoint ----------------------------------------------------------------


def test_endpoint_needs_the_token(client: TestClient, token: str) -> None:
    assert client.post("/hooks/claude-code", json=RECORDED["Stop"]).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (
        client.post("/hooks/claude-code", json=RECORDED["Stop"], headers=wrong).status_code == 401
    )
    foreign = {"Authorization": f"Bearer {token}", "Origin": "http://evil.example"}
    response = client.post("/hooks/claude-code", json=RECORDED["Stop"], headers=foreign)
    assert response.status_code == 403


def test_endpoint_answers_and_tells_the_session_its_id(client: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    response = client.post("/hooks/claude-code", json=RECORDED["SessionStart"], headers=auth)
    assert response.status_code == 200
    output = response.json()["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert CC_A in output["additionalContext"] and "hook_session" in output["additionalContext"]

    for event in ("PreToolUse", "Stop", "PostToolUse"):
        response = client.post("/hooks/claude-code", json=RECORDED[event], headers=auth)
        assert (response.status_code, response.content) == (204, b"")
    bad = client.post("/hooks/claude-code", content=b"{not json", headers=auth)
    assert bad.status_code == 400


def test_card_and_dialog_show_the_live_status(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, bound_task, service)
    token = board.app.state.settings.token_path.read_text(encoding="utf-8").strip()
    auth = {"Authorization": f"Bearer {token}"}
    board.post("/hooks/claude-code", json=RECORDED["Notification"], headers=auth)
    card = board.get(f"/p/acme-shop/t/{task.key}/card").text
    assert 'class="status-dot dot-waiting"' in card
    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "last activity: Claude needs your permission to use Bash" in view


# --- hook client ---------------------------------------------------------------


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _silent_port() -> socket.socket:
    """A socket that accepts connections and never answers (a hung server)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    return sock


def test_hook_client_is_silent_and_quick_when_relaybay_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("t0ken\n", encoding="utf-8")
    body = json.dumps(RECORDED["Stop"]).encode()
    hung = _silent_port()
    try:
        for url in (
            f"http://127.0.0.1:{_closed_port()}/hooks/claude-code",
            f"http://127.0.0.1:{hung.getsockname()[1]}/hooks/claude-code",
        ):
            monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
            started = time.monotonic()
            assert hook_client.main(["--url", url, "--token-file", str(token_file)]) == 0
            assert time.monotonic() - started < 1.0
    finally:
        hung.close()
    # Missing token file and broken arguments: still exit 0, still nothing printed.
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
    assert hook_client.main(["--url", "http://127.0.0.1:1/x", "--token-file", "missing"]) == 0
    assert hook_client.main([]) == 0
    assert capsys.readouterr().out == ""


def test_hook_command_against_stopped_and_running_server(live_server, tmp_path: Path) -> None:
    """The command exactly as setup writes it, run as a process like Claude Code runs it."""
    server = live_server()
    token_file = server.settings.token_path
    args = [sys.executable, "-I", "-m", "relaybay.hook_client", "--token-file", str(token_file)]
    start = json.dumps(RECORDED["SessionStart"]).encode()

    down = [*args, "--url", f"http://127.0.0.1:{_closed_port()}/hooks/claude-code"]
    started = time.monotonic()
    result = subprocess.run(down, input=start, capture_output=True, timeout=30, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert time.monotonic() - started < 5  # interpreter start included

    up = [*args, "--url", f"{server.url}/hooks/claude-code"]
    result = subprocess.run(up, input=start, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert CC_A in context


def test_relaybay_hook_command_is_silent_when_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from relaybay import cli

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    port = str(_closed_port())
    assert cli.main(["hook", "--home", str(tmp_path), "--port", port]) == 0
    assert capsys.readouterr() == ("", "")


def test_oversized_hook_input_is_dropped_quietly(client: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    big = hook("PreToolUse") | {"tool_input": {"content": "x" * (300 * 1024)}}
    response = client.post("/hooks/claude-code", json=big, headers=auth)
    assert response.status_code == 204
    assert client.post("/hooks/claude-code", json=hook("Stop"), headers=auth).status_code == 204


async def test_repeated_same_activity_is_held_back_too(tmp_path: Path) -> None:
    svc = Service(tmp_path / "db.sqlite", hook_activity_window=60)
    await svc.start()
    try:
        await bound_task(svc)
        await svc.record_hook(hook("PreToolUse", tool_name="Bash"))  # status change: written
        # Outside any pending window the same tool again is no change, but it restarts the
        # window, so a burst of identical calls is not written one by one.
        svc._activity_written.clear()
        await svc.record_hook(hook("PreToolUse", tool_name="Bash"))
        assert svc._activity_written  # window started by the unchanged activity
        with svc.bus.subscribe() as sub:
            for _ in range(5):
                await svc.record_hook(hook("PreToolUse", tool_name="Bash"))
            assert sub.queue.qsize() == 0
        assert len(svc._activity_pending) == 1  # held back, written once when the window ends
    finally:
        await svc.close()
