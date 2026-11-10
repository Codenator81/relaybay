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


async def test_status_follows_the_events_and_desktop_session_end_keeps_tasks(
    service: Service,
) -> None:
    task = await bound_task(service)
    for event, status in (
        ("SessionStart", "working"),
        ("Notification", "waiting"),
        ("Stop", "idle"),
        ("UserPromptSubmit", "working"),
        ("SessionEnd", "ended"),
    ):
        await service.record_hook(hook(event))
        current = await service.get_task(task.key)
        assert (current.session_external_id, current.session_status) == (DESKTOP_A, status)
    # A desktop session outlives its Claude Code process: the next SessionStart links the new one.
    assert (await service.task_detail(task.key)).bindings[0].unbound_at is None


async def test_session_end_unbinds_a_plain_session_unless_it_goes_on(service: Service) -> None:
    task = await bound_task(service, session="acme-shop")  # the folder name: hooks reuse it
    await service.record_hook(hook("SessionStart"))
    assert (await service.get_task(task.key)).session_status == "working"
    await service.record_hook(hook("SessionEnd", reason="clear"))  # /clear: same window goes on
    assert (await service.get_task(task.key)).session_name == "acme-shop"
    await service.record_hook(hook("SessionStart", CC_B, source="clear"))
    assert (await service.get_task(task.key)).session_status == "working"

    with service.bus.subscribe() as sub:
        await service.record_hook(hook("SessionEnd", CC_B))
        events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
    after = await service.get_task(task.key)
    assert after.session_id is None
    unbound = [e for e in events if e.kind == "task.session_unbound"]
    assert unbound and unbound[0].payload["reason"] == "session_end"
    assert unbound[0].payload["end_reason"] == "prompt_input_exit"
    detail = await service.task_detail(task.key)
    assert detail.bindings[0].unbound_at is not None  # history kept


async def test_desktop_session_relinks_its_new_claude_code_id(service: Service) -> None:
    """/clear or a restart gives the desktop session a new Claude Code id: the SessionStart
    names the desktop session (from the hook's environment) and the link moves by itself."""
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await service.record_hook(hook("SessionEnd", reason="clear"))
    linked = await service.record_hook(
        hook("SessionStart", CC_B, source="clear", relaybay_host_session=DESKTOP_A.upper())
    )
    assert linked is not None and linked.external_id == DESKTOP_A
    assert (linked.cc_session_id, linked.cc_exact) == (CC_B, True)
    assert (await service.get_task(task.key)).session_status == "working"
    await service.add_owner_comment(task.key, "Check the totals")
    answer = await service.hook_answer(hook("UserPromptSubmit", CC_B))
    assert answer is not None
    assert "Check the totals" in answer["hookSpecificOutput"]["additionalContext"]
    assert [s.name for s in await service.unlinked_hook_sessions()] == []


async def test_ended_desktop_session_is_not_guessed_and_late_end_is_dropped(
    service: Service,
) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await service.record_hook(hook("SessionEnd"))
    # A terminal `claude` in the same folder (no desktop id) is not guessed onto it ...
    terminal = await service.record_hook(hook("SessionStart", CC_B))
    assert terminal is not None and terminal.external_id is None
    assert (await service.get_task(task.key)).session_status == "ended"
    # ... so the reopened window links itself, even on a plain startup.
    reopened = "3c1e2d4f-0000-4000-8000-000000000003"
    linked = await service.record_hook(
        hook("SessionStart", reopened, relaybay_host_session=DESKTOP_A)
    )
    assert linked is not None and linked.external_id == DESKTOP_A
    # A SessionEnd for an id nobody holds (it arrived after the new id took over) adds nothing.
    before = await service.list_sessions()
    assert (
        await service.record_hook(hook("SessionEnd", "8b0c1f6e-0000-4000-8000-000000000004"))
        is None
    )
    assert len(await service.list_sessions()) == len(before)


