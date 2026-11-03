"""Comments from the board to agents: delivery through hooks, Stop blocks, wake-up, MCP, board."""

import asyncio
import io
import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from relaybay import hook_client
from relaybay.domain.comments import CONTEXT_MAX
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_hooks import CC_A, CC_B, CWD, DESKTOP_A, DESKTOP_B, hook
from tests.test_mcp import call, connect

HTMX = {"HX-Request": "true", "Origin": BASE}


async def fire(svc: Service, event: str, cc_id: str = CC_A, **extra: Any) -> Any:
    """One hook event as the receiver handles it: status first, then the answer."""
    payload = hook(event, cc_id, **extra)
    await svc.record_hook(payload)
    return await svc.hook_answer(payload)


async def linked_task(svc: Service, title: str = "Checkout", cc_id: str = CC_A):
    """A task bound to a desktop session whose agent linked its hook session itself."""
    await svc.upsert_project("acme-shop", repo_path=CWD)
    return await svc.create_task(
        "acme-shop",
        title,
        stage="dev",
        session=DESKTOP_A,
        session_title="Checkout",
        hook_session=cc_id,
    )


def context_of(answer: dict) -> str:
    return answer["hookSpecificOutput"]["additionalContext"]


# --- delivery ------------------------------------------------------------------


async def test_tool_event_delivers_owner_comments_once(service: Service) -> None:
    task = await linked_task(service)
    assert await fire(service, "PostToolUse") is None  # nothing to deliver: empty answer
    comment = await service.add_owner_comment(task.key, "Use the *green* button")
    with service.bus.subscribe() as sub:
        answer = await fire(service, "PostToolUse")
        kinds = [sub.queue.get_nowait().kind for _ in range(sub.queue.qsize())]
    assert answer["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    text = context_of(answer)
    assert f"Owner comment on {task.key} (#{comment.id}): Use the *green* button" in text
    assert "comment_ack" in text and "comment_add" in text
    assert "comment.delivered" in kinds
    assert await fire(service, "PreToolUse") is None  # delivered once
    _, [stored] = await service.list_comments(task.key)
    assert (stored.delivered_to_name, stored.read_at) == ("Checkout", None)
    assert stored.delivered_at is not None


async def test_prompt_and_pre_tool_events_deliver_too(service: Service) -> None:
    task = await linked_task(service)
    await service.add_owner_comment(task.key, "first")
    assert "first" in context_of(await fire(service, "UserPromptSubmit"))
    await service.add_owner_comment(task.key, "second")
    answer = await fire(service, "PreToolUse")
    assert answer["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "second" in context_of(answer) and "first" not in context_of(answer)


async def test_no_comment_text_for_guessed_unlinked_or_other_sessions(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    await service.add_owner_comment(task.key, "secret plan")
    # Linked by its folder only (the one bound desktop session): a guess, so nothing goes out.
    assert "secret" not in json.dumps(await fire(service, "SessionStart"))
    assert await fire(service, "PostToolUse") is None
    assert await fire(service, "Stop") is None
    # Another hook session in the same folder (unlinked, plain) gets nothing either.
    assert await fire(service, "PostToolUse", CC_B) is None
    # A session linked exactly but bound to another task gets nothing.
    other = await service.create_task("acme-shop", "Payment", session=DESKTOP_B, hook_session=CC_B)
    assert other.session_external_id == DESKTOP_B
    assert await fire(service, "PostToolUse", CC_B) is None
    _, [stored] = await service.list_comments(task.key)
    assert stored.delivered_at is None
    # Once the agent of DESKTOP_A links itself, the comment reaches it.
    await service.add_report(task.key, "changed · S · x", session=DESKTOP_A, hook_session=CC_A)
    assert "secret plan" in context_of(await fire(service, "PostToolUse"))


async def test_unbound_or_ended_session_gets_nothing(service: Service) -> None:
    task = await linked_task(service)
    await service.unbind_session(task.key)
    await service.add_owner_comment(task.key, "for whoever is bound")
    assert await fire(service, "PostToolUse") is None
    await fire(service, "SessionEnd")
    await service.bind_session(task.key, DESKTOP_A)
    assert await service.hook_answer(hook("PostToolUse")) is None  # ended, not resumed
    assert "for whoever is bound" in context_of(await fire(service, "PostToolUse"))  # resumed


async def test_session_start_counts_unread_comments(service: Service) -> None:
    task = await linked_task(service)
    answer = await fire(service, "SessionStart")
    assert CC_A in context_of(answer) and "unread owner comment" not in context_of(answer)
    await service.add_owner_comment(task.key, "first remark")
    await service.add_owner_comment(task.key, "second remark")
    text = context_of(await fire(service, "SessionStart"))
    assert "You have 2 unread owner comment(s)" in text and "first remark" not in text
    _, items = await service.list_comments(task.key)
    assert all(c.delivered_at is None for c in items)  # a count is not a delivery


async def test_long_comments_are_split_across_answers(service: Service) -> None:
    task = await linked_task(service)
    for n in range(3):
        await service.add_owner_comment(task.key, f"{n}" + "x" * 3990)
    answers = [context_of(await fire(service, "PostToolUse")) for _ in range(3)]
    assert all(len(text) < CONTEXT_MAX + 400 for text in answers)
    assert [text.count("Owner comment on") for text in answers] == [1, 1, 1]
    assert "2 more unread owner comment(s)" in answers[0]
    assert "more unread" not in answers[2]
    assert await fire(service, "PostToolUse") is None


# --- Stop ----------------------------------------------------------------------


async def test_stop_blocks_twice_per_turn_then_allows(service: Service) -> None:
    task = await linked_task(service)
    comment = await service.add_owner_comment(task.key, "rename it")
    first = await fire(service, "Stop")
    assert first["decision"] == "block" and f"(#{comment.id}): rename it" in first["reason"]
    second = await fire(service, "Stop", stop_hook_active=True)
    assert second["decision"] == "block"
    assert await fire(service, "Stop", stop_hook_active=True) is None  # relaybay's cap
    # A comment causes at most two Stop blocks in all: the next turns end normally, the
    # SessionStart line and comments_list still name it.
    await fire(service, "UserPromptSubmit")
    assert await fire(service, "Stop") is None
    assert "1 unread owner comment" in context_of(await fire(service, "SessionStart"))
    # A new comment blocks again, and its reason repeats only the new one.
    fresh = await service.add_owner_comment(task.key, "and the icon")
    await fire(service, "UserPromptSubmit")
    reason = (await fire(service, "Stop"))["reason"]
    assert f"#{fresh.id}" in reason and f"#{comment.id})" not in reason


async def test_rebind_hands_delivered_unread_comments_to_the_new_session(
    service: Service,
) -> None:
    task = await linked_task(service)
    comment = await service.add_owner_comment(task.key, "still open")
    assert "still open" in context_of(await fire(service, "PostToolUse"))
    # Session A never acknowledged it; the task moves to session B (linked by its own write).
    await service.bind_session(task.key, DESKTOP_B)
    await service.add_report(
        task.key, "changed · S · took over", session=DESKTOP_B, hook_session=CC_B
    )
    assert await fire(service, "PostToolUse") is None  # A is no longer bound
    answer = await fire(service, "PostToolUse", CC_B)
    assert f"(#{comment.id}): still open" in context_of(answer)
    _, [stored] = await service.list_comments(task.key)
    assert stored.delivered_to_name == DESKTOP_B
    assert (await fire(service, "Stop", CC_B))["decision"] == "block"


async def test_coordinator_binding_another_session_keeps_its_own_link(service: Service) -> None:
    from relaybay.service import collect_notes

    coordinator = "local_cccccccc-0000-4000-8000-000000000000"
    await service.upsert_project("acme-shop", repo_path=CWD)
    own = await service.create_task(
        "acme-shop", "Coordination", session=coordinator, hook_session=CC_A
    )
    work = await service.create_task("acme-shop", "Checkout", stage="dev")
    # The coordinator binds session B; task_bind_session links no hook id at all.
    await service.bind_session(work.key, DESKTOP_B)
    await service.add_owner_comment(work.key, "for B only")
    assert await fire(service, "PostToolUse") is None  # the coordinator never gets it
    assert (await service.get_task(own.key)).session_external_id == coordinator
    await fire(service, "Stop")
    assert (await service.get_task(own.key)).session_status == "idle"  # its link stays
    # B links itself; a later write naming B with the coordinator's id does not move it.
    await service.add_report(work.key, "changed · S · b", session=DESKTOP_B, hook_session=CC_B)
    with collect_notes() as notes:
        await service.add_report(work.key, "changed · S · c", session=DESKTOP_B, hook_session=CC_A)
    assert "belongs to another session" in notes[0]
    assert await fire(service, "PostToolUse") is None
    assert "for B only" in context_of(await fire(service, "PostToolUse", CC_B))
    assert (await service.get_task(own.key)).session_status == "working"


async def test_stop_after_ack_allows_and_never_stacks_on_other_hooks(service: Service) -> None:
    task = await linked_task(service)
    comment = await service.add_owner_comment(task.key, "check the totals")
    assert "check the totals" in context_of(await fire(service, "PostToolUse"))
    # Another hook keeps the turn going: relaybay does not add a block of its own.
    assert await fire(service, "Stop", stop_hook_active=True) is None
    assert (await fire(service, "Stop"))["decision"] == "block"
    row, read = await service.ack_comments(comment_id=comment.id, session=DESKTOP_A)
    assert (row.key, read) == (task.key, [comment.id])
    assert await fire(service, "Stop", stop_hook_active=True) is None
    assert await fire(service, "Stop") is None


async def test_reply_marks_read_and_lands_in_the_inbox(service: Service) -> None:
    task = await linked_task(service)
    a = await service.add_owner_comment(task.key, "a")
    b = await service.add_owner_comment(task.key, "b")
    reply, read = await service.add_agent_comment(
        task.key, "Done with a", in_reply_to=a.id, session=DESKTOP_A
    )
    assert read == [a.id] and reply.author == "Checkout" and reply.author_kind == "agent"
    inbox = await service.inbox([])
    assert [(t.key, [c.id for c in items]) for t, items in inbox.replies] == [
        (task.key, [reply.id])
    ]
    assert inbox.count == 1
    assert (await service.ack_comments(task=task.key))[1] == [b.id]
    _, items = await service.list_comments(task.key, unread_only=True)
    assert [c.id for c in items] == [reply.id]  # b was acked with the task
    # The owner answering marks the agent's replies read.
    await service.add_owner_comment(task.key, "thanks")
    assert (await service.inbox([])).replies == []


async def test_comment_validation(service: Service) -> None:
    from relaybay.domain.errors import NotFoundError, ValidationError

    task = await linked_task(service)
    with pytest.raises(ValidationError):
        await service.add_owner_comment(task.key, "   ")
    with pytest.raises(ValidationError):
        await service.add_owner_comment(task.key, "x" * 4001)
    with pytest.raises(ValidationError):
        await service.ack_comments()
    with pytest.raises(NotFoundError):
        await service.ack_comments(comment_id=999)
    other = await service.create_task("acme-shop", "Payment")
    foreign = await service.add_owner_comment(other.key, "elsewhere")
    with pytest.raises(NotFoundError):
        await service.add_agent_comment(task.key, "re", in_reply_to=foreign.id)


# --- wake-up (asyncRewake) -------------------------------------------------------


async def test_wait_wakes_an_idle_session_with_its_comment(service: Service) -> None:
    task = await linked_task(service)
    await fire(service, "Stop")  # idle, nothing pending
    waiter = asyncio.create_task(service.wait_for_comments(hook("Stop"), hold=5, poll=0.02))
    await asyncio.sleep(0.1)
    comment = await service.add_owner_comment(task.key, "wake up")
    text = await asyncio.wait_for(waiter, 5)
    assert text is not None and f"(#{comment.id}): wake up" in text
    _, [stored] = await service.list_comments(task.key)
    assert stored.delivered_at is not None
    assert await fire(service, "PostToolUse") is None  # not delivered twice


async def test_wait_gives_up_without_exact_link_or_when_superseded(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    task = await service.create_task("acme-shop", "Checkout", stage="dev", session=DESKTOP_A)
    await fire(service, "Stop")  # guessed link only
    await service.add_owner_comment(task.key, "not for a guess")
    assert await service.wait_for_comments(hook("Stop"), hold=1, poll=0.02) is None
    assert await service.wait_for_comments({"session_id": 5}, hold=1) is None

    await service.add_report(task.key, "changed · S · x", session=DESKTOP_A, hook_session=CC_A)
    await fire(service, "UserPromptSubmit")  # working: delivers the first comment itself
    late = await service.add_owner_comment(task.key, "while it works")
    first = asyncio.create_task(service.wait_for_comments(hook("Stop"), hold=5, poll=0.02))
    await asyncio.sleep(0.1)
    second = asyncio.create_task(service.wait_for_comments(hook("Stop"), hold=0.3, poll=0.02))
    assert await asyncio.wait_for(first, 2) is None  # a newer wait took over
    assert await second is None  # the session is working the whole time
    _, items = await service.list_comments(task.key)
    assert [c.delivered_at is None for c in items] == [False, True] and items[1].id == late.id


# --- hook endpoint and client -------------------------------------------------


def test_endpoint_answers_with_comments_and_stop_block(client: TestClient, token: str) -> None:
    auth = {"Authorization": f"Bearer {token}"}
    service: Service = client.app.state.service
    task = run_in_app(client, linked_task, service)
    run_in_app(client, service.add_owner_comment, task.key, "Owner says hi")
    answer = client.post("/hooks/claude-code", json=hook("PostToolUse"), headers=auth)
    assert answer.status_code == 200
    assert "Owner says hi" in context_of(answer.json())
    stop = client.post("/hooks/claude-code", json=hook("Stop"), headers=auth)
    assert stop.status_code == 200 and stop.json()["decision"] == "block"
    # Another session in the same folder: an empty answer, no comment text.
    other = client.post("/hooks/claude-code", json=hook("PostToolUse", CC_B), headers=auth)
    assert other.status_code == 204 and other.content == b""
    assert client.post("/hooks/claude-code/wait", json=hook("Stop")).status_code == 401


class _FakeServer(HTTPServer):
    reply: tuple[int, str, bytes] = (204, "application/json", b"")


class _Answer(BaseHTTPRequestHandler):
    """Answers every POST with the server's `reply` = (status, content type, body)."""

    server: _FakeServer

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, content_type, body = self.server.reply
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def fake_server() -> Any:
    server = _FakeServer(("127.0.0.1", 0), _Answer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_client_prints_only_a_json_object_from_a_200(
    fake_server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("t0ken", encoding="utf-8")
    url = f"http://127.0.0.1:{fake_server.server_address[1]}/hooks/claude-code"
    block = json.dumps({"decision": "block", "reason": "r"}).encode()
    cases = [
        ((200, "application/json", block), block.decode()),
        ((200, "text/plain", b"hello"), ""),
        ((200, "application/json", b"[1, 2]"), ""),
        ((200, "application/json", b"{broken"), ""),
        ((204, "application/json", b""), ""),
        ((500, "application/json", block), ""),
    ]
    for reply, printed in cases:
        fake_server.reply = reply
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert hook_client.main(["--url", url, "--token-file", str(token_file)]) == 0
        assert capsys.readouterr() == (printed, "")


def test_wait_client_exits_2_with_the_comment_on_stderr(
    fake_server: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("t0ken", encoding="utf-8")
    url = f"http://127.0.0.1:{fake_server.server_address[1]}/hooks/claude-code/wait"
    args = ["--url", url, "--token-file", str(token_file), "--wait"]
    for reply, code, err in (
        ((200, "application/json", b'{"wake": "Owner comment"}'), 2, "Owner comment\n"),
        ((204, "application/json", b""), 0, ""),
        ((200, "application/json", b'{"wake": ""}'), 0, ""),
        ((200, "text/plain", b"nope"), 0, ""),
    ):
        fake_server.reply = reply
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
        assert hook_client.main(args) == code
        assert capsys.readouterr() == ("", err)


def test_wait_command_against_a_live_server(live_server: Callable[[], LiveServer]) -> None:
    """The wake-up command as setup writes it, run as a process: it waits while nothing is
    there and exits 2 with the comment on stderr once the owner writes one on the board."""
    server = live_server()
    base = [
        sys.executable,
        *("-I", "-m", "relaybay.hook_client"),
        *("--token-file", str(server.settings.token_path)),
    ]
    post = [*base, "--url", f"{server.url}/hooks/claude-code"]

    async def seed() -> str:
        async with connect(server) as client:
            await call(client, "project_upsert", slug="acme-shop", repo_path=CWD)
            created = await call(
                client,
                "task_create",
                project="acme-shop",
                title="Checkout",
                session=DESKTOP_A,
                hook_session=CC_A,
            )
        return created.split("**")[1]

    key = asyncio.run(seed())
    subprocess.run(post, input=json.dumps(hook("Stop")).encode(), check=True, timeout=30)
    wait = subprocess.Popen(
        [*base, "--url", f"{server.url}/hooks/claude-code/wait", "--wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert wait.stdin is not None
        wait.stdin.write(json.dumps(hook("Stop")).encode())
        wait.stdin.close()
        wait.stdin = None  # communicate() must not flush the closed pipe (Python 3.12)
        time.sleep(1.5)
        assert wait.poll() is None  # still waiting: nothing to wake the session for
        with httpx.Client(base_url=server.url, headers={"Origin": server.url}) as board:
            assert board.get(f"/login?token={server.token}").status_code == 303
            form = {"body": "Wake up, please", "csrf_token": csrf_token(server.token)}
            sent = board.post(
                f"/p/acme-shop/t/{key}/comments", data=form, headers={"HX-Request": "true"}
            )
            assert sent.status_code == 200, sent.text
        out, err = wait.communicate(timeout=20)
    finally:
        wait.kill()
    assert wait.returncode == 2 and out == b""
    assert b"Wake up, please" in err and b"comment_ack" in err


# --- board -----------------------------------------------------------------------


def test_board_thread_shows_marks_and_escapes(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, linked_task, service)
    form = {"body": "Rename it <script>x()</script>", "csrf_token": csrf_token(token)}
    url = f"/p/acme-shop/t/{task.key}/comments"
    assert board.post(url, data={"body": "no csrf"}, headers=HTMX).status_code == 403
    saved = board.post(url, data=form, headers=HTMX)
    assert saved.status_code == 200 and saved.headers["X-Relaybay-Comment"] == "saved"
    assert "<script>x()" not in saved.text and "not delivered yet" in saved.text
    assert 'id="comments-' + task.key + '"' in saved.text

    run_in_app(board, fire, service, "PostToolUse")
    thread = board.get(url)
    assert "delivered" in thread.text and "to Checkout" in thread.text
    assert "mark-read" not in thread.text

    run_in_app(board, service.ack_comments, task=task.key)
    run_in_app(board, service.add_agent_comment, task.key, "On it", session=DESKTOP_A)
    thread = board.get(url)
    assert (
        "mark-read" in thread.text and "On it" in thread.text and "Mark replies read" in thread.text
    )

    dialog = board.get(f"/p/acme-shop/t/{task.key}/dialog")
    assert "comment-form" in dialog.text and "On it" in dialog.text
    page = board.get(f"/t/{task.key}")
    assert "comment-form" in page.text and "On it" in page.text
    inbox = board.get("/inbox")
    assert 'id="inbox-replies"' in inbox.text and "On it" in inbox.text

    read = board.post(f"{url}/read", data={"csrf_token": csrf_token(token)}, headers=HTMX)
    assert read.status_code == 200 and "Mark replies read" not in read.text
    assert 'id="inbox-replies"' not in board.get("/inbox").text

    empty = board.post(url, data={"body": " ", "csrf_token": csrf_token(token)}, headers=HTMX)
    assert "must not be empty" in empty.text and "X-Relaybay-Comment" not in empty.headers
    plain = board.post(url, data=form, follow_redirects=False, headers={"Origin": BASE})
    assert plain.status_code == 303 and plain.headers["location"] == f"/t/{task.key}"


def test_comment_events_reach_the_board_stream(board: TestClient) -> None:
    service: Service = board.app.state.service
    task = run_in_app(board, linked_task, service)
    run_in_app(board, service.add_owner_comment, task.key, "live")
    run_in_app(board, fire, service, "PostToolUse")
    run_in_app(board, service.ack_comments, task=task.key)
    activity = board.get("/activity").text
    for kind in ("comment.added", "comment.delivered", "comment.read"):
        assert kind in activity


# --- MCP -------------------------------------------------------------------------


async def test_mcp_comment_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop")
        created = await call(client, "task_create", project="acme-shop", title="Checkout")
        key = created.split("**")[1]
        assert "No comments" in await call(client, "comments_list", task=key)
        assert "Nothing to acknowledge" in await call(client, "comment_ack", task=key)
        reply = await call(client, "comment_add", task=key, body="Question?", session="dev-1")
        assert f"to **{key}**" in reply
        listed = await call(client, "comments_list", task=key)
        assert "dev-1" in listed and "Question?" in listed and "unread by the owner" in listed
        result = await client.call_tool("comment_ack", {})
        assert result.is_error
        result = await client.call_tool("comment_ack", {"comment_id": 1})
        assert result.is_error  # an agent's own reply is not an owner comment

        # hook_session: the caller's own id only; a taken id is kept, bind never links one.
        await call(
            client,
            "report_add",
            task=key,
            body="changed · S · a",
            session="dev-1",
            hook_session="cc-dev-1",
        )
        taken = await call(
            client,
            "report_add",
            task=key,
            body="changed · S · b",
            session="dev-2",
            hook_session="cc-dev-1",
        )
        assert "Warning: hook_session cc-dev-1 belongs to another session (dev-1)" in taken
        bound = await call(
            client, "task_bind_session", task=key, session="dev-2", hook_session="cc-dev-2"
        )
        assert "Note: hook_session is not linked by task_bind_session" in bound