async def test_host_session_never_takes_a_live_link(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await service.record_hook(hook("SessionStart"))
    # A `claude` started from the desktop session's own tools inherits its id: a plain startup
    # does not move a link that is still live.
    nested = "3c1e2d4f-0000-4000-8000-000000000001"
    other = await service.record_hook(
        hook("SessionStart", nested, cwd="/tmp/elsewhere", relaybay_host_session=DESKTOP_A)
    )
    assert other is not None and other.external_id is None
    current = await service.get_task(task.key)
    assert (current.session_external_id, current.session_status) == (DESKTOP_A, "working")

    # Nor does the nested process's own compaction or a `claude -c -p` resume: it runs unattended.
    for source in ("compact", "resume"):
        await service.record_hook(
            hook("SessionStart", nested, source=source, relaybay_host_session=DESKTOP_A)
        )
    await service.add_owner_comment(task.key, "For the window only")
    assert await service.hook_answer(hook("PreToolUse", nested)) is None
    answer = await service.hook_answer(hook("PreToolUse"))
    assert answer is not None and "For the window only" in str(answer)
    # The window itself resuming after a restart (its old process sent no SessionEnd) moves it.
    restarted = "3c1e2d4f-0000-4000-8000-000000000002"
    moved = await service.record_hook(
        hook(
            "SessionStart",
            restarted,
            source="resume",
            relaybay_host_session=DESKTOP_A,
            relaybay_host_attended="1",
        )
    )
    assert moved is not None and (moved.external_id, moved.cc_session_id) == (DESKTOP_A, restarted)

    # An id another session's agent linked itself is never taken.
    await service.create_task("acme-shop", "Payment", session=DESKTOP_B, hook_session=CC_B)
    await service.record_hook(hook("SessionStart", CC_B))
    taken = await service.record_hook(
        hook(
            "SessionStart",
            CC_B,
            source="resume",
            relaybay_host_session=DESKTOP_A,
            relaybay_host_attended="1",
        )
    )
    assert taken is not None and taken.external_id == DESKTOP_B
    # Unknown or malformed desktop ids change nothing.
    for host in (DESKTOP_B.replace("2", "9"), "local_nope", 7):
        assert parse_hook(hook("SessionStart", relaybay_host_session=host)).host_session in (
            None,
            DESKTOP_B.replace("2", "9"),
        )


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
        svc._status_evented.clear()  # as if the last stored event were a minute old
        with svc.bus.subscribe() as sub:
            for tool in ("Read", "Edit", "Bash"):
                await svc.record_hook(hook("PreToolUse", tool_name=tool))
            assert sub.queue.qsize() == 0  # held back inside the window
            assert (await svc.get_task(task.key)).session_activity == "started (startup)"
            # The window ends at 0.3 s; the flush then writes in the background, which a busy
            # CI runner may take a while for.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if sub.queue.qsize():
                    break
            events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        assert [e.payload["activity"] for e in events] == ["Bash"]  # only the latest
        assert (await svc.get_task(task.key)).session_activity == "Bash"

        # A status change is never held back.
        with svc.bus.subscribe() as sub:
            await svc.record_hook(hook("Notification"))
            assert sub.queue.qsize() == 1
    finally:
        await svc.close()


async def test_one_status_event_per_change_names_every_bound_task(tmp_path: Path) -> None:
    # No coalescing window: every tool call reaches the session row, as when calls are seconds
    # apart. Before, each of these writes was one event per bound task (192 here).
    svc = Service(tmp_path / "db.sqlite", hook_activity_window=0)
    await svc.start()
    try:
        await svc.upsert_project("acme-shop", repo_path=CWD)
        keys = [
            (await svc.create_task("acme-shop", f"Task {n}", stage="dev", session=DESKTOP_A)).key
            for n in range(16)
        ]
        first = await svc.last_event_id()
        await svc.record_hook(hook("SessionStart"))
        for n in range(10):
            tool = ("Read", "Edit", "Bash", "Grep", "Glob")[n % 5]
            await svc.record_hook(hook("PreToolUse", tool_name=tool))
            await svc.record_hook(hook("PostToolUse", tool_name=tool))
        assert (await svc.get_task(keys[0])).session_activity == "Glob"  # the row follows
        await svc.record_hook(hook("Stop"))
        events = [
            e
            for e in await svc.events_since(first, limit=500)
            if e.kind == "session.status_changed"
        ]
        assert [e.payload["status"] for e in events] == ["working", "idle"]
        assert all(e.payload["keys"] == keys and e.task_id is None for e in events)
        assert all(e.payload["project"] == "acme-shop" and "key" not in e.payload for e in events)
        assert events[1].payload["previous"] == "working"

        # Activity alone is stored again once the last event is a minute old.
        await svc.record_hook(hook("PreToolUse", tool_name="Read"))
        svc._status_evented.clear()
        await svc.record_hook(hook("PreToolUse", tool_name="Edit"))
        await svc.record_hook(hook("PreToolUse", tool_name="Bash"))
        later = await svc.events_since(events[-1].id, limit=500)
        assert [(e.payload["status"], e.payload["activity"]) for e in later] == [
            ("working", "Read"),
            ("working", "Edit"),
        ]
    finally:
        await svc.close()


async def test_status_event_per_project_of_the_bound_tasks(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api")
    shop = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    api = await service.create_task("demo-api", "Tokens", stage="dev", session=DESKTOP_A)
    first = await service.last_event_id()
    await service.record_hook(hook("SessionStart"))
    events = [
        e for e in await service.events_since(first, limit=50) if e.kind == "session.status_changed"
    ]
    assert {(e.payload["project"], tuple(e.payload["keys"])) for e in events} == {
        ("acme-shop", (shop.key,)),
        ("demo-api", (api.key,)),
    }
    assert [e.id for e in await service.events_since(first, "demo-api")] == [
        e.id for e in events if e.payload["project"] == "demo-api"
    ]
    # The session works in acme-shop's repository: the bound task there links it by itself.
    linked = await service.events_since(first, "acme-shop", limit=50)
    assert [e.payload["how"] for e in linked if e.kind == "task.updated"] == ["auto"]


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
            # A state-bearing event retries within the budget; an activity ping tries once.
            for event, limit in (("Stop", hook_client.BUDGET_SECONDS + 0.4), ("PostToolUse", 1.0)):
                payload = json.dumps(RECORDED[event]).encode()
                monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
                started = time.monotonic()
                assert hook_client.main(["--url", url, "--token-file", str(token_file)]) == 0
                assert time.monotonic() - started < limit
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


def test_hook_client_adds_the_desktop_session_to_session_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", DESKTOP_A)
    start = json.dumps(RECORDED["SessionStart"]).encode()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ATTENDED", "1")
    sent = json.loads(hook_client.with_host_session(start))
    assert (sent["relaybay_host_session"], sent["relaybay_host_attended"]) == (DESKTOP_A, "1")
    stop = json.dumps(RECORDED["Stop"]).encode()
    assert hook_client.with_host_session(stop) is stop  # other events are not even parsed
    monkeypatch.delenv("CLAUDE_CODE_HOST_SESSION_ID")
    assert hook_client.with_host_session(start) is start


def test_http_hook_sends_the_desktop_session_as_a_header(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, bound_task, service)
    token = board.app.state.settings.token_path.read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}", "X-Relaybay-Host-Session": DESKTOP_A}
    board.post("/hooks/claude-code", json=hook("SessionStart", CC_B), headers=headers)
    [row] = [r.session for r in run_in_app(board, service.list_sessions)]
    assert (row.external_id, row.cc_session_id, row.cc_exact) == (DESKTOP_A, CC_B, True)


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


async def test_status_event_readers_use_the_keys(service: Service) -> None:
    from relaybay.mcp_server.format import event_detail
    from relaybay.web.sse import format_event

    await service.upsert_project("acme-shop", repo_path=CWD)
    keys = [
        (await service.create_task("acme-shop", f"Task {n}", stage="dev", session=DESKTOP_A)).key
        for n in range(5)
    ]
    await service.record_hook(hook("SessionStart"))
    after = await service.last_event_id()
    await service.record_hook(hook("Notification"))
    [changed] = await service.events_since(after)
    assert event_detail(changed) == (
        f"{changed.payload['session']}: working → waiting · {', '.join(keys[:3])} +2"
    )
    data = json.loads(format_event(changed).split("data: ", 1)[1])
    assert data["keys"] == keys and data["project"] == "acme-shop"
    _, notices = await service.notifications(after)
    assert [n.kind for _, n in notices] == ["permission"]  # one notice, not one per task


async def test_held_back_activity_is_stored_when_the_event_window_ends(tmp_path: Path) -> None:
    svc = Service(tmp_path / "db.sqlite", hook_activity_window=0)
    svc.activity_event_window = 0.3
    await svc.start()
    try:
        task = await bound_task(svc)
        await svc.record_hook(hook("SessionStart"))
        with svc.bus.subscribe() as sub:
            for tool in ("Read", "Edit", "Grep"):
                await svc.record_hook(hook("PreToolUse", tool_name=tool))
            assert sub.queue.qsize() == 0  # inside the event window: only the row changes
            assert (await svc.get_task(task.key)).session_activity == "Grep"
            for _ in range(100):  # the trailing write runs in the background
                await asyncio.sleep(0.05)
                if sub.queue.qsize():
                    break
            events = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        assert [(e.kind, e.payload["activity"]) for e in events] == [
            ("session.status_changed", "Grep")
        ]
    finally:
        await svc.close()
